# Monitoring Mode (Native V2)

> 사용자 신고부터 재현·릴리스 비교·이슈 분류까지의 현재 구현 권위 문서는 [사용자 신고 기반 개선 루프](IMPROVEMENT_LOOP.md)다. 이 문서는 Monitoring 화면과 개별 Chat 지표·trace의 세부 계약을 설명한다.

> 2026-08-29 운영 계약: 현재 최상위 `Monitoring`은 한 명의 admin이 사용하는 production 전용 신고 재현 화면이다. 이 문서 아래쪽의 응답 지표·trace 설명은 `Chat > 개별 Chat Monitoring`과 기존 진단 함수의 계약으로 남아 있으며, 더 이상 별도의 전역 개발자 Monitoring 메뉴를 뜻하지 않는다.

`MONITORING_MODE=true`이면 로컬 `Chat > 개별 Chat Monitoring`과 `개선 실험`은 사용할 수 있다. 최상위 운영자 `Monitoring`은 이 값만으로 열리지 않는다. `DEPLOYMENT_ENVIRONMENT=production`이고 Supabase URL, publishable key, 인증된 operator Edge Function URL, local managed root가 모두 유효해야 메뉴가 나타난다. 서버는 Supabase Auth 사용자와 `private.monitoring_admins.active`를 다시 확인한다.

## 현재 동작 화면

2026년 8월 31일 Chrome의 `http://localhost:8501/`에서 동일한 `조치 중` 신고(`b48d0660`)를 따라 확인한 실제 운영 화면이다. 로그인 식별 정보와 브라우저 UI는 캡처에서 제외했다. 화면을 열고 선택·스크롤만 했으며 Run 실행, Comparison 저장, Issue 상태 변경처럼 기록을 만드는 버튼은 누르지 않았다.

### 상태별 작업함과 신고 선택

![운영 Monitoring 작업함에서 상태별 신고 건수와 조치 중인 신고 b48d0660을 선택한 화면](../images/monitoring/loop-03-work-inbox.png)

최근 신고의 상태별 건수와 필터를 확인하고, 재현·비교 기록이 연결된 신고를 한 건 선택한다.

### 신고 요약과 관측값

![선택한 신고의 응답 속도, 품질 판단 상태, 신고 버전과 경로를 확인하는 화면](../images/monitoring/loop-04-work-triage.png)

작업함은 선택한 신고의 응답 속도, 정성 판단 유무, 신고 버전, 실행 경로와 동의 범위를 한 화면에 보여준다.

### 현재 근거와 다음 할 일

![운영 배포본에서 증상이 재현됐고 같은 케이스의 Baseline과 Candidate 비교가 다음 행동임을 보여주는 화면](../images/monitoring/loop-05-next-action.png)

로컬 registry의 재현·비교 기록에서 파생한 진행 상태를 보여주고, 운영자가 이어서 수행할 작업을 안내한다.

### 이슈 종결 준비

![해결됨으로 종료를 선택하고 상태 변경 사유를 기록하도록 준비된 화면](../images/monitoring/loop-14-close-issue.png)

