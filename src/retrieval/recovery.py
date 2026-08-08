"""Deterministic startup reconciliation for the native V2 catalog.

Recovery never selects a snapshot by inspecting filenames.  It either trusts
an integrity-checked SQLite control plane, restores the checkpoint named by the
highest validated committed floor, serves the verified V2 predecessor in a
degraded state, or fails vector retrieval closed.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .schema import (
    SchemaError,
    checkpoint_isolated_catalog,
    require_main_file_only,
)
from .writer_lock import NativeWriterLock, WriterLease, assert_writer_lease_owned
from .publication import (
    PublicationCoordinator,
    PublicationCrash,
    PublicationError,
    _atomic_json_once,
    _create_or_validate_sqlite_copy,
    _floor_path,
    _fsync_directory,
    _fsync_file,
    _is_exact_empty_runtime,
    _open_catalog,
    _phase_before,
    _read_json,
    _read_publication,
    _read_runtime,
    _resolve_relative,
    _schema_keys_match,
    _set_phase,
    _sha256_file,
    _transaction,
    _validate_catalog_integrity,
    _validate_checkpoint_for_floor,
    _validate_new_floor,
    _validate_snapshot,
    read_commit_intents,
    read_durable_floors,
)


class RecoveryDisposition(str, Enum):
    ACTIVE = "active"
    PREVIOUS_ACTIVE = "previous_active"
    PUBLICATION_COMPLETED = "publication_completed"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    PREDECESSOR_DEGRADED = "predecessor_degraded"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class RecoveryOutcome:
    disposition: RecoveryDisposition
    publication_generation: int | None
    write_epoch: int | None
    active_snapshot_id: str | None
    predecessor_snapshot_id: str | None
    degraded: bool
    write_enabled: bool
    restored_checkpoint: bool = False
    reason: str | None = None


class StartupReconciler:
    """Reconcile interrupted publication and active-snapshot corruption."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        catalog_relative_path: str = "retrieval/v2/catalog.sqlite3",
    ) -> None:
        self.data_root = Path(data_root).resolve(strict=True)
        self.coordinator = PublicationCoordinator(
            self.data_root,
            catalog_relative_path=catalog_relative_path,
        )
        self.catalog_path = self.coordinator.catalog_path

    def reconcile(
        self,
        *,
        crash_after: str | None = None,
        writer_lease: WriterLease | None = None,
    ) -> RecoveryOutcome:
        """Return exactly one startup disposition and never reopen a floor."""

        if writer_lease is None:
            with NativeWriterLock(self.data_root) as owned_lease:
                return self.reconcile(
                    crash_after=crash_after,
                    writer_lease=owned_lease,
                )
        assert_writer_lease_owned(writer_lease, self.data_root)

        restored = False
        try:
            connection = _open_catalog(self.catalog_path, read_only=False)
            try:
                _validate_startup_control_plane(self.data_root, connection)
            except BaseException:
                connection.close()
                raise
        except (OSError, sqlite3.DatabaseError, PublicationError) as catalog_error:
            try:
                self._restore_highest_checkpoint()
                restored = True
                connection = _open_catalog(self.catalog_path, read_only=False)
                _validate_startup_control_plane(self.data_root, connection)
            except BaseException as restore_error:
                return RecoveryOutcome(
                    disposition=RecoveryDisposition.FAIL_CLOSED,
                    publication_generation=None,
                    write_epoch=None,
                    active_snapshot_id=None,
                    predecessor_snapshot_id=None,
                    degraded=True,
                    write_enabled=False,
                    reason=(
                        "catalog unavailable and no eligible committed checkpoint: "
                        f"{type(catalog_error).__name__}; "
                        f"{type(restore_error).__name__}"
                    ),
                )

        try:
            pending = connection.execute(
                """
                SELECT publication_id, phase
                FROM publication_runs
                WHERE state = 'running'
                ORDER BY created_at, publication_id
                """
            ).fetchall()
            if len(pending) > 1:
                return self._fail_closed(
                    connection,
                    "multiple running publication journals violate single-writer state",
                    restored=restored,
                )
            if pending:
                publication_id = str(pending[0]["publication_id"])
                phase = str(pending[0]["phase"])
                intent_path = (
                    self.data_root
                    / "retrieval"
                    / "v2"
                    / "evidence"
                    / publication_id
                    / "commit-intent.json"
                )
                if (
                    not _phase_before(phase, "commit_intent_durable")
                    and not intent_path.is_file()
                ):
                    raise PublicationError(
                        "publication journal claims a durable intent that is missing"
                    )
                if intent_path.is_file():
                    intent = self._read_any_intent(publication_id)
                else:
                    intent = None
                if (
                    _phase_before(phase, "commit_intent_durable")
                    and (
                        (
                            intent is not None
                            and intent.get("kind") == "active_recovery"
                        )
                        or (
                            intent is None
                            and self._journal_is_active_recovery(
                                connection, publication_id
                            )
                        )
                    )
                ):
                    intent = self._prepare_active_recovery(
                        connection,
                        _read_runtime(connection),
                        crash_after=crash_after,
                    )
                request = None
                if intent is None or intent.get("kind") != "active_recovery":
                    request = self.coordinator.request_from_journal(publication_id)
                connection.close()
                connection = None
                if intent is not None and intent.get("kind") == "active_recovery":
                    outcome = self._resume_active_recovery(
                        intent,
                        crash_after=crash_after,
                    )
                else:
                    assert request is not None
                    published = self.coordinator.publish(
                        request,
                        writer_lease=writer_lease,
                    )
                    outcome = RecoveryOutcome(
                        disposition=RecoveryDisposition.PUBLICATION_COMPLETED,
                        publication_generation=published.publication_generation,
                        write_epoch=published.write_epoch,
                        active_snapshot_id=published.active_snapshot_id,
                        predecessor_snapshot_id=published.predecessor_snapshot_id,
                        degraded=False,
                        write_enabled=request.enable_writes_on_complete,
                        restored_checkpoint=restored,
                    )
                if restored:
                    return RecoveryOutcome(
                        **{
                            **outcome.__dict__,
                            "disposition": RecoveryDisposition.CHECKPOINT_RESTORED,
                            "restored_checkpoint": True,
                        }
                    )
                return outcome

            outcome = self._validate_or_recover_active(
                connection,
                restored=restored,
                crash_after=crash_after,
            )
            if restored and outcome.disposition in {
                RecoveryDisposition.ACTIVE,
                RecoveryDisposition.PUBLICATION_COMPLETED,
            }:
                return RecoveryOutcome(
                    **{
                        **outcome.__dict__,
                        "disposition": RecoveryDisposition.CHECKPOINT_RESTORED,
                        "restored_checkpoint": True,
                    }
                )
            return outcome
        except PublicationCrash:
            raise
        except (OSError, sqlite3.DatabaseError, PublicationError) as exc:
            if connection is None:
                return RecoveryOutcome(
                    disposition=RecoveryDisposition.FAIL_CLOSED,
                    publication_generation=None,
                    write_epoch=None,
                    active_snapshot_id=None,
                    predecessor_snapshot_id=None,
                    degraded=True,
                    write_enabled=False,
                    restored_checkpoint=restored,
                    reason=str(exc),
                )
            return self._fail_closed(connection, str(exc), restored=restored)
        finally:
            if connection is not None:
                connection.close()

    def _validate_current_active(
        self,
        connection: sqlite3.Connection,
        *,
        disposition: RecoveryDisposition,
        restored: bool,
    ) -> RecoveryOutcome:
        runtime = _read_runtime(connection)
        active = runtime["active_snapshot_id"]
        if active is None:
            if _is_exact_empty_runtime(runtime):
                return _runtime_outcome(
                    runtime,
                    disposition,
                    restored=restored,
                )
            return self._fail_closed(
                connection, "runtime has no active V2 snapshot", restored=restored
            )
        _validate_snapshot(
            connection,
            self.data_root,
            str(active),
            allowed_build_states={"fully_complete"},
            allowed_snapshot_states={"ready"},
        )
        return _runtime_outcome(runtime, disposition, restored=restored)

    def _validate_or_recover_active(
        self,
        connection: sqlite3.Connection,
        *,
        restored: bool,
        crash_after: str | None,
    ) -> RecoveryOutcome:
        runtime = _read_runtime(connection)
        active = runtime["active_snapshot_id"]
        if active is None:
            return self._validate_current_active(
                connection,
                disposition=RecoveryDisposition.ACTIVE,
                restored=restored,
            )
        try:
            _validate_snapshot(
                connection,
                self.data_root,
                str(active),
                allowed_build_states={"fully_complete"},
                allowed_snapshot_states={"ready"},
            )
            return _runtime_outcome(
                runtime, RecoveryDisposition.ACTIVE, restored=restored
            )
        except PublicationError as active_error:
            predecessor = runtime["predecessor_snapshot_id"]
            if predecessor is None:
                return self._fail_closed(
                    connection,
                    "active snapshot invalid and no predecessor exists: "
                    f"{active_error}",
                    restored=restored,
                )
            try:
                _validate_snapshot(
                    connection,
                    self.data_root,
                    str(predecessor),
                    allowed_build_states={"fully_complete"},
                    allowed_snapshot_states={"ready"},
                )
            except PublicationError as predecessor_error:
                return self._fail_closed(
                    connection,
                    "active and predecessor snapshots are invalid: "
                    f"{active_error}; {predecessor_error}",
                    restored=restored,
                )

            intent = self._prepare_active_recovery(
                connection,
                runtime,
                crash_after=crash_after,
            )
            connection.close()
            outcome = self._resume_active_recovery(
                intent,
                crash_after=crash_after,
            )
            return RecoveryOutcome(
                **{**outcome.__dict__, "restored_checkpoint": restored}
            )

    def _prepare_active_recovery(
        self,
        connection: sqlite3.Connection,
        runtime: sqlite3.Row,
        *,
        crash_after: str | None,
    ) -> dict[str, Any]:
        from_snapshot = str(runtime["active_snapshot_id"])
        to_snapshot = str(runtime["predecessor_snapshot_id"])
        target_generation = int(runtime["publication_generation"]) + 1
        suffix = hashlib.sha256(
            f"{from_snapshot}\0{to_snapshot}\0{target_generation}".encode("utf-8")
        ).hexdigest()[:16]
        publication_id = f"recovery-g{target_generation}-{suffix}"
        to_build = connection.execute(
            "SELECT build_id FROM vector_snapshots WHERE snapshot_id = ?",
            (to_snapshot,),
        ).fetchone()
        if to_build is None:
            raise PublicationError("predecessor build is missing")

        evidence_directory = (
            self.data_root
            / "retrieval"
            / "v2"
            / "evidence"
            / publication_id
        )
        manifest = {
            "schema_version": 2,
            "kind": "active_recovery",
            "publication_id": publication_id,
            "from_snapshot_id": from_snapshot,
            "to_snapshot_id": to_snapshot,
            "reason_code": "active_snapshot_validation_failed",
        }
        manifest_path = evidence_directory / "manifest.json"
        _atomic_json_once(manifest_path, manifest)
        manifest_hash = _sha256_file(manifest_path)
        manifest_relative = (
            f"retrieval/v2/evidence/{publication_id}/manifest.json"
        )
        intent = {
            "schema_version": 2,
            "kind": "active_recovery",
            "publication_id": publication_id,
            "target_publication_generation": target_generation,
            "write_epoch": int(runtime["write_epoch"]),
            "from_snapshot_id": from_snapshot,
            "to_snapshot_id": to_snapshot,
            "to_build_id": str(to_build[0]),
        }
        existing = connection.execute(
            "SELECT * FROM publication_runs WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        if existing is None:
            _transaction(
                connection,
                lambda: connection.execute(
                    """
                    INSERT INTO publication_runs (
                        publication_id, from_snapshot_id, to_snapshot_id,
                        evidence_manifest_relative_path,
                        evidence_manifest_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        publication_id,
                        from_snapshot,
                        to_snapshot,
                        manifest_relative,
                        manifest_hash,
                    ),
                ),
            )
        else:
            expected_journal = (
                from_snapshot,
                to_snapshot,
                manifest_relative,
                manifest_hash,
                "running",
            )
            actual_journal = (
                existing["from_snapshot_id"],
                existing["to_snapshot_id"],
                existing["evidence_manifest_relative_path"],
                existing["evidence_manifest_sha256"],
                existing["state"],
            )
            if actual_journal != expected_journal:
                raise PublicationError(
                    "active-recovery journal conflicts with deterministic recovery"
                )

        if crash_after == "recovery_journal_durable":
            raise PublicationCrash("recovery_journal_durable")

        # The immutable intent is published only after its exact journal is
        # durable.  The journal may claim commit_intent_durable only after the
        # external record has survived close/fsync and unique publication.
        _atomic_json_once(evidence_directory / "commit-intent.json", intent)
        if crash_after == "recovery_commit_intent_written":
            raise PublicationCrash("recovery_commit_intent_written")

        row = _read_publication(connection, publication_id)
        if _phase_before(str(row["phase"]), "commit_intent_durable"):
            _set_phase(connection, publication_id, "commit_intent_durable")
        return intent

    def _journal_is_active_recovery(
        self,
        connection: sqlite3.Connection,
        publication_id: str,
    ) -> bool:
        row = _read_publication(connection, publication_id)
        relative = row["evidence_manifest_relative_path"]
        expected_hash = row["evidence_manifest_sha256"]
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise PublicationError("running journal has incomplete evidence identity")
        manifest_path = _resolve_relative(self.data_root, relative)
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or _sha256_file(manifest_path) != expected_hash
        ):
            raise PublicationError("running journal evidence is missing or changed")
        manifest = _read_json(manifest_path)
        if manifest.get("kind") != "active_recovery":
            return False
        required_manifest = {
            "kind": "active_recovery",
            "publication_id": publication_id,
            "from_snapshot_id": row["from_snapshot_id"],
            "to_snapshot_id": row["to_snapshot_id"],
            "reason_code": "active_snapshot_validation_failed",
        }
        required_keys = {"schema_version", *required_manifest}
        if (
            not _schema_keys_match(manifest, required_keys)
            or not required_manifest.items() <= manifest.items()
        ):
            raise PublicationError("active-recovery manifest conflicts with its journal")
        return True

    def _resume_active_recovery(
        self,
        intent: Mapping[str, Any],
        *,
        crash_after: str | None,
    ) -> RecoveryOutcome:
        _validate_recovery_intent(intent)
        publication_id = str(intent["publication_id"])
        connection = _open_catalog(self.catalog_path, read_only=False)
        try:
            _validate_catalog_integrity(connection)
            row = _read_publication(connection, publication_id)
            if _phase_before(str(row["phase"]), "committed_pending_checkpoint"):
                if crash_after == "recovery_commit_intent_durable":
                    raise PublicationCrash("recovery_commit_intent_durable")

                def recover_pointer() -> None:
                    runtime = _read_runtime(connection)
                    expected = (
                        int(intent["target_publication_generation"]) - 1,
                        intent["from_snapshot_id"],
                        intent["to_snapshot_id"],
                        int(intent["write_epoch"]),
                    )
                    actual = (
                        int(runtime["publication_generation"]),
                        runtime["active_snapshot_id"],
                        runtime["predecessor_snapshot_id"],
                        int(runtime["write_epoch"]),
                    )
                    if actual != expected:
                        raise PublicationError(
                            "runtime changed after recovery intent became durable"
                        )
                    connection.execute(
                        """
                        UPDATE vector_snapshots
                        SET state = 'failed',
                            state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE snapshot_id = ? AND state = 'ready'
                        """,
                        (intent["from_snapshot_id"],),
                    )
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise PublicationError("bad active snapshot cannot be failed")
                    connection.execute(
                        """
                        UPDATE retrieval_runtime
                        SET active_snapshot_id = ?, active_build_id = ?,
                            predecessor_snapshot_id = NULL,
                            publication_generation = ?, degraded = 1,
                            write_enabled = 0,
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE runtime_id = 1
                        """,
                        (
                            intent["to_snapshot_id"],
                            intent["to_build_id"],
                            intent["target_publication_generation"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE publication_runs
                        SET phase = 'committed_pending_checkpoint',
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE publication_id = ?
                        """,
                        (publication_id,),
                    )
                    if crash_after == "during_recovery_transaction":
                        raise PublicationCrash("during_recovery_transaction")

                _transaction(connection, recover_pointer)
                if crash_after == "recovery_pointer_committed":
                    raise PublicationCrash("recovery_pointer_committed")
            else:
                _validate_recovered_runtime(connection, intent)

            checkpoint_relative = (
                "retrieval/v2/backups/"
                f"catalog-current-g{intent['target_publication_generation']}-"
                f"{publication_id}.sqlite3"
            )
            checkpoint_path = _resolve_relative(
                self.data_root, checkpoint_relative
            )

            def validate_checkpoint(check: sqlite3.Connection) -> None:
                _validate_catalog_integrity(check)
                _validate_recovered_runtime(check, intent)
                _validate_snapshot(
                    check,
                    self.data_root,
                    str(intent["to_snapshot_id"]),
                    allowed_build_states={"fully_complete"},
                    allowed_snapshot_states={"ready"},
                )

            checkpoint_hash = _create_or_validate_sqlite_copy(
                connection,
                checkpoint_path,
                validate_checkpoint,
            )
            if crash_after == "recovery_checkpoint_created":
                raise PublicationCrash("recovery_checkpoint_created")
            row = _read_publication(connection, publication_id)
            if _phase_before(str(row["phase"]), "checkpoint_validated"):
                _set_phase(connection, publication_id, "checkpoint_validated")

            floor_payload = {
                "schema_version": 2,
                "publication_id": publication_id,
                "publication_generation": int(
                    intent["target_publication_generation"]
                ),
                "write_epoch": int(intent["write_epoch"]),
                "active_snapshot_id": str(intent["to_snapshot_id"]),
                "checkpoint_relative_path": checkpoint_relative,
                "checkpoint_sha256": checkpoint_hash,
            }
            floors = read_durable_floors(self.data_root)
            _validate_new_floor(floor_payload, floors)
            _atomic_json_once(
                _floor_path(self.data_root, publication_id), floor_payload
            )
            row = _read_publication(connection, publication_id)
            if _phase_before(str(row["phase"]), "committed_floor_durable"):
                _set_phase(connection, publication_id, "committed_floor_durable")
            if crash_after == "recovery_floor_durable":
                raise PublicationCrash("recovery_floor_durable")

            row = _read_publication(connection, publication_id)
            if str(row["state"]) != "fully_complete":
                _transaction(
                    connection,
                    lambda: connection.execute(
                        """
                        UPDATE publication_runs
                        SET phase = 'fully_complete', state = 'fully_complete',
                            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                        WHERE publication_id = ? AND state = 'running'
                        """,
                        (publication_id,),
                    ),
                )
            runtime = _read_runtime(connection)
            return _runtime_outcome(
                runtime,
                RecoveryDisposition.PREDECESSOR_DEGRADED,
                restored=False,
            )
        finally:
            connection.close()

    def _restore_highest_checkpoint(self) -> None:
        floors = read_durable_floors(self.data_root)
        intents = read_commit_intents(self.data_root)
        if not floors:
            raise PublicationError("no committed floor is available for restore")
        highest = floors[-1]
        for intent in intents:
            target = intent.get("target_publication_generation")
            publication_id = intent.get("publication_id")
            if not isinstance(target, int) or isinstance(target, bool):
                raise PublicationError("commit intent generation is invalid")
            if target > highest.publication_generation or (
                target == highest.publication_generation
                and publication_id != highest.publication_id
            ):
                raise PublicationError(
                    "unresolved commit intent forbids rollback-only restore"
                )

        _validate_checkpoint_for_floor(self.data_root, highest)
        checkpoint = _resolve_relative(
            self.data_root, highest.checkpoint_relative_path
        )
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.catalog_path.parent / f".restore-{uuid.uuid4().hex[:12]}.sqlite3"
        try:
            source = _open_catalog(checkpoint, read_only=True, immutable=True)
            try:
                destination = sqlite3.connect(temporary)
                try:
                    source.backup(destination)
                    destination.commit()
                    try:
                        checkpoint_isolated_catalog(destination)
                    except SchemaError as exc:
                        raise PublicationError(
                            'restored catalog WAL checkpoint did not complete'
                        ) from exc
                finally:
                    destination.close()
            finally:
                source.close()
            try:
                require_main_file_only(temporary)
            except SchemaError as exc:
                raise PublicationError(
                    'restored catalog is not main-file-only'
                ) from exc
            _fsync_file(temporary)
            check = _open_catalog(temporary, read_only=True, immutable=True)
            try:
                _validate_catalog_integrity(check)
                runtime = _read_runtime(check)
                if (
                    int(runtime["publication_generation"])
                    < highest.publication_generation
                    or int(runtime["write_epoch"]) < highest.write_epoch
                ):
                    raise PublicationError(
                        "checkpoint is below the durable committed floor"
                    )
            finally:
                check.close()
            self._quarantine_sqlite_sidecars()
            os.replace(temporary, self.catalog_path)
            _fsync_directory(self.catalog_path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _quarantine_sqlite_sidecars(self) -> None:
        """Prevent a stale WAL/journal from being applied to restored bytes."""

        quarantine = self.data_root / "retrieval" / "v2" / "quarantine"
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{self.catalog_path}{suffix}")
            if not sidecar.exists():
                continue
            quarantine.mkdir(parents=True, exist_ok=True)
            target = quarantine / (
                f"{self.catalog_path.name}{suffix}.{uuid.uuid4().hex}.rejected"
            )
            try:
                os.rename(sidecar, target)
            except OSError as exc:
                raise PublicationError(
                    f"catalog sidecar could not be quarantined: {suffix}"
                ) from exc
            _fsync_directory(quarantine)

    def _read_any_intent(self, publication_id: str) -> dict[str, Any]:
        path = (
            self.data_root
            / "retrieval"
            / "v2"
            / "evidence"
            / publication_id
            / "commit-intent.json"
        )
        value = _read_json(path)
        if value.get("publication_id") != publication_id:
            raise PublicationError("commit intent identity does not match journal")
        return value

    def _fail_closed(
        self,
        connection: sqlite3.Connection,
        reason: str,
        *,
        restored: bool,
    ) -> RecoveryOutcome:
        try:
            _transaction(
                connection,
                lambda: connection.execute(
                    """
                    UPDATE retrieval_runtime
                    SET degraded = 1, write_enabled = 0,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE runtime_id = 1
                    """
                ),
            )
            runtime = _read_runtime(connection)
            return RecoveryOutcome(
                disposition=RecoveryDisposition.FAIL_CLOSED,
                publication_generation=int(runtime["publication_generation"]),
                write_epoch=int(runtime["write_epoch"]),
                active_snapshot_id=(
                    None
                    if runtime["active_snapshot_id"] is None
                    else str(runtime["active_snapshot_id"])
                ),
                predecessor_snapshot_id=(
                    None
                    if runtime["predecessor_snapshot_id"] is None
                    else str(runtime["predecessor_snapshot_id"])
                ),
                degraded=True,
                write_enabled=False,
                restored_checkpoint=restored,
                reason=reason,
            )
        except (sqlite3.DatabaseError, PublicationError):
            return RecoveryOutcome(
                disposition=RecoveryDisposition.FAIL_CLOSED,
                publication_generation=None,
                write_epoch=None,
                active_snapshot_id=None,
                predecessor_snapshot_id=None,
                degraded=True,
                write_enabled=False,
                restored_checkpoint=restored,
                reason=reason,
            )


def _validate_startup_control_plane(
    data_root: Path,
    connection: sqlite3.Connection,
    *,
    validate_integrity: bool = True,
) -> None:
    if validate_integrity:
        _validate_catalog_integrity(connection)
    _validate_startup_control_plane_records(data_root, connection)


def _validate_startup_control_plane_records(
    data_root: Path,
    connection: sqlite3.Connection,
) -> None:
    """Validate journals and durable floors without a whole-catalog scan."""

    _validate_external_commit_intents(data_root, connection)
    running = connection.execute(
        """
        SELECT * FROM publication_runs
        WHERE state = 'running'
        ORDER BY created_at, publication_id
        """
    ).fetchall()
    journal = running[0] if len(running) == 1 else None
    _validate_runtime_floor_at_startup(data_root, connection, journal)


def _validate_runtime_floor_at_startup(
    data_root: Path,
    connection: sqlite3.Connection,
    journal: sqlite3.Row | None,
) -> None:
    runtime = _read_runtime(connection)
    generation = int(runtime["publication_generation"])
    floors = read_durable_floors(data_root)
    if not floors:
        if generation == 0:
            return
        if (
            generation == 1
            and journal is not None
            and str(journal["state"]) == "running"
            and not _phase_before(
                str(journal["phase"]), "committed_pending_checkpoint"
            )
            and journal["to_snapshot_id"] == runtime["active_snapshot_id"]
        ):
            return
        raise PublicationError(
            "a positive publication generation requires a durable committed floor"
        )
    highest = floors[-1]
    if generation == highest.publication_generation:
        if (
            int(runtime["write_epoch"]) != highest.write_epoch
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
        and not _phase_before(
            str(journal["phase"]), "committed_pending_checkpoint"
        )
        and journal["to_snapshot_id"] == runtime["active_snapshot_id"]
    ):
        return
    raise PublicationError("runtime generation is not covered by a durable floor")


def _validate_external_commit_intents(
    data_root: Path,
    connection: sqlite3.Connection,
) -> None:
    floors = read_durable_floors(data_root)
    intents = read_commit_intents(data_root)
    if not intents:
        return
    highest_generation = floors[-1].publication_generation if floors else 0
    highest_publication = floors[-1].publication_id if floors else None
    runtime = _read_runtime(connection)
    future_count = 0
    for intent in intents:
        target = intent.get("target_publication_generation")
        publication_id = intent.get("publication_id")
        if (
            not isinstance(target, int)
            or isinstance(target, bool)
            or target <= 0
            or not isinstance(publication_id, str)
            or not publication_id
        ):
            raise PublicationError("commit intent generation or identity is invalid")
        if target < highest_generation:
            continue
        if target == highest_generation:
            if publication_id != highest_publication:
                raise PublicationError(
                    "commit intent conflicts with the highest durable floor"
                )
            continue
        if target != highest_generation + 1:
            raise PublicationError("commit intent skips the durable floor generation")
        future_count += 1
        if future_count > 1:
            raise PublicationError("multiple unresolved commit intents are unsafe")
        row = connection.execute(
            "SELECT * FROM publication_runs WHERE publication_id = ?",
            (publication_id,),
        ).fetchone()
        if (
            row is None
            or str(row["state"]) != "running"
        ):
            raise PublicationError(
                "unresolved commit intent is missing its exact running journal"
            )
        to_snapshot = intent.get("to_snapshot_id", intent.get("snapshot_id"))
        if not isinstance(to_snapshot, str) or row["to_snapshot_id"] != to_snapshot:
            raise PublicationError("commit intent target conflicts with its journal")
        generation = int(runtime["publication_generation"])
        if generation == highest_generation:
            if not _phase_before(
                str(row["phase"]), "committed_pending_checkpoint"
            ):
                raise PublicationError(
                    "pre-commit runtime conflicts with unresolved intent"
                )
            if (
                floors
                and runtime["active_snapshot_id"] != floors[-1].active_snapshot_id
            ):
                raise PublicationError(
                    "pre-commit runtime conflicts with unresolved intent"
                )
            if not floors and not _is_exact_empty_runtime(runtime):
                raise PublicationError(
                    "pre-commit runtime conflicts with unresolved intent"
                )
        elif generation == target:
            if (
                _phase_before(str(row["phase"]), "committed_pending_checkpoint")
                or runtime["active_snapshot_id"] != to_snapshot
            ):
                raise PublicationError(
                    "committed runtime conflicts with unresolved intent"
                )
        else:
            raise PublicationError("runtime is not covered by unresolved intent")


def _validate_recovery_intent(intent: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "kind",
        "publication_id",
        "target_publication_generation",
        "write_epoch",
        "from_snapshot_id",
        "to_snapshot_id",
        "to_build_id",
    }
    if (
        not _schema_keys_match(intent, required)
        or intent.get("kind") != "active_recovery"
    ):
        raise PublicationError("active-recovery intent is invalid")
    if any(
        not isinstance(intent.get(field), int)
        or isinstance(intent.get(field), bool)
        or int(intent[field]) < 0
        for field in ("target_publication_generation", "write_epoch")
    ):
        raise PublicationError("active-recovery generation/epoch is invalid")
    for field in (
        "publication_id",
        "from_snapshot_id",
        "to_snapshot_id",
        "to_build_id",
    ):
        if not isinstance(intent.get(field), str) or not str(intent[field]).strip():
            raise PublicationError(f"active-recovery {field} is invalid")
    if intent["from_snapshot_id"] == intent["to_snapshot_id"]:
        raise PublicationError("active recovery cannot select the failed snapshot")


def _validate_recovered_runtime(
    connection: sqlite3.Connection,
    intent: Mapping[str, Any],
) -> None:
    runtime = _read_runtime(connection)
    actual = (
        int(runtime["publication_generation"]),
        int(runtime["write_epoch"]),
        runtime["active_snapshot_id"],
        runtime["active_build_id"],
        runtime["predecessor_snapshot_id"],
        bool(runtime["degraded"]),
        bool(runtime["write_enabled"]),
    )
    expected = (
        int(intent["target_publication_generation"]),
        int(intent["write_epoch"]),
        intent["to_snapshot_id"],
        intent["to_build_id"],
        None,
        True,
        False,
    )
    if actual != expected:
        raise PublicationError("runtime conflicts with active-recovery intent")


def _runtime_outcome(
    runtime: sqlite3.Row,
    disposition: RecoveryDisposition,
    *,
    restored: bool,
) -> RecoveryOutcome:
    return RecoveryOutcome(
        disposition=disposition,
        publication_generation=int(runtime["publication_generation"]),
        write_epoch=int(runtime["write_epoch"]),
        active_snapshot_id=(
            None
            if runtime["active_snapshot_id"] is None
            else str(runtime["active_snapshot_id"])
        ),
        predecessor_snapshot_id=(
            None
            if runtime["predecessor_snapshot_id"] is None
            else str(runtime["predecessor_snapshot_id"])
        ),
        degraded=bool(runtime["degraded"]),
        write_enabled=bool(runtime["write_enabled"]),
        restored_checkpoint=restored,
    )
