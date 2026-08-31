@echo off
setlocal
title Live Highlight Console
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_console.ps1"
set "APP_EXIT=%ERRORLEVEL%"
if not "%APP_EXIT%"=="0" (
  echo.
  echo Startup failed. See startup-error.log in this folder.
  echo Keep this window open and send that log file for diagnosis.
  pause
)
exit /b %APP_EXIT%
