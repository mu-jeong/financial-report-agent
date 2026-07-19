'''Raw FAISS snapshot port for V2 retrieval.

The V2 reader owns only FAISS vectors and positive snapshot-local IDs. Text,
metadata, and logical identity remain in SQLite; this module never reads or
writes a LangChain ``index.pkl`` sidecar.
'''

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import faiss
import numpy as np


SUPPORTED_METRICS = {'l2', 'inner_product'}


class VectorIndexError(ValueError):
    '''Raised when a vector snapshot violates the native V2 contract.'''


@dataclass(frozen=True, slots=True)
class SearchResult:
    physical_id: int
    score: float


@dataclass(frozen=True, slots=True)
class VectorSearchBatch:
    '''Immutable columnar FAISS results for the native reader hot path.'''

    physical_ids: np.ndarray
    scores: np.ndarray

    def __post_init__(self) -> None:
        if (
            self.physical_ids.ndim != 1
            or self.scores.ndim != 1
            or len(self.physical_ids) != len(self.scores)
        ):
            raise VectorIndexError('search batch columns are invalid')
        self.physical_ids.setflags(write=False)
        self.scores.setflags(write=False)

    def __len__(self) -> int:
        return len(self.physical_ids)


@dataclass(frozen=True, slots=True)
class SnapshotDescriptor:
    sha256: str
    size_bytes: int
    dimension: int
    metric: str
    ntotal: int


