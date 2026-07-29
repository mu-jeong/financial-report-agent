# Monitoring Mode 개발 내용

이 문서는 Finance LLM의 Monitoring Mode에서 개발된 내용을 전체 모니터링과 개별 Chat Monitoring으로 나누어 기록한다. Monitoring Mode는 일반 채팅 UX와 분리된 개발자용 진단 화면이며, `.env`의 `MONITORING_MODE=true`일 때만 노출된다.

## 0. 현재 평가 데이터 상태

2026-07-30 기준으로 정식 evaluation fixture, multi-turn dataset과 fixed
snapshot은 없다. 과거 `tests/fixtures` 데이터와 그 데이터에서 생성된
evaluation run은 제거했다. 현재 live DB나 단위 테스트의 임시 입력을
승인된 baseline으로 간주하면 안 된다.

평가 실행기와 검증 코드는 미래 데이터에 사용할 계약으로만 유지한다.
데이터가 없으면 관련 UI는 준비 중 상태를 표시하고 데이터 의존 테스트는
skip한다. 실제 데이터 준비 후 작업은
[`docs/EVALUATION_DATASET.md`](EVALUATION_DATASET.md)에 기록되어 있다.

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
| 평가 데이터 준비 TODO | `docs/EVALUATION_DATASET.md` |
| 정식 evaluation dataset | 현재 없음. 향후 `tests/fixtures/evaluation_dataset.json`에 생성 |
| Multi-turn dataset | 현재 없음. 향후 `tests/fixtures/multiturn_evaluation_dataset.json`에 생성 |
| Fixed snapshot | 현재 없음. 향후 `tests/fixtures/eval_snapshot/`에 생성 |
| 평가 실행 산출물 | `debug/evaluation_runs/evaluation_run_*.json` |
| regression 후보 산출물 | `debug/regression_candidates/*.json` |

## 2. 화면 구조

`MONITORING_MODE=true`이면 Streamlit 진입 구조가 바뀐다.

```text
Sidebar
└─ Page radio
   ├─ Chat
   │  ├─ Chat
   │  └─ Chat Monitoring
   └─ 전체 Monitoring
      ├─ 운영 상태
      │  ├─ 데이터 상태
      │  ├─ 임베딩 누락 문서
      │  └─ 전체 응답 품질
      ├─ 평가/실험
      │  ├─ 고정 평가셋
      │  ├─ 실험 실행
      │  └─ Parsing 비교
      └─ 이슈/회귀
         └─ 이슈 신고/회귀 후보
```

구현상 상위 page label은 `build_monitoring_page_labels()`가 `Chat`, `전체 Monitoring`을 반환한다. `Chat` page에서는 일반 채팅 tab과 현재 선택 chat 전용 `Chat Monitoring` tab이 같이 표시된다. `전체 Monitoring` page에서는 현재 선택 chat과 무관한 전역 진단 tab만 표시한다. 이 분리는 전체 운영 상태와 개별 대화 trace가 섞이지 않도록 하기 위한 UX 결정이다.

## 3. 전체 Monitoring

전체 Monitoring은 선택된 chat 하나가 아니라 저장소, 평가 준비 상태, 모든 thread, issue report를 기준으로 시스템 상태를 본다.

### 3.1 데이터 상태

`render_global_monitoring_page()`의 `데이터 상태` 화면은 `get_data_status()` 결과를 표시한다.

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
- 미임베딩 문서 전용 추출기: `unembedded_extraction_engine`
- 날짜별 데이터 캘린더 원천: `report_date_counts`, `report_date_type_counts`

V1의 report/parent/FAISS 수는 legacy DB/index에서, V2의 report/parent/vector 수는 active catalog와 immutable snapshot에서 파생된다. 이 화면은 검색 품질 문제가 실제 retrieval 로직 때문인지 DB/index/PDF 준비 상태 때문인지 먼저 확인하는 진입점이다.

### 3.2 임베딩 누락 문서

V1에서는 `reports.is_embedded=0` row를 표시한다. V2의 의도된 의미는 latest catalog report object 중 active snapshot의 `active_reports`에 포함되지 않은 object, 즉 active manifest backlog다. RDB 목록/집계에는 문서가 보이는데 follow-up 상세 답변이 적은 source만 사용하는 경우 이 화면에서 DB/VectorDB coverage 차이를 확인한다.

