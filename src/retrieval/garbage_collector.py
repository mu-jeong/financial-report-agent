"""Lease-safe cleanup for retired native retrieval artifacts."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from src.retrieval.publication import PublicationError
from src.retrieval.repository import (
    SnapshotCache,
    SnapshotInUseError,
    shared_snapshot_cache,
)
from src.retrieval.schema import SchemaError, configure_catalog_storage
from src.retrieval.writer_lock import (
    NativeWriterLock,
    WriterLease,
    assert_writer_lease_owned,
)


class GarbageCollectionError(RuntimeError):
    """Raised when cleanup cannot be proved safe."""


class GarbageCollectionBlocked(GarbageCollectionError):
    """Raised when the requested object is still part of the served lineage."""


@dataclass(frozen=True)
class SnapshotGCOutcome:
    snapshot_id: str
    state: str
    deleted: bool
    evicted_revisions: int
    reason: str


@dataclass(frozen=True)
class CompactedArtifactGCOutcome:
    segment_id: str
    relative_path: str
    deleted: bool
    evicted_revisions: int
    reason: str


FileRemover = Callable[[Path], None]


class RetrievalGarbageCollector:
    """Reconcile immutable artifacts without deleting an open or served file."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        cache: SnapshotCache | None = None,
        remove_file: FileRemover | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve(strict=True)
        self.catalog_path = self.data_root / "retrieval" / "v2" / "catalog.sqlite3"
        self.cache = cache or shared_snapshot_cache(self.data_root)
        self._remove_file = remove_file or _remove_file

    def collect_snapshot(
        self,
        snapshot_id: str,
        *,
        writer_lease: WriterLease | None = None,
    ) -> SnapshotGCOutcome:
        """Collect one snapshot, then reconcile eligible compacted child bytes."""

        if writer_lease is None:
            with NativeWriterLock(self.data_root) as owned_lease:
                return self.collect_snapshot(
                    snapshot_id,
                    writer_lease=owned_lease,
                )
        assert_writer_lease_owned(writer_lease, self.data_root)
        self._validate_catalog_for_mutation()
        return self._collect_snapshot_after_validation(
            snapshot_id,
            writer_lease=writer_lease,
        )

    def _collect_snapshot_after_validation(
        self,
        snapshot_id: str,
        *,
        writer_lease: WriterLease,
    ) -> SnapshotGCOutcome:
        """Collect after the caller established full catalog integrity."""

        assert_writer_lease_owned(writer_lease, self.data_root)
        outcome = self._collect_snapshot(snapshot_id)
        self._reconcile_compacted_delta_artifacts_after_validation(
            writer_lease=writer_lease
        )
        return outcome

    def _collect_snapshot(self, snapshot_id: str) -> SnapshotGCOutcome:
        if not isinstance(snapshot_id, str) or not snapshot_id.strip():
            raise ValueError("snapshot_id must be a non-empty string")
        state, relative_path = self._mark_snapshot_pending(snapshot_id)
        if state == "garbage_collected":
            return SnapshotGCOutcome(snapshot_id, state, True, 0, "already collected")

        try:
            evicted = self.cache.evict_snapshot(snapshot_id)
        except SnapshotInUseError as exc:
            return SnapshotGCOutcome(
                snapshot_id,
                "garbage_pending",
                False,
                0,
                str(exc),
            )
        except (PermissionError, OSError) as exc:
            return SnapshotGCOutcome(
                snapshot_id,
                "garbage_pending",
                False,
                0,
                f"snapshot cache eviction failed: {exc}",
            )

        snapshot_path = _resolve_relative(self.data_root, relative_path)
        try:
            if snapshot_path.is_symlink():
                raise GarbageCollectionError("snapshot path became a symbolic link")
            if snapshot_path.exists():
                if not snapshot_path.is_file():
                    raise GarbageCollectionError("snapshot path is not a regular file")
                self._remove_file(snapshot_path)
        except (PermissionError, OSError) as exc:
            return SnapshotGCOutcome(
                snapshot_id,
                "garbage_pending",
                False,
                evicted,
                f"snapshot deletion is pending: {exc}",
            )

        connection = _open_catalog(self.catalog_path)
        try:
            with connection:
                row = connection.execute(
                    "SELECT state FROM vector_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if row is None:
                    raise GarbageCollectionError("snapshot audit row disappeared")
                if row[0] == "garbage_pending":
                    connection.execute(
                        """
                        UPDATE vector_snapshots
                        SET state = 'garbage_collected',
                            state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE snapshot_id = ?
                        """,
                        (snapshot_id,),
                    )
                elif row[0] != "garbage_collected":
                    raise GarbageCollectionError("snapshot cleanup state changed unexpectedly")
        finally:
            connection.close()
        return SnapshotGCOutcome(
            snapshot_id,
            "garbage_collected",
            True,
            evicted,
            "snapshot bytes deleted after cache eviction",
        )

    def reconcile_pending_snapshots(
        self,
        *,
        writer_lease: WriterLease | None = None,
    ) -> tuple[SnapshotGCOutcome, ...]:
        """Retry pending snapshots and sweep compacted children, as startup does."""

        if writer_lease is None:
            with NativeWriterLock(self.data_root) as owned_lease:
                return self.reconcile_pending_snapshots(writer_lease=owned_lease)
        assert_writer_lease_owned(writer_lease, self.data_root)
        self._validate_catalog_for_mutation()
        return self._reconcile_pending_snapshots_after_validation(
            writer_lease=writer_lease
        )

    def _reconcile_pending_snapshots_after_validation(
        self,
        *,
        writer_lease: WriterLease,
    ) -> tuple[SnapshotGCOutcome, ...]:
        """Reconcile pending artifacts after a full recovery/publication check."""

        assert_writer_lease_owned(writer_lease, self.data_root)
        connection = _open_catalog(self.catalog_path, read_only=True)
        try:
            snapshot_ids = tuple(
                row[0]
                for row in connection.execute(
                    """
                    SELECT snapshot_id FROM vector_snapshots
                    WHERE state = 'garbage_pending'
                    ORDER BY snapshot_id
                    """
                )
            )
        finally:
            connection.close()
        outcomes = tuple(
            self._collect_snapshot(str(snapshot_id))
            for snapshot_id in snapshot_ids
        )
        self._reconcile_compacted_delta_artifacts_after_validation(
            writer_lease=writer_lease
        )
        return outcomes

    def reconcile_compacted_delta_artifacts(
        self,
        *,
        writer_lease: WriterLease | None = None,
    ) -> tuple[CompactedArtifactGCOutcome, ...]:
        """Delete compacted delta bytes once their owning base completed GC."""

        if writer_lease is None:
            with NativeWriterLock(self.data_root) as owned_lease:
                return self.reconcile_compacted_delta_artifacts(
                    writer_lease=owned_lease
                )
        assert_writer_lease_owned(writer_lease, self.data_root)
        self._validate_catalog_for_mutation()
        return self._reconcile_compacted_delta_artifacts_after_validation(
            writer_lease=writer_lease
        )

    def _reconcile_compacted_delta_artifacts_after_validation(
        self,
        *,
        writer_lease: WriterLease,
    ) -> tuple[CompactedArtifactGCOutcome, ...]:
        """Sweep compacted artifacts after the caller's full validation."""

        assert_writer_lease_owned(writer_lease, self.data_root)
        from src.retrieval.delta_schema import (
            delta_schema_installed,
            install_delta_schema,
        )

        connection = _open_catalog(self.catalog_path)
        try:
            try:
                has_delta_schema = delta_schema_installed(connection)
            except SchemaError as exc:
                raise GarbageCollectionError(
                    "retrieval delta schema is invalid during artifact cleanup"
                ) from exc
            if not has_delta_schema:
                return ()
            install_delta_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE retrieval_delta_segments
                SET state = 'compacted',
                    state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE state IN ('ready', 'failed')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM retrieval_runtime AS runtime
                      WHERE runtime.runtime_id = 1
                        AND runtime.active_snapshot_id =
                            retrieval_delta_segments.base_snapshot_id
                        AND runtime.publication_generation =
                            retrieval_delta_segments.base_publication_generation
                  )
                """
            )
            connection.commit()
            rows = connection.execute(
                """
                SELECT segment.segment_id, segment.relative_path,
                       segment.file_sha256, segment.size_bytes
                FROM retrieval_delta_segments AS segment
                JOIN vector_snapshots AS base
                  ON base.snapshot_id = segment.base_snapshot_id
                LEFT JOIN retrieval_delta_artifact_gc AS artifact_gc
                  ON artifact_gc.segment_id = segment.segment_id
                WHERE segment.state = 'compacted'
                  AND segment.relative_path IS NOT NULL
                  AND base.state = 'garbage_collected'
                  AND artifact_gc.segment_id IS NULL
                ORDER BY segment.segment_id
                """
            ).fetchall()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        outcomes: list[CompactedArtifactGCOutcome] = []
        collected_segment_ids: list[str] = []
        for (
            segment_id_value,
            relative_path_value,
            file_sha256_value,
            size_bytes_value,
        ) in rows:
            segment_id = str(segment_id_value)
            relative_path = str(relative_path_value)
            file_sha256 = str(file_sha256_value)
            size_bytes = int(size_bytes_value)
            try:
                evicted = self.cache.evict_snapshot(segment_id)
            except SnapshotInUseError as exc:
                outcomes.append(
                    CompactedArtifactGCOutcome(
                        segment_id,
                        relative_path,
                        False,
                        0,
                        str(exc),
                    )
                )
                continue
            except (PermissionError, OSError) as exc:
                outcomes.append(
                    CompactedArtifactGCOutcome(
                        segment_id,
                        relative_path,
                        False,
                        0,
                        f"artifact cache eviction failed: {exc}",
                    )
                )
                continue

            try:
                existed = self._remove_verified_compacted_artifact(
                    segment_id=segment_id,
                    relative_path=relative_path,
                    expected_sha256=file_sha256,
                    expected_size_bytes=size_bytes,
                )
            except (PermissionError, OSError) as exc:
                outcomes.append(
                    CompactedArtifactGCOutcome(
                        segment_id,
                        relative_path,
                        False,
                        evicted,
                        f"artifact deletion is pending: {exc}",
                    )
                )
                continue
            outcomes.append(
                CompactedArtifactGCOutcome(
                    segment_id,
                    relative_path,
                    True,
                    evicted,
                    (
                        "compacted artifact bytes deleted after cache eviction"
                        if existed
                        else "compacted artifact bytes were already absent"
                    ),
                )
            )
            collected_segment_ids.append(segment_id)
        self._record_compacted_artifacts_collected(collected_segment_ids)
        return tuple(outcomes)

    def _remove_verified_compacted_artifact(
        self,
        *,
        segment_id: str,
        relative_path: str,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> bool:
        """Quarantine and verify immutable bytes before path-based deletion."""

        artifact_path = _lexical_relative(self.data_root, relative_path)
        _assert_safe_directory_chain(self.data_root, artifact_path.parent)
        try:
            source_stat = artifact_path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(source_stat.st_mode):
            raise GarbageCollectionError(
                "compacted artifact path is not a regular file"
            )

        staging_root = self.data_root / "retrieval" / "v2" / "gc-staging"
        _ensure_safe_staging_root(self.data_root, staging_root)
        operation_dir = staging_root / f"segment-{segment_id}-{uuid.uuid4().hex}"
        operation_dir.mkdir()
        staged_path = operation_dir / "artifact"
        try:
            try:
                os.replace(artifact_path, staged_path)
            except FileNotFoundError:
                if _path_exists_no_follow(artifact_path):
                    raise GarbageCollectionError(
                        "compacted artifact changed while entering quarantine"
                    )
                return False

            try:
                _verify_regular_file_descriptor(
                    staged_path,
                    expected_sha256=expected_sha256,
                    expected_size_bytes=expected_size_bytes,
                )
            except Exception as verification_error:
                try:
                    _restore_staged_artifact(
                        self.data_root,
                        staged_path,
                        artifact_path,
                    )
                except Exception as restore_error:
                    raise GarbageCollectionError(
                        "compacted artifact verification failed and quarantine "
                        f"restore failed: {restore_error}"
                    ) from verification_error
                raise

            try:
                self._remove_file(staged_path)
                if _path_exists_no_follow(staged_path):
                    raise OSError("artifact remover returned without deleting")
            except (PermissionError, OSError):
                if _path_exists_no_follow(staged_path):
                    _restore_staged_artifact(
                        self.data_root,
                        staged_path,
                        artifact_path,
                    )
                raise
            return True
        finally:
            try:
                operation_dir.rmdir()
            except OSError:
                pass

    def _record_compacted_artifacts_collected(
        self,
        segment_ids: list[str],
    ) -> None:
        if not segment_ids:
            return
        connection = _open_catalog(self.catalog_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT OR IGNORE INTO retrieval_delta_artifact_gc (segment_id)
                VALUES (?)
                """,
                [(segment_id,) for segment_id in segment_ids],
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _validate_catalog_for_mutation(self) -> None:
        """Run one full suite at a standalone GC trust boundary."""

        from src.retrieval import publication as publication_module

        connection = _open_catalog(self.catalog_path, read_only=True)
        try:
            publication_module._validate_catalog_integrity(connection)
        except (sqlite3.Error, PublicationError) as exc:
            raise GarbageCollectionError(
                f"native catalog integrity validation failed: {exc}"
            ) from exc
        finally:
            connection.close()

    def _mark_snapshot_pending(self, snapshot_id: str) -> tuple[str, str]:
        connection = _open_catalog(self.catalog_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM publication_runs WHERE state = 'running' LIMIT 1"
                ).fetchone() is not None:
                    raise GarbageCollectionBlocked(
                        "snapshot GC cannot overlap a running publication"
                    )
                row = connection.execute(
                    """
                    SELECT snapshot.state, snapshot.relative_path,
                           runtime.active_snapshot_id,
                           runtime.predecessor_snapshot_id
                    FROM vector_snapshots AS snapshot
                    CROSS JOIN retrieval_runtime AS runtime
                    WHERE snapshot.snapshot_id = ? AND runtime.runtime_id = 1
                    """,
                    (snapshot_id,),
                ).fetchone()
                if row is None:
                    raise GarbageCollectionError("snapshot does not exist")
                state, relative_path, active, predecessor = row
                if snapshot_id in {active, predecessor}:
                    raise GarbageCollectionBlocked(
                        "active and verified predecessor snapshots cannot be collected"
                    )
                if state not in {
                    "ready",
                    "failed",
                    "garbage_pending",
                    "garbage_collected",
                }:
                    raise GarbageCollectionBlocked(
                        f"snapshot state {state!r} is not eligible for GC"
                    )
                if state in {"ready", "failed"}:
                    connection.execute(
                        """
                        UPDATE vector_snapshots
                        SET state = 'garbage_pending',
                            state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE snapshot_id = ?
                        """,
                        (snapshot_id,),
                    )
                    state = "garbage_pending"
                connection.commit()
                return str(state), str(relative_path)
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