비교 근거를 검토한 뒤 허용된 다음 상태와 사유를 선택한다. 위 화면은 `해결됨으로 종료`를 선택한 저장 전 상태이며 실제 Issue 상태는 변경하지 않았다. 신고 접수부터 Fixture·Snapshot·Run·Comparison까지의 전체 화면 순서는 [사용자 신고 기반 개선 루프의 전체 동작 화면](IMPROVEMENT_LOOP.md#전체-동작-화면)을 따른다.

## 0. 운영자 Monitoring 범위

최상위 `Monitoring`은 `작업함`, `재현 케이스`, `버전 비교` 세 화면으로 신고 확인, 재현 자산 준비, Baseline/Candidate 결과 비교를 제공한다. 자산 availability와 control drift·cleanup 경고는 별도 업무 화면을 만들지 않고 설정 expander에 표시한다. 누락된 Git Release cache는 실행 시 등록 commit에서 자동 재생성한다.

신고 접수부터 Issue 종결까지의 전체 순서는 [현재 구현된 전체 흐름](IMPROVEMENT_LOOP.md#2-현재-구현된-전체-흐름)을, 저장 권위는 [저장소와 권위의 분리](IMPROVEMENT_LOOP.md#3-저장소와-권위의-분리)를 따른다. 상태 전이·재현 자산·Run·Comparison의 불변조건은 [production Issue lifecycle](IMPROVEMENT_LOOP.md#5-production-issue-lifecycle)부터 9장까지가 기준이다. 이 문서는 그 계약을 반복하지 않고 화면에서 어떤 상태와 증거를 어떻게 보여주는지만 설명한다.

작업함은 최근 신고 최대 200건의 상태별 건수와 목록을 보여준다. 신고를 선택하면 동의된 원문을 자동 조회해 최초 열람을 감사하고, Fixture/Snapshot 제안에 필요한 최소 seed만 현재 session에 둔다. 상태 변경 화면은 현재 허용된 다음 상태만 제시하고 근거 사유를 요구한다.

Baseline/Candidate 실행 요청 중에는 `사전 점검 → 자산 검증 → 대기열 등록 → 모델 실행 → 결과 검증 → 결과 저장`의 실제 단계와 완료 단계 수를 표시한다. 브라우저를 다시 열었을 때는 registry의 Run 상태를 다시 읽으며, 성공 결과와 저장된 실패 정보를 같은 영역에 표시한다.

활성 화면과 새 평가 산출물은 Native V2만 기준으로 삼는다. 스키마와 hash가 유효하지 않은 평가 run은 활성 화면에서 제외한다.

## 1. 화면 원칙

호환을 위해 남겨 둔 로컬 집계 화면에는 다음 두 지표만 표시한다. 현재 최상위 `Monitoring`은 이 집계 화면을 호출하지 않는다.

| 지표 | 정의 |
| --- | --- |
| 응답 속도 | 실제 Native V2 snapshot/generation provenance가 있는 assistant 응답의 `latency_seconds` P95. 평균과 표본 수도 보조 문구로 표시한다. |
| 답변 정확도 | 승인된 질문으로 실행하고 Native V2 snapshot/build/profile/generation을 고정·검증한 평가 run의 correctness 검사 통과율. latency 검사는 제외한다. |

속도 표본이나 승인된 V2 평가 run이 없으면 `측정 전`으로 표시한다. 데이터가 없다는 사실을 0초나 정확도 0%로 오해하지 않게 하기 위한 계약이다.

이 로컬 집계 helper의 나머지 정보는 기본 지표 아래의 용도별 가로 내비게이션에서 하나씩 선택한다. `운영 모니터링`과 `성능 개선 실험`을 먼저 분리하고, 선택한 그룹 안에서 세부 화면을 다시 고른다. 선택하지 않은 route, source, snapshot, parser 비교는 렌더링하지 않는다. 새 최상위 `개선 실험`은 이 묶음을 다시 노출하지 않고 PDF 파싱 비교만 직접 렌더링한다.

```text
로컬 집계 helper (top-level 미노출)
├─ 응답 속도 (P95)
├─ 답변 정확도 (correctness-only)
└─ 용도별 상단 내비게이션
   ├─ 운영 모니터링
   │  ├─ 현재 문제
   │  ├─ 응답 원인 확인
   │  └─ 검색 자료 준비
   └─ 성능 개선 실험
      ├─ 정확도 평가
      └─ 문서 읽기 품질 비교
```

그룹 내부 ID는 `operations`, `experiments`로 고정한다. 세부 영역의 내부 ID는 각각 `summary`, `response`, `search_data`, `evaluation`, `parsing`으로 유지한다. 화면 문구가 바뀌어도 widget state와 테스트가 흔들리지 않게 하기 위해서다. Streamlit `st.tabs`는 숨은 panel까지 모두 계산하므로, 이 화면은 선택한 panel만 계산하는 `st.segmented_control`을 탭형 내비게이션으로 사용한다.

각 그룹은 마지막으로 선택한 세부 화면을 따로 기억한다. `현재 문제`의 경고 행은 상태 이름만 표시하지 않고 실제 집계 세부값과 다음 확인 경로를 함께 제공한다.

## 2. 진입 구조

```text
Sidebar
├─ Chat
│  ├─ Chat
│  └─ 개별 Chat Monitoring
├─ Monitoring
└─ 개선 실험
```

- `Chat > 개별 Chat Monitoring`은 로그인한 운영 신고함이 아니라 현재 사용자의 대화 한 건만 보는 로컬 진단이다. 응답을 선택하면 좌측에 저장된 compact 실행 단계 그래프가 나타나고 우측은 기본적으로 `총시간`, 실제 검색 실행 방식, 요청 대상별 근거 확보, 인용 연결 상태를 보여준다. 좌측 노드를 선택하면 우측을 해당 단계의 상태·계측·입출력 요약으로 전환한다. 대상별 검색시간과 사용 문서는 전체 지표에 두고, 현재 대화 평균·backend 평균·응답별 시간 추이는 `대화 전체 속도 추이`에 접어 둔다. compact state, 검색 k, prompt chunk 같은 구현 상세는 `기술 세부정보`를 명시적으로 선택했을 때만 렌더링한다.
- 최상위 `Monitoring`은 production 관리자만 접근하는 신고 작업함·재현 케이스·버전 비교를 다룬다. 모든 대화의 로컬 집계 화면은 더 이상 상위 메뉴로 노출하지 않는다.
- 최상위 `개선 실험`은 운영 신고 처리와 분리된 로컬 실험 화면이며 사이드바에서만 진입한다. Chat 내부에는 같은 버튼이나 탭을 중복 노출하지 않는다. 현재는 같은 PDF 표본을 여러 파싱 엔진으로 실행해 추출 품질 지표와 결과 파일을 비교하는 기능만 제공한다.
- `MONITORING_MODE=false`이거나 설정이 없으면 일반 Chat 화면만 렌더링한다.

아래 3장 이후의 지표·trace 설명은 `Chat > 개별 Chat Monitoring`과 재사용되는 로컬 집계 함수의 세부 계약이다. Supabase 신고 원문이나 운영자 제어 기록을 이 경로에서 조회한다는 뜻이 아니다.

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

`Chat > 개별 Chat Monitoring`에서 assistant 응답을 선택하면 좌측 그래프와 우측 상세 패널을 함께 표시한다. 신규 응답은 실행 당시 compiled LangGraph의 `xray=True` topology와 실제 task event를 응답 metadata에 스냅샷으로 저장하며, 화면은 이 데이터만으로 노드와 edge를 구성한다.

- `graph_schema_version`: 저장 계약 버전. 현재 버전은 `1`
- `graph_manifest`: graph ID, topology revision, 노드, 일반/조건부 edge. 회사 비교 subgraph도 펼쳐서 저장
- `node_runs`: 실행 순서, 노드 ID, invocation 회차, 완료/실패/중단 상태, trace 시작 기준 상대 시각, 실측 시간, 결과 key 이름만 저장
- 질문, node input, node result 값, 답변 본문은 graph trace에 중복 저장하지 않음
- 코드의 graph topology가 바뀌면 이후 응답은 새 manifest와 revision을 자동 저장하고, 과거 응답은 당시 스냅샷을 그대로 표시
- edge는 실행 당시 topology만 나타내며 실제 통과 여부를 추정해 강조하지 않음
- 같은 노드가 병렬 실행되면 화면의 노드 시간은 최초 시작부터 최종 종료까지의 wall-clock 구간이며, 개별 invocation 시간 합계는 별도 보조 정보로 표시
- 제한 시간을 넘긴 응답도 deadline까지 수집한 manifest와 NodeRun을 저장하고 실행 중이던 NodeRun은 `interrupted`로 봉인
- 현재 지원하지 않는 schema 또는 손상된 graph trace는 명시적 오류로 표시하고 6단계 그래프로 대체하지 않음
- topology/stream 관측 계약이 깨진 경우에도 답변 자체는 유지하되 `graph_manifest.capture_error`를 저장해 신규 응답을 legacy로 오인하지 않음
- 위 필드가 없는 기존 응답은 `input`, `query_rewrite`, `search_scope`, `routing`, `retrieval`, `answer` 6단계 호환 그래프로 표시

- 좌측 기본 선택: `전체 지표`; 단계 노드를 선택하면 우측이 노드 상세로 전환
- 우측 전체 지표: 총시간, 검색 실행 방식, 대상별 근거, 인용 연결
- 우측 전체 지표: 실제로 측정된 구간만 있는 병목 표와 비교 대상별 검색 상태
- 우측 전체 지표: 입력·출력 토큰, 최초 토큰, 실제 provider, 출력 토큰/초
- 우측 전체 지표: 답변에 사용된 문서
- 우측 노드 상세: 저장된 NodeRun 상태·시간·결과 key, 연결 edge와 기존 단계별 검색 근거
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

### 문제 신고 경계

`Chat > 개별 Chat Monitoring`과 아래의 legacy 집계 helper는 Supabase 신고 목록이나 production Issue lifecycle을 렌더링하지 않는다. 문제 신고는 Chat 화면에서 사용자가 동의한 항목만 redaction한 뒤 별도 SQLite outbox에 기록하고 Supabase 수신함으로 비동기 전송한다. 최상위 `Monitoring`만 인증된 신고 목록과 lifecycle을 다루며, 과거 `src/core/monitoring.py`의 로컬 candidate API는 호환·연구용 backend로 남아 있지만 production 작업함의 discovery 대상은 아니다.

## 5. V2 데이터 경계

활성 Monitoring은 다음 원칙을 지킨다.

1. 검색 상태는 `catalog.sqlite3`, active base snapshot과 ready delta segment를 기준으로 계산한다.
2. 무결성 검사는 V2 snapshot, membership, manifest, runtime, cleanup backlog만 본다.
3. 정확도는 스키마·hash·실제 runtime provenance가 모두 검증된 Native V2 run만 집계한다.
4. 이 절의 legacy 로컬 집계 화면은 신고와 회귀 후보를 discovery하지 않는다. production 최상위 `Monitoring`은 별도 인증 경계에서 Supabase Issue를 조회한다.
5. 계약이 유효하지 않은 평가 산출물은 현재 지표로 집계하지 않는다.

## 6. 주요 구현 파일

| 영역 | 파일 |
| --- | --- |
| Streamlit 진입 및 page 선택 | `apps/gui/app.py`, `apps/gui/sidebar_views.py` |
| production 운영자 Monitoring | `apps/gui/operator_monitoring_views.py`, `src/core/monitoring_admin_client.py` |
| 재현 registry와 service | `src/core/operator_monitoring.py`, `src/core/operator_monitoring_service.py` |
| FixedSnapshot·Release·runner | `src/core/fixed_snapshot.py`, `src/core/release_assets.py`, `apps/cli/reproduction_runner.py` |
| 개별 Chat Monitoring | `apps/gui/monitoring_views.py`, `src/core/graph_observability.py` |
| 시간·정확도·trace·V2 무결성 집계 | `src/core/monitoring.py` |
| Chat 문제 신고 payload와 outbox | `src/core/issue_report_store.py`, `src/core/issue_report_outbox.py` |
| Native V2 상태 | `src/core/status.py` |
| 응답 metadata 생성 | `src/graphs/state.py`, `src/nodes/*` |

## 7. 화면 진입 조건

production 운영자 화면은 `MONITORING_MODE=true`만으로 열리지 않는다. `DEPLOYMENT_ENVIRONMENT=production`, Supabase project URL·publishable key·operator Function URL, `MONITORING_ARTIFACT_ROOT`가 모두 필요하다. 전체 설정과 배포 검증은 [사용자 신고 기반 개선 루프](IMPROVEMENT_LOOP.md#13-검증과-배포-판정)를 따른다.

```bash
streamlit run apps/gui/app.py
```

## 8. 현재 한계

- 승인된 정식 V2 evaluation fixture가 준비되기 전에는 정확도를 수치로 확정할 수 없다.
- 자동 debug hint는 규칙 기반이므로 조사 시작점으로만 사용한다.
- 적은 평가 표본만으로 품질 우열이나 배포 여부를 자동 결정하지 않는다.
- Chat 문제 신고는 제출 전 redaction preview를 제공하지만, 사용자가 명시적으로 동의한 대화 metadata는 원격 전송될 수 있다.
- LLM 생성·query rewrite의 독립 latency는 아직 계측하지 않으며 전체 응답시간에만 포함된다.
- 일반 대화 observability는 conversation DB 보존 정책을 따르며 정식 평가 증거로 취급하지 않는다.
