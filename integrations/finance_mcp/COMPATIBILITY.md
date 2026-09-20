# Upstream compatibility

This integration is maintained on the `mcp` branch as an additive feature. Existing application files, root dependency pins, launchers, and test discovery settings are unchanged.

## Reference revision

- Repository main / branch base: `23d5dd00e97bbd8ba2db631ee75ea1993f082bc1`.
- Implementation environment: Windows, Python 3.10.11, MCP SDK 2.2.0.
- MCP requirements are installed alongside the unchanged root requirements in `integrations/finance_mcp/.venv`.
- No rebase was necessary for the initial implementation: `main`, the branch base, and HEAD had the same commit before adding these files. Future main updates require the checks below.

## Application boundary

Only `upstream_adapter.py` imports `src.*` in production integration code. Tests use their own `tests/upstream_fixture.py` for upstream schema/index construction; they do not import the application's test helpers. The service and transport use integration-owned JSON contracts.

The adapter relies on these public Python names, which are internal application APIs rather than a versioned third-party interface:

| Surface | Assumption to verify after rebase |
|---|---|
| `src.configs.config` | Embedding model and OpenRouter configuration constants remain available. Configuration is loaded once after CLI environment resolution. |
| `src.llms.embeddings.OpenRouterEmbeddings` | Constructor accepts model, key, timeout, application headers, data collection and max_retries; `embed_query` returns a vector. |
| `src.retrieval.bootstrap.inspect_runtime` | Read inspection distinguishes missing/empty/ready Native V2 and validates the active runtime without writer recovery. |
| `src.retrieval.repository.CatalogRepository` | `data_root`, request lifetimes, snapshot leases, close semantics remain compatible. |
| `src.retrieval.repository.compile_scope_filters` | Parameterized predicates use the `report` alias; absent and explicitly empty arrays retain different meanings. |
| `src.retrieval.reader.NativeRetrievalReader.search` | Accepts float32 vector, k, scope; returns revision, strategy, eligibility and hydrated hits. |
| Native revision / retrieved chunk fields | Publication/snapshot/build/profile/delta identities and report/chunk/parent IDs, metadata and text remain available. |

No graph node, GUI helper, private application method, or monkey patch is used. If an upstream interface changes, update the adapter and its tests rather than spreading application imports into the server or service.

## Read-only SQL boundary

Metadata/body reads use SQLite `mode=ro`, `query_only`, and a single read transaction. There is no schema migration or corpus writer in the integration.

- `active_reports`: report_id, report_uid, canonical_relative_path, report_type, report_date, target_name, title, broker. The view must reflect active base **and delta** publications and exclude replaced/deleted reports.
- `active_vector_membership`: active chunk identity for body membership. Indexed parents are selected through their active chunks; historical/inactive parents are excluded.
- `retrieval_parents`: parent_uid, report_id, profile_id, parent_order, content; ordered extracted text is read in bounded slices.
- `retrieval_chunks`: chunk_uid, parent_uid, profile_id; used to associate active membership and parent text.
- `retrieval_runtime`: singleton runtime_id=1, schema_version, publication_generation, active_snapshot_id, active_build_id.
- `retrieval_builds`: build_id, profile_id, state; active build must be fully_complete.
- `embedding_profiles`: profile_id, profile_hash, model, dimension, metric.
- `retrieval_delta_segments`: base_snapshot_id, base_publication_generation, state, sequence; ready segment generation/count contribute to revision identity.

The opaque revision token combines publication, snapshot, build, profile, delta generation and segment count. Both SQL reads and native search must produce the same token for the same visible corpus. Cross-call revision changes yield `STALE_REVISION`; the server does not retain leases across tool calls or claim to serve historical snapshots.

Schema version alone is not a sufficient compatibility test: a view or visibility rule can change without a version bump. Fixture tests cover additions, same-count replacements and deletions in delta publications.

## Transport boundary

The transport uses the SDK's public low-level `Server`, tool-list/tool-call callbacks, MCP types and `stdio_server`. It does not override private handlers or mutate SDK tool managers. Validation and bounded/signed pagination remain in the integration service. Startup tool discovery does not initialize the application adapter.

MCP input/output schema version is 1. Cursors are signed per server instance and bound to tool, filters/document and revision; they expire on restart. Results contain actual relative source paths and indexed text, not fabricated page numbers or PDF completeness claims.

## Verification after each main update

1. Rebase normally after preserving local work. Record the new main SHA. This document is a maintenance procedure, not an automatic destructive git operation.
2. Inspect `git diff --name-status main...HEAD` and `git status --short`. Changes should remain under `integrations/finance_mcp/`. Any necessary upstream exception must be explained and separated from extension commits.
3. Recreate/update only the MCP environment using the two requirements files and run `pip check`.
4. Run the extension tests explicitly. Do not rely on root pytest discovery to include them.
5. Run the unchanged application's fast tests in its own environment and relevant retrieval lifecycle tests when retrieval contracts have changed.
6. Verify stdio initialization, schemas, successful calls, safe errors, Unicode/spaces in paths, and shutdown. Update the evidence below with actual results, not just successful conflict resolution.

## Initial verification evidence

- Existing application fast suite: **1,317 passed, 7 skipped, 5 deselected**. Three existing SWIG deprecation warnings; no failures.
- Dedicated MCP environment: root requirements plus `mcp==2.2.0` installed successfully; `pip check` reports no broken requirements.
- Official SDK client against the actual local corpus: initialized a stdio subprocess, discovered all five tools, read status for **7,095 reports (2026-01-02 through 2026-09-18)**, listed one report and read a 300-character body window using its returned report ID and revision. A continuation cursor was returned and the process shut down successfully. The API key was blank in this test process; no provider request or corpus update was performed.
- Final extension suite: **56 passed** in the dedicated environment (three SWIG deprecation warnings, no failures). Covers contract validation, empty inputs/corpus, signed/revision-bound pagination, delta visibility, bounded body windows, malformed provider responses, profile/index races, and import boundaries.
- Transport evidence: raw JSON-RPC and official SDK clients initialize/list/call/shut down successfully; successful semantic search with a test embedding provider leads to document reads and continuation pages. Two simultaneous server-process clients read independently and reject each other's cursors. Deliberate third-party stdout noise is routed to stderr.
- Syntax/whitespace checks passed for all 12 Python files. `git diff --exit-code` confirmed no modifications to existing tracked application files; all 15 new deliverable files are under `integrations/finance_mcp/`.
- Review: no critical/high issues remained. The identified malformed-provider error classification was corrected and covered for both IndexError and AttributeError before the final test run.
- No lint/typecheck configuration was found in the repository. Syntax/import and architectural boundary checks are included; no new lint/typecheck dependency was installed.
- Paid OpenRouter calls and registration in a particular desktop LLM application's settings are not part of automated fixture verification. Client protocol verification must be distinguished from an end-user desktop session.

## Subsequent live verification (2026-09-19)

The opt-in `live_smoke.py` passed all 13 calls against the actual 7,095-report corpus, including two real OpenRouter searches, body pagination, expected errors, unchanged revision and clean shutdown. The extension suite passed again: **56 passed**.

The server was registered as `finance-research` in the local Codex user configuration. A fresh Codex CLI session successfully called status, performed a real filtered semantic search, and used the returned report ID/revision to read 500 characters. This verifies an actual LLM host in addition to the SDK client; desktop UI interaction was not tested. See [LIVE_TEST.md](LIVE_TEST.md) for evidence, timing, reproduction and limitations.
