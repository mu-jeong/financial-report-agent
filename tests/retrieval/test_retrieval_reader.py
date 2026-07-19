from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.retrieval.reader import (
    NativeRetrievalReader,
    SearchStrategy,
)
from src.retrieval.repository import (
    CatalogRepository,
    SnapshotCache,
    SnapshotSession,
)
from src.retrieval.vector_index import SearchResult, VectorSearchBatch
from tests.retrieval.test_retrieval_repository import _create_catalog


class RecordingIndex:
    dimension = 2
    metric = 'l2'

    def __init__(self, ranked_ids: list[int]):
        self.ranked_ids = ranked_ids
        self.ntotal = len(ranked_ids)
        self.calls: list[tuple[int, tuple[int, ...] | None]] = []
        self.load_count = 0

    def search(self, _query, k, allowed_ids=None):
        allowed = None if allowed_ids is None else tuple(allowed_ids)
        self.calls.append((k, allowed))
        candidates = self.ranked_ids
        if allowed is not None:
            allowed_set = set(allowed)
            candidates = [value for value in candidates if value in allowed_set]
        return [
            SearchResult(physical_id=value, score=float(rank))
            for rank, value in enumerate(candidates[:k])
        ]

    def search_batch(self, query, k, allowed_ids=None):
        results = self.search(query, k, allowed_ids=allowed_ids)
        return VectorSearchBatch(
            np.asarray([result.physical_id for result in results], dtype=np.int64),
            np.asarray([result.score for result in results], dtype=np.float32),
        )


def _reader_with_fake_index(
    root: Path,
    index: RecordingIndex,
    **reader_options,
):
    catalog_path, rows = _create_catalog(root, count=index.ntotal)

    def load(_path, _descriptor):
        index.load_count += 1
        return index

    cache = SnapshotCache(loader=load)
    repository = CatalogRepository(
        catalog_path,
        data_root=root,
        cache=cache,
        query_batch_size=2,
    )
    return NativeRetrievalReader(repository, **reader_options), repository, cache, rows


def test_empty_scope_returns_without_a_faiss_call(tmp_path):
    index = RecordingIndex([1, 2, 3, 4, 5])
    reader, _, _, _ = _reader_with_fake_index(tmp_path, index)

    response = reader.search(
        np.zeros(2, dtype=np.float32),
        3,
        scope={'file_names': []},
    )

    assert response.strategy is SearchStrategy.EMPTY
    assert response.results == ()
    assert response.faiss_calls == 0
    assert response.timings is not None
    assert response.timings.total_ns >= response.timings.lease_ns > 0
    assert response.timings.faiss_ns == 0
    assert index.load_count == 0
    assert index.calls == []


def test_filtered_universal_scope_uses_direct_search_without_id_materialization(
    tmp_path,
    monkeypatch,
):
    index = RecordingIndex([3, 1, 5, 2, 4])
    reader, _, _, _ = _reader_with_fake_index(tmp_path, index)

    monkeypatch.setattr(
        SnapshotSession,
        'eligible_physical_ids',
        lambda *_args, **_kwargs: pytest.fail('N-sized allowed set allocated'),
    )
    response = reader.search(
        np.zeros(2, dtype=np.float32),
        3,
        scope={
            'report_date_start': '2026-01-01',
            'report_date_end': '2026-12-31',
        },
    )

    assert response.strategy is SearchStrategy.DIRECT
    assert response.eligible_count == response.snapshot_total == 5
    assert index.calls == [(3, None)]
    assert [result.physical_id for result in response.results] == [3, 1, 5]


def test_small_selector_scope_passes_only_eligible_ids_to_faiss(tmp_path):
    index = RecordingIndex([1, 3, 4, 2, 5])
    reader, _, _, _ = _reader_with_fake_index(
        tmp_path,
        index,
        selector_max_ids=10,
        selector_max_fraction=0.5,
    )

    response = reader.search(
        np.zeros(2, dtype=np.float32),
        5,
        scope={'target_name': 'Beta'},
    )

    assert response.strategy is SearchStrategy.SELECTOR
    assert index.calls == [(5, (2, 5))]
    assert [result.physical_id for result in response.results] == [2, 5]
    assert all(result.target_name == 'Beta' for result in response.results)


