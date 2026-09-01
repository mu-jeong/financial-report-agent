# Quick Start

개발 명령어를 몰라도 처음에는 `RUN_QUICKSTART.bat` 파일 하나만 더블클릭하면 설치, OpenRouter API 키 설정, 실행일 포함 이전 7일 범위(총 최대 8일)의 데이터 준비, 검색 인덱스 생성, 웹 화면 실행까지 자동으로 진행됩니다.

초기 준비가 끝난 뒤 앱만 다시 열 때는 `RUN_APP.bat`을 사용하세요. 이 파일은 `.venv`와 `.env`를 확인하고 retrieval runtime의 catalog와 active snapshot을 검증한 뒤 Streamlit GUI를 실행하며, 설치·수집·임베딩은 반복하지 않습니다.

활성 V2의 추출 정책을 배포 기본값으로 바꾸려면 incremental update가 아니라 전체 successor 재구축이 필요합니다. 앱과 데이터 업데이트 창을 모두 닫고 `tools\recovery\REBUILD_V2.bat --check`로 읽기 전용 점검을 수행한 뒤, `tools\recovery\REBUILD_V2.bat`을 실행해 안내에 따라 진행하세요. 자세한 내용은 [Native V2 전체 재구축](../reference/migrations/v2/V2_REBUILD.md)을 참고하세요.

## 기존 V1 사용자 마이그레이션

`DATA_ROOT`에 V1 `reports.db`와 `vector_db`가 있다면 새 앱을 실행하기 전에 프로젝트 루트의 `MIGRATE_V2.bat`을 실행하세요. 기존 청크와 FAISS 벡터를 Native V2 저장 구조로 옮기므로 전체 PDF 파싱과 전체 임베딩을 다시 수행하지 않습니다. 성공 후 V1 `reports.db`와 `vector_db`는 삭제되지만, 데이터 업데이트와 재구축에 필요한 `downloaded` PDF는 유지됩니다. 자세한 절차와 실패 시 동작은 [V1 → Native V2 사용자 마이그레이션](../reference/migrations/v2/V2_MIGRATION_USER.md)을 참고하세요.

## 1. 실행 방법

1. 이 프로젝트 폴더를 엽니다.
2. `RUN_QUICKSTART.bat`을 더블클릭합니다.
3. 처음 실행할 때 OpenRouter API 키를 물어보면 붙여넣고 Enter를 누릅니다.
4. 설치와 데이터 준비가 끝나면 브라우저에서 Finance LLM 화면이 열립니다.

이후 일상적으로 앱만 실행할 때는 같은 폴더에서 `RUN_APP.bat`을 더블클릭합니다. `.venv` 또는 `.env`가 없다는 안내가 나오면 먼저 `RUN_QUICKSTART.bat`을 실행해 초기 준비를 완료하세요.

앱 실행 후 사이드바의 데이터 업데이트 영역에서는 업데이트할 카테고리(`company`, `industry`, `economy`)를 선택할 수 있습니다. 이미 `company` 데이터가 있는 날짜라도 `industry`를 선택하면 해당 날짜의 산업 리포트를 추가로 확인하고 임베딩할 수 있습니다. Native V2 업데이트 중에도 기존 검색은 계속 사용할 수 있고, 처리가 끝난 문서는 전체 작업 종료 전부터 순차적으로 검색에 반영됩니다.

## 2. OpenRouter API 키 준비

Finance LLM은 답변 생성과 임베딩에 OpenRouter API를 사용합니다.

API 키가 없다면 아래 문서를 먼저 따라 발급받으세요.

- [OpenRouter API 키 발급 방법](OPENROUTER_API_KEY.md)

## 3. Quick Start가 자동으로 하는 일

`RUN_QUICKSTART.bat`은 내부적으로 `scripts/quickstart.py`를 실행해 다음 작업을 순서대로 처리합니다.
터미널에는 각 단계가 끝날 때마다 `[진행]` 프로그레스 바가 표시되어 현재 준비 상태를 확인할 수 있습니다.

