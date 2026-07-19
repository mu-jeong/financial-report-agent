from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

import src.retrieval.publication as publication_module
from src.retrieval.publication import (
    PublicationCoordinator,
    PublicationCrash,
    PublicationError,
    PublicationRequest,
    activate_epoch_zero_seed,
    publish_immutable_artifact,
    read_durable_floors,
)
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from src.retrieval.runtime_guard import guard_before_retrieval_write
from src.retrieval.schema import install_schema
from src.retrieval.vector_index import build_index
from src.retrieval.writer_lock import NativeWriterLock, WriterLockError


PROFILE_HASH = "10" * 32
SOURCE_HASH = "20" * 32
METADATA_HASH = "30" * 32
REPORT_UID = "40" * 32
PARENT_UID = "50" * 32
CHUNK_UID = "60" * 32
CONTENT_HASH = "70" * 32
EMBEDDING_HASH = "80" * 32
COMPATIBILITY_BUNDLE_ID = "90" * 32
SUCCESSOR_REPORT_UID = "a1" * 32
SUCCESSOR_PARENT_UID = "b1" * 32
SUCCESSOR_CHUNK_UID = "c1" * 32
SUCCESSOR_SOURCE_HASH = "d1" * 32
SUCCESSOR_METADATA_HASH = "e1" * 32
SUCCESSOR_CONTENT_HASH = "f1" * 32
SUCCESSOR_EMBEDDING_HASH = "a2" * 32


def _advance_build(
    connection: sqlite3.Connection,
    build_id: str,
    *states: str,
) -> None:
    for state in states:
        connection.execute(
            "UPDATE retrieval_builds SET state = ? WHERE build_id = ?",
            (state, build_id),
        )


def _insert_build_and_snapshot(
    connection: sqlite3.Connection,
    data_root: Path,
    *,
    build_id: str,
    snapshot_id: str,
    vector: tuple[float, float],
    chunk_uid: str = CHUNK_UID,
) -> None:
    connection.execute(
        """
        INSERT INTO retrieval_builds (
            build_id, profile_id, source_manifest_json,
            source_manifest_sha256, included_count, excluded_count,
            expected_count, exclusion_policy_version
        ) VALUES (?, 'profile-v2', ?, ?, 1, 0, 1, 'exclusions-v1')
        """,
        (
            build_id,
            '{"counts":{"discovered":1,"excluded":0,"included":1}}',
            hashlib.sha256(build_id.encode("utf-8")).hexdigest(),
        ),
    )
    _advance_build(connection, build_id, "cataloging", "vector_building")

    relative_path = f"retrieval/v2/snapshots/{snapshot_id}.faiss"
    raw = build_index(np.asarray([vector], dtype=np.float32), [1], "l2")
    descriptor = raw.write(data_root / relative_path)
    connection.execute(
        """
        INSERT INTO vector_snapshots (
            snapshot_id, build_id, relative_path, file_sha256,
            size_bytes, dimension, metric, ntotal
        ) VALUES (?, ?, ?, ?, ?, 2, 'l2', 1)
        """,
        (
            snapshot_id,
            build_id,
            relative_path,
            descriptor.sha256,
            descriptor.size_bytes,
        ),
    )
    connection.execute(
        """
        INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id)
        VALUES (?, ?, 1)
        """,
        (snapshot_id, chunk_uid),
    )
    _advance_build(connection, build_id, "validating")
    connection.execute(
        "UPDATE vector_snapshots SET state = 'validating' WHERE snapshot_id = ?",
        (snapshot_id,),
    )
    connection.execute(
        "UPDATE vector_snapshots SET state = 'ready' WHERE snapshot_id = ?",
        (snapshot_id,),
    )
    _advance_build(connection, build_id, "ready")


