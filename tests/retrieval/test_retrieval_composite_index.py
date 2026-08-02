from __future__ import annotations

import numpy as np
import pytest

from src.retrieval.composite_index import CompositeArtifact, CompositeVectorIndex
from src.retrieval.vector_index import SearchResult


class FakeIndex:
    dimension = 2

    def __init__(self, scores: list[float], *, metric: str = 'l2'):
        self.metric = metric
        self.scores = scores
        self.ntotal = len(scores)
        self.calls = []

    def search(self, _query, k, allowed_ids=None):
        allowed = set(range(1, self.ntotal + 1)) if allowed_ids is None else set(allowed_ids)
        self.calls.append((k, None if allowed_ids is None else tuple(allowed_ids)))
        ranked = [
            SearchResult(index, score)
            for index, score in enumerate(self.scores, 1)
            if index in allowed
        ]
        ranked.sort(
            key=(
                (lambda item: (item.score, item.physical_id))
                if self.metric == 'l2'
                else (lambda item: (-item.score, item.physical_id))
            )
        )
        return ranked[:k]

    def reconstruct(self, ids):
        return np.asarray([[float(value), float(value * 10)] for value in ids])


def test_composite_search_merges_local_top_k_and_hides_shadowed_rows():
    base = FakeIndex([0.01, 0.02, 0.30, 0.40])
    delta = FakeIndex([0.10, 0.20])
    index = CompositeVectorIndex(
        (
            CompositeArtifact('base', 0, base, hidden_local_ids=frozenset({1, 2})),
            CompositeArtifact('delta', 4, delta),
        )
    )

    results = index.search(np.zeros(2, dtype=np.float32), 3)

    assert [item.physical_id for item in results] == [5, 6, 3]
    assert [item.score for item in results] == pytest.approx([0.10, 0.20, 0.30])
    assert base.calls == [(4, None)]
    assert index.ntotal == 4


def test_composite_selector_splits_virtual_ids_by_artifact():
    base = FakeIndex([0.1, 0.2, 0.3])
    delta = FakeIndex([0.05, 0.4])
    index = CompositeVectorIndex(
        (
            CompositeArtifact('base', 0, base),
            CompositeArtifact('delta', 3, delta),
        )
    )

    results = index.search(
        np.zeros(2, dtype=np.float32),
        5,
        allowed_ids=(2, 4),
    )

    assert [item.physical_id for item in results] == [4, 2]
    assert base.calls == [(1, (2,))]
    assert delta.calls == [(1, (1,))]


def test_composite_inner_product_uses_descending_score_and_reconstructs_virtual_ids():
    base = FakeIndex([0.7, 0.5], metric='inner_product')
    delta = FakeIndex([0.9], metric='inner_product')
    index = CompositeVectorIndex(
        (
            CompositeArtifact('base', 0, base),
            CompositeArtifact('delta', 2, delta),
        )
    )

    assert [item.physical_id for item in index.search(np.zeros(2), 2)] == [3, 1]
    assert index.reconstruct([3, 2]).tolist() == [[1.0, 10.0], [2.0, 20.0]]


def test_composite_rejects_duplicate_artifact_ids():
    with pytest.raises(ValueError, match='artifact IDs must be unique'):
        CompositeVectorIndex(
            (
                CompositeArtifact('duplicate', 0, FakeIndex([0.1])),
                CompositeArtifact('duplicate', 1, FakeIndex([0.2])),
            )
        )
