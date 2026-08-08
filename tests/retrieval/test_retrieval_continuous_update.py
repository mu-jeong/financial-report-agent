from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from src.retrieval.bootstrap import inspect_runtime
from src.retrieval.build_service import NativeBuildError, execute_incremental_update
from src.retrieval.continuous_update import execute_continuous_update
from src.retrieval.reader import NativeRetrievalReader
from src.retrieval.repository import CatalogRepository, SnapshotCache
from tests.retrieval.native_build_fixtures import (
    PREFIX,
    DeterministicEmbeddings,
    _extract,
    _metadata,
    _native_profile,
    _native_seed,
)

def _metadata_with_new_reports(file_name: str):
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


def _extract_with_new_reports(path: Path, engine: str) -> str:
    if path.name in {"a.pdf", "b.pdf"}:
        return _extract(path, engine)
    return f"{path.stem} newly searchable report content"


def _options(embeddings: DeterministicEmbeddings) -> dict[str, object]:
    return {
        "embeddings": embeddings,
        "model": "model-a",
        "extractor_name": "deterministic-extractor",
        "extractor": _extract_with_new_reports,
        "metadata_parser": _metadata_with_new_reports,
        "allow_extraction_fallback": False,
        "use_parent_child": True,
        "parent_chunk_size": 2000,
        "child_chunk_size": 500,
        "metric": "l2",
        "normalization": "none",
    }


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    data_root, sources = _native_seed(
        tmp_path,
        seed_matches_current_source=True,
        profile=_native_profile(),
    )
    (sources / "b.pdf").write_bytes(b"baseline-change")
    return data_root, sources


def _runtime_revision(data_root: Path) -> tuple[str, int, int]:
    selection = inspect_runtime(
        data_root,
        validate_snapshot=True,
    )
    return (
        selection.active_snapshot_id or "",
        selection.publication_generation,
        selection.write_epoch,
    )


def _active_report_uids(data_root: Path) -> dict[str, str]:
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        return dict(
            connection.execute(
                "SELECT canonical_relative_path, report_uid FROM active_reports"
            )
        )


def _vector_visible_report_uids(data_root: Path) -> set[str]:
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    reader = NativeRetrievalReader(CatalogRepository(catalog, data_root=data_root))
    response = reader.search(np.zeros(3, dtype=np.float32), 100)
    return {result.report_uid for result in response.results}


def test_continuous_update_publishes_batches_then_compacts_once(tmp_path: Path) -> None:
    data_root, sources = _seed(tmp_path)
    (sources / "c.pdf").write_bytes(b"new-c")
    (sources / "d.pdf").write_bytes(b"new-d")
    before_snapshot, before_generation, before_epoch = _runtime_revision(data_root)
    embeddings = DeterministicEmbeddings()
    callback_states: list[tuple[int, list[list[str]]]] = []

    result = execute_continuous_update(
        data_root,
        sources,
        batch_size=2,
        progress_callback=lambda publication: callback_states.append(
            (publication.sequence, [list(call) for call in embeddings.calls])
        ),
        **_options(embeddings),
    )

    assert result is not None
    assert [publication.sequence for publication in result.delta_publications] == [1, 2]
    assert [state[0] for state in callback_states] == [1, 2]
    assert [len(state[1]) for state in callback_states] == [2, 3]
    assert embeddings.calls == callback_states[-1][1]
    assert embeddings.calls[1:] == [
        [
            PREFIX.format(target_name="C", title="C update")
            + "c newly searchable report content",
            PREFIX.format(target_name="Sector", title="Outlook")
            + "sector outlook newly searchable content",
        ],
        [
            PREFIX.format(target_name="D", title="D update")
            + "d newly searchable report content"
        ],
    ]
    assert result.publication_outcome.publication_generation == before_generation + 1
    assert result.publication_outcome.write_epoch == before_epoch + 1
    assert result.publication_outcome.active_snapshot_id != before_snapshot

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        ready_for_old_base = connection.execute(
            """
            SELECT COUNT(*) FROM retrieval_delta_segments
            WHERE base_snapshot_id = ?
              AND base_publication_generation = ?
              AND state = 'ready'
            """,
            (before_snapshot, before_generation),
        ).fetchone()[0]
    active = _active_report_uids(data_root)
    visible = _vector_visible_report_uids(data_root)
    published = {
        uid
        for publication in result.delta_publications
        for uid in publication.published_report_uids
    }

    assert ready_for_old_base == 0
    assert set(active) == {
        "downloaded/a.pdf",
        "downloaded/b.pdf",
        "downloaded/c.pdf",
        "downloaded/d.pdf",
    }
    assert visible == set(active.values())
    assert published <= visible


