<#
.SYNOPSIS
    Powers off Bluetooth headphones after AUDIO inactivity on Windows 11.

.DESCRIPTION
    Windows keeps the Bluetooth connection alive while the headphones are the
    active audio output, even when nothing is playing, so the Sony's own
    auto-off (which counts time WITHOUT a CONNECTION) never fires.

    This script monitors the playback endpoint's peak meter. If there is no
    audio for -IdleMinutes, it POWERS OFF the headphones.

    PRIMARY mechanism: sends the Sony protocol "power off" command over RFCOMM
    (sony_poweroff.py). Real, immediate power-off; does NOT require admin.

    FALLBACK: if the power-off fails, it disables the device's parent PnP node
    (Disable-PnpDevice, requires admin). On losing the connection the Sony
    starts its own timer (~15 min) and powers off; the script then re-enables
    the node after -ReArmAfterMinutes.

.NOTES
    - The primary path (RFCOMM power-off) needs NO elevation; it does need the
      Windows "Let apps access Bluetooth" privacy toggle ON.
    - The FALLBACK (Disable/Enable-PnpDevice) DOES require running ELEVATED.
    - Test interactively before scheduling it as a task.
#>

[CmdletBinding()]
param(
    # Substring the audio endpoint name must contain (case-insensitive).
    [string]$DeviceNameMatch = "CH720N",

    # Minutes of continuous silence before powering off.
    [int]$IdleMinutes = 10,

    # How often (seconds) audio is sampled.
    [int]$PollSeconds = 15,

    # Peak level below which audio is considered "silence".
    [single]$SilenceThreshold = 0.0005,

    # Minutes to wait (fallback path) after disconnecting before re-enabling the
    # node. Must exceed the Sony's auto-off (~15 min) so it does NOT reconnect.
    [int]$ReArmAfterMinutes = 17,

    # Log file (optional).
    [string]$LogPath = "$env:LOCALAPPDATA\AutoDisconnect-CH720N.log",

    # Python executable for the RFCOMM power-off (empty = uv's .venv, else system python).
    [string]$PythonExe = "",

    # Python script that sends the Sony power-off (empty = next to this .ps1).
    [string]$PowerOffScript = "",

    # If the power-off fails, do NOT fall back to disconnecting (Disable-PnpDevice).
    [switch]$NoFallback,

    # Log the audio peak and idle counter on every poll (diagnostic).
    [switch]$ShowPeak
)

# --- Core Audio interop: read an endpoint's peak level by name ------------------
$cs = @"
using System;
using System.Runtime.InteropServices;

namespace AudioMon {
  [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator { }

  [ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int stateMask, out IMMDeviceCollection devices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice device);
  }

