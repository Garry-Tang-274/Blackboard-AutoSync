@echo off
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" powershell.exe -NoProfile -File "%~dp0setup.ps1"
if not exist "%~dp0.venv\Scripts\pythonw.exe" exit /b 1
start "Blackboard course selector" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0course_selector.py"
