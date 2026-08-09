# 의사결정 검토안 0002: 버전 영역 분리와 실행 상태 식별

| 항목 | 값 |
| --- | --- |
| 상태 | 운영판 검토안 · 미승인 |
| 작성일 | 2026-08-08 |
| 검토 책임자 | 기획자 / 개발자 공동 |
| 기술 검토자 | 지정 필요 |
| 관련 요구사항 | PROD-007/008, DATA-007/008, VER-001~009, IMP-003~011 |

> 현재 PoC/MVP 저장소의 구조 결정이 아니다. 별도로 진행할 운영판에서 채택할지 검토하기 위한 자료이며, 운영판 저장소에서 승인되기 전에는 ADR이나 구현 계약으로 간주하지 않는다.

## 배경

결과에 영향을 주는 요소는 application code만이 아니다. persisted schema, source corpus, 포함·제외 규칙, extractor, chunk/overlap, embedding, base snapshot과 active delta, prompt, model, config, dependency, evaluation policy가 독립적으로 바뀐다.

현재 저장소도 이미 이 문제를 부분적으로 분리해 관리한다.

- retrieval schema version
- logical identity/profile hash
- corpus manifest hash
- build/snapshot ID
- publication generation/write epoch
- reproduction fingerprints
- evaluation run/candidate lifecycle

하나의 수동 `전체 버전`이나 `디버깅 관련 변경만 버전업` 규칙으로는 두 실행 상태가 실제로 같은지 판정할 수 없다. 특히 active delta chain이 달라지면 같은 base snapshot이라도 검색 결과가 달라질 수 있다.

또한 공개 설치본은 local-first이며 corpus/delta가 설치별로 달라질 수 있다. 이를 중앙 software release identity에 포함하면 모든 정상 local update가 새 unknown release가 되고, 중앙 rollback이 로컬 권위를 침범한다. 반대로 Edge validation/DB migration이 바뀌어도 client release는 그대로일 수 있으므로 server ingest identity도 분리해야 한다.

## 검토안

1. 사람이 읽는 software `app_version`은 SemVer로 관리한다.
2. 중앙 배포 package는 code/build/dependency/local-schema compatibility/prompt/model/default config를 담은 immutable `SoftwareReleaseManifest`로 식별한다. corpus/snapshot/delta는 넣지 않는다.
3. 각 설치본의 corpus/profile/base snapshot/content hashes와 publication generation/write epoch/ordered delta action chain을 canonicalize한 exact-state `LocalRuntimeRevision`으로 식별한다. delete-only 또는 zero-vector segment의 nullable artifact도 segment action chain에서 제외하지 않는다.
4. Edge code, validation/redaction/rate policy, remote DB migration은 `IngestDeploymentManifest`로 식별하고 서버 receipt에 stamp한다.
5. persisted DB는 catalog/conversation/outbox/remote migration domain별 schema version과 read/write range를 가진다.
6. 모든 response trace, issue, event, evaluation run은 SoftwareReleaseManifest와 exact LocalRuntimeRevision을 함께 참조한다. anonymous 값은 claim이며 operator artifact 검증 전에는 신뢰를 올리지 않는다.
7. evaluation case/scorer/threshold는 `EvaluationSuiteRevision`으로 식별한다.
8. qualifying run ID, tested local runtime, 승인 시각, 배포 상태, predecessor와 decision은 manifest가 아니라 append-only `PromotionRecord` event에 둔다. 각 event는 immutable ID, 이전 event ID, release별 단조 sequence, action, reason을 가지며 unique constraint와 predecessor compare-and-swap으로 동시 결정을 직렬화한다. 이 분리로 run과 candidate manifest의 순환 identity를 막는다.
9. 중앙 known-release quarantine은 SoftwareReleaseManifest에만 적용한다. 처음 보는 유효한 LocalRuntimeRevision은 `unverified_claim` cohort로 관측한다.
10. baseline/candidate/qualifying run/unresolved issue/rollback predecessor가 참조하는 local artifact에는 hold를 걸어 GC를 방지한다.
11. promotion은 manual package 배포 승인이다. software 설치/rollback과 local retrieval publication/rollback은 분리하며 중앙에서 자동 강제하지 않는다.
12. 이미 사용된 artifact/manifest/revision/promotion record는 수정하지 않고 새 identity 또는 후속 record를 만든다.
13. Figma와 서술 문서는 version 의미를 설명하지만 exact 계약의 기준 원본은 machine-readable schema, generator, test다.

## 변경 시 새 식별자가 필요한 기준

다음 질문 중 하나라도 `예`이면 관련 식별자가 바뀌어야 한다.

