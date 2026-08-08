@echo off
chcp 65001 > nul
cd /d "%~dp0"

set "VENV_PYTHON=.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo .venv를 찾을 수 없습니다.
    echo 먼저 RUN_QUICKSTART.bat을 실행해 설치를 완료하세요.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo .env를 찾을 수 없습니다.
    echo 먼저 RUN_QUICKSTART.bat을 실행해 초기 설정을 완료하세요.
    echo.
    pause
    exit /b 1
)

echo Finance LLM의 기존 GUI와 CLI 창을 모두 닫아 주세요.
echo 기존 청크와 FAISS 벡터를 재사용하며 전체 재임베딩은 하지 않습니다.
echo 성공 후 V1 reports.db와 vector_db는 삭제됩니다.
echo downloaded PDF는 데이터 업데이트와 재구축을 위해 유지됩니다.
echo.

"%VENV_PYTHON%" scripts\migrations\v2\migrate_v2_user.py %*
set "EXIT_CODE=%errorlevel%"

echo.
@if "%EXIT_CODE%"=="0" (
    echo Native V2 마이그레이션이 정상적으로 끝났습니다.
) else (
    echo 마이그레이션이 중단되었습니다. 위 오류를 확인하세요.
)
echo Press any key to close this window.
pause > nul
exit /b %EXIT_CODE%
