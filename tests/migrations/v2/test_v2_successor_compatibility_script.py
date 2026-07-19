from __future__ import annotations

from scripts.migrations.v2.analyze_v2_successor_compatibility import analyze_compatibility
from src.retrieval.build_service import materialize_candidate, publish_candidate
from tests.retrieval.test_retrieval_build_service import (
    DeterministicEmbeddings,
    _native_seed,
    _prepare,
)
from tests.migrations.v2.test_release_transitions import _write_query_spec


def test_successor_compatibility_analysis_fails_closed_on_content_loss(tmp_path):
    data_root, sources = _native_seed(tmp_path)

    def lossy_extract(path, engine):
        assert engine == "deterministic-extractor"
        if path.name == "a.pdf":
            return "replacement content with no legacy chunks"
        return "sector outlook newly searchable content"

    plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        extractor=lossy_extract,
    )
    publish_candidate(materialize_candidate(plan, data_root), data_root)
    query_spec = tmp_path / "query.json"
    _write_query_spec(data_root, query_spec)

    evidence = analyze_compatibility(data_root, query_spec, k=3)

    quality = evidence["retrieval_quality"]
    assert evidence["passed"] is False
    assert evidence["approved"] is False
    assert evidence["release_eligible"] is False
    assert evidence["structural_delta"]["unique_chunk_content_loss"] > 0
    assert quality["predecessor_vector_queries"] == 2
    assert quality["citation_complete_top_one"] == 2
    assert quality["gate_d_query_passed"] is True