class RawVectorIndex:
    '''Validated ``IndexIDMap2`` with deterministic positive external IDs.'''

    def __init__(self, index: faiss.IndexIDMap2, metric: str):
        self._index = index
        self.metric = _normalize_metric(metric)
        self._validate_index()
        # ``IndexIDMap2.search`` adds substantial dispatch overhead for the
        # dense 1..N identity map required by V2.  Search the owned base index
        # directly and translate its zero-based ordinals back to physical IDs.
        # The wrapper remains authoritative for validation, reconstruction,
        # and snapshot serialization.
        self._base_index = faiss.downcast_index(self._index.index)

    @property
    def dimension(self) -> int:
        return int(self._index.d)

    @property
    def ntotal(self) -> int:
        return int(self._index.ntotal)

    @property
    def physical_ids(self) -> tuple[int, ...]:
        values = faiss.vector_to_array(self._index.id_map)
        return tuple(int(value) for value in values.tolist())

    def search(
        self,
        query: np.ndarray,
        k: int,
        allowed_ids: Iterable[int] | None = None,
    ) -> list[SearchResult]:
        batch = self.search_batch(query, k, allowed_ids=allowed_ids)
        return [
            SearchResult(physical_id=int(physical_id), score=float(score))
            for physical_id, score in zip(batch.physical_ids, batch.scores)
        ]

    def search_batch(
        self,
        query: np.ndarray,
        k: int,
        allowed_ids: Iterable[int] | None = None,
    ) -> VectorSearchBatch:
        '''Return ranked IDs and scores without allocating one object per hit.'''

        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise VectorIndexError('k must be a positive integer')
        query_matrix = _query_matrix(query, self.dimension)

        params = None
        selector = None
        normalized_allowed: tuple[int, ...] | None = None
        if allowed_ids is not None:
            normalized_allowed = _normalize_allowed_ids(allowed_ids, self.ntotal)
            if not normalized_allowed:
                return VectorSearchBatch(
                    np.empty(0, dtype=np.int64),
                    np.empty(0, dtype=np.float32),
                )
            # A validated unique in-range set of ntotal IDs is necessarily the
            # whole dense 1..N universe.  Avoid materializing the snapshot's N
            # IDs merely to recognize this direct-search case.
            if len(normalized_allowed) != self.ntotal:
                id_array = np.asarray(normalized_allowed, dtype=np.int64) - 1
                selector = faiss.IDSelectorBatch(id_array)
                params = faiss.SearchParameters()
                params.sel = selector

        distances, ordinals = self._search_faiss(query_matrix, k, params=params)
        raw_ids = ordinals[0]
        raw_scores = distances[0]
        valid = (raw_ids >= 0) & np.isfinite(raw_scores)
        physical_ids = np.asarray(raw_ids[valid] + 1, dtype=np.int64)
        scores = np.asarray(raw_scores[valid], dtype=np.float32)
        if len(physical_ids) > 1 and np.any(scores[1:] == scores[:-1]):
            score_key = scores if self.metric == 'l2' else -scores
            order = np.lexsort((physical_ids, score_key))
            physical_ids = physical_ids[order]
            scores = scores[order]
        return VectorSearchBatch(physical_ids, scores)

    def _search_faiss(
        self,
        query_matrix: np.ndarray,
        k: int,
        params: faiss.SearchParameters | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        if params is None:
            return self._base_index.search(query_matrix, k)
        return self._base_index.search(query_matrix, k, params=params)

    def reconstruct(self, physical_ids: Iterable[int]) -> np.ndarray:
        ids = _normalize_requested_ids(physical_ids, self.ntotal)
        if not ids:
            return np.empty((0, self.dimension), dtype=np.float32)
        return np.asarray(
            self._index.reconstruct_batch(np.asarray(ids, dtype=np.int64)),
            dtype=np.float32,
        )

    def write(self, path: str | Path) -> SnapshotDescriptor:
        target = Path(path)
        if target.exists():
            raise FileExistsError(f'snapshot already exists: {target.name}')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
        try:
            with temporary.open('xb') as stream:
                writer = faiss.PyCallbackIOWriter(stream.write)
                faiss.write_index(self._index, writer)
                stream.flush()
                os.fsync(stream.fileno())

            descriptor = SnapshotDescriptor(
                sha256=_sha256_file(temporary),
                size_bytes=temporary.stat().st_size,
                dimension=self.dimension,
                metric=self.metric,
                ntotal=self.ntotal,
            )
            # A ready descriptor is never returned for bytes that have not
            # survived close/reopen and the complete native validation.
            reopened = load_index(temporary, descriptor)
            if reopened.physical_ids != self.physical_ids:
                raise VectorIndexError('reopened snapshot physical IDs changed')

            _publish_without_overwrite(temporary, target)
            _fsync_directory(target.parent)
            return descriptor
        finally:
            if temporary.exists():
                temporary.unlink()

    def _validate_index(self) -> None:
        if not isinstance(self._index, faiss.IndexIDMap2):
            raise VectorIndexError('snapshot must be an IndexIDMap2')
        expected_metric = _faiss_metric(self.metric)
        if int(self._index.metric_type) != expected_metric:
            raise VectorIndexError('FAISS metric does not match descriptor metric')
        if self.dimension <= 0:
            raise VectorIndexError('vector dimension must be positive')

        try:
            self._index.check_consistency()
        except RuntimeError as exc:
            raise VectorIndexError(f'FAISS ID map is inconsistent: {exc}') from exc

        ids = self.physical_ids
        if ids != tuple(range(1, self.ntotal + 1)):
            raise VectorIndexError('physical IDs must be unique dense positive values 1..N')
        _validate_finite_vectors(self._index)


def build_index(
    vectors: np.ndarray,
    physical_ids: Iterable[int],
    metric: str,
) -> RawVectorIndex:
    normalized_metric = _normalize_metric(metric)
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] == 0 or matrix.shape[1] == 0:
        raise VectorIndexError('vectors must be a non-empty two-dimensional matrix')
    if not np.isfinite(matrix).all():
        raise VectorIndexError('vectors must contain only finite values')

    ids = _normalize_build_ids(physical_ids, matrix.shape[0])
    order = np.argsort(np.asarray(ids, dtype=np.int64), kind='stable')
    ordered_vectors = np.ascontiguousarray(matrix[order], dtype=np.float32)
    ordered_ids = np.asarray(ids, dtype=np.int64)[order]

    base = (
        faiss.IndexFlatL2(matrix.shape[1])
        if normalized_metric == 'l2'
        else faiss.IndexFlatIP(matrix.shape[1])
    )
    index = faiss.IndexIDMap2(base)
    index.add_with_ids(ordered_vectors, ordered_ids)
    return RawVectorIndex(index, normalized_metric)


def load_index(path: str | Path, descriptor: SnapshotDescriptor) -> RawVectorIndex:
    source = Path(path)
    if not source.is_file():
        raise VectorIndexError('snapshot file is missing')
    if source.stat().st_size != descriptor.size_bytes:
        raise VectorIndexError('snapshot size does not match descriptor')
    if _sha256_file(source) != descriptor.sha256.lower():
        raise VectorIndexError('snapshot hash does not match descriptor')

    try:
        index = read_faiss_index_file(source)
    except RuntimeError as exc:
        raise VectorIndexError(f'FAISS snapshot cannot be read: {exc}') from exc
    wrapped = RawVectorIndex(index, descriptor.metric)
    if wrapped.dimension != descriptor.dimension:
        raise VectorIndexError('snapshot dimension does not match descriptor')
    if wrapped.ntotal != descriptor.ntotal:
        raise VectorIndexError('snapshot count does not match descriptor')
    return wrapped


