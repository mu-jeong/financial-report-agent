# V2 native retrieval release certification runbook

This runbook builds developer release-certification evidence on copied data. It
does not authorize mutation of the live V1 installation, a version bump, or a
release commit. Run every command from the repository root with the project
virtual environment activated.

This is a developer release-certification runbook, not the normal user
migration. `MIGRATE_V2.bat` follows the separate
[`V2_MIGRATION_USER.md`](V2_MIGRATION_USER.md) contract: it validates and
directly activates the same converted seed at publication generation 2 and
write epoch 1, with `predecessor=NULL`. It does not build a new full-corpus
successor. The distinct-successor steps below are release-only rehearsals and
must not be presented as part of the one-click path.

## Lifecycle boundary

Everything under `src/migrations/v2`, `scripts/migrations/v2`,
`tests/migrations/v2`, and `docs/migrations/v2` is transition-scoped. Remove
those trees, together with `MIGRATE_V2.bat`, only after V1 support has ended.
The native retrieval engine and its regression tests remain under
`src/retrieval` and `tests/retrieval`.

`src/retrieval/compatibility_bundle.py` is the one runtime-side V1 bridge
contract. At V1 end-of-support, remove it together with the epoch-zero
compatibility branches in `src/retrieval/bootstrap.py` and
`src/nodes/vectordb.py`; permanent retrieval code must never import the
transition package.

## Stop conditions

Stop before conversion or publication if any required artifact, source PDF,
SHA-256 value, profile field, full-corpus member, canary, parity check,
performance gate, or durability check is missing or fails. Never substitute a
guessed digest. Migration evidence is immutable, so use a fresh output path for
each attempt.

The copied V1 input must contain exactly these trusted local artifacts:

- `reports.db`
- `vector_db/index.faiss`
- `vector_db/index.pkl`

Conversion reads those artifacts and caller-supplied JSON only. It does not
read PDFs, crawl, extract, chunk, embed, access the network, or call an API.

## 1. Capture and assess a copied installation

```powershell
$Run = "reports/v2_migration/run-YYYYMMDD-HHMMSS"
$Copied = "$Run/copied-v1"
$DataRoot = "$Run/data-root"
New-Item -ItemType Directory -Force -Path $Run | Out-Null

python scripts/migrations/v2/migrate_v2_native.py copy `
  --source-root data `
  --destination-root $Copied `
  --output "$Run/copy.json"
```

Create `expected-hashes.json` from the three files in `$Copied`, not from a
different installation. Its keys are the relative paths above and its values
are lowercase SHA-256 digests. Re-hash the original V1 files after every stage
and require them to match the pre-copy values.

Optional `provenance.json` fields are `model`, `model_revision`,
`normalization`, `library_version`, and `same_space_attested`. Unknown facts
must remain null or false.

```powershell
python scripts/migrations/v2/migrate_v2_native.py assess `
  --copied-root $Copied `
  --expected-hashes "$Run/expected-hashes.json" `
  --provenance "$Run/provenance.json" `
  --output "$Run/assessment.json"
```

Assessment must report the same symbolic `N` for the legacy mapping and FAISS
`ntotal`. Any uncertainty remains explicit evidence and must be resolved by the
same-space canary before writable seed activation.

## 2. Seal the epoch-zero compatibility bundle

```powershell
python scripts/migrations/v2/migrate_v2_native.py seal `
  --copied-root $Copied `
  --data-root $DataRoot `
  --output "$Run/bundle.json"
```

Record `bundle.bundle_id` from `bundle.json`. The sealed files under
`retrieval/compat/v1/<bundle_id>` are read-only and selectable only while the
catalog is at write epoch 0 with V1 fallback open.

## 3. Convert existing vectors without embedding

Prepare these reviewed inputs:

- `source-hashes.json`: V1 report filename to exact source-PDF SHA-256. Every
  report must be present; a missing PDF blocks conversion.
- `canonical-paths.json` (optional): V1 report filename to normalized relative
  source path. Absolute paths and traversal are invalid.
- `profile.json`: `model`, `dimension`, `metric`, `normalization`,
  `prefix_template`, `extractor`, `parent_policy`, and `child_policy`. The
  dimension and metric must match the observed V1 artifact.

