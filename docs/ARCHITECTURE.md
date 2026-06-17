# Architecture

Finance LLM은 증권사 PDF 리포트를 수집, 추출, 색인하고 질문에 맞는 RAG 답변을 생성하는 로컬 애플리케이션입니다.

## 1. 구성 요소

| 영역 | 구현 |
| --- | --- |
| 데이터 수집 | `src/core/report_crawler.py` |
| GUI 백그라운드 업데이트 | `src/core/data_update_jobs.py` |
| PDF 추출 | `src/core/pdf_extraction.py`, `src/core/compare_pdf_extractors.py` |
| 메타데이터 저장 | SQLite `data/reports.db` |
| 대화 저장 | SQLite `data/conversations.db` |
| 임베딩 색인 | FAISS `data/vector_db` |
| 문제 신고 저장 | 텍스트 파일 `debug/issue_report_*.txt` |
| 생성 모델 | OpenRouter `deepseek/deepseek-v4-flash` |
| 임베딩 모델 | OpenRouter `baai/bge-m3` |
| Rerank | 기본 비활성화, 필요 시 OpenRouter `cohere/rerank-v3.5` |
| 검색/답변 그래프 | LangGraph nodes in `src/nodes/` |
| 답변 참조 링크 | `src/utils/citations.py` |
| UI | Streamlit `apps/gui/app.py` 중심. CLI `apps/cli/app.py`는 deprecated 호환 모드 |

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

Streamlit GUI의 사이드바 데이터 업데이트는 `data_update_jobs`를 별도 Python
프로세스로 실행합니다. 사용자가 지정한 업데이트 기간 중 이미 임베딩 완료된
날은 건너뛰고, 남은 날짜만 다운로드와 임베딩 대상으로 전달합니다. 진행 상태는
`logs/data_update_jobs/status.json`에 기록되며 GUI는 fragment로 이 파일만 주기적으로
읽어 다운로드/임베딩/완료 단계를 갱신합니다.

## 3. 수집 계층

`src/core/report_crawler.py`는 리포트 카테고리와 날짜 범위를 기준으로 PDF를 다운로드합니다.

주요 설정은 `src/configs/settings.py`의 `CONFIG_SPECS`에서 단일 관리합니다. 대표적으로 `CRAWLER_CATEGORIES`, `CRAWLER_MODE`, `CRAWLER_TARGET_DATE`, `CRAWLER_TARGET_COUNT`, `CRAWLER_LOOKBACK_DAYS`, `CRAWLER_MAX_LOOKBACK_DAYS`가 있으며 `.env.example`은 이 정의에서 자동 생성됩니다.

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
   - `should_rewrite_with_history()`는 날짜만 바뀐 후속 질문, 직전 답변 범위를 가리키는 질문, 독립적인 새 검색 질문을 구분합니다.
   - `query_rewrite_node()`는 `rewritten_query`와 함께 `uses_chat_history`, `followup_scope_intent`를 state에 기록합니다.
2. Router: RDB 검색이 적합한지 VectorDB 검색이 적합한지 선택합니다.
   - GUI가 전달한 `prior_search_scope`와 `followup_scope_intent`를 함께 봅니다. 후속 질문이고 현재 질문에 새 날짜 조건이 없으면 직전 답변의 `search_filters`, `temporal_context`, 참고 PDF `file_names`를 검색 조건으로 재사용합니다.
   - 현재 질문에 새 날짜 조건이 있으면 직전 검색 범위보다 현재 질문의 날짜/필터를 우선합니다.
3. RDB path: SQL guardrail을 통과한 read-only SELECT만 SQLite에 실행합니다.
4. VectorDB path: FAISS 후보를 넉넉히 가져온 뒤 metadata filter, 최신성 가중치, 선택형 rerank를 적용합니다.
   - `metadata_matches()`는 날짜/종목/증권사/리포트 유형뿐 아니라 `file_names` 필터도 처리해 직전 답변에 실제 사용된 PDF 범위로 재검색할 수 있습니다.
5. VectorDB 결과가 없으면 해당 thread의 short-term memory 영향을 제거하고 원질문으로 한 번 더 검색합니다.
6. Final response: 검색 결과와 참고 문서를 바탕으로 답변을 생성합니다.
   - GUI는 성공한 assistant 메시지 metadata에 `rerank_info`와 `search_scope`를 저장합니다. `search_scope`는 다음 후속 질문의 `prior_search_scope` 입력으로 전달됩니다.
   - GUI source renderer는 같은 PDF에서 검색된 여러 chunk를 `group_sources_by_document()`로 문서 단위로 묶고, `document_rank_aliases()`로 원래 chunk rank를 1부터 시작하는 문서 표시 번호로 다시 매깁니다.

