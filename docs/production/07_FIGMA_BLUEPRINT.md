# Figma 시각적 기획 지식베이스 설계도

| 항목 | 값 |
| --- | --- |
| 상태 | 운영판 시각 기획 초안 |
| 문서 버전 | 0.2.0 |
| 기획 책임자 | 기획자 / 설계자 |
| 기술 검토자 | 지정 필요 |
| 최종 갱신일 | 2026-08-09 |
| Figma 주소 | 입력 필요 |

> 현재 PoC/MVP 화면 설계가 아니라, 별도 운영판의 범위·관계·생애주기를 한눈에 검토하기 위한 시각 기획 구조다. 확정된 구현 계약은 Git 문서와 코드·시험에 둔다.

## 1. Figma의 목적

Figma는 이 운영판 기획의 **1차 열람·이해·검토 화면**이다. 독자는 긴 문서를 순서대로 읽기 전에 Figma에서 전체 구조와 관계를 파악하고, 필요한 경우 연결된 Git 문서의 정확한 계약으로 내려간다.

```text
Figma: 왜 존재하고 어떻게 연결되는가
  -> Git 문서·contract: 정확히 무엇을 지켜야 하는가
  -> 코드·테스트·운영 증거: 실제로 지켜졌는가
```

Figma의 목표는 장식적인 요약본을 만드는 것이 아니라 다음 내용을 공간·방향·경계·상태로 표현해 한눈에 이해하도록 만드는 것이다.

- 로컬 앱과 Supabase의 신뢰 경계
- 데이터 객체의 권위·흐름·보존
- 버전 계보와 변경 영향
- telemetry/outbox/이슈 상태
- 개선·평가·승격·롤백 흐름
- 운영자 dashboard와 사용자 이슈 제출 UX

기획 탐색과 구조화는 Figma/FigJam에서 시작할 수 있다. 다만 승인된 정책, 정확한 필드, 실행 가능한 상태 전이, DDL, JSON Schema, hash 계산, retention job, RLS policy는 Git 문서·machine-readable contract·코드·테스트에서 관리한다. Figma는 이 정확한 계약을 사람이 가장 먼저 이해하는 시각적 지식베이스이며, 의미가 바뀌는 결정은 Git 기준 원본과 동기화되어야 승인될 수 있다.

운영 원칙은 다음 한 문장으로 요약한다.

> 읽기 시작은 Figma에서, 정확성 확인과 변경 이력은 Git에서 한다.

## 2. 파일 구조

Figma 파일 이름 제안: `Finance LLM — Production Operating Model`

| Page | 목적 | 연결 문서 |
| --- | --- | --- |
| `00 Cover & Governance` | 첫 방문자를 위한 전체 지도, 문서 상태, 범례, 링크, 승인자 | [문서 허브](README.md) |
| `01 Product Boundary` | 사용자/운영자/로컬/Supabase 경계 | [제품 범위](01_PRODUCT_AND_SCOPE.md) |
| `02 Data Inventory & Lineage` | 데이터 사전, 권위, 계보 | [데이터](02_DATA_DEFINITIONS_AND_LIFECYCLE.md), [버전](03_VERSIONING_AND_LINEAGE.md) |
| `03 Lifecycles` | artifact/outbox/issue/release 상태도 | [데이터](02_DATA_DEFINITIONS_AND_LIFECYCLE.md) |
| `04 Version Impact` | version tuple, change-impact matrix | [버전](03_VERSIONING_AND_LINEAGE.md) |
| `05 Observability` | public POST, validation, DB, dashboard | [관측성](04_OBSERVABILITY_AND_ISSUE_COLLECTION.md) |
| `06 Issue Experience` | 제출 preview/동의/접수 상태 | [관측성](04_OBSERVABILITY_AND_ISSUE_COLLECTION.md) |
| `07 Improvement & Release` | triage→평가→canary→승격/롤백 | [개선 루프](05_IMPROVEMENT_AND_RELEASE_LOOP.md) |
| `08 Operator Console` | 운영 화면 IA와 wireframe | [관측성](04_OBSERVABILITY_AND_ISSUE_COLLECTION.md) |
| `09 의사결정 검토안과 인계` | 검토안, 미결정, 구현·시험 링크 | [개발 인계](06_ENGINEERING_HANDOFF.md) |