```powershell
$BundleId = (Get-Content "$Run/bundle.json" -Raw | ConvertFrom-Json).bundle.bundle_id
# Omit --canonical-paths when no reviewed optional file was created.
python scripts/migrations/v2/migrate_v2_native.py convert `
  --copied-root $Copied `
  --data-root $DataRoot `
  --expected-hashes "$Run/expected-hashes.json" `
  --source-hashes "$Run/source-hashes.json" `
  --canonical-paths "$Run/canonical-paths.json" `
  --profile "$Run/profile.json" `
  --provenance "$Run/provenance.json" `
  --bundle-id $BundleId `
  --output "$Run/conversion.json"

python scripts/migrations/v2/migrate_v2_native.py validate `
  --data-root $DataRoot `
  --conversion-result "$Run/conversion.json" `
  --output "$Run/validation.json"
```

Require full-N catalog/membership/raw-FAISS equality, valid spans and IDs,
vector parity, SQLite integrity and foreign keys, an immutable active seed at
epoch 0, and unchanged copied V1 hashes.

## 4. Run copied-install reader parity at epoch zero

Use the same opaque query-vector file prepared for the performance gate. Create
an independent `reader-parity-scopes.json` with this shape:

```json
{
  "schema_version": 1,
  "kind": "v2_reader_parity_scopes",
  "workloads": {
    "unfiltered": {"scope": null},
    "company": {"scope": {"company": true}},
    "report_type": {"scope": {"report_type": "company"}},
    "date": {"scope": {"report_date_start": "YYYY-MM-DD"}},
    "narrow": {"scope": {"file_name": "one-reviewed-report.pdf"}},
    "broad": {"scope": {"report_date_start": "YYYY-MM-DD"}},
    "empty": {"scope": {"empty": true}},
    "prior_scope": {
      "scope": {"prior_scope": {"file_name": "one-reviewed-report.pdf"}}
    }
  }
}
```

Choose the company, type, date, narrow, broad, and prior scopes from the copied
corpus so they exercise both populated and fewer-than-`k` cases. The CLI
validates the sealed conversion manifest and legacy mapping against the active
catalog before opening either reader. It retains only input and artifact
hashes, snapshot identity, counts, and aggregate mismatch summaries; it does
not retain paths, vectors, queries, or report content.

```powershell
python scripts/migrations/v2/run_v2_reader_parity.py `
  --data-root $DataRoot `
  --query-input "$Run/benchmark-input.json" `
  --scope-input "$Run/reader-parity-scopes.json" `
  --output "$Run/reader-parity.json"
```

The raw V1 ordinal order for an exact-score tie is an `index.pkl` storage
artifact, not logical rank semantics. Before applying top `k`, this gate keeps
unequal-score ordering unchanged and orders only exact-score tie groups by
`chunk_uid`: ascending score then `chunk_uid` for L2, descending score then
`chunk_uid` for inner product. Native physical IDs are assigned in the same
canonical `chunk_uid` order. Any unequal-score, logical chunk, source, body,
citation, eligible-set, snapshot, or generation mismatch still fails.

The output path must not exist. A passing or failing evidence file is written
once and made read-only; use a new path for every rerun.

## 5. Run the copied-install performance gate at epoch zero

Run this gate against the validated epoch-zero release fixture before direct seed
activation or any distinct successor. The copied-install adapter deliberately
requires an epoch-zero seed with V1 fallback open, so reusing a data root after
activation is invalid. If benchmark preparation must continue in parallel,
preserve an immutable clone of the validated epoch-zero data root and benchmark
only that clone.

Prepare at least 30 fixed opaque query IDs and vectors. The input must define
`unfiltered`, `empty`, `narrow`, `broad`, `near_universe`, and `prior_scope`
workloads. Do not retain query text. Each workload definition contains only
`scope`: `null` for `unfiltered` and a non-empty object for every filtered
workload. Caller-supplied `v1_fetch_k` is rejected; the adapter derives the
production baseline (`k` when unfiltered, full `N` before post-search filtering
otherwise).

```powershell
$DataRootResolved = (Resolve-Path $DataRoot).Path
$env:V2_BENCHMARK_DATA_ROOT = $DataRootResolved
$env:V2_BENCHMARK_INPUT = (Resolve-Path "$Run/benchmark-input.json").Path
python scripts/migrations/v2/run_v2_retrieval_benchmark.py `
  --factory src.migrations.v2.validation.copied_install_benchmark:create_factory `
  --protocol-profile epoch_zero `
  --processes 3 --warmups 10 --samples 200 `
  --bootstrap-resamples 10000 `
  --output "$Run/benchmark.json"
