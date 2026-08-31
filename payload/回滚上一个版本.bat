@echo off
setlocal
title Live Highlight Rollback
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0rollback_update.ps1"
set "ROLLBACK_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %ROLLBACK_EXIT%
