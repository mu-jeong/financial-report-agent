@echo off
chcp 65001 > nul
cd /d "%~dp0"

set "VENV_PYTHON=.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo .venv를 찾을 수 없습니다.
    echo 먼저 RUN_QUICKSTART.bat을 실행해 설치와 초기 설정을 완료하세요.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo .env를 찾을 수 없습니다.
    echo 먼저 RUN_QUICKSTART.bat을 실행해 OpenRouter 설정을 완료하세요.
    echo.
    pause
    exit /b 1
)

echo Finance LLM을 실행 중이라면 해당 창을 닫아 주세요.
echo V1 원본은 삭제하지 않으며, 쓰기 가능한 V2 검증에 실패하면 활성화하지 않습니다.
echo.

"%VENV_PYTHON%" scripts\migrations\v2\migrate_v2_user.py %*
set "EXIT_CODE=%errorlevel%"

echo.
@if "%EXIT_CODE%"=="0" (
    echo 쓰기 가능한 V2 마이그레이션이 정상적으로 끝났습니다.
) else (
    echo 마이그레이션이 중단되었습니다. V1 원본과 실패 기록을 확인하세요.
)
echo Press any key to close this window.
pause > nul
exit /b %EXIT_CODE%
