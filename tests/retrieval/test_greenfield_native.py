from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from src.core import db_manager, embed_pipeline
from src.nodes import vectordb
from src.retrieval.bootstrap import inspect_runtime, reconcile_and_inspect_runtime
from src.retrieval.build_service import (
    CandidateResult,
    execute_full_corpus_successor,
    materialize_candidate,
    prepare_full_corpus_build,
)
from src.retrieval.dispatch import reset_native_dispatchers, resolve_retrieval_dispatch
from src.retrieval.initializer import NativeInitializationError, initialize_empty_native
from src.retrieval.publication import (
    PublicationCoordinator,
    PublicationCrash,
    PublicationRequest,
    read_durable_floors,
)
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from src.retrieval.update_lock import RetrievalUpdateLock
from src.retrieval.writer_lock import NativeWriterLock


class _DeterministicEmbeddings:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [
            [
                1.0 + hashlib.sha256(text.encode("utf-8")).digest()[0] / 255,
                2.0,
                3.0,
            ]
            for text in texts
        ]


def _metadata(_file_name: str) -> dict[str, str]:
    return {
        "report_type": "company",
        "report_date": "2026-08-05",
        "target_name": "Example",
        "title": "Greenfield",
        "broker": "Test",
    }


def _extract(_path: Path, _engine: str) -> str:
    return "alpha beta gamma delta epsilon zeta eta theta"


def test_greenfield_inspection_is_read_only_and_reconcile_publishes_exact_empty(
    tmp_path: Path,
) -> None:
    before = set(tmp_path.rglob("*"))

    uninitialized = inspect_runtime(tmp_path)

    assert uninitialized.mode == "uninitialized"
    assert uninitialized.initialization_state == "uninitialized"
    assert set(tmp_path.rglob("*")) == before

    empty = reconcile_and_inspect_runtime(tmp_path)
    again = reconcile_and_inspect_runtime(tmp_path)

    assert empty == again
    assert empty.mode == "native"
    assert empty.initialization_state == "empty"
    assert (
        empty.active_snapshot_id,
        empty.active_build_id,
        empty.predecessor_snapshot_id,
        empty.publication_generation,
        empty.write_epoch,
        empty.degraded,
        empty.write_enabled,
    ) == (None, None, None, 0, 0, False, False)


@pytest.mark.parametrize("boundary", ["schema_committed", "catalog_replaced"])
def test_initializer_crash_boundaries_are_retryable(
    tmp_path: Path,
    boundary: str,
) -> None:
    with RetrievalUpdateLock(tmp_path):
        with NativeWriterLock(tmp_path) as lease:
            with pytest.raises(NativeInitializationError, match=boundary):
                initialize_empty_native(
                    tmp_path,
                    writer_lease=lease,
                    crash_after=boundary,
                )

    if boundary == "schema_committed":
        assert not (tmp_path / "retrieval" / "v2" / "catalog.sqlite3").exists()
    assert not tuple((tmp_path / "retrieval" / "v2").glob("catalog.empty.*"))
    first = reconcile_and_inspect_runtime(tmp_path)
    second = reconcile_and_inspect_runtime(tmp_path)
    assert first == second
    assert first.initialization_state == "empty"
    assert (
        first.active_snapshot_id,
        first.publication_generation,
        first.write_epoch,
        first.write_enabled,
    ) == (None, 0, 0, False)


def test_greenfield_reconcile_removes_abandoned_empty_initializer_sidecars(
    tmp_path: Path,
) -> None:
    v2_root = tmp_path / "retrieval" / "v2"
    v2_root.mkdir(parents=True)
    abandoned = v2_root / f"catalog.empty.{'a' * 32}.tmp"
    for path in (abandoned, Path(f"{abandoned}-wal"), Path(f"{abandoned}-shm")):
        path.touch()

    selection = reconcile_and_inspect_runtime(tmp_path)

    assert selection.initialization_state == "empty"
    assert not tuple(v2_root.glob("catalog.empty.*"))
    assert (
        selection.active_snapshot_id,
        selection.publication_generation,
        selection.write_epoch,
        selection.write_enabled,
    ) == (None, 0, 0, False)


