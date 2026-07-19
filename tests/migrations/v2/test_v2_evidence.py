from __future__ import annotations

import hashlib
import pickle
import sqlite3
from pathlib import Path

import faiss
import numpy as np
import pytest
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_core.documents import Document

from src.migrations.v2.evidence import (
    EvidenceError,
    create_copied_v1_install,
    seal_compatibility_bundle,
    validate_compatibility_bundle,
)
from src.migrations.v2.compatibility import (
    CompatibilityReaderError,
    V1CompatibilityReader,
)
from src.retrieval.publication import PublicationCoordinator
from tests.retrieval.test_retrieval_publication import make_native_install


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_install(root: Path) -> None:
    (root / "vector_db").mkdir(parents=True)
    with sqlite3.connect(root / "reports.db") as connection:
        connection.executescript(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY,
                report_type TEXT NOT NULL,
                report_date TEXT NOT NULL,
                target_name TEXT,
                title TEXT NOT NULL,
                broker TEXT NOT NULL,
                file_name TEXT NOT NULL UNIQUE,
                is_embedded INTEGER NOT NULL
            );
            CREATE TABLE parent_chunks (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                file_name TEXT NOT NULL,
                metadata TEXT
            );
            INSERT INTO reports VALUES
                (1, 'company', '2026-01-01', 'A', 'Result', 'Broker', 'a.pdf', 1);
            INSERT INTO parent_chunks VALUES ('p1', 'alpha', 'a.pdf', NULL);
            """
        )
    index = faiss.IndexFlatL2(2)
    index.add(np.asarray([[1.0, 0.0]], dtype=np.float32))
    faiss.write_index(index, str(root / "vector_db" / "index.faiss"))
    document = Document(
        page_content="[Company: A, Title: Result]\nalpha",
        metadata={"parent_id": "p1", "file_name": "a.pdf", "child_index": 0},
    )
    with (root / "vector_db" / "index.pkl").open("wb") as stream:
        pickle.dump((InMemoryDocstore({"d1": document}), {0: "d1"}), stream)


def _source_state(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            _hash(path),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_online_backup_copy_is_off_path_and_source_bytes_remain_unchanged(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _legacy_install(source)
    before = _source_state(source)
    copied = tmp_path / "copied"

    evidence = create_copied_v1_install(source, copied)

    assert evidence.source_report_count == 1
    assert evidence.source_parent_count == 1
    assert (copied / "copy-manifest.json").is_file()
    assert _source_state(source) == before
    for relative in ("vector_db/index.faiss", "vector_db/index.pkl"):
        assert _hash(source / relative) == _hash(copied / relative)


def test_copy_refuses_existing_destination(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _legacy_install(source)
    destination = tmp_path / "existing"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        create_copied_v1_install(source, destination)


def test_bundle_is_content_addressed_read_only_and_detects_tampering(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _legacy_install(source)
    copied = tmp_path / "copied"
    create_copied_v1_install(source, copied)
    data_root = tmp_path / "data-root"

    first = seal_compatibility_bundle(copied, data_root)
    second = seal_compatibility_bundle(copied, data_root)

    assert first == second
    bundle = data_root / "retrieval" / "compat" / "v1" / first.bundle_id
    assert {path.name for path in bundle.iterdir()} == {
        "reports.db",
        "index.faiss",
        "index.pkl",
        "manifest.json",
    }
    assert all(not (path.stat().st_mode & 0o222) for path in bundle.iterdir())

    index_path = bundle / "index.faiss"
    index_path.chmod(0o600)
    index_path.write_bytes(index_path.read_bytes() + b"tampered")
    with pytest.raises(EvidenceError, match="hash mismatch"):
        validate_compatibility_bundle(data_root, first.bundle_id)


def test_epoch_zero_bundle_is_searchable_and_closure_precedes_pickle_open(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    _legacy_install(source)
    copied = tmp_path / "copied"
    create_copied_v1_install(source, copied)
    data_root = tmp_path / "data-root"
    manifest = seal_compatibility_bundle(copied, data_root)
    data_root, successor = make_native_install(
        tmp_path,
        data_root=data_root,
        compatibility_bundle_id=manifest.bundle_id,
    )

    with pytest.raises(CompatibilityReaderError, match="not authorized"):
        V1CompatibilityReader(data_root, "00" * 32)

    with V1CompatibilityReader(data_root, manifest.bundle_id) as reader:
        results = reader.search(np.asarray([1.0, 0.0], dtype=np.float32), k=1)
        assert results[0].legacy_ordinal == 0
        assert results[0].metadata["file_name"] == "a.pdf"

    published = PublicationCoordinator(data_root).publish(successor)
    assert published.write_epoch == 1
    assert published.v1_fallback_open is False
    bundle = data_root / "retrieval" / "compat" / "v1" / manifest.bundle_id
    assert bundle.is_dir()
    assert (bundle / "cleanup-pending.json").is_file()

    import src.migrations.v2.compatibility as compatibility

    monkeypatch.setattr(
        compatibility,
        "load_trusted_legacy_docstore",
        lambda _path: pytest.fail("closed fallback must not open index.pkl"),
    )
    with pytest.raises(CompatibilityReaderError, match="permanently closed"):
        V1CompatibilityReader(data_root, manifest.bundle_id)
