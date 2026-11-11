#!/usr/bin/env python3
"""Watchdog launcher (Windows): keeps the tray app running 24/7.

It runs tray_app.py and, if it ever dies unexpectedly (e.g. a native COM fault
that Python can't catch), relaunches it after a short backoff. When the tray app
exits cleanly — exit code 0, i.e. the user chose Quit, or another instance is
already running — the watchdog stops too.

The "Start with Windows" shortcut and Start-Tray.cmd point here so a crash
self-recovers without user intervention.

macOS does not use this file: the LaunchAgent's KeepAlive does the same job
(see platform_glue.launch_agent_plist).
"""
import subprocess
import sys
import time
from datetime import datetime

import platform_glue as glue

INSTANCE_NAME = "SonyCH720NTrayLauncher"
BACKOFF_SECONDS = 5


def log(msg: str) -> None:
    try:
        with open(glue.LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  [launcher] {msg}\n")
    except OSError:
        pass


def main() -> int:
    if not glue.IS_WINDOWS:
        print("tray_launcher.py is for Windows. On macOS run start-tray.command, "
              "or turn on 'Start at login' (launchd restarts the app after a crash).",
              file=sys.stderr)
        return 2
    _instance = glue.acquire_single_instance(INSTANCE_NAME)
    if _instance is None:
        return 0  # another watchdog already supervising
    exe = glue.venv_python(gui=True)
    crashes = 0
    while True:
        rc = subprocess.run([exe, str(glue.TRAY)],
                            creationflags=glue.NO_WINDOW_FLAGS).returncode
        if rc == 0:
            return 0  # clean quit (user) or another instance owns the tray
        crashes += 1
        log(f"tray exited with code {rc}; relaunching in {BACKOFF_SECONDS}s "
            f"(crash #{crashes}).")
        time.sleep(BACKOFF_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
