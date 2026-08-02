"""Version-aware retrieval bootstrap and fail-closed runtime inspection.

The native catalog is the only authority once it exists.  A legacy-only
installation remains selectable until an off-path V2 seed is published, while
an invalid native installation never silently falls back to arbitrary V1
files.  Epoch-zero fallback is limited to the sealed compatibility bundle.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from src.retrieval.compatibility_bundle import validate_compatibility_bundle
from src.retrieval.schema import (
    RETRIEVAL_TABLES,
    SCHEMA_VERSION,
    SchemaError,
    configure_catalog_storage,
)


class RetrievalBootstrapError(RuntimeError):
    """Raised when retrieval state cannot be selected without guessing."""


RuntimeValidationMode = Literal["full", "read"]


def load_index(*args, **kwargs):
    """Preserve the validation seam without importing FAISS during status load."""
    from src.retrieval.vector_index import load_index as _load_index

    return _load_index(*args, **kwargs)


@dataclass(frozen=True)
class RetrievalPaths:
    data_root: Path
    catalog: Path
    v2_root: Path


@dataclass(frozen=True)
class RuntimeSelection:
    mode: str
    paths: RetrievalPaths
    active_snapshot_id: str | None = None
    active_build_id: str | None = None
    predecessor_snapshot_id: str | None = None
    publication_generation: int = 0
    write_epoch: int = 0
    v1_fallback_open: bool = True
    degraded: bool = False
    write_enabled: bool = False
    compatibility_bundle_id: str | None = None
    error_code: str | None = None

    @property
    def is_native(self) -> bool:
        return self.mode == "native"


def retrieval_paths(
    legacy_db_path: str | Path,
    *,
    data_root: str | Path | None = None,
) -> RetrievalPaths:
    """Return the canonical native paths anchored to one local data root."""

    legacy = Path(legacy_db_path).expanduser()
    root = Path(data_root).expanduser() if data_root is not None else legacy.parent
    root = root.resolve()
    v2_root = root / "retrieval" / "v2"
    return RetrievalPaths(data_root=root, catalog=v2_root / "catalog.sqlite3", v2_root=v2_root)


def inspect_runtime(
    legacy_db_path: str | Path,
    *,
    data_root: str | Path | None = None,
    validate_snapshot: bool = True,
    catalog_validation: RuntimeValidationMode = "full",
) -> RuntimeSelection:
    """Inspect and validate the one supported retrieval runtime.

    Absence of both a native catalog and every V2 footprint means the
    installation is still V1. Once any durable V2 state exists, a missing or
    invalid catalog fails closed instead of reopening the legacy path.
    """

    if catalog_validation not in {"full", "read"}:
        raise ValueError("catalog_validation must be 'full' or 'read'")

    paths = retrieval_paths(legacy_db_path, data_root=data_root)
    if not paths.catalog.exists():
        if _has_native_footprint(paths):
            raise RetrievalBootstrapError(
                "native catalog is missing while V2 recovery evidence exists"
            )
        return RuntimeSelection(mode="legacy_v1", paths=paths)
    if paths.catalog.is_symlink() or not paths.catalog.is_file():
        raise RetrievalBootstrapError("native catalog must be a real local file")

    connection = _open_read_only(paths.catalog)
    try:
        _validate_catalog(
            connection,
            validate_integrity=catalog_validation == "full",
        )
        try:
            from src.retrieval.recovery import _validate_startup_control_plane
            from src.retrieval.publication import _open_catalog

            control = _open_catalog(paths.catalog, read_only=True)
            try:
                _validate_startup_control_plane(
                    paths.data_root,
                    control,
                    validate_integrity=False,
                )
            finally:
                control.close()
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            raise RetrievalBootstrapError(
                f"native runtime conflicts with durable recovery evidence: {exc}"
            ) from exc
        row = connection.execute(
            """
            SELECT active_snapshot_id, active_build_id, predecessor_snapshot_id,
                   publication_generation, write_epoch, v1_fallback_open,
                   degraded, write_enabled
            FROM retrieval_runtime
            WHERE runtime_id = 1
            """
        ).fetchone()
        if row is None:
            raise RetrievalBootstrapError("native runtime singleton is missing")

        selection = RuntimeSelection(
            mode="native",
            paths=paths,
            active_snapshot_id=row[0],
            active_build_id=row[1],
            predecessor_snapshot_id=row[2],
            publication_generation=int(row[3]),
            write_epoch=int(row[4]),
            v1_fallback_open=bool(row[5]),
            degraded=bool(row[6]),
            write_enabled=bool(row[7]),
        )
        if not selection.active_snapshot_id or not selection.active_build_id:
            raise RetrievalBootstrapError("native catalog has no active complete snapshot")

        descriptor_row = connection.execute(
            """
            SELECT snapshot.relative_path, snapshot.file_sha256,
                   snapshot.size_bytes, snapshot.dimension, snapshot.metric,
                   snapshot.ntotal, snapshot.state, build.state
            FROM vector_snapshots AS snapshot
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            WHERE snapshot.snapshot_id = ? AND snapshot.build_id = ?
            """,
            (selection.active_snapshot_id, selection.active_build_id),
        ).fetchone()
        if descriptor_row is None:
            raise RetrievalBootstrapError("active snapshot catalog row is missing")
        if descriptor_row[6] != "ready" or descriptor_row[7] != "fully_complete":
            raise RetrievalBootstrapError("active snapshot/build is not fully complete")

        if validate_snapshot:
            try:
                from src.retrieval.vector_index import (
                    SnapshotDescriptor,
                    VectorIndexError,
                )
            except (ImportError, OSError) as exc:
                return _epoch_zero_fallback_or_raise(connection, selection, exc)

            try:
                snapshot_path = _anchored_path(paths.data_root, descriptor_row[0])
                if snapshot_path.is_symlink():
                    raise RetrievalBootstrapError("active snapshot cannot be a symlink")
                load_index(
                    snapshot_path,
                    SnapshotDescriptor(
                        sha256=descriptor_row[1],
                        size_bytes=int(descriptor_row[2]),
                        dimension=int(descriptor_row[3]),
                        metric=descriptor_row[4],
                        ntotal=int(descriptor_row[5]),
                    ),
                )
            except (OSError, VectorIndexError, RetrievalBootstrapError) as exc:
                return _epoch_zero_fallback_or_raise(connection, selection, exc)
        return selection
    finally:
        connection.close()


def reconcile_and_inspect_runtime(
    legacy_db_path: str | Path,
    *,
    data_root: str | Path | None = None,
    allow_live_writer_read: bool = False,
    prefer_fast_read: bool = False,
) -> RuntimeSelection:
    """Recover when needed, with an optional zero-scan path for trusted reads."""

    paths = retrieval_paths(legacy_db_path, data_root=data_root)
    if paths.catalog.is_symlink() or (
        paths.catalog.exists() and not paths.catalog.is_file()
    ):
        raise RetrievalBootstrapError("native catalog must be a real local file")
    if not paths.catalog.exists() and not _has_native_footprint(paths):
        return RuntimeSelection(mode="legacy_v1", paths=paths)

    from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
    from src.retrieval.garbage_collector import (
        GarbageCollectionError,
        RetrievalGarbageCollector,
    )
    from src.retrieval.writer_lock import (
        NativeWriterLock,
        WriterLockBusyError,
        WriterLockError,
    )

    try:
        with NativeWriterLock(paths.data_root) as writer_lease:
            if prefer_fast_read:
                try:
                    selection = inspect_runtime(
                        legacy_db_path,
                        data_root=paths.data_root,
                        validate_snapshot=True,
                        catalog_validation="read",
                    )
                    if (
                        not _catalog_has_running_publication(paths.catalog)
                        and not _catalog_requires_garbage_reconciliation(
                            paths.catalog
                        )
                    ):
                        if selection.is_native:
                            from src.retrieval.dispatch import prime_native_dispatch

                            prime_native_dispatch(selection)
                        return selection
                except (RetrievalBootstrapError, sqlite3.Error):
                    # Missing, inconsistent, or unreadable native state must use
                    # the existing full recovery path instead of falling back.
                    pass

            outcome = StartupReconciler(paths.data_root).reconcile(
                writer_lease=writer_lease,
            )
            if outcome.disposition == RecoveryDisposition.FAIL_CLOSED:
                raise RetrievalBootstrapError(outcome.reason or "native recovery failed closed")
            try:
                RetrievalGarbageCollector(
                    paths.data_root
                )._reconcile_pending_snapshots_after_validation(
                    writer_lease=writer_lease
                )
            except GarbageCollectionError as exc:
                raise RetrievalBootstrapError(
                    f"native snapshot garbage reconciliation failed: {exc}"
                ) from exc
            selection = inspect_runtime(
                legacy_db_path,
                data_root=paths.data_root,
                validate_snapshot=True,
                catalog_validation="read",
            )
            if selection.is_native:
                # Startup owns cold validation; keep the same process on the
                # reusable production reader instead of repeating inspection
                # and construction on every query.
                from src.retrieval.dispatch import prime_native_dispatch

                prime_native_dispatch(selection)
            return selection
    except WriterLockBusyError as exc:
        if not allow_live_writer_read:
            raise RetrievalBootstrapError(
                f"native startup reconciliation is locked: {exc}"
            ) from exc
        # A live build owns all recovery mutations. SQLite publication and the
        # durable floor make the last committed active snapshot safe to inspect
        # concurrently, so readers remain available between batch publications.
        selection = inspect_runtime(
            legacy_db_path,
            data_root=paths.data_root,
            validate_snapshot=True,
            catalog_validation="read",
        )
        if selection.is_native:
            from src.retrieval.dispatch import prime_native_dispatch

            prime_native_dispatch(selection)
        return selection
    except WriterLockError as exc:
        raise RetrievalBootstrapError(f"native startup reconciliation is locked: {exc}") from exc


def _has_native_footprint(paths: RetrievalPaths) -> bool:
    """Distinguish a fresh V1 root from a damaged or interrupted V2 root."""

    for candidate in (
        paths.v2_root,
        paths.data_root / "retrieval" / "compat" / "v1",
    ):
        if candidate.is_symlink():
            return True
        if not candidate.exists():
            continue
        if not candidate.is_dir():
            return True
        try:
            next(candidate.iterdir())
        except StopIteration:
            continue
        except OSError:
            return True
        return True
    return False


def resolve_epoch_zero_compatibility_bundle_id(
    selection: RuntimeSelection,
) -> str:
    """Validate and return bridge evidence without changing runtime selection."""

    if (
        selection.write_epoch != 0
        or not selection.v1_fallback_open
        or not selection.active_snapshot_id
    ):
        raise RetrievalBootstrapError("epoch-zero compatibility evidence is unavailable")
    connection = _open_read_only(selection.paths.catalog)
    try:
        _validate_catalog(connection)
        return _compatibility_bundle_id_from_evidence(connection, selection)
    finally:
        connection.close()


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        configure_catalog_storage(connection)
        connection.execute("PRAGMA query_only = ON")
        return connection
    except (sqlite3.Error, SchemaError) as exc:
        raise RetrievalBootstrapError(f"native catalog cannot be opened: {exc}") from exc


def _validate_catalog(
    connection: sqlite3.Connection,
    *,
    validate_integrity: bool = True,
) -> None:
    try:
        if validate_integrity:
            from src.retrieval.publication import _validate_catalog_integrity

            _validate_catalog_integrity(connection)
        _validate_catalog_structure(connection)
    except RetrievalBootstrapError:
        raise
    except (sqlite3.Error, RuntimeError) as exc:
        raise RetrievalBootstrapError(f"native catalog validation failed: {exc}") from exc


def _validate_catalog_structure(connection: sqlite3.Connection) -> None:
    """Validate the small control-plane shape without scanning every DB page."""

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not RETRIEVAL_TABLES.issubset(tables):
        raise RetrievalBootstrapError("native catalog schema is incomplete")
    runtime = connection.execute(
        "SELECT COUNT(*), MIN(schema_version), MAX(schema_version) FROM retrieval_runtime"
    ).fetchone()
    if runtime != (1, SCHEMA_VERSION, SCHEMA_VERSION):
        raise RetrievalBootstrapError("native catalog schema/runtime version is invalid")


def _catalog_has_running_publication(catalog_path: Path) -> bool:
    """Return whether an idle writer lease inherited a journal to reconcile."""

    connection = _open_read_only(catalog_path)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM publication_runs WHERE state = 'running' LIMIT 1"
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _catalog_requires_garbage_reconciliation(catalog_path: Path) -> bool:
    """Detect durable cleanup work before selecting the non-mutating fast path."""

    connection = _open_read_only(catalog_path)
    try:
        if connection.execute(
            """
            SELECT 1 FROM vector_snapshots
            WHERE state = 'garbage_pending' LIMIT 1
            """
        ).fetchone() is not None:
            return True
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name IN (
                    'retrieval_delta_segments', 'retrieval_delta_artifact_gc'
                )
                """
            )
        }
        if "retrieval_delta_segments" not in tables:
            return False
        if "retrieval_delta_artifact_gc" not in tables:
            return True
        return (
            connection.execute(
                """
                SELECT 1
                FROM retrieval_delta_segments AS segment
                JOIN vector_snapshots AS base
                  ON base.snapshot_id = segment.base_snapshot_id
                LEFT JOIN retrieval_delta_artifact_gc AS artifact_gc
                  ON artifact_gc.segment_id = segment.segment_id
                WHERE segment.relative_path IS NOT NULL
                  AND base.state = 'garbage_collected'
                  AND artifact_gc.segment_id IS NULL
                  AND (
                      segment.state = 'compacted'
                      OR (
                          segment.state IN ('ready', 'failed')
                          AND NOT EXISTS (
                              SELECT 1 FROM retrieval_runtime AS runtime
                              WHERE runtime.runtime_id = 1
                                AND runtime.active_snapshot_id =
                                    segment.base_snapshot_id
                                AND runtime.publication_generation =
                                    segment.base_publication_generation
                          )
                      )
                  )
                LIMIT 1
                """
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _epoch_zero_fallback_or_raise(
    connection: sqlite3.Connection,
    selection: RuntimeSelection,
    snapshot_error: Exception,
) -> RuntimeSelection:
    if selection.write_epoch != 0 or not selection.v1_fallback_open:
        raise RetrievalBootstrapError(
            f"active native snapshot is invalid after fallback closure: {snapshot_error}"
        ) from snapshot_error

    try:
        bundle_id = _compatibility_bundle_id_from_evidence(connection, selection)
    except RetrievalBootstrapError as exc:
        raise exc from snapshot_error
    return RuntimeSelection(
        **{
            **selection.__dict__,
            "mode": "epoch_zero_compatibility",
            "compatibility_bundle_id": bundle_id,
            "error_code": "ACTIVE_SNAPSHOT_INVALID",
            "write_enabled": False,
        }
    )


def _compatibility_bundle_id_from_evidence(
    connection: sqlite3.Connection,
    selection: RuntimeSelection,
) -> str:
    evidence = connection.execute(
        """
        SELECT evidence_manifest_relative_path, evidence_manifest_sha256
        FROM publication_runs
        WHERE to_snapshot_id = ? AND state = 'fully_complete'
        ORDER BY created_at DESC, publication_id DESC
        LIMIT 1
        """,
        (selection.active_snapshot_id,),
    ).fetchone()
    if evidence is None or not evidence[0] or not evidence[1]:
        raise RetrievalBootstrapError(
            "epoch-zero snapshot is invalid and has no sealed compatibility evidence"
        )
    manifest_path = _anchored_path(selection.paths.data_root, evidence[0])
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RetrievalBootstrapError("compatibility evidence manifest is unavailable")
    if _sha256_file(manifest_path) != str(evidence[1]).lower():
        raise RetrievalBootstrapError("compatibility evidence manifest hash changed")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RetrievalBootstrapError("compatibility evidence manifest is invalid") from exc
    bundle_id = payload.get("compatibility_bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        raise RetrievalBootstrapError("compatibility bundle identity is missing")

    try:
        validate_compatibility_bundle(selection.paths.data_root, bundle_id)
    except Exception as exc:
        raise RetrievalBootstrapError("sealed compatibility bundle validation failed") from exc
    return bundle_id


def _anchored_path(data_root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise RetrievalBootstrapError("catalog path is empty")
    candidate = data_root.joinpath(*relative_path.replace("\\", "/").split("/"))
    resolved = candidate.resolve()
    try:
        resolved.relative_to(data_root.resolve())
    except ValueError as exc:
        raise RetrievalBootstrapError("catalog path escapes the data root") from exc
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
