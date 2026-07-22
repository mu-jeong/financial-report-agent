from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.migrations.v2 import migrate_v2_user as migration_module
from scripts.migrations.v2.migrate_v2_user import (
    MigrationError,
    UserMigrationSettings,
    migrate_v1_to_v2,
)
from src.retrieval.bootstrap import inspect_runtime
from src.retrieval.update_lock import RetrievalUpdateLock, RetrievalUpdateLockError
from tests.retrieval.test_retrieval_build_service import (
    DeterministicEmbeddings,
    _legacy_install,
    _profile,
)


class WrongEmbeddings:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[100.0, 100.0, 100.0] for _text in texts]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installation(tmp_path: Path) -> tuple[UserMigrationSettings, dict[str, str]]:
    install_root = tmp_path / "한글 경로"
    data_root = install_root / "data"
    ascii_seed = tmp_path / "v1-seed"
    ascii_seed.mkdir()
    original_hashes = _legacy_install(ascii_seed)
    install_root.mkdir()
    ascii_seed.rename(data_root)
    source_dir = data_root / "downloaded"
    source_dir.mkdir()
    for file_name in ("a.pdf", "b.pdf"):
        (source_dir / file_name).write_bytes(f"pdf:{file_name}".encode("utf-8"))

    settings = UserMigrationSettings(
        install_root=install_root,
        data_root=data_root,
        db_path=data_root / "reports.db",
        faiss_dir=data_root / "vector_db",
        source_dir=source_dir,
        model=_profile().model,
        extractor="pymupdf",
        parent_chunk_size=2000,
        child_chunk_size=500,
        single_chunk_size=1500,
        use_parent_child=True,
    )
    return settings, original_hashes


def _only_run_root(settings: UserMigrationSettings) -> Path:
    runs_root = settings.data_root.parent / ".v2m"
    run_roots = sorted(path for path in runs_root.iterdir() if path.is_dir())
    assert len(run_roots) == 1
    return run_roots[0]


def _journal_phase(run_root: Path) -> str:
    journal = json.loads(
        (run_root / "cutover-journal.json").read_text(encoding="utf-8")
    )
    return journal["phase"]


def _assert_native_smoke(
    db_path: Path,
    expected_snapshot_id: str,
    require_write: bool,
) -> None:
    selection = inspect_runtime(db_path, validate_snapshot=True)
    assert selection.mode == "native"
    assert selection.active_snapshot_id == expected_snapshot_id
    assert selection.write_epoch > 0
    assert selection.v1_fallback_open is False
    assert selection.write_enabled is True
    assert selection.degraded is False
    if require_write:
        assert selection.write_enabled is True


def test_one_click_migration_preserves_v1_and_activates_converted_seed(
    tmp_path: Path,
) -> None:
    settings, original_hashes = _installation(tmp_path)

    outcome = migrate_v1_to_v2(
        settings,
        embeddings_factory=DeterministicEmbeddings,
        smoke_check=_assert_native_smoke,
    )

    selection = inspect_runtime(settings.db_path, validate_snapshot=True)
    assert outcome.status == "migrated"
    assert outcome.snapshot_id == selection.active_snapshot_id
    assert outcome.write_epoch > 0
    assert outcome.v1_fallback_open is False
    assert outcome.write_enabled is True
    assert selection.predecessor_snapshot_id is None
    assert outcome.backup_root is not None
    assert (outcome.backup_root / "copy-manifest.json").is_file()
    assert outcome.receipt_path is not None and outcome.receipt_path.is_file()
    receipt = json.loads(outcome.receipt_path.read_text(encoding="utf-8"))
    assert receipt["profile_assumptions"] == {
        "chunk_policy": "current-config-unattested",
        "extractor": "current-config-unattested",
    }
    assert "historical embedding model revision is unknown" in receipt[
        "assessment_uncertainties"
    ]
    assert {
        relative: _sha256(settings.data_root / relative)
        for relative in original_hashes
    } == original_hashes
    assert (settings.data_root / "retrieval" / "v2" / "catalog.sqlite3").is_file()
    with sqlite3.connect(
        settings.data_root / "retrieval" / "v2" / "catalog.sqlite3"
    ) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vector_snapshots").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM retrieval_builds").fetchone() == (1,)
        assert connection.execute(
            "SELECT extractor FROM embedding_profiles"
        ).fetchone() == (
            "legacy-v1-import|configured=pymupdf|unattested",
        )


