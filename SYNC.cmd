@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -File "%~dp0run.ps1" -Mode sync
if errorlevel 1 pause
