'''Exact query-time merge for one immutable base and sparse delta indexes.'''

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from src.retrieval.vector_index import SearchResult, VectorSearchBatch


@dataclass(frozen=True, slots=True)
class CompositeArtifact:
    artifact_id: str
    offset: int
    index: Any
    visible_local_ids: frozenset[int] | None = None
    hidden_local_ids: frozenset[int] = frozenset()

    @property
    def local_total(self) -> int:
        return int(self.index.ntotal)

    @property
    def visible_total(self) -> int:
        return (
            self.local_total - len(self.hidden_local_ids)
            if self.visible_local_ids is None
            else len(self.visible_local_ids)
        )

    def is_visible(self, local_id: int) -> bool:
        if local_id in self.hidden_local_ids:
            return False
        return self.visible_local_ids is None or local_id in self.visible_local_ids

    def contains_virtual_id(self, physical_id: int) -> bool:
        return self.offset < physical_id <= self.offset + self.local_total

    def local_id(self, physical_id: int) -> int:
        return physical_id - self.offset

    def virtual_id(self, local_id: int) -> int:
        return self.offset + local_id


class CompositeVectorIndex:
    '''Expose several local dense indexes as one sparse virtual ID space.'''

    def __init__(self, artifacts: tuple[CompositeArtifact, ...]):
        if not artifacts:
            raise ValueError('composite index requires at least one artifact')
        artifact_ids = [artifact.artifact_id for artifact in artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError('composite artifact IDs must be unique')
        dimensions = {int(artifact.index.dimension) for artifact in artifacts}
        metrics = {str(artifact.index.metric) for artifact in artifacts}
        if len(dimensions) != 1 or len(metrics) != 1:
            raise ValueError('composite artifacts must share dimension and metric')
        expected_offset = 0
        for artifact in artifacts:
            if artifact.offset != expected_offset:
                raise ValueError('composite artifact offsets must be contiguous')
            expected_offset += artifact.local_total
            if artifact.visible_local_ids is not None:
                if any(
                    value <= 0 or value > artifact.local_total
                    for value in artifact.visible_local_ids
                ):
                    raise ValueError('visible local ID is outside its artifact')
            if any(
                value <= 0 or value > artifact.local_total
                for value in artifact.hidden_local_ids
            ):
                raise ValueError('hidden local ID is outside its artifact')
            if (
                artifact.visible_local_ids is not None
                and artifact.visible_local_ids.intersection(artifact.hidden_local_ids)
            ):
                raise ValueError('artifact ID cannot be both visible and hidden')
        self._artifacts = artifacts
        self.dimension = dimensions.pop()
        self.metric = metrics.pop()
        self.ntotal = sum(artifact.visible_total for artifact in artifacts)
        self.virtual_span = expected_offset

    @property
    def physical_ids(self) -> tuple[int, ...]:
        values: list[int] = []
        for artifact in self._artifacts:
            local_ids = (
                (
                    value
                    for value in range(1, artifact.local_total + 1)
                    if value not in artifact.hidden_local_ids
                )
                if artifact.visible_local_ids is None
                else sorted(artifact.visible_local_ids)
            )
            values.extend(artifact.virtual_id(value) for value in local_ids)
        return tuple(values)

    def search(
        self,
        query: np.ndarray,
        k: int,
        allowed_ids=None,
    ) -> list[SearchResult]:
        batch = self.search_batch(query, k, allowed_ids=allowed_ids)
        return [
            SearchResult(int(physical_id), float(score))
            for physical_id, score in zip(batch.physical_ids, batch.scores)
        ]

    def search_batch(
        self,
        query: np.ndarray,
        k: int,
        allowed_ids=None,
    ) -> VectorSearchBatch:
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ValueError('k must be a positive integer')
        explicit = None if allowed_ids is None else tuple(int(value) for value in allowed_ids)
        if explicit is not None and len(set(explicit)) != len(explicit):
            raise ValueError('allowed physical IDs must be unique')
        by_artifact: dict[str, list[int]] = {
            artifact.artifact_id: [] for artifact in self._artifacts
        }
        if explicit is not None:
            for physical_id in explicit:
                artifact = self.artifact_for_virtual_id(physical_id)
                local_id = artifact.local_id(physical_id)
                if artifact.is_visible(local_id):
                    by_artifact[artifact.artifact_id].append(local_id)

        merged: list[tuple[float, str, int, int]] = []
        for artifact in self._artifacts:
            if explicit is not None:
                local_allowed = tuple(by_artifact[artifact.artifact_id])
                if not local_allowed:
                    continue
                fetch_k = min(k, len(local_allowed))
                results = artifact.index.search(
                    query,
                    fetch_k,
                    allowed_ids=local_allowed,
                )
            else:
                if artifact.visible_total == 0:
                    continue
                hidden_count = artifact.local_total - artifact.visible_total
                fetch_k = min(artifact.local_total, k + hidden_count)
                results = artifact.index.search(query, fetch_k, allowed_ids=None)
            for result in results:
                local_id = int(result.physical_id)
                if not artifact.is_visible(local_id):
                    continue
                merged.append(
                    (
                        float(result.score),
                        artifact.artifact_id,
                        local_id,
                        artifact.virtual_id(local_id),
                    )
                )

        if self.metric == 'l2':
            merged.sort(key=lambda item: (item[0], item[3]))
        else:
            merged.sort(key=lambda item: (-item[0], item[3]))
        selected = merged[:k]
        return VectorSearchBatch(
            np.asarray([item[3] for item in selected], dtype=np.int64),
            np.asarray([item[0] for item in selected], dtype=np.float32),
        )

    def reconstruct(self, physical_ids) -> np.ndarray:
        requested = tuple(int(value) for value in physical_ids)
        if len(set(requested)) != len(requested):
            raise ValueError('requested physical IDs must be unique')
        if not requested:
            return np.empty((0, self.dimension), dtype=np.float32)
        positions: dict[str, list[tuple[int, int]]] = {}
        for position, physical_id in enumerate(requested):
            artifact = self.artifact_for_virtual_id(physical_id)
            local_id = artifact.local_id(physical_id)
            if not artifact.is_visible(local_id):
                raise ValueError('requested physical ID is hidden in this revision')
            positions.setdefault(artifact.artifact_id, []).append((position, local_id))
        output = np.empty((len(requested), self.dimension), dtype=np.float32)
        for artifact in self._artifacts:
            artifact_positions = positions.get(artifact.artifact_id)
            if not artifact_positions:
                continue
            vectors = artifact.index.reconstruct(
                [local_id for _position, local_id in artifact_positions]
            )
            for (position, _local_id), vector in zip(
                artifact_positions,
                vectors,
                strict=True,
            ):
                output[position] = vector
        return output

    def artifact_for_virtual_id(self, physical_id: int) -> CompositeArtifact:
        if physical_id <= 0:
            raise ValueError('physical IDs must be positive')
        for artifact in self._artifacts:
            if artifact.contains_virtual_id(physical_id):
                return artifact
        raise ValueError('physical ID is outside this composite revision')


__all__ = ['CompositeArtifact', 'CompositeVectorIndex']