  [ComImport, Guid("0BD7A1BE-7A1A-44DB-8397-CC5392387B5E"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDeviceCollection {
    int GetCount(out int count);
    int Item(int index, out IMMDevice device);
  }

  [ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDevice {
    int Activate(ref Guid iid, int clsctx, IntPtr activationParams,
                 [MarshalAs(UnmanagedType.IUnknown)] out object iface);
    int OpenPropertyStore(int access, out IPropertyStore props);
    int GetId(out IntPtr id);
    int GetState(out int state);
  }

  [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPropertyStore {
    int GetCount(out int count);
    int GetAt(int index, out PROPERTYKEY key);
    int GetValue(ref PROPERTYKEY key, out PROPVARIANT value);
    int SetValue(ref PROPERTYKEY key, ref PROPVARIANT value);
    int Commit();
  }

  [ComImport, Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioMeterInformation {
    int GetPeakValue(out float peak);
  }

  [StructLayout(LayoutKind.Sequential)]
  struct PROPERTYKEY { public Guid fmtid; public int pid; }

  [StructLayout(LayoutKind.Explicit)]
  struct PROPVARIANT {
    [FieldOffset(0)] public short vt;
    [FieldOffset(8)] public IntPtr pwszVal;
  }

  public static class Meter {
    static Guid IID_IAudioMeter =
      new Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064");
    static PROPERTYKEY PKEY_FriendlyName = new PROPERTYKEY {
      fmtid = new Guid("a45c254e-df1c-4efd-8020-67d146a850e0"), pid = 14 };

    // Returns the peak [0..1]; -1 if no endpoint is active (disconnected/off).
    public static float GetPeak(string nameMatch) {
      var en = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
      IMMDeviceCollection col;
      en.EnumAudioEndpoints(0, 0x1, out col); // eRender, DEVICE_STATE_ACTIVE
      int count; col.GetCount(out count);
      float best = -1f; // -1 = no endpoint of this device is active
      for (int i = 0; i < count; i++) {
        IMMDevice dev; col.Item(i, out dev);
        IPropertyStore store; dev.OpenPropertyStore(0, out store);
        PROPVARIANT pv; store.GetValue(ref PKEY_FriendlyName, out pv);
        string name = pv.pwszVal != IntPtr.Zero
          ? Marshal.PtrToStringUni(pv.pwszVal) : "";
        if (!string.IsNullOrEmpty(name) &&
            name.IndexOf(nameMatch, StringComparison.OrdinalIgnoreCase) >= 0) {
          object o; Guid iid = IID_IAudioMeter;
          dev.Activate(ref iid, 1, IntPtr.Zero, out o); // CLSCTX_INPROC_SERVER
          var meter = (IAudioMeterInformation)o;
          float peak; meter.GetPeakValue(out peak);
          if (peak > best) best = peak; // max peak across Headphones/Headset
        }
      }
      return best;
    }
  }
}
"@

if (-not ("AudioMon.Meter" -as [type])) {
    Add-Type -TypeDefinition $cs -Language CSharp
}

# --- Helpers --------------------------------------------------------------------
function Write-Log {
    param([string]$Message)
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    if ($LogPath) { Add-Content -Path $LogPath -Value $line -ErrorAction SilentlyContinue }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-PowerOff {
    # Primary mechanism: send the Sony RFCOMM power-off command.
    # Returns $true if sony_poweroff.py exits with code 0.
    # Uses Start-Process with file redirection: captures raw text and avoids the
    # NativeCommandError that PowerShell 5.1 wraps around native stderr.
    if (-not (Test-Path $PowerOffScript)) {
        Write-Log "ERROR: sony_poweroff.py not found at '$PowerOffScript'."
        return $false
    }
    $o = [System.IO.Path]::GetTempFileName()
    $e = [System.IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath $PythonExe `
            -ArgumentList ('"{0}" --verbose' -f $PowerOffScript) `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $o -RedirectStandardError $e
        $code = $proc.ExitCode
    } catch {
        Write-Log "ERROR running Python: $($_.Exception.Message)"
        Remove-Item $o, $e -ErrorAction SilentlyContinue
        return $false
    }
    foreach ($l in @(Get-Content $o, $e -ErrorAction SilentlyContinue)) {
        if ($l) { Write-Log "  [py] $l" }
    }
    Remove-Item $o, $e -ErrorAction SilentlyContinue
    if ($code -eq 0) { return $true }
    Write-Log "RFCOMM power-off failed (exit=$code)."
    return $false
}

function Get-HeadphoneNodes {
    # Only the PARENT Bluetooth node (InstanceId BTHENUM\DEV_<MAC>), not the
    # per-profile child nodes (A2DP/HFP/AVRCP) or the audio endpoints.
    # Disabling this single node cuts the WHOLE connection at once, which is what
    # makes the Sony register "no connection" and start its own power-off timer.
    Get-PnpDevice -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FriendlyName -like "*$DeviceNameMatch*" -and
            $_.InstanceId  -like "BTHENUM\DEV_*"        -and
            $_.Status -eq 'OK'
        }
}

function Disconnect-Headphones {
    $nodes = Get-HeadphoneNodes
    if (-not $nodes) { Write-Log "No active nodes to disconnect."; return @() }
    $ids = @()
    foreach ($n in $nodes) {
        try {
            Disable-PnpDevice -InstanceId $n.InstanceId -Confirm:$false -ErrorAction Stop
            $ids += $n.InstanceId
            Write-Log "Disabled: $($n.FriendlyName)  [$($n.InstanceId)]"
        } catch {
            Write-Log "ERROR disabling $($n.FriendlyName): $($_.Exception.Message)"
        }
    }
    return $ids
}

function Reconnect-Headphones {
    param([string[]]$InstanceIds)
    foreach ($id in $InstanceIds) {
        try {
            Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
            Write-Log "Re-enabled: $id"
        } catch {
            Write-Log "ERROR re-enabling ${id}: $($_.Exception.Message)"
        }
    }
}

# --- Main loop ------------------------------------------------------------------
# Resolve paths here (NOT in the param defaults: $PSScriptRoot is empty during
# parameter binding when the script is invoked with -File).
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $PowerOffScript) {
    $PowerOffScript = Join-Path $scriptDir "sony_poweroff.py"
}
if (-not $PythonExe) {
    # Prefer uv's project virtualenv; fall back to system python.
    $venvPython = Join-Path $scriptDir ".venv\Scripts\python.exe"
    $PythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }
}

Write-Log "Started. Target='$DeviceNameMatch'  Idle=$IdleMinutes min  Poll=$PollSeconds s"
$adminTxt = if (Test-Admin) { "elevated" } else { "non-elevated (disconnect fallback unavailable)" }
Write-Log "Mechanism: RFCOMM power-off via '$PythonExe' '$PowerOffScript'. Session: $adminTxt."

$idleAccumSeconds = 0
$idleTarget = $IdleMinutes * 60

while ($true) {
    $peak = [AudioMon.Meter]::GetPeak($DeviceNameMatch)

    if ($peak -lt 0) {
        # Endpoint inactive => headphones disconnected/off. Nothing to do.
        $idleAccumSeconds = 0
    }
    elseif ($peak -lt $SilenceThreshold) {
        $idleAccumSeconds += $PollSeconds
        if ($idleAccumSeconds -ge $idleTarget) {
            Write-Log ("Inactivity reached ({0} min without audio). Powering off..." -f $IdleMinutes)
            $idleAccumSeconds = 0

            if (Invoke-PowerOff) {
                Write-Log "RFCOMM power-off OK. No re-arm needed (headphones already off)."
            }
            elseif ($NoFallback) {
                Write-Log "Power-off failed and -NoFallback is set. Not attempting disconnect."
            }
            elseif (-not (Test-Admin)) {
                Write-Log "Power-off failed. The fallback (Disable-PnpDevice) needs admin; run the script ELEVATED to enable it."
            }
            else {
                Write-Log "Power-off failed. Using fallback: disabling the parent PnP node..."
                $disabled = Disconnect-Headphones
                if ($disabled.Count -gt 0) {
                    Write-Log ("Waiting {0} min to re-arm (let the Sony power off first)..." -f $ReArmAfterMinutes)
                    Start-Sleep -Seconds ($ReArmAfterMinutes * 60)
                    Reconnect-Headphones -InstanceIds $disabled
                    Write-Log "Re-armed. Ready for the next reconnection."
                }
            }
        }
    }
    else {
        # Audio is playing -> reset the counter.
        $idleAccumSeconds = 0
    }

    if ($ShowPeak) {
        $state = if ($peak -lt 0) { "inactive" } elseif ($peak -lt $SilenceThreshold) { "silence" } else { "AUDIO  " }
        Write-Log ("peak={0:F4}  state={1}  idle={2}/{3}s" -f $peak, $state, $idleAccumSeconds, $idleTarget)
    }

    Start-Sleep -Seconds $PollSeconds
}
