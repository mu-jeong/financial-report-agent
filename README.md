# Finance LLM

증권사 리포트 PDF를 수집하고 SQLite + FAISS에 색인한 뒤, LangGraph 기반 RAG 파이프라인으로 재무 질문에 답하는 프로젝트입니다.

## 최근 변경 요약

- **OpenRouter 단일 연동**: 생성 모델, 임베딩, 선택형 rerank를 OpenRouter API 기준으로 통합했습니다.
- **저비용 임베딩**: 약 2,000건의 리포트를 임베딩 벡터화하는 데 약 **$0.05**가 소요되었습니다. 실제 비용은 문서 길이와 청크 수에 따라 달라질 수 있습니다.
- **기본 모델**: 생성 모델은 `deepseek/deepseek-v4-flash`, 임베딩은 `baai/bge-m3`를 사용합니다.
- **Rerank 기본 비활성화**: 비용을 고려해 `USE_RERANKER=false`가 기본값입니다. 필요할 때 `cohere/rerank-v3.5`를 켤 수 있습니다.
- **대화 장기 메모리**: CLI/GUI 대화는 `data/conversations.db`에 저장되어 프로그램 재시작 후에도 불러올 수 있습니다.
- **수집 옵션 개선**: 다운로드 카테고리, 목표 개수, 조회 기간을 설정할 수 있습니다. 기본 카테고리는 `company`입니다.
- **날짜 기반 검색 개선**: 질문에서 날짜, 월, 분기, 연도를 추론해 `report_date` 메타데이터로 필터링합니다.
- **참고 문서 UI 개선**: GUI의 참고 문서는 기본적으로 접힌 dropdown 안에 텍스트 목록으로 표시됩니다.

## 주요 기능

- 증권사 리포트 PDF 다운로드 및 파일명 파싱
- SQLite `reports` 테이블과 FAISS 벡터 인덱스 동기화
- Parent-Child Chunking 기반 문맥 확장 검색
- LangGraph 기반 query rewrite, routing, RDB 검색, VectorDB 검색, 답변 생성
- SQL guardrail: `SELECT`와 `reports` 테이블 중심의 read-only SQLite 접근
- OpenRouter 임베딩(`baai/bge-m3`) 지원
- 선택형 OpenRouter rerank(`cohere/rerank-v3.5`) 또는 FlashRank fallback
- `report_date` 기준 최신성 가중치(`RECENCY_WEIGHT`) 지원
- FinanceDataReader 기반 주가 조회 tool calling
- Streamlit GUI와 CLI 실행

## 설치

```bash
git clone <repository-url>
cd finance_llm
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 환경 변수 설정

루트의 `.env.example`을 복사해 `.env`를 만들고 OpenRouter API 키를 설정합니다.

```env
OPENROUTER_API_KEY=sk-or-v1-your-key
OPENROUTER_DATA_COLLECTION=deny

GENERATION_MODEL=deepseek/deepseek-v4-flash
GENERATION_TEMPERATURE=0.1
GENERATION_MAX_TOKENS=4096

EMBEDDING_MODEL=baai/bge-m3
EMBEDDING_DIMENSIONS=1024

USE_RERANKER=false
RERANK_PROVIDER=openrouter
RERANK_MODEL=cohere/rerank-v3.5
RERANK_TIMEOUT=20
RERANK_CANDIDATE_MULTIPLIER=3
RECENCY_WEIGHT=0.15

