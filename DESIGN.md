# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-08-03
- Primary product surfaces: Streamlit chat, per-answer monitoring, global Monitoring Mode, data update controls
- Evidence reviewed: `README.md`, `docs/MONITORING.md`, `docs/ARCHITECTURE.md`, `apps/gui/app.py`, `apps/gui/sidebar_views.py`, `apps/gui/monitoring_views.py`, `apps/gui/data_views.py`, `tests/test_gui_view_contracts.py`

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

- Primary navigation: sidebar `Chat` / `Monitoring` when `MONITORING_MODE=true`; Chat contains `Chat` / `답변 모니터링`
- Core routes/screens: global Monitoring keeps speed and correctness metrics first, followed by purpose-based horizontal navigation
- Content hierarchy:
  - `운영 모니터링`: `현재 문제`, `응답 원인 확인`, `검색 자료 준비`
  - `성능 개선 실험`: `정확도 평가`, `문서 읽기 품질 비교`, `신고·수정 확인 · 묶음 전 단계`
  - Internal area IDs remain `summary`, `response`, `search_data`, `evaluation`, `parsing`, and `issues`

## Design principles

- Separate observation from intervention: live operational evidence and change experiments must never look like one undifferentiated menu
- Render only the selected diagnostic surface: hidden experiments and large trace views must not add work to the default dashboard
- Preserve honest states: unavailable, empty, warning, failure, and unmeasured are distinct outcomes
- Tradeoffs: use horizontal segmented controls as tab navigation because Streamlit tabs eagerly compute hidden panels; operational responsiveness takes priority over literal tab-container semantics

## Visual language

- Color: inherit Streamlit theme; reserve success/warning/error colors for semantic state
- Typography: inherit Streamlit typography with short Korean labels and readable technical details
- Spacing/layout rhythm: metrics first, one compact navigation band, then one selected content panel
- Shape/radius/elevation: reuse Streamlit controls and the existing compact rounded sidebar/button treatment
- Motion: framework-default transitions only; no decorative animation
- Imagery/iconography: no imagery is required for Monitoring; icons must not be the sole carrier of status

## Components

- Existing components to reuse: `st.metric`, `st.segmented_control`, `st.expander`, `st.dataframe`, `st.caption`, `st.warning`, `st.error`, and existing render helpers in `apps/gui/monitoring_views.py`
- New/changed components: two-level horizontal Monitoring navigation for purpose and area; the single problem-area selectbox is retired
- Variants and states: default to `운영 모니터링 > 현재 문제`; preserve each group's last selected area in separate session-state keys
- Token/component ownership: use Streamlit theme and existing app-level CSS; do not add a parallel design-token layer

## Accessibility

- Target standard: practical WCAG 2.1 AA behavior within Streamlit's supported semantics
- Keyboard/focus behavior: all navigation and actions remain native Streamlit widgets with visible focus and deterministic order
- Contrast/readability: use theme semantic states and text labels; never rely on color alone
- Screen-reader semantics: navigation controls keep visible or accessible labels and each selected panel has a descriptive heading/caption
- Reduced motion and sensory considerations: no custom animation or flashing status

## Responsive behavior

- Supported breakpoints/devices: desktop-first wide layout; narrow browser windows remain usable through Streamlit wrapping
- Layout adaptations: horizontal navigation may wrap; metrics use existing responsive columns; data tables retain horizontal scrolling
- Touch/hover differences: controls must remain understandable without hover-only help

## Interaction states

- Loading: long operations use existing spinners; passive status reads should complete promptly or show an explicit unavailable/error state
- Empty: show `측정 전`, `없습니다`, or the next preparation step instead of a numeric zero with ambiguous meaning
- Error: keep the rest of Monitoring navigable and expose a concise error plus optional technical details
- Success: use success messages only for verified health or completed persisted actions
- Disabled: explain unmet prerequisites near the disabled action
- Offline/slow network, if applicable: local status and stored traces remain readable; provider-backed experiments surface provider failure without changing approved state

## Content voice

- Tone: concise Korean operator language; technical English is limited to stable product terms and artifact IDs
- Terminology: use `운영 모니터링`, `성능 개선 실험`, `현재 문제`, `측정 전`, and Native V2 consistently
- Microcopy rules: say what is measured, the sample boundary, and the next action; do not claim accuracy from structural evidence alone

## Implementation constraints

- Framework/styling system: Python 3.10+, Streamlit 1.54.0, app-level inline CSS only where Streamlit primitives cannot express an existing pattern
- Design-token constraints: inherit Streamlit theme and existing styles; no new dependency or design-system abstraction
- Performance constraints: avoid eager rendering of hidden diagnostics; reuse one Native V2 status snapshot per global Monitoring render; on the current local dataset target at most 5 seconds for a cold passive status read and 3 seconds for a repeat read
- Compatibility constraints: Monitoring remains gated by `MONITORING_MODE`; stable internal area IDs and existing data contracts are preserved
- Test/screenshot expectations: update source/state contracts, run targeted status/monitoring/GUI tests, then verify the live Streamlit navigation and current-problem state

## Open questions

- [ ] Revisit whether `신고·수정 확인` should become its own third group after evaluation-bundle operations are fully implemented / product owner / affects future navigation only
- [ ] Define a portable cross-machine status benchmark fixture before turning the local 5-second cold / 3-second repeat targets into a hard CI gate / engineering / affects performance regression gating
