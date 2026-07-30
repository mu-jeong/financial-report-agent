from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from src.retrieval.bootstrap import (
    RetrievalBootstrapError,
    inspect_runtime,
    reconcile_and_inspect_runtime,
)
from src.retrieval.runtime_guard import RetrievalWriteBlocked, guard_before_retrieval_write
from src.retrieval.publication import PublicationCoordinator
from src.retrieval.schema import install_schema
from src.retrieval.vector_index import build_index
from src.retrieval.writer_lock import NativeWriterLock, WriterLockError
from tests.retrieval.test_retrieval_publication import make_native_install


HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64


def _native_install(tmp_path: Path, *, epoch: int = 0) -> tuple[Path, Path]:
    legacy = tmp_path / "reports.db"
    legacy.write_bytes(b"legacy")
    v2_root = tmp_path / "retrieval" / "v2"
    snapshots = v2_root / "snapshots"
    snapshots.mkdir(parents=True)
    snapshot_path = snapshots / "snapshot.faiss"
    descriptor = build_index(
        np.asarray([[1.0, 2.0]], dtype=np.float32),
        [1],
        "l2",
    ).write(snapshot_path)

    catalog = v2_root / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        install_schema(connection)
        connection.execute(
            """
            INSERT INTO embedding_profiles (
                profile_id, profile_hash, model, dimension, metric,
                normalization, prefix_template, extractor,
                parent_policy_json, child_policy_json
            ) VALUES (?, ?, 'model', 2, 'l2', 0, '{content}', 'extractor', ?, ?)
            """,
            (HEX_A, HEX_A, json.dumps({"size": 10}), json.dumps({"size": 5})),
        )
        connection.execute(
            """
            INSERT INTO reports (
                report_id, report_uid, canonical_relative_path, source_sha256,
                retrieval_metadata_sha256, report_type, report_date,
                target_name, title, broker
            ) VALUES (1, ?, 'downloaded/a.pdf', ?, ?, 'company',
                      '2026-07-16', 'A', 'Title', 'Broker')
            """,
            (HEX_B, HEX_A, HEX_C),
        )
        connection.execute(
            """
            INSERT INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order,
                content, content_sha256
            ) VALUES (?, 1, ?, 0, 'hello', ?)
            """,
            (HEX_C, HEX_A, hashlib.sha256(b"hello").hexdigest()),
        )
        connection.execute(
            """
            INSERT INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order,
                span_start, span_end, embedding_text_sha256
            ) VALUES (?, ?, ?, 0, 0, 5, ?)
            """,
            (HEX_B, HEX_C, HEX_A, HEX_C),
        )
        manifest = json.dumps({"included": [HEX_B]}, separators=(",", ":"))
        connection.execute(
            """
            INSERT INTO retrieval_builds (
                build_id, profile_id, source_manifest_json,
                source_manifest_sha256, included_count, excluded_count,
                expected_count, exclusion_policy_version
            ) VALUES ('build', ?, ?, ?, 1, 0, 1, 'test-v1')
            """,
            (HEX_A, manifest, hashlib.sha256(manifest.encode()).hexdigest()),
        )
        connection.execute(
            "UPDATE retrieval_builds SET state = 'cataloging' WHERE build_id = 'build'"
        )
        connection.execute(
            "UPDATE retrieval_builds SET state = 'vector_building' WHERE build_id = 'build'"
        )
        connection.execute(
            """
            INSERT INTO vector_snapshots (
                snapshot_id, build_id, relative_path, file_sha256,
                size_bytes, dimension, metric, ntotal
            ) VALUES ('snapshot', 'build', 'retrieval/v2/snapshots/snapshot.faiss',
                      ?, ?, 2, 'l2', 1)
            """,
            (descriptor.sha256, descriptor.size_bytes),
        )
        connection.execute(
            "INSERT INTO snapshot_membership VALUES ('snapshot', ?, 1)",
            (HEX_B,),
        )
        connection.execute(
            "UPDATE retrieval_builds SET state = 'validating' WHERE build_id = 'build'"
        )
        connection.execute(
            "UPDATE vector_snapshots SET state = 'validating' WHERE snapshot_id = 'snapshot'"
        )
        connection.execute(
            "UPDATE vector_snapshots SET state = 'ready' WHERE snapshot_id = 'snapshot'"
        )
        connection.execute(
            "UPDATE retrieval_builds SET state = 'ready' WHERE build_id = 'build'"
        )
        connection.execute(
            "UPDATE retrieval_builds SET state = 'committed_pending_checkpoint' WHERE build_id = 'build'"
        )
        connection.execute(
            "UPDATE retrieval_builds SET state = 'fully_complete' WHERE build_id = 'build'"
        )
        connection.execute(
            """
            UPDATE retrieval_runtime
            SET active_snapshot_id = 'snapshot', active_build_id = 'build',
                publication_generation = 1, write_epoch = ?,
                v1_fallback_open = ?, write_enabled = ?
            WHERE runtime_id = 1
            """,
            (epoch, int(epoch == 0), int(epoch > 0)),
        )
        connection.commit()
    finally:
        connection.close()
    backups = v2_root / "backups"
    backups.mkdir()
    checkpoint = backups / "catalog-current-bootstrap-fixture.sqlite3"
    source = sqlite3.connect(catalog)
    destination = sqlite3.connect(checkpoint)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    evidence = v2_root / "evidence" / "bootstrap-fixture"
    evidence.mkdir(parents=True)
    (evidence / "committed-floor.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "publication_id": "bootstrap-fixture",
                "publication_generation": 1,
                "write_epoch": epoch,
                "v1_fallback_floor": "open" if epoch == 0 else "closed",
                "active_snapshot_id": "snapshot",
                "checkpoint_relative_path": (
                    "retrieval/v2/backups/catalog-current-bootstrap-fixture.sqlite3"
                ),
                "checkpoint_sha256": hashlib.sha256(
                    checkpoint.read_bytes()
                ).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return legacy, snapshot_path


def test_absent_native_catalog_selects_legacy_without_creating_files(tmp_path: Path):
    legacy = tmp_path / "reports.db"
    before = set(tmp_path.rglob("*"))

    selection = inspect_runtime(legacy)

    assert selection.mode == "legacy_v1"
    assert set(tmp_path.rglob("*")) == before


def test_missing_post_successor_catalog_restores_checkpoint_without_reopening_v1(
    tmp_path: Path,
):
    fixture_root = tmp_path / "published"
    fixture_root.mkdir()
    data_root, request = make_native_install(fixture_root)
    outcome = PublicationCoordinator(data_root).publish(request)
    legacy = data_root / "reports.db"
    legacy.write_bytes(b"legacy-must-remain-closed")
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    lost_catalog = catalog.with_name("catalog.removed-for-test.sqlite3")
    catalog.rename(lost_catalog)

    selection = reconcile_and_inspect_runtime(legacy)
    guarded = guard_before_retrieval_write(legacy)

    assert outcome.write_epoch == 1
    assert selection.mode == "native"
    assert selection.write_epoch == 1
    assert selection.v1_fallback_open is False
    assert selection.active_snapshot_id == "snapshot-successor"
    assert guarded.mode == "native"
    assert catalog.is_file()
    assert lost_catalog.is_file()


def test_missing_native_catalog_with_v2_evidence_never_selects_legacy_for_writes(
    tmp_path: Path,
):
    fixture_root = tmp_path / "guarded"
    fixture_root.mkdir()
    data_root, request = make_native_install(fixture_root)
    PublicationCoordinator(data_root).publish(request)
    legacy = data_root / "reports.db"
    legacy.write_bytes(b"legacy-must-remain-closed")
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    lost_catalog = catalog.with_name("catalog.removed-for-guard-test.sqlite3")
    catalog.rename(lost_catalog)

    with pytest.raises(RetrievalBootstrapError, match="V2 recovery evidence"):
        inspect_runtime(legacy)
    with pytest.raises(RetrievalBootstrapError, match="V2 recovery evidence"):
        guard_before_retrieval_write(legacy)

    assert not catalog.exists()
    assert lost_catalog.is_file()


def test_valid_epoch_zero_seed_is_readable_but_generic_writes_are_blocked(
    tmp_path: Path,
):
    legacy, _snapshot = _native_install(tmp_path)

    selection = inspect_runtime(legacy)

    assert selection.mode == "native"
    assert selection.write_epoch == 0
    assert not selection.write_enabled
    with pytest.raises(RetrievalWriteBlocked, match="writes are disabled"):
        guard_before_retrieval_write(legacy)


def test_epoch_zero_write_enabled_bit_cannot_bypass_the_generic_guard(
    tmp_path: Path,
):
    legacy, _snapshot = _native_install(tmp_path)
    catalog = tmp_path / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        connection.execute(
            "UPDATE retrieval_runtime SET write_enabled = 1 WHERE runtime_id = 1"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RetrievalWriteBlocked, match="epoch zero"):
        guard_before_retrieval_write(legacy)


def test_epoch_zero_rejects_a_duck_typed_writer_lease(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path)

    class FakeLease:
        def assert_owned(self, _data_root):
            return None

    with pytest.raises(RetrievalWriteBlocked, match="writer lease is invalid"):
        guard_before_retrieval_write(
            legacy,
            first_successor_writer_lease=FakeLease(),
        )


def test_epoch_zero_lease_gate_rejects_wrong_root_nonce_and_release(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path)
    other_root = tmp_path / "other-root"
    other_root.mkdir()

    with NativeWriterLock(other_root) as wrong_root_lease:
        with pytest.raises(WriterLockError, match="different data root"):
            guard_before_retrieval_write(
                legacy,
                first_successor_writer_lease=wrong_root_lease,
            )

    lock_path = tmp_path / "retrieval" / "v2" / "writer.lock"
    with NativeWriterLock(tmp_path) as lease:
        original = lock_path.read_bytes()
        record = json.loads(original)
        record["nonce"] = "replacement-nonce"
        lock_path.write_text(json.dumps(record), encoding="utf-8")
        try:
            with pytest.raises(WriterLockError, match="no longer owned"):
                guard_before_retrieval_write(
                    legacy,
                    first_successor_writer_lease=lease,
                )
        finally:
            lock_path.write_bytes(original)

    with pytest.raises(WriterLockError, match="no longer owned"):
        guard_before_retrieval_write(
            legacy,
            first_successor_writer_lease=lease,
        )


def test_startup_reconciliation_returns_validated_active_runtime(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path)

    selection = reconcile_and_inspect_runtime(legacy)

    assert selection.mode == "native"
    assert selection.active_snapshot_id == "snapshot"
    assert not (tmp_path / "retrieval" / "v2" / "writer.lock").exists()


def test_live_writer_allows_validated_read_only_startup(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path, epoch=1)

    with NativeWriterLock(tmp_path):
        selection = reconcile_and_inspect_runtime(
            legacy,
            allow_live_writer_read=True,
        )

    assert selection.mode == "native"
    assert selection.active_snapshot_id == "snapshot"
    assert selection.publication_generation == 1


def test_live_writer_still_blocks_mutating_startup_by_default(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path, epoch=1)

    with NativeWriterLock(tmp_path):
        with pytest.raises(RetrievalBootstrapError, match="locked"):
            reconcile_and_inspect_runtime(legacy)


def test_live_writer_does_not_bypass_active_snapshot_validation(tmp_path: Path):
    legacy, snapshot = _native_install(tmp_path, epoch=1)
    snapshot.write_bytes(b"corrupt")

    with NativeWriterLock(tmp_path):
        with pytest.raises(RetrievalBootstrapError, match="fallback closure"):
            reconcile_and_inspect_runtime(
                legacy,
                allow_live_writer_read=True,
            )


def test_untrusted_writer_lock_error_does_not_bypass_reconciliation(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path, epoch=1)
    lock_path = tmp_path / "retrieval" / "v2" / "writer.lock"
    lock_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(
        RetrievalBootstrapError,
        match="record is unreadable",
    ):
        reconcile_and_inspect_runtime(legacy)


def test_closed_floor_never_falls_back_when_active_snapshot_is_corrupt(tmp_path: Path):
    legacy, snapshot = _native_install(tmp_path, epoch=1)
    snapshot.write_bytes(b"corrupt")

    with pytest.raises(RetrievalBootstrapError, match="fallback closure"):
        inspect_runtime(legacy)


def test_epoch_zero_corruption_without_sealed_bundle_fails_closed(tmp_path: Path):
    legacy, snapshot = _native_install(tmp_path)
    snapshot.unlink()

    with pytest.raises(RetrievalBootstrapError, match="no sealed compatibility evidence"):
        inspect_runtime(legacy)


def test_running_publication_blocks_before_write_work(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path, epoch=1)
    catalog = tmp_path / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        connection.execute(
            """
            INSERT INTO publication_runs (
                publication_id, from_snapshot_id, to_snapshot_id
            ) VALUES ('running-publication', 'snapshot', NULL)
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RetrievalWriteBlocked, match="already running"):
        guard_before_retrieval_write(legacy)


def test_degraded_runtime_blocks_writes(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path, epoch=1)
    catalog = tmp_path / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        connection.execute(
            """
            UPDATE retrieval_runtime
            SET degraded = 1, write_enabled = 0
            WHERE runtime_id = 1
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RetrievalWriteBlocked, match="degraded"):
        guard_before_retrieval_write(legacy)

    recovery = guard_before_retrieval_write(
        legacy,
        allow_degraded_forward_recovery=True,
    )
    assert recovery.degraded
    assert recovery.write_epoch == 1
    assert not recovery.v1_fallback_open
    assert not recovery.write_enabled


def test_forward_recovery_flag_cannot_bypass_epoch_zero_degraded_guard(tmp_path: Path):
    legacy, _snapshot = _native_install(tmp_path, epoch=0)
    catalog = tmp_path / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        connection.execute(
            "UPDATE retrieval_runtime SET degraded = 1, write_enabled = 0 WHERE runtime_id = 1"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RetrievalWriteBlocked, match="degraded"):
        guard_before_retrieval_write(
            legacy,
            allow_degraded_forward_recovery=True,
        )
