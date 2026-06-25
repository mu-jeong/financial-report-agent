# Monitoring Mode 개발 내용

이 문서는 Finance LLM의 Monitoring Mode에서 개발된 내용을 전체 모니터링과 개별 Chat Monitoring으로 나누어 기록한다. Monitoring Mode는 일반 채팅 UX와 분리된 개발자용 진단 화면이며, `.env`의 `MONITORING_MODE=true`일 때만 노출된다.

## 1. 설계 목표

Monitoring Mode의 목적은 단순 사용량 통계가 아니라 RAG 품질 개선과 회귀 확인에 필요한 근거를 남기는 것이다.

- 데이터 준비 상태, 설정, 평가셋, parsing 비교, 전체 대화 품질을 한 화면에서 확인한다.
- 특정 assistant 응답의 query rewrite, scope/filter, routing, retrieval/rerank, source, citation 흐름을 추적한다.
- 답변 실패나 품질 저하를 issue report와 regression 후보로 연결한다.
- 일반 사용자 화면에는 진단 metadata를 노출하지 않고, 개발자가 명시적으로 켠 경우에만 표시한다.

관련 주요 파일은 다음과 같다.

| 영역 | 파일 |
| --- | --- |
| Streamlit UI | `apps/gui/app.py` |
| 모니터링 집계/평가/trace helper | `src/core/monitoring.py` |
| 그래프 상태 metadata 계약 | `src/graphs/state.py` |
| 검색 scope/routing metadata | `src/nodes/search_scope.py`, `src/nodes/router.py` |
| VectorDB retrieval metadata | `src/nodes/vectordb.py` |
| RDB 실행 metadata | `src/nodes/rdb.py` |
| 고정 평가셋 | `tests/fixtures/evaluation_dataset.json` |
| 고정 snapshot manifest | `tests/fixtures/eval_snapshot/manifest.json` |
| 평가 실행 산출물 | `debug/evaluation_runs/evaluation_run_*.json` |
| regression 후보 산출물 | `debug/regression_candidates/*.json` |

## 2. 화면 구조

`MONITORING_MODE=true`이면 Streamlit 진입 구조가 바뀐다.

```text
Sidebar
└─ Page radio
   ├─ Chat
   │  ├─ Chat tab
   │  └─ Chat Monitoring tab
   └─ 전체 Monitoring
      ├─ 데이터/설정
      ├─ 실험 실행
      ├─ 고정 테스트셋
      ├─ Parsing engines
      ├─ 전체 Monitoring
      └─ Issue reports
```

구현상 상위 page label은 `build_monitoring_page_labels()`가 `Chat`, `전체 Monitoring`을 반환한다. `Chat` page에서는 일반 채팅 tab과 현재 선택 chat 전용 `Chat Monitoring` tab이 같이 표시된다. `전체 Monitoring` page에서는 현재 선택 chat과 무관한 전역 진단 tab만 표시한다. 이 분리는 전체 운영 상태와 개별 대화 trace가 섞이지 않도록 하기 위한 UX 결정이다.

## 3. 전체 Monitoring

전체 Monitoring은 선택된 chat 하나가 아니라 저장소, 평가 기준선, 모든 thread, issue report를 기준으로 시스템 상태를 본다.

### 3.1 데이터/설정 tab

`render_global_monitoring_page()`의 `데이터/설정` tab은 `get_data_status()` 결과를 표시한다.

주요 지표:

- 리포트 수: `db_status['total_reports']`
- 임베딩 완료 수: `db_status['embedded_reports']`
- 미완료 수: `db_status['pending_reports']`
- 검색 커버리지: `status['search_coverage_ratio']`
- FAISS index 존재 여부: `vector_status['has_faiss_index']`
- vector file 수: `vector_status['file_count']`
- parent chunk 수: `db_status['parent_chunks']`
- 다운로드 PDF 수: `status['downloaded_pdfs']`
- 현재 파이프라인 설정: generation model, embedding model, extraction engine, parent-child chunk 사용 여부, reranker 사용 여부, search top-k, test limit
- 날짜별 데이터 캘린더 원천: `report_date_counts`, `report_date_type_counts`

이 tab은 검색 품질 문제가 실제 retrieval 로직 때문인지, DB/index/PDF 준비 상태 때문인지 먼저 확인하는 진입점이다.

### 3.2 실험 실행 tab

`_render_experiment_monitoring()`은 고정 evaluation dataset을 실제 graph에 통과시켜 pass/fail 결과를 저장한다.