def test_migration_profile_records_configured_fallback_policy(tmp_path: Path) -> None:
    settings, _original_hashes = _installation(tmp_path)
    settings = replace(
        settings,
        extractor="pymupdf",
        fallback_extractor="opendataloader",
    )

    profile = migration_module._embedding_profile(settings, dimension=3, metric="l2")

    assert profile.extractor == (
        "legacy-v1-import|configured="
        "pymupdf|fallback=opendataloader|unattested"
    )


def test_cli_settings_only_carry_fallback_for_the_primary_extractor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.configs import config

    monkeypatch.setattr(config, "EXTRACTION_ENGINE", "pymupdf")
    monkeypatch.setattr(config, "EXTRACTION_FALLBACK_ENGINE", "opendataloader")
    monkeypatch.setattr(config, "UNEMBEDDED_EXTRACTION_ENGINE", "")

    primary = migration_module._settings_from_cli(tmp_path / "primary")

    assert primary.extractor == "pymupdf"
    assert primary.fallback_extractor == "opendataloader"

    monkeypatch.setattr(config, "UNEMBEDDED_EXTRACTION_ENGINE", "marker")

    override = migration_module._settings_from_cli(tmp_path / "override")

    assert override.extractor == "marker"
    assert override.fallback_extractor is None


def test_canary_failure_never_activates_staged_retrieval(tmp_path: Path) -> None:
    settings, original_hashes = _installation(tmp_path)

    with pytest.raises(MigrationError, match="same-space canary"):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=WrongEmbeddings,
            smoke_check=_assert_native_smoke,
        )

    selection = inspect_runtime(settings.db_path, validate_snapshot=True)
    assert selection.mode == "legacy_v1"
    assert not (settings.data_root / "retrieval").exists()
    assert {
        relative: _sha256(settings.data_root / relative)
        for relative in original_hashes
    } == original_hashes


def test_live_smoke_failure_automatically_rolls_back_to_v1(tmp_path: Path) -> None:
    settings, _original_hashes = _installation(tmp_path)
    calls: list[Path] = []

    def smoke(db_path: Path, expected_snapshot_id: str, require_write: bool) -> None:
        calls.append(db_path)
        if db_path.parent == settings.data_root:
            raise RuntimeError("simulated GUI startup failure")
        _assert_native_smoke(db_path, expected_snapshot_id, require_write)

    with pytest.raises(MigrationError, match="rolled back"):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=smoke,
        )

    assert len(calls) == 2
    assert inspect_runtime(settings.db_path).mode == "legacy_v1"
    assert not (settings.data_root / "retrieval").exists()
    failed_candidates = list(
        (settings.data_root.parent / ".v2m").glob(
            "*/failed-retrieval"
        )
    )
    assert len(failed_candidates) == 1
    assert (failed_candidates[0] / "v2" / "catalog.sqlite3").is_file()


def test_live_smoke_remains_inside_the_shared_cutover_fence(tmp_path: Path) -> None:
    settings, _original_hashes = _installation(tmp_path)
    phases: list[bool] = []

    def smoke(db_path: Path, expected_snapshot_id: str, require_write: bool) -> None:
        phases.append(require_write)
        if db_path.parent == settings.data_root:
            with pytest.raises(RetrievalUpdateLockError, match="already running"):
                with RetrievalUpdateLock(settings.data_root):
                    raise AssertionError("live smoke must remain fenced")
        _assert_native_smoke(db_path, expected_snapshot_id, require_write)

    migrate_v1_to_v2(
        settings,
        embeddings_factory=DeterministicEmbeddings,
        smoke_check=smoke,
    )

    assert phases == [True, False]
    with RetrievalUpdateLock(settings.data_root):
        pass


