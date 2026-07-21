from __future__ import annotations

import hashlib
import json
import stat
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import faiss
import numpy as np
import pytest

from scripts.migrations.v2 import run_v2_reader_parity
from src.migrations.v2.evidence import seal_compatibility_bundle
from src.migrations.v2.import_v1 import convert_v1_seed
from src.retrieval.identity import canonical_json
from tests.migrations.v2.fixtures_factory.v1 import build_v1_fixture


def test_gate_c_accepts_exact_replay_evidence_and_rejects_tampering():
    policy = {
        "chunk_overlap": 1,
        "chunk_size": 12,
        "is_separator_regex": False,
        "keep_separator": True,
        "length_function": "python-len",
        "policy_id": "legacy-recursive-splitter-v1",
        "separators": ["\n\n", "\n", ". ", " ", ""],
        "strip_whitespace": True,
    }
    policy_hash = hashlib.sha256(canonical_json(policy).encode()).hexdigest()
    row = {
        "publication_id": "11" * 32,
        "build_id": "22" * 32,
        "snapshot_id": "33" * 32,
        "profile_hash": "44" * 32,
        "snapshot_path": "retrieval/v2/snapshots/example.faiss",
        "snapshot_sha256": "55" * 32,
        "size_bytes": 10,
        "dimension": 3,
        "metric": "l2",
        "ntotal": 4,
    }
    manifest = {
        "schema_version": 2,
        "publication_id": row["publication_id"],
        "build_id": row["build_id"],
        "snapshot_id": row["snapshot_id"],
        "profile_hash": row["profile_hash"],
        "compatibility_bundle_id": "66" * 32,
        "assessment_digest": "77" * 32,
        "reconstruction_digest": "88" * 32,
        "source_manifest_sha256": "99" * 32,
        "snapshot": {
            "relative_path": row["snapshot_path"],
            "sha256": row["snapshot_sha256"],
            "size_bytes": 10,
            "dimension": 3,
            "metric": "l2",
            "ntotal": 4,
        },
        "counts": {"reports": 1, "parents": 1, "chunks": 4},
        "legacy_mapping": {
            "relative_path": "retrieval/v2/evidence/example/legacy-mapping.json",
            "sha256": "aa" * 32,
        },
        "vector_max_absolute_error": 0.0,
        "prohibited_conversion_calls": {
            "api": 0,
            "chunking": 0,
            "crawler": 0,
            "embedding": 0,
            "extraction": 0,
            "network": 0,
            "pdf_reads": 0,
        },
        "span_reconstruction": {
            "claims": [{
                "ambiguous_child_order": 2,
                "full_sequence_replay_matched": True,
                "global_assignment_cardinality": "multiple",
                "legacy_parent_id": "p1",
                "local_occurrence_count": 6,
                "method": "legacy-recursive-splitter-v1",
                "policy_id": "legacy-recursive-splitter-v1",
                "policy_sha256": policy_hash,
                "selected_start": 20,
            }],
            "method": "ordered-span-v1-with-ambiguity-replay",
            "operations": {
                "embedding": 0,
                "legacy_replay_chunking": 1,
                "network": 0,
                "pdf_reads": 0,
                "source_pdf_chunking": 0,
            },
            "policy": policy,
            "policy_sha256": policy_hash,
            "replayed_parent_count": 1,
            "resolver_version": 2,
        },
    }

    run_v2_reader_parity._validate_conversion_manifest(
        manifest,
        row=row,
        bundle_id="66" * 32,
        source_hash="99" * 32,
        counts=(1, 1, 4),
    )
    tampered = deepcopy(manifest)
    tampered["span_reconstruction"]["claims"][0]["selected_start"] = -1
    with pytest.raises(run_v2_reader_parity.ReaderParityError, match="offset"):
        run_v2_reader_parity._validate_conversion_manifest(
            tampered,
            row=row,
            bundle_id="66" * 32,
            source_hash="99" * 32,
            counts=(1, 1, 4),
        )


