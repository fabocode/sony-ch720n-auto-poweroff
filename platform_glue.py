"""OS-specific bits for the tray app and the watchdog: paths, single instance,
opening files, start at login, and running UI code on the right thread."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

TRAY = HERE / "tray_app.py"
LAUNCHER = HERE / "tray_launcher.py"      # Windows watchdog that autostart points at

# Hide child consoles on Windows; must be 0 elsewhere.
NO_WINDOW_FLAGS = 0x08000000 if IS_WINDOWS else 0   # CREATE_NO_WINDOW

LAUNCH_AGENT_LABEL = "com.fabocode.sony-ch720n-tray"

if IS_WINDOWS:
    _LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    LOG = _LOCALAPPDATA / "sony-ch720n-tray.log"
    CONFIG = _LOCALAPPDATA / "sony-ch720n-tray.json"
    STARTUP_LNK = (Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" /
                   "Windows" / "Start Menu" / "Programs" / "Startup" / "SonyCH720NTray.lnk")
    AUTOSTART_LABEL = "Start with Windows"
else:
    LOG = Path.home() / "Library" / "Logs" / "sony-ch720n-tray.log"
    CONFIG = Path.home() / "Library" / "Application Support" / "sony-ch720n-tray.json"
    LOCK_DIR = Path.home() / "Library" / "Application Support"
    LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
    AUTOSTART_LABEL = "Start at login"


def venv_python(gui: bool = False) -> str:
    """Python of the uv virtualenv (the windowless one on Windows when `gui`),
    or the current interpreter when there is no venv."""
    if IS_WINDOWS:
        scripts = HERE / ".venv" / "Scripts"
        for candidate in ([scripts / "pythonw.exe"] if gui else []) + [scripts / "python.exe"]:
            if candidate.exists():
                return str(candidate)
    else:
        candidate = HERE / ".venv" / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    return sys.executable


# --- single instance -----------------------------------------------------------
_ERROR_ALREADY_EXISTS = 183


def acquire_single_instance(name: str):
    """Return a handle to keep alive while running, or None if another
    instance already holds `name`."""
    if IS_WINDOWS:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\{name}")
        if ctypes.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            return None
        return h
    import fcntl
    path = LOCK_DIR / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f                    # the lock lives as long as this file stays open


# --- files / UI ------------------------------------------------------------------
def open_file(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606
    else:
        subprocess.run(["open", str(path)], check=False)


def prepare_ui() -> None:
    """macOS: run as a menu-bar app (no Dock icon). Call before creating the icon."""
    if IS_MACOS:
        import AppKit
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)


def run_on_ui(fn) -> None:
    """Run `fn` on the UI thread. AppKit must be used from the main thread;
    Windows' pystray backend is safe to call from any thread."""
    if IS_MACOS:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn)
    else:
        fn()


# --- start at login ------------------------------------------------------------
def is_autostart() -> bool:
    return STARTUP_LNK.exists() if IS_WINDOWS else LAUNCH_AGENT.exists()


def set_autostart(enable: bool) -> None:
    if IS_WINDOWS:
        _set_autostart_windows(enable)
    else:
        _set_autostart_macos(enable)


def _set_autostart_windows(enable: bool) -> None:
    if not enable:
        STARTUP_LNK.unlink(missing_ok=True)
        return
    target = venv_python(gui=True)
    STARTUP_LNK.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "$ws = New-Object -ComObject WScript.Shell; "
         f"$s = $ws.CreateShortcut('{STARTUP_LNK}'); "
         f"$s.TargetPath = '{target}'; "
         f"$s.Arguments = '\"{LAUNCHER}\"'; "
         f"$s.WorkingDirectory = '{HERE}'; "
         "$s.Save()"],
        creationflags=NO_WINDOW_FLAGS,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch_agent_plist() -> dict:
    """LaunchAgent for the tray app. launchd is the watchdog: KeepAlive restarts
    it after a crash (non-zero exit) but not after Quit (exit 0)."""
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [venv_python(), str(TRAY)],
        "WorkingDirectory": str(HERE),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG.with_suffix(".stdout.log")),
        "StandardErrorPath": str(LOG.with_suffix(".stderr.log")),
    }


def _set_autostart_macos(enable: bool) -> None:
    # Only the plist file changes here; launchd reads it at the next login.
    # (No launchctl bootout: if this tray is the launchd job, that would kill it.)
    if not enable:
        LAUNCH_AGENT.unlink(missing_ok=True)
        return
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(launch_agent_plist(), f)