표시 metadata:

- `report_date`
- `report_type`
- `target_name`
- `broker`
- `title`
- `file_name`

`임베딩 누락 문서 ... 임베딩 시도` 버튼은 `src.core.data_update_jobs.start_embedding_job()`으로 embedding-only background job을 시작한다.

- V1 처리 건수 0: `python -m src.core.embed_pipeline --all`
- V1 처리 건수 N: `python -m src.core.embed_pipeline --limit N`
- V2: 두 인자 모두 전체 inventory를 스캔하고 신규·변경 파일만 처리하므로 `--limit`을 무시
- 진행 상태는 기존 data update progress box를 재사용하며 `(현재/전체) 파일명`을 표시한다.
- 이미 데이터 업데이트/임베딩 job이 실행 중이면 버튼은 비활성화된다.

Native V2에서는 status가 반환한 `catalog.sqlite3` 경로를 data root에
다시 연결한 뒤 active build manifest와 `active_reports`를 비교한다.
따라서 V1의 `reports.id`/`is_embedded` 쿼리를 native catalog에 적용하지
않는다. 목록 조회 자체가 실패하더라도 오류를 해당 탭에 표시하고 전체
Monitoring 화면은 계속 렌더링한다.

### 3.3 실험 실행

`_render_experiment_monitoring()`은 정식 evaluation dataset이 준비된 이후
dataset을 실제 graph에 통과시켜 pass/fail 결과를 저장하기 위한 화면이다.
현재는 dataset이 없으므로 실행할 수 없다.

데이터 준비 후 지원할 모드:

1. `current_data`
   - `DB_PATH`를 legacy anchor로 넘기되 canonical runtime dispatch가 활성 backend를 선택한다. V1이면 `reports.db`/`vector_db`, V2이면 `retrieval/v2/catalog.sqlite3`와 active immutable snapshot을 사용한다.
   - 로컬 데이터가 바뀌면 baseline 비교가 흔들릴 수 있다.
2. `fixed_snapshot`
   - `tests/fixtures/eval_snapshot`의 고정 V1형 `reports.db`/`vector_db` baseline을 별도 Python 프로세스로 사용한다.
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

`run_multiturn_evaluation_dataset()` helper는 후속 질문 scope 회귀를
programmatic하게 실행할 수 있지만, 정식 multi-turn dataset은 아직 없다.
이 runner는 현재 GUI에도 연결되지 않았다.

실패 case는 `build_evaluation_failure_actions()`가 다음 조치 후보로 분류한다.

- route 실패: router/query classification 확인
- filter 실패: metadata filter extraction 확인
- source 실패: retrieval index, chunking, rerank 확인
- citation 실패: citation generation/removal 확인
- latency 실패: latency budget 확인
- no-result: filter 완화 retry 또는 데이터 업데이트 확인

### 3.4 고정 평가셋

`고정 평가셋` 화면은 미래에 생성될
`tests/fixtures/evaluation_dataset.json`의 coverage를 요약한다. 현재는
dataset과 fixed snapshot이 없다는 준비 상태만 표시한다.

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

향후 fixture는 성능 개선 전후를 비교하기 위한 승인된 기준선으로 만든다.
source PDF 본문은 포함하지 않고 question, expected
route/filter/source/RDB expectation 같은 재현 가능한 기대값만 저장한다.
생성·검토·완료 조건은 `docs/EVALUATION_DATASET.md`를 따른다.

### 3.5 Parsing 비교

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

### 3.6 전체 응답 품질

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
- recent failures

`recent_failures`는 실패한 assistant 응답의 thread id/name, created_at, status, route, latency, no_vector_results, error를 최근순으로 최대 10개 보여준다.

데이터 무결성은 `summarize_data_integrity()`가 계산한다.