def test_broad_scope_adaptively_expands_direct_prefix_without_allowed_set(tmp_path):
    # Beta is a broad 7/10 scope.  The artificial FAISS rank puts three Alpha
    # rows first so deterministic 2 -> 4 -> 8 expansion is observable.
    index = RecordingIndex([1, 3, 4, 2, 5, 6, 7, 8, 9, 10])
    reader, _, _, _ = _reader_with_fake_index(
        tmp_path,
        index,
        selector_max_ids=10,
        selector_max_fraction=0.25,
        adaptive_initial_multiplier=1,
        adaptive_growth=2.0,
    )

    response = reader.search(
        np.zeros(2, dtype=np.float32),
        2,
        scope={'target_name': 'Beta'},
    )

    assert response.strategy is SearchStrategy.ADAPTIVE
    assert response.eligible_count == 7
    assert index.calls == [(2, None), (4, None), (8, None)]
    assert response.faiss_fetch_k == 8
    assert [result.physical_id for result in response.results] == [2, 5]
    assert all(result.target_name == 'Beta' for result in response.results)


def test_direct_reader_hydration_preserves_faiss_rank_across_batches(tmp_path):
    index = RecordingIndex([4, 1, 3, 5, 2])
    reader, _, _, rows = _reader_with_fake_index(tmp_path, index)

    response = reader.search(np.zeros(2, dtype=np.float32), 3)

    assert response.strategy is SearchStrategy.DIRECT
    assert [result.physical_id for result in response.results] == [4, 1, 3]
    with pytest.raises(AttributeError):
        response.strategy = SearchStrategy.EMPTY
    assert [result.rank for result in response.results] == [1, 2, 3]
    assert [result.parent_slice for result in response.results] == [
        rows[3]['body'],
        rows[0]['body'],
        rows[2]['body'],
    ]
    assert response.hydration_batches == 2


def test_explicit_path_scope_hydrates_only_that_native_report(tmp_path):
    index = RecordingIndex([5, 4, 3, 2, 1])
    reader, _, _, rows = _reader_with_fake_index(
        tmp_path,
        index,
        selector_max_fraction=0.5,
    )

    response = reader.search(
        np.zeros(2, dtype=np.float32),
        3,
        scope={'canonical_relative_path': rows[2]['path']},
    )

    assert response.strategy is SearchStrategy.SELECTOR
    assert [result.physical_id for result in response.results] == [3]
    assert response.results[0].canonical_relative_path == rows[2]['path']
    assert response.results[0].chunk_uid
    assert response.results[0].parent_uid
    assert response.results[0].report_uid


def test_prior_file_scope_uses_canonical_path_membership(tmp_path):
    index = RecordingIndex([5, 4, 3, 2, 1])
    reader, _, _, rows = _reader_with_fake_index(
        tmp_path,
        index,
        selector_max_fraction=0.5,
    )

    response = reader.search(
        np.zeros(2, dtype=np.float32),
        3,
        scope={'prior_scope': {'file_names': ['company-2.pdf']}},
    )

    assert response.strategy is SearchStrategy.SELECTOR
    assert [result.physical_id for result in response.results] == [2]
    assert response.results[0].canonical_relative_path == rows[1]['path']


def test_native_reader_uses_real_raw_faiss_snapshot_end_to_end(tmp_path):
    catalog_path, rows = _create_catalog(tmp_path, count=5)
    repository = CatalogRepository(catalog_path, data_root=tmp_path)
    reader = NativeRetrievalReader(
        repository,
        selector_max_ids=10,
        selector_max_fraction=0.75,
    )

    response = reader.search(
        np.asarray([0.0, 5.0], dtype=np.float32),
        3,
        scope={'report_type': 'company'},
    )

    assert response.strategy is SearchStrategy.SELECTOR
    assert [result.physical_id for result in response.results] == [1, 2, 3]
    assert [result.parent_slice for result in response.results] == [
        rows[0]['body'],
        rows[1]['body'],
        rows[2]['body'],
    ]
    assert all(result.report_type == 'company' for result in response.results)


def test_reader_releases_snapshot_lease_when_faiss_raises(tmp_path):
    class FailingIndex(RecordingIndex):
        def search(self, _query, k, allowed_ids=None):
            raise TimeoutError('synthetic timeout')

    index = FailingIndex([1, 2, 3, 4, 5])
    reader, _, cache, _ = _reader_with_fake_index(tmp_path, index)

    with pytest.raises(TimeoutError, match='synthetic'):
        reader.search(np.zeros(2, dtype=np.float32), 2)

    revisions = cache.cached_revisions()
    assert len(revisions) == 1
    assert cache.lease_count(revisions[0]) == 0
