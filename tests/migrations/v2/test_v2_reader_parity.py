from __future__ import annotations

from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_core.embeddings import Embeddings

from src.core.metadata_filters import filter_docs_with_scores
from src.migrations.v2.evidence import seal_compatibility_bundle
from src.migrations.v2.import_v1 import convert_v1_seed
from src.retrieval.reader import NativeRetrievalReader
from src.retrieval.repository import CatalogRepository
from tests.migrations.v2.fixtures_factory.v1 import build_v1_fixture


class _NoEmbeddingCalls(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("parity must reuse stored vectors")

    def embed_query(self, text: str) -> list[float]:
        raise AssertionError("parity supplies a fixed query vector")


def _body(page_content: str) -> str:
    return page_content.split("\n", 1)[1]


def test_converted_reader_matches_v1_ids_ranks_sources_and_filters(tmp_path: Path):
    copied = tmp_path / "copied"
    fixture = build_v1_fixture(copied)
    data_root = tmp_path / "native reader 한글"
    bundle = seal_compatibility_bundle(copied, data_root)
    convert_v1_seed(
        copied,
        data_root,
        expected_hashes=fixture.artifact_hashes,
        profile=fixture.embedding_profile(),
        source_hashes=fixture.source_hashes,
        compatibility_bundle_id=bundle.bundle_id,
    )
    legacy = FAISS.load_local(
        str(copied / "vector_db"),
        _NoEmbeddingCalls(),
        allow_dangerous_deserialization=True,
    )
    repository = CatalogRepository(
        data_root / "retrieval" / "v2" / "catalog.sqlite3",
        data_root=data_root,
    )
    reader = NativeRetrievalReader(repository)
    query = [0.0] * fixture.known_vectors.shape[1]
    workloads = (
        None,
        {"file_names": ["missing.pdf"]},
        {"target_name": "Alpha Corp", "report_date_start": "2026-02-01"},
        {"report_type": "company"},
        {"report_date_start": "2026-01-01", "report_date_end": "2026-12-31"},
        {"file_names": [fixture.file_names["company_january"]]},
    )
    try:
        all_legacy = legacy.similarity_search_with_score_by_vector(
            query,
            k=fixture.symbolic_n,
        )
        for scope in workloads:
            expected = filter_docs_with_scores(all_legacy, scope)
            actual = reader.search(query, k=fixture.symbolic_n, scope=scope).chunks

            assert [(_body(doc.page_content), doc.metadata["file_name"]) for doc, _ in expected] == [
                (chunk.parent_slice, chunk.file_name) for chunk in actual
            ]
            assert [score for _doc, score in expected] == [chunk.score for chunk in actual]
            assert all(chunk.publication_generation == 1 for chunk in actual)
            assert all(chunk.snapshot_id == actual[0].snapshot_id for chunk in actual) if actual else True
    finally:
        repository.close()
        repository.cache.close()