| backend | check | 기준 |
| --- | --- | --- |
| V1 | `faiss_index` | FAISS index 존재 여부 |
| V1 | `embedding_backlog` | pending report가 0이면 pass, 남아 있으면 warning |
| V1 | `pdf_vs_db` | 다운로드 PDF 수가 embedded report 수 이상인지 비교 |
| V2 | `native_snapshot` | build state가 `committed_pending_checkpoint` 또는 `fully_complete`, snapshot state가 `ready` |
| V2 | `native_membership` | catalog membership 수와 snapshot `ntotal`이 같고 0보다 큼 |
| V2 | `manifest_backlog` | active manifest 밖 최신 source object 수 |
| V2 | `pdf_vs_manifest` | PDF 수와 active report 수 비교 |
| V2 | `search_coverage` | active/latest report 비율 |
| V2 | `runtime_health` | generation, epoch, degraded, write_enabled |

### 3.7 이슈 신고/회귀 후보

사용자 신고 진입점은 하나다. 일반 채팅의 `신고` 화면에서 `특정 응답` 또는
`화면·시스템(응답 없음)`을 대상으로 고른다. 특정 응답이면 선택 메시지,
직전 질문, 축약 trace와 이전 검색 범위를 자동으로 붙이고, 응답이 없는
문제는 같은 신고 스키마에 수동 재현 시나리오를 남긴다. 전체 대화 첨부는
기본 해제이며 저장 전에 포함 항목 미리보기를 표시한다.

`_render_issue_report_monitoring()`은 `debug/issue_report_*.txt`와 같은 stem의 구조화 `.json` sidecar로 저장된 사용자 신고를 전체 개선 루프의 입력으로 모아 보여준다. 내부 상세 subheader는 `Issue reports`다.

표시 내용:

- report 수
- report가 연결된 thread 수
- category 수
- category별 count
- report rows: created_at, id, category, thread, file path, preview
- 선택 report 상세 원문

선택한 report는 `promote_issue_report_to_eval_candidate()`를 통해 regression 후보 artifact로 저장할 수 있다. Chat Monitoring trace에서 생성된 신고처럼 구조화된 `context.trace_detail`이 있는 경우에는 운영자가 검토·수정할 수 있는 `eval_case_draft`도 함께 만든다.

저장 위치:

- 신고 원문: `debug/issue_report_*.txt`
- 구조화 sidecar: `debug/issue_report_*.json`
- regression 후보: `debug/regression_candidates/<candidate_id>.json`

Regression 후보 artifact에는 초기 운영 lifecycle 필드가 포함된다.

- `triage_status`: 기본값 `new`
- `operator_decision`: 기본값 `unreviewed`
- `severity`: 기본값 `untriaged`
- `impact_area`: debug hint/category/content 기반 추정값 (`filter_scope`, `routing`, `retrieval_source`, `citation`, `latency`, `ui`, `answer_quality`)
- `eval_case_draft`: trace 기반 case 초안. `question`, `expected_route`, `expected_filters`, `expected_sources`, `expected_state`, `monitoring_dimensions`를 포함하며 정식 fixture 반영 전 운영자 검토가 필요하다.

`이슈 신고/회귀 후보` 화면의 후보 영역은 저장된 후보 파일을 디스크에서 다시 읽어 상태와 개정 번호, 관찰 결과, 승인된 기대 결과를 표시한다. 상태 흐름은 다음과 같다.

```text
신규
  → 분류 완료
  → 기대 결과 작성
  → 수정 전 재현 준비
  → 오류 재현
  → 수정 중
  → 수정 후 검증
  → 종료
```

후보 파일은 두 개의 개정 번호를 사용한다.

- `record_revision`: 분류, 실행 결과 연결, 전달물 연결처럼 후보 기록이 바뀔 때 증가한다.
- `contract_revision`: 재현 입력, 기대 결과, 적용 검사, 검증 방식처럼 검증 계약이 바뀔 때 증가한다.

새 신고에서 승격한 후보는 검증 계약 v2를 사용한다.

- `quality_profile`: `accuracy_first`, `balanced`, `speed_first`
- `validation_plan.hard_checks`: 자동 검사, 수동 검사,
  `performance_p95_pass`를 조합한 필수 기준
- `validation_plan.soft_objectives`: 통과 여부와 분리해 보존하는 p95,
  답변 간결성·깊이 목표
- `validation_plan.performance_budget`: p95 예산, 측정 반복 수,
  워밍업 수와 hard/soft 판정
