@echo off
chcp 65001 > nul
cd /d "%~dp0..\.."

set "VENV_PYTHON=.venv\Scripts\python.exe"
set "REBUILD_SCRIPT=scripts\migrations\v2\rebuild_v2_successor.py"

if not exist "%VENV_PYTHON%" (
    echo .venv를 찾을 수 없습니다.
    echo 먼저 RUN_QUICKSTART.bat을 실행해 설치와 초기 설정을 완료하세요.
    echo.
    exit /b 1
)

if not exist ".env" (
    echo .env를 찾을 수 없습니다.
    echo 먼저 RUN_QUICKSTART.bat을 실행해 OpenRouter 설정을 완료하세요.
    echo.
    exit /b 1
)

if /I "%~1"=="--check" goto run_direct
if /I "%~1"=="--help" goto run_direct

echo Finance LLM을 실행 중인 창과 데이터 업데이트 작업을 먼저 종료하세요.
echo.
echo 이 작업은 다음 순서로 진행됩니다.
echo   1. 현재 V2와 설정된 추출 프로필 확인
echo   2. 전체 PDF를 별도 successor snapshot으로 파싱 및 임베딩
echo      - PyMuPDF와 fallback이 모두 실패한 PDF는 실패 상태로 기록 후 계속
echo   3. 문서 수, 프로필, snapshot 무결성 검증
echo   4. 성공한 경우에만 새 snapshot 활성화
echo.
echo 기존 data\reports.db와 data\downloaded PDF는 삭제하지 않습니다.
echo 실패하면 현재 활성 V2 snapshot이 계속 사용됩니다.
echo 개별 PDF 파싱 실패는 관리 페이지에서 나중에 다시 시도할 수 있습니다.
echo OpenRouter 임베딩 API 호출로 시간과 비용이 발생할 수 있습니다.
echo.

choice /C YN /N /M "V2 전체 successor 재생성을 진행할까요? [Y/N] "
if errorlevel 2 goto cancelled

"%VENV_PYTHON%" "%REBUILD_SCRIPT%" --yes %*
set "EXIT_CODE=%errorlevel%"

echo.
if "%EXIT_CODE%"=="0" (
    echo V2 successor 작업이 정상적으로 끝났습니다.
) else (
    echo V2 successor 작업이 실패하거나 중단되었습니다.
    echo 기존 활성 snapshot과 위 오류 메시지를 확인하세요.
)
echo Press any key to close this window.
pause > nul
exit /b %EXIT_CODE%

:run_direct
"%VENV_PYTHON%" "%REBUILD_SCRIPT%" %*
exit /b %errorlevel%

:cancelled
echo.
echo 취소했습니다. 아무 데이터도 변경하지 않았습니다.
exit /b 0
