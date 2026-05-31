# 🏗 시스템 설계 철학 및 기술 아키텍처 (Architecture Guide)

Finance LLM은 로컬에 저장한 증권사 리포트를 SQLite + FAISS로 색인하고, LangGraph 기반 대화 흐름으로 RDB 조회·벡터 검색·도구 호출을 조합하는 개인용 금융 RAG 프로젝트입니다.

---

## 1. 핵심 기술 스택

| 영역 | 현재 구성 |
|---|---|
| 실행 환경 | Python 3.10+ |
| 생성 LLM | OpenRouter `deepseek/deepseek-v4-flash` 기본값 |
| 임베딩 | OpenRouter `baai/bge-m3` 기본값 |
| LLM fallback | `LLM_PROVIDER=gemini` 설정 시 Google Gemini 사용 가능 |
| Embedding fallback | `EMBEDDING_PROVIDER=gemini` 설정 시 Gemini embedding 사용 가능 |
| Orchestration | LangChain, LangGraph, Pydantic |
| Vector DB | 로컬 FAISS (`data/vector_db/`) |
| Relational DB | SQLite (`data/reports.db`) |
| PDF 추출 | `pymupdf` 기본, `marker`, `opendataloader` 선택 가능 |
| 실시간 주가 도구 | FinanceDataReader 기반 `get_stock_price` tool |
| SQL guardrail | `sqlglot` AST 검증 + SQLite read-only connection |

생성 모델과 임베딩 모델은 각각 factory로 분리되어 있습니다.

- 생성 모델: `src/llms/factory.py::build_chat_model()`
- 임베딩 모델: `src/llms/embeddings.py::build_embeddings_model()`

따라서 LangGraph 노드는 특정 vendor SDK를 직접 생성하지 않고, 설정에 따라 OpenRouter 또는 Gemini 구현을 받아 사용합니다.

---

## 2. 환경 변수와 모델 선택

기본 `.env` 구성:

```env
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=your_openrouter_api_key_here
GENERATION_MODEL=deepseek/deepseek-v4-flash
OPENROUTER_DATA_COLLECTION=deny

EMBEDDING_PROVIDER=openrouter
EMBEDDING_MODEL=baai/bge-m3
```

Gemini로 되돌릴 경우:

```env
LLM_PROVIDER=gemini
GENERATION_MODEL=gemini-2.5-flash
GEMINI_API_KEY=your_gemini_api_key_here
```

임베딩만 Gemini로 되돌릴 경우:

```env
EMBEDDING_PROVIDER=gemini
EMBEDDING_MODEL=models/gemini-embedding-001
GEMINI_API_KEY=your_gemini_api_key_here
```

> 임베딩 모델을 변경하면 기존 FAISS 인덱스는 반드시 삭제하고 재생성해야 합니다.

---

## 3. 데이터 적재 파이프라인 (`src/core/embed_pipeline.py`)

1. **파일 스캔 및 메타데이터 파싱**
   - `data/downloaded/` 아래 PDF를 스캔합니다.
   - 파일명 규칙: `[유형]_[YYYY-MM-DD]_[대상]_[증권사]_[제목].pdf`
2. **SQLite 동기화**
   - `reports` 테이블에 리포트 메타데이터와 `is_embedded` 상태를 저장합니다.
3. **PDF 텍스트 추출**
   - `EXTRACTION_ENGINE`에 따라 `pymupdf`, `marker`, `opendataloader`를 사용합니다.
   - 실패 시 가능한 경우 `pymupdf`로 fallback합니다.
4. **금융 리포트 정제**
   - 준법고지, 표 조각, 숫자 위주 행, 재무 레이블 등 검색 품질을 해치는 노이즈를 줄입니다.
5. **Parent-Child Chunking**
   - parent chunk는 SQLite `parent_chunks`에 저장합니다.
   - child chunk는 검색 단위로 FAISS에 저장합니다.
6. **OpenRouter 임베딩 + FAISS 저장**
   - 기본적으로 `baai/bge-m3` 임베딩을 OpenRouter `/embeddings` API로 생성합니다.
   - 저장 위치: `data/vector_db/`
7. **완료 표시**
   - 처리된 리포트는 SQLite `reports.is_embedded=1`로 갱신합니다.

---

## 4. 검색 및 응답 파이프라인

LangGraph 기본 흐름:

```text
START
  -> query_rewrite
  -> router
  -> rdb_sql_gen_node -> rdb_execute_node
      또는
     vectordb_node
  -> stock_price_tools?   # tool_calls가 있을 때만
  -> final_response_node? # tool 결과를 반영할 때만
  -> END
```

### RDB 경로

- 리포트 개수, 최근 발간일, 증권사별 목록처럼 메타데이터만으로 답할 수 있는 질문을 처리합니다.
- LLM이 SQL을 생성하지만, 실행 전 `sqlglot` guardrail이 아래를 강제합니다.
  - `SELECT`만 허용
  - `reports` 테이블만 허용
  - SQLite 연결은 `?mode=ro` read-only

### VectorDB 경로

- 리포트 본문 의미 검색을 처리합니다.
- FAISS에서 후보를 넉넉히 가져온 뒤, 질문에 명시된 종목명/증권사/리포트 유형을 `search_filters`로 후필터링합니다.
- child chunk가 검색되면 `parent_id`를 통해 SQLite `parent_chunks`에서 더 넓은 parent context를 가져옵니다.

### Tool Calling

- RDB/VectorDB 노드가 직접 답변 생성과 tool 호출 여부를 판단합니다.
- 최신 주가가 필요할 때만 `get_stock_price` tool을 호출합니다.
- tool이 호출된 경우에만 `final_response_node`가 tool 결과를 반영해 최종 답변을 생성합니다.

---

## 5. FAISS 재빌드 원칙

다음 경우에는 `data/vector_db`를 삭제하고 전체 재임베딩해야 합니다.

- `EMBEDDING_MODEL` 변경
- `EMBEDDING_PROVIDER` 변경
- chunk size / parent-child 설정 변경
- 정제 로직이 크게 변경되어 기존 chunk와 새 chunk가 섞이면 안 되는 경우

PowerShell 기준 재빌드:

```powershell
Copy-Item data/reports.db data/reports.backup.db -Force
if (Test-Path data/vector_db) { Remove-Item -Recurse -Force data/vector_db }
python -c "import sqlite3; con=sqlite3.connect('data/reports.db'); con.execute('UPDATE reports SET is_embedded=0'); con.execute('DELETE FROM parent_chunks'); con.commit(); con.close()"
python -m src.core.embed_pipeline --all
```

---

## 6. 보안 및 신뢰 경계

- `.env`와 실제 API 키는 git에 커밋하지 않습니다.
- `OPENROUTER_DATA_COLLECTION=deny`는 데이터 수집을 하지 않는 provider로 라우팅하도록 제한합니다.
- `FAISS.load_local(..., allow_dangerous_deserialization=True)`를 사용하므로, 외부에서 받은 `data/vector_db/index.pkl`은 신뢰하지 말고 재생성합니다.
- RDB 조회용 SQL은 guardrail과 read-only DB connection을 모두 통과해야 실행됩니다.

---

## 7. 검증 명령

```bash
python -m pytest -q
python apps/cli/app.py --status
python -m src.core.embed_pipeline --help
```

현재 테스트는 파일명 파싱, SQL guardrail, 상태 스냅샷, metadata filter, tool calling final response, OpenRouter embedding payload를 검증합니다.