지원 모드:

1. `current_data`
   - 현재 `data/reports.db`와 `data/vector_db`를 사용한다.
   - 로컬 데이터가 바뀌면 baseline 비교가 흔들릴 수 있다.
2. `fixed_snapshot`
   - `tests/fixtures/eval_snapshot`의 고정 DB/index를 별도 Python 프로세스로 사용한다.
   - 실행 전 `validate_evaluation_snapshot()`으로 dataset metadata와 manifest/file 존재 여부를 검증한다.

실행 입력:

- 실행할 case id multiselect
- latency threshold seconds

저장 위치:

- `debug/evaluation_runs/evaluation_run_<run_id>.json`

평가 결과는 `run_evaluation_dataset()`과 `evaluate_dataset_case_result()`가 만든다. 개별 case는 다음 기준으로 판정한다.

| 기준 | 의미 |
| --- | --- |
| `route_pass` | 실제 route가 `expected_route`와 일치 |
| `filter_pass` | 기대 metadata filter가 실제 `search_filters`에 포함 |
| `source_hit` | 기대 source 파일이 실제 source 안에 포함 |
| `hit_at_k` | 기대 source가 몇 번째 source에서 발견됐는지 |
| `citation_valid` | 답변의 `[n]` citation이 source 개수 범위 안에 있는지 |
| `latency_pass` | latency가 threshold 이하인지 |
| `no_result` | VectorDB no-result 여부 |

전체 run summary는 pass rate, route/filter/source/citation pass rate, no-result rate, 평균 latency를 제공한다. 같은 execution mode의 직전 run이 있으면 `compare_evaluation_runs()`로 delta를 표시한다. mode가 다른 run끼리는 비교하지 않는다.

실패 case는 `build_evaluation_failure_actions()`가 다음 조치 후보로 분류한다.

- route 실패: router/query classification 확인
- filter 실패: metadata filter extraction 확인
- source 실패: retrieval index, chunking, rerank 확인
- citation 실패: citation generation/removal 확인
- latency 실패: latency budget 확인
- no-result: filter 완화 retry 또는 데이터 업데이트 확인

### 3.3 고정 테스트셋 tab

`고정 테스트셋` tab은 `tests/fixtures/evaluation_dataset.json`의 coverage를 요약한다.

표시 내용:

- dataset version
- case 수
- expected source 수
- snapshot date
- stability policy
- route case coverage
- monitoring dimensions 분포
- 변경 허용 사유
- 평가 case 목록

`docs/EVALUATION_DATASET.md`에 정의된 정책처럼, 이 fixture는 성능 개선 전후를 비교하기 위한 기준선이다. source PDF 본문은 포함하지 않고 question, expected route/filter/source/RDB expectation 같은 재현 가능한 기대값만 저장한다.

### 3.4 Parsing engines tab

`_render_parsing_engine_evaluation()`은 PDF extraction engine 비교를 실행한다.

입력:

- PDF 파일 또는 폴더 경로
- 비교할 parsing engine 목록
- limit
- raw 저장 여부
- sample 저장 여부
- sample character 수

출력:

- run id
- 대상 file 수
- engine 수
- raw 여부
- engine별 summary
- CSV/JSON/sample directory 경로
- PDF별 row
- error row

이 tab은 retrieval 이전 단계인 PDF parsing 품질과 latency를 비교하기 위한 것이다. parsing 품질 변화가 chunking/retrieval 품질 변화로 이어질 수 있으므로 전체 개선 루프의 앞단 지표로 둔다.

### 3.5 전체 Monitoring tab

`_render_global_monitoring()`은 모든 thread의 assistant 응답 metadata를 집계한다. 원문을 기본 노출하지 않고 운영 품질 신호만 보여준다.

집계 함수는 `summarize_all_chat_threads()`이다.

주요 지표:

- thread 수
- assistant message 수
- failure rate
- no-result rate
- average latency
- p95 latency
- data integrity issue 수
- status counts
- route counts
- recent failed responses

`recent_failed_responses`는 실패한 assistant 응답의 thread id/name, created_at, status, route, latency, no_vector_results, error를 최근순으로 최대 10개 보여준다.

데이터 무결성은 `summarize_data_integrity()`가 계산한다.

| check | 기준 |
| --- | --- |
| `faiss_index` | FAISS index 존재 여부 |
| `embedding_backlog` | pending report가 0이면 pass, 남아 있으면 warning |
| `pdf_vs_db` | 다운로드 PDF 수가 embedded report 수 이상이면 pass |
| `search_coverage` | 전체 report 중 embedded 비율이 95% 이상이면 pass |