def make_native_install(
    tmp_path: Path,
    *,
    data_root: Path | None = None,
    compatibility_bundle_id: str = COMPATIBILITY_BUNDLE_ID,
) -> tuple[Path, PublicationRequest]:
    data_root = data_root or tmp_path / "복구 데이터 root with spaces"
    bundle = (
        data_root
        / "retrieval"
        / "compat"
        / "v1"
        / compatibility_bundle_id
    )
    if not bundle.exists():
        bundle.mkdir(parents=True)
        (bundle / "manifest.json").write_text("{}\n", encoding="utf-8")
    seed_manifest_relative = (
        "retrieval/v2/evidence/publication-seed/manifest.json"
    )
    seed_manifest = data_root / seed_manifest_relative
    seed_manifest.parent.mkdir(parents=True)
    seed_manifest_bytes = (
        json.dumps(
            {"compatibility_bundle_id": compatibility_bundle_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    seed_manifest.write_bytes(seed_manifest_bytes)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(catalog)
    try:
        install_schema(connection)
        connection.execute(
            """
            INSERT INTO embedding_profiles (
                profile_id, profile_hash, model, dimension, metric,
                normalization, prefix_template, extractor,
                parent_policy_json, child_policy_json
            ) VALUES (
                'profile-v2', ?, 'test/model', 2, 'l2', 1,
                '[Company: {target_name}, Title: {title}]\n',
                'test-extractor', '{"size":100}', '{"size":50}'
            )
            """,
            (PROFILE_HASH,),
        )
        report_id = connection.execute(
            """
            INSERT INTO reports (
                report_uid, canonical_relative_path, source_sha256,
                retrieval_metadata_sha256, report_type, report_date,
                target_name, title, broker
            ) VALUES (?, 'reports/company.pdf', ?, ?, 'company',
                      '2026-07-16', 'Company', 'Title', 'Broker')
            """,
            (REPORT_UID, SOURCE_HASH, METADATA_HASH),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order,
                content, content_sha256
            ) VALUES (?, ?, 'profile-v2', 0, 'abcdef', ?)
            """,
            (PARENT_UID, report_id, CONTENT_HASH),
        )
        connection.execute(
            """
            INSERT INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order,
                span_start, span_end, embedding_text_sha256
            ) VALUES (?, ?, 'profile-v2', 0, 0, 3, ?)
            """,
            (CHUNK_UID, PARENT_UID, EMBEDDING_HASH),
        )
        _insert_build_and_snapshot(
            connection,
            data_root,
            build_id="build-seed",
            snapshot_id="snapshot-seed",
            vector=(1.0, 0.0),
        )
        _advance_build(
            connection,
            "build-seed",
            "committed_pending_checkpoint",
            "fully_complete",
        )
        connection.execute(
            """
            INSERT INTO publication_runs (
                publication_id, to_snapshot_id,
                evidence_manifest_relative_path,
                evidence_manifest_sha256
            ) VALUES ('publication-seed', 'snapshot-seed', ?, ?)
            """,
            (
                seed_manifest_relative,
                hashlib.sha256(seed_manifest_bytes).hexdigest(),
            ),
        )
        connection.execute(
            """
            UPDATE publication_runs
            SET phase = 'fully_complete', state = 'fully_complete'
            WHERE publication_id = 'publication-seed'
            """
        )
        connection.execute(
            """
            UPDATE retrieval_runtime
            SET active_snapshot_id = 'snapshot-seed',
                active_build_id = 'build-seed',
                publication_generation = 1,
                write_epoch = 0,
                v1_fallback_open = 1,
                degraded = 0,
                write_enabled = 0
            WHERE runtime_id = 1
            """
        )
        successor_report_id = connection.execute(
            """
            INSERT INTO reports (
                report_uid, canonical_relative_path, source_sha256,
                retrieval_metadata_sha256, report_type, report_date,
                target_name, title, broker
            ) VALUES (?, 'reports/new-company.pdf', ?, ?, 'company',
                      '2026-07-16', 'New Company', 'New Title', 'Broker')
            """,
            (SUCCESSOR_REPORT_UID, SUCCESSOR_SOURCE_HASH, SUCCESSOR_METADATA_HASH),
        ).lastrowid
        connection.execute(
            """
            INSERT INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order,
                content, content_sha256
            ) VALUES (?, ?, 'profile-v2', 0, 'uvwxyz', ?)
            """,
            (SUCCESSOR_PARENT_UID, successor_report_id, SUCCESSOR_CONTENT_HASH),
        )
        connection.execute(
            """
            INSERT INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order,
                span_start, span_end, embedding_text_sha256
            ) VALUES (?, ?, 'profile-v2', 0, 0, 3, ?)
            """,
            (SUCCESSOR_CHUNK_UID, SUCCESSOR_PARENT_UID, SUCCESSOR_EMBEDDING_HASH),
        )
        _insert_build_and_snapshot(
            connection,
            data_root,
            build_id="build-successor",
            snapshot_id="snapshot-successor",
            vector=(0.0, 1.0),
            chunk_uid=SUCCESSOR_CHUNK_UID,
        )
        connection.commit()
    finally:
        connection.close()

    backups = data_root / "retrieval" / "v2" / "backups"
    backups.mkdir(parents=True)
    seed_checkpoint = backups / "catalog-current-g1-publication-seed.sqlite3"
    source = sqlite3.connect(catalog)
    destination = sqlite3.connect(seed_checkpoint)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    seed_checkpoint_hash = hashlib.sha256(seed_checkpoint.read_bytes()).hexdigest()
    seed_floor = (
        data_root
        / "retrieval"
        / "v2"
        / "evidence"
        / "publication-seed"
        / "committed-floor.json"
    )
    seed_floor.parent.mkdir(parents=True, exist_ok=True)
    seed_floor.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "publication_id": "publication-seed",
                "publication_generation": 1,
                "write_epoch": 0,
                "v1_fallback_floor": "open",
                "active_snapshot_id": "snapshot-seed",
                "checkpoint_relative_path": (
                    "retrieval/v2/backups/"
                    "catalog-current-g1-publication-seed.sqlite3"
                ),
                "checkpoint_sha256": seed_checkpoint_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

    manifest_relative = "retrieval/v2/evidence/publication-successor/manifest.json"
    manifest = data_root / manifest_relative
    manifest.parent.mkdir(parents=True)
    manifest_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "native_full_corpus_candidate",
                "publication_id": "publication-successor",
                "build_id": "build-successor",
                "snapshot_id": "snapshot-successor",
                "base_snapshot_id": "snapshot-seed",
                "base_publication_generation": 1,
                "base_write_epoch": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    manifest.write_bytes(manifest_bytes)
    request = PublicationRequest(
        publication_id="publication-successor",
        to_snapshot_id="snapshot-successor",
        evidence_manifest_relative_path=manifest_relative,
        evidence_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    return data_root, request


def _runtime(data_root: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    )
    try:
        return connection.execute(
            """
            SELECT active_snapshot_id, active_build_id,
                   predecessor_snapshot_id, publication_generation,
                   write_epoch, v1_fallback_open, degraded, write_enabled
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
    finally:
        connection.close()


