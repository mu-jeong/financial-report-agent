"""Crash-safe publication protocol for native V2 retrieval snapshots.

SQLite remains the only active-pointer authority.  Files written here are
immutable evidence or checkpoints: they are published at unique names without
replacement and are never interpreted as active pointers by filename.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping
from urllib.parse import quote

from .schema import (
    SchemaError,
    checkpoint_isolated_catalog,
    configure_catalog_storage,
    require_main_file_only,
)
from .vector_index import SnapshotDescriptor, VectorIndexError, load_index
from .writer_lock import NativeWriterLock, WriterLease, assert_writer_lease_owned


PHASES = (
    "journal_created",
    "catalog_written",
    "artifact_written",
    "artifact_durable",
    "artifact_published",
    "artifact_validated",
    "rollback_backup_validated",
    "commit_intent_durable",
    "committed_pending_checkpoint",
    "checkpoint_validated",
    "committed_floor_durable",
    "fully_complete",
)
_PHASE_NUMBER = {phase: number for number, phase in enumerate(PHASES)}


class PublicationError(RuntimeError):
    """Raised when a publication cannot be proved safe."""


class PublicationCrash(PublicationError):
    """Deterministic test-only termination at a durable protocol boundary."""

    def __init__(self, boundary: str):
        self.boundary = boundary
        super().__init__(f"injected publication crash after {boundary}")


@dataclass(frozen=True)
class PublicationRequest:
    publication_id: str
    to_snapshot_id: str
    evidence_manifest_relative_path: str
    evidence_manifest_sha256: str
    increment_write_epoch: bool = True
    enable_writes_on_complete: bool = True
    allow_active_snapshot_promotion: bool = False


@dataclass(frozen=True)
class PublicationOutcome:
    publication_id: str
    publication_generation: int
    write_epoch: int
    active_snapshot_id: str
    predecessor_snapshot_id: str | None
    v1_fallback_open: bool
    checkpoint_relative_path: str
    checkpoint_sha256: str
    committed_floor_relative_path: str
    cleanup_pending: bool = False
    cleanup_error: str | None = None


@dataclass(frozen=True)
class DurableFloor:
    publication_id: str
    publication_generation: int
    write_epoch: int
    v1_fallback_floor: str
    active_snapshot_id: str
    checkpoint_relative_path: str
    checkpoint_sha256: str
    path: Path

    @property
    def fallback_open(self) -> bool:
        return self.v1_fallback_floor == "open"


@dataclass(frozen=True)
class _CommitIntent:
    publication_id: str
    target_publication_generation: int
    old_write_epoch: int
    new_write_epoch: int
    v1_fallback_floor: str
    from_snapshot_id: str | None
    to_snapshot_id: str
    to_build_id: str
    snapshot_sha256: str
    enable_writes_on_complete: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "native_publication",
            "publication_id": self.publication_id,
            "target_publication_generation": self.target_publication_generation,
            "old_write_epoch": self.old_write_epoch,
            "new_write_epoch": self.new_write_epoch,
            "v1_fallback_floor": self.v1_fallback_floor,
            "from_snapshot_id": self.from_snapshot_id,
            "to_snapshot_id": self.to_snapshot_id,
            "to_build_id": self.to_build_id,
            "snapshot_sha256": self.snapshot_sha256,
            "enable_writes_on_complete": self.enable_writes_on_complete,
        }


CrashHook = Callable[[str], None]


class PublicationCoordinator:
    """Complete or replay one ready whole-corpus snapshot publication."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        catalog_relative_path: str = "retrieval/v2/catalog.sqlite3",
    ) -> None:
        self.data_root = Path(data_root).resolve(strict=True)
        self.catalog_relative_path = _normalize_relative_path(catalog_relative_path)
        self.catalog_path = _resolve_relative(
            self.data_root, self.catalog_relative_path
        )

    def publish(
        self,
        request: PublicationRequest,
        *,
        crash_after: str | None = None,
        crash_hook: CrashHook | None = None,
        writer_lease: WriterLease | None = None,
    ) -> PublicationOutcome:
        """Publish a ready candidate and durably close its compatibility floor.

        ``crash_after`` exists solely for deterministic crash-matrix tests.  It
        raises only after the named durable boundary, except
        ``during_pointer_transaction`` which raises inside the transaction and
        therefore proves rollback behavior.
        """

        if writer_lease is None:
            with NativeWriterLock(self.data_root) as owned_lease:
                return self.publish(
                    request,
                    crash_after=crash_after,
                    crash_hook=crash_hook,
                    writer_lease=owned_lease,
                )
        assert_writer_lease_owned(writer_lease, self.data_root)
        _validate_request(request)
        if crash_after is not None and crash_after not in {
            *PHASES,
            "commit_intent_written",
            "checkpoint_created",
            "before_pointer_transaction",
            "during_pointer_transaction",
            "before_fully_complete",
        }:
            raise ValueError(f"unknown crash boundary: {crash_after}")

        connection = _open_catalog(self.catalog_path, read_only=False)
        try:
            _validate_catalog_integrity(connection)
            existing = connection.execute(
                "SELECT * FROM publication_runs WHERE publication_id = ?",
                (request.publication_id,),
            ).fetchone()
            if existing is None and connection.execute(
                "SELECT 1 FROM vector_snapshots "
                "WHERE state = 'garbage_pending' LIMIT 1"
            ).fetchone() is not None:
                raise PublicationError(
                    "pending snapshot garbage must reconcile before publication"
                )
            _validate_runtime_floor(self.data_root, connection, existing)
            if existing is None or str(existing["state"]) != "fully_complete":
                _validate_first_successor_new_member(connection, request)
            if existing is None:
                # A lease already held before publication is an ordinary
                # preflight rejection, not an interrupted durable workflow.
                # Recheck immediately before the pointer transaction below to
                # close the concurrent-reader race after journal creation.
                _preflight_retiring_predecessor(connection, self.data_root)
            row = self._ensure_journal(connection, request)
            self._crash("journal_created", crash_after, crash_hook)
            self._validate_request_against_journal(connection, row, request)

            if str(row["state"]) == "failed":
                raise PublicationError("failed publication journals cannot be replayed")
            if str(row["state"]) == "fully_complete":
                return self._completed_outcome(connection, request.publication_id)

            phase = str(row["phase"])
            snapshot = _read_snapshot(connection, request.to_snapshot_id)
            self._advance_candidate_phases(
                connection,
                request.publication_id,
                request.to_snapshot_id,
                phase,
                crash_after,
                crash_hook,
            )

            intent = self._load_or_create_intent(
                connection,
                request,
                snapshot,
                crash_after,
                crash_hook,
            )
            row = _read_publication(connection, request.publication_id)
            phase = str(row["phase"])

            if _phase_before(phase, "committed_pending_checkpoint"):
                _preflight_retiring_predecessor(connection, self.data_root)
                self._crash(
                    "before_pointer_transaction", crash_after, crash_hook
                )
                self._commit_active_pointer(
                    connection,
                    intent,
                    crash_inside=(crash_after == "during_pointer_transaction"),
                )
                self._crash(
                    "committed_pending_checkpoint", crash_after, crash_hook
                )
            else:
                _validate_committed_pointer(connection, intent)

            checkpoint_relative = _checkpoint_relative_path(intent)
            checkpoint_path = _resolve_relative(self.data_root, checkpoint_relative)
            checkpoint_hash = _create_or_validate_checkpoint(
                connection,
                checkpoint_path,
                data_root=self.data_root,
                expected_intent=intent,
            )
            self._crash("checkpoint_created", crash_after, crash_hook)

            row = _read_publication(connection, request.publication_id)
            if _phase_before(str(row["phase"]), "checkpoint_validated"):
                _set_phase(connection, request.publication_id, "checkpoint_validated")
            self._crash("checkpoint_validated", crash_after, crash_hook)

            floor = self._write_or_validate_floor(
                connection,
                intent,
                checkpoint_relative,
                checkpoint_hash,
            )
            row = _read_publication(connection, request.publication_id)
            if _phase_before(str(row["phase"]), "committed_floor_durable"):
                _set_phase(
                    connection,
                    request.publication_id,
                    "committed_floor_durable",
                )
            self._crash("committed_floor_durable", crash_after, crash_hook)
            self._mark_epoch_zero_bundle_cleanup_pending(connection, intent)
            self._crash("before_fully_complete", crash_after, crash_hook)

            row = _read_publication(connection, request.publication_id)
            if str(row["state"]) != "fully_complete":
                self._finish_publication(connection, intent)
            self._crash("fully_complete", crash_after, crash_hook)
            outcome = _outcome_from_floor(connection, floor)
            cleanup_error = _reconcile_retired_snapshots(
                self.data_root,
                writer_lease=writer_lease,
            )
            if cleanup_error is not None:
                outcome = replace(
                    outcome,
                    cleanup_pending=True,
                    cleanup_error=cleanup_error,
                )
            return outcome
        finally:
            connection.close()

    def _mark_epoch_zero_bundle_cleanup_pending(
        self,
        connection: sqlite3.Connection,
        intent: _CommitIntent,
    ) -> None:
        if intent.old_write_epoch != 0 or intent.new_write_epoch <= 0:
            return
        seed = connection.execute(
            """
            SELECT evidence_manifest_relative_path
            FROM publication_runs
            WHERE to_snapshot_id = ? AND state = 'fully_complete'
            ORDER BY created_at DESC, publication_id DESC
            LIMIT 1
            """,
            (intent.from_snapshot_id,),
        ).fetchone()
        if seed is None or not seed[0]:
            raise PublicationError("epoch-zero seed compatibility evidence is missing")
        evidence_path = _resolve_relative(self.data_root, str(seed[0]))
        evidence = _read_json(evidence_path)
        bundle_id = evidence.get("compatibility_bundle_id")
        if (
            not isinstance(bundle_id, str)
            or len(bundle_id) != 64
            or any(character not in "0123456789abcdef" for character in bundle_id)
        ):
            raise PublicationError("epoch-zero compatibility bundle identity is invalid")
        bundle = self.data_root / "retrieval" / "compat" / "v1" / bundle_id
        if not bundle.is_dir() or bundle.is_symlink():
            raise PublicationError("epoch-zero compatibility bundle is unavailable")
        _atomic_json_once(
            bundle / "cleanup-pending.json",
            {
                "schema_version": 1,
                "state": "cleanup_pending",
                "bundle_id": bundle_id,
                "closing_publication_id": intent.publication_id,
                "publication_generation": intent.target_publication_generation,
                "write_epoch": intent.new_write_epoch,
            },
        )

    def request_from_journal(self, publication_id: str) -> PublicationRequest:
        """Reconstruct the immutable request fields needed for startup replay."""

        connection = _open_catalog(self.catalog_path, read_only=True)
        try:
            _validate_catalog_integrity(connection)
            row = _read_publication(connection, publication_id)
            intent = _read_commit_intent_if_present(self.data_root, publication_id)
            return PublicationRequest(
                publication_id=publication_id,
                to_snapshot_id=str(row["to_snapshot_id"]),
                evidence_manifest_relative_path=str(
                    row["evidence_manifest_relative_path"]
                ),
                evidence_manifest_sha256=str(row["evidence_manifest_sha256"]),
                increment_write_epoch=(
                    True
                    if intent is None
                    else intent.new_write_epoch > intent.old_write_epoch
                ),
                enable_writes_on_complete=(
                    True
                    if intent is None
                    else intent.enable_writes_on_complete
                ),
                allow_active_snapshot_promotion=(
                    row["from_snapshot_id"] is not None
                    and row["from_snapshot_id"] == row["to_snapshot_id"]
                ),
            )
        finally:
            connection.close()

    def _ensure_journal(
        self,
        connection: sqlite3.Connection,
        request: PublicationRequest,
    ) -> sqlite3.Row:
        existing = connection.execute(
            "SELECT * FROM publication_runs WHERE publication_id = ?",
            (request.publication_id,),
        ).fetchone()
        if existing is not None:
            return existing

        evidence = _validate_evidence_manifest(self.data_root, request)

        def create_journal() -> None:
            runtime = _read_runtime(connection)
            _validate_candidate_evidence_lineage(
                connection,
                request,
                evidence,
                expected_from_snapshot_id=runtime["active_snapshot_id"],
                live_runtime=runtime,
            )
            connection.execute(
                """
                INSERT INTO publication_runs (
                    publication_id,
                    from_snapshot_id,
                    to_snapshot_id,
                    evidence_manifest_relative_path,
                    evidence_manifest_sha256
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request.publication_id,
                    runtime["active_snapshot_id"],
                    request.to_snapshot_id,
                    request.evidence_manifest_relative_path,
                    request.evidence_manifest_sha256.lower(),
                ),
            )

        _transaction(connection, create_journal)
        return _read_publication(connection, request.publication_id)

    def _validate_request_against_journal(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        request: PublicationRequest,
    ) -> None:
        expected = (
            request.to_snapshot_id,
            request.evidence_manifest_relative_path,
            request.evidence_manifest_sha256.lower(),
        )
        actual = (
            row["to_snapshot_id"],
            row["evidence_manifest_relative_path"],
            row["evidence_manifest_sha256"],
        )
        if actual != expected:
            raise PublicationError(
                "publication replay does not match its immutable journal"
            )
        evidence = _validate_evidence_manifest(self.data_root, request)
        live_runtime = None
        if (
            str(row["state"]) == "running"
            and _phase_before(str(row["phase"]), "committed_pending_checkpoint")
        ):
            live_runtime = _read_runtime(connection)
        _validate_candidate_evidence_lineage(
            connection,
            request,
            evidence,
            expected_from_snapshot_id=row["from_snapshot_id"],
            live_runtime=live_runtime,
        )

    def _advance_candidate_phases(
        self,
        connection: sqlite3.Connection,
        publication_id: str,
        snapshot_id: str,
        phase: str,
        crash_after: str | None,
        crash_hook: CrashHook | None,
    ) -> None:
        for next_phase in (
            "catalog_written",
            "artifact_written",
            "artifact_durable",
            "artifact_published",
        ):
            if _phase_before(phase, next_phase):
                _set_phase(connection, publication_id, next_phase)
                phase = next_phase
            self._crash(next_phase, crash_after, crash_hook)

        _validate_snapshot(
            connection,
            self.data_root,
            snapshot_id,
            allowed_build_states={
                "ready",
                "committed_pending_checkpoint",
                "fully_complete",
            },
            allowed_snapshot_states={"ready"},
        )
        if _phase_before(phase, "artifact_validated"):
            _set_phase(connection, publication_id, "artifact_validated")
            phase = "artifact_validated"
        self._crash("artifact_validated", crash_after, crash_hook)

        if _phase_before(phase, "rollback_backup_validated"):
            runtime = _read_runtime(connection)
            rollback_relative = _rollback_relative_path(
                publication_id,
                int(runtime["publication_generation"]),
            )
            rollback_path = _resolve_relative(self.data_root, rollback_relative)
            _create_or_validate_backup(
                connection,
                rollback_path,
                expected_generation=int(runtime["publication_generation"]),
            )
            _set_phase(connection, publication_id, "rollback_backup_validated")
        self._crash("rollback_backup_validated", crash_after, crash_hook)

    def _load_or_create_intent(
        self,
        connection: sqlite3.Connection,
        request: PublicationRequest,
        snapshot: sqlite3.Row,
        crash_after: str | None,
        crash_hook: CrashHook | None,
    ) -> _CommitIntent:
        existing = _read_commit_intent_if_present(
            self.data_root, request.publication_id
        )
        if existing is not None:
            if (
                existing.to_snapshot_id != request.to_snapshot_id
                or existing.enable_writes_on_complete
                != request.enable_writes_on_complete
            ):
                raise PublicationError("commit intent conflicts with replay request")
            intent = existing
        else:
            runtime = _read_runtime(connection)
            new_epoch = int(runtime["write_epoch"]) + int(
                request.increment_write_epoch
            )
            fallback = (
                "closed"
                if new_epoch > 0 or not bool(runtime["v1_fallback_open"])
                else "open"
            )
            intent = _CommitIntent(
                publication_id=request.publication_id,
                target_publication_generation=int(
                    runtime["publication_generation"]
                )
                + 1,
                old_write_epoch=int(runtime["write_epoch"]),
                new_write_epoch=new_epoch,
                v1_fallback_floor=fallback,
                from_snapshot_id=(
                    None
                    if runtime["active_snapshot_id"] is None
                    else str(runtime["active_snapshot_id"])
                ),
                to_snapshot_id=request.to_snapshot_id,
                to_build_id=str(snapshot["build_id"]),
                snapshot_sha256=str(snapshot["file_sha256"]),
                enable_writes_on_complete=request.enable_writes_on_complete,
            )
            path = _intent_path(self.data_root, request.publication_id)
            _atomic_json_once(path, intent.as_json())

        self._crash("commit_intent_written", crash_after, crash_hook)
        _validate_intent_against_catalog(connection, request, snapshot, intent)

        row = _read_publication(connection, request.publication_id)
        if _phase_before(str(row["phase"]), "commit_intent_durable"):
            _set_phase(connection, request.publication_id, "commit_intent_durable")
        self._crash("commit_intent_durable", crash_after, crash_hook)
        return intent

    def _commit_active_pointer(
        self,
        connection: sqlite3.Connection,
        intent: _CommitIntent,
        *,
        crash_inside: bool,
    ) -> None:
        runtime = _read_runtime(connection)
        retiring_predecessor = runtime["predecessor_snapshot_id"]
        expected_runtime = (
            intent.target_publication_generation - 1,
            intent.old_write_epoch,
            intent.from_snapshot_id,
        )
        actual_runtime = (
            int(runtime["publication_generation"]),
            int(runtime["write_epoch"]),
            runtime["active_snapshot_id"],
        )
        if actual_runtime != expected_runtime:
            raise PublicationError("runtime changed after commit intent became durable")
        promotes_active_snapshot = intent.from_snapshot_id == intent.to_snapshot_id
        if promotes_active_snapshot and retiring_predecessor is not None:
            raise PublicationError("epoch-zero seed activation cannot retire a predecessor")

        def commit() -> None:
            if promotes_active_snapshot:
                state = connection.execute(
                    "SELECT state FROM retrieval_builds WHERE build_id = ?",
                    (intent.to_build_id,),
                ).fetchone()
                if state is None or str(state[0]) != "fully_complete":
                    raise PublicationError(
                        "epoch-zero seed activation requires a fully complete build"
                    )
            else:
                connection.execute(
                    """
                    UPDATE retrieval_builds
                    SET state = 'committed_pending_checkpoint',
                        state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE build_id = ? AND state = 'ready'
                    """,
                    (intent.to_build_id,),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PublicationError("target build is not ready for commit")
            connection.execute(
                """
                UPDATE retrieval_runtime
                SET active_snapshot_id = ?, active_build_id = ?,
                    predecessor_snapshot_id = ?,
                    publication_generation = ?, write_epoch = ?,
                    v1_fallback_open = ?, degraded = 0, write_enabled = 0,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE runtime_id = 1
                """,
                (
                    intent.to_snapshot_id,
                    intent.to_build_id,
                    None if promotes_active_snapshot else intent.from_snapshot_id,
                    intent.target_publication_generation,
                    intent.new_write_epoch,
                    int(intent.v1_fallback_floor == "open"),
                ),
            )
            connection.execute(
                """
                UPDATE publication_runs
                SET phase = 'committed_pending_checkpoint',
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE publication_id = ?
                """,
                (intent.publication_id,),
            )
            if retiring_predecessor is not None and not promotes_active_snapshot:
                connection.execute(
                    """
                    UPDATE vector_snapshots
                    SET state = 'garbage_pending',
                        state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE snapshot_id = ? AND state IN ('ready', 'failed')
                    """,
                    (retiring_predecessor,),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise PublicationError(
                        "retiring predecessor could not enter garbage_pending"
                    )
            if crash_inside:
                raise PublicationCrash("during_pointer_transaction")

        _transaction(connection, commit)

    def _write_or_validate_floor(
        self,
        connection: sqlite3.Connection,
        intent: _CommitIntent,
        checkpoint_relative: str,
        checkpoint_hash: str,
    ) -> DurableFloor:
        payload = {
            "schema_version": 1,
            "publication_id": intent.publication_id,
            "publication_generation": intent.target_publication_generation,
            "write_epoch": intent.new_write_epoch,
            "v1_fallback_floor": intent.v1_fallback_floor,
            "active_snapshot_id": intent.to_snapshot_id,
            "checkpoint_relative_path": checkpoint_relative,
            "checkpoint_sha256": checkpoint_hash,
        }
        existing_floors = read_durable_floors(self.data_root)
        _validate_new_floor(payload, existing_floors)
        path = _floor_path(self.data_root, intent.publication_id)
        _atomic_json_once(path, payload)
        return _parse_floor(path, _read_json(path))

    def _finish_publication(
        self,
        connection: sqlite3.Connection,
        intent: _CommitIntent,
    ) -> None:
        def finish() -> None:
            connection.execute(
                """
                UPDATE retrieval_builds
                SET state = 'fully_complete',
                    state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE build_id = ? AND state = 'committed_pending_checkpoint'
                """,
                (intent.to_build_id,),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                state = connection.execute(
                    "SELECT state FROM retrieval_builds WHERE build_id = ?",
                    (intent.to_build_id,),
                ).fetchone()
                if state is None or str(state[0]) != "fully_complete":
                    raise PublicationError("target build cannot become fully complete")
            connection.execute(
                """
                UPDATE retrieval_runtime
                SET degraded = 0, write_enabled = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE runtime_id = 1
                  AND publication_generation = ?
                  AND active_snapshot_id = ?
                """,
                (
                    int(intent.enable_writes_on_complete),
                    intent.target_publication_generation,
                    intent.to_snapshot_id,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise PublicationError(
                    "runtime no longer matches committed publication"
                )
            connection.execute(
                """
                UPDATE publication_runs
                SET phase = 'fully_complete', state = 'fully_complete',
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE publication_id = ? AND state = 'running'
                """,
                (intent.publication_id,),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                state = connection.execute(
                    "SELECT state FROM publication_runs WHERE publication_id = ?",
                    (intent.publication_id,),
                ).fetchone()
                if state != ("fully_complete",):
                    raise PublicationError("publication journal cannot be completed")

        _transaction(connection, finish)

    def _completed_outcome(
        self,
        connection: sqlite3.Connection,
        publication_id: str,
    ) -> PublicationOutcome:
        floor_path = _floor_path(self.data_root, publication_id)
        if not floor_path.is_file():
            raise PublicationError("fully complete publication has no committed floor")
        floor = _parse_floor(floor_path, _read_json(floor_path))
        _validate_checkpoint_for_floor(self.data_root, floor)
        return _outcome_from_floor(connection, floor)

    @staticmethod
    def _crash(
        boundary: str,
        crash_after: str | None,
        crash_hook: CrashHook | None,
    ) -> None:
        if crash_hook is not None:
            crash_hook(boundary)
        if crash_after == boundary:
            raise PublicationCrash(boundary)


def activate_epoch_zero_seed(
    data_root: str | Path,
    *,
    snapshot_id: str,
    canary: Mapping[str, Any],
    writer_lease: WriterLease | None = None,
) -> PublicationOutcome:
    """Enable native writes while keeping the converted seed snapshot active."""

    root = Path(data_root).resolve(strict=True)
    if writer_lease is None:
        with NativeWriterLock(root) as owned_lease:
            return activate_epoch_zero_seed(
                root,
                snapshot_id=snapshot_id,
                canary=canary,
                writer_lease=owned_lease,
            )
    assert_writer_lease_owned(writer_lease, root)
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("snapshot_id must be a non-empty string")

    coordinator = PublicationCoordinator(root)
    publication_id = hashlib.sha256(
        f"native-seed-activation\0{snapshot_id}".encode("utf-8")
    ).hexdigest()
    connection = _open_catalog(coordinator.catalog_path, read_only=True)
    try:
        _validate_catalog_integrity(connection)
        existing = connection.execute(
            "SELECT 1 FROM publication_runs WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        if existing is not None:
            request = coordinator.request_from_journal(publication_id)
            if (
                request.to_snapshot_id != snapshot_id
                or not request.allow_active_snapshot_promotion
            ):
                raise PublicationError("seed activation journal identity conflicts")
            return coordinator.publish(request, writer_lease=writer_lease)

        _validate_runtime_floor(root, connection, None)
        runtime = _read_runtime(connection)
        if (
            runtime["active_snapshot_id"] != snapshot_id
            or int(runtime["write_epoch"]) != 0
            or not bool(runtime["v1_fallback_open"])
            or bool(runtime["degraded"])
            or bool(runtime["write_enabled"])
            or runtime["predecessor_snapshot_id"] is not None
        ):
            raise PublicationError(
                "seed activation requires the healthy converted epoch-zero runtime"
            )
        snapshot = _validate_snapshot(
            connection,
            root,
            snapshot_id,
            allowed_build_states={"fully_complete"},
            allowed_snapshot_states={"ready"},
        )
        evidence = {
            "schema_version": 1,
            "kind": "native_seed_activation",
            "publication_id": publication_id,
            "base_publication_generation": int(runtime["publication_generation"]),
            "base_snapshot_id": snapshot_id,
            "base_write_epoch": 0,
            "build_id": str(snapshot["build_id"]),
            "snapshot_id": snapshot_id,
            "snapshot_file_sha256": str(snapshot["file_sha256"]),
            "same_space_canary": dict(canary),
        }
        _validate_seed_activation_canary(evidence, int(snapshot["dimension"]))
    finally:
        connection.close()

    evidence_relative = f"retrieval/v2/evidence/{publication_id}/manifest.json"
    evidence_path = _resolve_relative(root, evidence_relative)
    _atomic_json_once(evidence_path, evidence)
    evidence_path.chmod(stat.S_IREAD)
    return coordinator.publish(
        PublicationRequest(
            publication_id=publication_id,
            to_snapshot_id=snapshot_id,
            evidence_manifest_relative_path=evidence_relative,
            evidence_manifest_sha256=_sha256_file(evidence_path),
            increment_write_epoch=True,
            enable_writes_on_complete=True,
            allow_active_snapshot_promotion=True,
        ),
        writer_lease=writer_lease,
    )


def publish_immutable_artifact(
    staged_path: str | Path,
    final_path: str | Path,
    descriptor: SnapshotDescriptor,
) -> None:
    """Publish validated bytes at a unique same-volume name without overwrite."""

    staged_input = Path(staged_path)
    if staged_input.is_symlink():
        raise PublicationError("staged artifacts must not be symlinks")
    staged = staged_input.resolve(strict=True)
    final = Path(final_path).resolve(strict=False)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        raise FileExistsError(f"publication target already exists: {final.name}")
    if staged.anchor.casefold() != final.anchor.casefold():
        raise PublicationError("publication staging and final path must share a volume")
    if staged.stat().st_dev != final.parent.stat().st_dev:
        raise PublicationError("publication staging and final path must share a volume")

    with staged.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    load_index(staged, descriptor)
    try:
        os.link(staged, final)
    except FileExistsError:
        raise FileExistsError(
            f"publication target already exists: {final.name}"
        ) from None
    except OSError as exc:
        raise PublicationError(
            f"atomic non-overwriting publication failed: {exc}"
        ) from exc
    _fsync_directory(final.parent)
    try:
        load_index(final, descriptor)
    except Exception as validation_error:
        try:
            final.unlink()
        except OSError as cleanup_error:
            raise PublicationError(
                "published artifact validation failed and cleanup failed: "
                f"{cleanup_error}"
            ) from validation_error
        raise
    staged.unlink()


def _preflight_retiring_predecessor(
    connection: sqlite3.Connection,
    data_root: Path,
) -> None:
    """Close an unleased predecessor handle before its pointer is replaced."""

    runtime = _read_runtime(connection)
    predecessor = runtime["predecessor_snapshot_id"]
    if predecessor is None:
        return
    from src.retrieval.repository import (
        RepositoryError,
        SnapshotInUseError,
        shared_snapshot_cache,
    )

    try:
        shared_snapshot_cache(data_root).evict_snapshot(str(predecessor))
    except SnapshotInUseError as exc:
        raise PublicationError(
            "verified predecessor is still leased; publication is blocked"
        ) from exc
    except (RepositoryError, PermissionError, OSError) as exc:
        raise PublicationError(
            "verified predecessor handle could not be closed before publication"
        ) from exc


def _validate_first_successor_new_member(
    connection: sqlite3.Connection,
    request: PublicationRequest,
) -> None:
    """Allow only the atomic first-successor transition from the V1 bridge."""

    runtime = _read_runtime(connection)
    if (
        int(runtime["write_epoch"]) != 0
        or not bool(runtime["v1_fallback_open"])
    ):
        return
    if not request.increment_write_epoch:
        raise PublicationError(
            "epoch-zero publication must increment write epoch and close V1 fallback"
        )
    if not request.enable_writes_on_complete:
        raise PublicationError(
            "first native successor must enable writes when publication completes"
        )
    active_snapshot = runtime["active_snapshot_id"]
    if active_snapshot is None:
        raise PublicationError("epoch-zero runtime has no converted active snapshot")
    if request.to_snapshot_id == active_snapshot:
        if not request.allow_active_snapshot_promotion:
            raise PublicationError(
                "active epoch-zero snapshot requires the dedicated seed activation protocol"
            )
        if runtime["predecessor_snapshot_id"] is not None:
            raise PublicationError("epoch-zero seed activation cannot have a predecessor")
        return
    if request.allow_active_snapshot_promotion:
        raise PublicationError(
            "seed activation must keep the converted active snapshot selected"
        )

    def report_uids(snapshot_id: str) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT report.report_uid
                FROM snapshot_membership AS membership
                JOIN retrieval_chunks AS chunk
                  ON chunk.chunk_uid = membership.chunk_uid
                JOIN retrieval_parents AS parent
                  ON parent.parent_uid = chunk.parent_uid
                 AND parent.profile_id = chunk.profile_id
                JOIN reports AS report ON report.report_id = parent.report_id
                WHERE membership.snapshot_id = ?
                """,
                (snapshot_id,),
            )
        }

    if not report_uids(request.to_snapshot_id).difference(
        report_uids(str(active_snapshot))
    ):
        raise PublicationError(
            "first native successor must include at least one new logical corpus member"
        )


