# Finance LLM

## Quick Start: 개발을 몰라도 한 번에 실행하기

Windows에서 `RUN_QUICKSTART.bat`을 더블클릭하면 설치, OpenRouter API 키 설정, 실행일 포함 이전 7일 범위(총 최대 8일)의 리포트 수집, 임베딩 생성, 웹 화면 실행까지 자동으로 진행됩니다.

1. [OpenRouter API 키 발급 방법](docs/OPENROUTER_API_KEY.md)을 따라 API 키와 크레딧을 준비합니다.
2. 프로젝트 폴더에서 `RUN_QUICKSTART.bat`을 더블클릭합니다.
3. 처음 실행 시 API 키를 붙여넣고 Enter를 누릅니다.
4. 브라우저가 열리면 바로 질문을 입력합니다.

Quick Start는 매번 실행하는 날짜를 기준으로 실행일과 그 이전 7일(총 최대 8일)의 리포트를 준비합니다. 자세한 실행 방법은 [docs/QUICK_START.md](docs/QUICK_START.md)를 참고하세요.

---

증권사 리포트 PDF를 수집하고 SQLite + FAISS에 색인한 뒤, LangGraph 기반 RAG 파이프라인으로 재무 질문에 답하는 프로젝트입니다. 생성 모델, 임베딩, 선택형 rerank는 OpenRouter API를 기준으로 연동합니다.

> 이 프로젝트는 투자 조언이나 매수/매도 추천을 제공하지 않습니다. 답변은 수집·색인된 리포트와 공개 데이터 기반의 참고 정보로만 사용하세요.

## 주요 기능

- 증권사 리포트 PDF 다운로드 및 파일명 기반 메타데이터 파싱
- `company`, `industry`, `economy` 카테고리별 리포트 수집
- SQLite `reports` 테이블과 FAISS 벡터 인덱스 동기화
- PyMuPDF, OpenDataLoader, Marker 중 선택 가능한 PDF 텍스트 추출 엔진
- Parent-Child Chunking 기반 문맥 확장 검색
- LangGraph 기반 query rewrite, routing, RDB 검색, VectorDB 검색, 답변 생성
- SQL guardrail: `SELECT`와 `reports` 테이블 중심의 read-only SQLite 접근
- OpenRouter 임베딩(`baai/bge-m3`) 지원
- 선택형 OpenRouter rerank(`cohere/rerank-v3.5`) 또는 FlashRank fallback
- `report_date` 기준 날짜/월/분기/연도 필터링과 최신성 가중치(`RECENCY_WEIGHT`) 지원
- VectorDB 검색 실패 시 short-term memory 영향을 제거하고 원질문으로 재검색
- 답변의 `[숫자]` citation과 참고 문서 목록 연동
- Streamlit GUI 대화 기록 저장, 대화 이름 변경/삭제, 참고 PDF 열기
- FinanceDataReader 기반 주가 조회 tool calling
- Streamlit GUI 실행 (CLI는 유지보수 전용 deprecated 모드)

## 설치

```bash
git clone <repository-url>
cd finance_llm
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Python 3.10 이상을 권장합니다.

## 환경 변수 설정

수정 가능한 설정의 기본값, 타입, 설명은 `src/configs/settings.py`에서 한 번만 관리합니다. `.env.example`은 이 파일에서 자동 생성되는 템플릿이고, `.env`는 실제 실행값만 저장합니다.

수동으로 환경을 준비할 때는 루트의 `.env.example`을 `.env`로 복사한 뒤 `OPENROUTER_API_KEY`를 채웁니다.

```powershell
Copy-Item .env.example .env
```

주요 기본값은 다음과 같습니다.

```env
GENERATION_MODEL=deepseek/deepseek-v4-flash
EMBEDDING_MODEL=baai/bge-m3
USE_RERANKER=false
RERANK_PROVIDER=openrouter
RERANK_MODEL=cohere/rerank-v3.5
SEARCH_TOP_K=20
RECENCY_WEIGHT=0.15
EXTRACTION_ENGINE=pymupdf
```

약 2,000건의 리포트를 임베딩 벡터화하는 데 약 **$0.05**가 소요되었습니다. 실제 비용은 문서 길이, 청크 수, 호출량, 모델 가격에 따라 달라질 수 있습니다.

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

## 임베딩 인덱스 생성

```bash
python -m src.core.embed_pipeline          # TEST_LIMIT 적용
python -m src.core.embed_pipeline --all    # pending 전체 처리
python -m src.core.embed_pipeline --limit 100
```

Quick Start는 pending 문서 전체 처리를 위해 `--all`을 사용합니다. 직접 실행할 때 `.env`의 `TEST_LIMIT=10`이 남아 있으면 일부 문서만 처리될 수 있습니다.

모델, chunk 크기, PDF 추출 엔진을 바꾼 뒤에는 기존 FAISS 인덱스를 재생성하는 것이 안전합니다.

```powershell
Remove-Item -Recurse -Force data\vector_db
python - <<'PY'
from src.core.db_manager import get_connection

