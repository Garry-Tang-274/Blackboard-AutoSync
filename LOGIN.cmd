@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0run.ps1" -Mode login
pause
