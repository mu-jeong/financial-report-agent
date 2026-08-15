# Monitoring Mode (Native V2)

Monitoring Mode는 전체 운영 상태와 개별 대화 turn을 서로 다른 깊이로 확인하는 개발자용 화면이다. `.env`의 `MONITORING_MODE=true`일 때만 노출된다.

활성 화면과 새 산출물은 Native V2만 기준으로 삼는다. 스키마가 유효하지 않은 평가 run·신고·회귀 후보는 활성 화면에서 제외한다.

## 1. 화면 원칙

평상시 화면에는 다음 두 지표만 표시한다.

| 지표 | 정의 |
| --- | --- |
| 응답 속도 | 실제 Native V2 snapshot/generation provenance가 있는 assistant 응답의 `latency_seconds` P95. 평균과 표본 수도 보조 문구로 표시한다. |
| 답변 정확도 | 승인된 질문으로 실행하고 Native V2 snapshot/build/profile/generation을 고정·검증한 평가 run의 correctness 검사 통과율. latency 검사는 제외한다. |

속도 표본이나 승인된 V2 평가 run이 없으면 `측정 전`으로 표시한다. 데이터가 없다는 사실을 0초나 정확도 0%로 오해하지 않게 하기 위한 계약이다.

나머지 정보는 기본 지표 아래의 용도별 가로 내비게이션에서 하나씩 선택한다. `운영 모니터링`과 `성능 개선 실험`을 먼저 분리하고, 선택한 그룹 안에서 세부 화면을 다시 고른다. 선택하지 않은 route, source, snapshot, parser 비교, 신고 목록은 렌더링하지 않는다.

```text
Monitoring
├─ 응답 속도 (P95)
├─ 답변 정확도 (correctness-only)
└─ 용도별 상단 내비게이션
   ├─ 운영 모니터링
   │  ├─ 현재 문제
   │  ├─ 응답 원인 확인
   │  └─ 검색 자료 준비
   └─ 성능 개선 실험
      ├─ 정확도 평가
      ├─ 문서 읽기 품질 비교
      └─ 신고·수정 확인
```

그룹 내부 ID는 `operations`, `experiments`로 고정한다. 세부 영역의 내부 ID는 각각 `summary`, `response`, `search_data`, `evaluation`, `parsing`, `issues`로 유지한다. 화면 문구가 바뀌어도 widget state와 테스트가 흔들리지 않게 하기 위해서다. Streamlit `st.tabs`는 숨은 panel까지 모두 계산하므로, 이 화면은 선택한 panel만 계산하는 `st.segmented_control`을 탭형 내비게이션으로 사용한다.

각 그룹은 마지막으로 선택한 세부 화면을 따로 기억한다. `현재 문제`의 경고 행은 상태 이름만 표시하지 않고 실제 집계 세부값과 다음 확인 경로를 함께 제공한다.

## 2. 진입 구조

```text
Sidebar
├─ Chat
│  ├─ Chat
│  └─ 답변 모니터링
└─ Monitoring
```

- `Chat > 답변 모니터링`은 선택한 응답의 `총시간`, 실제 검색 실행 방식, 요청 대상별 근거 확보, 인용 연결 상태를 먼저 보여준다. 대상별 검색시간과 사용 문서를 그 아래에 두고, 현재 대화 평균·backend 평균·응답별 시간 추이는 `대화 전체 속도 추이`에 접어 둔다. compact state, 검색 k, prompt chunk 같은 구현 상세는 `기술 세부정보`를 명시적으로 선택했을 때만 렌더링한다.
- `Monitoring`은 모든 대화의 속도, 공통 정확도, Native V2 검색 자료 상태와 문제 처리 도구를 다룬다.
- `MONITORING_MODE=false`이거나 설정이 없으면 일반 Chat 화면만 렌더링한다.

## 3. 기본 지표와 turn 근거

### 3.1 응답 속도

`summarize_chat_messages()`와 `summarize_all_chat_threads()`가 assistant 응답 metadata에서 실제 Native V2 `runtime_mode`, `snapshot_id`, `publication_generation`이 확인된 latency 표본만 모은다. provenance가 없는 과거 응답은 속도에 섞지 않는다.