- 사용자 또는 시스템 동작이 달라질 수 있는가?
- 기존 저장 데이터나 API와 호환성이 달라지는가?
- 동일 입력의 결과를 재현하는 데 필요한 조건이 달라지는가?
- 개인정보, 보안, redaction 또는 운영 안전 판단이 달라지는가?
- 평가 결과나 승격 판단의 의미가 달라지는가?

모든 식별자를 무조건 바꾸는 것은 아니다. 변경 영향 매트릭스가 어떤 domain revision과 검증을 갱신할지 결정한다.

## 예상 영향

### 기대 효과

- 이슈와 성능 변화를 정확한 software/local runtime/server ingest 상태에 연결할 수 있다.
- data-only, prompt-only, evaluation-only 변경을 app SemVer와 혼동하지 않는다.
- 정상적인 설치별 local update를 unknown software release로 오분류하지 않는다.
- software와 local retrieval의 서로 다른 activation/rollback 책임을 명확히 한다.
- 자동 hash가 수동 version 누락을 줄인다.

### 비용과 제약

- 네 identity/record의 canonicalization과 validation code가 필요하다.
- artifact retention과 reference graph 관리가 복잡해진다.
- 기존 evaluation/reproduction row에 migration 또는 legacy unknown 상태가 필요하다.
- 동일 SemVer 아래 software build와 여러 local-runtime cohort를 운영자 UI가 구분해 보여줘야 한다.

## 채택하지 않는 대안

### 하나의 수동 전체 버전

어떤 구성요소가 바뀌었는지 설명할 수 없고 bump 누락에 취약하다. 호환성과 runtime generation까지 한 숫자에 섞인다.

### 디버깅에 직접 필요한 변경만 버전업

`직접 필요`의 판단이 사후적·주관적이다. privacy, migration, evaluator threshold처럼 결과 원인 분석 외에도 안전과 승인 의미를 바꾸는 요소가 누락된다.

### Git commit만 전체 버전으로 사용

같은 code에서도 corpus, active delta, model alias, runtime config가 달라질 수 있다. Git은 중요한 한 축이지만 전체 effective state가 아니다.

### Software와 local runtime을 한 manifest에 포함

설치별 local update마다 중앙 software release hash가 달라지고 unknown quarantine이 정상 event를 막는다. 중앙 승격/롤백이 로컬 data authority를 통제하는 것처럼 보이므로 분리한다.

### DB/FAISS rollback만으로 software rollback을 대체

이전 data/index가 현재 code/schema와 호환된다는 보장이 없다. 기존 retrieval publication 안전 장치는 local data rollback에 유지하고 software package rollback은 별도 installer/migration 절차로 수행한다.

### Figma를 version matrix의 기준 원본으로 사용

실행 코드와 자동 검증할 수 없고 drift가 생긴다. Figma는 승인 계약을 시각화하고 Git reference를 표시한다.

## 적용 순서

1. SoftwareReleaseManifest, LocalRuntimeRevision, IngestDeploymentManifest, PromotionRecord v1 schema와 canonicalization을 정의한다.
2. 현재 code/schema/prompt/model/default config를 legacy software manifest로 capture한다.
3. corpus/profile/base snapshot/publication generation/write epoch/ordered delta action chain을 LocalRuntimeRevision으로 계산한다.
4. response, issue, event, evaluation provenance를 software + local reference로 확장한다.
5. server receipt에 current ingest deployment hash를 추가한다.
6. evaluation suite revision, authenticated run 등록, artifact hold, PromotionRecord gate를 추가한다.
7. release tooling이 clean build에서 software manifest와 package checksum을 자동 생성하게 한다.
8. manual installer activation과 software/local rollback을 각각 검증한다.
9. provenance 없는 legacy record는 `legacy_unknown`으로 표시하고 승격 근거에서 제외한다.

## 승인 전 확인 사항

- 동일 canonical input이 플랫폼/실행 순서와 무관하게 같은 hash를 만든다.
- code/dependency/prompt/default config는 software manifest를, corpus/chunk/delta는 local runtime을, Edge/RLS/remote migration은 ingest manifest를, scorer는 suite revision을 각각 바꾼다.
- secret 값이 manifest에 포함되거나 fingerprint input이 되지 않는다.
- incomplete/dirty software manifest와 artifact가 없는 local revision은 qualification이 거부된다.
- active response와 evaluation run이 exact software/local revision을 참조한다.
- 처음 보는 valid local revision은 unknown software quarantine에 들어가지 않는다.
- anonymous evaluation observation은 PromotionRecord의 qualifying run으로 연결되지 않는다.
- 같은 software release에 대한 동시 PromotionRecord append는 unique sequence와 predecessor compare-and-swap으로 하나만 성공한다.
- predecessor software installer와 local retrieval revision rollback을 독립적으로 재현한다.
