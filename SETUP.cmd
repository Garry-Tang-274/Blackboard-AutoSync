@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0setup.ps1"
pause
