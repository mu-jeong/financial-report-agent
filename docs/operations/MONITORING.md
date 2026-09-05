# Monitoring Mode (Native V2)

> 사용자 신고부터 재현·릴리스 비교·이슈 분류까지의 현재 구현 권위 문서는 [사용자 신고 기반 개선 루프](IMPROVEMENT_LOOP.md)다. 이 문서는 Monitoring 화면과 개별 Chat 지표·trace의 세부 계약을 설명한다.

`MONITORING_MODE=true`이면 로컬 `Chat > 개별 Chat Monitoring`과 `개선 실험`을 쓸 수 있다. 최상위 운영자 `Monitoring`은 production 전용이며 진입 조건은 7장을 따른다.

## 현재 동작 화면

운영 화면은 작업함(상태별 신고 목록 → 신고 요약·관측값 → 재현·비교 진행 상태)과 이슈 종결 준비 흐름으로 이어진다. 아래 캡처는 읽기 전용 검증 기록이며 run 실행·Comparison 저장·상태 변경 같은 기록 버튼은 누르지 않았다.

- 상태별 작업함: 재현·비교 기록이 연결된 신고를 선택한다.
- 신고 요약·관측값: 응답 속도·판단 상태·버전·route·동의 범위를 한 화면에 보여준다.
- 현재 근거·다음 할 일: 로컬 registry에서 파생한 진행 상태와 다음 작업을 안내한다.
- 이슈 종결 준비: 비교 근거 검토 후 허용된 다음 상태와 사유를 선택한다.

