@echo off
rem OpenBox launcher for Windows: double-clickable wrapper around openbox.ps1.
setlocal
set "OPENBOX_LAUNCHER=%~dp0openbox.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%OPENBOX_LAUNCHER%" %*
exit /b %ERRORLEVEL%
