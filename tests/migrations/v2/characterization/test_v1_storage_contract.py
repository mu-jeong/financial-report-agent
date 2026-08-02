from __future__ import annotations

import builtins
import hashlib
import sqlite3
from pathlib import Path

import numpy as np
import pytest
import requests
from langchain_community.vectorstores import FAISS
from langchain_core.embeddings import Embeddings

from src.core import db_manager, embed_pipeline, pdf_extraction, report_crawler
from src.core.metadata_filters import filter_docs_with_scores
from src.llms import embeddings as embeddings_module
from src.migrations.v2.evidence import (
    create_copied_v1_install,
    seal_compatibility_bundle,
)
from src.migrations.v2.import_v1 import convert_v1_seed
from src.nodes import vectordb
from src.utils.citations import (
    document_rank_aliases,
    group_sources_by_document,
    normalize_citation_ranks,
    remove_unavailable_citations,
)
from tests.migrations.v2.fixtures_factory.v1 import PREFIX_TEMPLATE, build_v1_fixture


class _FixtureEmbeddings(Embeddings):
    def __init__(self, query_vector: np.ndarray):
        self.query_vector = query_vector.tolist()

    def embed_documents(self, _texts: list[str]) -> list[list[float]]:
        raise AssertionError("characterization must not create new embeddings")

    def embed_query(self, _text: str) -> list[float]:
        return list(self.query_vector)


def _load_v1_store(fixture) -> FAISS:
    return FAISS.load_local(
        str(fixture.root / "vector_db"),
        _FixtureEmbeddings(np.zeros(fixture.known_vectors.shape[1], dtype=np.float32)),
        allow_dangerous_deserialization=True,
    )


def test_generated_v1_fixture_is_byte_reproducible_without_repository_binaries(tmp_path):
    first = build_v1_fixture(tmp_path / "first")
    second = build_v1_fixture(tmp_path / "second")

    assert first.artifact_hashes == second.artifact_hashes
    assert first.artifact_sizes == second.artifact_sizes
    assert first.current_artifact_hashes() == first.artifact_hashes
    assert first.symbolic_n == len(first.documents_by_ordinal)
    assert all(len(digest) == 64 for digest in first.artifact_hashes.values())
    assert first.root.parent == tmp_path


def test_v1_database_contract_contains_legacy_readiness_and_parent_tables(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    monkeypatch.setattr(db_manager, "DB_PATH", str(db_path))

    db_manager.init_db()

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        report_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(reports)")
        }

    assert "parent_chunks" in tables
    assert {"file_name", "is_embedded"} <= report_columns


def test_v1_parent_child_embedding_text_and_metadata_contract(monkeypatch):
    monkeypatch.setattr(embed_pipeline.config, "USE_PARENT_CHILD", True)
    monkeypatch.setattr(embed_pipeline.config, "PARENT_CHUNK_SIZE", 2000)
    monkeypatch.setattr(embed_pipeline.config, "CHILD_CHUNK_SIZE", 500)
    monkeypatch.setattr(embed_pipeline.uuid, "uuid4", lambda: "legacy-parent-id")
    state = {
        "raw_text": "# Summary\nRevenue grew and margins improved.",
        "file_name": "company_2026-07-16_A_Broker_Result.pdf",
        "target_name": "A",
        "title": "Result",
        "report_date": "2026-07-16",
        "report_type": "company",
        "broker": "Broker",
    }

    result = embed_pipeline.node_split_documents(state)

    assert len(result["parent_documents"]) == 1
    assert len(result["documents"]) == 1
    parent = result["parent_documents"][0]
    child = result["documents"][0]
    assert parent.page_content == "# Summary\nRevenue grew and margins improved."
    assert child.page_content == (
        "[Company: A, Title: Result]\n"
        "# Summary\nRevenue grew and margins improved."
    )
    assert child.metadata == {
        "Header 1": "Summary",
        "parent_id": "legacy-parent-id",
        "child_index": 0,
        "file_name": state["file_name"],
        "target_name": "A",
        "title": "Result",
        "report_date": "2026-07-16",
        "report_type": "company",
        "broker": "Broker",
    }


