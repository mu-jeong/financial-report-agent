# Architecture

Finance LLM은 증권사 PDF 리포트를 수집, 추출, 색인하고 질문에 맞는 RAG 답변을 생성하는 로컬 애플리케이션입니다.

## 1. 구성 요소

| 영역 | 구현 |
| --- | --- |
| 데이터 수집 | `src/core/report_crawler.py` |
| GUI 백그라운드 업데이트 | `src/core/data_update_jobs.py` |
| PDF 추출 | `src/core/pdf_extraction.py`, `src/core/compare_pdf_extractors.py` (`pymupdf`, `opendataloader`, `docling`, `pdf-to-markdown`) |
| 리포트/검색 메타데이터 | SQLite `DATA_ROOT/retrieval/v2/catalog.sqlite3` |
| 대화 저장 | SQLite `data/conversations.db` |
| 임베딩 색인 | Immutable `DATA_ROOT/retrieval/v2/snapshots/<snapshot_id>.faiss` + catalog membership |
| 문제 신고 저장 | `debug/issue_report_*.txt`와 같은 stem의 구조화 `.json` sidecar |
| 생성 모델 | OpenRouter `deepseek/deepseek-v4-flash` |
| 임베딩 모델 | OpenRouter `baai/bge-m3` |
| Rerank | 기본 비활성화, 필요 시 OpenRouter `cohere/rerank-v3.5` |
| 검색/답변 그래프 | LangGraph nodes in `src/nodes/` |
| 답변 참조 링크 | `src/utils/citations.py` |
| UI | Streamlit `apps/gui/app.py` 중심. GUI 답변 생성은 백그라운드 thread로 실행. CLI `apps/cli/app.py`는 deprecated 호환 모드 |

## 2. 데이터 흐름

```text
report_crawler → data/downloaded/*.pdf → embed_pipeline
  → source inventory hash 비교 → 신규·변경 PDF만 추출/chunk/embedding
  → 성공 batch를 작은 불변 검색 단위로 원자적 활성화
  → base snapshot + 활성 batch를 합성해 업데이트 중에도 검색
  → 변경 없는 chunk/vector를 재사용해 마지막에 한 번만 완전한 snapshot 게시
  → native catalog + immutable composite reader → answer + references
```

Streamlit GUI의 사이드바 데이터 업데이트는 `data_update_jobs`를 별도 Python 프로세스로 실행합니다. 이미 완료된 날짜/카테고리 조합을 제외한 날짜 범위만 crawler에 전달합니다. 임베딩 파이프라인은 전체 source inventory를 한 번 스캔한 뒤 신규·변경 PDF만 파싱·임베딩합니다. 성공한 문서는 batch commit 직후 검색 가능하고, 마지막 compaction은 base와 batch vector를 재임베딩하지 않고 재배치합니다. 진행 상태는 `logs/data_update_jobs/status.json`에 기록되며 GUI는 fragment로 이 파일을 주기적으로 읽습니다. 저장 구조와 장애 복구 계약은 [`CONTINUOUS_UPDATES.md`](CONTINUOUS_UPDATES.md)에 정리합니다.

## 3. 수집 계층

`src/core/report_crawler.py`는 리포트 카테고리와 날짜 범위를 기준으로 PDF를 다운로드합니다.

주요 설정은 `src/configs/settings.py`의 `CONFIG_SPECS`에서 단일 관리합니다. 대표적으로 `CRAWLER_CATEGORIES`, `CRAWLER_MODE`, `CRAWLER_TARGET_DATE`, `CRAWLER_TARGET_COUNT`, `CRAWLER_LOOKBACK_DAYS`, `CRAWLER_MAX_LOOKBACK_DAYS`가 있으며 `.env.example`은 이 정의에서 자동 생성됩니다.

특정 날짜에 데이터가 부족해도 이전 날짜를 이어서 탐색해 목표 개수 또는 최대 lookback 범위까지 수집합니다.

## 4. 추출 및 색인 계층

추출과 chunking은 다음 순서로 처리합니다.

1. `extract_pdf_text()`로 PDF 텍스트 또는 Markdown을 추출합니다.
   - 지원 엔진은 `pymupdf`, `opendataloader`, `docling`, `pdf-to-markdown`입니다.
   - 모든 엔진 출력은 색인 전에 표 제거 계약을 통과합니다. PyMuPDF는 기본 `find_tables()`로 찾은 표 BBox와 면적의 50%를 초과해 겹치는 텍스트 block만 제외하고, OpenDataLoader JSON table node와 Docling table structure는 비활성/제거합니다. 별도 off 옵션이 없는 CLI 출력도 공통 Markdown/HTML/plain-text table 제거 후처리를 거칩니다.
   - 배포 템플릿의 production embedding은 기본 `pymupdf`가 실패하면 명시된 `PDF_EXTRACTION_FALLBACK_ENGINE=opendataloader`를 한 번 시도합니다. 새 키가 없는 기존 설정, 빈 값, 또는 primary와 다른 pending extractor override는 자동 fallback하지 않습니다. Native V2 incremental build는 active embedding profile과 동일한 추출 정책만 재사용합니다.
