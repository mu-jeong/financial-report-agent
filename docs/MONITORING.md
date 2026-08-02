# Monitoring Mode (Native V2)

Monitoring Mode는 답변 품질을 빠르게 확인하고, 문제가 생겼을 때만 상세 진단으로 내려가는 개발자용 화면이다. `.env`의 `MONITORING_MODE=true`일 때만 노출된다.

활성 화면과 새 산출물은 Native V2만 기준으로 삼는다. 스키마가 없는 과거 평가 run·신고·회귀 후보는 활성 화면에서 자동으로 제외한다. 파일을 삭제하지는 않으며, 명시적인 변환이나 호환성 테스트에 필요한 범용 로더만 별도 경계로 유지한다.

## 1. 화면 원칙

평상시 화면에는 다음 두 지표만 표시한다.

| 지표 | 정의 |
| --- | --- |
| 응답 속도 | 실제 Native V2 snapshot/generation provenance가 있는 assistant 응답의 `latency_seconds` P95. 평균과 표본 수도 보조 문구로 표시한다. |
| 답변 정확도 | 승인된 질문으로 실행하고 Native V2 snapshot/build/profile/generation을 고정·검증한 평가 run의 correctness 검사 통과율. latency 검사는 제외한다. |

속도 표본이나 승인된 V2 평가 run이 없으면 `측정 전`으로 표시한다. 데이터가 없다는 사실을 0초나 정확도 0%로 오해하지 않게 하기 위한 계약이다.

나머지 정보는 하나의 `문제 상황 자세히 보기 · 확인 필요 N건` expander 안에 둔다. 기본 대시보드에 route, source 수, snapshot 세부값, parser 비교, 신고 목록을 동시에 펼치지 않는다.

```text
Monitoring
├─ 응답 속도 (P95)
├─ 답변 정확도 (correctness-only)
└─ 문제 상황 자세히 보기
   ├─ 현재 문제
   ├─ 응답 원인 확인
   ├─ 검색 자료 준비
   ├─ 정확도 평가
   ├─ 문서 읽기 품질 비교
   └─ 신고·수정 확인
```

문제 영역의 내부 ID는 각각 `summary`, `response`, `search_data`, `evaluation`, `parsing`, `issues`로 고정한다. 화면 문구가 바뀌어도 widget state와 테스트가 흔들리지 않게 하기 위해서다.

## 2. 진입 구조

```text
Sidebar
├─ Chat
│  ├─ Chat
│  └─ 답변 모니터링
└─ Monitoring
```

- `Chat > 답변 모니터링`은 현재 대화의 시간 지표만 다룬다. 최근 성공 응답의 전체시간, 현재 대화 평균, RDB 평균 조회시간, Vector DB 평균 검색시간과 응답별 시간 표를 표시한다.
- `Monitoring`은 모든 대화의 속도, 공통 정확도, Native V2 검색 자료 상태와 문제 처리 도구를 다룬다.
- `MONITORING_MODE=false`이거나 설정이 없으면 일반 Chat 화면만 렌더링한다.

## 3. 두 기본 지표

### 3.1 응답 속도

`summarize_chat_messages()`와 `summarize_all_chat_threads()`가 assistant 응답 metadata에서 실제 Native V2 `runtime_mode`, `snapshot_id`, `publication_generation`이 확인된 latency 표본만 모은다. provenance가 없는 과거 응답은 속도에 섞지 않는다.

- 대표값: P95
- 보조값: 평균, 표본 수
- 표본 없음: `측정 전`
- 실패 응답은 Native V2 provenance까지 기록된 경우에만 속도 표본에 포함한다.

P95를 기본값으로 쓰는 이유는 평균만으로 가려지는 느린 꼬리 응답을 확인하기 위해서다.

개별 Chat 화면의 평균은 현재 thread에서 성공했고 Native V2 provenance가 검증된 응답만 사용한다. RDB 조회시간은 SQL guardrail·연결·실행·결과 반환 구간이며, Vector DB 검색시간은 scope compile·lease·FAISS·hydration을 포함하는 `native_total_ns`이다. 호출하지 않은 backend나 과거 metadata처럼 표본이 없는 값은 `0`이 아니라 `측정 전`으로 표시한다. 정확도는 개별 응답에서 바로 판정하지 않고 전체 Monitoring의 검증된 evaluation run에서만 다룬다.

### 3.2 답변 정확도

`summarize_evaluation_accuracy()`는 hash와 실제 Native V2 runtime provenance가 함께 검증된 평가 run만 읽는다.

- 포함: route, filter, source, citation, no-result, expected-state 등 활성 correctness 검사
- 제외: `latency_pass`, performance budget
- 집계 단위: correctness 검사가 하나 이상 활성화된 평가 case
- 평가 자료 없음 또는 구형 run: `측정 전`

