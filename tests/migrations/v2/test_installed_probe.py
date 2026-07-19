from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from src.migrations.v2.validation import installed_probe
from tests.retrieval.test_retrieval_repository import _create_catalog


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _query_spec() -> dict[str, object]:
    query_text = "Alpha native child body one"
    vector = [0.0, 5.0]
    query_text_sha256 = _digest(query_text)
    vector_sha256 = hashlib.sha256(
        np.asarray(vector, dtype=np.float32).tobytes()
    ).hexdigest()
    return {
        "schema_version": 1,
        "kind": "v2_release_semantic_query",
        "query_id": "synthetic-alpha-query",
        "query_text": query_text,
        "vector": vector,
        "embedding_attestation": {
            "provider": "openrouter",
            "model": "test-model",
            "input_type": "search_query",
            "provider_calls": 1,
            "query_text_sha256": query_text_sha256,
            "vector_sha256": vector_sha256,
        },
        "expected_report_uid": _digest("report-1"),
        "expected_citation": {
            "canonical_relative_path": "reports/company-1.pdf",
            "report_type": "company",
            "report_date": "2026-07-01",
            "target_name": "Alpha",
            "title": "Report 1",
            "broker": "Broker A",
        },
        "k": 3,
        "scopes": {
            "unfiltered": None,
            "empty": {"empty": True},
            "narrow": {
                "target_name": "Alpha",
                "report_date": "2026-07-01",
            },
            "broad": {"report_type": "company"},
            "near_universe": {
                "report_date_start": "2026-07-01",
                "report_date_end": "2026-07-05",
            },
            "prior_scope": {
                "prior_scope": {"canonical_relative_path": "reports/company-1.pdf"}
            },
        },
    }


def test_probe_runs_all_scope_classes_and_seals_gate_d_citation(tmp_path: Path):
    root = tmp_path / "data-root"
    root.mkdir()
    catalog, _rows = _create_catalog(root, count=5, generation=7)
    specification = _query_spec()

    result = installed_probe.run_probe(root, specification, samples=3)

    assert result["passed"] is True
    assert result["kind"] == "v2_installed_validation_probe"
    assert result["query_id"] == "synthetic-alpha-query"
    assert "query_text" not in result
    assert result["query_text_sha256"] == _digest(
        str(specification["query_text"])
    )
    assert result["query_vector_sha256"] == hashlib.sha256(
        np.asarray(specification["vector"], dtype=np.float32).tobytes()
    ).hexdigest()
    assert result["query_generation"] == {
        "provider": "openrouter",
        "model": "test-model",
        "input_type": "search_query",
        "provider_calls": 1,
        "attestation_sha256": _digest(
            json.dumps(
                specification["embedding_attestation"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    }
    assert set(result["workloads"]) == {
        "unfiltered",
        "empty",
        "narrow",
        "broad",
        "near_universe",
        "prior_scope",
    }
    assert result["workloads"]["empty"]["faiss_calls"] == [0, 0, 0]
    assert result["workloads"]["narrow"]["top_report_uids"] == [
        _digest("report-1")
    ] * 3
    assert result["gate_d_search"] == {
        "expected_report_uid": _digest("report-1"),
        "top_report_uid": _digest("report-1"),
        "top_rank": 1,
        "citation_complete": True,
        "citation_sha256": _digest(
            json.dumps(
                specification["expected_citation"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
    }
    assert result["runtime_identity"]["active_snapshot_id"] == "snapshot-1"
    assert result["runtime_identity"]["publication_generation"] == 7
    assert catalog.is_file()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(query_text=""), "query text"),
        (lambda value: value.update(vector=[1.0]), "attestation"),
        (lambda value: value["scopes"].pop("empty"), "workloads"),
        (
            lambda value: value.update(expected_report_uid="not-a-digest"),
            "report UID",
        ),
        (
            lambda value: value["embedding_attestation"].update(
                vector_sha256="0" * 64
            ),
            "attestation",
        ),
    ],
)
def test_probe_rejects_incomplete_or_malformed_query_contract(
    tmp_path: Path,
    mutation,
    message: str,
):
    root = tmp_path / "data-root"
    root.mkdir()
    _create_catalog(root, count=5, generation=7)
    specification = json.loads(json.dumps(_query_spec()))
    mutation(specification)

    with pytest.raises(installed_probe.InstalledProbeError, match=message):
        installed_probe.run_probe(root, specification, samples=2)


def test_probe_rejects_wrong_expected_gate_d_report(tmp_path: Path):
    root = tmp_path / "data-root"
    root.mkdir()
    _create_catalog(root, count=5, generation=7)
    specification = _query_spec()
    specification["expected_report_uid"] = _digest("report-2")

    with pytest.raises(installed_probe.InstalledProbeError, match="Gate D"):
        installed_probe.run_probe(root, specification, samples=2)