def test_cli_canonicalizes_identical_vector_ties_and_redacts_evidence(tmp_path: Path):
    copied = tmp_path / "copied"
    fixture = build_v1_fixture(copied)
    tied_vectors = np.zeros_like(fixture.known_vectors)
    tied_index = faiss.IndexFlatL2(tied_vectors.shape[1])
    tied_index.add(tied_vectors)
    _write_faiss(tied_index, copied / "vector_db" / "index.faiss")
    expected_hashes = {
        relative: _sha256(copied / relative)
        for relative in fixture.artifact_hashes
    }

    data_root = tmp_path / "native root 한글"
    data_root.mkdir()
    bundle = seal_compatibility_bundle(copied, data_root)
    convert_v1_seed(
        copied,
        data_root,
        expected_hashes=expected_hashes,
        profile=fixture.embedding_profile(),
        source_hashes=fixture.source_hashes,
        compatibility_bundle_id=bundle.bundle_id,
    )
    mapping_path = next(
        (data_root / "retrieval" / "v2" / "evidence").glob(
            "*/legacy-mapping.json"
        )
    )
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    legacy_order = [row["chunk_uid"] for row in mapping["rows"]]
    assert legacy_order != sorted(legacy_order)

    query_input = tmp_path / "query-input.json"
    query_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "v2_retrieval_query_vectors",
                "k": 3,
                "queries": [
                    {"query_id": "opaque-query-1", "vector": [0.0, 0.0, 0.0]}
                ],
                "workloads": {
                    "unfiltered": {"scope": None},
                    "empty": {"scope": {"empty": True}},
                    "narrow": {
                        "scope": {"file_name": fixture.file_names["company_january"]}
                    },
                    "broad": {"scope": {"report_date_start": "2026-01-01"}},
                    "near_universe": {
                        "scope": {"report_date_start": "2026-01-01"}
                    },
                    "prior_scope": {
                        "scope": {
                            "prior_scope": {
                                "file_name": fixture.file_names["company_february"]
                            }
                        }
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    scope_input = tmp_path / "scope-input.json"
    scope_input.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "v2_reader_parity_scopes",
                "workloads": {
                    "unfiltered": {"scope": None},
                    "company": {"scope": {"company": True}},
                    "report_type": {"scope": {"report_type": "industry"}},
                    "date": {
                        "scope": {
                            "report_date_start": "2026-02-01",
                            "report_date_end": "2026-02-28",
                        }
                    },
                    "narrow": {
                        "scope": {"file_name": fixture.file_names["company_january"]}
                    },
                    "broad": {"scope": {"report_date_start": "2026-01-01"}},
                    "empty": {"scope": {"empty": True}},
                    "prior_scope": {
                        "scope": {
                            "prior_scope": {
                                "file_name": fixture.file_names["company_february"]
                            }
                        }
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "reader-parity.json"

    assert run_v2_reader_parity.main(
        [
            "--data-root",
            str(data_root),
            "--query-input",
            str(query_input),
            "--scope-input",
            str(scope_input),
            "--output",
            str(output),
        ]
    ) == 0

    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["status"] == "passed"
    assert evidence["protocol"]["request_count"] == 8
    assert evidence["counts"]["legacy_exact_score_tie_groups"] > 0
    assert evidence["counts"]["native_exact_score_tie_groups"] > 0
    assert not any(evidence["mismatches"].values())
    encoded = output.read_text(encoding="utf-8")
    assert str(tmp_path) not in encoded
    assert "alpha-extra" not in encoded
    assert "canonical_relative_path" not in encoded
    assert not (
        output.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )
    with pytest.raises(run_v2_reader_parity.ReaderParityError, match="already exists"):
        run_v2_reader_parity.main(
            [
                "--data-root",
                str(data_root),
                "--query-input",
                str(query_input),
                "--scope-input",
                str(scope_input),
                "--output",
                str(output),
            ]
        )


def test_unequal_score_difference_remains_a_parity_failure():
    citation = run_v2_reader_parity._CitationSource(
        report_uid="a" * 64,
        canonical_relative_path="downloaded/report.pdf",
        source_sha256="b" * 64,
    )
    legacy = run_v2_reader_parity._ParityHit(
        chunk_uid="c" * 64,
        parent_uid="d" * 64,
        score=1.0,
        body="body",
        source=("report.pdf", "company", "2026-01-01", "Target", "Title", "Broker"),
        citation_source=citation,
    )
    native = replace(legacy, score=2.0)

    mismatches = run_v2_reader_parity._compare_hits((legacy,), (native,), k=1)

    assert mismatches["top_k_scores"] == 1
    assert sum(mismatches.values()) == 1


def _write_faiss(index: faiss.Index, path: Path) -> None:
    with path.open("wb") as stream:
        writer = faiss.PyCallbackIOWriter(stream.write)
        faiss.write_index(index, writer)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