def _open_catalog(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise GarbageCollectionError("native catalog is unavailable or unsafe")
    if read_only:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            isolation_level=None,
            timeout=5.0,
        )
    else:
        connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    try:
        try:
            configure_catalog_storage(connection, writable=not read_only)
        except SchemaError as exc:
            raise GarbageCollectionError(
                'native catalog storage mode is invalid'
            ) from exc
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _lexical_relative(data_root: Path, relative_path: str) -> Path:
    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or path.as_posix() != relative_path
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise GarbageCollectionError("artifact path is not a canonical relative path")
    return data_root.joinpath(*path.parts)


def _resolve_relative(data_root: Path, relative_path: str) -> Path:
    candidate = _lexical_relative(data_root, relative_path).resolve(strict=False)
    try:
        candidate.relative_to(data_root)
    except ValueError as exc:
        raise GarbageCollectionError("artifact path escapes the data root") from exc
    return candidate


def _path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _assert_safe_directory_chain(data_root: Path, directory: Path) -> None:
    try:
        relative = directory.relative_to(data_root)
    except ValueError as exc:
        raise GarbageCollectionError(
            "artifact parent escapes the data root"
        ) from exc
    current = data_root
    for part in relative.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError as exc:
            raise GarbageCollectionError(
                "artifact parent directory is missing"
            ) from exc
        if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(
            current_stat.st_mode
        ):
            raise GarbageCollectionError(
                "artifact parent directory is linked or not a directory"
            )