def test_empty_native_queries_do_not_initialize_embeddings(tmp_path: Path, monkeypatch) -> None:
    reconcile_and_inspect_runtime(tmp_path)
    monkeypatch.setattr(db_manager, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(vectordb, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        vectordb,
        "build_embeddings_fn",
        lambda: pytest.fail("empty vector query initialized embeddings"),
    )

    with closing(db_manager.get_connection()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0

    dispatch = resolve_retrieval_dispatch(tmp_path)
    assert dispatch.mode == "native"
    assert dispatch.native is None
    assert dispatch.selection is not None and dispatch.selection.is_empty
    docs_with_scores, metrics = vectordb._retrieve_docs_with_scores("query", None)

    assert docs_with_scores == []
    assert metrics["runtime_mode"] == "native"
    assert metrics["native_search_strategy"] == "empty"
    assert metrics["native_candidate_count"] == 0
    assert metrics["native_snapshot_total"] == 0
    assert metrics["native_faiss_calls"] == 0
    assert metrics["snapshot_id"] is None
    assert metrics["publication_generation"] == 0
    reset_native_dispatchers(tmp_path)


def test_empty_native_update_is_a_noop_before_embedding_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "downloaded"
    source_root.mkdir()
    reconcile_and_inspect_runtime(tmp_path)
    monkeypatch.setattr(embed_pipeline.config, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(embed_pipeline.config, "SAVE_DIR", str(source_root))
    monkeypatch.setattr(
        embed_pipeline,
        "build_embeddings_fn",
        lambda: pytest.fail("empty update initialized embeddings"),
    )

    assert embed_pipeline._run_pipeline_locked() == 0
    assert inspect_runtime(tmp_path).initialization_state == "empty"


def test_first_pdf_publishes_greenfield_generation_and_epoch_one(tmp_path: Path) -> None:
    source_root = tmp_path / "downloaded"
    source_root.mkdir()
    (source_root / "report.pdf").write_bytes(b"%PDF-test")
    reconcile_and_inspect_runtime(tmp_path)
    embeddings = _DeterministicEmbeddings()

    result, outcome = execute_full_corpus_successor(
        tmp_path,
        source_root,
        embeddings=embeddings,
        model="deterministic-model",
        extractor_name="deterministic-extractor",
        parent_chunk_size=40,
        child_chunk_size=20,
        extractor=_extract,
        metadata_parser=_metadata,
    )

    selection = inspect_runtime(tmp_path)
    assert outcome.publication_generation == 1
    assert outcome.write_epoch == 1
    assert outcome.predecessor_snapshot_id is None
    assert selection.initialization_state == "ready"
    assert selection.active_snapshot_id == result.snapshot_id
    assert selection.publication_generation == 1
    assert selection.write_epoch == 1
    assert not selection.degraded
    assert selection.write_enabled
    assert len(embeddings.calls) == 1
    with closing(sqlite3.connect(selection.paths.catalog)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM active_reports").fetchone()[0] == 1


def _materialize_first_candidate(tmp_path: Path) -> CandidateResult:
    source_root = tmp_path / "downloaded"
    source_root.mkdir()
    (source_root / "report.pdf").write_bytes(b"%PDF-test")
    reconcile_and_inspect_runtime(tmp_path)
    with NativeWriterLock(tmp_path) as lease:
        plan = prepare_full_corpus_build(
            tmp_path,
            source_root,
            embeddings=_DeterministicEmbeddings(),
            model="deterministic-model",
            extractor_name="deterministic-extractor",
            parent_chunk_size=40,
            child_chunk_size=20,
            extractor=_extract,
            metadata_parser=_metadata,
            allow_degraded_forward_recovery=True,
            writer_lease=lease,
        )
        assert plan is not None
        result = materialize_candidate(plan, tmp_path, writer_lease=lease)
    return result


@pytest.mark.parametrize(
    "boundary",
    [
        "journal_created",
        "artifact_validated",
        "rollback_backup_validated",
        "commit_intent_written",
        "commit_intent_durable",
        "before_pointer_transaction",
        "during_pointer_transaction",
        "committed_pending_checkpoint",
        "checkpoint_created",
        "checkpoint_validated",
        "committed_floor_durable",
        "before_fully_complete",
        "fully_complete",
    ],
)
def test_first_publication_restart_is_idempotent_across_crash_boundaries(
    tmp_path: Path,
    boundary: str,
) -> None:
    result = _materialize_first_candidate(tmp_path)
    request = PublicationRequest(
        publication_id=result.publication_id,
        to_snapshot_id=result.snapshot_id,
        evidence_manifest_relative_path=result.evidence_manifest_relative_path,
        evidence_manifest_sha256=result.evidence_manifest_sha256,
        increment_write_epoch=True,
        enable_writes_on_complete=True,
    )

    with pytest.raises(PublicationCrash) as crashed:
        PublicationCoordinator(tmp_path).publish(request, crash_after=boundary)
    assert crashed.value.boundary == boundary

    first = StartupReconciler(tmp_path).reconcile()
    second = StartupReconciler(tmp_path).reconcile()

    assert first.disposition in {
        RecoveryDisposition.PUBLICATION_COMPLETED,
        RecoveryDisposition.ACTIVE,
    }
    assert second.disposition is RecoveryDisposition.ACTIVE
    selection = inspect_runtime(tmp_path)
    assert (
        selection.active_snapshot_id,
        selection.predecessor_snapshot_id,
        selection.publication_generation,
        selection.write_epoch,
        selection.degraded,
        selection.write_enabled,
    ) == (result.snapshot_id, None, 1, 1, False, True)
    floors = read_durable_floors(tmp_path)
    assert len(floors) == 1
    assert (
        floors[0].publication_generation,
        floors[0].write_epoch,
        floors[0].active_snapshot_id,
    ) == (1, 1, result.snapshot_id)
    with closing(sqlite3.connect(selection.paths.catalog)) as connection:
        publications = connection.execute(
            """
            SELECT from_snapshot_id, to_snapshot_id, phase, state
            FROM publication_runs
            """
        ).fetchall()
        assert publications == [
            (None, result.snapshot_id, "fully_complete", "fully_complete")
        ]