느리지만 맞는 답변을 오답으로 계산하지 않고, 속도와 정확도를 서로 독립적으로 판단한다.

현재 저장소에는 승인된 정식 evaluation fixture가 없으므로 기본 상태는 `측정 전`이다. 임시 데이터나 live DB 결과를 승인된 정확도 기준으로 간주하지 않는다. 준비 절차는 [Evaluation Dataset 준비 작업](EVALUATION_DATASET.md)을 따른다.

## 4. 문제 상황 상세 영역

### 현재 문제

- 최근 실패 응답
- Native V2 무결성 warning/fail
- 확인이 필요한 항목 수

기술 키 대신 사용자 문구를 표시한다. 예를 들어 `native_membership`은 `검색 대상 일치`, `cleanup_backlog`는 `검색 데이터 정리 대기`로 표현한다.

### 응답 원인 확인

대화와 assistant 응답을 선택한 뒤 다음 정보를 필요할 때만 expander로 연다.

- 자동 debug hint
- 직전 성공 응답과의 차이
- query rewrite와 검색 범위
- routing
- retrieval/rerank와 선택 출처
- answer/citation
- 처리 흐름 metadata

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

raw snapshot ID, generation, epoch 같은 값은 `기술 세부정보` expander 안에 둔다. Native V2 상태가 없으면 V1 지표로 우회하지 않고 `V2 retrieval status is unavailable` 문제를 표시한다.

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

활성 화면은 다음 V2 계약만 자동 발견한다.

| 산출물 | 활성 계약 |
| --- | --- |
| Issue report | `schema_version=2`, `report_contract_version=2` |
| Regression candidate | `schema_version=2`, `contract_schema_version=2` |
| Evaluation run | `schema_version=2`, 유효한 `run_hash`, 검증된 successor Native V2 provenance |

구형 파일은 활성 목록에서 조용히 제외한다. 새 issue report와 이메일 text import는 V2 계약으로 저장한다.

회귀 후보의 자동 baseline·verification은 Native V2 revision을 고정한 실행 결과만 증거로 연결한다. 활성 UI는 과거 고정 DB/vector 실행기를 호출하지 않는다. 아직 V2 고정 revision runner가 준비되지 않은 경우 수동 검사 결과를 기록하거나, 외부에서 생성된 검증 가능한 V2 run을 미연결 실행 목록에서 연결한다. 단순 후보 초안 진단 실행은 현재 Native V2 backend를 사용하지만 정식 lifecycle 증거로 자동 승격하지 않는다.

## 5. V2 데이터 경계

활성 Monitoring은 다음 원칙을 지킨다.

1. 검색 상태는 `catalog.sqlite3`, active base snapshot과 ready delta segment를 기준으로 계산한다.
2. 무결성 검사는 V2 snapshot, membership, manifest, runtime, cleanup backlog만 본다.
3. 정확도는 스키마·hash·실제 runtime provenance가 모두 검증된 Native V2 run만 집계한다.
4. 신고와 회귀 후보 discovery도 V2 계약만 사용한다.
5. 과거 산출물은 자동 삭제하거나 현재 지표로 변환하지 않는다.

범용 legacy loader는 과거 artifact의 명시적 점검·변환과 기존 회귀 테스트를 위해 남아 있지만, `render_global_monitoring_page()`와 `render_chat_monitoring_page()`에서는 호출하지 않는다.

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
- 개별 Chat에서 정확도와 trace 진단을 노출하지 않음
- provenance 없는 과거 응답과 legacy runtime latency 제외
- latency가 정확도에 섞이지 않음
- self-labeled/tampered run 및 runtime revision 불일치 차단
- 평가 자료가 없을 때 `측정 전`
- 기본 화면의 두 지표와 단일 문제 expander
- 활성 UI에 과거 DB/vector 실행 경로가 없음
- 스키마 없는 report/candidate/run의 활성 discovery 제외
- Native V2 무결성 및 cleanup backlog 표시
- Native V2가 없을 때 legacy DB/vector 상태를 읽거나 렌더하지 않음
- issue report와 candidate lifecycle의 기존 호환 경계

## 8. 현재 한계

- 승인된 정식 V2 evaluation fixture가 준비되기 전에는 정확도를 수치로 확정할 수 없다.
- 자동 debug hint는 규칙 기반이므로 조사 시작점으로만 사용한다.
- 적은 평가 표본만으로 품질 우열이나 배포 여부를 자동 결정하지 않는다.
- issue report에는 재현에 필요한 대화 metadata가 포함될 수 있으므로 외부 전달 전에 내용을 확인한다.