각 화면 캡처는 [`../images/monitoring/loop-*.png`](../images/monitoring/)에서 확인한다. 접수부터 종결까지 전체 순서는 [개선 루프의 전체 동작 화면](IMPROVEMENT_LOOP.md#전체-동작-화면)을 따른다.

## 1. 운영자 Monitoring 범위

최상위 `Monitoring`은 `작업함`, `테스트 케이스 설정`, `개선 확인` 세 화면이고, 자산 availability·control drift·cleanup 경고는 설정 expander에 표시한다. 상태 전이·재현 자산·Run·Comparison의 불변조건은 [개선 루프](IMPROVEMENT_LOOP.md)가 기준이며 이 문서는 화면에서 무엇을 어떻게 보여주는지만 설명한다.

작업함은 최근 신고 최대 200건의 상태별 건수·목록을 보여주고, 신고 선택 시 동의 원문을 자동 조회해 최초 열람을 감사한다. Baseline/Candidate 실행 중에는 `사전 점검 → 자산 검증 → 대기열 등록 → 실행 → 결과 검증·저장` 단계를 표시한다. 활성 화면과 새 평가 산출물은 Native V2만 기준으로 삼는다.

## 2. 화면 원칙과 진입 구조

```text
Sidebar
├─ Chat
│  ├─ Chat
│  └─ 개별 Chat Monitoring
├─ Monitoring          (production 관리자 전용)
└─ 개선 실험            (로컬 파싱 엔진 비교)
```

- `Chat > 개별 Chat Monitoring`은 자기 대화 한 건만 보는 로컬 진단이다(운영 신고함 아님). 응답 선택 시 좌측에 실행 단계 그래프, 우측에 총시간·검색 방식·대상별 근거·인용 연결을 표시한다.
- 최상위 `Monitoring`은 production 관리자만 접근하는 신고 작업함·테스트 케이스 설정·개선 확인이다.
- 최상위 `개선 실험`은 Chat 내부에 중복 노출하지 않고 사이드바에서만 진입한다.
- `MONITORING_MODE=false`이면 일반 Chat 화면만 렌더링한다.

호환용 로컬 집계 helper는 top-level에 노출하지 않으며 `응답 속도(P95)`와 `답변 정확도(correctness-only)` 두 지표만 남겨 둔다. 세부 화면은 `운영 모니터링`과 `성능 개선 실험` 그룹 아래에서 선택하며, 선택하지 않은 route·source·snapshot·parser 비교는 렌더링하지 않는다. 각 그룹은 마지막 선택을 따로 기억하고, 선택된 panel만 계산하도록 `st.segmented_control`을 쓴다.

## 3. 기본 지표와 turn 근거

### 응답 속도

Native V2 `runtime_mode`·`snapshot_id`·`publication_generation`이 확인된 응답만 표본으로 쓴다. 대표값은 P95(느린 꼬리 확인 목적), 보조로 평균·표본 수, 없으면 `측정 전`이다.

주요 시간 구간: 전체 응답(wall-clock), RDB 조회, Vector DB 검색(scope compile·lease·FAISS·hydration 합), 비교 대상별 검색, 답변 합성, 최초 토큰. 병렬 branch 작업시간 합은 전체 wall-clock과 직접 합산하지 않으며, 계측이 없는 구간은 추정 없이 `측정 전`으로 둔다.

### 개별 turn state와 검색 근거

응답 metadata에는 원문 state 대신 재현·진단에 필요한 compact snapshot만 남긴다(route·filter·scope 출처, no-result·memory retry 여부, 단계별 상태). 검색 `k`는 하나로 합치지 않고 `configured_top_k / requested_k / fetch_k / candidate_count_before_filter / candidate_count_after_filter / context_count`로 나눈다.

Vector DB 근거는 최종 prompt에 들어간 passage만 기록하며(`chunk_uid`, `parent_uid`, `report_uid`, span, rank, score), RDB 근거는 별도 `rdb_evidence`로 표시한다. 청크·PDF 본문, provider 원문 응답, 전체 router metadata는 복제하지 않는다. `근거 연결 상태`는 의미 정확도 점수가 아니라 구조적 연결 여부(`linked/partial/unavailable/not_applicable/not_measured`)다.

### 답변 정확도

hash와 Native V2 runtime provenance가 함께 검증된 평가 run만 집계한다. 집계 단위는 correctness 검사가 활성화된 평가 case이며 `latency_pass`·performance budget은 제외한다. 평가 자료가 없으면 `측정 전`이다. 승인된 정식 evaluation fixture가 없으면 임시 데이터나 현재 `DATA_ROOT` 결과를 승인된 정확도 기준으로 간주하지 않는다.

## 4. 문제 상황 상세 영역

- **현재 문제:** 최근 실패 응답, Native V2 무결성 warning/fail, 확인 필요 항목 수. 기술 키 대신 사용자 문구(`native_membership` → `검색 대상 일치`)로 표시.
- **응답 원인 확인:** 실행 당시 `graph_schema_version`(현재 `1`), `graph_manifest`, `node_runs`(노드 ID·invocation 회차·상태·상대 시각·실측 시간, 값은 제외)를 응답 metadata 스냅샷으로 저장하고 화면은 이 데이터만으로 그래프를 구성한다. topology가 바뀌면 다음 응답은 새 manifest를, 과거 응답은 당시 스냅샷을 쓴다. edge 통과 여부는 추정하지 않고, 미지원/손상 schema는 6단계 호환 그래프로 대체하지 않고 오류로 표시한다(위 필드가 없는 기존 응답만 6단계 호환).
- **검색 자료 준비:** 검색 가능·미반영 문서 수, 반영률, snapshot/build 상태, membership·vector 수 일치, backlog, cleanup 대기 수·용량·최장 시간, 제외 문서 재시도. Native V2가 없으면 우회하지 않고 문제를 표시.
- **정확도 평가:** 승인된 질문을 현재 Native V2 데이터로 실행(`schema_version=2`, `run_hash` 검증, 고정 provenance). 실패 case는 route/filter/source/citation/no-result 원인별로 조치 후보를 준다.
- **문서 읽기 품질 비교:** PDF 추출 엔진별 성공/실패·추출량·시간 비교. 문제 문서 의심 시에만 열며 정확도 수치에 자동 합산하지 않는다.
- **문제 신고 경계:** 개별 Chat Monitoring과 legacy 집계 helper는 Supabase 신고 목록·production Issue lifecycle을 렌더링하지 않는다. 신고는 Chat 화면에서 동의·redaction 뒤 outbox 기록 → Supabase 비동기 전송이며, 최상위 `Monitoring`만 인증된 신고 목록과 lifecycle을 다룬다.

## 5. V2 데이터 경계

검색 상태는 `catalog.sqlite3`와 active base snapshot·ready delta를 기준으로 계산하고, 무결성 검사는 V2 snapshot·membership·manifest·runtime·cleanup backlog만 본다. 정확도는 스키마·hash·runtime provenance가 모두 검증된 run만 집계하며, 계약이 유효하지 않은 산출물은 현재 지표로 쓰지 않는다.

## 6. 주요 구현 파일

| 영역 | 파일 |
| --- | --- |
| production 운영자 Monitoring | `apps/gui/operator_monitoring_views.py`, `src/core/monitoring_admin_client.py` |
| 재현 registry·service | `src/core/operator_monitoring.py`, `src/core/operator_monitoring_service.py` |
| FixedSnapshot·Release·runner | `src/core/fixed_snapshot.py`, `src/core/release_assets.py`, `apps/cli/reproduction_runner.py` |
| 개별 Chat Monitoring | `apps/gui/monitoring_views.py`, `src/core/graph_observability.py` |
| 시간·정확도·trace·V2 무결성 집계 | `src/core/monitoring.py` |
| Chat 신고 payload·outbox | `src/core/issue_report_store.py`, `src/core/issue_report_outbox.py` |
| Native V2 상태 | `src/core/status.py` |
| 진입·page 선택 | `apps/gui/app.py`, `apps/gui/sidebar_views.py` |

## 7. 화면 진입 조건과 현재 한계

production 운영자 화면은 `MONITORING_MODE=true`만으로 열리지 않는다. `DEPLOYMENT_ENVIRONMENT=production`, Supabase URL·publishable key·operator Function URL, `MONITORING_ARTIFACT_ROOT`가 모두 필요하고, 서버는 Supabase Auth 사용자와 `private.monitoring_admins.active`를 다시 확인한다. 배포 검증은 [개선 루프의 배포 판정](IMPROVEMENT_LOOP.md#13-검증과-배포-판정)을 따른다.

```bash
streamlit run apps/gui/app.py
```

현재 한계: 승인된 evaluation fixture 없이는 정확도를 수치로 확정할 수 없고, debug hint는 규칙 기반으로 조사 시작점에만 쓴다. 적은 표본으로 품질 우열·배포를 자동 결정하지 않으며, LLM 생성·query rewrite의 독립 latency는 아직 계측하지 않는다. 일반 대화 observability는 정식 평가 증거가 아니다.