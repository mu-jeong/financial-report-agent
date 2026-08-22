from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path

import faiss
import numpy as np
import pytest

from scripts.migrations.v2.migrate_v2_user import (
    MigrationError,
    UserMigrationSettings,
    migrate_v1_to_v2,
)
from src.migrations.v2 import import_v1 as import_v1_module
from src.migrations.v2.import_v1 import V1ImportError
from src.retrieval.bootstrap import inspect_runtime
from src.retrieval.build_service import execute_incremental_update
from src.retrieval.vector_index import SnapshotDescriptor, load_index
from tests.migrations.v2.fixtures_factory.v1 import build_v1_fixture
from tests.retrieval.native_build_fixtures import _native_seed


def _settings(data_root: Path) -> UserMigrationSettings:
    return UserMigrationSettings(
        data_root=data_root,
        source_dir=data_root / "downloaded",
        model="deterministic-v1-fixture",
        extractor="pymupdf",
        parent_chunk_size=2000,
        child_chunk_size=500,
        use_parent_child=True,
    )


def _create_source_pdfs(data_root: Path, file_names: dict[str, str]) -> None:
    source = data_root / "downloaded"
    source.mkdir()
    for key, name in file_names.items():
        (source / name).write_bytes(f"fixture-pdf:{key}".encode("utf-8"))


def test_migration_reuses_v1_vectors_and_removes_only_legacy_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = build_v1_fixture(tmp_path / "data")
    _create_source_pdfs(fixture.root, fixture.file_names)
    original_hashes = fixture.current_artifact_hashes()

    def forbidden_full_rebuild(*_args, **_kwargs):
        raise AssertionError("V1 migration must not parse PDFs or embed the corpus")

    monkeypatch.setattr(
        "src.retrieval.build_service.prepare_full_corpus_build",
        forbidden_full_rebuild,
    )
    monkeypatch.setattr(
        "src.llms.embeddings.build_embeddings_model",
        forbidden_full_rebuild,
    )

    outcome = migrate_v1_to_v2(_settings(fixture.root))

    assert outcome.status == "migrated"
    assert outcome.vector_count == fixture.symbolic_n
    assert outcome.max_vector_absolute_error == 0.0
    assert outcome.removed_v1_artifacts == (
        "reports.db",
        "vector_db/index.faiss",
        "vector_db/index.pkl",
    )
    assert not (fixture.root / "reports.db").exists()
    assert not (fixture.root / "vector_db").exists()
    assert all((fixture.root / "downloaded" / name).is_file() for name in fixture.file_names.values())

    selection = inspect_runtime(fixture.root)
    assert selection.is_native
    assert selection.initialization_state == "ready"
    assert selection.active_snapshot_id == outcome.snapshot_id
    assert selection.publication_generation == 1
    assert selection.write_epoch == 1
    assert selection.write_enabled is True

    catalog = fixture.root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        row = connection.execute(
            """
            SELECT relative_path, file_sha256, size_bytes, dimension, metric, ntotal
            FROM vector_snapshots WHERE snapshot_id = ?
            """,
            (outcome.snapshot_id,),
        ).fetchone()
    assert row is not None
    descriptor = SnapshotDescriptor(
        sha256=row[1],
        size_bytes=int(row[2]),
        dimension=int(row[3]),
        metric=row[4],
        ntotal=int(row[5]),
    )
    imported = load_index(fixture.root / row[0], descriptor).reconstruct(
        range(1, fixture.symbolic_n + 1)
    )
    assert np.array_equal(
        imported[np.argsort(imported[:, 0])],
        fixture.known_vectors[np.argsort(fixture.known_vectors[:, 0])],
    )
    assert original_hashes == fixture.artifact_hashes


def test_migration_is_idempotent_after_v1_cleanup(tmp_path: Path) -> None:
    fixture = build_v1_fixture(tmp_path / "data")
    _create_source_pdfs(fixture.root, fixture.file_names)

    first = migrate_v1_to_v2(_settings(fixture.root))
    second = migrate_v1_to_v2(_settings(fixture.root))

    assert second.status == "already_migrated"
    assert second.snapshot_id == first.snapshot_id
    assert second.removed_v1_artifacts == ()


def test_unrelated_native_runtime_does_not_authorize_v1_deletion(tmp_path: Path) -> None:
    data_root, _sources = _native_seed(tmp_path)
    build_v1_fixture(data_root)

    with pytest.raises(MigrationError, match="cleanup marker"):
        migrate_v1_to_v2(_settings(data_root))

    assert (data_root / "reports.db").is_file()
    assert (data_root / "vector_db" / "index.faiss").is_file()
    assert (data_root / "vector_db" / "index.pkl").is_file()


def test_partial_v1_cleanup_retries_empty_vector_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = build_v1_fixture(tmp_path / "data")
    _create_source_pdfs(fixture.root, fixture.file_names)
    original_rmdir = Path.rmdir
    failed_once = False

    def fail_vector_directory_once(path: Path) -> None:
        nonlocal failed_once
        if path == fixture.root / "vector_db" and not failed_once:
            failed_once = True
            raise PermissionError("injected vector directory failure")
        original_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", fail_vector_directory_once)

    with pytest.raises(MigrationError, match="vector directory"):
        migrate_v1_to_v2(_settings(fixture.root))

    marker = fixture.root / "retrieval" / "v2" / "migration" / "v1-cleanup.json"
    assert (fixture.root / "vector_db").is_dir()
    assert marker.is_file()

    outcome = migrate_v1_to_v2(_settings(fixture.root))

    assert outcome.status == "already_migrated"
    assert not (fixture.root / "vector_db").exists()
    assert not marker.exists()