@pytest.mark.slow
def test_failure_is_durable_retains_old_version_and_is_not_retried(
    tmp_path: Path,
) -> None:
    data_root, sources = _seed(tmp_path)
    baseline = execute_continuous_update(
        data_root,
        sources,
        **_options(DeterministicEmbeddings()),
    )
    assert baseline is not None
    old_b_uid = _active_report_uids(data_root)["downloaded/b.pdf"]

    (sources / "b.pdf").write_bytes(b"changed-b")
    (sources / "c.pdf").write_bytes(b"new-c")
    extracted: list[str] = []

    def fail_b(path: Path, engine: str) -> str:
        extracted.append(path.name)
        if path.name == "b.pdf":
            raise RuntimeError("deterministic extraction failure")
        return _extract_with_new_reports(path, engine)

    embeddings = DeterministicEmbeddings()
    options = _options(embeddings)
    options["extractor"] = fail_b
    result = execute_continuous_update(
        data_root,
        sources,
        batch_size=2,
        **options,
    )

    assert result is not None
    assert extracted == ["b.pdf", "c.pdf"]
    assert len(result.failed_report_uids) == 1
    active = _active_report_uids(data_root)
    assert active["downloaded/b.pdf"] == old_b_uid
    assert "downloaded/c.pdf" in active
    assert _vector_visible_report_uids(data_root) == set(active.values())

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        manifest = json.loads(
            connection.execute(
                """
                SELECT source_manifest_json FROM retrieval_builds
                WHERE build_id = (
                    SELECT active_build_id FROM retrieval_runtime WHERE runtime_id = 1
                )
                """
            ).fetchone()[0]
        )
    failed_entries = [
        entry
        for entry in manifest["reports"]
        if entry["reason_code"] == "source-extraction-failed"
    ]
    assert {entry["report_uid"] for entry in failed_entries} == set(
        result.failed_report_uids
    )

    before_noop = _runtime_revision(data_root)
    extracted.clear()
    noop_embeddings = DeterministicEmbeddings()
    noop_options = _options(noop_embeddings)
    noop_options["extractor"] = fail_b
    assert execute_continuous_update(
        data_root,
        sources,
        **noop_options,
    ) is None
    assert extracted == []
    assert noop_embeddings.calls == []
    assert _runtime_revision(data_root) == before_noop