- 대표값: P95
- 보조값: 평균, 표본 수
- 표본 없음: `측정 전`
- 실패 응답은 Native V2 provenance까지 기록된 경우에만 속도 표본에 포함한다.

P95를 기본값으로 쓰는 이유는 평균만으로 가려지는 느린 꼬리 응답을 확인하기 위해서다.

개별 Chat의 기본 화면은 선택한 응답의 시간을 우선한다. 현재 thread 평균과 backend 평균은 `대화 전체 속도 추이`에서만 제공하며, 성공했고 Native V2 provenance가 검증된 응답만 표본으로 사용한다. RDB 조회시간은 SQL guardrail·연결·실행·결과 반환 구간이며, Vector DB 검색시간은 scope compile·lease·FAISS·hydration을 포함하는 `native_total_ns`이다. 호출하지 않은 backend나 과거 metadata처럼 표본이 없는 값은 `0`이 아니라 `측정 전`으로 표시한다. 정확도는 개별 응답에서 바로 판정하지 않고 전체 Monitoring의 검증된 evaluation run에서만 다룬다.

개별 turn 상세의 시간 경계는 다음과 같다.

| 값 | 의미 |
| --- | --- |
| 전체 응답 | 질문 제출 후 graph가 끝나고 assistant message metadata를 저장하기 직전까지의 wall-clock 시간 |
| RDB 조회 | SQL guardrail·연결·실행·결과 반환 구간 |
| Vector DB 검색 | Native V2 scope compile·lease·FAISS·hydration의 합계 |
| Vector 세부 구간 | scope compile, eligibility, FAISS, hydration, lease가 backend에서 제공될 때의 개별 값 |
| 비교 대상 검색 | 각 기업 branch의 retrieval wall time. 가장 느린 branch와 모든 branch 작업시간 합을 분리한다. |
| 비교 검색 대기 | 동시성 제한을 얻기 전 각 branch의 queue wait. 화면에는 최댓값을 표시한다. |
| 답변 합성 | 답변 생성에 사용된 모든 LLM 호출의 요청 시작부터 마지막 stream chunk까지의 합계 |
| 최초 토큰 | 각 LLM 요청 시작부터 첫 content·reasoning·tool-call chunk까지의 시간. 여러 호출이면 최종 호출 값을 기본 표시한다. |

병렬 branch 작업시간 합은 전체 wall-clock 시간이 아니므로 전체 응답시간과 직접 합산하지 않는다. query rewrite와 rerank의 독립 구간 시간이 없으면 추정값을 만들지 않고 `측정 전`으로 둔다.

답변 생성은 스트림을 직접 계측해 입력·출력·전체 토큰, 최초 토큰 시간, 요청 완료시간, 출력 토큰/초를 저장한다. 출력 토큰/초는 `출력 토큰 ÷ 요청 시작~완료 시간`인 client 관측 end-to-end 값이다. OpenRouter 요청은 router metadata를 opt-in해 실제 선택 provider와 gateway/model을 구분한다. cache 응답 등 router metadata가 없는 경우 provider를 추정하지 않고 `측정 전`으로 표시한다. 도구 호출 후 재생성처럼 LLM 호출이 여러 번이면 호출별 compact 지표와 turn 합계를 함께 저장한다.

### 3.2 개별 turn state와 검색 근거

각 assistant 응답 metadata에는 원문 state 전체 대신 재현과 진단에 필요한 compact snapshot을 남긴다.

- 저장된 state key 목록
- route, 검색 filter, scope 출처
- 검색 결과 없음과 memory retry 여부
- generation/RDB 결과 존재 여부
- 최종 context 수
- input → query rewrite → scope → routing → retrieval → answer 단계 상태

검색 `k`는 하나의 숫자로 합치지 않는다.