### 2.1 읽는 방식

Figma 파일은 `Page -> Section -> Frame -> Node` 순서로 탐색한다.

- `Page`: 하나의 기획 영역. 왼쪽에서 오른쪽으로 읽는 순서를 유지한다.
- `Section`: 하나의 질문 또는 리뷰 단위. 고유 링크를 공유할 수 있어야 한다.
- `Overview Frame`: 해당 Page를 Git 문서 없이도 30초 안에 설명하는 한 화면 요약.
- `Detail Frame`: 상태 전이, 예외, actor, guard, evidence를 확대해 설명.
- `Node`: 상태, 데이터, actor, process처럼 다른 객체와 관계를 맺는 최소 의미 단위.

모든 Page의 첫 Section에는 `Overview`와 `이 Page에서 답하는 질문`을 둔다. 각 Detail Frame에는 상위 Overview로 돌아가는 링크와 정확한 Git section으로 내려가는 링크를 둔다. `00 Cover & Governance`는 파일을 열었을 때의 시작 지점이며 모든 Page의 Overview로 연결한다.

### 2.2 Figma Design과 FigJam의 경계

- FigJam은 브레인스토밍, 대안 탐색, 워크숍, 아직 정리되지 않은 흐름에 사용한다.
- Figma Design의 `Draft` Section은 리뷰 가능한 기획안에 사용한다.
- 승인된 지식은 Figma Design의 안정된 Frame ID와 Git requirement ID를 함께 가진다.
- FigJam의 sticky note, 투표, comment는 결정 근거가 될 수 있지만 그 자체를 승인된 정책으로 간주하지 않는다.

## 3. 시각적 기획서 작성 원칙

### 3.1 한 프레임, 한 질문

각 프레임은 제목만 읽어도 답하려는 질문이 분명해야 한다.

- 데이터는 어디에서 만들어지고 언제 폐기되는가?
- local authoritative store와 remote observability copy는 어떻게 다른가?
- 익명 이슈는 어떤 gate를 거쳐 평가 후보가 되는가?
- 승인, 게시, 설치, 활성화, 롤백은 어떻게 구분되는가?

하나의 프레임이 여러 질문을 다루기 시작하면 Overview와 Detail로 분리한다.

### 3.2 문장을 관계로 바꾼다

긴 문단을 그대로 붙여 넣지 않고 다음과 같은 관계로 변환한다.

```text
상태 -> 상태
입력 -> 처리 -> 출력
원본 -> 파생 데이터 -> 관측 사본
actor -> action -> evidence
candidate -> evaluate -> qualify -> publish -> install -> activate
```

노드에는 식별에 필요한 짧은 문구만 두고, 예외·근거·수용 기준은 annotation 또는 연결 문서로 내린다. 그림만으로 의미가 모호해지는 경우에는 줄이는 것보다 명시적인 label을 우선한다.

### 3.3 Overview와 Detail의 정보 밀도

- Overview는 주요 actor, 경계, happy path, 핵심 금지 경로만 보여준다.
- Detail은 transition별 actor, guard, evidence와 실패·복구 경로를 보여준다.
- 정확한 field/type/value 목록은 Figma에 복제하지 않고 Git section으로 연결한다.
- 한 프레임 안에서 축소해야만 글자를 읽을 수 있다면 프레임을 분리한다.

### 3.4 공통 프레임 메타데이터

모든 review 대상 프레임의 오른쪽 위에 `Spec Badge` component를 둔다.

