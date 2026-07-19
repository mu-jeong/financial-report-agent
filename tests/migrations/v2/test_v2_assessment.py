from __future__ import annotations

import hashlib
import os
import pickle
import sqlite3
from pathlib import Path

import faiss
import numpy as np
import pytest
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_core.documents import Document

from src.migrations.v2 import assess as assessment_module
from src.migrations.v2.assess import (
    AssessmentError,
    ProvenanceEvidence,
    assess_v1_install,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_fixture(root: Path, *, vectors: np.ndarray | None = None) -> dict[str, str]:
    (root / "vector_db").mkdir(parents=True)
    db_path = root / "reports.db"
    with sqlite3.connect(db_path) as connection:
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
            INSERT INTO parent_chunks VALUES
                ('parent-1', 'alpha beta gamma', 'a.pdf', NULL);
            """
        )

    matrix = np.asarray(
        vectors if vectors is not None else [[1.0, 0.0], [0.0, 1.0]],
        dtype=np.float32,
    )
    index = faiss.IndexFlatL2(2)
    index.add(matrix)
    faiss.write_index(index, str(root / "vector_db" / "index.faiss"))

    documents = {
        "doc-0": Document(
            page_content="[Company: A, Title: Result]\nalpha",
            metadata={
                "parent_id": "parent-1",
                "file_name": "a.pdf",
                "child_index": 0,
            },
        ),
        "doc-1": Document(
            page_content="[Company: A, Title: Result]\nbeta",
            metadata={
                "parent_id": "parent-1",
                "file_name": "a.pdf",
                "child_index": 1,
            },
        ),
    }
    with (root / "vector_db" / "index.pkl").open("wb") as stream:
        pickle.dump((InMemoryDocstore(documents), {0: "doc-0", 1: "doc-1"}), stream)

    return {
        "reports.db": _sha256(db_path),
        "vector_db/index.faiss": _sha256(root / "vector_db" / "index.faiss"),
        "vector_db/index.pkl": _sha256(root / "vector_db" / "index.pkl"),
    }


def _file_state(root: Path) -> dict[str, tuple[int, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            _sha256(path),
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_assessment_is_repeatable_read_only_and_separates_provenance(tmp_path):
    expected = _build_fixture(tmp_path)
    before = _file_state(tmp_path)
    provenance = ProvenanceEvidence(
        model="model-a",
        model_revision=None,
        normalization="none",
        library_version="1.0",
        same_space_attested=True,
    )

    first = assess_v1_install(tmp_path, expected_hashes=expected, provenance=provenance)
    second = assess_v1_install(tmp_path, expected_hashes=expected, provenance=provenance)

    assert first == second
    assert first.digest == second.digest
    assert first.observable.ntotal == first.observable.mapping_count == 2
    assert first.observable.finite_vector_count == 2
    assert first.observable.metric == "l2"
    assert first.provenance.model == "model-a"
    assert first.uncertainties == (
        "historical embedding model revision is unknown",
    )
    assert _file_state(tmp_path) == before


def test_hash_mismatch_blocks_before_pickle_deserialization(tmp_path, monkeypatch):
    expected = _build_fixture(tmp_path)
    expected["vector_db/index.pkl"] = "00" * 32
    monkeypatch.setattr(
        assessment_module,
        "load_trusted_legacy_docstore",
        lambda _path: pytest.fail("pickle should not be opened"),
    )

    with pytest.raises(AssessmentError, match="trusted hash mismatch"):
        assess_v1_install(tmp_path, expected_hashes=expected)


def test_restricted_unpickler_rejects_executable_globals(tmp_path):
    expected = _build_fixture(tmp_path)
    sentinel = tmp_path / "pickle-executed"

    class Malicious:
        def __reduce__(self):
            return os.system, (f'echo unsafe > "{sentinel}"',)

    pickle_path = tmp_path / "vector_db" / "index.pkl"
    with pickle_path.open("wb") as stream:
        pickle.dump(Malicious(), stream)
    expected["vector_db/index.pkl"] = _sha256(pickle_path)

    with pytest.raises(AssessmentError, match="forbidden global"):
        assess_v1_install(tmp_path, expected_hashes=expected)

    assert not sentinel.exists()


def test_count_or_reference_mismatch_blocks_assessment(tmp_path):
    expected = _build_fixture(tmp_path)
    with sqlite3.connect(tmp_path / "reports.db") as connection:
        connection.execute("DELETE FROM parent_chunks")
    expected["reports.db"] = _sha256(tmp_path / "reports.db")

    with pytest.raises(AssessmentError, match="missing parent"):
        assess_v1_install(tmp_path, expected_hashes=expected)


def test_nonfinite_legacy_vector_blocks_assessment(tmp_path):
    expected = _build_fixture(
        tmp_path,
        vectors=np.asarray([[1.0, 0.0], [np.nan, 1.0]], dtype=np.float32),
    )

    with pytest.raises(AssessmentError, match="non-finite"):
        assess_v1_install(tmp_path, expected_hashes=expected)


def test_symlinked_artifact_is_rejected_when_supported(tmp_path):
    expected = _build_fixture(tmp_path)
    target = tmp_path / "real.pkl"
    original = tmp_path / "vector_db" / "index.pkl"
    original.replace(target)
    try:
        original.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable for this Windows account")
    expected["vector_db/index.pkl"] = _sha256(target)

    with pytest.raises(AssessmentError, match="symlink"):
        assess_v1_install(tmp_path, expected_hashes=expected)