@pytest.mark.parametrize(
    ("crash_phase", "expected_journal_phase"),
    (
        ("retrieval_renamed", "PREPARED"),
        ("journal_activated", "ACTIVATED"),
    ),
)
def test_interrupted_cutover_is_reconciled_to_verified_on_rerun(
    tmp_path: Path,
    crash_phase: str,
    expected_journal_phase: str,
) -> None:
    settings, _original_hashes = _installation(tmp_path)

    def crash_after(phase: str) -> None:
        if phase == crash_phase:
            raise migration_module.MigrationProcessCrash(phase)

    with pytest.raises(migration_module.MigrationProcessCrash):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=_assert_native_smoke,
            cutover_hook=crash_after,
        )

    run_root = _only_run_root(settings)
    interrupted = inspect_runtime(settings.db_path, validate_snapshot=True)
    assert interrupted.mode == "native"
    assert interrupted.write_epoch == 1
    assert interrupted.publication_generation == 2
    assert _journal_phase(run_root) == expected_journal_phase
    assert not (run_root / "migration-receipt.json").exists()

    recovery_smokes: list[tuple[Path, str, bool]] = []

    def recovery_smoke(
        db_path: Path,
        expected_snapshot_id: str,
        require_write: bool,
    ) -> None:
        recovery_smokes.append((db_path, expected_snapshot_id, require_write))
        _assert_native_smoke(db_path, expected_snapshot_id, require_write)

    outcome = migrate_v1_to_v2(
        settings,
        embeddings_factory=lambda: (_ for _ in ()).throw(
            AssertionError("recovery must not rebuild or call the provider")
        ),
        smoke_check=recovery_smoke,
    )

    assert outcome.status == "recovered"
    assert outcome.run_root == run_root
    assert outcome.snapshot_id == interrupted.active_snapshot_id
    assert recovery_smokes == [
        (settings.db_path, interrupted.active_snapshot_id, False)
    ]
    assert outcome.receipt_path == run_root / "migration-receipt.json"
    assert outcome.receipt_path.is_file()
    assert _journal_phase(run_root) == "VERIFIED"


def test_receipt_write_failure_rolls_back_exact_identity_and_journals_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, _original_hashes = _installation(tmp_path)

    def fail_receipt(path: Path, payload: dict) -> None:
        raise OSError("simulated receipt write failure")

    monkeypatch.setattr(migration_module, "_write_json_once_or_same", fail_receipt)

    with pytest.raises(MigrationError, match="rolled back"):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=_assert_native_smoke,
        )

    run_root = _only_run_root(settings)
    assert _journal_phase(run_root) == "ROLLED_BACK"
    assert inspect_runtime(settings.db_path).mode == "legacy_v1"
    assert not (settings.data_root / "retrieval").exists()
    assert not (run_root / "migration-receipt.json").exists()

    failed_retrieval = run_root / "failed-retrieval"
    owner = json.loads(
        (failed_retrieval / "migration-owner.json").read_text(encoding="utf-8")
    )
    with sqlite3.connect(failed_retrieval / "v2" / "catalog.sqlite3") as connection:
        runtime = connection.execute(
            """
            SELECT active_snapshot_id, predecessor_snapshot_id,
                   publication_generation, write_epoch
            FROM retrieval_runtime
            WHERE runtime_id = 1
            """
        ).fetchone()
    assert runtime is not None
    assert runtime[0] == owner["active_snapshot_id"]
    assert runtime[1] is None
    assert runtime[2:] == (
        owner["publication_generation"],
        owner["write_epoch"],
    )


def test_receipt_written_crash_then_failed_recovery_quarantines_receipt(
    tmp_path: Path,
) -> None:
    settings, _original_hashes = _installation(tmp_path)

    def crash_after(phase: str) -> None:
        if phase == "receipt_written":
            raise migration_module.MigrationProcessCrash(phase)

    with pytest.raises(migration_module.MigrationProcessCrash):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=_assert_native_smoke,
            cutover_hook=crash_after,
        )

    run_root = _only_run_root(settings)
    receipt_path = run_root / "migration-receipt.json"
    receipt_bytes = receipt_path.read_bytes()
    assert _journal_phase(run_root) == "ACTIVATED"

    def fail_recovery_smoke(
        db_path: Path,
        expected_snapshot_id: str,
        require_write: bool,
    ) -> None:
        raise RuntimeError("simulated recovery smoke failure")

    with pytest.raises(MigrationError, match="rolled back"):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=lambda: (_ for _ in ()).throw(
                AssertionError("recovery must not rebuild or call the provider")
            ),
            smoke_check=fail_recovery_smoke,
        )

    assert _journal_phase(run_root) == "ROLLED_BACK"
    assert inspect_runtime(settings.db_path).mode == "legacy_v1"
    assert not receipt_path.exists()
    assert (run_root / "rolled-back-receipt.json").read_bytes() == receipt_bytes


