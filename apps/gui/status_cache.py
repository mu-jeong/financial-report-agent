"""Process-persistent cache for expensive passive GUI status snapshots."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import os
from pathlib import Path, PurePosixPath
import sqlite3
from threading import RLock
from typing import Any
from urllib.parse import quote

from src.core import data_update_jobs
from src.core import status as status_module


_CACHE_SIZE = 8
_CACHE_SCHEMA_VERSION = 1
_CACHE_LOCK = RLock()
_STATUS_CONFIG_FIELDS = (
    "EMBEDDING_MODEL",
    "EXTRACTION_ENGINE",
    "GENERATION_MODEL",
    "SEARCH_TOP_K",
    "UNEMBEDDED_EXTRACTION_ENGINE",
    "USE_PARENT_CHILD",
    "USE_RERANKER",
)


def _path_revision(path: Path) -> tuple[str, bool, int, int, int]:
    resolved = path.expanduser().resolve(strict=False)
    try:
        file_stat = resolved.stat()
    except OSError:
        return str(resolved), False, 0, 0, 0
    return (
        str(resolved),
        True,
        int(file_stat.st_size),
        int(file_stat.st_mtime_ns),
        int(file_stat.st_ctime_ns),
    )


def _sqlite_file_revisions(
    path: Path,
) -> tuple[tuple[str, bool, int, int, int], ...]:
    """Fallback fingerprint for missing or unreadable catalogs."""

    return tuple(
        _path_revision(Path(f"{path}{suffix}"))
        for suffix in ("", "-wal", "-shm", "-journal")
    )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        )
    }


def _group_revision(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> tuple[tuple[Any, ...], ...]:
    return tuple(tuple(row) for row in connection.execute(query, parameters))


def _artifact_revision(data_root: Path, relative_path: str) -> tuple[Any, ...]:
    relative = PurePosixPath(relative_path)
    if (
        relative.is_absolute()
        or relative.as_posix() != relative_path
        or any(part in {"", ".", ".."} for part in relative.parts)
        or (relative.parts and ":" in relative.parts[0])
    ):
        return ("invalid", relative_path)
    return (relative_path, *_path_revision(data_root.joinpath(*relative.parts))[1:])


def _evidence_revision(data_root: Path) -> tuple[tuple[Any, ...], ...]:
    """Track immutable files used by runtime fail-closed validation."""

    evidence_root = data_root / "retrieval" / "v2" / "evidence"
    try:
        evidence_root.stat()
    except OSError:
        return (("evidence", False),)

    revisions: list[tuple[Any, ...]] = [("evidence", True)]
    try:
        with os.scandir(evidence_root) as entries:
            publications = sorted(entries, key=lambda entry: entry.name)
    except OSError as exc:
        return (*revisions, ("scan-error", type(exc).__name__))

    for publication in publications:
        try:
            is_directory = publication.is_dir(follow_symlinks=False)
        except OSError as exc:
            revisions.append(
                (publication.name, "stat-error", type(exc).__name__)
            )
            continue
        revisions.append(
            (
                publication.name,
                "directory" if is_directory else "non-directory",
                publication.is_symlink(),
            )
        )
        if not is_directory:
            continue
        try:
            with os.scandir(publication.path) as entries:
                control_files = sorted(
                    (
                        entry
                        for entry in entries
                        if entry.name
                        in {"committed-floor.json", "commit-intent.json"}
                    ),
                    key=lambda entry: entry.name,
                )
        except OSError as exc:
            revisions.append(
                (
                    publication.name,
                    "scan-error",
                    type(exc).__name__,
                )
            )
            continue
        for control_file in control_files:
            try:
                file_stat = control_file.stat(follow_symlinks=False)
                is_file = control_file.is_file(follow_symlinks=False)
            except OSError as exc:
                revisions.append(
                    (
                        publication.name,
                        control_file.name,
                        "stat-error",
                        type(exc).__name__,
                    )
                )
                continue
            revisions.append(
                (
                    publication.name,
                    control_file.name,
                    "file" if is_file else "non-file",
                    control_file.is_symlink(),
                    int(file_stat.st_size),
                    int(file_stat.st_mtime_ns),
                    int(file_stat.st_ctime_ns),
                )
            )
    return tuple(revisions)


def _native_catalog_revision(
    catalog: Path,
    data_root: Path,
) -> tuple[Any, ...]:
    """Read a cheap logical revision that observes committed WAL changes."""

    catalog_identity = str(catalog.expanduser().resolve(strict=False))
    uri_path = quote(catalog.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    try:
        tables = _table_names(connection)
        if "retrieval_runtime" not in tables:
            raise sqlite3.DatabaseError("native runtime table is missing")

        runtime = connection.execute(
            """
            SELECT schema_version, active_snapshot_id, active_build_id,
                   predecessor_snapshot_id, publication_generation,
                   write_epoch, degraded, write_enabled, updated_at
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
        if runtime is None:
            raise sqlite3.DatabaseError("native runtime singleton is missing")

        report_revision: tuple[Any, ...] = ()
        if "reports" in tables:
            report_revision = tuple(
                connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(MAX(report_id), 0),
                           COALESCE(MAX(created_at), '')
                    FROM reports
                    """
                ).fetchone()
            )

        delta_revision: tuple[tuple[Any, ...], ...] = ()
        artifact_paths: list[str] = []
        if "retrieval_delta_segments" in tables:
            delta_revision = _group_revision(
                connection,
                """
                SELECT state, COUNT(*), COALESCE(MAX(sequence), 0),
                       COALESCE(MAX(segment_id), ''),
                       COALESCE(MAX(state_changed_at), '')
                FROM retrieval_delta_segments
                GROUP BY state ORDER BY state
                """,
            )
            artifact_paths.extend(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT relative_path FROM retrieval_delta_segments
                    WHERE relative_path IS NOT NULL
                    ORDER BY relative_path
                    """
                )
            )

        gc_revision: tuple[Any, ...] = ()
        if "retrieval_delta_artifact_gc" in tables:
            gc_revision = tuple(
                connection.execute(
                    """
                    SELECT COUNT(*), COALESCE(MAX(segment_id), ''),
                           COALESCE(MAX(collected_at), '')
                    FROM retrieval_delta_artifact_gc
                    """
                ).fetchone()
            )

        publication_revision: tuple[tuple[Any, ...], ...] = ()
        if "publication_runs" in tables:
            publication_revision = _group_revision(
                connection,
                """
                SELECT state, COUNT(*), COALESCE(MAX(publication_id), ''),
                       COALESCE(MAX(updated_at), '')
                FROM publication_runs
                GROUP BY state ORDER BY state
                """,
            )

        if "vector_snapshots" in tables and runtime[1]:
            snapshot = connection.execute(
                """
                SELECT relative_path FROM vector_snapshots
                WHERE snapshot_id = ?
                """,
                (runtime[1],),
            ).fetchone()
            if snapshot is not None and snapshot[0]:
                artifact_paths.append(str(snapshot[0]))

        artifacts = tuple(
            _artifact_revision(data_root, relative_path)
            for relative_path in sorted(set(artifact_paths))
        )
        return (
            "native",
            catalog_identity,
            tuple(runtime),
            report_revision,
            delta_revision,
            gc_revision,
            publication_revision,
            artifacts,
            _evidence_revision(data_root),
        )
    finally:
        connection.close()