def test_migrated_profile_accepts_the_next_incremental_update(tmp_path: Path) -> None:
    fixture = build_v1_fixture(tmp_path / "data")
    _create_source_pdfs(fixture.root, fixture.file_names)
    connection = sqlite3.connect(fixture.root / "reports.db")
    try:
        metadata_by_name = {
            row[0]: {
                "report_type": row[1],
                "report_date": row[2],
                "target_name": row[3],
                "title": row[4],
                "broker": row[5],
            }
            for row in connection.execute(
                """
                SELECT file_name, report_type, report_date, target_name, title, broker
                FROM reports
                """
            )
        }
    finally:
        connection.close()
    migrated_vectors = np.ascontiguousarray(
        fixture.known_vectors + np.asarray([1.0, 2.0, 3.0], dtype=np.float32)
    )
    legacy_index = faiss.IndexFlatL2(int(migrated_vectors.shape[1]))
    legacy_index.add(migrated_vectors)
    faiss.write_index(legacy_index, str(fixture.root / "vector_db" / "index.faiss"))
    migrated = migrate_v1_to_v2(_settings(fixture.root))

    class SameSpaceEmbeddings:
        def __init__(self) -> None:
            self.known = {
                document.page_content: vector
                for document, vector in zip(
                    fixture.documents_by_ordinal,
                    migrated_vectors,
                    strict=True,
                )
            }

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            values: list[list[float]] = []
            for text in texts:
                vector = self.known.get(text)
                if vector is None:
                    digest = hashlib.sha256(text.encode("utf-8")).digest()
                    vector = np.asarray(
                        [1.0 + digest[0] / 255, 1.0 + digest[1] / 255, 1.0 + digest[2] / 255],
                        dtype=np.float32,
                    )
                values.append(vector.tolist())
            return values

    changed_name = next(iter(fixture.file_names.values()))
    (fixture.root / "downloaded" / changed_name).write_bytes(b"updated-pdf")

    result = execute_incremental_update(
        fixture.root,
        fixture.root / "downloaded",
        embeddings=SameSpaceEmbeddings(),
        model="deterministic-v1-fixture",
        extractor_name="pymupdf",
        extractor=lambda path, _engine: f"updated text for {path.name}",
        metadata_parser=metadata_by_name.get,
        allow_extraction_fallback=False,
        use_parent_child=True,
        parent_chunk_size=2000,
        child_chunk_size=500,
        metric="l2",
        normalization="none",
    )

    assert result is not None
    _candidate, publication = result
    assert publication.publication_generation == migrated.publication_generation + 1


def test_failed_migration_keeps_all_v1_artifacts(tmp_path: Path) -> None:
    fixture = build_v1_fixture(tmp_path / "data")
    _create_source_pdfs(fixture.root, fixture.file_names)
    missing = fixture.root / "downloaded" / next(iter(fixture.file_names.values()))
    missing.unlink()
    before = fixture.current_artifact_hashes()

    try:
        migrate_v1_to_v2(_settings(fixture.root))
    except Exception:
        pass
    else:
        raise AssertionError("migration unexpectedly succeeded with a missing source PDF")

    assert fixture.current_artifact_hashes() == before
    assert (fixture.root / "reports.db").is_file()
    assert (fixture.root / "vector_db" / "index.faiss").is_file()
    assert (fixture.root / "vector_db" / "index.pkl").is_file()


def test_publication_failure_keeps_all_v1_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = build_v1_fixture(tmp_path / "data")
    _create_source_pdfs(fixture.root, fixture.file_names)
    before = fixture.current_artifact_hashes()
    original_publication = import_v1_module.publish_candidate

    def fail_publication(*_args, **_kwargs):
        raise RuntimeError("injected publication failure")

    monkeypatch.setattr(
        "src.migrations.v2.import_v1.publish_candidate",
        fail_publication,
    )

    try:
        migrate_v1_to_v2(_settings(fixture.root))
    except RuntimeError as exc:
        assert "injected publication failure" in str(exc)
    else:
        raise AssertionError("migration unexpectedly survived publication failure")

    assert fixture.current_artifact_hashes() == before
    assert (fixture.root / "reports.db").is_file()
    assert (fixture.root / "vector_db" / "index.faiss").is_file()
    assert (fixture.root / "vector_db" / "index.pkl").is_file()

    monkeypatch.setattr(
        "src.migrations.v2.import_v1.publish_candidate",
        original_publication,
    )
    retried = migrate_v1_to_v2(_settings(fixture.root))

    assert retried.status == "migrated"
    assert not (fixture.root / "reports.db").exists()
    assert not (fixture.root / "vector_db").exists()


def test_cleanup_marker_directory_cannot_redirect_outside_data_root(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    v2_root = data_root / "retrieval" / "v2"
    outside = tmp_path / "outside"
    v2_root.mkdir(parents=True)
    outside.mkdir()
    redirect = v2_root / "migration"
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(redirect), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(f"directory junctions are unavailable: {completed.stderr}")
    else:
        redirect.symlink_to(outside, target_is_directory=True)

    with pytest.raises(V1ImportError, match="cannot be redirected"):
        import_v1_module._ensure_plain_marker_directory(data_root)

    assert not (outside / "v1-cleanup.json").exists()


def test_batch_entrypoint_uses_the_native_migration_cli() -> None:
    batch = Path("MIGRATE_V2.bat").read_text(encoding="utf-8-sig")

    assert "scripts\\migrations\\v2\\migrate_v2_user.py" in batch
    assert "rebuild_v2_successor.py" not in batch


def test_default_install_does_not_restore_langchain_community() -> None:
    requirements = Path("requirements.txt").read_text(encoding="utf-8")

    assert "langchain-community" not in requirements
    assert "langchain_community" not in requirements