def test_publication_rejects_a_writer_lease_from_another_data_root(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    other_root = tmp_path / "other-data-root"
    other_root.mkdir()
    before = _runtime(data_root)

    with NativeWriterLock(other_root) as wrong_lease:
        with pytest.raises(WriterLockError, match="different data root"):
            PublicationCoordinator(data_root).publish(
                request,
                writer_lease=wrong_lease,
            )

    assert _runtime(data_root) == before
    with sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    ) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE publication_id = ?",
            (request.publication_id,),
        ).fetchone() == (0,)


def test_first_successor_without_new_logical_member_is_rejected_before_journal(
    tmp_path: Path,
) -> None:
    data_root, _ = make_native_install(tmp_path)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        _insert_build_and_snapshot(
            connection,
            data_root,
            build_id="build-no-new-member",
            snapshot_id="snapshot-no-new-member",
            vector=(0.5, 0.5),
        )
        connection.commit()
    finally:
        connection.close()

    manifest_relative = "retrieval/v2/evidence/publication-no-new/manifest.json"
    manifest_path = data_root.joinpath(*manifest_relative.split("/"))
    manifest_path.parent.mkdir(parents=True)
    manifest_bytes = b'{"kind":"test-evidence"}\n'
    manifest_path.write_bytes(manifest_bytes)
    request = PublicationRequest(
        publication_id="publication-no-new",
        to_snapshot_id="snapshot-no-new-member",
        evidence_manifest_relative_path=manifest_relative,
        evidence_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )

    with pytest.raises(PublicationError, match="new logical corpus member"):
        PublicationCoordinator(data_root).publish(request)

    assert _runtime(data_root)[0:6] == (
        "snapshot-seed",
        "build-seed",
        None,
        1,
        0,
        1,
    )
    connection = sqlite3.connect(catalog)
    try:
        journal_count = connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE publication_id = ?",
            (request.publication_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert journal_count == 0


def test_epoch_zero_seed_activation_keeps_the_converted_snapshot_active(
    tmp_path: Path,
) -> None:
    data_root, _ = make_native_install(tmp_path)

    outcome = activate_epoch_zero_seed(
        data_root,
        snapshot_id="snapshot-seed",
        canary={
            "sample_count": 1,
            "dimension": 2,
            "minimum_cosine_similarity": 1.0,
            "maximum_norm_relative_error": 0.0,
            "self_rank_one_count": 1,
        },
    )

    assert outcome.active_snapshot_id == "snapshot-seed"
    assert outcome.predecessor_snapshot_id is None
    assert outcome.publication_generation == 2
    assert outcome.write_epoch == 1
    assert outcome.v1_fallback_open is False
    assert _runtime(data_root) == (
        "snapshot-seed",
        "build-seed",
        None,
        2,
        1,
        0,
        0,
        1,
    )
    with sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    ) as connection:
        assert connection.execute(
            "SELECT active_snapshot_id FROM retrieval_runtime"
        ).fetchone() == ("snapshot-seed",)
        assert connection.execute(
            "SELECT state FROM vector_snapshots WHERE snapshot_id = 'snapshot-seed'"
        ).fetchone() == ("ready",)
        assert connection.execute(
            "SELECT state FROM retrieval_builds WHERE build_id = 'build-seed'"
        ).fetchone() == ("fully_complete",)
    assert [
        (floor.publication_generation, floor.write_epoch, floor.active_snapshot_id)
        for floor in read_durable_floors(data_root)
    ] == [
        (1, 0, "snapshot-seed"),
        (2, 1, "snapshot-seed"),
    ]