conn = get_connection()
conn.execute("UPDATE reports SET is_embedded = 0")
conn.execute("DELETE FROM parent_chunks")
conn.commit()
conn.close()
PY
python -m src.core.embed_pipeline --all
```

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

참고 문서의 `열기` 버튼은 브라우저 링크가 아니라 Streamlit 서버가 실행 중인 PC에서 PDF를 직접 엽니다. 파일은 `REPORT_PDF_DIR` 환경 변수의 폴더와 참고 문서의 파일명을 조합해 찾습니다. `REPORT_PDF_DIR`은 임베딩 파이프라인이 문서 폴더의 절대경로를 기준으로 `.env`에 자동 생성하거나 기존 값만 갱신합니다. 로컬 사용에는 적합하지만, 원격 서버에 배포한 경우에는 서버 PC에서 파일이 열립니다.

사이드바 캘린더는 임베딩 완료 날짜만 데이터 있음으로 표시하고, 선택한 업데이트 기간 중 이미 임베딩된 날은 건너뜁니다. 필요한 다운로드와 임베딩은 백그라운드 작업으로 실행됩니다.

## 검색 및 답변 흐름

1. `query_rewrite`: 질문을 검색 친화적으로 정리합니다.
2. `router`: RDB 질문인지 VectorDB 질문인지 판단합니다.
3. RDB 검색: LLM이 SQL을 생성하고 guardrail을 통과한 read-only `SELECT`만 실행합니다.
4. VectorDB 검색: FAISS 후보를 넉넉히 가져온 뒤 날짜/종목/증권사/리포트 유형 필터와 최신성 가중치를 적용합니다.
5. `USE_RERANKER=true`일 때 OpenRouter rerank를 추가로 적용합니다. 기본값은 비용을 고려해 false입니다.
6. VectorDB 검색 결과가 없으면 해당 대화의 short-term memory 영향을 제거하고 원질문으로 한 번 더 검색합니다.
7. 답변과 참고 문서 목록을 반환합니다. GUI에서는 답변의 `[숫자]` 참조가 참고 문서 목록의 해당 항목으로 이동하고, 참고 문서는 접힌 expander 안의 텍스트 목록과 `열기` 버튼으로 표시됩니다.

## PDF 추출 엔진 비교

`EXTRACTION_ENGINE`은 `pymupdf`, `marker`, `opendataloader` 중 하나로 설정할 수 있습니다.

```bash
python -m src.core.compare_pdf_extractors --limit 10
python -m src.core.compare_pdf_extractors --engines pymupdf opendataloader marker --limit 5
```

자세한 내용은 [`docs/PDF_EXTRACTION_COMPARISON.md`](docs/PDF_EXTRACTION_COMPARISON.md)를 참고하세요.

## 테스트

```bash
python -m pytest -q
```

현재 테스트는 파일명 파싱, SQL guardrail, 상태 요약, metadata filter, query rewrite, OpenRouter embedding/rerank payload, conversation store, citation 링크 변환, Quick Start, 백그라운드 데이터 업데이트, VectorDB Top-K/최신성 및 no-result 재시도 로직을 검증합니다.

### 평가용 테스트셋

현재 로컬 `data/reports.db` 메타데이터에서 뽑은 평가용 fixture는 `tests/fixtures/evaluation_dataset.json`에 있습니다. PDF 본문은 포함하지 않고, 질문/기대 라우팅/기대 필터/기대 출처 파일명/RDB 기대 집계값만 담았습니다.

```bash
python -m pytest tests/test_evaluation_dataset.py -q
```

이 테스트셋은 향후 retrieval 품질, RDB 라우팅, 날짜별 데이터 캘린더, latency/비용 측정 회귀 평가에 사용할 기준 데이터입니다.

## 주의사항

- 이 프로젝트는 투자 조언이나 매수/매도 추천을 제공하지 않습니다.
- 오래된 PDF와 잘못 추출된 표, 누락된 리포트는 답변 품질에 영향을 줄 수 있습니다.
- `.env`에는 실제 API 키가 들어가므로 커밋하지 마세요.
- FAISS의 `index.pkl`은 pickle 역직렬화를 사용하므로 신뢰할 수 없는 파일을 로드하지 마세요.
- 로컬 GUI에서 PDF `열기` 기능은 Streamlit 서버가 실행 중인 PC 기준으로 동작합니다. 원격 서버에 배포하면 서버 PC에서 파일을 열려고 시도합니다.

## Debug Mode 계획 및 TODO

Debug Mode는 일반 사용자 화면을 복잡하게 만들지 않으면서, 성능개선과 품질 진단에 필요한 정보를 별도 대시보드로 모아보는 개발/운영용 모드입니다. 일반 GUI에서는 검색 준비 상태, 상세 인덱스 상태, latency breakdown, rerank 점수 같은 진단 정보를 숨기고, Debug Mode 화면에서만 노출하는 방향으로 개발합니다.

### 목표

- 검색/답변 품질 저하 원인을 빠르게 찾는 성능개선 대시보드 제공
- 수집 → 추출 → 임베딩 → 검색 → rerank → 답변 생성까지 단계별 병목 확인
- API 비용, latency, Top-K 품질, 필터 적용 결과를 한 화면에서 추적
- 일반 사용자 UX와 개발자 진단 UX를 분리

### TODO

- [ ] 일반 사용자 UX와 분리된 진단 모드의 진입 방식과 노출 범위를 정합니다.
- [ ] 데이터 준비 상태와 검색 가능 여부를 한눈에 파악할 수 있는 대시보드 방향을 잡습니다.
- [ ] 질문 처리 흐름을 추적해 검색 실패, 라우팅 오류, 답변 품질 저하 원인을 확인할 수 있게 합니다.
- [ ] 검색·rerank·생성 단계의 품질과 비용/latency를 비교할 수 있는 관측 지표를 정리합니다.
- [ ] 설정 변경이나 파이프라인 개선 전후를 비교할 수 있는 실험·평가 흐름을 마련합니다.
- [ ] 디버깅 결과를 안전하게 공유할 수 있도록 민감정보를 제외한 export 방식을 검토합니다.
- [ ] Debug Mode가 일반 실행 경로에 영향을 주지 않는지 회귀 테스트로 보호합니다.