- `reproduction_manifest`: 앱·코드·모델·프롬프트·도구·데이터·인덱스·
  설정·기능 플래그의 안전한 버전 또는 SHA-256 지문

속도 우선 프로파일도 경로·필터·출처·오류 없음 같은 정확성·안전성
검사를 최소 하나 요구한다. 자동 검사와 수동 문항이 함께 있으면 `mixed`
계약이 되며, 수정 전 재현과 수정 후 검증 모두 두 종류의 최신 증거가
있어야 다음 상태로 이동한다. 응답 없는 UI 후보는 질문 대신 승인된
`scenario`로 수동 재현할 수 있다.

화면에서 읽은 `record_revision`과 디스크의 현재 값이 다르면 덮어쓰지 않고 충돌로 처리한다. 검증 계약이 바뀌면 과거 실행 결과와 Codex 전달물은 감사 이력으로 보존되지만 현재 증거로 사용하지 않는다.

주요 저장 위치:

- 후보: `debug/regression_candidates/<candidate_id>.json`
- 후보별 수정 전·후 실행: `debug/candidate_evaluation_runs/evaluation_run_*.json`
- Codex 전달물: `debug/codex_handoffs/<safe_candidate_token>/<handoff_id>.manifest.json`
- 사람이 읽는 전달 문서: 같은 위치의 `<handoff_id>.md`

신고 JSON과 Codex 전달물 명세는 원본 파일이다. 신고의 `.txt`와 전달물의 `.md`는 원본에서 다시 만들 수 있는 동반 파일이다. 쓰기 중단으로 동반 파일만 누락되면 화면에서 발견해 재생성할 수 있다. 후보 실행이나 전달물 저장 뒤 후보 연결 전에 앱이 종료된 경우에도, 다음 화면 렌더링에서 디스크를 다시 탐색해 현재 검증 계약과 일치하는 파일만 연결 대상으로 제시한다.

Codex 전달물은 후보 전체나 대화 전체를 내보내지 않는다. 허용된 재현 입력, 관찰 결과, 승인 기대 결과, 실패한 검사, 검증 명령만 구조화하고 이메일·전화번호·인증정보·로컬 경로 등은 제거한다. 운영자가 제거 결과 미리보기와 전달 내용을 확인하고 승인 사유를 입력해야 명세와 Markdown을 저장한다. 명세, 구조화 내용, Markdown 각각의 SHA-256 값을 다시 검증한 뒤에만 후보에 연결한다.

기존 `eval_case_draft`의 현재 데이터 실행은 이전 진단 기능과의 호환을 위해 남아 있으며, 정식 수정 전·후 증거로 사용하지 않는다. 정식 후보 실행은 승인된 검사만 판정하고 수정 전 실행과 수정 후 실행을 별도 종류로 기록한다.

정식 후보 실행은 질문만 새 스레드에 보내지 않는다. 승인된
`chat_history`와 실행 가능한 `prior_search_scope`를 함께 주입한다.
계약 v2는 성능 예산의 워밍업 뒤 지정 횟수만큼 반복하고 p95와 각
연성 목표 결과를 실행 파일에 보존한다. 수정 전 실행은 후보의 전체
재현 매니페스트와 같아야 한다. 수정 후 실행은 코드·프롬프트·도구
변경은 기록하되 모델·데이터·인덱스·설정·기능 플래그가 달라지면
증거 연결을 차단한다.

정식 후보 실행 기능은 운영 화면에 연결되어 있지만 자동으로 시작되지 않는다. 실제 평가 자료와 V2 고정 스냅샷의 생성·검수가 끝났음을 운영자가 명시적으로 확인해야 별도 프로세스에서 수정 전 또는 수정 후 평가를 실행할 수 있다. 스냅샷이 없거나 검증에 실패하면 그래프를 호출하지 않고 `snapshot_unavailable` 차단 시도만 기록해 운영 화면에서 확인할 수 있다. 재현 매니페스트가 다르면 `reproduction_manifest_mismatch`로 그래프 호출 전에 차단한다. 자료가 확정되기 전까지는 합성 시험 자료로 신고 불러오기부터 수정 전 실패, Codex 전달물, 수정 후 통과, 종료까지의 전체 경로만 검증한다. 생성 중인 활성 DB·벡터 인덱스와 기존 고정 스냅샷은 이 작업에서 변경하지 않는다.

