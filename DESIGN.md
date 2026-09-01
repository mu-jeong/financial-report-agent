# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-08-30
- Primary product surfaces: Streamlit chat, per-answer diagnostics, authenticated operator Monitoring, improvement experiments, data update controls
- Evidence reviewed: `README.md`, `docs/operations/MONITORING.md`, `docs/architecture/ARCHITECTURE.md`, `.omx/plans/user-feedback-error-improvement-loop-2026-07-26.md`, `apps/gui/app.py`, `apps/gui/sidebar_views.py`, `apps/gui/monitoring_views.py`, `apps/gui/operator_monitoring_views.py`, `apps/gui/data_views.py`, `src/core/monitoring.py`, `src/core/fixed_snapshot.py`, `src/core/operator_monitoring_service.py`, `src/nodes/vectordb_comparison.py`, `tests/test_gui_view_contracts.py`

## Brand

- Personality: calm, evidence-led, operational, and precise
- Trust signals: explicit measurement boundaries, source provenance, honest empty states such as `측정 전`, and visible failure details
- Avoid: decorative dashboards, unexplained technical IDs, implied investment advice, and controls that appear to complete an operation they only propose

## Product goals

- Goals: answer questions from indexed finance reports; make retrieval health, answer quality, and improvement work observable to a local operator
- Non-goals: investment recommendations, public multi-tenant administration, and presenting unapproved evaluation data as production quality
- Success signals: operators can distinguish live health from experiments, locate a problem without scanning unrelated tools, and understand when a metric is not yet measured

## Personas and jobs

- Primary personas: a local Finance LLM operator/developer and a technically comfortable finance researcher
- User jobs: ask evidence-backed questions, update report data, diagnose failed or weak answers, verify search readiness, and compare candidate improvements safely
- Key contexts of use: local Windows desktop, wide browser layout, long-running data preparation, and intermittent diagnostic work

## Information architecture

- Primary navigation: when the operator surface is enabled, the sidebar exposes `Chat` / `Monitoring` / `개선 실험`; the Chat workspace contains only `Chat` / `개별 Chat Monitoring`, while `개선 실험` is available only as its independent sidebar route
- Core routes/screens: `Monitoring` owns the authenticated issue-reproduction workflow; `개선 실험` owns local change experiments and currently exposes only PDF parsing-engine comparison
- Content hierarchy:
  - `Chat > 개별 Chat Monitoring`: 응답을 먼저 선택한 뒤 좌측에는 실행 당시 저장한 versioned graph manifest와 NodeRun으로 구성한 실행 그래프를, 우측에는 선택 답변의 전체 지표를 표시한다. 실행 노드와 실행되지 않은 조건부 분기를 구분하고, 좌측 노드를 선택하면 우측을 해당 NodeRun·edge·연결된 단계 근거로 전환한다. graph snapshot이 없는 기존 응답만 6단계 호환 그래프를 사용한다.
  - `Monitoring`: `작업함`에서 신고를 선택하고, `재현 케이스`에서 Fixture와 사람이 확인한 문서 범위로 FixedSnapshot을 만들며, `버전 비교`에서 같은 Case의 Baseline과 Candidate를 정성 비교한다.
  - `Monitoring > 작업함`: 새 신고는 `미확인`, 운영자가 확인을 시작한 신고는 `조치 중`, 종결한 신고는 `해결됨` 또는 `이슈 아님`으로 명확히 분리한다. 각 상태의 건수를 먼저 보여주고 상태 필터와 감사 가능한 사유 기반 전환을 제공한다. 기존 `CLOSED` 기록은 의미를 추정하지 않고 `종료(미분류)`로 남겨 재분류할 수 있게 한다.
  - `개선 실험`: 현재는 같은 PDF 표본을 여러 파싱 엔진으로 실행하고 추출 품질 지표와 산출물을 비교한다. 정확도 평가나 운영 진단은 이 화면에 함께 노출하지 않는다.
  - `재현 케이스 > Snapshot 범위`: `신고 당시 사용` 문서는 필수 근거로 먼저 보여주고, `같은 조건 제안`은 운영자가 제외할 수 있으며, 활성 카탈로그 검색으로 `운영자 추가` 문서를 보완한다. 기술 식별자는 상세정보로 내린다.
  - 노출되지 않은 로컬 진단 helper의 internal area IDs는 기존 데이터 계약 호환을 위해 `summary`, `response`, `search_data`, `evaluation`, `parsing`, `issues`로 유지한다.

## Design principles