def read_faiss_index_file(path: str | Path) -> faiss.Index:
    """Read FAISS bytes through Python so Windows Unicode paths remain valid."""

    source = Path(path)
    try:
        with source.open('rb') as stream:
            reader = faiss.PyCallbackIOReader(stream.read)
            return faiss.read_index(reader)
    except (OSError, RuntimeError) as exc:
        raise VectorIndexError(f'FAISS snapshot cannot be read: {exc}') from exc


def _normalize_metric(metric: str) -> str:
    if metric not in SUPPORTED_METRICS:
        raise VectorIndexError(f'unsupported metric: {metric!r}')
    return metric


def _faiss_metric(metric: str) -> int:
    return faiss.METRIC_L2 if metric == 'l2' else faiss.METRIC_INNER_PRODUCT


def _normalize_build_ids(values: Iterable[int], expected_count: int) -> tuple[int, ...]:
    ids = _integer_ids(values)
    if len(ids) != expected_count:
        raise VectorIndexError('physical ID count must match vector count')
    if sorted(ids) != list(range(1, expected_count + 1)):
        raise VectorIndexError('physical IDs must be unique dense positive values 1..N')
    return ids


def _normalize_allowed_ids(values: Iterable[int], ntotal: int) -> tuple[int, ...]:
    ids = _integer_ids(values)
    if len(ids) != len(set(ids)):
        raise VectorIndexError('allowed physical IDs must be unique')
    if any(value < 1 or value > ntotal for value in ids):
        raise VectorIndexError('allowed physical ID is outside this snapshot')
    return tuple(sorted(ids))


def _normalize_requested_ids(values: Iterable[int], ntotal: int) -> tuple[int, ...]:
    ids = _integer_ids(values)
    if len(ids) != len(set(ids)):
        raise VectorIndexError('requested physical IDs must be unique')
    if any(value < 1 or value > ntotal for value in ids):
        raise VectorIndexError('requested physical ID is outside this snapshot')
    return ids


def _integer_ids(values: Iterable[int]) -> tuple[int, ...]:
    result: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
            raise VectorIndexError('physical IDs must be integers')
        result.append(int(value))
    return tuple(result)


def _query_matrix(query: np.ndarray, dimension: int) -> np.ndarray:
    matrix = np.asarray(query, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2 or matrix.shape != (1, dimension):
        raise VectorIndexError(f'query dimension must be exactly {dimension}')
    if not np.isfinite(matrix).all():
        raise VectorIndexError('query must contain only finite values')
    return np.ascontiguousarray(matrix, dtype=np.float32)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _validate_finite_vectors(index: faiss.IndexIDMap2) -> None:
    ntotal = int(index.ntotal)
    if ntotal == 0:
        return
    ids = faiss.vector_to_array(index.id_map)
    batch_size = min(4096, ntotal)
    for start in range(0, ntotal, batch_size):
        batch_ids = np.ascontiguousarray(ids[start : start + batch_size], dtype=np.int64)
        try:
            vectors = np.asarray(index.reconstruct_batch(batch_ids), dtype=np.float32)
        except RuntimeError as exc:
            raise VectorIndexError(f'snapshot vectors cannot be reconstructed: {exc}') from exc
        if vectors.shape != (len(batch_ids), int(index.d)):
            raise VectorIndexError('snapshot reconstruction returned the wrong shape')
        if not np.isfinite(vectors).all():
            raise VectorIndexError('snapshot vectors must contain only finite values')


def _publish_without_overwrite(temporary: Path, target: Path) -> None:
    """Make complete bytes visible at a unique name without replacing a file."""

    try:
        os.link(temporary, target)
    except FileExistsError:
        raise FileExistsError(f'snapshot already exists: {target.name}') from None
    except OSError as exc:
        # Hard links are the non-overwriting atomic primitive on the supported
        # same-volume local filesystems.  Falling back to replace/overwrite here
        # would violate immutable snapshot publication.
        raise VectorIndexError(f'atomic snapshot publication failed: {exc}') from exc
    temporary.unlink()


def _fsync_directory(path: Path) -> None:
    if os.name == 'nt':
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
