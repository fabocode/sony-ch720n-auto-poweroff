# Sony WH-CH720N — audio-idle auto power-off (Windows 11)

Powers off Sony WH-CH720N headphones after X minutes **without audio**, the way an
Xbox Wireless Headset does.

**The problem:** the Sony's built-in auto-off counts time *without a Bluetooth
connection*, not *without audio*. Windows keeps the connection alive while the
headphones are the active audio output (even when nothing is playing), so they
never power off on their own.

**The solution:** monitor the Core Audio peak meter and, after X minutes of
silence, **send the Sony protocol "power off" command** over RFCOMM. Real,
immediate power-off — no admin required.

## Components

| File | Role |
|---|---|
| `sony_poweroff.py` | Sends the Sony power-off command over RFCOMM (Python + WinRT). **No admin needed.** |
| `AutoDisconnect-CH720N.ps1` | Daemon: monitors audio and, after the idle threshold, calls `sony_poweroff.py`. |
| `pyproject.toml` / `uv.lock` | Python project + locked dependencies (managed with [uv](https://docs.astral.sh/uv/)). |

## Requirements

1. **Python 3.13+** and **[uv](https://docs.astral.sh/uv/)** (`pip install uv`, or the standalone installer).
2. Install dependencies into a local virtualenv:
   ```powershell
   uv sync
   ```
3. Windows: **Settings → Privacy & security → Bluetooth** → "Let apps access
   Bluetooth" = **ON** (required for the RFCOMM path).

## Usage

**Power off the headphones now (one-shot):**
```powershell
uv run sony_poweroff.py            # power off
uv run sony_poweroff.py --dry-run  # INIT handshake without powering off (safe test)
uv run sony_poweroff.py --verbose  # log every TX/RX frame
```
Exit codes: `0` = power-off sent and acknowledged; non-zero = failure
(headphones disconnected, Bluetooth app-access off, etc.).

**Daemon (continuous monitoring):**
```powershell
powershell -ExecutionPolicy Bypass -File .\AutoDisconnect-CH720N.ps1 -IdleMinutes 10
```
The daemon automatically uses the uv virtualenv's Python (`.venv\Scripts\python.exe`).
Useful parameters: `-IdleMinutes`, `-PollSeconds`, `-SilenceThreshold`,
`-DeviceNameMatch`, `-PythonExe`, `-PowerOffScript`, `-NoFallback`, `-ShowPeak`, `-LogPath`.
Default log: `%LOCALAPPDATA%\AutoDisconnect-CH720N.log`.

See the live audio measurement that drives the decision:
```powershell
powershell -ExecutionPolicy Bypass -File .\AutoDisconnect-CH720N.ps1 -ShowPeak -PollSeconds 2
```
(`state` shows `silence`/`AUDIO`; the `idle` counter only advances during silence
and resets to `0` the instant any audio plays.)

## How it works

1. **Primary — RFCOMM power-off (no admin):** `sony_poweroff.py` connects to
   Sony's proprietary RFCOMM service (V2 UUID `956C7B26-…`), performs the INIT
   handshake, and sends `COMMAND_1` with payload `24 03 01` (`POWER_SET`). The
   headphones power off immediately. Since they end up off, nothing needs to be
   "re-armed".
2. **Fallback — PnP disconnect (needs admin):** if the power-off fails, the
   daemon disables the parent Bluetooth node (`Disable-PnpDevice`); on losing the
   connection the Sony starts its own ~15-minute timer and powers off, and the
   daemon re-enables the node after `-ReArmAfterMinutes`. Disable with `-NoFallback`.

### Sony protocol (credit: Gadgetbridge)

Framing, checksum, escaping, and opcodes come from the open-source
[Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge) project
(`service/devices/sony/headphones/protocol/*`). Frame:

```
0x3E | type | seq | length(4B big-endian) | payload | checksum(1B) | 0x3C
  checksum = sum(type,seq,length,payload) & 0xFF
  escape   = 0x3E/0x3C/0x3D  ->  0x3D, (byte & 0xEF)
  seq      = single bit 0/1; adopted from the device's ACK (becomes 1 after INIT)
```

**Verified on real hardware:** the WH-CH720N speaks **Protocol V2** and powers
fully off with `POWER_SET 24 03 01`.

## Deferred

- A **Task Scheduler session-unlock trigger** to re-enable the node on return is
  only useful if you rely on the *disconnect fallback* (the primary power-off
  path has no "blind window" — the headphones simply turn off).
