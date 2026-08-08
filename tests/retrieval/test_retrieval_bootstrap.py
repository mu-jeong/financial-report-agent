from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.retrieval import publication as publication_module
from src.retrieval import recovery as recovery_module
from src.retrieval.bootstrap import (
    RetrievalBootstrapError,
    inspect_runtime,
    reconcile_and_inspect_runtime,
)
from src.retrieval.runtime_guard import RetrievalWriteBlocked, guard_before_retrieval_write
from src.retrieval.writer_lock import NativeWriterLock
from tests.retrieval.native_build_fixtures import _native_seed


def _native_install(tmp_path: Path) -> tuple[Path, Path]:
    data_root, _sources = _native_seed(tmp_path)
    selection = inspect_runtime(data_root)
    assert selection.active_snapshot_id is not None
    with sqlite3.connect(selection.paths.catalog) as connection:
        relative_path = connection.execute(
            "SELECT relative_path FROM vector_snapshots WHERE snapshot_id = ?",
            (selection.active_snapshot_id,),
        ).fetchone()[0]
    return data_root, data_root / relative_path


def test_absent_native_catalog_is_uninitialized_without_creating_files(tmp_path: Path):
    before = set(tmp_path.rglob("*"))

    selection = inspect_runtime(tmp_path)

    assert selection.mode == "uninitialized"
    assert selection.initialization_state == "uninitialized"
    assert set(tmp_path.rglob("*")) == before


def test_startup_directs_complete_v1_install_to_migration_before_initializing(
    tmp_path: Path,
):
    (tmp_path / "reports.db").write_bytes(b"legacy")
    vector_root = tmp_path / "vector_db"
    vector_root.mkdir()
    (vector_root / "index.faiss").write_bytes(b"legacy")
    (vector_root / "index.pkl").write_bytes(b"legacy")

    with pytest.raises(RetrievalBootstrapError, match="MIGRATE_V2.bat"):
        reconcile_and_inspect_runtime(tmp_path)

    assert not (tmp_path / "retrieval").exists()


def test_startup_reconciliation_returns_validated_active_runtime(tmp_path: Path):
    data_root, _snapshot = _native_install(tmp_path)

    selection = reconcile_and_inspect_runtime(data_root)

    assert selection.mode == "native"
    assert selection.active_snapshot_id is not None
    assert not (data_root / "retrieval" / "v2" / "writer.lock").exists()


def test_fast_read_startup_runs_no_full_catalog_integrity_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data_root, _snapshot = _native_install(tmp_path)

    def reject_full_validation(_connection):
        raise AssertionError("fast read startup ran a full catalog scan")

    monkeypatch.setattr(publication_module, "_validate_catalog_integrity", reject_full_validation)
    monkeypatch.setattr(
        recovery_module.StartupReconciler,
        "reconcile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("clean fast read entered recovery")
        ),
    )

    selection = reconcile_and_inspect_runtime(data_root, prefer_fast_read=True)

    assert selection.mode == "native"
    assert selection.active_snapshot_id is not None


def test_direct_runtime_inspection_runs_one_full_catalog_integrity_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data_root, _snapshot = _native_install(tmp_path)
    original_validate = publication_module._validate_catalog_integrity
    validations = 0

    def count_full_validation(connection):
        nonlocal validations
        validations += 1
        original_validate(connection)

    monkeypatch.setattr(publication_module, "_validate_catalog_integrity", count_full_validation)

    assert inspect_runtime(data_root).mode == "native"
    assert validations == 1


def test_strict_startup_runs_one_full_catalog_integrity_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data_root, _snapshot = _native_install(tmp_path)
    original_validate = recovery_module._validate_catalog_integrity
    validations = 0

    def count_full_validation(connection):
        nonlocal validations
        validations += 1
        original_validate(connection)

    monkeypatch.setattr(recovery_module, "_validate_catalog_integrity", count_full_validation)

    assert reconcile_and_inspect_runtime(data_root).mode == "native"
    assert validations == 1


def test_live_writer_allows_validated_read_only_startup(tmp_path: Path):
    data_root, _snapshot = _native_install(tmp_path)

    with NativeWriterLock(data_root):
        selection = reconcile_and_inspect_runtime(data_root, allow_live_writer_read=True)

    assert selection.mode == "native"
    assert selection.active_snapshot_id is not None


def test_live_writer_still_blocks_mutating_startup_by_default(tmp_path: Path):
    data_root, _snapshot = _native_install(tmp_path)

    with NativeWriterLock(data_root):
        with pytest.raises(RetrievalBootstrapError, match="locked"):
            reconcile_and_inspect_runtime(data_root)


def test_live_writer_does_not_bypass_active_snapshot_validation(tmp_path: Path):
    data_root, snapshot = _native_install(tmp_path)
    snapshot.write_bytes(b"corrupt")

    with NativeWriterLock(data_root):
        with pytest.raises(RetrievalBootstrapError, match="invalid"):
            reconcile_and_inspect_runtime(data_root, allow_live_writer_read=True)


def test_untrusted_writer_lock_error_does_not_bypass_reconciliation(tmp_path: Path):
    data_root, _snapshot = _native_install(tmp_path)
    lock_path = data_root / "retrieval" / "v2" / "writer.lock"
    lock_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(RetrievalBootstrapError, match="record is unreadable"):
        reconcile_and_inspect_runtime(data_root)


def test_corrupt_active_snapshot_fails_closed(tmp_path: Path):
    data_root, snapshot = _native_install(tmp_path)
    snapshot.write_bytes(b"corrupt")

    with pytest.raises(RetrievalBootstrapError, match="invalid"):
        inspect_runtime(data_root)


def test_running_publication_blocks_before_write_work(tmp_path: Path):
    data_root, _snapshot = _native_install(tmp_path)
    selection = inspect_runtime(data_root)
    with sqlite3.connect(selection.paths.catalog) as connection:
        connection.execute(
            """
            INSERT INTO publication_runs (
                publication_id, from_snapshot_id, to_snapshot_id
            ) VALUES ('running-publication', ?, NULL)
            """,
            (selection.active_snapshot_id,),
        )
        connection.commit()

    with pytest.raises(RetrievalWriteBlocked, match="already running"):
        guard_before_retrieval_write(data_root)


def test_degraded_runtime_blocks_writes_but_allows_forward_recovery(tmp_path: Path):
    data_root, _snapshot = _native_install(tmp_path)
    selection = inspect_runtime(data_root)
    with sqlite3.connect(selection.paths.catalog) as connection:
        connection.execute(
            "UPDATE retrieval_runtime SET degraded = 1, write_enabled = 0 WHERE runtime_id = 1"
        )
        connection.commit()

    with pytest.raises(RetrievalWriteBlocked, match="degraded"):
        guard_before_retrieval_write(data_root)

    recovery = guard_before_retrieval_write(
        data_root,
        allow_degraded_forward_recovery=True,
    )
    assert recovery.degraded
    assert recovery.write_epoch > 0
    assert not recovery.write_enabled