def test_epoch_zero_publication_cannot_bypass_first_successor_epoch_increment(
    tmp_path: Path,
) -> None:
    data_root, successor = make_native_install(tmp_path)
    request = PublicationRequest(
        publication_id="publication-no-epoch-increment",
        to_snapshot_id=successor.to_snapshot_id,
        evidence_manifest_relative_path=successor.evidence_manifest_relative_path,
        evidence_manifest_sha256=successor.evidence_manifest_sha256,
        increment_write_epoch=False,
        enable_writes_on_complete=True,
    )

    with pytest.raises(
        PublicationError,
        match="must increment write epoch and close V1 fallback",
    ):
        PublicationCoordinator(data_root).publish(request)

    assert _runtime(data_root) == (
        "snapshot-seed",
        "build-seed",
        None,
        1,
        0,
        1,
        0,
        0,
    )
    connection = sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    )
    try:
        journal_count = connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE publication_id = ?",
            (request.publication_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert journal_count == 0


def test_first_successor_cannot_close_fallback_with_writes_disabled(
    tmp_path: Path,
) -> None:
    data_root, successor = make_native_install(tmp_path)
    request = PublicationRequest(
        publication_id="publication-writes-disabled",
        to_snapshot_id=successor.to_snapshot_id,
        evidence_manifest_relative_path=successor.evidence_manifest_relative_path,
        evidence_manifest_sha256=successor.evidence_manifest_sha256,
        increment_write_epoch=True,
        enable_writes_on_complete=False,
    )

    with pytest.raises(PublicationError, match="must enable writes"):
        PublicationCoordinator(data_root).publish(request)

    assert _runtime(data_root) == (
        "snapshot-seed",
        "build-seed",
        None,
        1,
        0,
        1,
        0,
        0,
    )
    connection = sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    )
    try:
        journal_count = connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE publication_id = ?",
            (request.publication_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert journal_count == 0


def test_pending_epoch_zero_journal_cannot_replay_without_epoch_increment(
    tmp_path: Path,
) -> None:
    data_root, successor = make_native_install(tmp_path)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        connection.execute(
            """
            INSERT INTO publication_runs (
                publication_id, from_snapshot_id, to_snapshot_id,
                evidence_manifest_relative_path, evidence_manifest_sha256
            ) VALUES (?, 'snapshot-seed', ?, ?, ?)
            """,
            (
                "publication-stale-no-epoch",
                successor.to_snapshot_id,
                successor.evidence_manifest_relative_path,
                successor.evidence_manifest_sha256,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    request = PublicationRequest(
        publication_id="publication-stale-no-epoch",
        to_snapshot_id=successor.to_snapshot_id,
        evidence_manifest_relative_path=successor.evidence_manifest_relative_path,
        evidence_manifest_sha256=successor.evidence_manifest_sha256,
        increment_write_epoch=False,
        enable_writes_on_complete=True,
    )

    with pytest.raises(PublicationError, match="must increment write epoch"):
        PublicationCoordinator(data_root).publish(request)

    assert _runtime(data_root) == (
        "snapshot-seed",
        "build-seed",
        None,
        1,
        0,
        1,
        0,
        0,
    )
    connection = sqlite3.connect(catalog)
    try:
        journal = connection.execute(
            "SELECT phase, state FROM publication_runs WHERE publication_id = ?",
            (request.publication_id,),
        ).fetchone()
    finally:
        connection.close()
    assert journal == ("journal_created", "running")


def test_publication_commits_checkpoint_before_closing_floor_and_is_idempotent(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    coordinator = PublicationCoordinator(data_root)

    outcome = coordinator.publish(request)

    assert _runtime(data_root) == (
        "snapshot-successor",
        "build-successor",
        "snapshot-seed",
        2,
        1,
        0,
        0,
        1,
    )
    assert outcome.publication_generation == 2
    assert outcome.write_epoch == 1
    assert outcome.v1_fallback_open is False
    assert outcome.committed_floor_relative_path == (
        "retrieval/v2/evidence/publication-successor/committed-floor.json"
    )
    checkpoint = data_root / outcome.checkpoint_relative_path
    assert checkpoint.is_file()
    assert not [
        path
        for path in checkpoint.parent.iterdir()
        if path.name.endswith(("-wal", "-shm", "-journal"))
    ]
    assert checkpoint.read_bytes()[18:20] == b'\x02\x02'
    checkpoint_connection = sqlite3.connect(
        f'file:{checkpoint.as_posix()}?mode=ro&immutable=1',
        uri=True,
    )
    try:
        assert checkpoint_connection.execute(
            'SELECT COUNT(*) FROM retrieval_runtime'
        ).fetchone() == (1,)
    finally:
        checkpoint_connection.close()
    assert (
        hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        == outcome.checkpoint_sha256
    )
    floors = read_durable_floors(data_root)
    assert [(floor.publication_generation, floor.write_epoch) for floor in floors] == [
        (1, 0),
        (2, 1),
    ]
    assert floors[-1].v1_fallback_floor == "closed"
    cleanup_marker = (
        data_root
        / "retrieval"
        / "compat"
        / "v1"
        / COMPATIBILITY_BUNDLE_ID
        / "cleanup-pending.json"
    )
    assert json.loads(cleanup_marker.read_text(encoding="utf-8")) == {
        "bundle_id": COMPATIBILITY_BUNDLE_ID,
        "closing_publication_id": "publication-successor",
        "publication_generation": 2,
        "schema_version": 1,
        "state": "cleanup_pending",
        "write_epoch": 1,
    }
    connection = sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    )
    try:
        assert connection.execute(
            "SELECT state FROM retrieval_builds WHERE build_id = 'build-successor'"
        ).fetchone() == ("fully_complete",)
        assert connection.execute(
            """SELECT state FROM vector_snapshots
               WHERE snapshot_id = 'snapshot-successor'"""
        ).fetchone() == ("ready",)
        assert connection.execute(
            """SELECT phase, state FROM publication_runs
               WHERE publication_id = 'publication-successor'"""
        ).fetchone() == ("fully_complete", "fully_complete")
    finally:
        connection.close()

    replay = coordinator.publish(request)
    assert replay == outcome
    assert read_durable_floors(data_root) == floors

    connection = sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="monotonic"):
            connection.execute(
                "UPDATE retrieval_runtime SET v1_fallback_open = 1 WHERE runtime_id = 1"
            )
    finally:
        connection.close()


def test_pointer_transaction_crash_rolls_back_every_canonical_state(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    with pytest.raises(PublicationCrash):
        PublicationCoordinator(data_root).publish(
            request,
            crash_after="during_pointer_transaction",
        )

    assert _runtime(data_root) == (
        "snapshot-seed",
        "build-seed",
        None,
        1,
        0,
        1,
        0,
        0,
    )
    connection = sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    )
    try:
        assert connection.execute(
            "SELECT state FROM retrieval_builds WHERE build_id = 'build-successor'"
        ).fetchone() == ("ready",)
        assert connection.execute(
            """SELECT phase, state FROM publication_runs
               WHERE publication_id = 'publication-successor'"""
        ).fetchone() == ("commit_intent_durable", "running")
    finally:
        connection.close()
    assert [
        floor.publication_generation for floor in read_durable_floors(data_root)
    ] == [1]


@pytest.mark.parametrize(
    "boundary",
    [
        "commit_intent_written",
        "commit_intent_durable",
        "during_pointer_transaction",
        "committed_pending_checkpoint",
        "checkpoint_created",
        "checkpoint_validated",
        "committed_floor_durable",
        "before_fully_complete",
        "fully_complete",
    ],
)
def test_startup_replay_is_idempotent_across_committed_crash_boundaries(
    tmp_path: Path,
    boundary: str,
) -> None:
    data_root, request = make_native_install(tmp_path)
    coordinator = PublicationCoordinator(data_root)
    with pytest.raises(PublicationCrash) as captured:
        coordinator.publish(request, crash_after=boundary)
    assert captured.value.boundary == boundary

    first = StartupReconciler(data_root).reconcile()
    second = StartupReconciler(data_root).reconcile()

    assert first.disposition in {
        RecoveryDisposition.PUBLICATION_COMPLETED,
        RecoveryDisposition.ACTIVE,
    }
    assert second.disposition is RecoveryDisposition.ACTIVE
    assert _runtime(data_root) == (
        "snapshot-successor",
        "build-successor",
        "snapshot-seed",
        2,
        1,
        0,
        0,
        1,
    )
    assert len(read_durable_floors(data_root)) == 2


@pytest.mark.parametrize(
    "boundary",
    ["journal_created", "artifact_validated", "rollback_backup_validated"],
)
def test_pre_intent_crash_replays_and_releases_the_next_writer(
    tmp_path: Path,
    boundary: str,
) -> None:
    data_root, request = make_native_install(tmp_path)
    with pytest.raises(PublicationCrash):
        PublicationCoordinator(data_root).publish(request, crash_after=boundary)

    first = StartupReconciler(data_root).reconcile()
    second = StartupReconciler(data_root).reconcile()
    selection = guard_before_retrieval_write(
        data_root / "reports.db",
        data_root=data_root,
    )

    assert first.disposition is RecoveryDisposition.PUBLICATION_COMPLETED
    assert second.disposition is RecoveryDisposition.ACTIVE
    assert first.active_snapshot_id == "snapshot-successor"
    assert first.v1_fallback_open is False
    assert selection.write_enabled is True
    assert selection.write_epoch == 1
    connection = sqlite3.connect(
        data_root / "retrieval" / "v2" / "catalog.sqlite3"
    )
    try:
        assert connection.execute(
            "SELECT phase, state FROM publication_runs WHERE publication_id = ?",
            (request.publication_id,),
        ).fetchone() == ("fully_complete", "fully_complete")
    finally:
        connection.close()
    floors = read_durable_floors(data_root)
    assert [
        (floor.publication_generation, floor.fallback_open) for floor in floors
    ] == [(1, True), (2, False)]


def test_snapshot_artifact_publication_is_unique_and_non_overwriting(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "same volume" / "한글"
    staged = directory / "staged.faiss"
    final = directory / "final.faiss"
    descriptor = build_index(
        np.asarray([[1.0, 2.0]], dtype=np.float32), [1], "l2"
    ).write(staged)

    publish_immutable_artifact(staged, final, descriptor)

    assert final.is_file()
    assert not staged.exists()
    second_staged = directory / "second.faiss"
    build_index(np.asarray([[1.0, 2.0]], dtype=np.float32), [1], "l2").write(
        second_staged
    )
    with pytest.raises(FileExistsError, match="already exists"):
        publish_immutable_artifact(second_staged, final, descriptor)
    assert second_staged.is_file()
    assert final.is_file()


def test_snapshot_validation_reports_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "held final"
    staged = directory / "staged.faiss"
    final = directory / "final.faiss"
    descriptor = build_index(
        np.asarray([[1.0, 2.0]], dtype=np.float32), [1], "l2"
    ).write(staged)
    real_load_index = publication_module.load_index
    real_unlink = Path.unlink

    def reject_final(path, expected):
        if Path(path) == final:
            raise ValueError("final validation failed")
        return real_load_index(path, expected)

    def hold_final(path, *args, **kwargs):
        if path == final:
            raise PermissionError("final handle is held")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(publication_module, "load_index", reject_final)
    monkeypatch.setattr(Path, "unlink", hold_final)

    with pytest.raises(PublicationError, match="cleanup failed") as captured:
        publish_immutable_artifact(staged, final, descriptor)

    assert isinstance(captured.value.__cause__, ValueError)
    assert "final handle is held" in str(captured.value)
    assert final.is_file()
    assert staged.is_file()
