@echo off
chcp 65001 >nul
title DoiHarvest Installer
echo ============================================
echo   DoiHarvest - One-Click Installer
echo ============================================
echo.

where python >nul 2>nul
if %errorlevel%==0 (
    python install.py
) else (
    echo [ERROR] Python not found.
    echo Please install Python 3.10-3.12 first:
    echo   https://www.python.org/downloads/
    echo.
    echo IMPORTANT: check "Add Python to PATH" during install.
    echo.
)

pause
