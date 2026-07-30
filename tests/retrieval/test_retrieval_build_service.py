from __future__ import annotations

import hashlib
import json
import pickle
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import faiss
import numpy as np
import pytest
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_core.documents import Document

from src.core import pdf_extraction
from src.core.pdf_extraction import ExtractionResult
from src.migrations.v2.evidence import seal_compatibility_bundle
from src.migrations.v2.import_v1 import convert_v1_seed
from src.retrieval import build_service
from src.retrieval.bootstrap import inspect_runtime
from src.retrieval.build_service import (
    NativeBuildError,
    execute_full_corpus_successor,
    execute_incremental_update,
    materialize_candidate,
    prepare_full_corpus_build,
    prepare_incremental_build,
    publish_candidate,
)
from src.retrieval.identity import EmbeddingProfile
from src.retrieval.publication import PublicationError, activate_epoch_zero_seed
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from src.retrieval.writer_lock import NativeWriterLock, WriterLockError


PREFIX = "[Company: {target_name}, Title: {title}]\n"
VECTORS = np.asarray([[1.0, 0.0, 0.5], [0.0, 1.0, 0.25]], dtype=np.float32)


class DeterministicEmbeddings:
    def __init__(self, *, break_canary: bool = False):
        self.break_canary = break_canary
        self.calls: list[list[str]] = []
        self.legacy = {
            PREFIX.format(target_name="A", title="Result") + "alpha": VECTORS[0],
            PREFIX.format(target_name="A", title="Result") + "beta": VECTORS[1],
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        values = []
        for text in texts:
            if text in self.legacy:
                vector = self.legacy[text].copy()
                if self.break_canary:
                    vector = -vector
            else:
                digest = hashlib.sha256(text.encode("utf-8")).digest()
                vector = np.asarray(
                    [1.0 + digest[0] / 255, 1.0 + digest[1] / 255, 1.0 + digest[2] / 255],
                    dtype=np.float32,
                )
            values.append(vector.tolist())
        return values


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


def _migration_profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        model="model-a",
        dimension=3,
        metric="l2",
        normalization="none",
        prefix_template=PREFIX,
        extractor="legacy-v1-import|configured=deterministic-extractor|unattested",
        parent_policy={
            "algorithm": "langchain-recursive-v1",
            "chunk_size": 2000,
            "chunk_overlap": 200,
        },
        child_policy={
            "algorithm": "langchain-recursive-v1",
            "chunk_size": 500,
            "chunk_overlap": 50,
        },
    )