def test_v1_symbolic_n_mapping_repeated_bodies_and_known_float_ranking(tmp_path):
    fixture = build_v1_fixture(tmp_path / "v1")
    store = _load_v1_store(fixture)
    query = np.zeros(fixture.known_vectors.shape[1], dtype=np.float32)

    docs_with_scores = store.similarity_search_with_score_by_vector(
        query.tolist(),
        k=fixture.symbolic_n,
    )
    reconstructed = store.index.reconstruct_n(0, fixture.symbolic_n)
    bodies = [document.page_content.split("\n", 1)[1] for document, _ in docs_with_scores]

    assert store.index.ntotal == fixture.symbolic_n
    assert len(store.index_to_docstore_id) == fixture.symbolic_n
    assert len(store.docstore._dict) == fixture.symbolic_n
    assert len(docs_with_scores) == fixture.symbolic_n
    assert np.array_equal(reconstructed, fixture.known_vectors)
    assert [score for _, score in docs_with_scores] == pytest.approx(
        fixture.known_squared_l2,
        abs=1e-7,
    )
    assert bodies.count("same") == 2
    assert {
        document.metadata["parent_id"]
        for document, _ in docs_with_scores
        if document.page_content.endswith("same")
    } == {"legacy-parent-alpha-main", "legacy-parent-beta-main"}


def test_v1_filters_preserve_embedded_report_and_vector_eligibility(tmp_path, monkeypatch):
    fixture = build_v1_fixture(tmp_path / "v1")
    store = _load_v1_store(fixture)
    all_docs = store.similarity_search_with_score_by_vector(
        [0.0] * fixture.known_vectors.shape[1],
        k=fixture.symbolic_n,
    )
    filters = {
        "report_type": "company",
        "target_name": "Alpha Corp",
        "report_date_start": "2026-02-01",
        "report_date_end": "2026-02-28",
    }

    filtered_docs = filter_docs_with_scores(all_docs, filters)
    monkeypatch.setattr(vectordb, "DB_PATH", str(fixture.root / "reports.db"))
    report_rows = vectordb.fetch_report_universe_for_filters(filters)

    assert {doc.metadata["file_name"] for doc, _ in filtered_docs} == {
        fixture.file_names["company_february"]
    }
    assert {row["file_name"] for row in report_rows} == {
        fixture.file_names["company_february"]
    }
    assert fixture.file_names["excluded"] not in {
        row["file_name"] for row in report_rows
    }


def test_v1_rerank_then_coverage_keeps_one_passage_per_required_report(
    tmp_path, monkeypatch
):
    fixture = build_v1_fixture(tmp_path / "v1")
    store = _load_v1_store(fixture)
    all_docs = store.similarity_search_with_score_by_vector(
        [0.0] * fixture.known_vectors.shape[1],
        k=fixture.symbolic_n,
    )

    class _PreservingRanker:
        def __init__(self):
            self.calls = []

        def rerank(self, query, passages, top_k):
            self.calls.append((query, tuple(passages), top_k))
            return [
                {**passage, "rerank_score": 1.0 - (index / 10)}
                for index, passage in enumerate(passages[:top_k])
            ]

    ranker = _PreservingRanker()
    monkeypatch.setattr(vectordb, "fetch_parent_content", fixture.parent_contents.get)
    monkeypatch.setattr(vectordb, "get_ranker", lambda: ranker)
    monkeypatch.setattr(vectordb, "USE_RERANKER", True)
    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 2)
    monkeypatch.setattr(vectordb, "SEARCH_CANDIDATE_MULTIPLIER", 2)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0.0)
    required_files = [
        fixture.file_names["company_january"],
        fixture.file_names["company_february"],
    ]

    selected, metrics = vectordb.select_top_passages(
        "compare the required reports",
        all_docs,
        search_filters={"target_name": "Alpha Corp"},
        required_file_names=required_files,
    )

    assert len(ranker.calls) == 1
    assert ranker.calls[0][2] == len(fixture.parent_contents)
    assert [passage["meta"]["file_name"] for passage in selected] == required_files
    assert metrics == {
        "document_coverage_applied": True,
        "document_coverage_reason": "required_file_names",
    }


