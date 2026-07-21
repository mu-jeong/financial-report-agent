"""Central write-entrypoint guard for V1/V2 retrieval transitions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from src.retrieval.bootstrap import RuntimeSelection, inspect_runtime
from src.retrieval.schema import SchemaError, configure_catalog_storage
from src.retrieval.writer_lock import WriterLease, assert_writer_lease_owned


class RetrievalWriteBlocked(RuntimeError):
    """Raised before crawler/extractor/embedder initialization is permitted."""


def guard_before_retrieval_write(
    legacy_db_path: str | Path,
    *,
    data_root: str | Path | None = None,
    allow_degraded_forward_recovery: bool = False,
    first_successor_writer_lease: WriterLease | None = None,
) -> RuntimeSelection:
    """Fail before side effects unless the selected runtime can accept a build."""

    selection = inspect_runtime(
        legacy_db_path,
        data_root=data_root,
        validate_snapshot=True,
    )
    if selection.mode == "legacy_v1":
        return selection
    if selection.mode != "native":
        raise RetrievalWriteBlocked(
            "native active snapshot is unavailable; compatibility mode is read-only"
        )
    first_successor_build = False
    if first_successor_writer_lease is not None:
        if type(first_successor_writer_lease) is not WriterLease:
            raise RetrievalWriteBlocked("first-successor writer lease is invalid")
        assert_writer_lease_owned(
            first_successor_writer_lease,
            selection.paths.data_root,
        )
        first_successor_build = bool(
            selection.write_epoch == 0
            and selection.v1_fallback_open
            and not selection.degraded
            and not selection.write_enabled
            and selection.active_snapshot_id
        )
    recovery_build = bool(
        allow_degraded_forward_recovery
        and selection.degraded
        and selection.write_epoch > 0
        and not selection.v1_fallback_open
        and selection.active_snapshot_id
    )
    if selection.degraded and not recovery_build:
        raise RetrievalWriteBlocked("native retrieval is degraded; forward recovery is required")
    if selection.write_epoch == 0 and not first_successor_build:
        raise RetrievalWriteBlocked("native writes are disabled at epoch zero")
    if not selection.write_enabled and not recovery_build and not first_successor_build:
        raise RetrievalWriteBlocked("native writes are disabled")
    if _has_running_publication(selection.paths.catalog):
        raise RetrievalWriteBlocked(
            "another native publication is already running or snapshot garbage is pending"
        )
    return selection


def _has_running_publication(catalog: Path) -> bool:
    uri = f"file:{quote(catalog.resolve().as_posix(), safe='/:')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            configure_catalog_storage(connection)
            connection.execute("PRAGMA query_only = ON")
            return connection.execute(
                """
                SELECT 1 FROM publication_runs WHERE state = 'running'
                UNION ALL
                SELECT 1 FROM vector_snapshots WHERE state = 'garbage_pending'
                LIMIT 1
                """
            ).fetchone() is not None
        finally:
            connection.close()
    except (sqlite3.Error, SchemaError) as exc:
        raise RetrievalWriteBlocked(f"native publication journal cannot be read: {exc}") from exc
