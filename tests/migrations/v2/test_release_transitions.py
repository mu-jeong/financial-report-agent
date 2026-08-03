from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import numpy as np
import pytest

from src.migrations.v2.validation import release_transitions
from src.retrieval.build_service import materialize_candidate, publish_candidate
from src.retrieval.repository import CatalogRepository
from src.migrations.v2.validation.release_transitions import (
    ReleaseTransitionError,
    execute_release_transitions,
    validate_release_transition_evidence,
)
from tests.retrieval.test_retrieval_build_service import (
    DeterministicEmbeddings,
    _extract,
    _metadata,
    _native_seed,
    _prepare,
)


def _write_query_spec(data_root: Path, target: Path) -> None:
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        active = connection.execute(
            "SELECT active_snapshot_id FROM retrieval_runtime WHERE runtime_id = 1"
        ).fetchone()[0]
        report_uid, physical_id = connection.execute(
            """
            SELECT report.report_uid, membership.faiss_id
            FROM snapshot_membership AS membership
            JOIN retrieval_chunks AS chunk
              ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent
              ON parent.parent_uid = chunk.parent_uid
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE membership.snapshot_id = ?
              AND report.canonical_relative_path = 'downloaded/b.pdf'
            ORDER BY membership.faiss_id
            LIMIT 1
            """,
            (active,),
        ).fetchone()
    finally:
        connection.close()
    with CatalogRepository(catalog, data_root=data_root) as repository:
        with repository.request() as session:
            vector = session.index.reconstruct([physical_id])[0].tolist()
    payload = {
        "schema_version": 1,
        "kind": "v2_release_semantic_query",
        "query_id": "sector-outlook-gate-d",
        "query_text": "Sector outlook newly searchable content",
        "vector": vector,
        "embedding_attestation": {
            "provider": "openrouter",
            "model": "model-a",
            "input_type": "search_query",
            "provider_calls": 1,
            "query_text_sha256": hashlib.sha256(
                "Sector outlook newly searchable content".encode("utf-8")
            ).hexdigest(),
            "vector_sha256": hashlib.sha256(
                np.asarray(vector, dtype=np.float32).tobytes()
            ).hexdigest(),
        },
        "expected_report_uid": report_uid,
        "expected_citation": {
            "canonical_relative_path": "downloaded/b.pdf",
            "report_type": "industry",
            "report_date": "2026-01-02",
            "target_name": "Sector",
            "title": "Outlook",
            "broker": "Broker",
        },
        "k": 3,
        "scopes": {
            "unfiltered": None,
            "empty": {"empty": True},
            "narrow": {
                "target_name": "Sector",
                "report_date": "2026-01-02",
            },
            "broad": {"report_type": "industry"},
            "near_universe": {
                "report_date_start": "2026-01-01",
                "report_date_end": "2026-01-02",
            },
            "prior_scope": {
                "prior_scope": {"canonical_relative_path": "downloaded/b.pdf"}
            },
        },
    }
    target.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.slow
def test_release_transition_run_proves_recovery_lease_gc_and_gate_d(tmp_path: Path):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        fallback_extractor_name="deterministic-fallback",
    )
    first = materialize_candidate(plan, data_root)
    published = publish_candidate(first, data_root)
    protected_root = tmp_path / "protected-copy"
    shutil.copytree(data_root, protected_root)
    query_spec = tmp_path / "query.json"
    _write_query_spec(data_root, query_spec)
    protected_before = hashlib.sha256(
        b"".join(
            path.read_bytes()
            for path in sorted(protected_root.rglob("*"))
            if path.is_file()
        )
    ).hexdigest()

    evidence = execute_release_transitions(
        data_root,
        protected_root,
        sources,
        query_spec,
        extractor=_extract,
        metadata_parser=_metadata,
    )
    validated = validate_release_transition_evidence(evidence)

    protected_after = hashlib.sha256(
        b"".join(
            path.read_bytes()
            for path in sorted(protected_root.rglob("*"))
            if path.is_file()
        )
    ).hexdigest()
    assert evidence["passed"] is True
    assert evidence["fixture_only"] is False
    assert evidence["protected_root_unchanged"] is True
    assert validated["final_runtime_identity"] == evidence["final"]
    assert validated["protected_tree_sha256_after"] == evidence["copy_proof"][
        "protected_tree_sha256_after"
    ]
    assert protected_after == protected_before
    assert evidence["initial"]["publication_generation"] == published.publication_generation
    assert evidence["recovery"]["after"]["degraded"] is True
    assert evidence["forward_recovery"]["embedding"]["provider_calls"] == 0
    assert evidence["lease_gc"]["publication_blocked_while_leased"] is True
    assert evidence["lease_gc"]["retired_snapshot_deleted"] is True
    assert evidence["final"]["publication_generation"] == (
        evidence["initial"]["publication_generation"] + 3
    )
    assert evidence["gate_d_search"]["top_rank"] == 1
    assert evidence["gate_d_search"]["citation_complete"] is True
    assert [event["event"] for event in evidence["event_sequence"]] == [
        "initial_health_validated",
        "active_snapshot_corrupted",
        "predecessor_recovery_completed",
        "degraded_snapshot_leased",
        "forward_recovery_published",
        "next_candidate_materialized",
        "publication_blocked_while_leased",
        "lease_released",
        "successor_published",
        "retired_snapshot_garbage_collected",
        "gate_d_search_validated",
        "protected_root_revalidated",
    ]


