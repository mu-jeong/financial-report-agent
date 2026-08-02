# Finance LLM

> Version: `0.5.1`

## Quick Start: 간편하게 실행하기

Windows에서 처음 실행할 때는 `RUN_QUICKSTART.bat`을 더블클릭하면 설치, OpenRouter API 키 설정, 실행일 포함 이전 7일 범위(총 최대 8일)의 리포트 수집, 임베딩 생성, 웹 화면 실행까지 자동으로 진행됩니다.

1. [OpenRouter API 키 발급 방법](docs/OPENROUTER_API_KEY.md)을 따라 API 키와 크레딧을 준비합니다.
2. 프로젝트 폴더에서 `RUN_QUICKSTART.bat`을 더블클릭합니다.
3. 처음 실행 시 API 키를 붙여넣고 Enter를 누릅니다.
4. 브라우저가 열리면 바로 질문을 입력합니다.

초기 준비가 끝난 뒤 앱만 다시 열 때는 `RUN_APP.bat`을 사용하세요. 이 파일은 `.venv`와 `.env`를 확인하고 retrieval runtime의 catalog와 active snapshot을 검증한 뒤 Streamlit GUI를 실행합니다. 패키지 설치·리포트 수집·임베딩은 반복하지 않습니다.

Quick Start는 매번 실행하는 날짜를 기준으로 실행일과 그 이전 7일(총 최대 8일)의 리포트를 준비합니다. 자세한 실행 방법과 `RUN_APP.bat` 사용 구분은 [docs/QUICK_START.md](docs/QUICK_START.md)를 참고하세요.

일부 PDF가 PyMuPDF와 OpenDataLoader에서 모두 파싱되지 않아도 실패 파일만 V2 manifest에 제외 상태로 기록하고, 나머지 문서의 파싱·임베딩·snapshot 게시와 앱 실행은 계속합니다. OpenDataLoader가 한 PDF에서 5분 안에 반환하지 않는 경우도 추출 실패로 기록해 전체 작업이 무기한 멈추지 않게 합니다. 기록된 문서는 Monitoring Mode의 `임베딩 누락 문서`에서 명시적으로 다시 시도할 수 있습니다.

기존 V1 검색 데이터가 있다면 `MIGRATE_V2.bat`을 더블클릭해 백업·임베딩 공간 확인·GUI 실행 테스트를 거친 뒤, 기존 청크와 벡터를 그대로 사용하는 쓰기 가능한 V2로 안전하게 전환할 수 있습니다. 전체 PDF를 다시 파싱하거나 재임베딩하지 않으며, 전환 후에는 새 문서와 변경된 문서만 처리합니다. 설계 배경은 [V2 마이그레이션과 검색 아키텍처](docs/migrations/v2/V2_MIGRATION.md), 실행 절차는 [일반 사용자용 V2 마이그레이션](docs/migrations/v2/V2_MIGRATION_USER.md)을 참고하세요.

### `tools\recovery\REBUILD_V2.bat`은 언제 필요한가

> **대부분의 사용자는 `tools\recovery\REBUILD_V2.bat`을 실행할 필요가 없습니다.** 과거 버전에서 만든 V2의 PDF 추출 설정을 현재 기본값인 `PyMuPDF 우선 → OpenDataLoader fallback`으로 바로잡을 때만 사용하세요.

| 하려는 작업 | 실행할 항목 |
| --- | --- |
| 처음 설치 | `RUN_QUICKSTART.bat` |
| 기존 V1을 V2로 전환 | `MIGRATE_V2.bat` — 재구축 불필요 |
| 리포트 추가·변경 | 앱의 일반 데이터 업데이트 — 재구축 불필요 |
| 파싱 실패 문서만 재시도 | Monitoring Mode의 `임베딩 누락 문서 → 파싱 실패 문서 재시도` |
| 과거 V2의 추출 설정을 바로잡기 | 먼저 `tools\recovery\REBUILD_V2.bat --check` |