def _reconcile_retired_snapshots(
    data_root: Path,
    *,
    writer_lease: WriterLease,
) -> str | None:
    """Finish snapshot and compacted-child deletion after a durable commit."""

    from src.retrieval.garbage_collector import (
        GarbageCollectionError,
        RetrievalGarbageCollector,
    )

    try:
        collector = RetrievalGarbageCollector(data_root)
        collector._reconcile_pending_snapshots_after_validation(
            writer_lease=writer_lease,
        )
    except (GarbageCollectionError, OSError, sqlite3.Error) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def read_durable_floors(data_root: str | Path) -> tuple[DurableFloor, ...]:
    """Read and validate the complete append-only compatibility-floor chain."""

    root = Path(data_root).resolve(strict=True)
    evidence_root = root / "retrieval" / "v2" / "evidence"
    if not evidence_root.exists():
        return ()
    floors = [
        _parse_floor(path, _read_json(path))
        for path in sorted(evidence_root.glob("*/committed-floor.json"))
    ]
    ordered = sorted(floors, key=lambda item: item.publication_generation)
    seen_generations: set[int] = set()
    previous_epoch = -1
    previous_closed = False
    for floor in ordered:
        if floor.publication_generation in seen_generations:
            raise PublicationError("durable floors contain a duplicate generation")
        seen_generations.add(floor.publication_generation)
        if floor.write_epoch < previous_epoch:
            raise PublicationError("durable floor write epoch moved backward")
        if previous_closed and floor.fallback_open:
            raise PublicationError("durable floor attempted to reopen V1 fallback")
        previous_epoch = floor.write_epoch
        previous_closed = not floor.fallback_open
    return tuple(ordered)


