# Architecture

Finance LLM은 증권사 PDF 리포트를 수집, 추출, 색인하고 질문에 맞는 RAG 답변을 생성하는 로컬 애플리케이션입니다.

## 1. 구성 요소

| 영역 | 구현 |
| --- | --- |
| 데이터 수집 | `src/core/report_crawler.py` |
| PDF 추출 | `src/core/pdf_extraction.py`, `src/core/compare_pdf_extractors.py` |
| 메타데이터 저장 | SQLite `data/reports.db` |
| 대화 저장 | SQLite `data/conversations.db` |
| 임베딩 색인 | FAISS `data/vector_db` |
| 생성 모델 | OpenRouter `deepseek/deepseek-v4-flash` |
| 임베딩 모델 | OpenRouter `baai/bge-m3` |
| Rerank | 기본 비활성화, 필요 시 OpenRouter `cohere/rerank-v3.5` |
| 검색/답변 그래프 | LangGraph nodes in `src/nodes/` |
| UI | CLI `apps/cli/app.py`, Streamlit `apps/gui/app.py` |

## 2. 데이터 흐름

```text
report_crawler
  -> data/downloaded/*.pdf
  -> SQLite reports
  -> embed_pipeline
  -> PDF extraction
  -> chunking / parent-child chunking
  -> OpenRouter embeddings
  -> FAISS vector_db + SQLite parent_chunks
  -> LangGraph retrieval
  -> final answer + references
```

## 3. 수집 계층

`src/core/report_crawler.py`는 리포트 카테고리와 날짜 범위를 기준으로 PDF를 다운로드합니다.

주요 설정:

- `CRAWLER_CATEGORIES`: `company`, `industry`, `economy`, `all` 또는 comma-separated 목록. 기본값은 `company`.
- `CRAWLER_MODE`: `LATEST` 또는 `SPECIFIC_DATE`.
- `CRAWLER_TARGET_DATE`: 특정 날짜 수집 시 기준 날짜.
- `CRAWLER_TARGET_COUNT`: 원하는 수집 개수.
- `CRAWLER_LOOKBACK_DAYS`: 기본 조회 기간.
- `CRAWLER_MAX_LOOKBACK_DAYS`: 목표 개수를 채우기 위해 과거로 확장할 최대 기간.

특정 날짜에 데이터가 부족해도 이전 날짜를 이어서 탐색해 목표 개수 또는 최대 lookback 범위까지 수집합니다.

## 4. 추출 및 색인 계층

`src/core/embed_pipeline.py`는 미처리 PDF를 가져와 다음 순서로 처리합니다.

1. `extract_pdf_text()`로 PDF 텍스트 또는 Markdown을 추출합니다.
2. `MarkdownHeaderTextSplitter`와 `RecursiveCharacterTextSplitter`로 문서를 chunk로 나눕니다.
3. `USE_PARENT_CHILD=true`이면 parent chunk는 SQLite `parent_chunks`에 저장하고 child chunk를 FAISS 검색 대상으로 사용합니다.
4. `build_embeddings_model()`이 OpenRouter 임베딩 모델을 생성합니다.
5. FAISS 인덱스를 새로 만들거나 기존 인덱스에 추가합니다.
6. `reports.is_embedded=1`로 처리 완료 표시합니다.

임베딩 모델이나 chunk 전략을 바꿀 때는 FAISS 인덱스를 초기화하고 재색인해야 합니다.

## 5. 검색 계층

LangGraph는 대략 다음 노드로 구성됩니다.

1. Query rewrite: 사용자 질문을 검색과 SQL 생성에 적합하게 정리합니다.
2. Router: RDB 검색이 적합한지 VectorDB 검색이 적합한지 선택합니다.
3. RDB path: SQL guardrail을 통과한 read-only SELECT만 SQLite에 실행합니다.
4. VectorDB path: FAISS 후보를 넉넉히 가져온 뒤 metadata filter, 최신성 가중치, 선택형 rerank를 적용합니다.
5. Final response: 검색 결과와 참고 문서를 바탕으로 답변을 생성합니다.

## 6. 메타데이터 필터와 최신성 가중치

`src/core/metadata_filters.py`는 질문에서 다음 정보를 추론합니다.

- 종목명 또는 대상명
- 증권사명
- 리포트 유형
- 날짜, 월, 분기, 연도 표현

`src/nodes/vectordb.py`는 필터링 전 후보를 충분히 가져온 뒤 조건에 맞는 문서를 고릅니다. `RECENCY_WEIGHT`가 0보다 크면 최신 `report_date` 문서에 점수 가중치를 부여합니다.

## 7. Rerank

Rerank는 비용을 고려해 기본값이 꺼져 있습니다.

```env
USE_RERANKER=false
```

켜면 OpenRouter의 `cohere/rerank-v3.5`를 사용합니다.

```env
USE_RERANKER=true
RERANK_PROVIDER=openrouter
RERANK_MODEL=cohere/rerank-v3.5
```

검색 후보 수는 `SEARCH_TOP_K`와 `RERANK_CANDIDATE_MULTIPLIER`에 의해 결정됩니다.

## 8. 대화 장기 메모리

`src/core/conversation_store.py`는 SQLite `data/conversations.db`에 thread와 message를 저장합니다.

- CLI는 기본 thread를 사용해 이전 대화를 이어갑니다.
- GUI는 저장된 thread와 메시지를 불러와 화면에 표시합니다.
- assistant 메시지는 참고 문서와 rerank 정보를 metadata로 함께 저장할 수 있습니다.

## 9. 참고 문서와 PDF 위치 이동 한계

현재 참고 문서는 검색된 chunk 텍스트와 metadata를 보여줍니다. PDF 파일의 정확한 페이지나 좌표로 바로 이동하려면 chunk 생성 단계에서 page number, bounding box, text offset 같은 위치 metadata를 함께 저장해야 합니다. 현재 구조에서는 해당 기능을 바로 제공하지 않으며, 별도 개선 과제로 남겨두는 것이 좋습니다.

## 10. 검증

주요 검증 명령은 다음과 같습니다.

```bash
python -m pytest -q
python -m py_compile apps/gui/app.py apps/cli/app.py src/core/conversation_store.py src/nodes/vectordb.py
```