def test_v1_citations_collapse_repeated_chunks_without_source_loss(tmp_path):
    fixture = build_v1_fixture(tmp_path / "v1")
    first, second, _, third = fixture.documents_by_ordinal[:4]
    rerank_info = [
        {**first.metadata, "rank": 1},
        {**second.metadata, "rank": 2},
        {**third.metadata, "rank": 3},
    ]

    grouped = group_sources_by_document(rerank_info)
    aliases = document_rank_aliases(rerank_info)
    normalized = normalize_citation_ranks(
        "January evidence [1][2]; February evidence [3]; missing [4].",
        aliases,
    )
    rendered = remove_unavailable_citations(normalized, source_count=len(grouped))

    assert [group["ranks"] for group in grouped] == [[1, 2], [3]]
    assert aliases == {1: 1, 2: 1, 3: 2}
    assert rendered == "January evidence [1]; February evidence [2]; missing ."


def test_v1_conversion_calls_no_crawler_extractor_chunker_embedder_network_or_pdf(
    tmp_path, monkeypatch
):
    source = build_v1_fixture(tmp_path / "source")
    source_before = source.current_artifact_hashes()
    copied_root = tmp_path / "copied"
    copy_evidence = create_copied_v1_install(source.root, copied_root)
    copied_hashes = {
        artifact.relative_path: artifact.sha256
        for artifact in copy_evidence.copied_artifacts
    }
    data_root = tmp_path / "data-root"
    bundle = seal_compatibility_bundle(copied_root, data_root)
    calls = {
        "crawler": 0,
        "extraction": 0,
        "chunking": 0,
        "embedding": 0,
        "network": 0,
        "pdf_reads": 0,
    }

    def forbidden(name):
        def fail(*_args, **_kwargs):
            calls[name] += 1
            pytest.fail(f"{name} called during zero-call conversion")

        return fail

    monkeypatch.setattr(report_crawler, "download_naver_reports", forbidden("crawler"))
    monkeypatch.setattr(pdf_extraction, "extract_pdf_text", forbidden("extraction"))
    monkeypatch.setattr(embed_pipeline, "node_extract_pdf", forbidden("extraction"))
    monkeypatch.setattr(embed_pipeline, "node_split_documents", forbidden("chunking"))
    monkeypatch.setattr(embed_pipeline, "node_embed_and_store", forbidden("embedding"))
    monkeypatch.setattr(embed_pipeline, "build_embeddings_model", forbidden("embedding"))
    monkeypatch.setattr(embeddings_module, "build_embeddings_model", forbidden("embedding"))
    monkeypatch.setattr(
        embeddings_module.OpenRouterEmbeddings,
        "embed_documents",
        forbidden("embedding"),
    )
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden("network"))

    real_path_open = Path.open
    real_builtin_open = builtins.open

    def guarded_path_open(path, *args, **kwargs):
        if path.suffix.casefold() == ".pdf":
            calls["pdf_reads"] += 1
            pytest.fail("PDF opened during zero-call conversion")
        return real_path_open(path, *args, **kwargs)

    def guarded_builtin_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)) and Path(file).suffix.casefold() == ".pdf":
            calls["pdf_reads"] += 1
            pytest.fail("PDF opened during zero-call conversion")
        return real_builtin_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(builtins, "open", guarded_builtin_open)

    result = convert_v1_seed(
        copied_root,
        data_root,
        expected_hashes=copied_hashes,
        profile=source.embedding_profile(),
        source_hashes=source.source_hashes,
        compatibility_bundle_id=bundle.bundle_id,
    )

    assert result.chunk_count == source.symbolic_n
    assert calls == {name: 0 for name in calls}
    assert source.current_artifact_hashes() == source_before
    assert {
        relative_path: hashlib.sha256((copied_root / relative_path).read_bytes()).hexdigest()
        for relative_path in copied_hashes
    } == copied_hashes