### 3.6 Issue reports tab

`_render_issue_report_monitoring()`은 `debug/issue_report_*.txt`로 저장된 사용자 신고를 전체 개선 루프의 입력으로 모아 보여준다.

표시 내용:

- report 수
- report가 연결된 thread 수
- category 수
- category별 count
- report rows: created_at, id, category, thread, file path, preview
- 선택 report 상세 원문

선택한 report는 `promote_issue_report_to_eval_candidate()`를 통해 regression 후보 artifact로 저장할 수 있다.

저장 위치:

- `debug/regression_candidates/<candidate_id>.json`

이 단계는 수동 신고를 이후 evaluation dataset case로 승격하기 전의 중간 저장소 역할을 한다.

## 4. 개별 Chat Monitoring

개별 Chat Monitoring은 현재 선택된 thread 하나의 assistant 응답을 대상으로 한다. 전체 모니터링이 운영 상태의 폭을 보는 화면이라면, Chat Monitoring은 특정 답변이 왜 그렇게 생성됐는지 trace하는 화면이다.

### 4.1 대화 요약 지표

`render_chat_monitoring_page()`는 현재 thread의 messages를 읽고 `summarize_chat_messages()`로 요약한다.

표시 지표:

- message 수
- assistant message 수
- 평균 source 수
- 평균 latency
- status counts
- route counts

이 요약은 현재 대화가 정상/실패 응답을 얼마나 포함하는지, VectorDB/RDB route가 어떻게 분포하는지 빠르게 확인하기 위한 것이다.

### 4.2 Assistant response rows

`build_message_monitoring_rows()`는 assistant 응답마다 안전한 monitoring row를 만든다. row는 원문 전체가 아니라 preview와 metadata 중심으로 구성된다.

주요 column:

| column | 의미 |
| --- | --- |
| `message_id` | assistant message id |
| `created_at` | 생성 시각 |
| `user_question_preview` | 직전 user 질문 또는 metadata question preview |
| `assistant_preview` | assistant 답변 preview |
| `status` | succeeded/failed/unknown |
| `route` | vectordb/rdb 등 route |
| `latency_seconds` | 답변 생성 latency |
| `source_count` | 문서 단위로 묶은 source 수 |
| `search_filters` | 적용된 metadata filter |
| `scope_source` | prior scope 사용 여부 등 scope 출처 |
| `scope_decision_reason` | scope decision reason |
| `no_vector_results` | VectorDB no-result 여부 |
| `selected_file_names` | 선택 source 파일명 목록 |
| `rdb_row_count` | RDB route 결과 row 수 |
| `error` | 실패 시 error |
| `label` | 상세 선택용 compact label |

이 row는 특정 답변을 골라 trace viewer로 들어가기 전의 목록 역할을 한다.

### 4.3 응답 선택 trace viewer

사용자가 assistant response row 중 하나를 선택하면 다음 세 가지가 계산된다.

- `build_message_trace_detail(selected_message, user_question=selected_user_question)`
- `build_response_diff(selected_message, previous_message)`
- `build_chat_trace_debug_hints(selected_message, previous_message, user_question=selected_user_question)`

`previous_message`는 `previous_successful_assistant()`가 선택 응답 직전의 성공한 assistant 응답을 찾는다. 따라서 diff는 단순 직전 row가 아니라 직전 성공 응답 기준이다.

Trace viewer는 선택 응답을 세 개의 목적별 tab으로 나누어 보여준다. 자주 보는 판단 근거는 기본 tab에 모으고, raw retrieval/source/citation detail은 `Advanced diagnostics` 안에 접어 둔다.

#### 4.3.1 Trace summary

`Trace summary`는 선택 응답을 빠르게 triage하기 위한 기본 화면이다. `build_message_trace_summary()`가 상세 trace에서 자주 보는 값을 평탄화한다.

표시 metadata:

- 원질문
- rewritten query
- `followup_scope_intent`
- route
- `scope_source`
- `scope_reason`
- `industry_term`
- `search_filters`
- `candidate_count_after_filter`
- source count
- citation valid 여부
- debug hint 수
- 직전 성공 응답과의 diff 존재 여부

이 tab 안에서 `Debug hints`와 `Previous vs selected diff`도 함께 보여준다. 따라서 후속 질문에서 날짜 필터가 사라졌는지, prior scope를 놓쳤는지, route가 갑자기 바뀌었는지 같은 흔한 회귀는 첫 화면에서 확인할 수 있다.