def _catalog_revision(data_root: str) -> tuple[Any, ...]:
    paths, _data_root = status_module._status_retrieval_paths(data_root)
    if paths.catalog.is_file():
        try:
            return _native_catalog_revision(paths.catalog, paths.data_root)
        except (OSError, sqlite3.Error) as exc:
            return (
                "native-unavailable",
                type(exc).__name__,
                str(exc),
                _sqlite_file_revisions(paths.catalog),
                _evidence_revision(paths.data_root),
            )
    return (
        "uninitialized",
        _sqlite_file_revisions(paths.catalog),
        _path_revision(paths.v2_root),
    )


def _update_job_revision() -> tuple[Any, ...]:
    """Invalidate on job lifecycle changes, not every progress write."""

    try:
        job = data_update_jobs.read_status() or {}
    except OSError:
        return ("unavailable",)
    return (
        job.get("state"),
        job.get("phase"),
        job.get("pid"),
        job.get("parent_pid"),
        job.get("label"),
    )


def _status_revision(
    *,
    data_root: str,
) -> tuple[Any, ...]:
    catalog_revision = _catalog_revision(data_root)
    source_path = Path(status_module.__file__ or "")
    config_revision = tuple(
        getattr(status_module, field, None) for field in _STATUS_CONFIG_FIELDS
    )
    return (
        _CACHE_SCHEMA_VERSION,
        catalog_revision,
        _path_revision(source_path),
        _update_job_revision(),
        config_revision,
    )