`--check` 결과가 `[확인]`이면 아무 작업도 하지 마세요. `[조치 필요]`이고 표시된 현재·목표 설정이 실제로 다를 때만 `tools\recovery\REBUILD_V2.bat`을 실행하세요. 전체 PDF를 다시 처리하므로 시간과 API 비용이 발생합니다. 자세한 내용은 [일반 사용자용 V2 마이그레이션](docs/migrations/v2/V2_MIGRATION_USER.md#활성-v2-전체-재구축)을 참고하세요.

---

증권사 리포트 PDF를 수집하고 V2 SQLite catalog와 immutable FAISS snapshot에 색인한 뒤, LangGraph 기반 RAG 파이프라인으로 재무 질문에 답하는 프로젝트입니다. 기존 V1 `reports.db`/`vector_db`는 마이그레이션 전 설치에서만 검색 권위로 사용합니다. 생성 모델, 임베딩, 선택형 rerank는 OpenRouter API를 기준으로 연동합니다.

> 이 프로젝트는 투자 조언이나 매수/매도 추천을 제공하지 않습니다. 답변은 수집·색인된 리포트와 공개 데이터 기반의 참고 정보로만 사용하세요.

## 주요 기능

- 증권사 리포트 PDF 다운로드 및 파일명 기반 메타데이터 파싱
- `company`, `industry`, `economy` 카테고리별 리포트 수집
- V2 SQLite catalog(`data/retrieval/v2/catalog.sqlite3`)와 immutable FAISS snapshot의 membership·publication 동기화
- PyMuPDF, OpenDataLoader, Marker, Docling, pdf-to-markdown 중 선택 가능한 PDF 텍스트 추출 엔진
- Parent-Child Chunking 기반 문맥 확장 검색
- LangGraph 기반 query rewrite, routing, RDB 검색, VectorDB 검색, 답변 생성
- SQL guardrail: `SELECT`와 `reports` 테이블 중심의 read-only SQLite 접근
- OpenRouter 임베딩(`baai/bge-m3`) 지원
- 선택형 OpenRouter rerank(`cohere/rerank-v3.5`) 또는 명시적으로 설정하는 로컬 FlashRank adapter 지원(자동 fallback 없음)
- `report_date` 기준 날짜/월/분기/연도 필터링과 최신성 가중치(`RECENCY_WEIGHT`) 지원
- KRX 상장법인 업종 CSV 기반 섹터/분야 질문의 회사 universe lookup 지원
- VectorDB 검색 실패 시 short-term memory 영향을 제거하고 원질문으로 재검색
- 답변의 `[숫자]` citation과 기본 접힘 상태의 참고 문서 목록 연동
- Streamlit GUI 대화 기록 저장, 백그라운드 답변 생성, 대화 이름 변경/삭제, 참고 PDF 열기
- FinanceDataReader 기반 주가 조회 tool calling
- Streamlit GUI 실행 (CLI는 유지보수 전용 deprecated 모드)

## 설치

```powershell
git clone <repository-url>
cd finance_llm
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Python 3.10 이상을 권장합니다.

## 환경 변수 설정

수정 가능한 설정의 기본값, 타입, 설명은 `src/configs/settings.py`에서 한 번만 관리합니다. `.env.example`은 이 파일에서 자동 생성되는 템플릿이고, `.env`는 실제 실행값만 저장합니다.

수동으로 환경을 준비할 때는 루트의 `.env.example`을 `.env`로 복사한 뒤 `OPENROUTER_API_KEY`를 채웁니다.

```powershell
Copy-Item .env.example .env
```

Quick Start와 배포용 `.env.example`의 주요 값은 다음과 같습니다.

```env
GENERATION_MODEL=deepseek/deepseek-v4-flash
EMBEDDING_MODEL=baai/bge-m3
USE_RERANKER=false
RERANK_PROVIDER=openrouter
RERANK_MODEL=cohere/rerank-v3.5
SEARCH_TOP_K=20
RECENCY_WEIGHT=0.15
PDF_EXTRACTION_ENGINE=pymupdf
PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader
UNEMBEDDED_PDF_EXTRACTION_ENGINE=pymupdf
```

배포 템플릿은 일반 문서와 미임베딩 문서를 먼저 `pymupdf`로 추출하고, PyMuPDF가 실패한 문서만 `opendataloader`로 한 번 재시도합니다. fallback 실행에는 Java 11+와 `java` 명령의 `PATH` 등록이 필요합니다. 이 새 키가 없는 기존 `.env`와 빈 값은 fallback을 비활성화하므로, 사용하려면 `PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader`를 명시하세요.

과거 로컬 측정에서는 약 2,000건의 리포트를 임베딩하는 데 약 **$0.05**가 들었습니다. 이 수치는 재현 가능한 비용 보장이 아니며 문서 길이, 청크 수, 호출량, 모델 가격에 따라 달라집니다.

`.env.example`을 다시 생성해야 할 때는 아래 명령을 실행합니다.

```bash
python -m src.configs.generate_env_example
```

자세한 설정은 [`docs/API_SETUP.md`](docs/API_SETUP.md)를 참고하세요.

## PDF 파일명 규칙

다운로드된 PDF는 기본적으로 `data/downloaded/` 아래에 저장됩니다. 파일명은 DB 메타데이터 파싱에 사용하므로 아래 규칙을 따릅니다.

```text
[카테고리]_[YYYY-MM-DD]_[대상]_[증권사]_[제목].pdf
```

예시:

```text
company_2026-05-29_NAVER_미래에셋증권_기업 분석.pdf
industry_2026-05-20_반도체_신한투자증권_HBM 전망.pdf
economy_2026-05-15_null_한국투자증권_금리 전망.pdf
```

## 리포트 다운로드

```bash
python -m src.core.report_crawler
```

주요 옵션은 `src/configs/settings.py`와 `.env.example`에서 확인합니다. 기본 Quick Start 정책은 실행일을 기준일로 자동 설정하고, 실행일과 그 이전 7일(총 최대 8일) 범위의 리포트를 수집하는 것입니다.

```env
CRAWLER_MODE=LATEST
CRAWLER_CATEGORIES=company
CRAWLER_TARGET_DATE=
CRAWLER_TARGET_COUNT=0
CRAWLER_LOOKBACK_DAYS=7
CRAWLER_MAX_LOOKBACK_DAYS=7
```

- `CRAWLER_MODE=LATEST`: 실행 시점의 KST 기준일을 사용합니다.
- `CRAWLER_MODE=SPECIFIC_DATE`: `CRAWLER_TARGET_DATE`에 지정한 날짜를 사용합니다.
- `CRAWLER_CATEGORIES`: `company`, `industry`, `economy`, 쉼표 구분 목록, 또는 `all`을 사용할 수 있습니다.
- `CRAWLER_LOOKBACK_DAYS=7`: 기준일 포함 최대 8일 범위를 조회합니다.
- `CRAWLER_TARGET_COUNT=0`: 개수 제한 없이 가능한 리포트를 수집합니다.

## 상장기업 업종 데이터

섹터나 분야에 속한 기업을 묻는 질문은 레포에 포함된 KRX 상장법인 업종 CSV를 회사 universe lookup에 사용합니다. 원본 데이터는 KRX 상장법인목록 페이지에서 내려받은 Excel을 `company_name,industry,main_products` CSV로 변환한 것입니다.

- 출처: https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage
- 기본 데이터 파일: `data/listed_company_industries.csv`
- 명시 설정: `.env`의 `COMPANY_INDUSTRY_DATA_PATH`에 CSV 경로를 지정합니다.

이 데이터는 리포트 본문 검색용 VectorDB가 아니라 `회사명/업종/주요제품` 구조화 lookup에 사용합니다. 예를 들어 “반도체 섹터에 속한 기업”은 먼저 업종 CSV에서 관련 회사를 찾고, 현재 날짜/report_type scope의 `reports` DB와 교집합을 낸 뒤 해당 `file_name` 범위로 VectorDB 검색을 좁힙니다.

## 임베딩 인덱스 생성

```bash
python -m src.core.embed_pipeline              # V1: TEST_LIMIT 적용, V2: 전체 inventory 검사
python -m src.core.embed_pipeline --all        # V1: pending 전체, V2: 같은 incremental 검사
python -m src.core.embed_pipeline --limit 100  # V1에서만 처리량 제한
```

V1에서는 `TEST_LIMIT`, `--limit`, `--all`이 pending 처리량을 결정합니다. V2에서는 limit 옵션을 무시하고 전체 PDF 목록의 변경 여부를 검사하며, 새 문서와 변경된 문서만 파싱·임베딩합니다. 성공한 문서는 작은 불변 업데이트 단위로 즉시 검색에 반영되므로 전체 작업 중에도 기존 검색을 계속 사용할 수 있습니다. 모든 문서 처리가 끝나면 기존 청크와 벡터까지 재사용해 완전한 snapshot을 한 번만 게시합니다. primary와 fallback이 모두 실패한 변경 문서는 이전 검색 가능 버전을 유지한 채 실패 상태로 기록하고 나머지를 계속 처리합니다. 같은 바이트의 기존 실패는 일반 업데이트에서 반복 파싱하지 않으며, Monitoring Mode의 `임베딩 누락 문서`에서 재시도할 때만 다시 처리합니다. 새 변경도 정리할 중간 상태도 없으면 publication을 만들지 않습니다. 자세한 동작과 복구 경계는 [`docs/CONTINUOUS_UPDATES.md`](docs/CONTINUOUS_UPDATES.md)를 참고하세요.

V2 활성 상태에서는 `data/retrieval/v2`, `data/reports.db`, `data/vector_db`를 수동으로 삭제하거나 수정하지 마세요. V2 updater는 활성 embedding profile과 현재 모델·추출기·chunk 설정이 다르면 새 snapshot을 게시하기 전에 중단합니다. 위 표의 추출 정책 변경에 해당할 때만 `tools\recovery\REBUILD_V2.bat --check`로 점검한 뒤 `tools\recovery\REBUILD_V2.bat`으로 검증된 full-corpus successor를 만드세요.

PDF 추출 엔진 비교는 [`docs/PDF_EXTRACTION_COMPARISON.md`](docs/PDF_EXTRACTION_COMPARISON.md)를 참고하세요.

## 실행

### Streamlit GUI

권장 실행 방식입니다.

```bash
streamlit run apps/gui/app.py
```

### CLI (deprecated)

CLI 모드는 유지보수 전용입니다. 신규 기능 개발은 중단하며, 일반 사용은 Quick Start 또는 Streamlit GUI를 권장합니다. 기존 자동화 호환성을 위해 `--status` 등 최소 동작만 유지합니다.

```bash
python apps/cli/app.py --status
```


GUI 대화 이력은 `data/conversations.db`에 저장됩니다. deprecated CLI도 기존 호환성을 위해 같은 저장소를 사용합니다. Streamlit 사이드바의 대화 목록에서 각 대화 오른쪽의 연필 버튼으로 이름을 변경하고 `×` 버튼으로 삭제할 수 있습니다. 삭제 후에는 다음 대화가 자동 선택되고, 삭제 UI가 화면에 남지 않도록 즉시 rerun합니다. 이 파일은 로컬 상태 파일이며 일반적으로 Git에 포함하지 않습니다.

GUI 답변 생성은 백그라운드 thread에서 실행됩니다. 답변 생성 중인 대화는 입력창이 잠기고, 다른 대화로 이동해도 작업은 계속되며 완료/실패 상태가 toast와 대화 목록 배지로 표시됩니다.

채팅 입력창 아래의 `⚠ 신고` 버튼은 현재 대화에서 발생한 문제를 사람이 읽는 `debug/issue_report_*.txt`와 같은 stem의 구조화 `.json` sidecar로 저장합니다. `debug/` 폴더 내용은 Git에 포함하지 않습니다(`debug/.gitkeep`만 폴더 유지용). 민감정보가 포함될 수 있으므로 외부로 전달하기 전에 두 파일을 모두 확인하세요.

참고 문서의 `열기` 버튼은 브라우저 링크가 아니라 Streamlit 서버가 실행 중인 PC에서 PDF를 직접 엽니다. 파일은 `REPORT_PDF_DIR` 환경 변수의 폴더와 참고 문서의 파일명을 조합해 찾습니다. V1 임베딩 경로는 이 값을 `.env`에 자동 동기화합니다. V2는 기존 `REPORT_PDF_DIR` 또는 기본 `data/downloaded`를 사용하므로 PDF 위치를 바꿨다면 `.env`에서 직접 갱신하세요. 로컬 사용에는 적합하지만 원격 배포에서는 서버 PC에서 파일이 열립니다.

사이드바 캘린더는 검색 가능한 리포트 날짜를 데이터 있음으로 표시합니다. V1은 `reports.is_embedded=1`, V2는 현재 base snapshot과 이미 반영된 업데이트를 합친 `active_reports`를 기준으로 집계합니다. 데이터 업데이트에서는 `company`, `industry`, `economy` 카테고리를 선택할 수 있고, 선택한 카테고리 중 하나라도 비어 있는 평일은 업데이트 대상으로 포함합니다. 필요한 다운로드와 임베딩은 백그라운드 작업으로 실행되며, 처리 완료 문서는 작업 종료 전에도 검색과 캘린더에 순차 반영됩니다.

### 대화 후속 질문과 참고 문서 표시

GUI 채팅은 성공한 assistant 답변의 검색 범위를 메시지 metadata에 저장합니다. 저장되는 범위에는 VectorDB 필터, 상대 날짜 해석 결과, 실제 답변에 사용된 참고 PDF 파일명, 답변 섹션별 `answer_scope_index`가 포함됩니다. 사용자가 "주요 내용 정리", "방금 내용 요약", "위 내용 핵심"처럼 직전 답변을 가리키는 후속 질문을 하면 같은 문서 범위를 재사용해 답변이 다른 리포트로 새지 않도록 합니다.

직전 답변의 특정 섹션을 더 자세히 묻는 질문도 별도로 처리합니다. 예를 들어 "개별 종목/주요 기업 리포트를 상세하게 정리해줘", "섹터 부분을 자세히 알려줘", "거시경제 내용을 더 알려줘" 같은 질문은 직전 답변의 날짜 범위를 유지하면서 `report_type=company|industry|economy` 필터를 추가합니다. 이때 직전 답변의 전체 PDF 파일명 목록은 섹션 범위를 과도하게 제한하지 않도록 제거하고, 어떤 섹션 alias와 필터가 적용됐는지는 `scope_decision` metadata로 남깁니다. 다만 "5월 주요 내용"처럼 새 날짜 조건을 명시한 질문은 현재 질문의 조건을 우선합니다.

참고 문서 목록은 답변 안의 citation 링크와 연결되지만, 화면에서는 기본적으로 접힌 상태로 표시됩니다. 필요한 경우 사용자가 직접 펼쳐서 문서명과 `열기` 버튼을 확인합니다.

## 검색 및 답변 흐름

1. `query_rewrite`: 질문을 검색 친화적으로 정리합니다.
   - 후속 질문 여부를 판단해 `followup_scope_intent`를 상태에 남깁니다.
   - 직전 답변 섹션을 가리키는 deep-dive 질문은 `src/core/followup_scope.py`의 섹션 alias(`company`, `industry`, `economy`)로 감지합니다.
2. `search_scope`: 날짜/메타데이터 필터와 직전 답변 scope 재사용 여부를 결정합니다.
   - `followup_scope_intent`가 켜져 있고 새 날짜 조건이 없으면 직전 VectorDB 답변의 검색 필터, 날짜 범위, 실제 참고 파일명을 `prior_search_scope`로 재사용합니다.
   - 섹션 follow-up이면 직전 날짜 범위는 유지하고 섹션별 `report_type` 필터를 추가하며, `scope_decision`에 적용 근거를 기록합니다.
   - 섹션 follow-up은 "top company"류 rewrite가 끼어들어도 단일 기업 선택으로 축소하지 않습니다.
3. `router`: RDB 질문인지 VectorDB 질문인지 판단합니다.
4. RDB 검색: LLM이 SQL을 생성하고 guardrail을 통과한 read-only `SELECT`만 실행합니다.
5. VectorDB 검색: FAISS 후보를 넉넉히 가져온 뒤 날짜/종목/증권사/리포트 유형/파일명 필터와 최신성 가중치를 적용합니다.
   - 복수 문서 의도, 명시 파일 scope, 섹션 follow-up에서는 특정 PDF chunk에 결과가 쏠리지 않도록 문서 coverage를 적용합니다.
6. `USE_RERANKER=true`일 때 OpenRouter rerank를 추가로 적용합니다. 기본값은 비용을 고려해 false입니다.
7. VectorDB 검색 결과가 없으면 해당 대화의 short-term memory 영향을 제거하고 원질문으로 한 번 더 검색합니다.

## PDF 추출 엔진 비교

`PDF_EXTRACTION_ENGINE`은 `pymupdf`, `marker`, `opendataloader`, `docling`, `pdf-to-markdown` 중 하나로 설정할 수 있으며 기본값은 `pymupdf`입니다. 배포용 `.env.example`은 `PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader`를 명시하지만, 이 키가 없거나 빈 값이면 fallback을 사용하지 않습니다. `UNEMBEDDED_PDF_EXTRACTION_ENGINE`은 미임베딩 문서에 적용할 primary 엔진이며 배포 템플릿에서는 `pymupdf`를 사용합니다. 이 값이 primary와 다르면 명시적 override로 간주해 자동 fallback하지 않습니다. 기존 `EXTRACTION_ENGINE`, `EXTRACTION_FALLBACK_ENGINE`, `UNEMBEDDED_EXTRACTION_ENGINE` 환경변수도 alias로 동작합니다. 모든 엔진 출력은 downstream 색인 전에 공통 표 제거 로직을 통과합니다. Native V2 incremental update는 active profile에 기록된 primary/fallback 정책과 현재 설정이 정확히 같을 때만 실행되며, 정책 변경에는 검증된 full-corpus successor가 필요합니다.

```bash
python -m src.core.compare_pdf_extractors --limit 10
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader marker --limit 5
# 선택형 엔진까지 비교하려면 런타임 요구사항을 설치한 뒤 실행합니다.
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader marker docling pdf-to-markdown --limit 5
```

자세한 내용은 [`docs/PDF_EXTRACTION_COMPARISON.md`](docs/PDF_EXTRACTION_COMPARISON.md)를 참고하세요.

## 테스트

```bash
python -m pytest -q
```

현재 테스트는 파일명 파싱, SQL guardrail, 상태 요약, metadata filter와 날짜 해석, query rewrite, 후속 질문 검색 범위 재사용, 답변 섹션 기반 follow-up scope 결정, 섹션 follow-up의 문서 coverage, OpenRouter embedding/rerank payload, PDF 추출 엔진/표 제거 계약, conversation store, citation 링크 변환, 문서 단위 citation 재번호, Quick Start, 백그라운드 데이터 업데이트, VectorDB no-result 재시도 로직을 검증합니다. 또한 native V2 schema/catalog, V1 무재임베딩 변환, launcher guard, snapshot publication·recovery·rollback, writer/update lock, reader parity와 변경 문서만 처리하는 incremental vector reuse를 검증합니다.

### 평가용 테스트셋

현재 승인된 정식 evaluation fixture는 없습니다. 임시 데이터나 과거 run을 정확도 기준으로 사용하지 않으며, Monitoring 화면은 이 경우 정확도를 `측정 전`으로 표시합니다.

향후 fixture는 Native V2 data/index revision과 함께 고정하고, 질문·기대 라우팅·필터·출처·상태만 저장합니다. PDF 본문은 포함하지 않습니다. 준비 기준과 절차는 [`docs/EVALUATION_DATASET.md`](docs/EVALUATION_DATASET.md)를 참고하세요.

```bash
python -m pytest tests/test_evaluation_dataset.py -q
```

평가 계약은 향후 parsing, chunking, retrieval/rerank, 모델 변경에 따른 정확도와 latency 회귀를 분리해 측정합니다. 현재 자동 evaluator는 provider cost나 answer similarity를 직접 계산하지 않습니다.

## 주의사항

- 이 프로젝트는 투자 조언이나 매수/매도 추천을 제공하지 않습니다.
- 오래된 PDF와 잘못 추출된 표, 누락된 리포트는 답변 품질에 영향을 줄 수 있습니다.
- `.env`에는 실제 API 키가 들어가므로 커밋하지 마세요.
- FAISS의 `index.pkl`은 pickle 역직렬화를 사용하므로 신뢰할 수 없는 파일을 로드하지 마세요.
- 로컬 GUI에서 PDF `열기` 기능은 Streamlit 서버가 실행 중인 PC 기준으로 동작합니다. 원격 서버에 배포하면 서버 PC에서 파일을 열려고 시도합니다.

## Monitoring Mode

Monitoring Mode는 답변 속도와 정확도를 우선 확인하고, 문제가 있을 때만 상세 진단으로 내려가는 Native V2 개발자 화면입니다. 일반 GUI에서는 숨기고 `.env`에서 명시적으로 켰을 때만 노출합니다. 구현 계약은 [`docs/MONITORING.md`](docs/MONITORING.md)에 정리되어 있습니다.

### 실행 방법

Monitoring Mode UI는 `.env`에서 명시적으로 켰을 때만 Streamlit에 표시됩니다.

```env
MONITORING_MODE=true
```

이후 평소처럼 GUI를 실행합니다.

```bash
streamlit run apps/gui/app.py
```

활성화되면 사이드바에 `Chat`과 `Monitoring`이 표시되고, `Chat`에는 `Chat / 답변 모니터링` 탭이 생깁니다. 개별 답변 모니터링은 현재 대화의 최근·평균 응답시간과 RDB·Vector DB 평균 조회시간만 보여줍니다. 전체 Monitoring 기본 화면은 `응답 속도(P95)`와 correctness-only `답변 정확도`를 보여주고, 응답 trace, 검색 자료 상태, 정확도 평가, parsing 비교, issue report와 회귀 후보는 `문제 상황 자세히 보기` 안에서 선택합니다. `MONITORING_MODE=false`이거나 설정이 없으면 일반 채팅 UI만 동작합니다.

### 테스트 방법

```bash
python -m pytest tests/test_settings.py tests/test_monitoring.py tests/test_gui_view_contracts.py tests/test_feedback_loop.py -q
python -m pytest -q
```

### 화면 원칙

- 속도는 assistant 응답 latency의 P95로 표시합니다.
- 속도는 실제 Native V2 runtime provenance가 저장된 응답만 집계해 과거 V1 기록을 제외합니다.
- 정확도는 snapshot/build/profile/generation과 hash가 검증된 Native V2 평가 run의 correctness 검사만 집계하며 latency는 제외합니다.
- 평가 자료가 없으면 0%가 아니라 `측정 전`으로 표시합니다.
- Native V2 상태가 없을 때 과거 지표로 우회하지 않습니다.
- 스키마 없는 과거 report·candidate·run은 활성 화면에서 제외하되 자동 삭제하지 않습니다.

### 문제 상황 상세 영역

- 현재 문제
- 응답 원인 확인
- 검색 자료 준비
- 정확도 평가
- 문서 읽기 품질 비교
- 신고·수정 확인

### TODO

- [x] 일반 사용자 UX와 분리된 Monitoring Mode의 진입 방식과 노출 범위를 정합니다.
- [x] 데이터 준비 상태와 검색 가능 여부를 한눈에 파악할 수 있는 대시보드 방향을 잡습니다.
- [x] 질문 처리 흐름을 추적해 검색 실패, 라우팅 오류, 답변 품질 저하 원인을 확인할 수 있게 합니다.
- [x] 기존 active V2 profile에 다른 추출 정책을 자동으로 섞지 않고, 정책 변경을 검증된 full-corpus successor 경계로 제한합니다.
- [x] PyMuPDF가 실패한 문서를 OpenDataLoader로 즉시 한 번 재시도하고 V2 profile에 fallback 정책을 기록합니다.
- [x] 두 추출기가 모두 실패한 V2 문서를 manifest 제외 상태로 기록하고 나머지 문서의 snapshot 게시를 계속합니다.
- [ ] 상세 parser 오류·시도 횟수와 fallback 사용 추이를 별도 진단 이력으로 관측할 수 있게 합니다.
- [ ] parsing·chunking·retrieval·rerank·모델 변경의 품질, 답변 변화량, 비용/latency를 비교할 수 있는 관측 지표를 정리합니다.
- [ ] 설정 변경이나 파이프라인 개선 전후를 비교할 수 있는 실험·평가 흐름을 마련합니다.
- [x] Monitoring Mode가 일반 실행 경로에 영향을 주지 않는지 회귀 테스트로 보호합니다.
