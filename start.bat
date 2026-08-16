@echo off
setlocal enabledelayedexpansion
title DoiHarvest - Paper Download Pipeline

rem ============================================================
rem  DoiHarvest Launcher (start.bat)
rem  优先使用 install.py 创建的 .venv，其次使用系统 Python。
rem ============================================================

pushd "%~dp0"

set "PYTHON="

rem 1. 项目虚拟环境（install.py 创建）
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%~dp0.venv\Scripts\python.exe"
    goto found_python
)

rem 2. 系统 Python
where python >nul 2>&1
if !errorlevel! equ 0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYTHON set "PYTHON=%%P"
    )
)
if defined PYTHON goto found_python

rem -- 没有找到 Python
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