def _legacy_install(root: Path) -> dict[str, str]:
    vector_root = root / "vector_db"
    vector_root.mkdir(parents=True)
    connection = sqlite3.connect(root / "reports.db")
    try:
        connection.executescript(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY, report_type TEXT NOT NULL,
                report_date TEXT NOT NULL, target_name TEXT, title TEXT NOT NULL,
                broker TEXT NOT NULL, file_name TEXT NOT NULL UNIQUE,
                is_embedded INTEGER NOT NULL
            );
            CREATE TABLE parent_chunks (
                id TEXT PRIMARY KEY, content TEXT NOT NULL,
                file_name TEXT NOT NULL, metadata TEXT
            );
            INSERT INTO reports VALUES
                (1, 'company', '2026-01-01', 'A', 'Result', 'Broker', 'a.pdf', 1),
                (2, 'industry', '2026-01-02', 'Sector', 'Outlook', 'Broker', 'b.pdf', 0);
            INSERT INTO parent_chunks VALUES ('p1', 'alpha--beta', 'a.pdf', NULL);
            """
        )
        connection.commit()
    finally:
        connection.close()
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
    index.add(VECTORS)
    faiss.write_index(index, str(vector_root / "index.faiss"))
    with (vector_root / "index.pkl").open("wb") as stream:
        pickle.dump((InMemoryDocstore(documents), {0: "d0", 1: "d1"}), stream)
    return {
        relative: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for relative in ("reports.db", "vector_db/index.faiss", "vector_db/index.pkl")
    }


def _native_seed(
    tmp_path: Path,
    *,
    seed_matches_current_source: bool = False,
    profile: EmbeddingProfile | None = None,
) -> tuple[Path, Path]:
    copied = tmp_path / "copied-v1"
    copied.mkdir()
    expected = _legacy_install(copied)
    data_root = tmp_path / "데이터 root"
    bundle = seal_compatibility_bundle(copied, data_root)
    current_a = b"current-a"
    convert_v1_seed(
        copied,
        data_root,
        expected_hashes=expected,
        profile=profile or _profile(),
        source_hashes={
            "a.pdf": (
                hashlib.sha256(current_a).hexdigest()
                if seed_matches_current_source
                else "11" * 32
            ),
            "b.pdf": "22" * 32,
        },
        compatibility_bundle_id=bundle.bundle_id,
    )
    sources = data_root / "downloaded"
    sources.mkdir()
    (sources / "a.pdf").write_bytes(current_a)
    (sources / "b.pdf").write_bytes(b"new-b")
    return data_root, sources


def _activate_seed_for_incremental(data_root: Path) -> str:
    selection = inspect_runtime(
        data_root / "reports.db",
        data_root=data_root,
        validate_snapshot=True,
    )
    snapshot_id = selection.active_snapshot_id or ""
    activate_epoch_zero_seed(
        data_root,
        snapshot_id=snapshot_id,
        canary={
            "sample_count": 2,
            "dimension": 3,
            "minimum_cosine_similarity": 1.0,
            "maximum_norm_relative_error": 0.0,
            "self_rank_one_count": 2,
        },
    )
    return snapshot_id


def _metadata(file_name: str):
    values = {
        "a.pdf": {
            "report_type": "company",
            "report_date": "2026-01-01",
            "target_name": "A",
            "title": "Result",
            "broker": "Broker",
        },
        "b.pdf": {
            "report_type": "industry",
            "report_date": "2026-01-02",
            "target_name": "Sector",
            "title": "Outlook",
            "broker": "Broker",
        },
    }
    return values.get(file_name)


def _extract(path: Path, engine: str) -> str:
    assert engine == "deterministic-extractor"
    return {
        "a.pdf": "alpha beta current report content",
        "b.pdf": "sector outlook newly searchable content",
    }[path.name]


def _write_poison_snapshot(path: Path, plan) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    vectors = np.full(
        (len(plan.chunks), plan.profile.dimension),
        123.0,
        dtype=np.float32,
    )
    base = faiss.IndexFlatL2(plan.profile.dimension)
    index = faiss.IndexIDMap2(base)
    index.add_with_ids(
        vectors,
        np.arange(1, len(plan.chunks) + 1, dtype=np.int64),
    )
    with path.open("wb") as stream:
        faiss.write_index(index, faiss.PyCallbackIOWriter(stream.write))
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _prepare(
    data_root: Path,
    sources: Path,
    embeddings: DeterministicEmbeddings,
    **overrides,
):
    writer_lease = overrides.pop("writer_lease", None)
    options = {
        "model": "model-a",
        "extractor_name": "deterministic-extractor",
        "parent_chunk_size": 40,
        "child_chunk_size": 20,
        "extractor": _extract,
        "metadata_parser": _metadata,
        **overrides,
    }
    if writer_lease is None:
        with NativeWriterLock(data_root) as owned_lease:
            return prepare_full_corpus_build(
                data_root / "reports.db",
                sources,
                data_root=data_root,
                embeddings=embeddings,
                writer_lease=owned_lease,
                **options,
            )
    return prepare_full_corpus_build(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=embeddings,
        writer_lease=writer_lease,
        **options,
    )


def test_full_corpus_successor_closes_fallback_and_adds_new_member(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    embeddings = DeterministicEmbeddings()

    plan = _prepare(data_root, sources, embeddings)
    result = materialize_candidate(plan, data_root)

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        before = connection.execute(
            "SELECT active_snapshot_id, write_epoch, v1_fallback_open FROM retrieval_runtime"
        ).fetchone()
        candidate_state = connection.execute(
            "SELECT state FROM retrieval_builds WHERE build_id = ?", (plan.build_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert before == (plan.base_snapshot_id, 0, 1)
    assert candidate_state == "ready"
    assert plan.same_space_canary is not None
    assert plan.same_space_canary.sample_count == 2
    assert plan.manifest.included_count == 2
    assert plan.manifest.excluded_count == 1
    assert {
        entry.reason_code for entry in plan.manifest.entries if entry.status == "excluded"
    } == {"source-superseded"}
    assert result.chunk_count == result.descriptor.ntotal == len(plan.chunks)

    outcome = publish_candidate(result, data_root)

    connection = sqlite3.connect(catalog)
    try:
        runtime = connection.execute(
            """
            SELECT active_snapshot_id, predecessor_snapshot_id, write_epoch,
                   v1_fallback_open, write_enabled, degraded
            FROM retrieval_runtime
            """
        ).fetchone()
        active_files = {
            row[0] for row in connection.execute("SELECT canonical_relative_path FROM active_reports")
        }
    finally:
        connection.close()
    assert runtime == (plan.snapshot_id, plan.base_snapshot_id, 1, 0, 1, 0)
    assert outcome.active_snapshot_id == plan.snapshot_id
    assert active_files == {"downloaded/a.pdf", "downloaded/b.pdf"}
    assert (data_root / result.snapshot_relative_path).is_file()
    assert (data_root / result.evidence_manifest_relative_path).is_file()


def test_full_corpus_successor_records_extraction_failure_and_embeds_remaining_reports(
    tmp_path: Path,
) -> None:
    data_root, sources = _native_seed(tmp_path)
    embeddings = DeterministicEmbeddings()

    def extract_with_one_failure(path: Path, engine: str) -> str:
        if path.name == "a.pdf":
            raise RuntimeError("primary and fallback failed")
        return _extract(path, engine)

    plan = _prepare(
        data_root,
        sources,
        embeddings,
        extractor=extract_with_one_failure,
    )

    decisions = {entry.report_uid: entry for entry in plan.manifest.entries}
    current_by_name = {report.file_name: report for report in plan.reports}
    assert decisions[current_by_name["a.pdf"].report_uid].status == "excluded"
    assert decisions[current_by_name["b.pdf"].report_uid].status == "included"
    assert (
        decisions[current_by_name["a.pdf"].report_uid].reason_code
        == "source-extraction-failed"
    )
    assert plan.manifest.included_count == 1
    assert plan.manifest.excluded_count == 2
    assert {parent.report_uid for parent in plan.parents} == {
        current_by_name["b.pdf"].report_uid
    }

    result = materialize_candidate(plan, data_root)
    outcome = publish_candidate(result, data_root)

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        active_files = {
            row[0]
            for row in connection.execute(
                "SELECT canonical_relative_path FROM active_reports"
            )
        }
        failed_row = connection.execute(
            "SELECT COUNT(*) FROM reports WHERE report_uid = ?",
            (current_by_name["a.pdf"].report_uid,),
        ).fetchone()[0]
    assert outcome.active_snapshot_id == plan.snapshot_id
    assert active_files == {"downloaded/b.pdf"}
    assert failed_row == 1


def test_incremental_update_reuses_unchanged_vectors_and_processes_only_pending_pdf(
    tmp_path: Path,
) -> None:
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=_migration_profile(),
    )
    activate_epoch_zero_seed(
        data_root,
        snapshot_id=inspect_runtime(
            data_root / "reports.db",
            data_root=data_root,
            validate_snapshot=True,
        ).active_snapshot_id
        or "",
        canary={
            "sample_count": 2,
            "dimension": 3,
            "minimum_cosine_similarity": 1.0,
            "maximum_norm_relative_error": 0.0,
            "self_rank_one_count": 2,
        },
    )
    embeddings = DeterministicEmbeddings()
    extracted: list[str] = []

    def tracking_extract(path: Path, engine: str) -> str:
        extracted.append(path.name)
        return _extract(path, engine)

    result = execute_incremental_update(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=embeddings,
        model="model-a",
        extractor_name="deterministic-extractor",
        extractor=tracking_extract,
        metadata_parser=_metadata,
        allow_extraction_fallback=False,
        use_parent_child=True,
        parent_chunk_size=2000,
        child_chunk_size=500,
        metric="l2",
        normalization="none",
    )

    assert result is not None
    candidate, publication = result
    assert extracted == ["b.pdf"]
    assert publication.active_snapshot_id == candidate.snapshot_id
    assert publication.predecessor_snapshot_id is not None
    assert candidate.report_count == 2
    assert candidate.chunk_count > 2
    assert len(embeddings.calls) == 2
    assert all("newly searchable" not in text for text in embeddings.calls[0])
    assert any("newly searchable" in text for text in embeddings.calls[1])


def test_incremental_update_batches_publish_the_final_partial_batch(
    tmp_path: Path,
) -> None:
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=_migration_profile(),
    )
    _activate_seed_for_incremental(data_root)
    (sources / "c.pdf").write_bytes(b"new-c")
    (sources / "d.pdf").write_bytes(b"new-d")

    def metadata(file_name: str):
        existing = _metadata(file_name)
        if existing is not None:
            return existing
        stem = Path(file_name).stem.upper()
        return {
            "report_type": "company",
            "report_date": "2026-01-03",
            "target_name": stem,
            "title": f"{stem} update",
            "broker": "Broker",
        }

    def extract(path: Path, engine: str) -> str:
        if path.name in {"a.pdf", "b.pdf"}:
            return _extract(path, engine)
        return f"{path.stem} newly searchable report content"

    options = {
        "data_root": data_root,
        "embeddings": DeterministicEmbeddings(),
        "model": "model-a",
        "extractor_name": "deterministic-extractor",
        "extractor": extract,
        "metadata_parser": metadata,
        "allow_extraction_fallback": False,
        "use_parent_child": True,
        "parent_chunk_size": 2000,
        "child_chunk_size": 500,
        "metric": "l2",
        "normalization": "none",
        "max_changed_reports": 2,
    }
    first_result, first_publication = execute_incremental_update(
        data_root / "reports.db",
        sources,
        **options,
    )

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        first_active_paths = {
            row[0]
            for row in connection.execute(
                "SELECT canonical_relative_path FROM active_reports"
            )
        }
        first_manifest = json.loads(
            connection.execute(
                "SELECT source_manifest_json FROM retrieval_builds WHERE build_id = ?",
                (first_result.build_id,),
            ).fetchone()[0]
        )

    assert len(first_result.attempted_report_uids) == 2
    assert first_result.deferred_report_count == 1
    assert first_active_paths == {
        "downloaded/a.pdf",
        "downloaded/b.pdf",
        "downloaded/c.pdf",
    }
    assert first_manifest["exclusion_policy"]["version"] == "native-batched-corpus-v1"
    assert {
        entry["reason_code"]
        for entry in first_manifest["reports"]
        if entry["status"] == "excluded"
    } == {"source-batch-deferred"}

    second_result, second_publication = execute_incremental_update(
        data_root / "reports.db",
        sources,
        **options,
    )
    with sqlite3.connect(catalog) as connection:
        final_active_paths = {
            row[0]
            for row in connection.execute(
                "SELECT canonical_relative_path FROM active_reports"
            )
        }

    assert len(second_result.attempted_report_uids) == 1
    assert second_result.deferred_report_count == 0
    assert final_active_paths == {
        "downloaded/a.pdf",
        "downloaded/b.pdf",
        "downloaded/c.pdf",
        "downloaded/d.pdf",
    }
    assert (
        second_publication.publication_generation
        == first_publication.publication_generation + 1
    )


def test_deferred_changed_report_keeps_its_previous_version_searchable(
    tmp_path: Path,
) -> None:
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=_migration_profile(),
    )
    _activate_seed_for_incremental(data_root)
    common_options = {
        "data_root": data_root,
        "model": "model-a",
        "extractor_name": "deterministic-extractor",
        "allow_extraction_fallback": False,
        "use_parent_child": True,
        "parent_chunk_size": 2000,
        "child_chunk_size": 500,
        "metric": "l2",
        "normalization": "none",
    }
    execute_incremental_update(
        data_root / "reports.db",
        sources,
        embeddings=DeterministicEmbeddings(),
        extractor=_extract,
        metadata_parser=_metadata,
        **common_options,
    )
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        previous_b_uid = connection.execute(
            """
            SELECT report_uid FROM active_reports
            WHERE canonical_relative_path = 'downloaded/b.pdf'
            """
        ).fetchone()[0]

    (sources / "aa.pdf").write_bytes(b"new-aa")
    (sources / "b.pdf").write_bytes(b"changed-b")

    def metadata(file_name: str):
        if file_name == "aa.pdf":
            return {
                "report_type": "company",
                "report_date": "2026-01-03",
                "target_name": "AA",
                "title": "AA update",
                "broker": "Broker",
            }
        return _metadata(file_name)

    def extract(path: Path, engine: str) -> str:
        if path.name == "aa.pdf":
            return "aa newly searchable report content"
        if path.name == "b.pdf":
            return "changed b report content"
        return _extract(path, engine)

    first_result, _first_publication = execute_incremental_update(
        data_root / "reports.db",
        sources,
        embeddings=DeterministicEmbeddings(),
        extractor=extract,
        metadata_parser=metadata,
        max_changed_reports=1,
        **common_options,
    )
    with sqlite3.connect(catalog) as connection:
        active_after_first = dict(
            connection.execute(
                "SELECT canonical_relative_path, report_uid FROM active_reports"
            )
        )
        manifest = json.loads(
            connection.execute(
                "SELECT source_manifest_json FROM retrieval_builds WHERE build_id = ?",
                (first_result.build_id,),
            ).fetchone()[0]
        )

    assert first_result.deferred_report_count == 1
    assert active_after_first["downloaded/b.pdf"] == previous_b_uid
    assert "downloaded/aa.pdf" in active_after_first
    assert any(
        entry["report_uid"] == previous_b_uid
        and entry["status"] == "included"
        for entry in manifest["reports"]
    )

    second_result, _second_publication = execute_incremental_update(
        data_root / "reports.db",
        sources,
        embeddings=DeterministicEmbeddings(),
        extractor=extract,
        metadata_parser=metadata,
        max_changed_reports=1,
        **common_options,
    )
    with sqlite3.connect(catalog) as connection:
        current_b_uid = connection.execute(
            """
            SELECT report_uid FROM active_reports
            WHERE canonical_relative_path = 'downloaded/b.pdf'
            """
        ).fetchone()[0]

    assert second_result.deferred_report_count == 0
    assert current_b_uid != previous_b_uid


def test_incremental_update_is_a_noop_without_source_changes(tmp_path: Path) -> None:
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=_migration_profile(),
    )
    selection = inspect_runtime(
        data_root / "reports.db",
        data_root=data_root,
        validate_snapshot=True,
    )
    activate_epoch_zero_seed(
        data_root,
        snapshot_id=selection.active_snapshot_id or "",
        canary={
            "sample_count": 2,
            "dimension": 3,
            "minimum_cosine_similarity": 1.0,
            "maximum_norm_relative_error": 0.0,
            "self_rank_one_count": 2,
        },
    )
    (sources / "b.pdf").unlink()
    embeddings = DeterministicEmbeddings()

    assert prepare_incremental_build(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=embeddings,
        model="model-a",
        extractor_name="deterministic-extractor",
        extractor=_extract,
        metadata_parser=_metadata,
        allow_extraction_fallback=False,
        use_parent_child=True,
        parent_chunk_size=2000,
        child_chunk_size=500,
        metric="l2",
        normalization="none",
    ) is None
    assert embeddings.calls == []


def test_incremental_existing_profile_accepts_only_its_exact_fallback_policy(
    tmp_path: Path,
) -> None:
    profile = replace(_migration_profile(), extractor="pymupdf")
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=profile,
    )
    active_snapshot_id = _activate_seed_for_incremental(data_root)
    extracted: list[str] = []

    def tracking_extract(path: Path, engine: str):
        extracted.append(engine)
        return SimpleNamespace(
            text=_extract(path, "deterministic-extractor"),
            used_engine=engine,
        )

    plan = prepare_incremental_build(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=DeterministicEmbeddings(),
        model="model-a",
        extractor_name="pymupdf",
        fallback_extractor_name="",
        extractor=tracking_extract,
        metadata_parser=_metadata,
        parent_chunk_size=2000,
        child_chunk_size=500,
    )

    assert plan is not None
    assert plan.profile.extractor == "pymupdf"
    assert extracted == ["pymupdf"]
    assert inspect_runtime(
        data_root / "reports.db",
        data_root=data_root,
        validate_snapshot=True,
    ).active_snapshot_id == active_snapshot_id


def test_incremental_existing_profile_rejects_new_fallback_before_extraction(
    tmp_path: Path,
) -> None:
    profile = replace(_migration_profile(), extractor="pymupdf")
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=profile,
    )
    active_snapshot_id = _activate_seed_for_incremental(data_root)
    extracted: list[str] = []

    with pytest.raises(NativeBuildError, match="incremental extractor differs"):
        prepare_incremental_build(
            data_root / "reports.db",
            sources,
            data_root=data_root,
            embeddings=DeterministicEmbeddings(),
            model="model-a",
            extractor_name="pymupdf",
            fallback_extractor_name="opendataloader",
            extractor=lambda path, engine: extracted.append(engine) or _extract(
                path,
                "deterministic-extractor",
            ),
            metadata_parser=_metadata,
            parent_chunk_size=2000,
            child_chunk_size=500,
        )

    assert extracted == []
    assert inspect_runtime(
        data_root / "reports.db",
        data_root=data_root,
        validate_snapshot=True,
    ).active_snapshot_id == active_snapshot_id


def test_incremental_legacy_profile_rejects_unrecorded_fallback_before_extraction(
    tmp_path: Path,
) -> None:
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=_migration_profile(),
    )
    _activate_seed_for_incremental(data_root)
    extracted: list[str] = []

    with pytest.raises(NativeBuildError, match="incremental extractor differs"):
        prepare_incremental_build(
            data_root / "reports.db",
            sources,
            data_root=data_root,
            embeddings=DeterministicEmbeddings(),
            model="model-a",
            extractor_name="deterministic-extractor",
            fallback_extractor_name="opendataloader",
            extractor=lambda path, engine: extracted.append(engine) or _extract(path, engine),
            metadata_parser=_metadata,
            parent_chunk_size=2000,
            child_chunk_size=500,
        )

    assert extracted == []


def test_incremental_legacy_profile_accepts_its_recorded_fallback(
    tmp_path: Path,
) -> None:
    profile = replace(
        _migration_profile(),
        extractor=(
            "legacy-v1-import|configured="
            "pymupdf|fallback=opendataloader|unattested"
        ),
    )
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=profile,
    )
    _activate_seed_for_incremental(data_root)

    plan = prepare_incremental_build(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=DeterministicEmbeddings(),
        model="model-a",
        extractor_name="pymupdf",
        fallback_extractor_name="opendataloader",
        extractor=lambda path, _engine: SimpleNamespace(
            text=_extract(path, "deterministic-extractor"),
            used_engine="opendataloader-fallback",
        ),
        metadata_parser=_metadata,
        parent_chunk_size=2000,
        child_chunk_size=500,
    )

    assert plan is not None
    assert plan.profile == profile


def test_incremental_source_extraction_failure_is_recorded_and_later_retried_explicitly(
    tmp_path: Path,
) -> None:
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=_migration_profile(),
    )
    _activate_seed_for_incremental(data_root)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"

    extraction_calls: list[str] = []
    def fail_extract(_path: Path, _engine: str):
        extraction_calls.append(_path.name)
        raise RuntimeError("both parsers failed")

    failed_result, failed_outcome = execute_incremental_update(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=DeterministicEmbeddings(),
        model="model-a",
        extractor_name="deterministic-extractor",
        fallback_extractor_name="",
        allow_extraction_fallback=False,
        extractor=fail_extract,
        metadata_parser=_metadata,
        parent_chunk_size=2000,
        child_chunk_size=500,
    )

    assert extraction_calls == ["b.pdf"]
    with sqlite3.connect(catalog) as connection:
        manifest = json.loads(
            connection.execute(
                "SELECT source_manifest_json FROM retrieval_builds WHERE build_id = ?",
                (failed_result.build_id,),
            ).fetchone()[0]
        )
        active_files = {
            row[0]
            for row in connection.execute(
                "SELECT canonical_relative_path FROM active_reports"
            )
        }
    failed_entries = [
        row
        for row in manifest["reports"]
        if row["reason_code"] == "source-extraction-failed"
    ]
    assert failed_outcome.active_snapshot_id == failed_result.snapshot_id
    assert len(failed_entries) == 1
    assert active_files == {"downloaded/a.pdf"}

    with sqlite3.connect(catalog) as connection:
        before_retry = connection.execute(
            """
            SELECT active_snapshot_id, publication_generation, write_epoch
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
    extraction_calls.clear()
    repeated_result, repeated_outcome = execute_incremental_update(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=DeterministicEmbeddings(),
        model="model-a",
        extractor_name="deterministic-extractor",
        fallback_extractor_name="",
        allow_extraction_fallback=False,
        extractor=fail_extract,
        metadata_parser=_metadata,
        parent_chunk_size=2000,
        child_chunk_size=500,
        retry_extraction_failures=True,
    )
    with sqlite3.connect(catalog) as connection:
        after_retry = connection.execute(
            """
            SELECT active_snapshot_id, publication_generation, write_epoch
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
    assert extraction_calls == ["b.pdf"]
    assert repeated_outcome.active_snapshot_id == repeated_result.snapshot_id
    assert after_retry[0] == repeated_result.snapshot_id
    assert after_retry[0] != before_retry[0]
    assert after_retry[1] == before_retry[1] + 1
    assert after_retry[2] == before_retry[2] + 1

    extraction_calls.clear()

    def successful_extract(path: Path, engine: str) -> str:
        extraction_calls.append(path.name)
        return _extract(path, engine)

    skipped = execute_incremental_update(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=DeterministicEmbeddings(),
        model="model-a",
        extractor_name="deterministic-extractor",
        fallback_extractor_name="",
        allow_extraction_fallback=False,
        extractor=successful_extract,
        metadata_parser=_metadata,
        parent_chunk_size=2000,
        child_chunk_size=500,
    )
    assert skipped is None
    assert extraction_calls == []

    retried_result, retried_outcome = execute_incremental_update(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=DeterministicEmbeddings(),
        model="model-a",
        extractor_name="deterministic-extractor",
        fallback_extractor_name="",
        allow_extraction_fallback=False,
        extractor=successful_extract,
        metadata_parser=_metadata,
        parent_chunk_size=2000,
        child_chunk_size=500,
        retry_extraction_failures=True,
    )
    assert extraction_calls == ["b.pdf"]
    with sqlite3.connect(catalog) as connection:
        active_files = {
            row[0]
            for row in connection.execute(
                "SELECT canonical_relative_path FROM active_reports"
            )
        }
    assert retried_outcome.active_snapshot_id == retried_result.snapshot_id
    assert active_files == {"downloaded/a.pdf", "downloaded/b.pdf"}


@pytest.mark.parametrize(
    "profile",
    [
        "",
        "primary|fallback=",
        "primary|fallback=primary",
        "primary|fallback=secondary|extra",
    ],
)
def test_extraction_profile_parser_rejects_ambiguous_policies(profile: str) -> None:
    with pytest.raises(NativeBuildError):
        build_service.parse_extraction_profile(profile, allow_custom=True)


def test_extraction_profile_parser_round_trips_custom_policy() -> None:
    profile = build_service.format_extraction_profile(
        "custom-primary",
        allow_fallback=True,
        fallback_engine="custom-fallback",
        allow_custom=True,
    )

    assert profile == "custom-primary|fallback=custom-fallback"
    assert build_service.parse_extraction_profile(
        profile,
        allow_custom=True,
    ) == ("custom-primary", "custom-fallback")
    with pytest.raises(NativeBuildError, match="invalid extraction engine"):
        build_service.parse_extraction_profile(profile, allow_custom=False)


def test_build_mutation_boundaries_reject_duck_typed_writer_leases(
    tmp_path: Path,
):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())

    class FakeLease:
        def assert_owned(self, _data_root):
            return None

    with pytest.raises(WriterLockError, match="invalid"):
        materialize_candidate(plan, data_root, writer_lease=FakeLease())

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM retrieval_builds WHERE build_id = ?",
            (plan.build_id,),
        ).fetchone() == (0,)

    result = materialize_candidate(plan, data_root)
    with sqlite3.connect(catalog) as connection:
        runtime_before = connection.execute(
            "SELECT active_snapshot_id, publication_generation, write_epoch "
            "FROM retrieval_runtime WHERE runtime_id = 1"
        ).fetchone()

    with pytest.raises(WriterLockError, match="invalid"):
        publish_candidate(result, data_root, writer_lease=FakeLease())

    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            "SELECT active_snapshot_id, publication_generation, write_epoch "
            "FROM retrieval_runtime WHERE runtime_id = 1"
        ).fetchone() == runtime_before
        assert connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE publication_id = ?",
            (result.publication_id,),
        ).fetchone() == (0,)


def test_materialize_rejects_conflicting_lineage_addressed_orphan(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())
    snapshot = (
        data_root
        / "retrieval"
        / "v2"
        / "snapshots"
        / f"{plan.snapshot_id}.faiss"
    )
    _write_poison_snapshot(snapshot, plan)

    with pytest.raises(NativeBuildError, match="vector payload differs"):
        materialize_candidate(plan, data_root)

    connection = sqlite3.connect(data_root / "retrieval" / "v2" / "catalog.sqlite3")
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM retrieval_builds WHERE build_id = ?",
            (plan.build_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 0


def test_completed_candidate_revalidates_reconstructed_vector_payload(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())
    result = materialize_candidate(plan, data_root)
    snapshot = data_root.joinpath(*result.snapshot_relative_path.split("/"))
    poison_sha256, poison_size = _write_poison_snapshot(snapshot, plan)
    connection = sqlite3.connect(data_root / "retrieval" / "v2" / "catalog.sqlite3")
    try:
        # Recreate the durable state an older materializer could have admitted:
        # ready catalog rows whose descriptor authenticates the wrong vectors.
        connection.execute("DROP TRIGGER vector_snapshots_immutable_fields")
        connection.execute(
            """
            UPDATE vector_snapshots
            SET file_sha256 = ?, size_bytes = ?
            WHERE snapshot_id = ?
            """,
            (poison_sha256, poison_size, plan.snapshot_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(NativeBuildError, match="vector payload differs"):
        materialize_candidate(plan, data_root)


def test_stale_sibling_candidate_cannot_roll_back_newer_source(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    stale_plan = _prepare(data_root, sources, DeterministicEmbeddings())
    stale_result = materialize_candidate(stale_plan, data_root)

    current_plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        model="model-b",
    )
    current_result = materialize_candidate(current_plan, data_root)
    current_outcome = publish_candidate(current_result, data_root)

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        runtime_before = connection.execute(
            "SELECT * FROM retrieval_runtime WHERE runtime_id = 1"
        ).fetchone()
        active_b_before = connection.execute(
            """
            SELECT report_uid, source_sha256
            FROM active_reports
            WHERE canonical_relative_path = 'downloaded/b.pdf'
            """
        ).fetchone()
    finally:
        connection.close()

    with pytest.raises(PublicationError, match="does not match live runtime"):
        publish_candidate(stale_result, data_root)

    connection = sqlite3.connect(catalog)
    try:
        runtime_after = connection.execute(
            "SELECT * FROM retrieval_runtime WHERE runtime_id = 1"
        ).fetchone()
        active_b_after = connection.execute(
            """
            SELECT report_uid, source_sha256
            FROM active_reports
            WHERE canonical_relative_path = 'downloaded/b.pdf'
            """
        ).fetchone()
        stale_journal_count = connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE publication_id = ?",
            (stale_plan.publication_id,),
        ).fetchone()[0]
    finally:
        connection.close()

    assert current_outcome.active_snapshot_id == current_plan.snapshot_id
    assert runtime_after == runtime_before
    assert active_b_after == active_b_before
    assert active_b_after[1] == hashlib.sha256(b"new-b").hexdigest()
    assert stale_journal_count == 0


def test_same_space_canary_blocks_before_full_corpus_extraction(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    extracted = []

    with pytest.raises(NativeBuildError, match="cosine threshold"):
        _prepare(
            data_root,
            sources,
            DeterministicEmbeddings(break_canary=True),
            model="wrong-space",
            extractor=lambda path, engine: extracted.append(path) or _extract(path, engine),
        )

    assert extracted == []


def test_same_space_rank_one_accepts_text_equivalence_or_numeric_ties() -> None:
    equivalent_stored = np.asarray(
        [[1.0, 0.0], [1.002, 0.0]],
        dtype=np.float32,
    )
    equivalent_regenerated = np.asarray(
        [[1.0, 0.0], [1.0, 0.0]],
        dtype=np.float32,
    )

    assert build_service._count_text_aware_self_rank_one(
        equivalent_stored,
        equivalent_regenerated,
        ["duplicate", "duplicate"],
        metric="l2",
    ) == 2
    assert build_service._count_text_aware_self_rank_one(
        equivalent_stored,
        equivalent_regenerated,
        ["first", "second"],
        metric="l2",
    ) == 1

    numeric_tie_stored = np.asarray(
        [[1.0, 0.0], [1.00084, 0.0]],
        dtype=np.float32,
    )
    assert build_service._count_text_aware_self_rank_one(
        numeric_tie_stored,
        equivalent_regenerated,
        ["first", "second"],
        metric="l2",
    ) == 2

    distinct_stored = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    swapped_regenerated = np.asarray([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    assert build_service._count_text_aware_self_rank_one(
        distinct_stored,
        swapped_regenerated,
        ["first", "second"],
        metric="l2",
    ) == 0


def test_structured_extractor_result_must_report_the_engine_used(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)

    with pytest.raises(NativeBuildError, match="must report the engine used"):
        _prepare(
            data_root,
            sources,
            DeterministicEmbeddings(),
            extractor=lambda path, engine: SimpleNamespace(
                text=_extract(path, engine)
            ),
        )


def test_structured_extractor_result_accepts_the_attested_engine(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)

    plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        extractor=lambda path, engine: SimpleNamespace(
            text=_extract(path, engine),
            used_engine=engine,
        ),
    )

    assert len(plan.reports) == 2


def test_structured_extractor_result_accepts_v1_fallback_engine(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)

    plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        extractor=lambda path, engine: SimpleNamespace(
            text=_extract(path, engine),
            used_engine="pymupdf-fallback",
        ),
    )

    assert len(plan.reports) == 2
    assert plan.profile.extractor == "deterministic-extractor|fallback=pymupdf"


def test_structured_extractor_result_accepts_configured_fallback_engine(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)

    plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        extractor_name="pymupdf",
        fallback_extractor_name="opendataloader",
        extractor=lambda path, engine: SimpleNamespace(
            text=_extract(path, "deterministic-extractor"),
            used_engine="opendataloader-fallback",
        ),
    )

    assert len(plan.reports) == 2
    assert plan.profile.extractor == "pymupdf|fallback=opendataloader"


def test_structured_extractor_result_rejects_undeclared_engine(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)

    with pytest.raises(NativeBuildError, match="undeclared engine"):
        _prepare(
            data_root,
            sources,
            DeterministicEmbeddings(),
            extractor=lambda path, engine: SimpleNamespace(
                text=_extract(path, engine),
                used_engine="unexpected-engine",
            ),
        )


def test_default_extractor_reuses_v1_fallback_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    captured = {}

    def fake_extract_pdf_text(
        path,
        engine,
        *,
        clean,
        allow_fallback,
        fallback_engine,
    ):
        captured.update(
            path=path,
            engine=engine,
            clean=clean,
            allow_fallback=allow_fallback,
            fallback_engine=fallback_engine,
        )
        return ExtractionResult(
            requested_engine=engine,
            used_engine="pymupdf-fallback",
            text="fallback candidate text",
        )

    monkeypatch.setattr(pdf_extraction, "extract_pdf_text", fake_extract_pdf_text)

    result = build_service._default_extractor(source, "opendataloader")

    assert captured == {
        "path": str(source),
        "engine": "opendataloader",
        "clean": True,
        "allow_fallback": True,
        "fallback_engine": "pymupdf",
    }
    assert result.used_engine == "pymupdf-fallback"


def test_prepare_normalizes_builtin_extractor_alias_before_use(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    observed_engines = []

    def alias_extractor(path: Path, engine: str):
        observed_engines.append(engine)
        return SimpleNamespace(
            text=_extract(path, "deterministic-extractor"),
            used_engine=engine,
        )

    plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        extractor_name="pspdfkit",
        extractor=alias_extractor,
    )

    assert observed_engines == ["pdf-to-markdown", "pdf-to-markdown"]
    assert plan.profile.extractor == "pdf-to-markdown|fallback=pymupdf"


def test_prepare_disables_fallback_for_pending_extractor_override(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)

    with pytest.raises(NativeBuildError, match="undeclared engine"):
        _prepare(
            data_root,
            sources,
            DeterministicEmbeddings(),
            extractor_name="opendataloader",
            allow_extraction_fallback=False,
            extractor=lambda path, engine: SimpleNamespace(
                text=_extract(path, "deterministic-extractor"),
                used_engine="pymupdf-fallback",
            ),
        )


def test_default_extractor_can_disable_v1_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    captured = {}

    def fake_extract_pdf_text(
        path,
        engine,
        *,
        clean,
        allow_fallback,
        fallback_engine,
    ):
        captured["allow_fallback"] = allow_fallback
        captured["fallback_engine"] = fallback_engine
        return ExtractionResult(
            requested_engine=engine,
            used_engine=engine,
            text="candidate text",
        )

    monkeypatch.setattr(pdf_extraction, "extract_pdf_text", fake_extract_pdf_text)

    build_service._default_extractor(
        source,
        "opendataloader",
        allow_fallback=False,
    )

    assert captured["allow_fallback"] is False
    assert captured["fallback_engine"] == "pymupdf"


def test_prepare_preserves_v1_single_level_chunking_policy(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    embeddings = DeterministicEmbeddings()

    plan = _prepare(
        data_root,
        sources,
        embeddings,
        use_parent_child=False,
        single_chunk_size=20,
    )

    profile = plan.profile.to_dict()
    assert profile["parent_policy"] == {
        "algorithm": "langchain-recursive-single-level-v1",
        "chunk_overlap": 2,
        "chunk_size": 20,
        "headers": ["#", "##", "###"],
        "separators": ["\n\n", "\n", ". ", " ", ""],
    }
    assert profile["child_policy"] == {
        "algorithm": "identity-span-v1",
        "span_source": "whole-parent",
    }
    parents = {parent.parent_uid: parent for parent in plan.parents}
    reports = {report.report_uid: report for report in plan.reports}
    assert len(plan.parents) == len(plan.chunks)
    for chunk in plan.chunks:
        parent = parents[chunk.parent_uid]
        report = reports[parent.report_uid]
        assert chunk.child_order == 0
        assert chunk.span_start == 0
        assert chunk.span_end == len(parent.content)
        expected_text = PREFIX.format(
            target_name=report.target_name,
            title=report.title,
        ) + parent.content
        assert chunk.embedding_text_sha256 == hashlib.sha256(
            expected_text.encode("utf-8")
        ).hexdigest()
    assert embeddings.calls[-1] == [
        PREFIX.format(
            target_name=reports[parents[chunk.parent_uid].report_uid].target_name,
            title=reports[parents[chunk.parent_uid].report_uid].title,
        )
        + parents[chunk.parent_uid].content
        for chunk in plan.chunks
    ]


def test_prepare_rejects_source_mutation_during_extraction(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)

    def mutating_extractor(path: Path, engine: str) -> str:
        if path.name == "a.pdf":
            (sources / "b.pdf").write_bytes(b"mutated-during-extraction")
        return _extract(path, engine)

    with pytest.raises(NativeBuildError, match="source corpus membership or bytes changed"):
        _prepare(
            data_root,
            sources,
            DeterministicEmbeddings(),
            extractor=mutating_extractor,
        )


def test_materialize_rejects_source_membership_change(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())
    (sources / "unexpected.pdf").write_bytes(b"new-source")

    with pytest.raises(NativeBuildError, match="source corpus membership or bytes changed"):
        materialize_candidate(plan, data_root)

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM retrieval_builds WHERE build_id = ?",
            (plan.build_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_publish_rejects_source_change_after_materialization(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())
    result = materialize_candidate(plan, data_root)
    (sources / "b.pdf").write_bytes(b"changed-after-materialization")
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        runtime_before = connection.execute(
            "SELECT * FROM retrieval_runtime WHERE runtime_id = 1"
        ).fetchone()
    finally:
        connection.close()

    with pytest.raises(NativeBuildError, match="source corpus membership or bytes changed"):
        publish_candidate(result, data_root)

    connection = sqlite3.connect(catalog)
    try:
        runtime_after = connection.execute(
            "SELECT * FROM retrieval_runtime WHERE runtime_id = 1"
        ).fetchone()
        journal_count = connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE publication_id = ?",
            (plan.publication_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert runtime_after == runtime_before
    assert journal_count == 0


def test_missing_active_source_requires_explicit_deletion(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    (sources / "a.pdf").unlink()

    with pytest.raises(NativeBuildError, match="missing without an explicit deletion"):
        _prepare(data_root, sources, DeterministicEmbeddings())


def test_first_successor_without_new_logical_member_is_rejected_before_embedding(
    tmp_path: Path,
):
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
    )
    (sources / "b.pdf").unlink()
    embeddings = DeterministicEmbeddings()

    with pytest.raises(NativeBuildError, match="new logical corpus member"):
        _prepare(data_root, sources, embeddings)

    assert embeddings.calls == []


def test_explicit_deletion_is_a_versioned_manifest_exclusion(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    (sources / "a.pdf").unlink()

    plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        deleted_relative_paths=("downloaded/a.pdf",),
    )
    decisions = {entry.status: entry for entry in plan.manifest.entries}

    assert plan.manifest.included_count == 1
    assert plan.manifest.excluded_count == 1
    assert decisions["included"].reason_code == "included"
    assert decisions["excluded"].reason_code == "source-deleted"
    assert plan.manifest.exclusion_policy.version == "native-full-corpus-v2"
    assert "source-deleted" in plan.manifest.exclusion_policy.excluded_reason_codes
    assert (
        "source-extraction-failed"
        in plan.manifest.exclusion_policy.excluded_reason_codes
    )

    result = materialize_candidate(plan, data_root)
    publish_candidate(result, data_root)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        active_paths = {
            row[0]
            for row in connection.execute(
                "SELECT canonical_relative_path FROM active_reports"
            )
        }
        old_object_count = connection.execute(
            "SELECT COUNT(*) FROM reports WHERE canonical_relative_path = 'downloaded/a.pdf'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert active_paths == {"downloaded/b.pdf"}
    assert old_object_count == 1


def test_corrupt_active_forward_builds_a_new_immutable_successor(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    first_plan = _prepare(data_root, sources, DeterministicEmbeddings())
    first_result = materialize_candidate(first_plan, data_root)
    first_outcome = publish_candidate(first_result, data_root)
    data_root.joinpath(*first_result.snapshot_relative_path.split("/")).write_bytes(
        b"corrupt-active-snapshot"
    )

    recovery = StartupReconciler(data_root).reconcile()
    assert recovery.disposition is RecoveryDisposition.PREDECESSOR_DEGRADED
    assert recovery.active_snapshot_id == first_plan.base_snapshot_id
    assert recovery.write_epoch == first_outcome.write_epoch == 1
    assert recovery.degraded

    recovered_result, recovered_outcome = execute_full_corpus_successor(
        data_root / "reports.db",
        sources,
        data_root=data_root,
        embeddings=DeterministicEmbeddings(),
        model="model-a",
        extractor_name="deterministic-extractor",
        parent_chunk_size=40,
        child_chunk_size=20,
        extractor=_extract,
        metadata_parser=_metadata,
    )

    assert recovered_result.snapshot_id != first_result.snapshot_id
    assert recovered_outcome.active_snapshot_id == recovered_result.snapshot_id
    assert recovered_outcome.predecessor_snapshot_id == first_plan.base_snapshot_id
    assert recovered_outcome.write_epoch == 2
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        runtime = connection.execute(
            """
            SELECT active_snapshot_id, predecessor_snapshot_id,
                   publication_generation, write_epoch,
                   v1_fallback_open, degraded, write_enabled
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
        failed_state = connection.execute(
            "SELECT state FROM vector_snapshots WHERE snapshot_id = ?",
            (first_result.snapshot_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert runtime[0] == recovered_result.snapshot_id
    assert runtime[1] == first_plan.base_snapshot_id
    assert runtime[3:] == (2, 0, 0, 1)
    assert failed_state == "failed"