## 6. 메타데이터 필터와 최신성 가중치

`src/core/metadata_filters.py`는 질문에서 다음 정보를 추론합니다.

- 종목명 또는 대상명
- 증권사명
- 리포트 유형
- 날짜, 월, 분기, 연도 표현

`src/nodes/vectordb.py`는 필터링 전 후보를 충분히 가져온 뒤 조건에 맞는 문서를 고릅니다. `RECENCY_WEIGHT`가 0보다 크면 최신 `report_date` 문서에 점수 가중치를 부여합니다.

후속 질문에서 직전 답변의 문서 범위를 재사용할 때는 `file_names` 필터가 함께 들어갈 수 있습니다. 이 필터는 종목/날짜 조건만으로는 같은 범위가 보장되지 않는 경우를 막기 위해, 직전 답변에 실제 사용된 PDF 파일명과 일치하는 chunk만 남깁니다.

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

- GUI는 저장된 thread와 메시지를 불러와 화면에 표시합니다.
- deprecated CLI는 기존 호환성을 위해 기본 thread를 계속 사용하지만 신규 기능 개발 대상이 아닙니다.
- assistant 메시지는 참고 문서와 rerank 정보를 metadata로 함께 저장할 수 있습니다.
- GUI assistant 메시지는 성공 시 `search_scope` metadata를 추가로 저장합니다. 값에는 `route`, `search_filters`, `temporal_context`, `scope_source`, `file_names`가 포함될 수 있으며, 이후 같은 thread의 후속 질문에서 가장 최근 성공 답변의 scope를 재사용합니다.

## 9. 문제 신고

`src/core/issue_report_store.py`는 GUI의 `⚠ 신고` 버튼에서 제출된 문제 내용을
읽기 쉬운 텍스트 파일로 저장합니다. 기본 저장 위치는 `debug/`이며 파일명은
`issue_report_*.txt` 형식입니다.

- 신고 내용에는 문제 유형, 사용자가 작성한 설명, thread 정보, 선택 시 대화 전문과 축약된 metadata가 포함됩니다.
- `debug/*`는 Git에 포함하지 않고 `debug/.gitkeep`만 폴더 유지용으로 추적합니다.
- 신고 파일은 사용자가 내용을 확인한 뒤 복사하거나 `.txt` 파일로 전달하는 로컬 디버깅 산출물입니다.

## 10. 참고 문서 네비게이션과 PDF 위치 이동 한계

현재 GUI는 답변 안의 `[숫자]` 참조를 참고 문서 expander의 해당 항목으로
이동하는 내부 anchor 링크로 변환합니다. Streamlit 상단 바에 가려지지 않도록
anchor에는 scroll margin을 둡니다.

검색 결과는 chunk 단위 rank를 갖지만, GUI 참고 문서 목록은 PDF 문서 기준으로
중복을 제거합니다. 같은 PDF에서 여러 chunk가 검색되면 답변 citation은 하나의
문서 번호로 alias되고, 문서 목록 표시 번호는 원래 chunk rank가 아니라
1부터 순차적으로 다시 부여됩니다. 따라서 원래 검색 rank가 `[1], [2], [4], [13]`
처럼 건너뛰어도 화면에는 `[1], [2], [3], [4]`처럼 표시됩니다.

참고 문서의 `열기` 버튼은 로컬 PDF 파일 자체를 엽니다. PDF 파일의 정확한
페이지나 좌표로 바로 이동하려면 chunk 생성 단계에서 page number, bounding box,
text offset 같은 위치 metadata를 함께 저장해야 합니다. 현재 구조에서는 해당
정밀 위치 이동까지는 제공하지 않으며, 별도 개선 과제로 남겨두는 것이 좋습니다.

## 11. 데이터 상태와 캘린더

`src/core/status.py`는 DB/FAISS/설정 상태를 읽기 전용으로 요약합니다. GUI의
리포트 캘린더는 `reports.is_embedded=1`인 날짜만 데이터 있음으로 표시하므로,
다운로드만 끝난 PDF가 아니라 실제 검색 가능한 임베딩 완료 리포트 기준으로
초록색 상태가 표시됩니다.

## 12. 검증

주요 검증 명령은 다음과 같습니다.

```bash
python -m py_compile apps/gui/app.py apps/cli/app.py src/core/data_update_jobs.py src/graphs/main_graph.py src/nodes/vectordb.py
python -m pytest -q
```
