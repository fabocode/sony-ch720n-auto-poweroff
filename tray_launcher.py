#!/usr/bin/env python3
"""Watchdog launcher: keeps the tray app running 24/7.

It runs tray_app.py and, if it ever dies unexpectedly (e.g. a native COM fault
that Python can't catch), relaunches it after a short backoff. When the tray app
exits cleanly — exit code 0, i.e. the user chose Quit, or another instance is
already running — the watchdog stops too.

The "Start with Windows" shortcut and Start-Tray.cmd point here so a crash
self-recovers without user intervention.
"""
import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PYW = HERE / ".venv" / "Scripts" / "pythonw.exe"
PY = HERE / ".venv" / "Scripts" / "python.exe"
TRAY = HERE / "tray_app.py"
LOG = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "sony-ch720n-tray.log"

MUTEX_NAME = "Global\\SonyCH720NTrayLauncher"
ERROR_ALREADY_EXISTS = 183
BACKOFF_SECONDS = 5
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  [launcher] {msg}\n")
    except OSError:
        pass


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def main() -> int:
    if not single_instance():
        return 0  # another watchdog already supervising
    exe = str(PYW if PYW.exists() else PY)
    crashes = 0
    while True:
        rc = subprocess.run([exe, str(TRAY)], creationflags=CREATE_NO_WINDOW).returncode
        if rc == 0:
            return 0  # clean quit (user) or another instance owns the tray
        crashes += 1
        log(f"tray exited with code {rc}; relaunching in {BACKOFF_SECONDS}s "
            f"(crash #{crashes}).")
        time.sleep(BACKOFF_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