```text
Frame ID: OBS-INGEST-01
Status: Draft | Reviewed | Approved | Implemented | Verified
Doc Ref: docs/production/04_OBSERVABILITY_AND_ISSUE_COLLECTION.md
Requirement IDs: OBS-001, OBS-004, OBS-005
Doc Version: 0.1.0
Git Commit: <commit>
Last Synced: YYYY-MM-DD
Owner / Reviewer: <name> / <name>
```

프레임 이름 형식:

```text
[STATUS] [DOMAIN-ID] 제목 — v<doc-version>
예: [DRAFT] [OBS-INGEST-01] Anonymous Event Ingest — v0.1.0
```

## 4. 시각 언어

색상만으로 상태를 전달하지 않고 label과 icon을 함께 쓴다.

| 의미 | 색/스타일 제안 | 필수 label |
| --- | --- | --- |
| 현재 구현 | Gray, solid | `CURRENT` |
| 승인된 제품 결정 | Blue, solid | `PRODUCT DECISION` |
| Engineering extension | Violet, dashed | `ENGINEERING` |
| 아직 결정 안 됨 | Amber, dashed | `TBD` |
| 금지/신뢰 경계 | Red, thick border | `DENY` / `UNTRUSTED` |
| 권위 데이터 | database icon + double border | `AUTHORITATIVE` |
| 관측 사본 | database icon + single border | `OBSERVABILITY COPY` |
| 비동기·실패 허용 | dotted arrow | `ASYNC / NON-BLOCKING` |
| 승인 gate | diamond | requirement ID |

공통 component:

- Actor: User, Single Operator, Background Worker
- Boundary: Public Device, Operator-Controlled Cloud
- Store: Local Authoritative, Remote Copy, Private Attachment
- Process: Persist, Redact, Enqueue, Validate, Evaluate, Promote
- Artifact Card: name, ID/hash, schema, owner, retention, state
- State Node: allowed transition, actor, evidence
- 의사결정 카드: 배경, 검토안, 채택하지 않은 대안, 관련 문서 링크
- Risk Card: trigger, impact, mitigation, owner

## 5. Page별 상세 설계

### 00 Cover & Governance

프레임:

1. `Start Here`: 이 파일에서 답하는 질문, 권장 읽기 경로, 영역별 Overview 링크.
2. `Document Map`: Figma Page와 Git 문서의 대응 관계 및 승인 상태.
3. `Knowledge Model`: Figma는 1차 열람·이해·리뷰 화면이고 Git 문서·contract는 정확한 기준 원본임을 표시.
4. `Role Split`: Product decision / Engineering extension / 공동 승인.
5. `Open Decisions`: PD/ER ID, owner, gate, due status.
6. `Change History`: doc version, commit, decision, affected frames.

### 01 Product Boundary

프레임 `PROD-BOUNDARY-01`:

- 왼쪽 신뢰 경계 밖: 불특정 다수 사용자와 공개 설치본.
- 설치본 내부: UI, local SQLite/FAISS/artifacts, outbox.
- 오른쪽 운영자 통제 경계: Edge Function, private application tables, private Storage, operator console.
- 금지 화살표: Supabase → active local snapshot/prompt 자동 변경.
- 붉은 note: `publishable key / installation_id ≠ credential`.
- 점선 화살표: `retry-only outbox -> async POST -> terminal local delete`, `Supabase outage must not block`. Model provider 의존성은 별도 실선으로 표시한다.

프레임 `PROD-SCOPE-02`:

- In scope / Out of scope / Later의 세 column.
- 각 item에 PROD requirement ID를 붙인다.

### 02 Data Inventory & Lineage

프레임 `DATA-INVENTORY-01`:

