@echo off
chcp 65001 > nul
cd /d "%~dp0"

set "PYTHON_CMD="
where python >nul 2>nul
if %errorlevel%==0 set "PYTHON_CMD=python"

if "%PYTHON_CMD%"=="" (
    where py >nul 2>nul
    if %errorlevel%==0 set "PYTHON_CMD=py -3"
)

if "%PYTHON_CMD%"=="" (
    echo Python was not found.
    echo Install Python 3.10 or newer, then run RUN_QUICKSTART.bat again.
    echo Download: https://www.python.org/downloads/
    echo.
    if /I "%~1"=="--runtime-smoke" exit /b 1
    pause
    exit /b 1
)

echo Starting Finance LLM Quick Start...
%PYTHON_CMD% quickstart.py %*
set "EXIT_CODE=%errorlevel%"
if /I "%~1"=="--runtime-smoke" exit /b %EXIT_CODE%

echo.
if not "%EXIT_CODE%"=="0" echo Quick Start exited with error code %EXIT_CODE%.
echo Press any key to close this window.
pause > nul
exit /b %EXIT_CODE%