1. Python 버전 확인
2. `.env` 파일 생성 또는 업데이트와 OpenRouter API 키 저장
3. `.venv` 가상환경 생성 또는 확인
4. 실행 산출물 폴더 생성 또는 확인 (`logs/`, `data/`, `data/downloaded/`, `reports/`)
5. pip 업데이트
6. `requirements.txt` 패키지 설치 또는 확인
7. retrieval runtime의 쓰기 가능 상태 검증
8. 실행일 포함 이전 7일 범위의 리포트 수집
9. 전체 PDF 변경 여부를 검사해 새 문서와 변경된 문서만 파싱·임베딩하고 immutable Native V2 snapshot 게시
10. 데이터 상태 출력
11. Streamlit GUI 실행

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
배포 템플릿은 일반 문서와 미임베딩 문서를 먼저 `pymupdf`로 추출하고 `PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader`를 명시합니다. fallback을 사용하려면 Java 11+와 `java` 명령의 `PATH` 등록이 필요합니다. 새 키가 없는 기존 `.env`와 빈 값은 fallback을 비활성화합니다. Native V2에서는 active profile과 다른 fallback 설정을 incremental update에 섞지 않으며, 정책을 바꾸려면 full-corpus successor가 필요합니다.
일부 PDF에서 primary와 fallback 파싱이 모두 실패해도 Quick Start는 실패 파일을 기록하고 다음 단계로 진행합니다. OpenDataLoader fallback이 한 PDF에서 5분 안에 반환하지 않는 경우도 같은 추출 실패로 처리합니다. 실패 PDF는 active build manifest에 `source-extraction-failed` 제외 상태로 남기고, 성공한 나머지 문서로 검증된 successor를 게시합니다. 같은 실패 파일은 일반 업데이트에서 반복 파싱하지 않으며 Monitoring Mode의 `운영 상태 → 임베딩 누락 문서`에서 `파싱 실패 문서 재시도`를 눌렀을 때만 다시 처리합니다.
`RUN_QUICKSTART.bat`을 다시 실행하면 이미 설치된 항목과 이미 임베딩된 리포트를 재사용하므로 처음보다는 빠르지만, 실행일 기준 데이터 수집과 임베딩 확인 단계를 다시 거칩니다. 단순히 앱만 열려면 `RUN_APP.bat`을 사용하세요.

## 7. 활성 V2 전체 재구축

배포 기본값은 PyMuPDF를 primary로 사용하고, 실패한 PDF만 OpenDataLoader로 한 번 재시도합니다. 기존 active V2가 이 순서를 반대로 기록했거나 현재 설정과 다른 추출 profile을 사용한다면 `tools\recovery\REBUILD_V2.bat`이 전체 PDF를 새 정책으로 다시 파싱·임베딩해 검증된 successor를 만듭니다.

```bat
tools\recovery\REBUILD_V2.bat --check
tools\recovery\REBUILD_V2.bat
```

- `--check`는 현재 snapshot·추출 profile·인덱싱 문서 수와 요청 profile·원본 PDF 수를 출력할 뿐 데이터를 바꾸지 않습니다.
- 전체 실행은 현재 `PDF_*` 추출 설정을 그대로 사용하며 다른 사용자 설정을 덮어쓰지 않습니다.
- active profile과 요청 profile이 이미 같다면 재구축하지 않고 종료합니다.
- 전체 corpus를 다시 처리하므로 OpenRouter API 비용이 발생하며 문서 수와 길이에 따라 오래 걸릴 수 있습니다. 실행 전에 API 크레딧을 확인하세요.
- 실행 창의 `[PDF 현재/전체]` 표시로 추출 진행률을 확인할 수 있습니다. 일부 OpenDataLoader fallback PDF는 한 파일 처리에도 여러 분이 걸릴 수 있습니다.
- PyMuPDF와 OpenDataLoader가 모두 실패한 PDF는 원본을 삭제하지 않고 manifest에 제외 상태로 기록합니다. 화면에는 `원본 PDF`, `인덱싱 성공`, `추출 실패/제외` 수가 따로 표시되며 나머지 문서 처리는 계속됩니다.
- 재구축 중에는 기존 active snapshot이 계속 검색을 담당합니다. 성공한 문서와 명시적 제외 결정을 모두 포함한 새 snapshot은 검증을 통과한 뒤에만 원자적으로 활성화됩니다. embedding·무결성 검증 등 빌드 자체가 실패하면 기존 active가 그대로 유지됩니다.
- `DATA_ROOT`의 현재 활성 snapshot과 원본 PDF는 삭제하지 않습니다. 전환 직전 snapshot은 검증된 predecessor로 잠시 보존하지만 전환 후 검색에는 사용하지 않습니다.
- `DATA_ROOT/retrieval/v2`를 수동으로 삭제하거나 수정하지 마세요.

## 8. 자주 생기는 문제

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