```

Passing requires the declared paired-bootstrap p95 limit, every process limit,
complete telemetry, and separate retrieval-quality parity. A synthetic probe is
only a runner test and is not release evidence. Raw evidence retains each fresh
process's exact seed plus a separate cold-start record containing factory
initialization time/memory and the first unfiltered probe. V1 and V2 cold records
run in separate fresh engine-isolated workers, use the same opaque query ID and
seed, and retain `factory_init_ns + probe.total_ns` as the comparable total.
Cold data is reported separately and is not folded into the warm p95 gate.

Direct seed activation closes V1 fallback but leaves `predecessor=NULL`. Run the
additional native predecessor-versus-successor regression gate only after a
distinct healthy successor has been published. The factory first validates the
healthy live snapshot pair, then pins both immutable revisions through one
checkpointed read-only catalog clone. This avoids mutating the runtime pointer
or comparing the active WAL against a different filesystem cache policy. The
environment retained in the artifact must name both snapshot IDs, their hashes
and counts, and
`catalog_policy=shared_checkpointed_catalog_clone_pinned_revisions`.

```powershell
$env:V2_BENCHMARK_DATA_ROOT = (Resolve-Path $DataRoot).Path
$env:V2_BENCHMARK_INPUT = (Resolve-Path "$Run/benchmark-input.json").Path
python scripts/migrations/v2/run_v2_retrieval_benchmark.py `
  --factory src.migrations.v2.validation.copied_install_benchmark:create_successor_factory `
  --protocol-profile successor_release `
  --processes 3 --warmups 10 --samples 4000 `
  --bootstrap-resamples 10000 `
  --output "$Run/successor-predecessor-benchmark.json"
```

The same 1.10 confidence-interval and 1.15 per-process caps apply. Retain failed
runs as diagnostic evidence, but only a new, passing, read-only artifact may be
used by the aggregate release gate.

## 6. Release-only distinct successor rehearsal under launcher race

This section is not part of `MIGRATE_V2.bat`. The release fixture intentionally
creates a distinct successor so launcher races, predecessor recovery, and
post-successor performance can be certified. Place the complete source corpus
in the configured `SAVE_DIR`; it must include
all converted reports and at least one genuinely new logical report. Prepare two
independent installs of the same reviewed code: the source checkout and the
packaged/default installation. They must be distinct directories and each must
contain its own `.venv\Scripts\python.exe`.

`reports.db` is only the legacy-compatible anchor for a native data root. It may
not exist after conversion; do not create a placeholder and do not use
`Resolve-Path` on that file. The release execution surface captures an
epoch-zero installed-launcher baseline, then holds one writer lock from startup
reconciliation through planning, materialization, the concurrent transition
wave, and publication:

`retrieval/v2/writer.guard` is the persistent OS-lock anchor. Its descriptor is
held for the entire writer run, so operators must not delete or replace the file
while any launcher is active. `retrieval/v2/writer.lock` is the transient,
nonce-bearing owner record. It is published only after a complete temporary
record is file-synced; malformed records fail closed, and only recovery that can
prove the recorded process identity stale may quarantine one. Do not remove
either file manually to bypass a blocked writer.

```powershell
$DataRootResolved = (Resolve-Path $DataRoot).Path
$SourceInstall = (Resolve-Path ".").Path
$PackagedInstall = (Resolve-Path "C:\path\to\packaged-install").Path
$env:DB_PATH = Join-Path $DataRootResolved "reports.db"
$env:SAVE_DIR = (Resolve-Path "$Run/source-pdfs").Path
python scripts/migrations/v2/run_v2_first_successor_race.py `
  --source-install $SourceInstall `
  --packaged-install $PackagedInstall `
  --workers 6 --timeout-seconds 60 `
  --output "$Run/first-successor-race.json"
```

The transition wave runs all six supported launcher surfaces plus the installed
`--write` updater guard from both installs. A process may select only the exact
epoch-zero or exact successor identity, or fail closed while the writer lock is
held. The updater may never become writable on the epoch-zero identity. The
source and packaged launcher-layout hashes must match and remain unchanged.

