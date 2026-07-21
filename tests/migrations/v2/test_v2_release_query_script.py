from __future__ import annotations

import hashlib

from scripts.migrations.v2.create_v2_release_query import create_query_spec
from tests.retrieval.test_retrieval_repository import _create_catalog


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_create_query_spec_binds_natural_language_vector_and_exact_citation(
    tmp_path,
):
    root = tmp_path / "data-root"
    root.mkdir()
    _create_catalog(root, count=5, generation=7)
    observed = []

    payload = create_query_spec(
        root,
        query_id="alpha-gate-d",
        query_text="Alpha native child body one",
        expected_report_uid=_digest("report-1"),
        k=3,
        embed_query=lambda text: observed.append(text) or [0.0, 5.0],
    )

    assert observed == ["Alpha native child body one"]
    assert payload["kind"] == "v2_release_semantic_query"
    assert payload["embedding_attestation"]["provider_calls"] == 1
    assert payload["embedding_attestation"]["input_type"] == "search_query"
    assert payload["embedding_attestation"]["model"] == "test-model"
    assert payload["expected_citation"] == {
        "canonical_relative_path": "reports/company-1.pdf",
        "report_type": "company",
        "report_date": "2026-07-01",
        "target_name": "Alpha",
        "title": "Report 1",
        "broker": "Broker A",
    }
    assert payload["scopes"]["narrow"] == {
        "target_name": "Alpha",
        "report_date": "2026-07-01",
    }
    assert payload["scopes"]["prior_scope"] == {
        "prior_scope": {"canonical_relative_path": "reports/company-1.pdf"}
    }