2. `MarkdownHeaderTextSplitter`와 `RecursiveCharacterTextSplitter`로 문서를 chunk로 나눕니다.
3. 신규·변경 문서만 처리해 작은 immutable FAISS segment를 catalog transaction과 함께 활성화합니다. reader와 SQL projection은 base snapshot과 현재 segment head를 같은 revision으로 읽습니다. 이전 버전은 새 버전이 성공할 때까지 유지되며 삭제는 provider 호출 없이 즉시 overlay에서 제외됩니다. 작업 종료 시 기존 parent/chunk/vector를 재사용해 완전한 candidate catalog와 새 immutable FAISS snapshot을 한 번 검증·게시합니다. 검색에서 제외된 segment artifact는 열린 lazy request를 보호하기 위해 소유 base의 GC까지 보존한 뒤 공용 snapshot garbage collector가 게시 후와 정상 시작 시 재조정하며, 운영 상태에는 사용자 용어로 정리 대기 파일 수·용량·최장 보존 시간을 표시합니다.

Profile 변경은 incremental writer가 거부합니다. `tools\recovery\REBUILD_V2.bat --check`로 상태를 확인한 뒤 검증된 full native successor를 준비·게시해야 합니다.

## 5. 검색 계층

LangGraph는 대략 다음 노드로 구성됩니다.

1. Query rewrite: 사용자 질문을 검색과 SQL 생성에 적합하게 정리합니다.
   - `should_rewrite_with_history()`는 날짜만 바뀐 후속 질문, 직전 답변 범위를 가리키는 질문, 독립적인 새 검색 질문을 구분합니다.
   - `query_rewrite_node()`는 `rewritten_query`와 함께 `uses_chat_history`, `followup_scope_intent`를 state에 기록합니다.
2. Search scope: `src/nodes/search_scope.py`가 metadata filter, 직전 답변 scope 재사용, scope-selection 요청을 결정합니다.
   - GUI가 전달한 `prior_search_scope`와 `followup_scope_intent`를 함께 봅니다. 후속 질문이고 현재 질문에 새 날짜 조건이 없으면 직전 답변의 `search_filters`, `temporal_context`, 참고 PDF `file_names`를 검색 조건으로 재사용합니다.
   - 현재 질문에 새 날짜 조건이 있으면 직전 검색 범위보다 현재 질문의 날짜/필터를 우선합니다.
   - `src/core/followup_scope.py`는 직전 답변의 `answer_scope_index`를 사용해 "개별 종목/주요 기업", "섹터", "거시경제" 같은 섹션 deep-dive 질문을 `company`, `industry`, `economy` report type scope로 해석합니다.
   - 섹션 follow-up이 매칭되면 직전 날짜 범위는 유지하고 stale `file_names` scope는 제거합니다. 적용 근거는 `scope_decision`에 `matched_alias`, `inherited_filters`, `added_filters`, `dropped_filters`로 기록합니다.
   - "top target company"처럼 rewrite된 표현이 있더라도 섹션 follow-up이면 단일 top-company 선택으로 축소하지 않습니다.
3. Router: RDB 검색이 적합한지 VectorDB 검색이 적합한지 선택합니다.
4. RDB path: SQL guardrail을 통과한 read-only SELECT만 SQLite에 실행합니다.
5. VectorDB path: FAISS 후보를 넉넉히 가져온 뒤 metadata filter, 최신성 가중치, 선택형 rerank를 적용합니다.
   - VectorDB 진입점은 `DATA_ROOT`에서 canonical Native V2 catalog와 active snapshot을 확인합니다.
   - 요청은 catalog에서 scope를 compile하고 한 composite revision의 base/segment lease를 함께 잡은 뒤 DIRECT/SELECTOR/ADAPTIVE 전략으로 검색·hydrate합니다. 요청 중 새 batch나 최종 snapshot이 게시되어도 열린 요청은 고정된 revision을 끝까지 사용합니다. Native authority가 손상되면 fail closed합니다.
   - `metadata_matches()`는 날짜/종목/증권사/리포트 유형뿐 아니라 `file_names` 필터도 처리해 직전 답변에 실제 사용된 PDF 범위로 재검색할 수 있습니다.
   - `select_top_passages()`는 명시 파일 scope, 복수 문서/list 의도, 섹션 follow-up에서 `ensure_document_coverage()`를 적용해 최종 context가 한 PDF의 여러 chunk로만 채워지는 상황을 줄입니다. 적용 여부와 이유는 `document_coverage_applied`, `document_coverage_reason`으로 monitoring metadata에 남습니다.
