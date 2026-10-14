@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0schedule.ps1" -Action status
if exist "%~dp0reports\latest.html" start "" "%~dp0reports\latest.html"
pause