def test_interrupted_cutover_refuses_changed_source_baseline(tmp_path: Path) -> None:
    settings, _original_hashes = _installation(tmp_path)

    def crash_after(phase: str) -> None:
        if phase == "retrieval_renamed":
            raise migration_module.MigrationProcessCrash(phase)

    with pytest.raises(migration_module.MigrationProcessCrash):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=_assert_native_smoke,
            cutover_hook=crash_after,
        )

    run_root = _only_run_root(settings)
    active_before = inspect_runtime(settings.db_path, validate_snapshot=True)
    (settings.source_dir / "a.pdf").write_bytes(b"changed-after-crash")

    with pytest.raises(MigrationError, match="baseline changed.*manual support"):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=lambda: (_ for _ in ()).throw(
                AssertionError("unsafe recovery must not call the provider")
            ),
            smoke_check=_assert_native_smoke,
        )

    active_after = inspect_runtime(settings.db_path, validate_snapshot=True)
    assert active_after.active_snapshot_id == active_before.active_snapshot_id
    assert _journal_phase(run_root) == "MANUAL_SUPPORT"
    assert (settings.data_root / "retrieval").is_dir()


def test_interrupted_cutover_marks_changed_owner_for_manual_support(
    tmp_path: Path,
) -> None:
    settings, _original_hashes = _installation(tmp_path)

    def crash_after(phase: str) -> None:
        if phase == "retrieval_renamed":
            raise migration_module.MigrationProcessCrash(phase)

    with pytest.raises(migration_module.MigrationProcessCrash):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=_assert_native_smoke,
            cutover_hook=crash_after,
        )

    run_root = _only_run_root(settings)
    owner_path = settings.data_root / "retrieval" / "migration-owner.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["run_id"] = "tampered-run"
    owner_path.write_text(
        migration_module.canonical_json(owner) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationError, match="manual support"):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=lambda: (_ for _ in ()).throw(
                AssertionError("unsafe recovery must not call the provider")
            ),
            smoke_check=_assert_native_smoke,
        )

    assert _journal_phase(run_root) == "MANUAL_SUPPORT"
    assert (settings.data_root / "retrieval").is_dir()


def test_interrupted_cutover_marks_preserved_empty_collision_for_support(
    tmp_path: Path,
) -> None:
    settings, _original_hashes = _installation(tmp_path)
    (settings.data_root / "retrieval").mkdir()

    def fail_live_smoke(
        db_path: Path,
        expected_snapshot_id: str,
        require_write: bool,
    ) -> None:
        if db_path.parent == settings.data_root:
            raise RuntimeError("simulated live smoke failure")
        _assert_native_smoke(db_path, expected_snapshot_id, require_write)

    def crash_after(phase: str) -> None:
        if phase == "rollback_retrieval_moved":
            raise migration_module.MigrationProcessCrash(phase)

    with pytest.raises(migration_module.MigrationProcessCrash):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=fail_live_smoke,
            cutover_hook=crash_after,
        )

    run_root = _only_run_root(settings)
    assert (run_root / "empty-retrieval").is_dir()
    assert (run_root / "failed-retrieval").is_dir()
    (settings.data_root / "retrieval").mkdir()

    with pytest.raises(MigrationError, match="manual support"):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=lambda: (_ for _ in ()).throw(
                AssertionError("unsafe recovery must not call the provider")
            ),
            smoke_check=_assert_native_smoke,
        )

    assert _journal_phase(run_root) == "MANUAL_SUPPORT"
    assert (settings.data_root / "retrieval").is_dir()
    assert (run_root / "empty-retrieval").is_dir()