#### 4.3.2 Scope / routing

`Scope / routing`은 검색 범위와 route 결정 흐름을 한 화면에서 이어서 보는 상세 화면이다. 기존의 `Query rewrite / follow-up`, `Scope / filters`, `Routing` 상세 JSON을 같은 tab에 묶는다.

표시 metadata:

- Query rewrite / follow-up
  - 원질문
  - rewritten query
  - chat history 사용 여부
  - `followup_scope_intent`
  - `scope_source`
  - `scope_decision`
- Scope / filters
  - `search_filters`
  - `temporal_context`
  - `selection_context`
  - `industry_lookup_context`
  - 저장된 `search_scope`
- Routing
  - route
  - routing context
  - route hint
  - vector intent 여부
  - full period request 여부

날짜, 종목, 증권사, 리포트 유형, 파일 범위 같은 필터가 의도대로 설정됐는지 확인한다. `search_scope`는 GUI가 성공 응답 metadata에 저장하고, 다음 후속 질문의 `prior_search_scope` 입력으로 전달할 수 있다. `search_scope_node`는 `routing_context`를 만들고, router는 이 hint를 사용해 RDB/VectorDB 경로를 결정한다.

#### 4.3.3 Advanced diagnostics

`Advanced diagnostics`는 평소에는 잘 보지 않는 raw detail을 expander로 접어 둔다. 정밀 디버깅이나 issue report 작성 시에만 펼쳐 보면 된다.

##### Retrieval / rerank

표시 metadata는 `monitoring.retrieval`이다. VectorDB route에서는 `vectordb_node()`가 다음 지표를 남긴다.

- `fetch_k`
- `candidate_count_before_filter`
- `candidate_count_after_filter`
- `search_top_k`
- `use_reranker`
- `document_coverage_applied`
- `document_coverage_reason`
- `selected_source_count`
- `selected_file_names`
- score summary: `score`, `rerank_score`, `recency_score`, `final_score`의 min/max/avg

이 raw detail은 metadata filter가 과도했는지, rerank 이후 source가 특정 문서에 편중됐는지, document coverage가 적용됐는지 확인할 때 사용한다.

##### Sources

`rerank_info`를 표로 보여준다. VectorDB source에는 rank, target name, report date, title, broker, file name, report type 등 검색 source metadata가 들어간다. RDB route에서는 `rdb_sources`를 `rerank_info` 위치에 넣어 동일 UI에서 source를 볼 수 있게 한다.

특히 `report_type`은 섹션 follow-up에서 company/industry/market source가 섞였는지 확인하는 데 사용된다.

##### Answer / citations

표시 metadata:

- assistant answer preview
- source count
- 답변에서 사용한 citation rank 목록
- citation valid 여부

`citation_valid`는 답변의 `[n]` 참조가 문서 단위 source count 범위 안에 있는지 확인한다. source가 없으면 citation도 없어야 valid로 본다.

### 4.4 Previous vs selected diff

`build_response_diff()`는 선택 응답과 직전 성공 응답을 비교한다.

비교 항목:

- rewritten query 변경 여부
- route 변경 여부
- search filter diff: kept, added, removed, changed
- temporal context 변경 여부
- scope source 변경 여부
- scope decision 변경 여부
- source diff: previous/current count, count delta, added files, removed files
- retrieval diff: `candidate_count_after_filter` delta

이 diff는 후속 질문에서 날짜 필터가 사라졌는지, source coverage가 줄었는지, route가 갑자기 바뀌었는지 같은 회귀를 빠르게 찾기 위한 것이다.

### 4.5 Debug hints

`build_chat_trace_debug_hints()`는 흔한 RAG 실패 패턴을 rule-based로 감지한다. 현재 구현된 감지 패턴은 다음과 같다.

| 감지 패턴 | 조건 | 의도 |
| --- | --- | --- |
| 후속 질문인데 prior scope 미사용 | `followup_scope_intent=True`, `scope_source != prior_search_scope`, `scope_decision` 없음 | 후속 질문 scope 상실 탐지 |
| 날짜 필터 손실 | 직전 응답에는 `report_date_start/end`가 있었는데 현재 응답에는 없음 | 기간 조건 회귀 탐지 |
| source 1개 문서 편중 | `rerank_info`가 2개 이상인데 unique file이 1개 이하 | 복수 문서 요청의 source 다양성 부족 탐지 |
| 필터 후 후보 0개 | `candidate_count_after_filter == 0` | metadata filter 과도 적용 탐지 |
| route/content intent 불일치 | `route=rdb`인데 질문에 `주요 내용`, `요약`, `리스크`, `투자포인트` 포함 | 본문 검색이 필요한 질문이 RDB로 간 경우 탐지 |
| document coverage 미적용 | 질문에 `전체`, `각각`, `리포트들`, `목록`, `비교`, `모두`, `여러`가 있고 `document_coverage_applied is False` | 여러 문서 질의에서 coverage 보장 누락 탐지 |