def test_restart_compacts_a_ready_delta_without_pending_source_changes(
    tmp_path: Path,
) -> None:
    data_root, sources = _seed(tmp_path)
    before = _runtime_revision(data_root)
    first_embeddings = DeterministicEmbeddings()

    def interrupt_after_delta(_publication) -> None:
        raise RuntimeError("simulate process interruption after delta publication")

    with pytest.raises(RuntimeError, match="simulate process interruption"):
        execute_continuous_update(
            data_root,
            sources,
            progress_callback=interrupt_after_delta,
            **_options(first_embeddings),
        )

    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        ready_before_restart = connection.execute(
            """
            SELECT COUNT(*) FROM retrieval_delta_segments
            WHERE base_snapshot_id = ?
              AND base_publication_generation = ?
              AND state = 'ready'
            """,
            (before[0], before[1]),
        ).fetchone()[0]
    assert ready_before_restart == 1
    assert _runtime_revision(data_root) == before

    restart_embeddings = DeterministicEmbeddings()
    result = execute_continuous_update(
        data_root,
        sources,
        **_options(restart_embeddings),
    )

    assert result is not None
    assert result.delta_publications == ()
    assert result.attempted_report_uids == ()
    assert restart_embeddings.calls == []
    assert result.publication_outcome.publication_generation == before[1] + 1
    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*) FROM retrieval_delta_segments
            WHERE base_snapshot_id = ?
              AND base_publication_generation = ?
              AND state = 'ready'
            """,
            (before[0], before[1]),
        ).fetchone()[0] == 0
    assert _vector_visible_report_uids(data_root) == set(
        _active_report_uids(data_root).values()
    )
def test_full_corpus_writer_rejects_an_active_delta_chain(tmp_path: Path) -> None:
    data_root, sources = _seed(tmp_path)

    def interrupt_after_delta(_publication) -> None:
        raise RuntimeError("simulate process interruption after delta publication")

    with pytest.raises(RuntimeError, match="simulate process interruption"):
        execute_continuous_update(
            data_root,
            sources,
            progress_callback=interrupt_after_delta,
            **_options(DeterministicEmbeddings()),
        )

    with pytest.raises(NativeBuildError, match="use execute_continuous_update"):
        execute_incremental_update(
            data_root,
            sources,
            **_options(DeterministicEmbeddings()),
        )


def test_open_composite_request_survives_final_compaction(tmp_path: Path) -> None:
    data_root, sources = _seed(tmp_path)
    before = _runtime_revision(data_root)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    repository = CatalogRepository(
        catalog,
        data_root=data_root,
        cache=SnapshotCache(),
    )
    request = repository.request()
    pinned = None
    visible_during_delta: set[str] = set()

    def pin_composite(_publication) -> None:
        nonlocal pinned
        pinned = request.__enter__()

    try:
        result = execute_continuous_update(
            data_root,
            sources,
            progress_callback=pin_composite,
            **_options(DeterministicEmbeddings()),
        )

        assert result is not None
        assert pinned is not None
        assert pinned.revision.publication_generation == before[1]
        assert pinned.revision.delta_generation == 1
        assert pinned.revision.delta_segment_count == 1
        candidates = pinned.search_index(np.zeros(3, dtype=np.float32), 100)
        visible_during_delta = {
            hit.report_uid for hit in pinned.hydrate_search_batch(candidates)
        }

        current = NativeRetrievalReader(repository).search(
            np.zeros(3, dtype=np.float32),
            100,
        )
        assert current.revision.publication_generation == before[1] + 1
        assert current.revision.delta_generation == 0
        assert (
            current.revision.snapshot_id
            == result.publication_outcome.active_snapshot_id
        )
        assert {hit.report_uid for hit in current.results} == visible_during_delta
        old_segment_id = result.delta_publications[0].segment_id
        assert any(
            revision.snapshot_id == old_segment_id
            for revision in repository.cache.cached_revisions()
        )

        request.__exit__(None, None, None)
        pinned = None
        NativeRetrievalReader(repository).search(
            np.zeros(3, dtype=np.float32),
            100,
        )
        assert all(
            revision.snapshot_id != old_segment_id
            for revision in repository.cache.cached_revisions()
        )
    finally:
        if pinned is not None:
            request.__exit__(None, None, None)
        repository.close()


def test_independent_cache_pinned_composite_survives_next_base_gc(
    tmp_path: Path,
) -> None:
    data_root, sources = _seed(tmp_path)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    repository = CatalogRepository(
        catalog,
        data_root=data_root,
        cache=SnapshotCache(),
    )
    request = repository.request()
    pinned = None

    def pin_without_searching(_publication) -> None:
        nonlocal pinned
        pinned = request.__enter__()

    try:
        first = execute_continuous_update(
            data_root,
            sources,
            progress_callback=pin_without_searching,
            **_options(DeterministicEmbeddings()),
        )
        assert first is not None
        assert pinned is not None
        expected_report_uids = set(_active_report_uids(data_root).values())
        first_delta_paths = [
            data_root
            / "retrieval"
            / "v2"
            / "deltas"
            / f"{publication.segment_id}.faiss"
            for publication in first.delta_publications
            if publication.descriptor.ntotal > 0
        ]
        assert first_delta_paths
        assert all(path.is_file() for path in first_delta_paths)

        (sources / "z.pdf").write_bytes(b"new-z")
        second = execute_continuous_update(
            data_root,
            sources,
            **_options(DeterministicEmbeddings()),
        )

        assert second is not None
        assert all(not path.exists() for path in first_delta_paths)
        candidates = pinned.search_index(np.zeros(3, dtype=np.float32), 100)
        assert {
            hit.report_uid
            for hit in pinned.hydrate_search_batch(candidates)
        } == expected_report_uids
    finally:
        if pinned is not None:
            request.__exit__(None, None, None)
        repository.close()


def test_independent_cache_pinned_base_survives_two_publications(
    tmp_path: Path,
) -> None:
    data_root, sources = _seed(tmp_path)
    expected_report_uids = set(_active_report_uids(data_root).values())
    old_snapshot_id = _runtime_revision(data_root)[0]
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        old_relative_path = connection.execute(
            "SELECT relative_path FROM vector_snapshots WHERE snapshot_id = ?",
            (old_snapshot_id,),
        ).fetchone()[0]
    old_snapshot_path = data_root.joinpath(*str(old_relative_path).split("/"))

    repository = CatalogRepository(
        catalog,
        data_root=data_root,
        cache=SnapshotCache(),
    )
    request = repository.request()
    pinned = request.__enter__()
    try:
        assert pinned.revision.delta_segment_count == 0

        first = execute_continuous_update(
            data_root,
            sources,
            **_options(DeterministicEmbeddings()),
        )
        assert first is not None

        (sources / "z.pdf").write_bytes(b"new-z")
        second = execute_continuous_update(
            data_root,
            sources,
            **_options(DeterministicEmbeddings()),
        )

        assert second is not None
        assert not old_snapshot_path.exists()
        candidates = pinned.search_index(np.zeros(3, dtype=np.float32), 100)
        assert {
            hit.report_uid
            for hit in pinned.hydrate_search_batch(candidates)
        } == expected_report_uids
    finally:
        request.__exit__(None, None, None)
        repository.close()


@pytest.mark.slow
def test_delete_only_composite_eagerly_pins_base_across_next_gc(
    tmp_path: Path,
) -> None:
    data_root, sources = _seed(tmp_path)
    baseline = execute_continuous_update(
        data_root,
        sources,
        **_options(DeterministicEmbeddings()),
    )
    assert baseline is not None
    active_reports = _active_report_uids(data_root)
    assert {"downloaded/a.pdf", "downloaded/b.pdf"} <= set(active_reports)
    expected_report_uids = {active_reports["downloaded/a.pdf"]}
    old_snapshot_id = _runtime_revision(data_root)[0]
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        old_relative_path = connection.execute(
            "SELECT relative_path FROM vector_snapshots WHERE snapshot_id = ?",
            (old_snapshot_id,),
        ).fetchone()[0]
    old_snapshot_path = data_root.joinpath(*str(old_relative_path).split("/"))
    (sources / "b.pdf").unlink()

    repository = CatalogRepository(
        catalog,
        data_root=data_root,
        cache=SnapshotCache(),
    )
    request = repository.request()
    pinned = None

    def pin_delete_only_revision(publication) -> None:
        nonlocal pinned
        assert publication.descriptor.ntotal == 0
        pinned = request.__enter__()

    try:
        first = execute_continuous_update(
            data_root,
            sources,
            deleted_relative_paths=("downloaded/b.pdf",),
            progress_callback=pin_delete_only_revision,
            **_options(DeterministicEmbeddings()),
        )
        assert first is not None
        assert pinned is not None
        assert pinned.revision.delta_segment_count == 1
        assert pinned.total_count > 0

        (sources / "z.pdf").write_bytes(b"new-z")
        second = execute_continuous_update(
            data_root,
            sources,
            **_options(DeterministicEmbeddings()),
        )

        assert second is not None
        assert not old_snapshot_path.exists()
        candidates = pinned.search_index(np.zeros(3, dtype=np.float32), 100)
        assert {
            hit.report_uid
            for hit in pinned.hydrate_search_batch(candidates)
        } == expected_report_uids
    finally:
        if pinned is not None:
            request.__exit__(None, None, None)
        repository.close()


def test_compacted_delta_artifact_is_retained_until_base_gc_boundary(
    tmp_path: Path,
) -> None:
    data_root, sources = _seed(tmp_path)
    before = _runtime_revision(data_root)
    result = execute_continuous_update(
        data_root,
        sources,
        **_options(DeterministicEmbeddings()),
    )

    assert result is not None
    assert _runtime_revision(data_root)[1:] == (before[1] + 1, before[2] + 1)
    assert (
        result.publication_outcome.active_snapshot_id
        == _runtime_revision(data_root)[0]
    )
    delta_paths = [
        data_root / "retrieval" / "v2" / "deltas" / f"{publication.segment_id}.faiss"
        for publication in result.delta_publications
        if publication.descriptor.ntotal > 0
    ]
    assert delta_paths
    assert all(path.is_file() for path in delta_paths)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM retrieval_delta_segments
            WHERE base_snapshot_id = ?
              AND base_publication_generation = ?
              AND state = 'compacted'
            """,
            (before[0], before[1]),
        ).fetchone()[0] == len(result.delta_publications)
    assert _vector_visible_report_uids(data_root) == set(
        _active_report_uids(data_root).values()
    )