| 필드 | 의미 |
| --- | --- |
| `configured_top_k` | 최종 context 상한인 `SEARCH_TOP_K` |
| `requested_k` | retrieval backend에 요청한 후보 수. 기본 계산은 `SEARCH_TOP_K × SEARCH_CANDIDATE_MULTIPLIER` |
| `fetch_k` | backend가 실제 FAISS 조회에 사용한 수 |
| `candidate_count_before_filter` | metadata filter 전 후보 수 |
| `candidate_count_after_filter` | filter 후 후보 수 |
| `context_count` | 최종 prompt에 들어간 passage 수 |

Vector DB 사용 근거는 최종 prompt에 들어간 passage만 기록한다. Native V2에서는 `chunk_uid`, `parent_uid`, `report_uid`, span, rank, score와 문서 metadata를 저장한다. 문서 그룹과 인용 별칭은 `report_uid`, canonical path, 과거 `file_name` 순으로 식별한다. RDB 참고 문서는 별도 `rdb_evidence`로 표시하며 vector chunk로 세지 않는다. 청크 본문, PDF 본문, provider 원문 응답이나 전체 router metadata는 monitoring metadata에 복제하지 않는다. 과거 응답에 안정적 식별자가 없으면 임의 값을 만들지 않고 `identity_status=not_measured`와 빈 ID로 표시한다.

`근거 연결 상태`는 의미 정확도 점수가 아니다. 선택 출처와 인용 번호 또는 RDB 결과가 구조적으로 연결되는지만 `linked`, `partial`, `unavailable`, `not_applicable`, `not_measured`로 표시하며 의미 검토 상태는 항상 별도의 `not_evaluated`로 둔다.

복수 기업 비교 응답은 다음 compact 실행 근거를 추가로 저장한다.

- plan type과 실제 `execution_mode` (운영 응답은 `send`; `sequential_reference`는 내부 회귀 비교에서만 사용)
- 요청 대상 수, 근거 확보 대상 수, 대상별 `success`/`no_result`/`failed` 상태
- 대상별 후보 수, retrieval 시간, queue wait
- 전역 rerank 횟수, 답변 합성 횟수, 검색 동시성 상한

개별 화면의 `Send 병렬 비교` 표시는 저장된 `execution_mode=send`가 있을 때만 사용한다. 과거 응답처럼 실행 방식이 저장되지 않았다면 대상 수나 source 수로 추정하지 않고 `방식 미계측`으로 표시한다.

### 3.3 답변 정확도

`summarize_evaluation_accuracy()`는 hash와 실제 Native V2 runtime provenance가 함께 검증된 평가 run만 읽는다.

- 포함: route, filter, source, citation, no-result, expected-state 등 활성 correctness 검사
- 제외: `latency_pass`, performance budget
- 집계 단위: correctness 검사가 하나 이상 활성화된 평가 case
- 평가 자료 없음 또는 구형 run: `측정 전`

느리지만 맞는 답변을 오답으로 계산하지 않고, 속도와 정확도를 서로 독립적으로 판단한다.

승인된 정식 evaluation fixture가 없으면 기본 상태는 `측정 전`이다. 임시 데이터나 현재 `DATA_ROOT`의 결과를 승인된 정확도 기준으로 간주하지 않는다.

## 4. 문제 상황 상세 영역

### 현재 문제

- 최근 실패 응답
- Native V2 무결성 warning/fail
- 확인이 필요한 항목 수

기술 키 대신 사용자 문구를 표시한다. 예를 들어 `native_membership`은 `검색 대상 일치`, `cleanup_backlog`는 `검색 데이터 정리 대기`로 표현한다.

### 응답 원인 확인

전체 Monitoring은 기존 trace drill-down을 유지한다. `Chat > 답변 모니터링`은 assistant 응답을 선택하면 다음 순서로 표시한다.

- 기본: 총시간, 검색 실행 방식, 대상별 근거, 인용 연결
- 기본: 실제로 측정된 구간만 있는 병목 표와 비교 대상별 검색 상태
- 기본: 입력·출력 토큰, 최초 토큰, 실제 provider, 출력 토큰/초
- 기본: 답변에 사용된 문서
- 접힘: 현재 대화 평균과 backend별 시간 추이
- 접힘: 직전 성공 응답과의 차이
- 명시적 선택: `기술 세부정보`를 선택했을 때만 query rewrite, scope, routing, retrieval/rerank, 검색 k, answer/citation, prompt chunk, compact state를 렌더링

