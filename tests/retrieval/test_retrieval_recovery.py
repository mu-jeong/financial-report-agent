from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest

from src.retrieval.bootstrap import RetrievalBootstrapError, inspect_runtime
from src.retrieval.publication import (
    PublicationCoordinator,
    PublicationCrash,
    PublicationRequest,
    read_durable_floors,
)
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from src.retrieval.runtime_guard import guard_before_retrieval_write
from tests.retrieval.test_retrieval_publication import make_native_install


def _catalog(data_root: Path) -> Path:
    return data_root / "retrieval" / "v2" / "catalog.sqlite3"


def _snapshot_path(data_root: Path, snapshot_id: str) -> Path:
    return data_root / "retrieval" / "v2" / "snapshots" / f"{snapshot_id}.faiss"


def _online_backup(source: Path, target: Path) -> None:
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()


def _replace_catalog_with_backup(catalog: Path, backup: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{catalog}{suffix}").unlink(missing_ok=True)
    catalog.unlink()
    shutil.copyfile(backup, catalog)


def _capture_live_wal_topology(
    data_root: Path,
    request: PublicationRequest,
    *,
    boundary: str,
    capture_root: Path,
) -> tuple[Path, Path]:
    """Recreate the exact main/WAL/SHM files visible at a crash hook."""

    catalog = _catalog(data_root)
    captured_catalog = capture_root / "catalog.sqlite3"
    captured_wal = Path(f"{captured_catalog}-wal")
    captured_shm = Path(f"{captured_catalog}-shm")

    def capture(actual_boundary: str) -> None:
        if actual_boundary != boundary:
            return
        capture_root.mkdir(parents=True)
        shutil.copyfile(catalog, captured_catalog)
        shutil.copyfile(Path(f"{catalog}-wal"), captured_wal)
        shutil.copyfile(Path(f"{catalog}-shm"), captured_shm)

    with pytest.raises(PublicationCrash) as crashed:
        PublicationCoordinator(data_root).publish(
            request,
            crash_after=boundary,
            crash_hook=capture,
        )
    assert crashed.value.boundary == boundary
    assert captured_catalog.read_bytes()[18:20] == b"\x02\x02"
    wal_bytes = captured_wal.read_bytes()
    assert len(wal_bytes) > 56
    assert int.from_bytes(wal_bytes[:4], "big") in {0x377F0682, 0x377F0683}
    assert captured_shm.stat().st_size > 0

    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{catalog}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(captured_catalog, catalog)
    shutil.copyfile(captured_wal, Path(f"{catalog}-wal"))
    shutil.copyfile(captured_shm, Path(f"{catalog}-shm"))
    return catalog, Path(f"{catalog}-wal")


def _assert_live_wal_boundary(
    catalog: Path,
    *,
    expected_runtime: tuple[object, ...],
    expected_phase: str,
) -> None:
    """Prove that the captured boundary is valid and exists only in the WAL."""

    live = sqlite3.connect(f"file:{catalog.as_posix()}?mode=ro", uri=True)
    try:
        assert live.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert live.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert live.execute("PRAGMA foreign_key_check").fetchall() == []
        assert live.execute(
            """
            SELECT publication_generation, write_epoch, v1_fallback_open,
                   active_snapshot_id
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone() == expected_runtime
        assert live.execute(
            """SELECT phase FROM publication_runs
               WHERE publication_id = 'publication-successor'"""
        ).fetchone() == (expected_phase,)
    finally:
        live.close()

    main_only = sqlite3.connect(
        f"file:{catalog.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        assert main_only.execute(
            """
            SELECT publication_generation, write_epoch, v1_fallback_open,
                   active_snapshot_id
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone() == (1, 0, 1, "snapshot-seed")
        assert main_only.execute(
            """SELECT phase FROM publication_runs
               WHERE publication_id = 'publication-successor'"""
        ).fetchone() is None
    finally:
        main_only.close()


def _corrupt_first_wal_frame(wal: Path) -> None:
    payload = bytearray(wal.read_bytes())
    page_size = int.from_bytes(payload[8:12], "big")
    if page_size == 1:
        page_size = 65_536
    corrupt_offset = 32 + 24 + (page_size // 2)
    assert 0 < page_size <= 65_536
    assert corrupt_offset < len(payload)
    payload[corrupt_offset] ^= 0xFF
    wal.write_bytes(payload)


def _corrupt_sqlite_header(catalog: Path) -> None:
    payload = bytearray(catalog.read_bytes())
    assert payload[:16] == b"SQLite format 3\x00"
    payload[0] ^= 0xFF
    catalog.write_bytes(payload)


def _runtime(data_root: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(_catalog(data_root))
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


def _running_publication(data_root: Path) -> tuple[str, str]:
    connection = sqlite3.connect(_catalog(data_root))
    try:
        rows = connection.execute(
            """SELECT publication_id, phase FROM publication_runs
               WHERE state = 'running'"""
        ).fetchall()
        assert len(rows) == 1
        return str(rows[0][0]), str(rows[0][1])
    finally:
        connection.close()


def test_readable_stale_catalog_restores_highest_closed_floor(tmp_path: Path):
    data_root, request = make_native_install(tmp_path)
    catalog = _catalog(data_root)
    stale = tmp_path / "catalog-generation-1.sqlite3"
    _online_backup(catalog, stale)
    published = PublicationCoordinator(data_root).publish(request)
    assert published.write_epoch == 1
    assert published.v1_fallback_open is False

    _replace_catalog_with_backup(catalog, stale)
    outcome = StartupReconciler(data_root).reconcile()
    selection = inspect_runtime(data_root / "reports.db", data_root=data_root)

    assert outcome.disposition is RecoveryDisposition.CHECKPOINT_RESTORED
    assert selection.publication_generation == 2
    assert selection.write_epoch == 1
    assert selection.v1_fallback_open is False
    assert selection.active_snapshot_id == "snapshot-successor"


def test_unmatched_newer_intent_forbids_readable_stale_catalog(tmp_path: Path):
    data_root, request = make_native_install(tmp_path)
    catalog = _catalog(data_root)
    stale = tmp_path / "catalog-before-intent.sqlite3"
    _online_backup(catalog, stale)
    with pytest.raises(PublicationCrash):
        PublicationCoordinator(data_root).publish(
            request,
            crash_after="commit_intent_durable",
        )

    _replace_catalog_with_backup(catalog, stale)
    outcome = StartupReconciler(data_root).reconcile()

    assert outcome.disposition is RecoveryDisposition.FAIL_CLOSED
    assert outcome.v1_fallback_open is False
    with pytest.raises(RetrievalBootstrapError, match="durable recovery evidence"):
        inspect_runtime(data_root / "reports.db", data_root=data_root)


def _corrupt(path: Path) -> None:
    path.write_bytes(b"deliberately corrupt\x00")


def test_epoch_positive_active_corruption_promotes_verified_predecessor_once(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    PublicationCoordinator(data_root).publish(request)
    _corrupt(_snapshot_path(data_root, "snapshot-successor"))

    recovered = StartupReconciler(data_root).reconcile()
    replay = StartupReconciler(data_root).reconcile()

    assert recovered.disposition is RecoveryDisposition.PREDECESSOR_DEGRADED
    assert replay.disposition is RecoveryDisposition.ACTIVE
    assert _runtime(data_root) == (
        "snapshot-seed",
        "build-seed",
        None,
        3,
        1,
        0,
        1,
        0,
    )
    connection = sqlite3.connect(_catalog(data_root))
    try:
        assert connection.execute(
            """SELECT state FROM vector_snapshots
               WHERE snapshot_id = 'snapshot-successor'"""
        ).fetchone() == ("failed",)
    finally:
        connection.close()
    floors = read_durable_floors(data_root)
    assert [floor.publication_generation for floor in floors] == [1, 2, 3]
    assert floors[0].v1_fallback_floor == "open"
    assert all(
        floor.v1_fallback_floor == "closed" for floor in floors[1:]
    )


@pytest.mark.parametrize(
    ("boundary", "expected_phase", "intent_exists"),
    [
        ("recovery_journal_durable", "journal_created", False),
        ("recovery_commit_intent_written", "journal_created", True),
        ("recovery_commit_intent_durable", "commit_intent_durable", True),
        ("during_recovery_transaction", "commit_intent_durable", True),
        (
            "recovery_pointer_committed",
            "committed_pending_checkpoint",
            True,
        ),
        (
            "recovery_checkpoint_created",
            "committed_pending_checkpoint",
            True,
        ),
        ("recovery_floor_durable", "committed_floor_durable", True),
    ],
)
def test_active_recovery_replays_each_crash_boundary(
    tmp_path: Path,
    boundary: str,
    expected_phase: str,
    intent_exists: bool,
) -> None:
    data_root, request = make_native_install(tmp_path)
    PublicationCoordinator(data_root).publish(request)
    _corrupt(_snapshot_path(data_root, "snapshot-successor"))
    with pytest.raises(PublicationCrash) as captured:
        StartupReconciler(data_root).reconcile(crash_after=boundary)
    assert captured.value.boundary == boundary
    publication_id, phase = _running_publication(data_root)
    intent_path = (
        data_root
        / "retrieval"
        / "v2"
        / "evidence"
        / publication_id
        / "commit-intent.json"
    )
    assert phase == expected_phase
    assert intent_path.exists() is intent_exists

    first = StartupReconciler(data_root).reconcile()
    second = StartupReconciler(data_root).reconcile()

    assert first.disposition is RecoveryDisposition.PREDECESSOR_DEGRADED
    assert second.disposition is RecoveryDisposition.ACTIVE
    assert _runtime(data_root)[0:8] == (
        "snapshot-seed",
        "build-seed",
        None,
        3,
        1,
        0,
        1,
        0,
    )
    assert [
        floor.publication_generation for floor in read_durable_floors(data_root)
    ] == [
        1,
        2,
        3,
    ]


def test_corrupt_catalog_restores_only_matching_floor_checkpoint(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    PublicationCoordinator(data_root).publish(request)
    _corrupt(_catalog(data_root))

    restored = StartupReconciler(data_root).reconcile()

    assert restored.disposition is RecoveryDisposition.CHECKPOINT_RESTORED
    assert restored.restored_checkpoint is True
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


def test_checkpoint_restore_does_not_retain_stale_sqlite_sidecars(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    PublicationCoordinator(data_root).publish(request)
    wal = Path(f"{_catalog(data_root)}-wal")
    shared_memory = Path(f"{_catalog(data_root)}-shm")
    wal.write_bytes(b"stale wal")
    shared_memory.write_bytes(b"stale shm")
    _corrupt(_catalog(data_root))

    restored = StartupReconciler(data_root).reconcile()

    assert restored.disposition is RecoveryDisposition.CHECKPOINT_RESTORED
    assert not wal.exists()
    assert not shared_memory.exists()
    quarantined = list(
        (data_root / "retrieval" / "v2" / "quarantine").glob("*.rejected")
    )
    # SQLite may discard an invalid WAL while the failed catalog is opened;
    # any sidecar still present after close is moved aside by recovery.
    assert all(
        path.read_bytes() in {b"stale wal", b"stale shm"}
        for path in quarantined
    )


@pytest.mark.parametrize(
    ("boundary", "corrupt_file", "expected_runtime", "expected_phase"),
    [
        (
            "commit_intent_durable",
            "main",
            (1, 0, 1, "snapshot-seed"),
            "commit_intent_durable",
        ),
        (
            "committed_pending_checkpoint",
            "wal",
            (2, 1, 0, "snapshot-successor"),
            "committed_pending_checkpoint",
        ),
    ],
)
def test_live_wal_corruption_before_matching_floor_fails_closed(
    tmp_path: Path,
    boundary: str,
    corrupt_file: str,
    expected_runtime: tuple[object, ...],
    expected_phase: str,
) -> None:
    data_root, request = make_native_install(tmp_path)
    catalog, wal = _capture_live_wal_topology(
        data_root,
        request,
        boundary=boundary,
        capture_root=tmp_path / f"captured-{boundary}",
    )
    _assert_live_wal_boundary(
        catalog,
        expected_runtime=expected_runtime,
        expected_phase=expected_phase,
    )
    decoy = data_root / "retrieval" / "v2" / "snapshots" / "filename-decoy.faiss"
    shutil.copyfile(_snapshot_path(data_root, "snapshot-successor"), decoy)

    if corrupt_file == "main":
        _corrupt_sqlite_header(catalog)
    else:
        _corrupt_first_wal_frame(wal)

    outcome = StartupReconciler(data_root).reconcile()

    assert outcome.disposition is RecoveryDisposition.FAIL_CLOSED
    assert outcome.publication_generation is None
    assert outcome.write_epoch is None
    assert outcome.active_snapshot_id is None
    assert outcome.v1_fallback_open is False
    assert outcome.write_enabled is False
    assert outcome.restored_checkpoint is False
    assert decoy.is_file()
    with pytest.raises(RetrievalBootstrapError):
        inspect_runtime(data_root / "reports.db", data_root=data_root)
    with pytest.raises(RetrievalBootstrapError):
        guard_before_retrieval_write(
            data_root / "reports.db",
            data_root=data_root,
        )


@pytest.mark.parametrize("corrupt_file", ["main", "wal"])
def test_live_wal_corruption_after_floor_restores_only_same_floor_checkpoint(
    tmp_path: Path,
    corrupt_file: str,
) -> None:
    data_root, request = make_native_install(tmp_path)
    catalog, wal = _capture_live_wal_topology(
        data_root,
        request,
        boundary="committed_floor_durable",
        capture_root=tmp_path / f"captured-floor-{corrupt_file}",
    )
    _assert_live_wal_boundary(
        catalog,
        expected_runtime=(2, 1, 0, "snapshot-successor"),
        expected_phase="committed_floor_durable",
    )
    backups = data_root / "retrieval" / "v2" / "backups"
    decoy_checkpoint = backups / "catalog-current-g999-filename-decoy.sqlite3"
    shutil.copyfile(
        backups / "catalog-current-g1-publication-seed.sqlite3",
        decoy_checkpoint,
    )
    decoy_snapshot = (
        data_root / "retrieval" / "v2" / "snapshots" / "filename-decoy.faiss"
    )
    shutil.copyfile(_snapshot_path(data_root, "snapshot-successor"), decoy_snapshot)

    if corrupt_file == "main":
        _corrupt_sqlite_header(catalog)
    else:
        _corrupt_first_wal_frame(wal)

    restored = StartupReconciler(data_root).reconcile()
    selection = inspect_runtime(data_root / "reports.db", data_root=data_root)
    highest_floor = read_durable_floors(data_root)[-1]

    assert restored.disposition is RecoveryDisposition.CHECKPOINT_RESTORED
    assert restored.restored_checkpoint is True
    assert restored.publication_generation == highest_floor.publication_generation == 2
    assert restored.write_epoch == highest_floor.write_epoch == 1
    assert restored.active_snapshot_id == highest_floor.active_snapshot_id
    assert restored.active_snapshot_id == "snapshot-successor"
    assert restored.v1_fallback_open is highest_floor.fallback_open is False
    assert selection.publication_generation == 2
    assert selection.write_epoch == 1
    assert selection.active_snapshot_id == "snapshot-successor"
    assert selection.v1_fallback_open is False
    assert decoy_checkpoint.is_file()
    assert decoy_snapshot.is_file()


def test_corrupt_live_wal_and_floor_checkpoint_ignore_unreferenced_copy(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    catalog, wal = _capture_live_wal_topology(
        data_root,
        request,
        boundary="committed_floor_durable",
        capture_root=tmp_path / "captured-floor-checkpoint-corruption",
    )
    _assert_live_wal_boundary(
        catalog,
        expected_runtime=(2, 1, 0, "snapshot-successor"),
        expected_phase="committed_floor_durable",
    )
    highest_floor = read_durable_floors(data_root)[-1]
    checkpoint = data_root / highest_floor.checkpoint_relative_path
    unreferenced = checkpoint.with_name("catalog-current-g2-unreferenced.sqlite3")
    shutil.copyfile(checkpoint, unreferenced)

    _corrupt_first_wal_frame(wal)
    _corrupt_sqlite_header(checkpoint)

    outcome = StartupReconciler(data_root).reconcile()

    assert outcome.disposition is RecoveryDisposition.FAIL_CLOSED
    assert outcome.publication_generation is None
    assert outcome.write_epoch is None
    assert outcome.active_snapshot_id is None
    assert outcome.v1_fallback_open is False
    assert outcome.write_enabled is False
    assert outcome.restored_checkpoint is False
    assert unreferenced.is_file()
    with pytest.raises(RetrievalBootstrapError):
        inspect_runtime(data_root / "reports.db", data_root=data_root)


def test_unresolved_commit_intent_forbids_rollback_backup_restore(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    with pytest.raises(PublicationCrash):
        PublicationCoordinator(data_root).publish(
            request,
            crash_after="committed_pending_checkpoint",
        )
    rollback_backups = list(
        (data_root / "retrieval" / "v2" / "backups").glob(
            "catalog-rollback-*.sqlite3"
        )
    )
    assert len(rollback_backups) == 1
    _corrupt(_catalog(data_root))

    outcome = StartupReconciler(data_root).reconcile()

    assert outcome.disposition is RecoveryDisposition.FAIL_CLOSED
    assert outcome.write_enabled is False
    assert _catalog(data_root).read_bytes() == b"deliberately corrupt\x00"


def test_corrupt_active_and_predecessor_fails_closed_without_v1_reopen(
    tmp_path: Path,
) -> None:
    data_root, request = make_native_install(tmp_path)
    PublicationCoordinator(data_root).publish(request)
    _corrupt(_snapshot_path(data_root, "snapshot-successor"))
    _corrupt(_snapshot_path(data_root, "snapshot-seed"))

    outcome = StartupReconciler(data_root).reconcile()

    assert outcome.disposition is RecoveryDisposition.FAIL_CLOSED
    assert outcome.v1_fallback_open is False
    assert outcome.write_epoch == 1
    assert outcome.degraded is True
    assert outcome.write_enabled is False
    assert _runtime(data_root) == (
        "snapshot-successor",
        "build-successor",
        "snapshot-seed",
        2,
        1,
        0,
        1,
        0,
    )


def test_restore_never_drops_below_closed_recovery_floor(tmp_path: Path) -> None:
    data_root, request = make_native_install(tmp_path)
    PublicationCoordinator(data_root).publish(request)
    _corrupt(_snapshot_path(data_root, "snapshot-successor"))
    StartupReconciler(data_root).reconcile()
    assert _runtime(data_root)[3:6] == (3, 1, 0)
    _corrupt(_catalog(data_root))

    restored = StartupReconciler(data_root).reconcile()

    assert restored.disposition is RecoveryDisposition.CHECKPOINT_RESTORED
    assert _runtime(data_root) == (
        "snapshot-seed",
        "build-seed",
        None,
        3,
        1,
        0,
        1,
        0,
    )
    assert read_durable_floors(data_root)[-1].publication_generation == 3
    assert read_durable_floors(data_root)[-1].fallback_open is False
