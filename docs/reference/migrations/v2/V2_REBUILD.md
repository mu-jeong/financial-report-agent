# Native V2 full rebuild

Use the full rebuild only when an active Native V2 installation must adopt a
different extraction or embedding profile. Normal document additions and
changes use the incremental update path and do not require this operation.

First close the app and any data-update process, then run the read-only check:

```bat
tools\recovery\REBUILD_V2.bat --check
```

If the check reports that the active and target profiles already match, no
action is required. Otherwise, verify the displayed profiles and run:

```bat
tools\recovery\REBUILD_V2.bat
```

The rebuild processes the complete PDF corpus, so it can take time and incur
embedding API charges. The current active snapshot remains available until a
complete successor passes validation and is published atomically. Individual
PDF extraction failures are recorded as exclusions; a systemic embedding,
profile, manifest, or snapshot validation failure leaves the active snapshot
unchanged.

Do not delete or edit `DATA_ROOT/retrieval/v2` to force a rebuild. The supported
entry point invokes `scripts/migrations/v2/rebuild_v2_successor.py` and keeps
the recovery boundary explicit.
