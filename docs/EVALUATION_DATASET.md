# Evaluation Dataset 준비 작업

## 현재 상태

2026-07-30 기준으로 이 저장소에는 정식 evaluation fixture, multi-turn
evaluation dataset과 승인된 Native V2 평가 revision이 없다. 기존
`tests/fixtures` 데이터와 그 데이터에서 생성된 과거 evaluation run은
제거했다.

따라서 현재 코드와 테스트는 다음 파일이 존재한다고 가정하면 안 된다.

- `tests/fixtures/evaluation_dataset.json`
- `tests/fixtures/multiturn_evaluation_dataset.json`
- `tests/fixtures/eval_snapshot/manifest.json`
- `tests/fixtures/eval_snapshot/retrieval/v2/catalog.sqlite3`
- `tests/fixtures/eval_snapshot/retrieval/v2/snapshots/*`

평가 실행기와 검증 코드는 미래 데이터에 사용할 계약으로만 유지한다.
파일이 없으면 Monitoring UI는 평가 기능을 준비 중으로 표시하고, 데이터
의존 테스트는 명시적으로 skip한다. 임시 데이터나 현재 live DB를 정식
baseline으로 간주해서는 안 된다.

## 데이터 준비 후 해야 할 일

아래 작업은 원천 데이터가 완전히 준비되고 검토 책임자가 정해진 뒤에
진행한다.

1. 기준 데이터 범위를 고정한다.
   - 포함 기간, report type, 종목·산업·경제 coverage를 명시한다.
   - 누락 문서, 중복 문서, 임베딩 실패 문서를 먼저 정리한다.
   - 개인정보와 원문 보관 정책을 확인한다.
2. 실제 데이터에서 evaluation case를 생성한다.
   - route, filter, source, citation, RDB 집계 기대값을 검토한다.
   - 정확성 우선·균형·속도 우선 프로필별 대표 case를 포함한다.
   - 후속 질문과 이전 검색 범위 재사용 case도 별도 dataset으로 만든다.
3. 동일한 데이터 revision으로 Native V2 평가 bundle을 만든다.
   - V2 catalog, immutable base snapshot, 필요한 delta segment와 manifest를
     한 번에 고정한다.
   - manifest에는 data/index revision, 행 수, 날짜 범위, 모델·설정
     fingerprint와 파일 hash를 기록한다.
   - dataset과 snapshot 중 하나만 단독으로 갱신하지 않는다.
4. 운영자 검토를 거쳐 baseline을 승인한다.
   - 질문과 기대값을 사람이 검토한다.
   - 재현되지 않는 case나 우연히 통과하는 case를 제거한다.
   - latency p95의 반복 횟수, warm-up, 허용 예산을 실제 측정값으로
     확정한다.
5. 검증 후 저장소에 반영한다.
   - dataset schema 테스트
   - snapshot manifest/hash 검증
   - candidate baseline/verification 실행
   - 전체 회귀 테스트

## 생성 시 주의사항

현재 `scripts/build_evaluation_dataset.py`와 과거 snapshot runner는 호환성
검증용 `reports` schema를 전제로 하며 활성 Monitoring UI가 호출하지 않는다.
정식 기준 데이터를 만들기 전에 Native V2 catalog adapter와 요청 단위
revision pinning runner를 구현하고 별도로 검증해야 한다.

PDF 본문, 전체 대화 원문, API 응답 전문은 evaluation fixture에 넣지
않는다. 재현에 필요한 질문, 비식별 metadata, 기대 결과와 revision/hash만
저장한다.

저표본 case도 개선 대상에서 제외하지 않지만, 적은 표본만으로 자동 배포
또는 품질 우열을 결정하지 않는다.

## 완료 조건

다음 조건을 모두 만족해야 “정식 fixture와 Native V2 평가 revision이
준비됨”으로 간주한다.

- dataset과 V2 bundle이 동일한 승인된 data/index revision을 가리킨다.
- 모든 기대 source가 고정된 V2 composite revision에 존재한다.
- 재현 manifest가 완전하고 파일 hash 검증을 통과한다.
- 정확성·안전성 공통 하드 게이트가 모든 품질 프로필에 포함된다.
- 반복 latency 측정과 p95 산정 조건이 기록되어 있다.
- 데이터 의존 테스트와 전체 테스트가 통과한다.
- 생성일, 검토자, 변경 사유가 문서와 manifest에 기록되어 있다.