- Separate observation from intervention: live operational evidence and change experiments must never look like one undifferentiated menu
- Render only the selected diagnostic surface: hidden experiments and large trace views must not add work to the default dashboard
- Preserve honest states: unavailable, empty, warning, failure, and unmeasured are distinct outcomes
- Optimize for a decision: per-answer Monitoring must answer whether the requested targets were searched, whether evidence reached the answer, and where measured time was spent before exposing implementation state.
- Keep diagnostic evidence without promoting it to a KPI: state snapshots, search-k internals, hashes, revisions, and raw response diffs remain available only in collapsed technical details.
- Keep the Chat graph honest: render each response from its persisted `graph_schema_version`, `graph_manifest`, and `node_runs`; never substitute the current source graph for a historical answer or infer an unrecorded NodeRun, edge, status, or latency. Keep the six-stage projection only as a legacy fallback.
- Present human identity before machine identity: report title, date, target, broker, type, and file name lead document selection; full UID and hashes remain available for lineage and version control without becoming the primary label.
- Make inclusion reasons reviewable: distinguish mandatory observed evidence, filter-matched suggestions, and operator additions before creating an immutable FixedSnapshot.
- Separate work progress from outcome: `미확인`과 `조치 중`은 현재 작업 상태이고, `해결됨`과 `이슈 아님`은 서로 다른 종결 결과다. 포괄적인 `open`/`closed` 문구로 이 차이를 숨기지 않는다.
- Tradeoffs: use horizontal segmented controls as tab navigation because Streamlit tabs eagerly compute hidden panels; operational responsiveness takes priority over literal tab-container semantics

## Visual language

- Color: inherit Streamlit theme; reserve success/warning/error colors for semantic state
- Typography: inherit Streamlit typography with short Korean labels and readable technical details
- Spacing/layout rhythm: metrics first, one compact navigation band, then one selected content panel
- Shape/radius/elevation: reuse Streamlit controls and the existing compact rounded sidebar/button treatment
- Motion: framework-default transitions only; no decorative animation
- Imagery/iconography: no imagery is required for Monitoring; icons must not be the sole carrier of status

## Components

- Existing components to reuse: `st.columns`, `st.container`, `st.button`, `st.metric`, `st.segmented_control`, `st.expander`, `st.dataframe`, `st.caption`, `st.warning`, `st.error`, and existing render helpers in `apps/gui/monitoring_views.py`
- New/changed components: a responsive two-pane `개별 Chat Monitoring` workspace with clickable graph-node buttons on the left and overall/node-specific evidence on the right; top-level `개선 실험` route reusing the existing PDF parsing comparison form; authenticated operator Monitoring uses lifecycle count cards and explicit issue-state transitions in the work inbox plus a metadata-first Snapshot scope review with required evidence, editable suggestions, bounded catalog search, and a final selected-document table; Baseline/Candidate actions show an in-place stage status and step progress while the current request runs, followed by the durable latest Run summary and expandable recent results
- Variants and states: top-level navigation defaults to `Chat`; each selected answer defaults to `전체 지표`, preserves its own selected graph node in session state, and keeps the latest parsing comparison result separately; issue states are `OPEN` (`미확인`), `IN_PROGRESS` (`조치 중`), `RESOLVED` (`해결됨`), and `NOT_ISSUE` (`이슈 아님`), while legacy `CLOSED` is read-only-labelled `종료(미분류)` until reopened or categorized
- Token/component ownership: use Streamlit theme and existing app-level CSS; do not add a parallel design-token layer

## Accessibility

- Target standard: practical WCAG 2.1 AA behavior within Streamlit's supported semantics
- Keyboard/focus behavior: all navigation and actions remain native Streamlit widgets with visible focus and deterministic order
- Contrast/readability: use theme semantic states and text labels; never rely on color alone
- Screen-reader semantics: navigation controls keep visible or accessible labels and each selected panel has a descriptive heading/caption
- Reduced motion and sensory considerations: no custom animation or flashing status

## Responsive behavior

- Supported breakpoints/devices: desktop-first wide layout; narrow browser windows remain usable through Streamlit wrapping
- Layout adaptations: horizontal navigation may wrap; the Chat Monitoring graph/detail columns stack graph-first on narrow widths; metrics use existing responsive columns; data tables retain horizontal scrolling
- Touch/hover differences: controls must remain understandable without hover-only help

## Interaction states