The release-only race fixture validates the same-space canary, builds a distinct
complete corpus off path, reopens and verifies the raw FAISS snapshot, and
publishes it with the converted seed as predecessor. This is different from the
normal writer. `python -m src.core.embed_pipeline --all` now dispatches to
`execute_incremental_update`: it scans the complete source inventory, parses and
embeds only new or changed PDFs, reuses unchanged vectors, reflects deletions,
and publishes nothing when no source changed. It does not produce the mandatory
installed race evidence for release certification.

PDF extraction retains the declared candidate policy. The default primary is
PyMuPDF and the shipping environment template explicitly selects OpenDataLoader
as fallback; an absent or blank fallback setting disables it. A distinct
`UNEMBEDDED_PDF_EXTRACTION_ENGINE` does not silently fall back. The successor
still includes and chunks a successfully extracted report. V2 fingerprints the
DB-visible policy as `<requested-engine>|fallback=<fallback-engine>`, while an
undeclared engine transition continues to fail closed. Parent/child split sizes,
overlap, Markdown headers, and embedding prefixes remain the V1 values;
deterministic identities and child spans are V2 storage metadata rather than
text-processing changes.

## 7. Post-successor launcher matrix

This gate requires the healthy active+predecessor pair created by the
release-only distinct-successor rehearsal. The direct one-click activation
state alone does not satisfy it.

Run the launcher matrix as a non-admin Windows user with source/default,
packaged/default, and a custom data root containing spaces and Korean
characters. Use three distinct copies of the same completed post-successor data
root. Each `--case` points to a legacy-compatible `reports.db` anchor; the
anchor file may be absent, but the sibling `retrieval/v2/catalog.sqlite3` must
exist. The source and packaged labels use their corresponding independent
install roots; the custom-local case may reuse the source install root:

```powershell
python scripts/migrations/v2/run_v2_launcher_matrix.py `
  --case source-default=C:\path\to\source\reports.db `
  --case packaged-default=C:\path\to\package\reports.db `
  --case custom-local="C:\테스트 경로\reports.db" `
  --install-root source-default=$SourceInstall `
  --install-root packaged-default=$PackagedInstall `
  --install-root custom-local=$SourceInstall `
  --require-non-admin `
  --output "$Run/launcher-matrix.json"
```

The matrix fails unless every case has an existing native catalog; every
surface emits one consistent structured runtime identity; the runtime is
native, epoch-positive, fallback-closed, non-degraded, and writable; the
catalog remains byte-unchanged; and all three runtime identities and catalog
hashes agree. A legacy, epoch-zero, missing, administrator-run, or single-case
fixture cannot produce passing release evidence. Launcher evidence generated
without case-specific `--install-root` values or before the first-successor race
gate is synthetic/superseded and must not be promoted as release evidence.

## 8. Recovery, support evidence, and release gate

Exercise every declared crash boundary, active corruption, predecessor
fallback, forward recovery, open-handle/lease behavior, PermissionError retry,
writer-lock contention, and catalog/WAL restore case. Export only redacted
support data:

```powershell
python -m src.retrieval.support_export `
  --db-path "$DataRoot/reports.db" `
  --data-root $DataRoot `
  "$Run/support.json"
```

Create the Gate D query artifact once, before release transitions. This is the
only release-query step that calls the embedding provider to vectorize the Gate
D query; migration canaries and release-only successor builds may also call the
provider. It uses `search_query`, validates
the actual rank-one citation before writing, records the text/vector
attestation, and makes the output read-only. Never regenerate a retained query
merely to obtain a different result.

```powershell
python scripts/migrations/v2/create_v2_release_query.py `
  --data-root $DataRoot `
  --query-id skt-2q26-recovery-gate-d `
  --query-text "<natural-language recovery query>" `
  --expected-report-uid a58d1c5692ee6daface30166b1f08ff483cfd2f5e1c7aee6ace1d4a90fb5541f `
  --output "$Run/gate-d-query.json"
```

Run one installed validation against a fresh dedicated copy of the healthy
post-successor root with an active+predecessor pair, never the protected source
installation and never the direct-activation-only state. The runner
performs and seals one continuous transition chain: active corruption,
predecessor recovery, zero-provider-call forward recovery, publication blocked
by a held lease, lease release, successor publication, retired-predecessor GC,
and Gate D. It then runs exactly four launcher guards (source/package by
read/write) and one semantic probe in each install. There is no duration,
scheduler, heartbeat, mutable state, or alternate bypass path.

