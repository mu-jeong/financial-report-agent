from __future__ import annotations

import hashlib
import pickle
import sqlite3
from pathlib import Path

import faiss
import numpy as np
import pytest
import requests
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_core.documents import Document

from src.migrations.v2.evidence import (
    create_copied_v1_install,
    seal_compatibility_bundle,
)
from src.migrations.v2.import_v1 import ConversionError, convert_v1_seed, plan_v1_seed
from src.retrieval.identity import EmbeddingProfile


PREFIX = "[Company: {target_name}, Title: {title}]\n"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _legacy_source(root: Path) -> None:
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
                (1, 'company', '2026-01-01', 'A', 'Result', 'Broker', 'a.pdf', 1),
                (2, 'industry', '2026-01-02', 'Sector', 'Outlook', 'Broker', 'b.pdf', 0);
            INSERT INTO parent_chunks VALUES ('p1', 'alpha--beta', 'a.pdf', NULL);
            """
        )
    metadata = {
        "parent_id": "p1",
        "file_name": "a.pdf",
        "report_type": "company",
        "report_date": "2026-01-01",
        "target_name": "A",
        "title": "Result",
        "broker": "Broker",
    }
    documents = {
        "d0": Document(
            page_content=PREFIX.format(target_name="A", title="Result") + "alpha",
            metadata={**metadata, "child_index": 0},
        ),
        "d1": Document(
            page_content=PREFIX.format(target_name="A", title="Result") + "beta",
            metadata={**metadata, "child_index": 1},
        ),
    }
    index = faiss.IndexFlatL2(3)
    index.add(
        np.asarray(
            [[1.0, 0.0, 0.5], [0.0, 1.0, 0.25]],
            dtype=np.float32,
        )
    )
    faiss.write_index(index, str(root / "vector_db" / "index.faiss"))
    with (root / "vector_db" / "index.pkl").open("wb") as stream:
        pickle.dump((InMemoryDocstore(documents), {0: "d0", 1: "d1"}), stream)


def _profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        model="model-a",
        dimension=3,
        metric="l2",
        normalization="none",
        prefix_template=PREFIX,
        extractor="legacy-v1-parent-content",
        parent_policy={"size": 2000, "overlap": 200},
        child_policy={"size": 500, "overlap": 50},
    )


def _copied_fixture(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _legacy_source(source)
    copied = tmp_path / "copied"
    evidence = create_copied_v1_install(source, copied)
    expected = {item.relative_path: item.sha256 for item in evidence.copied_artifacts}
    return source, copied, expected


def _source_hashes() -> dict[str, str]:
    return {"a.pdf": "11" * 32, "b.pdf": "22" * 32}


def test_plan_is_write_free_and_requires_complete_source_hash_evidence(tmp_path):
    _source, copied, expected = _copied_fixture(tmp_path)
    before = {
        path.relative_to(copied).as_posix(): (_hash(path), path.stat().st_mtime_ns)
        for path in copied.rglob("*")
        if path.is_file()
    }

    plan = plan_v1_seed(
        copied,
        expected_hashes=expected,
        profile=_profile(),
        source_hashes=_source_hashes(),
        compatibility_bundle_id="aa" * 32,
    )

    assert len(plan.reports) == 2
    assert len(plan.parents) == 1
    assert len(plan.chunks) == 2
    assert plan.manifest.included_count == 1
    assert plan.manifest.excluded_count == 1
    assert {
        path.relative_to(copied).as_posix(): (_hash(path), path.stat().st_mtime_ns)
        for path in copied.rglob("*")
        if path.is_file()
    } == before

    with pytest.raises(ConversionError, match="cover every discovered"):
        plan_v1_seed(
            copied,
            expected_hashes=expected,
            profile=_profile(),
            source_hashes={"a.pdf": "11" * 32},
            compatibility_bundle_id="aa" * 32,
        )


def test_full_n_conversion_publishes_native_epoch_zero_seed_without_prohibited_calls(
    tmp_path, monkeypatch
):
    _source, copied, expected = _copied_fixture(tmp_path)
    data_root = tmp_path / "data root 한글"
    data_root.mkdir()
    bundle = seal_compatibility_bundle(copied, data_root)
    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        lambda *_args, **_kwargs: pytest.fail("network called during conversion"),
    )
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        if path.suffix.lower() == ".pdf":
            pytest.fail("PDF opened during conversion")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    result = convert_v1_seed(
        copied,
        data_root,
        expected_hashes=expected,
        profile=_profile(),
        source_hashes=_source_hashes(),
        compatibility_bundle_id=bundle.bundle_id,
    )

    assert result.chunk_count == 2
    assert result.max_vector_absolute_error <= 1e-6
    assert (data_root / result.catalog_relative_path).is_file()
    assert (data_root / result.snapshot_relative_path).is_file()
    backups = data_root / "retrieval" / "v2" / "backups"
    assert (backups / "catalog-current.sqlite3").is_file()
    assert not [
        path
        for path in backups.iterdir()
        if path.name.endswith(("-wal", "-shm", "-journal"))
    ]
    evidence_dir = data_root / "retrieval" / "v2" / "evidence" / result.publication_id
    assert {path.name for path in evidence_dir.iterdir()} == {
        "legacy-mapping.json",
        "manifest.json",
        "commit-intent.json",
        "committed-floor.json",
    }
    assert not list((data_root / "retrieval" / "v2").rglob("index.pkl"))
    with sqlite3.connect(data_root / result.catalog_relative_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM active_reports").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM snapshot_membership").fetchone()[0] == 2
        runtime = connection.execute(
            "SELECT write_epoch, v1_fallback_open, write_enabled FROM retrieval_runtime"
        ).fetchone()
        assert runtime == (0, 1, 0)


def test_clean_conversions_are_logically_and_vector_deterministic(tmp_path):
    _source, copied, expected = _copied_fixture(tmp_path)
    results = []
    for suffix in ("one", "two"):
        data_root = tmp_path / suffix
        data_root.mkdir()
        bundle = seal_compatibility_bundle(copied, data_root)
        result = convert_v1_seed(
            copied,
            data_root,
            expected_hashes=expected,
            profile=_profile(),
            source_hashes=_source_hashes(),
            compatibility_bundle_id=bundle.bundle_id,
        )
        with sqlite3.connect(data_root / result.catalog_relative_path) as connection:
            logical = connection.execute(
                "SELECT chunk_uid, faiss_id FROM snapshot_membership ORDER BY faiss_id"
            ).fetchall()
        results.append((result, logical))

    first, second = results
    assert first[0].profile_hash == second[0].profile_hash
    assert first[0].build_id == second[0].build_id
    assert first[0].snapshot_id == second[0].snapshot_id
    assert first[0].snapshot_sha256 == second[0].snapshot_sha256
    assert first[1] == second[1]


def test_missing_source_evidence_leaves_native_candidate_unpublished(tmp_path):
    _source, copied, expected = _copied_fixture(tmp_path)
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    bundle = seal_compatibility_bundle(copied, data_root)

    with pytest.raises(ConversionError, match="cover every discovered"):
        convert_v1_seed(
            copied,
            data_root,
            expected_hashes=expected,
            profile=_profile(),
            source_hashes={"a.pdf": "11" * 32},
            compatibility_bundle_id=bundle.bundle_id,
        )

    assert not (data_root / "retrieval" / "v2" / "catalog.sqlite3").exists()
    assert not (data_root / "retrieval" / "v2" / "snapshots").exists()