CRAWLER_CATEGORIES=company
CRAWLER_MODE=LATEST
CRAWLER_TARGET_DATE=2026-05-31
CRAWLER_TARGET_COUNT=100
CRAWLER_LOOKBACK_DAYS=7
CRAWLER_MAX_LOOKBACK_DAYS=30
```

자세한 설정은 [`docs/API_SETUP.md`](docs/API_SETUP.md)를 참고하세요.

## PDF 파일명 규칙

다운로드된 PDF는 기본적으로 `data/downloaded/` 아래에 저장됩니다.

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

주요 옵션:

- `CRAWLER_CATEGORIES=company`: 기본값. `industry`, `economy`, `company,industry`, `all`도 가능
- `CRAWLER_MODE=LATEST`: `CRAWLER_TARGET_DATE`를 탐색 종료일로 보고, `CRAWLER_LOOKBACK_DAYS`만큼 과거로 내려가며 최신 리포트 수집
- `CRAWLER_MODE=SPECIFIC_DATE`: `CRAWLER_TARGET_DATE` 기준 수집
- `CRAWLER_TARGET_COUNT=100`: 원하는 개수까지 여러 날짜를 이어서 수집
- `CRAWLER_LOOKBACK_DAYS=7`: 한 번의 조회에서 최근 며칠까지 볼지 설정
- `CRAWLER_MAX_LOOKBACK_DAYS=30`: 목표 개수 확보를 위해 과거로 확장할 최대 기간

예를 들어 아래 설정은 2026-05-31을 기준일로 삼고, 2026-05-31부터 과거 7일 범위에서 리포트를 찾습니다.

```env
CRAWLER_MODE=LATEST
CRAWLER_TARGET_DATE=2026-05-31
CRAWLER_LOOKBACK_DAYS=7
```

## 임베딩 인덱스 생성

```bash
python -m src.core.embed_pipeline
python -m src.core.embed_pipeline --all
python -m src.core.embed_pipeline --limit 100
```

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

## 실행

### CLI

```bash
python apps/cli/app.py
python apps/cli/app.py --status
```

### Streamlit GUI

```bash
streamlit run apps/gui/app.py
```

GUI와 CLI 대화 이력은 `data/conversations.db`에 저장됩니다. Streamlit 사이드바의 대화 목록에서 각 대화 오른쪽 `...` 메뉴를 열어 이름 변경 또는 삭제를 선택할 수 있습니다. 대화 목록은 sidebar fragment로 묶어 일부 관리 작업은 필요한 영역만 갱신되도록 했습니다. 이 파일은 로컬 상태 파일이며 일반적으로 Git에 포함하지 않습니다.

참고 문서의 `열기` 버튼은 브라우저 링크가 아니라 Streamlit 서버가 실행 중인 PC에서 PDF를 직접 엽니다. 파일은 `REPORT_PDF_DIR` 환경 변수의 폴더와 참고 문서의 파일명을 조합해 찾습니다. `REPORT_PDF_DIR`은 임베딩 파이프라인이 문서 폴더의 절대경로를 기준으로 `.env`에 자동 생성하거나 기존 값만 갱신합니다. 로컬 사용에는 적합하지만, 원격 서버에 배포한 경우에는 서버 PC에서 파일이 열립니다.

## 검색 및 답변 흐름

1. `query_rewrite`: 질문을 검색 친화적으로 정리합니다.
2. `router`: RDB 질문인지 VectorDB 질문인지 판단합니다.
3. RDB 검색: LLM이 SQL을 생성하고 guardrail을 통과한 read-only SELECT만 실행합니다.
4. VectorDB 검색: FAISS 후보를 넉넉히 가져온 뒤 날짜/종목/증권사/리포트 유형 필터와 최신성 가중치를 적용합니다.
5. `USE_RERANKER=true`일 때 OpenRouter rerank를 추가로 적용합니다. 기본값은 비용을 고려해 false입니다.
6. 답변과 참고 문서 목록을 반환합니다. GUI에서는 참고 문서가 접힌 dropdown 안에 텍스트 목록으로 표시되며, `열기` 버튼으로 로컬 PDF 뷰어를 실행할 수 있습니다.

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

현재 테스트는 파일명 파싱, SQL guardrail, 상태 요약, metadata filter, OpenRouter embedding/rerank payload, conversation store, VectorDB Top-K/최신성 로직을 검증합니다.

## 주의사항

- 이 프로젝트는 투자 조언이나 매수/매도 추천을 제공하지 않습니다.
- 오래된 PDF와 잘못 추출된 표는 답변 품질에 영향을 줄 수 있습니다.
- `.env`에는 실제 API 키가 들어가므로 커밋하지 마세요.
- FAISS의 `index.pkl`은 pickle 역직렬화를 사용하므로 신뢰할 수 없는 파일을 로드하지 마세요.

## 개선 제안

- PDF chunk 생성 시 page number, bbox, text offset을 함께 저장해 참고 문서 클릭 시 PDF의 해당 chunk 위치로 바로 이동
- `최근 7일`, `올해`, `전분기` 같은 상대 날짜 표현 추가 확장
- 비용 절감을 위한 rerank 조건부 실행 또는 rerank 결과 캐싱
- crawler HTML fixture를 늘려 사이트 구조 변경 감지 강화
- Streamlit 대화 목록 검색, 제목 수정, export 기능 추가
