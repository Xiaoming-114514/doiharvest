@echo off
setlocal enabledelayedexpansion
title DoiHarvest - Paper Download Pipeline

rem ============================================================
rem  DoiHarvest Launcher
rem  1. Use project virtual env (.venv created by install.py)
rem  2. Fall back to system Python
rem  NOTE: keep this file ASCII-only. Chinese chars break cmd.exe
rem        on GBK codepage because the file is saved as UTF-8.
rem ============================================================

pushd "%~dp0"

set "PYTHON="

rem 1. Project venv (created by install.py)
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python.exe"
    goto found_python
)

rem 2. System Python (skip Microsoft Store placeholder WindowsApps\python*.exe)
where python >nul 2>&1
if !errorlevel! equ 0 (
    for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i "WindowsApps"') do (
        if not defined PYTHON set "PYTHON=%%P"
    )
)
if defined PYTHON goto found_python

rem -- Python not found
echo [ERROR] Python not found on this system.
echo.
echo Please run install.bat first, or install Python 3.10-3.12:
echo   https://www.python.org/downloads/
echo   (IMPORTANT: check "Add Python to PATH" during install)
echo.
pause
popd
exit /b 1

:found_python
echo [*] Python: %PYTHON%
echo [*] Starting DoiHarvest...
echo.
"%PYTHON%" start.py %*
set EXITCODE=%errorlevel%

if %EXITCODE% neq 0 (
    echo.
    echo [ERROR] Launcher exited with code %EXITCODE%
    pause
)

popd
endlocal