def _ensure_safe_staging_root(data_root: Path, staging_root: Path) -> None:
    _assert_safe_directory_chain(data_root, staging_root.parent)
    try:
        staging_root.mkdir()
    except FileExistsError:
        pass
    try:
        staging_stat = staging_root.lstat()
    except FileNotFoundError as exc:
        raise GarbageCollectionError("GC staging directory disappeared") from exc
    if stat.S_ISLNK(staging_stat.st_mode) or not stat.S_ISDIR(
        staging_stat.st_mode
    ):
        raise GarbageCollectionError("GC staging path is unsafe")


def _verify_regular_file_descriptor(
    path: Path,
    *,
    expected_sha256: str,
    expected_size_bytes: int,
) -> None:
    _validate_digest(expected_sha256, "compacted artifact")
    if (
        not isinstance(expected_size_bytes, int)
        or isinstance(expected_size_bytes, bool)
        or expected_size_bytes <= 0
    ):
        raise GarbageCollectionError(
            "compacted artifact size descriptor is invalid"
        )
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise GarbageCollectionError(
            "compacted artifact disappeared from quarantine"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise GarbageCollectionError(
            "compacted artifact became linked or non-regular"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(before, opened)
        ):
            raise GarbageCollectionError(
                "compacted artifact identity changed during verification"
            )
        if opened.st_size != expected_size_bytes:
            raise GarbageCollectionError(
                "compacted artifact size does not match its immutable descriptor"
            )
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
    finally:
        os.close(descriptor)

    try:
        after = path.lstat()
    except FileNotFoundError as exc:
        raise GarbageCollectionError(
            "compacted artifact disappeared during verification"
        ) from exc
    if not stat.S_ISREG(after.st_mode) or not os.path.samestat(opened, after):
        raise GarbageCollectionError(
            "compacted artifact identity changed after verification"
        )
    if digest.hexdigest() != expected_sha256:
        raise GarbageCollectionError(
            "compacted artifact hash does not match its immutable descriptor"
        )


def _restore_staged_artifact(
    data_root: Path,
    staged_path: Path,
    artifact_path: Path,
) -> None:
    """Restore a quarantined entry without overwriting a concurrent file."""

    _assert_safe_directory_chain(data_root, artifact_path.parent)
    if _path_exists_no_follow(artifact_path):
        raise GarbageCollectionError(
            "artifact path was repopulated while quarantine was active"
        )
    staged_stat = staged_path.lstat()
    if stat.S_ISREG(staged_stat.st_mode):
        os.link(staged_path, artifact_path, follow_symlinks=False)
        staged_path.unlink()
        return
    if stat.S_ISLNK(staged_stat.st_mode):
        artifact_path.symlink_to(os.readlink(staged_path))
        staged_path.unlink()
        return
    raise GarbageCollectionError(
        "quarantined artifact is not restorable without replacement"
    )


def _remove_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)
    except FileNotFoundError:
        return
    path.unlink()


def _validate_digest(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} ID must be a lowercase SHA-256 digest")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "CompactedArtifactGCOutcome",
    "GarbageCollectionBlocked",
    "GarbageCollectionError",
    "RetrievalGarbageCollector",
    "SnapshotGCOutcome",
]