def test_custom_replay_profile_requires_extractor_before_snapshot_mutation(
    tmp_path: Path,
):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        fallback_extractor_name="deterministic-fallback",
    )
    publish_candidate(materialize_candidate(plan, data_root), data_root)
    protected_root = tmp_path / "protected-copy"
    shutil.copytree(data_root, protected_root)
    query_spec = tmp_path / "query.json"
    _write_query_spec(data_root, query_spec)
    selection = release_transitions._inspect(data_root)
    snapshot_path = release_transitions._snapshot_path(
        data_root,
        selection.active_snapshot_id or "",
    )
    snapshot_sha256 = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()

    with pytest.raises(ReleaseTransitionError, match="extractor profile"):
        execute_release_transitions(
            data_root,
            protected_root,
            sources,
            query_spec,
            metadata_parser=_metadata,
        )

    assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == snapshot_sha256


def test_transition_rejects_a_hardlinked_protected_snapshot_before_mutation(
    tmp_path: Path,
):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())
    publish_candidate(materialize_candidate(plan, data_root), data_root)
    protected_root = tmp_path / "protected-copy"
    shutil.copytree(data_root, protected_root)
    query_spec = tmp_path / "query.json"
    _write_query_spec(data_root, query_spec)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    connection = sqlite3.connect(catalog)
    try:
        relative_path = connection.execute(
            """
            SELECT snapshot.relative_path
            FROM retrieval_runtime AS runtime
            JOIN vector_snapshots AS snapshot
              ON snapshot.snapshot_id = runtime.active_snapshot_id
            WHERE runtime.runtime_id = 1
            """
        ).fetchone()[0]
    finally:
        connection.close()
    dedicated_snapshot = data_root.joinpath(*str(relative_path).split("/"))
    protected_snapshot = protected_root.joinpath(*str(relative_path).split("/"))
    os.chmod(protected_snapshot, stat.S_IREAD | stat.S_IWRITE)
    protected_snapshot.unlink()
    try:
        os.link(dedicated_snapshot, protected_snapshot)
    except OSError as exc:
        pytest.skip(f"hardlinks are unavailable: {exc}")
    snapshot_sha256 = hashlib.sha256(dedicated_snapshot.read_bytes()).hexdigest()

    with pytest.raises(ReleaseTransitionError, match="isolated|filesystem object"):
        execute_release_transitions(
            data_root,
            protected_root,
            sources,
            query_spec,
            extractor=_extract,
            metadata_parser=_metadata,
        )

    assert hashlib.sha256(dedicated_snapshot.read_bytes()).hexdigest() == snapshot_sha256


def test_isolation_rejects_cross_tree_hardlinks_with_different_names(
    tmp_path: Path,
):
    dedicated = tmp_path / "dedicated"
    protected = tmp_path / "protected"
    dedicated.mkdir()
    protected.mkdir()
    source = dedicated / "active.faiss"
    source.write_bytes(b"snapshot")
    alias = protected / "unrelated-name.bin"
    try:
        os.link(source, alias)
    except OSError as exc:
        pytest.skip(f"hardlinks are unavailable: {exc}")

    with pytest.raises(ReleaseTransitionError, match="filesystem object"):
        release_transitions._assert_isolated_trees(dedicated, protected)