6. VectorDB 결과가 없으면 해당 thread의 short-term memory 영향을 제거하고 원질문으로 한 번 더 검색합니다.
7. Final response: 검색 결과와 참고 문서를 바탕으로 답변을 생성합니다.
   - GUI는 성공한 assistant 메시지 metadata에 `selected_sources`와 `search_scope`를 저장합니다. `search_scope`는 다음 후속 질문의 `prior_search_scope` 입력으로 전달됩니다.
   - `selected_sources`에는 답변에 사용된 source의 `report_type`도 포함되어 이후 `answer_scope_index` 구성과 monitoring의 섹션별 source 확인에 사용됩니다. `rerank_info`는 기존 대화 호환용 read fallback입니다.
   - GUI source renderer는 같은 PDF에서 검색된 여러 chunk를 `group_sources_by_document()`로 문서 단위로 묶고, `document_rank_aliases()`로 원래 chunk rank를 1부터 시작하는 문서 표시 번호로 다시 매깁니다.

## 6. 메타데이터 필터와 최신성 가중치

`src/core/metadata_filters.py`는 질문에서 다음 정보를 추론합니다.

- 종목명 또는 대상명
- 증권사명
- 리포트 유형
- 날짜, 월, 분기, 연도 표현

`src/nodes/vectordb.py`는 필터링 전 후보를 충분히 가져온 뒤 조건에 맞는 문서를 고릅니다. `RECENCY_WEIGHT`가 0보다 크면 최신 `report_date` 문서에 점수 가중치를 부여합니다.

후속 질문에서 직전 답변의 문서 범위를 재사용할 때는 `file_names` 필터가 함께 들어갈 수 있습니다. 이 필터는 종목/날짜 조건만으로는 같은 범위가 보장되지 않는 경우를 막기 위해, 직전 답변에 실제 사용된 PDF 파일명과 일치하는 chunk만 남깁니다. 반대로 답변의 한 섹션을 더 자세히 묻는 경우에는 섹션 `report_type`을 새 범위로 사용하므로 기존 `file_names`를 제거합니다. 이렇게 해야 "개별 종목/주요 기업" 후속 질문이 직전 전체 답변의 일부 파일 목록이나 단일 top company로 과도하게 좁혀지지 않습니다.

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

검색 후보 수는 `SEARCH_TOP_K × SEARCH_CANDIDATE_MULTIPLIER`로 계산합니다. `SEARCH_CANDIDATE_MULTIPLIER`의 기본값은 `1`이며, 모니터링에서 문서 검색 누락이 확인될 때만 높입니다.

## 8. 대화 장기 메모리

`src/core/conversation_store.py`는 SQLite `data/conversations.db`에 thread와 message를 저장합니다.

- GUI는 저장된 thread와 메시지를 불러와 화면에 표시합니다.
- GUI 답변 생성은 백그라운드 thread에서 실행되고, assistant 메시지는 먼저 `status=running`으로 저장된 뒤 완료 시 `status=succeeded`, 실패 시 `status=failed` metadata로 갱신됩니다.
- deprecated CLI는 기존 호환성을 위해 기본 thread를 계속 사용하지만 신규 기능 개발 대상이 아닙니다.
- assistant 메시지는 참고 문서와 `selected_sources` 정보를 metadata로 함께 저장할 수 있습니다. 기존 `rerank_info`는 read fallback으로만 지원합니다.
- GUI assistant 메시지는 성공 시 `search_scope` metadata를 추가로 저장합니다. 값에는 `route`, `search_filters`, `temporal_context`, `scope_source`, `file_names`, `answer_scope_index`가 포함될 수 있으며, 이후 같은 thread의 후속 질문에서 가장 최근 성공 답변의 scope를 재사용합니다.

## 9. 문제 신고

`src/core/issue_report_store.py`는 GUI의 `⚠ 신고` 버튼에서 제출된 문제를 사람이 읽는 `.txt`와 모니터링·회귀 후보 승격용 구조화 `.json` sidecar로 함께 저장합니다. 기본 저장 위치는 `debug/`이며 두 파일은 같은 `issue_report_*` stem을 사용합니다.

- 신고 내용에는 문제 유형, 사용자가 작성한 설명, thread 정보, 선택 시 대화 전문과 축약된 metadata가 포함됩니다.
- `debug/*`는 Git에 포함하지 않고 `debug/.gitkeep`만 폴더 유지용으로 추적합니다.
- 두 파일 모두 로컬 디버깅 산출물이며 사용자가 내용을 확인한 뒤 전달해야 합니다.