def read_commit_intents(data_root: str | Path) -> tuple[Mapping[str, Any], ...]:
    """Return all final (never temporary) commit-intent records."""

    root = Path(data_root).resolve(strict=True)
    evidence_root = root / "retrieval" / "v2" / "evidence"
    if not evidence_root.exists():
        return ()
    return tuple(
        _read_json(path)
        for path in sorted(evidence_root.glob("*/commit-intent.json"))
    )


def _open_catalog(
    path: Path,
    *,
    read_only: bool,
    immutable: bool = False,
) -> sqlite3.Connection:
    if not path.is_file():
        raise PublicationError("native retrieval catalog is missing")
    if immutable and not read_only:
        raise PublicationError("immutable catalog opens must be read-only")
    if read_only:
        if immutable:
            try:
                require_main_file_only(path)
            except SchemaError as exc:
                raise PublicationError(
                    "immutable catalog copy is not main-file-only"
                ) from exc
            try:
                with path.open("rb") as stream:
                    header = stream.read(20)
            except OSError as exc:
                raise PublicationError("immutable catalog header is unreadable") from exc
            if len(header) < 20 or header[18:20] != b"\x02\x02":
                raise PublicationError("immutable native catalog is not WAL-formatted")
        immutable_query = "&immutable=1" if immutable else ""
        uri = (
            f"file:{quote(path.as_posix(), safe=':/')}?mode=ro"
            f"{immutable_query}"
        )
        connection = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=5.0,
        )
    else:
        connection = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        if not immutable:
            try:
                configure_catalog_storage(connection, writable=not read_only)
            except SchemaError as exc:
                raise PublicationError('native catalog storage mode is invalid') from exc
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _validate_catalog_integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise PublicationError("catalog quick_check failed")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise PublicationError("catalog integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise PublicationError("catalog foreign-key validation failed")


def _read_runtime(connection: sqlite3.Connection) -> sqlite3.Row:
    rows = connection.execute(
        "SELECT * FROM retrieval_runtime WHERE runtime_id = 1"
    ).fetchall()
    if len(rows) != 1:
        raise PublicationError("exactly one retrieval runtime row is required")
    return rows[0]


def _read_publication(
    connection: sqlite3.Connection, publication_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM publication_runs WHERE publication_id = ?",
        (publication_id,),
    ).fetchone()
    if row is None:
        raise PublicationError("publication journal is missing")
    return row


def _read_snapshot(connection: sqlite3.Connection, snapshot_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM vector_snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        raise PublicationError("target snapshot is missing from the catalog")
    return row


def _set_phase(
    connection: sqlite3.Connection, publication_id: str, phase: str
) -> None:
    if phase not in _PHASE_NUMBER:
        raise ValueError(f"unknown publication phase: {phase}")

    def update() -> None:
        connection.execute(
            """
            UPDATE publication_runs
            SET phase = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE publication_id = ? AND state = 'running'
            """,
            (phase, publication_id),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise PublicationError("publication journal is not running")

    _transaction(connection, update)


def _phase_before(current: str, target: str) -> bool:
    try:
        return _PHASE_NUMBER[current] < _PHASE_NUMBER[target]
    except KeyError as exc:
        raise PublicationError(f"unknown publication phase: {exc.args[0]}") from exc


def _transaction(connection: sqlite3.Connection, operation: Callable[[], Any]) -> Any:
    connection.execute("BEGIN IMMEDIATE")
    try:
        value = operation()
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
        return value


def _validate_request(request: PublicationRequest) -> None:
    for label, value in (
        ("publication_id", request.publication_id),
        ("to_snapshot_id", request.to_snapshot_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string")
        if (
            value in {".", ".."}
            or value != value.strip()
            or value.endswith(".")
            or any(character in value for character in '<>:"/\\|?*\0')
        ):
            raise ValueError(f"{label} must be a single safe path component")
    _normalize_relative_path(request.evidence_manifest_relative_path)
    _validate_sha256(request.evidence_manifest_sha256, "evidence manifest")
    if not isinstance(request.increment_write_epoch, bool):
        raise TypeError("increment_write_epoch must be a bool")
    if not isinstance(request.enable_writes_on_complete, bool):
        raise TypeError("enable_writes_on_complete must be a bool")
    if not isinstance(request.allow_active_snapshot_promotion, bool):
        raise TypeError("allow_active_snapshot_promotion must be a bool")
    if request.allow_active_snapshot_promotion and (
        not request.increment_write_epoch or not request.enable_writes_on_complete
    ):
        raise ValueError(
            "active snapshot promotion must increment the epoch and enable writes"
        )


def _validate_evidence_manifest(
    data_root: Path, request: PublicationRequest
) -> Mapping[str, Any]:
    path = _resolve_relative(data_root, request.evidence_manifest_relative_path)
    if not path.is_file() or path.is_symlink():
        raise PublicationError("publication evidence manifest is missing or unsafe")
    if _sha256_file(path) != request.evidence_manifest_sha256.lower():
        raise PublicationError("publication evidence manifest hash does not match")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError("publication evidence manifest is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PublicationError("publication evidence manifest must be an object")
    return value


def _validate_candidate_evidence_lineage(
    connection: sqlite3.Connection,
    request: PublicationRequest,
    evidence: Mapping[str, Any],
    *,
    expected_from_snapshot_id: object,
    live_runtime: sqlite3.Row | None,
) -> None:
    snapshot = _read_snapshot(connection, request.to_snapshot_id)
    allowed_kinds = (
        {"native_seed_activation"}
        if request.allow_active_snapshot_promotion
        else {"native_full_corpus_candidate", "native_incremental_candidate"}
    )
    expected_identity = (
        1,
        request.publication_id,
        request.to_snapshot_id,
        str(snapshot["build_id"]),
    )
    actual_identity = (
        evidence.get("schema_version"),
        evidence.get("publication_id"),
        evidence.get("snapshot_id"),
        evidence.get("build_id"),
    )
    if actual_identity != expected_identity or evidence.get("kind") not in allowed_kinds:
        raise PublicationError(
            "publication evidence identity does not match the target candidate"
        )

    base_generation = evidence.get("base_publication_generation")
    base_epoch = evidence.get("base_write_epoch")
    if (
        not isinstance(base_generation, int)
        or isinstance(base_generation, bool)
        or base_generation < 0
        or not isinstance(base_epoch, int)
        or isinstance(base_epoch, bool)
        or base_epoch < 0
    ):
        raise PublicationError("publication evidence base lineage is invalid")
    base_snapshot_id = evidence.get("base_snapshot_id")
    if not isinstance(base_snapshot_id, str) or not base_snapshot_id:
        raise PublicationError("publication evidence base lineage is invalid")

    if live_runtime is not None:
        evidence_base = (base_snapshot_id, base_generation, base_epoch)
        runtime_base = (
            live_runtime["active_snapshot_id"],
            int(live_runtime["publication_generation"]),
            int(live_runtime["write_epoch"]),
        )
        if evidence_base != runtime_base:
            raise PublicationError(
                "publication evidence base lineage does not match live runtime"
            )
    if base_snapshot_id != expected_from_snapshot_id:
        raise PublicationError(
            "publication evidence base snapshot does not match its journal"
        )
    if request.allow_active_snapshot_promotion:
        _validate_seed_activation_canary(evidence, int(snapshot["dimension"]))


def _validate_seed_activation_canary(
    evidence: Mapping[str, Any],
    expected_dimension: int,
) -> None:
    canary = evidence.get("same_space_canary")
    if not isinstance(canary, Mapping):
        raise PublicationError("seed activation evidence has no same-space canary")
    sample_count = canary.get("sample_count")
    dimension = canary.get("dimension")
    self_rank_one_count = canary.get("self_rank_one_count")
    minimum_cosine = canary.get("minimum_cosine_similarity")
    maximum_norm_error = canary.get("maximum_norm_relative_error")
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count <= 0
        or dimension != expected_dimension
        or self_rank_one_count != sample_count
        or not isinstance(minimum_cosine, (int, float))
        or isinstance(minimum_cosine, bool)
        or not math.isfinite(float(minimum_cosine))
        or float(minimum_cosine) < 0.999
        or not isinstance(maximum_norm_error, (int, float))
        or isinstance(maximum_norm_error, bool)
        or not math.isfinite(float(maximum_norm_error))
        or float(maximum_norm_error) > 0.01
    ):
        raise PublicationError("seed activation same-space canary is invalid")


def _validate_snapshot(
    connection: sqlite3.Connection,
    data_root: Path,
    snapshot_id: str,
    *,
    allowed_build_states: set[str],
    allowed_snapshot_states: set[str],
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT snapshot.*, build.state AS build_state,
               profile.dimension AS profile_dimension,
               profile.metric AS profile_metric
        FROM vector_snapshots AS snapshot
        JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
        JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
        WHERE snapshot.snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise PublicationError("snapshot descriptor is missing")
    if str(row["state"]) not in allowed_snapshot_states:
        raise PublicationError("snapshot is not in a publishable state")
    if str(row["build_state"]) not in allowed_build_states:
        raise PublicationError("snapshot build is not in a publishable state")
    if (
        int(row["dimension"]) != int(row["profile_dimension"])
        or str(row["metric"]) != str(row["profile_metric"])
    ):
        raise PublicationError("snapshot descriptor does not match its profile")

    ntotal = int(row["ntotal"])
    membership = connection.execute(
        """
        SELECT count(*) AS count,
               min(faiss_id) AS minimum_id,
               max(faiss_id) AS maximum_id
        FROM snapshot_membership WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if int(membership["count"]) != ntotal:
        raise PublicationError("snapshot membership count does not match ntotal")
    if ntotal and (
        int(membership["minimum_id"]) != 1
        or int(membership["maximum_id"]) != ntotal
    ):
        raise PublicationError("snapshot membership IDs are not dense 1..N")

    path = _resolve_relative(data_root, str(row["relative_path"]))
    descriptor = SnapshotDescriptor(
        sha256=str(row["file_sha256"]),
        size_bytes=int(row["size_bytes"]),
        dimension=int(row["dimension"]),
        metric=str(row["metric"]),
        ntotal=ntotal,
    )
    try:
        loaded = load_index(path, descriptor)
        if loaded.physical_ids != tuple(range(1, ntotal + 1)):
            raise PublicationError("snapshot physical IDs do not match 1..N")
    except (FileNotFoundError, OSError, VectorIndexError) as exc:
        raise PublicationError(f"snapshot artifact validation failed: {exc}") from exc
    return row


def _create_or_validate_backup(
    source: sqlite3.Connection,
    target: Path,
    *,
    expected_generation: int,
) -> str:
    def validate(connection: sqlite3.Connection) -> None:
        _validate_catalog_integrity(connection)
        runtime = _read_runtime(connection)
        if int(runtime["publication_generation"]) != expected_generation:
            raise PublicationError("rollback backup has the wrong generation")

    return _create_or_validate_sqlite_copy(source, target, validate)


def _create_or_validate_checkpoint(
    source: sqlite3.Connection,
    target: Path,
    *,
    data_root: Path,
    expected_intent: _CommitIntent,
) -> str:
    def validate(connection: sqlite3.Connection) -> None:
        _validate_catalog_integrity(connection)
        _validate_committed_pointer(connection, expected_intent)
        journal = _read_publication(connection, expected_intent.publication_id)
        if (
            journal["to_snapshot_id"] != expected_intent.to_snapshot_id
            or _phase_before(
                str(journal["phase"]), "committed_pending_checkpoint"
            )
        ):
            raise PublicationError(
                "checkpoint publication journal conflicts with commit intent"
            )
        _validate_snapshot(
            connection,
            data_root,
            expected_intent.to_snapshot_id,
            allowed_build_states={"committed_pending_checkpoint", "fully_complete"},
            allowed_snapshot_states={"ready"},
        )

    return _create_or_validate_sqlite_copy(source, target, validate)


def _create_or_validate_sqlite_copy(
    source: sqlite3.Connection,
    target: Path,
    validator: Callable[[sqlite3.Connection], None],
) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        check = _open_catalog(target, read_only=True, immutable=True)
        try:
            validator(check)
        finally:
            check.close()
        return _sha256_file(target)

    # Do not repeat the content-addressed final basename in temporary files:
    # that can exceed MAX_PATH on otherwise supported Windows installs.
    temporary = target.parent / f".sqlite-{uuid.uuid4().hex[:12]}.tmp"
    try:
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
            destination.commit()
            try:
                checkpoint_isolated_catalog(destination)
            except SchemaError as exc:
                raise PublicationError(
                    'isolated catalog checkpoint did not become durable'
                ) from exc
        finally:
            destination.close()
        try:
            require_main_file_only(temporary)
        except SchemaError as exc:
            raise PublicationError(
                'isolated catalog copy is not main-file-only'
            ) from exc
        _fsync_file(temporary)
        check = _open_catalog(temporary, read_only=True, immutable=True)
        try:
            validator(check)
        finally:
            check.close()
        _link_without_overwrite(temporary, target)
        return _sha256_file(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_committed_pointer(
    connection: sqlite3.Connection, intent: _CommitIntent
) -> None:
    runtime = _read_runtime(connection)
    actual = (
        int(runtime["publication_generation"]),
        int(runtime["write_epoch"]),
        bool(runtime["v1_fallback_open"]),
        runtime["active_snapshot_id"],
        runtime["active_build_id"],
        runtime["predecessor_snapshot_id"],
    )
    expected_predecessor = (
        None
        if intent.from_snapshot_id == intent.to_snapshot_id
        else intent.from_snapshot_id
    )
    expected = (
        intent.target_publication_generation,
        intent.new_write_epoch,
        intent.v1_fallback_floor == "open",
        intent.to_snapshot_id,
        intent.to_build_id,
        expected_predecessor,
    )
    if actual != expected:
        raise PublicationError("committed runtime pointer conflicts with its intent")
    snapshot = _read_snapshot(connection, intent.to_snapshot_id)
    if (
        snapshot["build_id"] != intent.to_build_id
        or snapshot["file_sha256"] != intent.snapshot_sha256
    ):
        raise PublicationError("committed snapshot conflicts with its intent")


def _validate_intent_against_catalog(
    connection: sqlite3.Connection,
    request: PublicationRequest,
    snapshot: sqlite3.Row,
    intent: _CommitIntent,
) -> None:
    journal = _read_publication(connection, request.publication_id)
    if (
        intent.publication_id != request.publication_id
        or intent.from_snapshot_id != journal["from_snapshot_id"]
        or intent.to_snapshot_id != journal["to_snapshot_id"]
        or intent.to_build_id != snapshot["build_id"]
        or intent.snapshot_sha256 != snapshot["file_sha256"]
        or intent.new_write_epoch - intent.old_write_epoch
        != int(request.increment_write_epoch)
    ):
        raise PublicationError("commit intent conflicts with catalog or request")
    if _phase_before(str(journal["phase"]), "committed_pending_checkpoint"):
        runtime = _read_runtime(connection)
        if (
            int(runtime["publication_generation"])
            != intent.target_publication_generation - 1
            or int(runtime["write_epoch"]) != intent.old_write_epoch
            or runtime["active_snapshot_id"] != intent.from_snapshot_id
        ):
            raise PublicationError("pre-commit runtime conflicts with commit intent")


def _validate_checkpoint_for_floor(data_root: Path, floor: DurableFloor) -> None:
    checkpoint = _resolve_relative(data_root, floor.checkpoint_relative_path)
    if not checkpoint.is_file() or checkpoint.is_symlink():
        raise PublicationError("committed floor checkpoint is missing or unsafe")
    if _sha256_file(checkpoint) != floor.checkpoint_sha256:
        raise PublicationError("committed floor checkpoint hash does not match")
    connection = _open_catalog(checkpoint, read_only=True, immutable=True)
    try:
        _validate_catalog_integrity(connection)
        runtime = _read_runtime(connection)
        if (
            int(runtime["publication_generation"]) != floor.publication_generation
            or int(runtime["write_epoch"]) != floor.write_epoch
            or bool(runtime["v1_fallback_open"]) != floor.fallback_open
            or runtime["active_snapshot_id"] != floor.active_snapshot_id
        ):
            raise PublicationError("checkpoint runtime conflicts with committed floor")
        _validate_snapshot(
            connection,
            data_root,
            floor.active_snapshot_id,
            allowed_build_states={"committed_pending_checkpoint", "fully_complete"},
            allowed_snapshot_states={"ready"},
        )
    finally:
        connection.close()


def _validate_runtime_floor(
    data_root: Path,
    connection: sqlite3.Connection,
    journal: sqlite3.Row | None,
) -> None:
    """Require a durable baseline, allowing only the journaled checkpoint gap."""

    runtime = _read_runtime(connection)
    generation = int(runtime["publication_generation"])
    floors = read_durable_floors(data_root)
    if not floors:
        if generation == 0:
            return
        raise PublicationError(
            "a positive publication generation requires a durable committed floor"
        )

    highest = floors[-1]
    _validate_checkpoint_for_floor(data_root, highest)
    if generation == highest.publication_generation:
        if (
            int(runtime["write_epoch"]) != highest.write_epoch
            or bool(runtime["v1_fallback_open"]) != highest.fallback_open
            or runtime["active_snapshot_id"] != highest.active_snapshot_id
        ):
            raise PublicationError(
                "runtime is below or conflicts with its durable floor"
            )
        return

    if (
        generation == highest.publication_generation + 1
        and journal is not None
        and str(journal["state"]) == "running"
        and not _phase_before(str(journal["phase"]), "committed_pending_checkpoint")
        and journal["to_snapshot_id"] == runtime["active_snapshot_id"]
    ):
        # The sole permitted gap is the short pointer-commit -> checkpoint/floor
        # interval for this exact durable journal.  Serving remains blocked by
        # the target build's committed_pending_checkpoint state.
        return
    raise PublicationError("runtime generation is not covered by a durable floor")


def _validate_new_floor(
    payload: Mapping[str, Any], existing: tuple[DurableFloor, ...]
) -> None:
    if not existing:
        return
    highest = existing[-1]
    generation = int(payload["publication_generation"])
    epoch = int(payload["write_epoch"])
    fallback = str(payload["v1_fallback_floor"])
    publication_id = str(payload["publication_id"])
    if generation < highest.publication_generation:
        raise PublicationError("committed floor generation cannot move backward")
    if generation == highest.publication_generation:
        if publication_id != highest.publication_id:
            raise PublicationError("one generation cannot have two committed floors")
        return
    if epoch < highest.write_epoch:
        raise PublicationError("committed floor write epoch cannot move backward")
    if not highest.fallback_open and fallback != "closed":
        raise PublicationError("committed floor cannot reopen V1 fallback")


def _parse_floor(path: Path, value: Mapping[str, Any]) -> DurableFloor:
    required = {
        "schema_version",
        "publication_id",
        "publication_generation",
        "write_epoch",
        "v1_fallback_floor",
        "active_snapshot_id",
        "checkpoint_relative_path",
        "checkpoint_sha256",
    }
    if set(value) != required or value.get("schema_version") != 1:
        raise PublicationError(f"invalid committed floor schema: {path.name}")
    generation = value["publication_generation"]
    epoch = value["write_epoch"]
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch < 0
    ):
        raise PublicationError("committed floor generation/epoch is invalid")
    fallback = value["v1_fallback_floor"]
    if fallback not in {"open", "closed"}:
        raise PublicationError("committed floor fallback value is invalid")
    if epoch > 0 and fallback != "closed":
        raise PublicationError("positive floor epoch requires closed V1 fallback")
    publication_id = str(value["publication_id"])
    if not publication_id or path.parent.name != publication_id:
        raise PublicationError("committed floor identity conflicts with its path")
    active_snapshot_id = str(value["active_snapshot_id"])
    if not active_snapshot_id:
        raise PublicationError("committed floor active snapshot is invalid")
    checkpoint_relative = _normalize_relative_path(
        str(value["checkpoint_relative_path"])
    )
    if not checkpoint_relative.startswith(
        "retrieval/v2/backups/catalog-current"
    ):
        raise PublicationError("committed floor cannot name a rollback-only backup")
    _validate_sha256(str(value["checkpoint_sha256"]), "checkpoint")
    return DurableFloor(
        publication_id=publication_id,
        publication_generation=generation,
        write_epoch=epoch,
        v1_fallback_floor=str(fallback),
        active_snapshot_id=active_snapshot_id,
        checkpoint_relative_path=checkpoint_relative,
        checkpoint_sha256=str(value["checkpoint_sha256"]),
        path=path,
    )


def _read_commit_intent_if_present(
    data_root: Path, publication_id: str
) -> _CommitIntent | None:
    path = _intent_path(data_root, publication_id)
    if not path.exists():
        return None
    value = _read_json(path)
    required = {
        "schema_version",
        "kind",
        "publication_id",
        "target_publication_generation",
        "old_write_epoch",
        "new_write_epoch",
        "v1_fallback_floor",
        "from_snapshot_id",
        "to_snapshot_id",
        "to_build_id",
        "snapshot_sha256",
        "enable_writes_on_complete",
    }
    if (
        set(value) != required
        or value.get("schema_version") != 1
        or value.get("kind") != "native_publication"
        or value.get("publication_id") != publication_id
    ):
        raise PublicationError("commit intent schema or identity is invalid")
    _validate_sha256(str(value["snapshot_sha256"]), "snapshot")
    generation = value["target_publication_generation"]
    old_epoch = value["old_write_epoch"]
    new_epoch = value["new_write_epoch"]
    if any(
        not isinstance(number, int) or isinstance(number, bool) or number < 0
        for number in (generation, old_epoch, new_epoch)
    ):
        raise PublicationError("commit intent generation/epoch is invalid")
    if new_epoch < old_epoch:
        raise PublicationError("commit intent write epoch moved backward")
    fallback = value["v1_fallback_floor"]
    if fallback not in {"open", "closed"}:
        raise PublicationError("commit intent fallback floor is invalid")
    if new_epoch > 0 and fallback != "closed":
        raise PublicationError("positive write epoch requires closed V1 fallback")
    if not isinstance(value["enable_writes_on_complete"], bool):
        raise PublicationError("commit intent writable flag is invalid")
    return _CommitIntent(
        publication_id=publication_id,
        target_publication_generation=generation,
        old_write_epoch=old_epoch,
        new_write_epoch=new_epoch,
        v1_fallback_floor=str(fallback),
        from_snapshot_id=(
            None
            if value["from_snapshot_id"] is None
            else str(value["from_snapshot_id"])
        ),
        to_snapshot_id=str(value["to_snapshot_id"]),
        to_build_id=str(value["to_build_id"]),
        snapshot_sha256=str(value["snapshot_sha256"]),
        enable_writes_on_complete=value["enable_writes_on_complete"],
    )


def _outcome_from_floor(
    connection: sqlite3.Connection, floor: DurableFloor
) -> PublicationOutcome:
    runtime = _read_runtime(connection)
    if (
        int(runtime["publication_generation"]) != floor.publication_generation
        or int(runtime["write_epoch"]) != floor.write_epoch
        or runtime["active_snapshot_id"] != floor.active_snapshot_id
        or bool(runtime["v1_fallback_open"]) != floor.fallback_open
    ):
        raise PublicationError("runtime does not match its committed floor")
    return PublicationOutcome(
        publication_id=floor.publication_id,
        publication_generation=floor.publication_generation,
        write_epoch=floor.write_epoch,
        active_snapshot_id=floor.active_snapshot_id,
        predecessor_snapshot_id=(
            None
            if runtime["predecessor_snapshot_id"] is None
            else str(runtime["predecessor_snapshot_id"])
        ),
        v1_fallback_open=floor.fallback_open,
        checkpoint_relative_path=floor.checkpoint_relative_path,
        checkpoint_sha256=floor.checkpoint_sha256,
        committed_floor_relative_path=str(
            floor.path.relative_to(Path(floor.path.parents[4])).as_posix()
        )
        if len(floor.path.parents) >= 5
        else floor.path.as_posix(),
    )


def _rollback_relative_path(publication_id: str, generation: int) -> str:
    return (
        "retrieval/v2/backups/"
        f"catalog-rollback-g{generation}-{publication_id}.sqlite3"
    )


def _checkpoint_relative_path(intent: _CommitIntent) -> str:
    return (
        "retrieval/v2/backups/"
        f"catalog-current-g{intent.target_publication_generation}-"
        f"{intent.publication_id}.sqlite3"
    )


def _intent_path(data_root: Path, publication_id: str) -> Path:
    return (
        data_root
        / "retrieval"
        / "v2"
        / "evidence"
        / publication_id
        / "commit-intent.json"
    )


def _floor_path(data_root: Path, publication_id: str) -> Path:
    return (
        data_root
        / "retrieval"
        / "v2"
        / "evidence"
        / publication_id
        / "committed-floor.json"
    )


def _atomic_json_once(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (_canonical_json(value) + "\n").encode("utf-8")
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != encoded:
            raise PublicationError(f"immutable evidence conflicts at {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".json-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _link_without_overwrite(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _link_without_overwrite(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except FileExistsError:
        raise FileExistsError(
            f"publication target already exists: {target.name}"
        ) from None
    except OSError as exc:
        raise PublicationError(
            f"atomic non-overwriting publication failed: {exc}"
        ) from exc
    source.unlink()
    _fsync_directory(target.parent)


def _normalize_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("relative path must be a non-empty string")
    if "\\" in value or "\0" in value:
        raise ValueError("relative path must use safe POSIX separators")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("relative path must remain under the data root")
    if path.parts and ":" in path.parts[0]:
        raise ValueError("relative path must not be drive-qualified")
    return path.as_posix()


def _resolve_relative(data_root: Path, relative_path: str) -> Path:
    normalized = _normalize_relative_path(relative_path)
    candidate = data_root.joinpath(*PurePosixPath(normalized).parts).resolve(
        strict=False
    )
    try:
        candidate.relative_to(data_root)
    except ValueError as exc:
        raise PublicationError("relative path escapes the selected data root") from exc
    return candidate


def _validate_sha256(value: str, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} SHA-256 must be lowercase hexadecimal")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise PublicationError(f"durable evidence is missing or unsafe: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"durable evidence is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise PublicationError(f"durable evidence must be an object: {path.name}")
    return value


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