Place both new evidence outputs in a separate validation-run directory outside
the dedicated/protected data roots, PDF corpus, and both install roots. The
runner rejects containment and symlink/junction parents. It rechecks install
layouts, interpreter/dependency fingerprints, query and transition hashes,
protected/source trees, and runtime/catalog/snapshots after both probes before
publishing immutable evidence.

```powershell
$ValidationRun = "C:\path\outside\all-input-and-install-roots\fresh-validation-run"
python scripts/migrations/v2/run_v2_installed_validation.py `
  --data-root "C:\path\to\new-dedicated-validation-copy" `
  --protected-root $DataRoot `
  --source-pdfs "$Run/source-pdfs" `
  --query-spec "$Run/gate-d-query.json" `
  --source-install $SourceInstall `
  --packaged-install $PackagedInstall `
  --transition-output "$ValidationRun/release-transitions.json" `
  --timeout-seconds 60 `
  --search-samples 20 `
  --output "$ValidationRun/installed-validation.json"
```

Valid evidence ends with `passed=true`, `fixture_only=false`, and
`release_eligible=false`; only the aggregate gate may make the final release
claim. An interrupted or failed run must restart with a fresh dedicated copy
and entirely new output paths. Never resume a mutated copy or relabel partial
evidence.

Any source-layout change makes an existing executable copied install stale.
The current ignored `.v2p` tree must not be used as `packaged-default` for a
new certificate. Cleanly rebuild both source and packaged installs before the
next real release certification; their launcher layouts, CPython version,
required package versions, and Python executable hashes must agree.
Run the fixed full suite after the final code change with the release runner.
It performs a sanitized collection pass, reconciles every collected node ID
against child JUnit testcases, pins the interpreter and source/test layouts,
and publishes both outputs read-only. Then assemble the schema-version-2 pending
candidate manifest. The assembler reopens, rehashes, and
strictly validates conversion, seed validation, reader parity, successor race,
launcher matrix, compatibility exception and its narrow architect approval,
both performance gates, the Gate D query, transition chain, installed
validation, pytest
attestation, and JUnit evidence. Every primary and approval input must already
be read-only:

```powershell
python scripts/migrations/v2/run_release_pytest.py `
  --junit-output "$Run/pytest-full.xml" `
  --attestation-output "$Run/pytest-full-attestation.json"

python scripts/migrations/v2/assemble_v2_release_gate.py --pending `
  --conversion "$Run/conversion.json" `
  --validation "$Run/validation.json" `
  --reader-parity "$Run/reader-parity.json" `
  --successor-race "<validated-v2_first_successor_execution_replay.json>" `
  --launcher-matrix "$Run/launcher-matrix.json" `
  --compatibility "$Run/successor-compatibility.json" `
  --compatibility-approval "$Run/successor-compatibility-approval.json" `
  --epoch-zero-performance "$Run/benchmark.json" `
  --successor-performance "$Run/successor-predecessor-benchmark.json" `
  --query-spec "$Run/gate-d-query.json" `
  --transition "$ValidationRun/release-transitions.json" `
  --installed-validation "$ValidationRun/installed-validation.json" `
  --pytest-attestation "$Run/pytest-full-attestation.json" `
  --pytest-junit "$Run/pytest-full.xml" `
  --output "$Run/release-candidate.json"
```

The current command chain has a blocking producer/consumer mismatch:
`run_v2_first_successor_race.py` emits kind
`v2_first_successor_execution` without a `replay` object, while
`assemble_v2_release_gate.py` accepts only kind
`v2_first_successor_execution_replay` with the exact replay payload and a
distinct active snapshot whose predecessor is the seed. No repository command
in this runbook currently produces that accepted artifact. Do not substitute
`first-successor-race.json`, relabel it, or claim aggregate release completion;
stop until the producer or an independently validated replay producer is fixed.

Independent Architect, Critic, and Verifier approvals must each use kind
`v2_release_review_approval`, report zero P0/P1 findings, remain individually
`release_eligible=false`, and bind the exact `release_bundle_sha256` printed by
the pending command. Rerun the same assembler without `--pending`, add one
`--approval ROLE=PATH` argument for each role, and choose a new output path. The
pending and final manifests, together with their bundle-hash basis, are all
schema version 2. Past soak artifacts, schema-v1 bundles, waivers, or approvals
bound to an older bundle must not be reused.
Only that final immutable artifact may contain `release_eligible=true`. Only
then may the version be raised and a Lore-format release commit be created.