- 객체별 Artifact Card.
- 행: Source, Parsed(transient), Parent/Chunk, Vector, Profile, Corpus Manifest, Snapshot, Local Runtime Revision, Software Release Manifest, Ingest Deployment Manifest, Promotion Record, Event, Issue, Eval, Attachment.
- 열: Meaning, ID, Authority, Sensitivity, Retention, Owner.
- 정확한 물리 column/type 대신 문서 section link를 사용한다.

프레임 `VER-LINEAGE-02`:

```text
Source + metadata
 -> report_uid
 -> parent_uid/chunk_uid <- retrieval profile hash
 -> build/snapshot <- corpus manifest hash
 -> LocalRuntimeRevision <- publication generation/write epoch/ordered delta action chain
Code/build/schema/prompt/model/default config
 -> SoftwareReleaseManifest
SoftwareReleaseManifest + LocalRuntimeRevision + effective inference
 -> execution observation -> trace -> issue -> evaluation run
EvaluationSuiteRevision + authenticated run -> PromotionRecord
Edge/policy/remote DB migration -> IngestDeploymentManifest -> remote receipt
```

각 node에 `예시 값`이 아니라 `식별자 이름`만 표시해 실제 값 drift를 방지한다.

### 03 Lifecycles

상태도를 한 장에 겹치지 않고 객체별 frame으로 분리한다.

각 frame은 가운데에 정상 상태 전이를 왼쪽에서 오른쪽으로 배치하고, 실패·거부·복구 경로는 아래쪽 lane으로 분리한다. 오른쪽에는 `이 흐름에서 반드시 지켜야 할 것`을 3개 이내로 요약하고, 하단에는 requirement와 상세 Git section 링크를 둔다.

```text
+------------------------------------------------------------------+
| 질문 · 범위 · Owner · Spec Badge                                |
+------------------------------------------------------------------+
| Input -> state A -> state B -> validation -> ready -> complete   |
|             |            |           |                           |
|             +---------- failure / retry / recovery lane --------+|
+-----------------------------------------------+------------------+
| 핵심 invariant · 금지 경로                    | DATA-* · Doc Ref |
+-----------------------------------------------+------------------+
```

1. `DATA-BUILD-LC`: planned → cataloging → vector_building → validating → ready → committed_pending_checkpoint → fully_complete, 각 허용 지점의 failed.
2. `DATA-SNAPSHOT-LC`: staged → validating → ready/failed → garbage_pending → garbage_collected.
3. `DATA-DELTA-LC`: staged → ready/failed → compacted.
4. `OBS-OUTBOX-LC`: queued → sending → retry 또는 terminal outcome(ack/reject/expiry) 직후 로컬 행 삭제.
5. `OBS-REMOTE-ISSUE-LC`: in-memory draft → submitted → triaged → candidate linked/closed.
6. `IMP-CANDIDATE-LC`: executable `new → triaged → needs_expectation → ready → reproduced → fixing → verified → closed`; `new/triaged/needs_expectation/ready → duplicate|rejected`, `ready → not_reproducible`, `closed → triaged` 재오픈 guard를 함께 표시.
7. `IMP-DISTRIBUTION-LC`: immutable SoftwareReleaseManifest를 참조하는 candidate → qualified → published와, 별도 설치 상태 downloaded → verified → installed/migrated → locally active/recovered.

각 transition에는 actor, guard, evidence를 3줄로 표시한다.

```text
Actor: Operator
Guard: qualifying run + rollback ready
Evidence: run_id, decision record
```

### 04 Version Impact

프레임 `VER-TUPLE-01`:

- 왼쪽에 immutable SoftwareReleaseManifest, 아래에 설치별 LocalRuntimeRevision, 오른쪽에 IngestDeploymentManifest를 분리한다.
- execution observation이 software + local runtime을 연결하고 server receipt가 ingest deployment hash를 추가한다.
- EvaluationSuiteRevision과 authenticated run은 PromotionRecord에 연결한다.
- qualifying run, 승인자·시각, 배포 상태, predecessor는 software manifest body에 넣지 않는다.

