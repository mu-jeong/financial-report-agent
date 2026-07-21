from __future__ import annotations

import hashlib

import faiss
import numpy as np
import pytest

from src.retrieval.vector_index import (
    RawVectorIndex,
    SnapshotDescriptor,
    VectorIndexError,
    build_index,
    load_index,
)


def _vectors() -> np.ndarray:
    return np.asarray(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 2.0, 0.0],
        ],
        dtype=np.float32,
    )


def _wrapper_results(
    index: RawVectorIndex,
    query: np.ndarray,
    k: int,
    allowed_ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    query_matrix = np.ascontiguousarray(query.reshape(1, -1), dtype=np.float32)
    if allowed_ids is None:
        distances, ids = index._index.search(query_matrix, k)
    else:
        params = faiss.SearchParameters()
        params.sel = faiss.IDSelectorBatch(np.asarray(allowed_ids, dtype=np.int64))
        distances, ids = index._index.search(query_matrix, k, params=params)
    results = [
        (int(physical_id), float(score))
        for physical_id, score in zip(ids[0], distances[0])
        if int(physical_id) > 0 and np.isfinite(score)
    ]
    if index.metric == 'l2':
        return sorted(results, key=lambda item: (item[1], item[0]))
    return sorted(results, key=lambda item: (-item[1], item[0]))


def _result_pairs(index: RawVectorIndex, query: np.ndarray, k: int, allowed_ids=None):
    return [
        (item.physical_id, item.score)
        for item in index.search(query, k=k, allowed_ids=allowed_ids)
    ]


@pytest.mark.parametrize('physical_ids', [[0, 1, 2], [-1, 1, 2], [1, 1, 2], [1, 2, 4]])
def test_build_rejects_non_positive_duplicate_or_non_dense_ids(physical_ids):
    with pytest.raises(VectorIndexError):
        build_index(_vectors(), physical_ids, metric='l2')


def test_build_rejects_non_finite_vectors():
    vectors = _vectors()
    vectors[1, 1] = np.nan

    with pytest.raises(VectorIndexError, match='finite'):
        build_index(vectors, [1, 2, 3], metric='l2')


def test_direct_search_returns_positive_external_ids_and_squared_l2_scores():
    index = build_index(_vectors(), [1, 2, 3], metric='l2')

    results = index.search(np.asarray([0.9, 0.0, 0.0], dtype=np.float32), k=3)

    assert [item.physical_id for item in results] == [2, 1, 3]
    assert [item.score for item in results] == pytest.approx([0.01, 0.81, 4.81])


def test_batch_search_matches_materialized_results_and_is_immutable():
    index = build_index(_vectors(), [1, 2, 3], metric='l2')
    query = np.asarray([0.9, 0.0, 0.0], dtype=np.float32)

    batch = index.search_batch(query, k=3)
    materialized = index.search(query, k=3)

    assert batch.physical_ids.tolist() == [item.physical_id for item in materialized]
    assert batch.scores.tolist() == pytest.approx(
        [item.score for item in materialized]
    )
    with pytest.raises(ValueError, match='read-only'):
        batch.physical_ids[0] = 3
    with pytest.raises(ValueError, match='read-only'):
        batch.scores[0] = 99.0


@pytest.mark.parametrize(
    ('metric', 'query'),
    [
        ('l2', np.asarray([0.9, 0.0, 0.0], dtype=np.float32)),
        ('inner_product', np.asarray([1.0, 0.25, 0.0], dtype=np.float32)),
    ],
)
def test_base_fast_path_matches_id_map_wrapper_without_calling_it(
    metric,
    query,
    monkeypatch,
):
    index = build_index(_vectors(), [1, 2, 3], metric=metric)
    expected = _wrapper_results(index, query, k=3)
    monkeypatch.setattr(
        faiss.IndexIDMap2,
        'search',
        lambda *_args, **_kwargs: pytest.fail('IndexIDMap2.search called'),
    )

    assert _result_pairs(index, query, k=3) == expected


@pytest.mark.parametrize('metric', ['l2', 'inner_product'])
def test_selector_fast_path_translates_external_ids_to_base_ordinals(
    metric,
    monkeypatch,
):
    index = build_index(_vectors(), [1, 2, 3], metric=metric)
    query = np.asarray([0.9, 0.0, 0.0], dtype=np.float32)
    allowed_ids = [1, 3]
    expected = _wrapper_results(index, query, k=3, allowed_ids=allowed_ids)
    monkeypatch.setattr(
        faiss.IndexIDMap2,
        'search',
        lambda *_args, **_kwargs: pytest.fail('IndexIDMap2.search called'),
    )

    assert _result_pairs(index, query, k=3, allowed_ids=allowed_ids) == expected


def test_base_fast_path_preserves_wrapper_choice_and_order_at_tied_k_boundary():
    vectors = np.asarray(
        [
            [1.0, 0.0],
            [-1.0, 0.0],
            [0.0, 1.0],
            [0.0, -1.0],
        ],
        dtype=np.float32,
    )
    query = np.zeros(2, dtype=np.float32)
    index = build_index(vectors, [1, 2, 3, 4], metric='l2')

    expected = _wrapper_results(index, query, k=2)
    actual = _result_pairs(index, query, k=2)

    assert actual == expected
    assert [physical_id for physical_id, _score in actual] == [1, 2]


def test_empty_eligibility_returns_without_calling_faiss(monkeypatch):
    index = build_index(_vectors(), [1, 2, 3], metric='l2')
    monkeypatch.setattr(index, '_search_faiss', lambda *args, **kwargs: pytest.fail('FAISS called'))

    assert index.search(np.asarray([0.0, 0.0, 0.0], dtype=np.float32), k=2, allowed_ids=[]) == []


def test_universal_scope_does_not_materialize_snapshot_ids(monkeypatch):
    index = build_index(_vectors(), [1, 2, 3], metric='l2')
    monkeypatch.setattr(
        RawVectorIndex,
        'physical_ids',
        property(lambda _self: pytest.fail('N-sized ID map materialized')),
    )

    results = index.search(
        np.asarray([0.9, 0.0, 0.0], dtype=np.float32),
        k=2,
        allowed_ids=[3, 1, 2],
    )

    assert [item.physical_id for item in results] == [2, 1]


def test_selector_search_never_returns_an_ineligible_id():
    index = build_index(_vectors(), [1, 2, 3], metric='l2')

    results = index.search(
        np.asarray([0.9, 0.0, 0.0], dtype=np.float32),
        k=3,
        allowed_ids=[1, 3],
    )

    assert [item.physical_id for item in results] == [1, 3]


def test_selector_rejects_ids_outside_the_snapshot():
    index = build_index(_vectors(), [1, 2, 3], metric='l2')

    with pytest.raises(VectorIndexError, match='snapshot'):
        index.search(np.zeros(3, dtype=np.float32), k=1, allowed_ids=[4])


def test_reconstruct_batch_uses_external_ids():
    index = build_index(_vectors(), [1, 2, 3], metric='l2')

    reconstructed = index.reconstruct([3, 1])

    np.testing.assert_array_equal(reconstructed, _vectors()[[2, 0]])


def test_snapshot_round_trip_validates_hash_size_shape_metric_and_ids(tmp_path):
    index = build_index(_vectors(), [1, 2, 3], metric='l2')
    snapshot_path = tmp_path / 'snapshot.faiss'

    descriptor = index.write(snapshot_path)
    loaded = load_index(snapshot_path, descriptor)

    assert descriptor.ntotal == 3
    assert descriptor.dimension == 3
    assert descriptor.metric == 'l2'
    assert descriptor.size_bytes == snapshot_path.stat().st_size
    assert descriptor.sha256 == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    assert loaded.physical_ids == (1, 2, 3)
    np.testing.assert_array_equal(loaded.reconstruct([1, 2, 3]), _vectors())
    assert not (tmp_path / 'index.pkl').exists()


def test_snapshot_write_refuses_to_overwrite_an_existing_file(tmp_path):
    snapshot_path = tmp_path / 'snapshot.faiss'
    snapshot_path.write_bytes(b'existing')
    index = build_index(_vectors(), [1, 2, 3], metric='l2')

    with pytest.raises(FileExistsError):
        index.write(snapshot_path)

    assert snapshot_path.read_bytes() == b'existing'


def test_load_rejects_tampering_before_index_is_used(tmp_path):
    snapshot_path = tmp_path / 'snapshot.faiss'
    index = build_index(_vectors(), [1, 2, 3], metric='l2')
    descriptor = index.write(snapshot_path)
    snapshot_path.write_bytes(snapshot_path.read_bytes() + b'tampered')

    with pytest.raises(VectorIndexError, match='size|hash'):
        load_index(snapshot_path, descriptor)


def test_load_rejects_descriptor_metric_mismatch(tmp_path):
    snapshot_path = tmp_path / 'snapshot.faiss'
    index = build_index(_vectors(), [1, 2, 3], metric='l2')
    descriptor = index.write(snapshot_path)
    wrong = SnapshotDescriptor(
        sha256=descriptor.sha256,
        size_bytes=descriptor.size_bytes,
        dimension=descriptor.dimension,
        metric='inner_product',
        ntotal=descriptor.ntotal,
    )

    with pytest.raises(VectorIndexError, match='metric'):
        load_index(snapshot_path, wrong)


def test_load_rejects_hash_valid_snapshot_containing_nonfinite_vectors(tmp_path):
    base = faiss.IndexFlatL2(2)
    raw = faiss.IndexIDMap2(base)
    raw.add_with_ids(
        np.asarray([[1.0, 0.0], [np.nan, 1.0]], dtype=np.float32),
        np.asarray([1, 2], dtype=np.int64),
    )
    snapshot_path = tmp_path / 'nonfinite.faiss'
    faiss.write_index(raw, str(snapshot_path))
    descriptor = SnapshotDescriptor(
        sha256=hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
        size_bytes=snapshot_path.stat().st_size,
        dimension=2,
        metric='l2',
        ntotal=2,
    )

    with pytest.raises(VectorIndexError, match='finite'):
        load_index(snapshot_path, descriptor)


def test_search_rejects_wrong_dimension_and_non_positive_k():
    index = build_index(_vectors(), [1, 2, 3], metric='l2')

    with pytest.raises(VectorIndexError, match='dimension'):
        index.search(np.zeros(4, dtype=np.float32), k=1)
    with pytest.raises(VectorIndexError, match='positive'):
        index.search(np.zeros(3, dtype=np.float32), k=0)