Debug hint는 확정 판정이 아니라 조사 시작점이다. false positive가 있을 수 있으므로 trace detail과 diff를 함께 확인해야 한다.

### 4.6 선택 trace에서 issue report 생성

Chat Monitoring 상세 화면의 `Create issue report with selected trace` 버튼은 선택 응답의 trace를 issue report로 저장한다.

`build_chat_trace_issue_context()`가 포함하는 정보:

- thread id/name
- 제출 출처: `chat_monitoring_trace`
- 선택 응답 직전 user question
- 선택 assistant 응답 compact metadata
- 직전 성공 assistant 응답 compact metadata
- trace detail
- previous vs selected diff
- debug hints

이 report는 전체 Monitoring의 Issue reports tab에서 다시 볼 수 있고, 필요하면 regression 후보로 승격할 수 있다.

## 5. 응답 metadata 수집 흐름

답변 생성이 성공하면 GUI background job은 `compact_graph_monitoring_metadata()`로 graph final state를 compact metadata로 바꿔 assistant message에 저장한다.

```text
user question
  ↓
graph_app.invoke(...)
  ↓
final_state
  ├─ route
  ├─ search_filters / temporal_context / selection_context
  ├─ scope_decision / scope_source / search_scope
  ├─ rerank_info 또는 rdb_sources
  ├─ monitoring_metrics.retrieval
  └─ monitoring_metrics.rdb
  ↓
compact_graph_monitoring_metadata()
  ↓
assistant message metadata
  ↓
Chat Monitoring / 전체 Monitoring / issue report
```

저장되는 compact metadata의 핵심 구조:

```text
metadata
├─ status
├─ question
├─ route
├─ latency_seconds
├─ search_filters
├─ temporal_context
├─ selection_context
├─ scope_decision
├─ no_vector_results
├─ rerank_info
├─ search_scope
└─ monitoring
   ├─ query_rewrite
   │  ├─ rewritten_query
   │  ├─ uses_chat_history
   │  └─ followup_scope_intent
   ├─ retrieval
   │  ├─ source_count
   │  ├─ score_summary
   │  └─ vectordb_node의 retrieval metrics
   └─ rdb
      ├─ sql_query
      ├─ row_count
      ├─ column_count
      ├─ guardrail_blocked
      └─ result_preview
```

실패 응답은 `status=failed`, `error`, `latency_seconds`를 저장한다. 전체 Monitoring의 failure rate와 recent failures는 이 metadata를 사용한다.

## 6. 현재 한계와 주의점

- Debug hints는 rule-based이므로 모든 실패를 포착하지 못한다.
- keyword 기반 hint는 질문 표현에 따라 false positive/false negative가 발생할 수 있다.
- `current_data` evaluation은 현재 DB/index 상태에 의존하므로 장기 baseline 비교에는 `fixed_snapshot`이 더 적합하다.
- Monitoring metadata는 compact 진단 정보이며, 전체 chain-of-thought나 LLM 내부 판단을 저장하지 않는다.
- Issue report와 evaluation run 산출물은 `debug/` 아래에 저장되며 Git에는 포함하지 않는다.
- Chat Monitoring row는 안전성을 위해 preview 중심이지만, issue report 원문에는 문제 재현에 필요한 정보가 포함될 수 있으므로 외부 전달 전 확인이 필요하다.

## 7. 검증

Monitoring 관련 주요 회귀 테스트는 다음 명령으로 실행한다.

```bash
python -m pytest tests/test_settings.py tests/test_monitoring.py tests/test_evaluation_dataset.py tests/test_evaluation_snapshot_runner.py -q
```

주요 테스트 범위:

- Monitoring Mode 설정 파싱
- 데이터/평가셋 요약
- evaluation snapshot 검증
- chat monitoring row와 trace detail 생성
- previous vs selected diff
- debug hints 감지
- issue report context 생성
- 전체 thread summary와 data integrity summary
- evaluation run 비교와 failure triage