프레임 `VER-IMPACT-02`:

- 행: code, schema, source, extraction, chunk/overlap, embedding, delta, prompt, model, config, evaluator, docs-only.
- 열: SemVer, software manifest, local runtime, ingest manifest, profile/corpus/composite, suite/promotion, rebuild, evaluation, migration.
- [버전 문서의 변경 영향 매트릭스](03_VERSIONING_AND_LINEAGE.md#5-변경-영향-매트릭스)에서 가져온다.
- matrix 값을 Figma에서 먼저 고치지 않는다.

프레임 `VER-GAPS-03`:

- delta provenance 누락
- README 기반 app version
- 부분 code fingerprint
- unapproved evaluation suite

각 risk에 owner, 영향, target work package를 붙인다.

### 05 Observability

프레임 `OBS-INGEST-01`:

1. Local feature commit.
2. Allowlist redact/normalize.
3. SQLite outbox enqueue.
4. Background batch and retry.
5. Edge size/schema/rate/replay/release checks.
6. server ingest manifest stamp.
7. accepted/duplicate/quarantined/rejected 분기.
8. private tables and operator dashboard.

public event/issue path와 authenticated canonical evaluation/release evidence path를 다른 색·lane으로 분리한다. anonymous `evaluation_run_observed`에서 PromotionRecord로 향하는 화살표는 `DENY`로 표시한다.

프레임 `OBS-TRUST-02`:

- 공개 가능한 값과 금지 credential을 좌우 비교.
- `installation_id`를 resettable correlation ID로 표시.
- public endpoint abuse 시나리오와 rate/quota/kill switch 연결.

프레임 `OBS-FIELDS-03`:

- Automatic / User-selected / Never의 세 column.
- 내용 데이터는 `explicit consent` gate를 지나야만 outbound로 연결.

### 06 Issue Experience

프레임 `OBS-ISSUE-USER-01`:

```text
문제 신고 열기
 -> 자동 진단 항목 확인
 -> 선택 내용 checkbox (기본 off)
 -> comment 작성
 -> 전송 preview
 -> 신고 접수 완료 안내
```

UX 요구사항:

- 사용자는 제출 직후 `신고가 접수되었습니다.` 안내만 확인한다.
- 원격 전송·재시도·최종 실패 상태는 화면에 노출하지 않는다.
- 사용자가 선택하지 않은 이전 turn은 preview에도 나타내지 않는다.
- installation ID reset과 telemetry opt-out 위치를 설정 화면에 둔다.

프레임 `OBS-ISSUE-OP-02`:

- Inbox → redacted detail → release/trace context → triage → expectation → candidate.
- `anonymous issue cannot auto-promote` gate를 붉은 diamond로 표시.

### 07 Improvement & Release

프레임 `IMP-LOOP-01`:

- Observe → Triage → Freeze/Artifact Hold → Expectation → Baseline fail → Candidate → Evaluate → Canary → Qualify → Publish → Manual install → Monitor → Close/Rollback.
- 각 단계에 입력, 산출물, blocker를 붙인다.

프레임 `IMP-COMPARE-02`:

- 왼쪽 baseline software/local pair, 오른쪽 candidate software/local pair.
- 가운데 software diff와 local-runtime diff를 분리한 intentional diff.
- 아래 동일 suite revision과 correctness/citation/latency/cost/health 결과.

프레임 `IMP-ROLLBACK-03`:

- software lane: current installer → previous compatible installer + local DB migration recovery.
- retrieval lane: current LocalRuntimeRevision → local publication predecessor compare-and-swap.
- 중앙 PromotionRecord는 승인/게시 상태만 바꾸며 공개 설치본을 강제 활성화하지 않는다고 표시한다.

### 08 Operator Console

Desktop frame 기준 1440 px wireframe 제안:

```text
+---------------------------------------------------------------+
| Software | Local runtime cohort | Ingest rev | Time | Alerts    |
+---------------+---------------+-------------------------------+
| Failure rate  | No-result     | p50/p95 latency | Quarantine  |
+---------------+---------------+-----------------+-------------+
| Release comparison chart                                     |
+--------------------------------------+------------------------+
| Error / Issue list                   | Selected detail        |
| code, count, manifest, status        | trace + provenance     |
|                                      | consent + actions      |
+--------------------------------------+------------------------+
```

별도 frame:

- Release Health
- Error Explorer
- Issue Inbox/Detail
- Ingest Health
- Evaluation Compare
- Promotion Checklist
- Retention/Delete Audit

모든 aggregate에 time range, software manifest, local-runtime cohort, ingest revision, sample count, `insufficient_data`, quarantine/unverified 제외 여부를 표시한다.

### 09 Decisions & Handoff

- 의사결정 카드: 상태, 검토안, 예상 영향, 대체 관계. 승인 전에는 ADR로 표시하지 않는다.
- Open decision board: ID, owner, gate, due state.
- Engineering work package board: WP-0~7, dependency, implementation/test/evidence links.
- Traceability matrix: Requirement → Frame → Code/DDL → Test → Operational evidence.

## 6. Product와 Engineering layer 운영

Figma page 안에서 layer 또는 section을 다음처럼 분리한다.

- `O — Overview`: 처음 읽는 사람이 전체 의미와 주요 관계를 이해하는 요약.
- `P — Product Contract`: 사용자 의미, 정책, 상태 이름, acceptance criteria.
- `E — Engineering Detail`: 실제 service/table/function, retry, migration, constraint.
- `V — Verification`: test scenario, run/evidence link.

`O`는 `P/E/V`의 내용을 다시 정의하지 않고 읽는 경로를 제공한다. 개발자는 `P` 요소를 분리해 임의 수정하지 않는다. 구현 제약이 제품 의미에 영향을 주면 댓글과 기술 검토 항목·의사결정 검토안 링크를 남기고 기획 문서와 Figma를 같은 변경 단위로 갱신한다.

## 7. 동기화 규칙

1. 탐색 단계에서는 FigJam 또는 Figma `Draft`에서 흐름과 관계를 먼저 구성할 수 있다.
2. 리뷰할 의미가 정해지면 requirement ID, owner, acceptance criteria를 Git 기획 문서에 기록하고 doc version을 올린다.
3. Figma Frame ID와 Git requirement ID를 연결하고 commit hash를 Spec Badge에 기록한다.
4. Product는 Figma에서 전체 의미와 이해 가능성을, Engineering은 Git contract와 `E` layer에서 구현 가능성을 review한다.
5. 의미를 바꾸지 않는 정렬·레이아웃·가독성 개선은 Figma에서 바로 변경할 수 있다. 관계, 상태, 정책, 수용 기준을 바꾸는 수정은 Git 문서와 같은 review cycle에서 처리한다.
6. 승인에는 Figma의 시각적 설명과 Git의 정확한 계약이 서로 일치해야 한다.
7. Engineering이 구현 링크와 test link를 `E/V` layer에 추가한다.
8. 코드 merge 후 frame status를 `Implemented`로 변경한다.
9. 실제 evidence 확인 후에만 `Verified`로 변경한다.
10. 문서와 Figma가 다르면 Git의 승인 문서/contract를 우선하고 drift issue를 연다. drift 상태인 프레임은 `Approved` 또는 `Verified`로 표시하지 않는다.

Figma 댓글은 검토 대화다. 댓글에서 합의된 내용은 해당 프레임과 Git 문서에 반영한 뒤 해결 처리한다. 운영판에서 승인된 중요한 구조 결정은 운영판 저장소의 ADR에도 반영한다. 댓글만 남아 있는 결정을 운영 계약으로 사용하지 않는다.

## 8. Figma 리뷰 체크리스트

- `00 Start Here`에서 전체 영역과 권장 읽기 순서를 이해할 수 있는가?
- 각 Page의 Overview가 30초 안에 핵심 질문과 주요 관계를 설명하는가?
- 각 Detail Frame이 하나의 질문 또는 리뷰 단위에 집중하는가?
- 긴 문단을 복사하기보다 상태·경계·흐름·관계로 표현했는가?
- Overview에서 Detail로, Detail에서 정확한 Git section으로 이동할 수 있는가?
- public user와 single operator가 다른 actor로 보이는가?
- local authoritative store와 remote observability copy가 구분되는가?
- Supabase 장애가 core flow를 막지 않는 점선 경로가 보이는가?
- publishable key/installation ID가 credential처럼 표시되지 않았는가?
- direct table access와 secret embedding 금지가 표시되는가?
- raw content 자동 수집 금지와 explicit consent gate가 보이는가?
- SoftwareReleaseManifest와 설치별 LocalRuntimeRevision, server IngestDeploymentManifest가 분리되어 있는가?
- base snapshot과 active delta chain이 composite revision에 포함되는가?
- write epoch와 delete-only/zero-vector segment가 nullable artifact 때문에 누락되지 않는가?
- artifact/promotion/issue lifecycle을 서로 섞지 않았는가?
- anonymous issue에서 evaluation suite로 가는 수동 승인 gate가 있는가?
- promotion, publish, install, local activation이 구분되고 software/local retrieval rollback이 각 lane에 있는가?
- anonymous evaluation observation이 qualifying evidence로 연결되지 않는가?
- unresolved issue/evaluation artifact hold가 GC를 막는가?
- 모든 중요한 node에 requirement ID와 문서 링크가 있는가?
- TBD를 구현된 상태처럼 표현하지 않았는가?

## 9. 안티패턴

- Git 문서의 긴 문단을 텍스트 박스로 그대로 복사한다.
- field/type/default/DDL을 Figma에 별도 원본처럼 유지한다.
- 한 프레임에 전체 시스템과 모든 예외를 겹쳐 그린다.
- 색상만으로 상태·권위·위험을 구분한다.
- requirement ID가 없는 새 상태나 정책을 그림에서 먼저 승인한다.
- comment에서 합의한 결정을 프레임이나 Git 문서에 반영하지 않는다.
- `Ready for dev`, `Approved`, `Implemented`, `Verified`를 같은 상태로 취급한다.
- 오래된 프레임을 상태 표시 없이 그대로 남겨 현재 기획처럼 보이게 한다.

## 10. 완료 기준

Figma 초안 완료는 화면이 예쁘다는 의미가 아니라 다음 두 가지가 모두 확보됐다는 의미다.

첫째, 처음 보는 사람이 Git 문서를 먼저 읽지 않고도 주요 actor, 경계, 데이터 계보, 상태 변화와 승인 gate를 설명할 수 있어야 한다.

둘째, 더 정확한 확인이 필요할 때 다음 추적 경로가 끊기지 않아야 한다.

```text
Product requirement
 -> Figma frame/node
 -> Engineering specification
 -> Code/DDL
 -> Test
 -> Production evidence
```

링크가 끊기거나 문서 version/commit이 없는 frame은 review 참고 자료일 수는 있지만 승인된 production 계약으로 보지 않는다.

최소 완료 조건:

- `00 Start Here`에서 모든 Page Overview로 이동할 수 있다.
- 각 Page에 한 화면 Overview와 필요한 Detail Frame이 구분되어 있다.
- 모든 review 대상 프레임에 Frame ID, 상태, requirement ID, 문서 version, commit이 있다.
- 화살표와 상태 전이에 actor, guard, evidence 중 필요한 정보가 표시되어 있다.
- 색상 없이 label만 보아도 권위·상태·금지 경로를 구분할 수 있다.
- Figma와 Git의 의미가 일치하고 open drift가 없다.
