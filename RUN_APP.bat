@echo off
chcp 65001 > nul
cd /d "%~dp0"

set "VENV_PYTHON=.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo .venv was not found.
    echo Run RUN_QUICKSTART.bat first to install dependencies and prepare data.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo .env was not found.
    echo Run RUN_QUICKSTART.bat first to create local configuration.
    echo.
    pause
    exit /b 1
)

echo Starting Finance LLM Streamlit app...
"%VENV_PYTHON%" -m streamlit run apps/gui/app.py
set "EXIT_CODE=%errorlevel%"

echo.
if not "%EXIT_CODE%"=="0" echo Streamlit app exited with error code %EXIT_CODE%.
echo Press any key to close this window.
pause > nul
exit /b %EXIT_CODE%