선택한 trace는 issue report의 재현 context로 연결할 수 있다.

### 검색 자료 준비

활성 Native V2 상태만 확인한다.

- 검색 가능한 문서 수
- 아직 반영되지 않은 문서 수
- 검색 자료 반영률
- active snapshot/build 상태
- catalog membership과 실제 vector 수 일치
- source manifest backlog
- runtime health
- compacted artifact 정리 대기 수·용량·최장 시간
- 파싱 실패 등 검색에서 제외된 문서와 재시도 동작

raw snapshot ID, generation, epoch 같은 값은 `기술 세부정보` expander 안에 둔다. Native V2 상태가 없으면 다른 저장소의 지표로 우회하지 않고 `V2 retrieval status is unavailable` 문제를 표시한다.

### 정확도 평가

승인된 질문을 현재 Native V2 검색 데이터로 실행한다.

- 실행 mode: `native_v2`
- 산출물: `debug/evaluation_runs/evaluation_run_*.json`
- 필수 계약: `schema_version=2`, `run_hash` 검증 성공, successor Native V2 runtime
- 고정 provenance: active snapshot/build/profile, publication generation, write epoch
- 실행 검증: 각 graph 결과의 runtime/snapshot/generation이 시작 시 고정한 값과 일치
- 비교 기준: 같은 execution mode의 직전 V2 run

실패 case는 route/filter/source/citation/no-result 등 원인별 조치 후보를 제공한다. 속도 기준은 별도로 보이지만 기본 정확도 수치에는 합산하지 않는다.

### 문서 읽기 품질 비교

PDF 추출 엔진 비교는 문제 문서가 의심될 때만 연다. 파일별 성공/실패, 추출량, 실행 시간과 결과 경로를 확인한다. 이 결과는 기본 정확도 수치에 자동 합산하지 않는다.

### 신고·수정 확인

이 화면은 사용자 신고를 회귀 후보로 승격하고 수정 전 재현과 수정 후 검증을 관리한다. `verified` 또는 `closed` 후보만 이후 새 평가 사례의 입력으로 사용할 수 있으며, 새로 고정한 데이터 기준에서 기대 조건과 출처를 다시 검토해야 한다.

활성 화면은 다음 V2 계약만 자동 발견한다.

| 산출물 | 활성 계약 |
| --- | --- |
| Issue report | `schema_version=2`, `report_contract_version=2` |
| Regression candidate | `schema_version=2`, `contract_schema_version=2` |
| Evaluation run | `schema_version=2`, 유효한 `run_hash`, 검증된 successor Native V2 provenance |

계약이 유효하지 않은 파일은 활성 목록에서 제외한다. 새 issue report와 이메일 text import는 V2 계약으로 저장한다.

회귀 후보의 baseline·verification은 Native V2 revision이 기록되고 검증된 실행 결과만 증거로 연결한다. 검증 계획은 판정 기준이고 재현 매니페스트는 환경 지문이므로 둘만으로 실행 자료를 복원하지 않는다. 단순 후보 초안 진단 실행은 현재 Native V2 backend를 사용하지만 정식 lifecycle 증거로 자동 승격하지 않는다.

기대 결과 작성 단계에서는 운영자가 계약 JSON을 직접 입력하지 않는다. `LLM으로 최소 조건 제안`을 명시적으로 눌렀을 때만 선택 turn의 질문·답변·신고 사유와 제한된 출처 메타데이터를 현재 생성 모델에 보내며, 모델은 1~5개의 자연어 최소 조건과 대상 별칭만 구조화해 제안한다. 제안은 후보를 변경하거나 승인하지 않고 운영자가 화면에서 수정한 뒤 별도로 저장·승인한다. VectorDB 답변 조건은 기본적으로 `답변에 대상 표현 존재`, `선택 출처 메타데이터에 같은 대상 존재`, `그 출처 순위 인용`을 모두 만족해야 통과하므로, 예를 들어 “SK하이닉스 자료가 없다”는 문장에 이름만 등장해도 하이닉스 출처와 인용이 없으면 실패한다. 모델 호출·구조화 검증 실패 시 기존 후보 계약은 그대로 유지한다. 품질 프로파일, 성능 예산, 재현 입력과 매니페스트는 고급 내부 설정으로 기존 값을 자동 유지하며 수동 UI 검사가 필요한 경우에만 한 줄짜리 확인 조건을 추가한다.