### Chat Monitoring trace viewer

개별 chat monitoring은 마지막 응답 하나만 보여주지 않고, assistant 응답 row에서 디버깅할 턴을 선택하는 trace viewer를 제공합니다. Row에는 직전 user 질문 preview, assistant 답변 preview, route, latency, source 수, 검색 필터, `scope_source`, `scope_decision_reason`, no-result/error 상태, 선택 source 파일명이 포함됩니다.

선택된 응답은 사용 빈도에 따라 세 개의 tab으로 나뉘어 표시됩니다.

1. Trace summary: 원질문, rewrite 결과, follow-up 여부, route, scope source/reason, search filter, source 수, prior/search scope file count, citation 유효성 같은 핵심 진단값을 한 번에 보여줍니다. Debug hint와 직전 성공 응답 대비 diff도 이 tab에서 함께 확인합니다.
2. Scope / routing: `Query rewrite / follow-up`, `Scope / filters`, `Routing`의 상세 JSON을 한 흐름으로 묶어 보여줍니다. 날짜/종목/리포트 유형/file scope가 어떻게 정해졌고 route hint가 어떻게 만들어졌는지 확인하는 개발자용 상세 화면입니다.
3. Advanced diagnostics: 평소에는 접어둔 `State transitions`, `Retrieval / rerank`, `Answer / citations` 원자료를 expander로 제공합니다. query rewrite 전 `prior_search_scope`와 rewrite/search scope/routing/retrieval 이후 state 변화를 먼저 보고, 후보 수, coverage 적용 여부, score summary, citation 번호/유효성 같은 raw metadata를 필요할 때만 펼쳐 봅니다. Source 표는 다른 화면에서 중복 확인 가능하므로 Advanced diagnostics에서는 노출하지 않습니다.

또한 선택 응답과 직전 성공 assistant 응답의 filter/source/retrieval 차이를 비교하고, 날짜 필터 손실, prior scope 미사용, no-result, route/content-intent 불일치, document coverage 미적용 같은 흔한 RAG 실패 패턴은 rule-based debug hint로 노출합니다. 선택한 trace, 직전 응답, diff, debug hint는 `Create issue report with selected trace` 버튼으로 issue report에 바로 저장할 수 있습니다.

## 10. 참고 문서 네비게이션과 PDF 위치 이동 한계

현재 GUI는 답변 안의 `[숫자]` 참조를 참고 문서 expander의 해당 항목으로
이동하는 내부 anchor 링크로 변환합니다. 참고 문서 expander는 기본적으로 접힌
상태이며, 사용자가 필요할 때 직접 펼칩니다. Streamlit 상단 바에 가려지지 않도록
anchor에는 scroll margin을 둡니다.

검색 결과는 chunk 단위 rank를 갖지만, GUI 참고 문서 목록은 PDF 문서 기준으로
중복을 제거합니다. 같은 PDF에서 여러 chunk가 검색되면 답변 citation은 하나의
문서 번호로 alias되고, 문서 목록 표시 번호는 원래 chunk rank가 아니라
1부터 순차적으로 다시 부여됩니다. 따라서 원래 검색 rank가 `[1], [2], [4], [13]`
처럼 건너뛰어도 화면에는 `[1], [2], [3], [4]`처럼 표시됩니다.

참고 문서의 `열기` 버튼은 로컬 PDF 파일 자체를 엽니다. V2 chunk에는 parent content 내부의 `span_start`/`span_end`가 있지만 원본 PDF의 page number와 bounding box는 없습니다. 따라서 parent text 위치는 복원해도 GUI에서 원본 PDF의 정확한 페이지·좌표로 이동하는 기능은 아직 제공하지 않습니다.

## 11. 데이터 상태와 캘린더

`src/core/status.py`는 canonical runtime을 통해 catalog/FAISS/설정 상태를 읽기 전용으로 요약합니다. 캘린더는 active snapshot의 `active_reports`를 기준으로 검색 가능한 날짜를 표시합니다.

업데이트 대상 계산은 날짜와 report type을 함께 봅니다. 예를 들어 특정 날짜에 `company`는 있지만 `industry`가 없으면 사용자가 `industry`를 선택했을 때 그 날짜를 다시 수집/임베딩 대상으로 포함합니다. 집계에는 latest report objects와 active manifest를 사용합니다.

## 12. 검증

주요 검증 명령은 다음과 같습니다.

```bash
python -m compileall -q apps src scripts
python -m pytest -q
```

활성 Native V2의 profile을 변경하는 전체 successor 절차는 [Native V2 전체 재구축](migrations/v2/V2_REBUILD.md)을 참고합니다.