@lru_cache(maxsize=_CACHE_SIZE)
def _cached_status(
    save_dir: str,
    data_root: str,
    native_only: bool,
    _revision: tuple[Any, ...],
) -> dict[str, Any]:
    if native_only:
        result = status_module.get_native_v2_data_status(
            save_dir=save_dir,
            data_root=data_root,
        )
    else:
        result = status_module.get_data_status(
            save_dir=save_dir,
            data_root=data_root,
        )
    return result


def _refresh_volatile_fields(
    snapshot: dict[str, Any],
    *,
    save_dir: str,
) -> dict[str, Any]:
    if "downloaded_pdfs" in snapshot:
        snapshot["downloaded_pdfs"] = status_module._safe_count_pdfs(save_dir)

    paths = snapshot.get("paths")
    db = snapshot.get("db")
    if isinstance(paths, dict) and isinstance(db, dict) and paths.get("catalog_path"):
        try:
            db["size_bytes"] = Path(str(paths["catalog_path"])).stat().st_size
        except OSError:
            db["size_bytes"] = 0

    retrieval = snapshot.get("retrieval")
    if isinstance(retrieval, dict) and retrieval.get("oldest_pending_cleanup_at"):
        try:
            oldest = datetime.fromisoformat(
                str(retrieval["oldest_pending_cleanup_at"]).replace("Z", "+00:00")
            )
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            retrieval["oldest_pending_cleanup_age_seconds"] = max(
                0,
                int((datetime.now(timezone.utc) - oldest).total_seconds()),
            )
        except (TypeError, ValueError):
            pass
    return snapshot


def get_data_status(
    *,
    save_dir: str | None = None,
    data_root: str | None = None,
) -> dict[str, Any]:
    """Return an isolated snapshot, reusing work for an unchanged data revision."""

    resolved_save_dir = save_dir or status_module.SAVE_DIR
    resolved_data_root = data_root or status_module.DATA_ROOT
    revision = _status_revision(
        data_root=resolved_data_root,
    )
    with _CACHE_LOCK:
        snapshot = deepcopy(
            _cached_status(
                resolved_save_dir,
                resolved_data_root,
                False,
                revision,
            )
        )
    return _refresh_volatile_fields(snapshot, save_dir=resolved_save_dir)


def get_native_v2_data_status(
    *,
    save_dir: str | None = None,
    data_root: str | None = None,
) -> dict[str, Any]:
    """Return the cached Native V2-only monitoring snapshot."""

    resolved_save_dir = save_dir or status_module.SAVE_DIR
    resolved_data_root = data_root or status_module.DATA_ROOT
    revision = _status_revision(
        data_root=resolved_data_root,
    )
    # A present native catalog has the same fail-closed result on both core
    # entry points. Reuse the sidebar snapshot instead of aggregating it again
    # when the user opens Monitoring.
    native_only = revision[1][0] != "native"
    with _CACHE_LOCK:
        snapshot = deepcopy(
            _cached_status(
                resolved_save_dir,
                resolved_data_root,
                native_only,
                revision,
            )
        )
    return _refresh_volatile_fields(snapshot, save_dir=resolved_save_dir)


def clear() -> None:
    """Clear process-local snapshots for tests and explicit operator recovery."""

    with _CACHE_LOCK:
        _cached_status.cache_clear()
