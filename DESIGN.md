# Design

## Source of truth
- Status: Draft
- Last refreshed: 2026-07-25
- Primary product surfaces: Streamlit chat, conversation/data controls in the sidebar, and developer-only Monitoring Mode.
- Evidence reviewed: `README.md`, `docs/QUICK_START.md`, `docs/MONITORING.md`, `apps/gui/app.py`, `src/core/chat_ui_helpers.py`, and `apps/gui/examples/example1.png` / `example2.png`.

## Brand
- Personality: Calm, evidence-led, practical, and trustworthy rather than promotional.
- Trust signals: Source citations, linked report documents, explicit retrieval/update status, safe failure messages, and the investment-advice disclaimer.
- Avoid: Unqualified investment recommendations, hidden data state, raw developer diagnostics in the normal chat flow, and decorative “AI demo” styling without a product purpose.

## Product goals
- Goals: Let a local user ask Korean financial-report questions quickly, understand which reports support an answer, and recover when local data is incomplete.
- Goals: Give maintainers a separate diagnostic surface for retrieval quality, evaluation, and issue-to-regression workflows.
- Non-goals: Brokerage execution, personalized investment advice, or exposing chain-of-thought and raw internal metadata to normal users.
- Success signals: Grounded answers with usable citations, visible background-job state, clear empty/error recovery, and reproducible Monitoring results.

## Personas and jobs
- Primary personas: A Korean-speaking local research user and a maintainer improving the RAG pipeline.
- User jobs: Ask report-backed questions, continue a scoped conversation, inspect sources, update local report data, and resume after failures.
- Maintainer jobs: Diagnose one response, compare evaluation runs, inspect data readiness, and promote reproducible issues into regression candidates.
- Key contexts of use: Desktop-first local execution through `RUN_QUICKSTART.bat` / `RUN_APP.bat`; Monitoring Mode is explicitly enabled by developers.

## Information architecture
- Primary navigation: Sidebar conversation list and data controls; Monitoring Mode adds a page selector.
- Core routes/screens: Chat; Chat Monitoring for the selected thread; global operations, evaluation/experiments, and issue/regression monitoring.
- Content hierarchy: Question and answer first, then scoped notices and cited sources; operational detail stays in status or Monitoring surfaces.

## Design principles
- Evidence before confidence: Keep citations, source access, and data readiness close to claims.
- Progressive disclosure: Keep normal chat focused; place dense diagnostics behind Monitoring tabs and expanders.
- Preserve user context: Thread, scope, background-job, and selected-page state must survive Streamlit reruns predictably.
- Fail visibly and safely: Explain recovery actions without exposing secrets, raw paths, or opaque internal exceptions.
- Tradeoffs: Prefer a stable desktop workflow and explicit state over animation or aggressive responsive rearrangement.

## Visual language
- Color: Use Streamlit theme defaults where possible; reserve semantic colors for success, warning, error, selection, and data availability.
- Typography: Korean body and control text should normally render at 14px or larger. Smaller text is limited to dense secondary metadata and still requires readable contrast.
- Spacing/layout rhythm: Use Streamlit spacing primitives and a small number of compact sidebar exceptions.
- Shape/radius/elevation: Moderate rounding; avoid shadows and nested card depth unless hierarchy or interaction requires it.
- Motion: Minimal. Scrolling and reruns must not repeatedly pull the viewport away from the user.
- Imagery/iconography: Functional status and action icons only; emoji may label compact states but must not be the sole meaning carrier.

## Components
- Existing components to reuse: Streamlit chat messages, sidebar controls, tabs, status callouts, metrics, dataframes, dialogs/expanders, and pure helpers in `src/core/chat_ui_helpers.py`.
- New/changed components: Extract only when a stable, testable boundary exists; do not split renderers merely to reduce line count.
- Variants and states: Chat messages cover running, failed, successful, scoped, no-result, and cited-source states.
- Token/component ownership: Prefer Streamlit theme values. Keep unavoidable custom CSS and HTML localized, escaped, and documented near the owning component.

## Accessibility
- Target standard: WCAG 2.1 AA for new or changed user-facing UI; existing gaps should be reduced incrementally.
- Keyboard/focus behavior: All actions remain native buttons/inputs where possible; destructive actions need an explicit confirmation path before broader deployment.
- Contrast/readability: Normal Korean text targets 14px or larger and 4.5:1 contrast; status must not rely on color alone.
- Screen-reader semantics: Dynamic feedback uses appropriate status/live-region semantics and meaningful control labels.
- Reduced motion and sensory considerations: Avoid repeated forced scrolling and nonessential animation.

## Responsive behavior
- Supported breakpoints/devices: Desktop browser is the evidenced primary target; tablet/mobile support is not yet specified.
- Layout adaptations: Dense monitoring tables may scroll horizontally; core chat and primary actions should remain usable at narrower desktop widths.
- Touch/hover differences: Do not make hover the only way to discover or understand an action.

## Interaction states
- Loading: Render the chat shell before initializing the search graph. Show engine preparation near the input, accept at most one pending first question, and process it automatically when preparation completes.
- Loading: Show the running message or job phase without blocking navigation to another conversation.
- Empty: Explain missing data or no-result state and expose only actionable retries.
- Error: Use a safe user message and preserve diagnostic evidence for maintainers.
- Success: Confirm saved reports, completed updates, and evaluation artifacts without interrupting the main flow.
- Disabled: Explain why an action is disabled when the reason is not obvious.
- Offline/slow network: Preserve the current local snapshot and make external-call or extraction failures visible.

## Content voice
- Tone: Concise, factual, calm Korean for normal users.
- Terminology: Keep necessary English technical terms in Monitoring Mode; prefer Korean action and feedback labels in the normal flow.
- Microcopy rules: State what happened and the next useful action; avoid filler, blame, or unsupported certainty.

## Implementation constraints
- Framework/styling system: Python 3.10+, Streamlit 1.54, and inline CSS/HTML only where native Streamlit components are insufficient.
- Design-token constraints: No separate design-system dependency; reuse Streamlit semantics and centralize repeated custom values before expanding them.
- Performance constraints: Reruns should avoid repeated database scans, duplicate file loads, unnecessary forced scroll attempts, and string-valued fragment intervals that trigger pandas solely for duration parsing.
- Compatibility constraints: Preserve widget keys, session-state names, runtime-smoke behavior, Windows-first launchers, and current local data formats.
- Deployment constraint: The background chat-job registry is process-local. Activate code that moves its cached owner only when no answer job is in flight; routine Streamlit reruns keep the stable module-qualified cache key.
- Test/screenshot expectations: Pure view-model/HTML helpers require unit tests; renderer changes require targeted Streamlit/runtime checks, and visible layout changes require before/after screenshots.

## Open questions
- [ ] Product owner: Is mobile or narrow-tablet use supported, or is desktop-only an explicit constraint?
- [ ] Product owner: Is there an approved logo, palette, or typography beyond the existing Streamlit examples?
- [ ] UX owner: Should conversation deletion use an inline confirmation, dialog, or recoverable undo?
- [ ] Maintainer: Which English labels in Monitoring Mode should remain technical terms versus be localized?
- [ ] Maintainer: Establish a screenshot and accessibility baseline for the current Streamlit version before broad component extraction.
