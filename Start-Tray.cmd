@echo off
rem Launch the system-tray app hidden (no console) using the uv virtualenv.
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0tray_app.py"
