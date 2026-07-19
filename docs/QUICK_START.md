# Quick Start

개발 명령어를 몰라도 처음에는 `RUN_QUICKSTART.bat` 파일 하나만 더블클릭하면 설치, OpenRouter API 키 설정, 실행일 포함 이전 7일 범위(총 최대 8일)의 데이터 준비, 검색 인덱스 생성, 웹 화면 실행까지 자동으로 진행됩니다.

초기 준비가 끝난 뒤 앱만 다시 열 때는 `RUN_APP.bat`을 사용하세요. 이 파일은 설치·수집·임베딩을 반복하지 않고 Streamlit GUI만 실행합니다.

기존 V1 데이터의 V2 변환은 `MIGRATE_V2.bat`을 더블클릭합니다. 이 작업은 V1 원본을 유지한 채 새 리포트를 포함한 전체 corpus 재임베딩과 실제 GUI 실행 검사를 통과한 경우에만 쓰기 가능한 V2를 활성화합니다. 새 리포트가 없으면 V1을 그대로 유지하므로 나중에 같은 파일을 다시 실행하면 됩니다. 자세한 내용은 [일반 사용자용 V2 마이그레이션](migrations/v2/V2_MIGRATION_USER.md)을 참고하세요.

## 1. 실행 방법

1. 이 프로젝트 폴더를 엽니다.
2. `RUN_QUICKSTART.bat`을 더블클릭합니다.
3. 처음 실행할 때 OpenRouter API 키를 물어보면 붙여넣고 Enter를 누릅니다.
4. 설치와 데이터 준비가 끝나면 브라우저에서 Finance LLM 화면이 열립니다.

이후 일상적으로 앱만 실행할 때는 같은 폴더에서 `RUN_APP.bat`을 더블클릭합니다. `.venv` 또는 `.env`가 없다는 안내가 나오면 먼저 `RUN_QUICKSTART.bat`을 실행해 초기 준비를 완료하세요.

앱 실행 후 사이드바의 데이터 업데이트 영역에서는 업데이트할 카테고리(`company`, `industry`, `economy`)를 선택할 수 있습니다. 이미 `company` 데이터가 있는 날짜라도 `industry`를 선택하면 해당 날짜의 산업 리포트를 추가로 확인하고 임베딩할 수 있습니다.

## 2. OpenRouter API 키 준비

Finance LLM은 답변 생성과 임베딩에 OpenRouter API를 사용합니다.

API 키가 없다면 아래 문서를 먼저 따라 발급받으세요.

- [OpenRouter API 키 발급 방법](OPENROUTER_API_KEY.md)

## 3. Quick Start가 자동으로 하는 일

`RUN_QUICKSTART.bat`은 내부적으로 `quickstart.py`를 실행해 다음 작업을 순서대로 처리합니다.
터미널에는 각 단계가 끝날 때마다 `[진행]` 프로그레스 바가 표시되어 현재 준비 상태를 확인할 수 있습니다.

1. Python 버전 확인
2. `.env` 파일 생성 또는 업데이트와 OpenRouter API 키 저장
3. `.venv` 가상환경 생성 또는 확인
4. 실행 산출물 폴더 생성 또는 확인 (`logs/`, `data/`, `data/downloaded/`, `reports/`)
5. pip 업데이트
6. `requirements.txt` 패키지 설치 또는 확인
7. 실행일 포함 이전 7일 범위의 리포트 수집
8. 수집된 전체 리포트 임베딩과 FAISS 검색 인덱스 생성
9. 데이터 상태 출력
10. Streamlit GUI 실행

## 4. 데이터 준비 기준

Quick Start는 실행하는 날짜를 기준일로 삼아, 실행일과 그 이전 7일(총 최대 8일)의 리포트를 준비합니다.

예를 들어 `2026-06-03`에 실행하면 다음 범위를 사용합니다.

- 기준일: `2026-06-03`
- 수집 범위: `2026-05-27 ~ 2026-06-03`

Quick Start 실행 시 아래 설정이 자동으로 `.env`에 반영됩니다.

```env
CRAWLER_MODE=LATEST
CRAWLER_TARGET_DATE=<실행일 자동 입력>
CRAWLER_LOOKBACK_DAYS=7
CRAWLER_TARGET_COUNT=0
CRAWLER_MAX_LOOKBACK_DAYS=7
```

`CRAWLER_TARGET_COUNT=0`은 건수 제한 없이 해당 기간의 결과를 사용한다는 뜻입니다. 실제 다운로드 건수는 주말, 휴일, 증권사 게시 여부에 따라 달라질 수 있습니다.

## 5. 실행 후 예시 질문

브라우저가 열리면 아래 질문을 입력해보세요.

- 최근 리포트의 주요 투자 아이디어를 요약해줘.
- 특정 기업의 최근 리포트를 요약해줘.
- 최근 증권사 리포트 중 실적 개선이 기대되는 기업은?

## 6. 처음 실행이 오래 걸리는 이유

처음 실행할 때는 패키지 설치, PDF 다운로드, 텍스트 추출, 임베딩 생성이 함께 진행됩니다.
`RUN_QUICKSTART.bat`을 다시 실행하면 이미 설치된 항목과 이미 임베딩된 리포트를 재사용하므로 처음보다는 빠르지만, 실행일 기준 데이터 수집과 임베딩 확인 단계를 다시 거칩니다. 단순히 앱만 열려면 `RUN_APP.bat`을 사용하세요.

## 7. 자주 생기는 문제

### Python을 찾을 수 없다고 나와요

Python 3.10 이상을 설치한 뒤 다시 실행하세요.

- Python 다운로드: <https://www.python.org/downloads/>

설치할 때 `Add Python to PATH` 옵션을 체크하는 것을 권장합니다.

### OpenRouter API 키를 잘못 입력했어요

프로젝트 루트의 `.env` 파일에서 `OPENROUTER_API_KEY=` 값을 수정하거나, `.env` 파일을 삭제한 뒤 `RUN_QUICKSTART.bat`을 다시 실행하세요.

### 브라우저가 열리지 않아요

터미널 창에 표시되는 Streamlit 주소를 직접 브라우저에 붙여넣으세요.
보통 아래 주소 중 하나입니다.

```text
http://localhost:8501
http://127.0.0.1:8501
```

### `missing ScriptRunContext` 경고가 보여요

`Thread 'MainThread': missing ScriptRunContext! This warning can be ignored when running in bare mode.` 경고는 Streamlit 앱 컨텍스트 없이 `st.*` 코드가 실행될 때 나옵니다. GUI는 아래처럼 Streamlit으로 실행해야 합니다.

```bash
streamlit run apps/gui/app.py
```

Quick Start 또는 위 명령으로 실행 중이고 화면이 정상 동작한다면 무시해도 되는 Streamlit 경고입니다. `python apps/gui/app.py`처럼 직접 실행했다면 위 명령으로 다시 실행하세요.

### 비용이 걱정돼요

Quick Start는 기본적으로 rerank를 끈 상태(`USE_RERANKER=false`)로 실행합니다. 그래도 OpenRouter API를 사용하므로 소액의 비용이 발생할 수 있습니다. 사용량은 OpenRouter 대시보드에서 확인하세요.