def test_rerun_is_idempotent_and_does_not_create_another_backup(tmp_path: Path) -> None:
    settings, _original_hashes = _installation(tmp_path)
    first = migrate_v1_to_v2(
        settings,
        embeddings_factory=DeterministicEmbeddings,
        smoke_check=_assert_native_smoke,
    )
    runs_root = settings.data_root.parent / ".v2m"
    before_runs = sorted(path.name for path in runs_root.iterdir() if path.is_dir())

    def unexpected_embeddings():
        raise AssertionError("an already-migrated rerun must not call the provider")

    second = migrate_v1_to_v2(
        settings,
        embeddings_factory=unexpected_embeddings,
        smoke_check=_assert_native_smoke,
    )

    assert first.snapshot_id == second.snapshot_id
    assert second.status == "already_migrated"
    assert sorted(path.name for path in runs_root.iterdir() if path.is_dir()) == before_runs


def test_missing_source_pdf_fails_before_native_activation(tmp_path: Path) -> None:
    settings, _original_hashes = _installation(tmp_path)
    missing = settings.source_dir / "a.pdf"
    missing.unlink()

    with pytest.raises(MigrationError, match="source PDF is missing"):
        migrate_v1_to_v2(
            settings,
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=_assert_native_smoke,
        )

    assert inspect_runtime(settings.db_path).mode == "legacy_v1"
    assert not (settings.data_root / "retrieval").exists()


def test_no_new_logical_report_still_activates_the_converted_seed(
    tmp_path: Path,
) -> None:
    settings, original_hashes = _installation(tmp_path)
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute("DELETE FROM reports WHERE file_name = 'b.pdf'")
        connection.commit()
    (settings.source_dir / "b.pdf").unlink()
    original_hashes["reports.db"] = _sha256(settings.db_path)
    embeddings = DeterministicEmbeddings()
    outcome = migrate_v1_to_v2(
        settings,
        embeddings_factory=lambda: embeddings,
        smoke_check=_assert_native_smoke,
    )

    assert outcome.status == "migrated"
    assert outcome.write_epoch > 0
    assert outcome.write_enabled is True
    assert len(embeddings.calls) == 1
    assert all("newly searchable" not in text for text in embeddings.calls[0])
    assert {
        relative: _sha256(settings.data_root / relative)
        for relative in original_hashes
    } == original_hashes


def test_single_level_v1_fails_before_backup_or_activation(tmp_path: Path) -> None:
    settings, _original_hashes = _installation(tmp_path)

    with pytest.raises(MigrationError, match="USE_PARENT_CHILD=true"):
        migrate_v1_to_v2(
            replace(settings, use_parent_child=False),
            embeddings_factory=DeterministicEmbeddings,
            smoke_check=_assert_native_smoke,
        )

    assert not (settings.data_root.parent / ".v2m").exists()
    assert not (settings.data_root / "retrieval").exists()


def test_rollback_refuses_to_move_a_different_native_identity(tmp_path: Path) -> None:
    settings, _original_hashes = _installation(tmp_path)
    migrate_v1_to_v2(
        settings,
        embeddings_factory=DeterministicEmbeddings,
        smoke_check=_assert_native_smoke,
    )
    current = inspect_runtime(settings.db_path, validate_snapshot=True)
    live_retrieval = settings.data_root / "retrieval"
    owner = json.loads(
        (live_retrieval / "migration-owner.json").read_text(encoding="utf-8")
    )

    with pytest.raises(MigrationError, match="identity changed"):
        migration_module._rollback_owned_retrieval(
            live_retrieval,
            tmp_path / "must-not-move",
            owner,
            replace(
                current,
                publication_generation=current.publication_generation + 1,
            ),
        )

    assert live_retrieval.is_dir()
    assert inspect_runtime(settings.db_path).active_snapshot_id == current.active_snapshot_id


def test_windows_batch_uses_project_venv_and_preserves_arguments_and_exit_code() -> None:
    root = Path(__file__).resolve().parents[3]
    batch_path = root / "MIGRATE_V2.bat"
    raw = batch_path.read_bytes()
    batch = raw.decode("utf-8-sig")

    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in raw.replace(b"\r\n", b"")
    assert 'set "VENV_PYTHON=.venv\\Scripts\\python.exe"' in batch
    assert '"%VENV_PYTHON%" scripts\\migrations\\v2\\migrate_v2_user.py %*' in batch
    assert 'set "EXIT_CODE=%errorlevel%"' in batch
    assert '@if "%EXIT_CODE%"=="0" (' in batch
    assert "exit /b %EXIT_CODE%" in batch
