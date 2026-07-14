@echo off
setlocal
title FrameFinder Launcher
cd /d "%~dp0"

echo Starting FrameFinder. Please wait...
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_ui.ps1"
set "START_RESULT=%ERRORLEVEL%"

if not "%START_RESULT%"=="0" (
    echo.
    echo [FAILED] Make sure Docker Desktop is running, then try again.
    echo If it still fails, send the error shown above for diagnosis.
    echo.
    pause
    exit /b %START_RESULT%
)

echo.
echo [READY] The web page has been opened in your browser.
exit /b 0