- Loading: long operations use existing spinners; Baseline/Candidate execution instead exposes the actual completed stage (`사전 점검`, `자산 검증`, `대기열 등록`, `모델 실행`, `결과 검증`, `결과 저장`) with step-based progress and never estimates elapsed-time completion; passive status reads should complete promptly or show an explicit unavailable/error state
- Empty: show `측정 전`, `없습니다`, or the next preparation step instead of a numeric zero with ambiguous meaning
- Partial measurement: show the values that were actually recorded and state which stages remain unmeasured; never render missing timing or search-k values as zero or use them to identify a bottleneck.
- Graph selection: every persisted stage remains selectable even when unmeasured; the right panel explains the missing evidence instead of fabricating a detail or silently falling back to overall metrics.
- Error: keep the rest of Monitoring navigable and expose a concise error plus optional technical details; a failed Run keeps its status, timestamps, exception type, and stored message visible in the same result surface
- Success: use success messages only for verified health or completed persisted actions; Run completion is confirmed from the persisted terminal record before the result summary is shown
- Disabled: explain unmet prerequisites near the disabled action
- Snapshot selection: show proposal failure without hiding manual catalog search; keep observed evidence selected, preserve operator choices across reruns, and state when search results are truncated. Apply add/remove/reset actions before the click rerun renders, keep the preparation panel open with explicit feedback, and distinguish a session-only scope change from the separate persisted `FixedSnapshot READY` registration.
- Issue transition: every transition requires an operator reason and optimistic revision check; active issues may move between `미확인` and `조치 중` or end as `해결됨`/`이슈 아님`, terminal issues can be reopened, and legacy unclassified closures can be categorized without inventing their past outcome.
- Offline/slow network, if applicable: local status and stored traces remain readable; provider-backed experiments surface provider failure without changing approved state

## Content voice

- Tone: concise Korean operator language; technical English is limited to stable product terms and artifact IDs
- Terminology: use `Monitoring`, `개선 실험`, `개별 Chat Monitoring`, `전체 지표`, `미확인`, `조치 중`, `해결됨`, `이슈 아님`, `종료(미분류)`, `측정 전`, and Native V2 consistently
- Microcopy rules: say what is measured, the sample boundary, and the next action; do not claim accuracy from structural evidence alone; label Snapshot rows as `신고 당시 사용`, `같은 조건 제안`, or `운영자 추가` instead of exposing an unexplained hash list

## Implementation constraints

- Framework/styling system: Python 3.10+, Streamlit 1.54.0, app-level inline CSS only where Streamlit primitives cannot express an existing pattern
- Design-token constraints: inherit Streamlit theme and existing styles; no new dependency or design-system abstraction
- Performance constraints: avoid eager rendering of hidden diagnostics; reuse one Native V2 status snapshot per global Monitoring render; reuse metadata from one active publication for Snapshot proposal, review, and search, invalidating it when the catalog/WAL/index identity changes; on the current local dataset target at most 5 seconds for a cold passive status read and 3 seconds for a repeat read
- Per-answer monitoring constraints: render only persisted compact metadata and never rerun retrieval, rerank, generation, or provider calls; identify `Send` only from persisted execution-mode evidence, not from target count or source count inference.
- Chat graph constraints: the left graph is rendered from the selected response's durable GraphManifest and NodeRun snapshot; node selection changes presentation state only and must not import or invoke `src.graphs.main_graph`. Stored edges describe topology only until edge traversal telemetry is introduced.
- Compatibility constraints: the three-route operator surface remains gated by `OPERATOR_MONITORING_ENABLED`; stable internal area IDs and existing data contracts are preserved; existing `OPEN` remains `미확인`, existing `CLOSED` is not silently treated as resolved and remains accessible as `종료(미분류)` but cannot be created by a new transition; Supabase is the sole production Issue lifecycle/audit authority, while the local registry retains reproduction assets without mirroring that state write
- Snapshot scope constraints: read document metadata only from the active Native V2 catalog; keep `report_uid` as the persisted identity; do not copy report contents into UI state; cap each manual-search result set and keep the full UID in collapsed technical details.
- Run visibility constraints: keep granular stage progress request-local and derived from real service boundaries; keep `QUEUED`/`RUNNING`/terminal lifecycle, timestamps, validity, and result artifact as the durable refresh-safe source of truth; do not add mutable progress fields to an immutable terminal Run or infer a percentage from wall-clock time.
- Test/screenshot expectations: verify the Chat Monitoring two-pane default, graph-node selection, per-answer selection isolation, unmeasured state, issue lifecycle filters/counts/transitions, legacy registry migration, targeted monitoring/GUI tests, then the full suite

## Open questions

- [ ] Decide which experiment should follow PDF parsing comparison before adding another control to `개선 실험` / product owner / affects future navigation only
- [ ] Define a portable cross-machine status benchmark fixture before turning the local 5-second cold / 3-second repeat targets into a hard CI gate / engineering / affects performance regression gating
- [ ] Decide whether legacy `종료(미분류)` records need a one-time bulk classification tool after operators review the current volume / product owner / affects migration convenience only