저표본 사건도 모두 분류·개선 대상이다. 집계 표본이 기본 20건보다
작으면 `low_sample`로 표시하고 비율만으로 자동 차단하지 않지만,
개별 사건의 `improvement_eligible` 상태는 유지한다. 통제된 후보 반복
실행은 실사용 표본과 분리된 승인 계약으로 다룬다.

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

Trace viewer는 선택 응답을 세 개의 목적별 tab으로 나누어 보여준다. 자주 보는 판단 근거는 기본 tab에 모으고, state 변화와 raw retrieval/citation detail은 `Advanced diagnostics` 안에 접어 둔다.

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
- prior/search scope file count
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

`Advanced diagnostics`는 평소에는 잘 보지 않는 raw detail을 expander로 접어 둔다. 정밀 디버깅이나 issue report 작성 시에만 펼쳐 보면 된다. Sources 표는 일반 답변 영역과 monitoring row에서 이미 확인할 수 있으므로 여기서는 중복 노출하지 않는다.

##### State transitions

`State transitions`는 선택 응답의 compact state 흐름을 보여준다. query rewrite 전 입력 state와 query rewrite/search scope/routing/retrieval 이후의 핵심 state를 나눠서 확인할 수 있다.

표시 metadata:

- input
  - 원질문
  - `prior_search_scope`
  - `prior_search_scope_file_count`
- after_query_rewrite
  - rewritten query
  - chat history 사용 여부
  - `followup_scope_intent`
- after_search_scope
  - `search_filters`
  - `temporal_context`
  - `scope_source`
  - `scope_decision`
  - `search_scope`
  - `search_scope_file_count`
- after_routing
  - route
  - routing context
- after_retrieval
  - `candidate_count_after_filter`
  - `document_coverage_applied`
  - `document_coverage_reason`
  - `selected_file_names`
- suspect_transitions
  - `prior_scope_files_dropped`: query rewrite 전 prior scope의 파일 수보다 현재 search scope 파일 수가 줄었는지 여부

이 화면은 “query rewrite 전에는 정상 scope였는가?”, “rewrite 이후 search scope에서 file_names가 줄었는가?”, “retrieval 이후 selected_file_names가 더 줄었는가?”를 turn 단위로 확인하기 위한 디버깅 기준점이다.

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
- backend: `runtime_mode`, `requested_k`, `fetch_k`
- native corpus/search: `native_candidate_count`, `native_eligible_count`, `native_snapshot_total`, `native_search_strategy`, `native_faiss_calls`
- native hydration: `native_hydration_batches`, `native_hydration_rows`, `native_hydration_cache_hits`, `native_hydration_cache_misses`
- native revision: `snapshot_id`, `publication_generation`
- native timings(ns): `native_scope_compile_ns`, `native_eligibility_ns`, `native_faiss_ns`, `native_hydration_ns`, `native_lease_ns`, `native_total_ns`

이 raw detail은 metadata filter가 과도했는지, rerank 이후 source가 특정 문서에 편중됐는지, document coverage가 적용됐는지 확인할 때 사용한다.

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
| source 1개 문서 편중 | `selected_sources`가 2개 이상인데 unique file이 1개 이하 | 복수 문서 요청의 source 다양성 부족 탐지 |
| 필터 후 후보 0개 | `candidate_count_after_filter == 0` | metadata filter 과도 적용 탐지 |
| route/content intent 불일치 | `route=rdb`인데 질문에 `주요 내용`, `요약`, `리스크`, `투자포인트` 포함 | 본문 검색이 필요한 질문이 RDB로 간 경우 탐지 |
| document coverage 미적용 | 질문에 `전체`, `각각`, `리포트들`, `목록`, `비교`, `모두`, `여러`가 있고 `document_coverage_applied is False` | 여러 문서 질의에서 coverage 보장 누락 탐지 |

Debug hint는 확정 판정이 아니라 조사 시작점이다. false positive가 있을 수 있으므로 trace detail과 diff를 함께 확인해야 한다.

### 4.6 선택 trace의 단일 신고 흐름 연결

