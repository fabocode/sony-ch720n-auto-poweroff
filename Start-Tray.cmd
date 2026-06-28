@echo off
rem Launch the watchdog (which runs the tray app and relaunches it on crash),
rem hidden (no console), using the uv virtualenv.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0tray_launcher.py"