## 5. V2 데이터 경계

활성 Monitoring은 다음 원칙을 지킨다.

1. 검색 상태는 `catalog.sqlite3`, active base snapshot과 ready delta segment를 기준으로 계산한다.
2. 무결성 검사는 V2 snapshot, membership, manifest, runtime, cleanup backlog만 본다.
3. 정확도는 스키마·hash·실제 runtime provenance가 모두 검증된 Native V2 run만 집계한다.
4. 신고와 회귀 후보 discovery도 V2 계약만 사용한다.
5. 계약이 유효하지 않은 산출물은 현재 지표로 집계하지 않는다.

## 6. 주요 구현 파일

| 영역 | 파일 |
| --- | --- |
| Streamlit 진입 및 page 선택 | `apps/gui/app.py`, `apps/gui/sidebar_views.py` |
| Monitoring 화면 | `apps/gui/monitoring_views.py` |
| 시간·정확도·trace·V2 무결성 집계 | `src/core/monitoring.py` |
| V2 issue report 저장과 discovery | `src/core/issue_report_store.py` |
| Native V2 상태 | `src/core/status.py` |
| 응답 metadata 생성 | `src/graphs/state.py`, `src/nodes/*` |

## 7. 실행과 검증

```env
MONITORING_MODE=true
```

```bash
streamlit run apps/gui/app.py
```

핵심 회귀 테스트:

```bash
python -m pytest -q tests/test_monitoring.py tests/test_gui_view_contracts.py tests/test_feedback_loop.py
```

검증 계약은 다음을 포함한다.

- P95와 표본 수 집계
- 개별 Chat의 최근/평균 응답시간과 RDB/Vector DB 평균 호출시간 집계
- 개별 Chat turn별 compact state, 단계별 k, 사용 chunk·문서 식별정보 노출
- 과거 trace는 근거가 없는 state 단계를 완료로 추정하지 않음
- RDB 참고 문서와 Vector DB prompt chunk를 분리
- `report_uid` 우선 문서 그룹 및 안정적 ID 미측정 상태
- chunk/PDF/provider 본문을 turn observability metadata에 복제하지 않음
- 근거 연결 상태와 의미 정확도를 분리
- Native V2 provenance가 없는 응답의 latency 제외
- latency가 정확도에 섞이지 않음
- self-labeled/tampered run 및 runtime revision 불일치 차단
- 평가 자료가 없을 때 `측정 전`
- 기본 화면의 두 지표와 용도별 상단 내비게이션
- 활성 UI는 Native V2 catalog/snapshot만 사용
- 스키마 없는 report/candidate/run의 활성 discovery 제외
- Native V2 무결성 및 cleanup backlog 표시
- Native V2가 없을 때 다른 DB/vector 상태를 읽거나 렌더하지 않음
- issue report와 candidate lifecycle의 V2 계약 경계

## 8. 현재 한계

- 승인된 정식 V2 evaluation fixture가 준비되기 전에는 정확도를 수치로 확정할 수 없다.
- 자동 debug hint는 규칙 기반이므로 조사 시작점으로만 사용한다.
- 적은 평가 표본만으로 품질 우열이나 배포 여부를 자동 결정하지 않는다.
- issue report에는 재현에 필요한 대화 metadata가 포함될 수 있으므로 외부 전달 전에 내용을 확인한다.
- LLM 생성·query rewrite의 독립 latency는 아직 계측하지 않으며 전체 응답시간에만 포함된다.
- 일반 대화 observability는 conversation DB 보존 정책을 따르며 정식 평가 증거로 취급하지 않는다.