별도의 trace 전용 신고 시스템을 두지 않는다. 일반 채팅의 공통 신고
화면에서 특정 응답을 선택하면 아래 축약 정보가 자동으로 표준 신고에
연결된다.

`build_chat_trace_issue_context()`가 포함하는 정보:

- thread id/name
- 제출 출처: `chat_monitoring_trace`
- 선택 응답 직전 user question
- 선택 assistant 응답 compact metadata
- 직전 성공 assistant 응답 compact metadata
- trace detail
- previous vs selected diff
- debug hints

선택 trace 신고에는 실행용 `reproduction_input`도 함께 만들어진다.
후속 질문이면 `requires_prior_scope=true`와 정규화된 이전 검색 범위를
저장한다. 전체 대화 원문은 사용자가 미리보기를 확인하고 별도로
선택한 경우에만 추가한다. 이 report는 전체 Monitoring의
`이슈 신고/회귀 후보` 화면에서 다시 볼 수 있고, 필요하면 regression
후보로 승격할 수 있다.

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
├─ selected_sources
├─ search_scope
└─ monitoring
   ├─ query_rewrite
   │  ├─ rewritten_query
   │  ├─ uses_chat_history
   │  └─ followup_scope_intent
   ├─ retrieval
   │  ├─ source_count
   │  ├─ score_summary
   │  ├─ runtime_mode / snapshot_id / publication_generation
   │  ├─ native candidate/eligible/snapshot counts와 search strategy
   │  ├─ FAISS/hydration/cache counters
   │  └─ native scope/eligibility/FAISS/hydration/lease/total timings
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
- 현재 정식 evaluation dataset과 fixed snapshot이 없으므로 장기 baseline 비교나 품질 수치 확정은 할 수 없다.
- Native V2 미임베딩 목록은 active build manifest의 `legacy_not_vectorized`와 `source-extraction-failed` 제외 항목을 표시한다.
- Monitoring metadata는 compact 진단 정보이며, 전체 chain-of-thought나 LLM 내부 판단을 저장하지 않는다.
- Issue report와 evaluation run 산출물은 `debug/` 아래에 저장되며 Git에는 포함하지 않는다.
- Chat Monitoring row는 안전성을 위해 preview 중심이지만, issue report 원문에는 문제 재현에 필요한 정보가 포함될 수 있으므로 외부 전달 전 확인이 필요하다.

## 7. 검증

Monitoring 관련 주요 회귀 테스트는 다음 명령으로 실행한다.

```bash
python -m pytest tests/test_artifact_io.py tests/test_feedback_loop.py tests/test_feedback_plan_revised.py tests/test_feedback_handoff.py tests/test_candidate_evaluation_snapshot_runner.py tests/test_monitoring.py tests/test_status.py tests/test_gui_view_contracts.py tests/test_evaluation_dataset.py tests/test_evaluation_snapshot_runner.py -q
```

주요 테스트 범위:

- Monitoring Mode 설정 파싱
- 데이터/평가셋 요약
- evaluation snapshot 검증
- chat monitoring row와 trace detail 생성
- previous vs selected diff
- debug hints 감지
- issue report context 생성
- 신고/후보의 이전 형식 읽기 호환성과 원본 불변성
- 후보 상태 전이, 개정 충돌, 계약 변경 시 과거 증거 무효화
- 승인된 검사만 사용하는 수정 전·후 평가
- 정확성 우선·속도 우선·응답 없는 UI 후보의 합성 종단 간 종료
- 이전 검색 범위 주입, 재현 매니페스트 비교와 반복 p95 판정
- 자동·수동 혼합 계약에서 두 증거를 모두 요구하는 상태 차단
- 저표본 사건의 개선 대상 유지와 집계 자동 판단 분리
- 실행 결과와 Codex 전달물의 디스크 재탐색·연결·복구
- 전달물 허용 목록, 민감정보 제거, 세 종류 해시 검증
- pytest 임시 입력의 수정 전 실패 → 전달물 → 수정 후 통과 → 종료
- 전체 thread summary와 data integrity summary
- evaluation run 비교와 failure triage
- native runtime/data-integrity 요약과 snapshot-aware retrieval metadata
- multi-turn evaluation helper 계약 (정식 dataset은 아직 없고 GUI에도 미노출)
