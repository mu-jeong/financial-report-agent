'''Read-only SQLite catalog access and immutable snapshot leases for V2.

The catalog is authoritative for runtime revision, logical identity, content,
and snapshot membership.  A request keeps one SQLite read transaction and one
revision-keyed FAISS lease from eligibility through hydration.
'''

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import quote

from src.retrieval.composite_index import CompositeArtifact, CompositeVectorIndex
from src.retrieval.delta_overlay import (
    ActiveDeltaOverlay,
    DeltaSegmentRecord,
    read_active_delta_overlay,
)
from src.retrieval.vector_index import (
    RawVectorIndex,
    SearchResult,
    SnapshotDescriptor,
    VectorSearchBatch,
    load_index,
)
from src.retrieval.schema import SchemaError, configure_catalog_storage


MAX_SCOPE_VALUES = 400
DEFAULT_QUERY_BATCH_SIZE = 200
DEFAULT_HYDRATION_CACHE_SIZE = 512


class RepositoryError(RuntimeError):
    '''Base error for fail-closed native catalog reads.'''


class ScopeValidationError(ValueError):
    '''Raised when a search scope cannot be compiled without widening it.'''


class SnapshotUnavailableError(RepositoryError):
    '''Raised when no fully complete active revision can be leased.'''


class SnapshotValidationError(RepositoryError):
    '''Raised when catalog membership and snapshot descriptor disagree.'''


class SnapshotInUseError(RepositoryError):
    '''Raised when eviction or close is attempted while a revision is leased.'''


class CrossSnapshotMembershipError(RepositoryError):
    '''Raised when ranked candidates are not from the leased revision.'''


class LeaseReleasedError(RepositoryError):
    '''Raised when a request session is used after its context exits.'''


@dataclass(frozen=True)
class SearchScope:
    '''Typed form of the existing metadata-filter mapping contract.

    Mapping scopes remain accepted so current graph callers do not need to
    construct this class.  Empty tuple defaults mean "not specified"; an
    explicitly empty list in a mapping means an empty result set.
    '''

    target_name: str | None = None
    target_names: tuple[str, ...] = ()
    report_type: str | None = None
    report_types: tuple[str, ...] = ()
    report_date: str | None = None
    report_date_start: str | None = None
    report_date_end: str | None = None
    broker: str | None = None
    brokers: tuple[str, ...] = ()
    file_names: tuple[str, ...] = ()
    canonical_relative_paths: tuple[str, ...] = ()
    prior_file_names: tuple[str, ...] = ()
    empty: bool = False


@dataclass(frozen=True)
class CompiledScope:
    '''Parameterized report predicate reusable across catalog queries.'''

    predicate_sql: str
    parameters: tuple[object, ...]
    is_unfiltered: bool
    is_empty: bool

    @property
    def where_sql(self) -> str:
        return self.predicate_sql

    @property
    def params(self) -> tuple[object, ...]:
        return self.parameters


_UNFILTERED_SCOPE = CompiledScope('1 = 1', (), True, False)
_EMPTY_SCOPE = CompiledScope('0 = 1', (), False, True)


@dataclass(frozen=True)
class SnapshotRevision:
    '''Immutable cache key and descriptor for one published runtime revision.'''

    catalog_path: Path
    publication_generation: int
    snapshot_id: str
    build_id: str
    profile_id: str
    snapshot_path: Path
    descriptor: SnapshotDescriptor
    delta_generation: int = 0
    delta_segment_count: int = 0

    @property
    def key(self) -> tuple[int, str]:
        return (self.publication_generation, self.snapshot_id)

    @property
    def view_key(self) -> tuple[int, str, int]:
        return (
            self.publication_generation,
            self.snapshot_id,
            self.delta_generation,
        )


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    '''A physical result tagged with the revision that gave it meaning.'''

    snapshot_id: str
    publication_generation: int
    physical_id: int
    score: float
    delta_generation: int = 0


@dataclass(frozen=True, slots=True)
class LeasedSearchBatch:
    '''Columnar results bound once to the revision that produced their IDs.'''

    revision: SnapshotRevision
    results: VectorSearchBatch

    def __len__(self) -> int:
        return len(self.results)


@dataclass(frozen=True, slots=True)
class _HydrationPayload:
    chunk_uid: str
    parent_uid: str
    report_uid: str
    report_id: int
    profile_id: str
    child_order: int
    span_start: int
    span_end: int
    parent_slice: str
    canonical_relative_path: str
    source_sha256: str
    retrieval_metadata_sha256: str
    report_type: str
    report_date: str
    target_name: str | None
    title: str
    broker: str


class _RetrievedChunkTuple(NamedTuple):
    rank: int
    score: float
    physical_id: int
    snapshot_id: str
    publication_generation: int
    payload: _HydrationPayload


class RetrievedChunk(_RetrievedChunkTuple):
    '''Native logical result hydrated from the canonical parent slice.'''

    __slots__ = ()

    @property
    def chunk_uid(self) -> str:
        return self.payload.chunk_uid

    @property
    def parent_uid(self) -> str:
        return self.payload.parent_uid

    @property
    def report_uid(self) -> str:
        return self.payload.report_uid

    @property
    def report_id(self) -> int:
        return self.payload.report_id

    @property
    def profile_id(self) -> str:
        return self.payload.profile_id

    @property
    def child_order(self) -> int:
        return self.payload.child_order

    @property
    def span_start(self) -> int:
        return self.payload.span_start

    @property
    def span_end(self) -> int:
        return self.payload.span_end

    @property
    def parent_slice(self) -> str:
        return self.payload.parent_slice

    @property
    def canonical_relative_path(self) -> str:
        return self.payload.canonical_relative_path

    @property
    def source_sha256(self) -> str:
        return self.payload.source_sha256

    @property
    def retrieval_metadata_sha256(self) -> str:
        return self.payload.retrieval_metadata_sha256

    @property
    def report_type(self) -> str:
        return self.payload.report_type

    @property
    def report_date(self) -> str:
        return self.payload.report_date

    @property
    def target_name(self) -> str | None:
        return self.payload.target_name

    @property
    def title(self) -> str:
        return self.payload.title

    @property
    def broker(self) -> str:
        return self.payload.broker

    @property
    def text(self) -> str:
        return self.parent_slice

    @property
    def file_name(self) -> str:
        return PurePosixPath(self.canonical_relative_path).name

    @property
    def metadata(self) -> dict[str, object]:
        return {
            'report_id': self.report_id,
            'report_uid': self.report_uid,
            'parent_uid': self.parent_uid,
            'chunk_uid': self.chunk_uid,
            'profile_id': self.profile_id,
            'canonical_relative_path': self.canonical_relative_path,
            'file_name': self.file_name,
            'source_sha256': self.source_sha256,
            'retrieval_metadata_sha256': self.retrieval_metadata_sha256,
            'report_type': self.report_type,
            'report_date': self.report_date,
            'target_name': self.target_name,
            'title': self.title,
            'broker': self.broker,
        }


@dataclass
class _CacheEntry:
    index: RawVectorIndex | None = None
    lease_count: int = 0
    hydrated_rows: OrderedDict[int, _HydrationPayload] = field(
        default_factory=OrderedDict
    )


class CachedSnapshotHandle:
    '''One revision lease whose FAISS handle is loaded only when searched.'''

    def __init__(
        self,
        cache: 'SnapshotCache',
        revision: SnapshotRevision,
        entry: _CacheEntry,
    ) -> None:
        self._cache = cache
        self._revision = revision
        self._entry = entry
        self._index = entry.index
        self._released = False

    @property
    def index(self) -> RawVectorIndex:
        if self._released:
            raise LeaseReleasedError('snapshot revision lease has been released')
        if self._index is None:
            self._index = self._cache._index_for(self._revision, self._entry)
        return self._index

    def hydration_payloads(
        self,
        physical_ids: Sequence[int],
    ) -> list[_HydrationPayload | None]:
        if self._released:
            raise LeaseReleasedError('snapshot revision lease has been released')
        return self._cache._hydration_payloads_for(
            self._revision,
            self._entry,
            physical_ids,
        )

    def remember_hydration_rows(
        self,
        rows: Mapping[int, _HydrationPayload],
    ) -> None:
        if self._released:
            raise LeaseReleasedError('snapshot revision lease has been released')
        self._cache._remember_hydration_rows(
            self._revision,
            self._entry,
            rows,
        )

    def _release(self) -> None:
        self._released = True


class SnapshotCache:
    '''Thread-safe cache whose entries cannot close while requests lease them.'''

    def __init__(
        self,
        loader: Callable[[Path, SnapshotDescriptor], RawVectorIndex] = load_index,
        closer: Callable[[RawVectorIndex], None] | None = None,
        hydration_cache_size: int = DEFAULT_HYDRATION_CACHE_SIZE,
    ) -> None:
        if (
            not isinstance(hydration_cache_size, int)
            or isinstance(hydration_cache_size, bool)
            or hydration_cache_size <= 0
        ):
            raise ValueError('hydration cache size must be a positive integer')
        self._loader = loader
        self._closer = closer
        self._hydration_cache_size = hydration_cache_size
        self._entries: dict[SnapshotRevision, _CacheEntry] = {}
        self._lock = threading.RLock()
        self._closed = False

    @contextmanager
    def lease(
        self,
        revision: SnapshotRevision,
        validate: Callable[[], None] | None = None,
    ) -> Iterator[CachedSnapshotHandle]:
        '''Lease a revision without requiring a FAISS call for empty scopes.'''

        with self._lock:
            if self._closed:
                raise RepositoryError('snapshot cache is closed')
            entry = self._entries.get(revision)
            if entry is None:
                if validate is not None:
                    validate()
                entry = _CacheEntry()
                self._entries[revision] = entry
            entry.lease_count += 1
            handle = CachedSnapshotHandle(self, revision, entry)

        try:
            yield handle
        finally:
            with self._lock:
                handle._release()
                current = self._entries.get(revision)
                if current is not entry or current.lease_count <= 0:
                    raise RepositoryError('snapshot lease accounting is inconsistent')
                current.lease_count -= 1

    @contextmanager
    def acquire(
        self,
        revision: SnapshotRevision,
        validate: Callable[[], None] | None = None,
    ) -> Iterator[RawVectorIndex]:
        '''Eager-index compatibility wrapper around the lazy revision lease.'''

        with self.lease(revision, validate=validate) as handle:
            yield handle.index

    def lease_count(self, revision: SnapshotRevision) -> int:
        with self._lock:
            entry = self._entries.get(revision)
            return 0 if entry is None else entry.lease_count

    def cached_revisions(self) -> tuple[SnapshotRevision, ...]:
        with self._lock:
            return tuple(self._entries)

    def evict(self, revision: SnapshotRevision) -> bool:
        '''Drop one cached handle, but never while its lease count is nonzero.'''

        with self._lock:
            entry = self._entries.get(revision)
            if entry is None:
                return False
            if entry.lease_count:
                raise SnapshotInUseError(
                    f'revision {revision.key!r} still has {entry.lease_count} lease(s)'
                )
            if entry.index is not None:
                self._close_index(entry.index)
            del self._entries[revision]
            return True

    def evict_snapshot(self, snapshot_id: str) -> int:
        '''Evict every cached generation for a physical snapshot atomically.'''

        with self._lock:
            matches = [
                (revision, entry)
                for revision, entry in self._entries.items()
                if revision.snapshot_id == snapshot_id
            ]
            leased = [entry.lease_count for _, entry in matches if entry.lease_count]
            if leased:
                raise SnapshotInUseError(
                    f'snapshot {snapshot_id!r} still has {sum(leased)} lease(s)'
                )
            for _, entry in matches:
                if entry.index is not None:
                    self._close_index(entry.index)
            for revision, _ in matches:
                del self._entries[revision]
            return len(matches)

    def close(self) -> None:
        '''Close all cached references only when every request has released.'''

        with self._lock:
            lease_total = sum(entry.lease_count for entry in self._entries.values())
            if lease_total:
                raise SnapshotInUseError(
                    f'snapshot cache still has {lease_total} active lease(s)'
                )
            entries = tuple(self._entries.values())
            for entry in entries:
                if entry.index is not None:
                    self._close_index(entry.index)
            self._entries.clear()
            self._closed = True

    def _index_for(
        self,
        revision: SnapshotRevision,
        entry: _CacheEntry,
    ) -> RawVectorIndex:
        with self._lock:
            current = self._entries.get(revision)
            if (
                self._closed
                or current is not entry
                or entry.lease_count <= 0
            ):
                raise LeaseReleasedError('snapshot revision is no longer leased')
            if entry.index is None:
                try:
                    index = self._loader(
                        revision.snapshot_path,
                        revision.descriptor,
                    )
                    _validate_loaded_index(index, revision.descriptor)
                except SnapshotValidationError:
                    raise
                except Exception as exc:
                    raise SnapshotValidationError(
                        'active snapshot could not be loaded and validated'
                    ) from exc
                entry.index = index
            return entry.index

    def _hydration_payloads_for(
        self,
        revision: SnapshotRevision,
        entry: _CacheEntry,
        physical_ids: Sequence[int],
    ) -> list[_HydrationPayload | None]:
        with self._lock:
            self._require_leased_entry(revision, entry)
            payloads: list[_HydrationPayload | None] = []
            move_hits = len(entry.hydrated_rows) >= self._hydration_cache_size
            for physical_id in physical_ids:
                row = entry.hydrated_rows.get(physical_id)
                if row is not None and move_hits:
                    entry.hydrated_rows.move_to_end(physical_id)
                payloads.append(row)
            return payloads

    def _remember_hydration_rows(
        self,
        revision: SnapshotRevision,
        entry: _CacheEntry,
        rows: Mapping[int, _HydrationPayload],
    ) -> None:
        with self._lock:
            self._require_leased_entry(revision, entry)
            for physical_id, row in rows.items():
                entry.hydrated_rows[physical_id] = row
                entry.hydrated_rows.move_to_end(physical_id)
            while len(entry.hydrated_rows) > self._hydration_cache_size:
                entry.hydrated_rows.popitem(last=False)

    def _require_leased_entry(
        self,
        revision: SnapshotRevision,
        entry: _CacheEntry,
    ) -> None:
        current = self._entries.get(revision)
        if self._closed or current is not entry or entry.lease_count <= 0:
            raise LeaseReleasedError('snapshot revision is no longer leased')

    def _close_index(self, index: RawVectorIndex) -> None:
        if self._closer is not None:
            self._closer(index)
            return
        close = getattr(index, 'close', None)
        if callable(close):
            close()


_SHARED_CACHE_LOCK = threading.RLock()
_SHARED_CACHES: dict[Path, SnapshotCache] = {}


def shared_snapshot_cache(data_root: str | Path) -> SnapshotCache:
    '''Return the process-wide cache used by both readers and lease-safe GC.'''

    root = Path(data_root).resolve()
    with _SHARED_CACHE_LOCK:
        cache = _SHARED_CACHES.get(root)
        if cache is None:
            cache = SnapshotCache()
            _SHARED_CACHES[root] = cache
        return cache


_ELIGIBILITY_FROM = '''
    snapshot_membership AS membership
    JOIN retrieval_chunks AS chunk
      ON chunk.chunk_uid = membership.chunk_uid
    JOIN retrieval_parents AS parent
      ON parent.parent_uid = chunk.parent_uid
     AND parent.profile_id = chunk.profile_id
    JOIN reports AS report
      ON report.report_id = parent.report_id
'''


class SnapshotSession:
    '''One request's read transaction and snapshot lease.'''

    def __init__(
        self,
        connection: sqlite3.Connection,
        revision: SnapshotRevision,
        snapshot_handle: CachedSnapshotHandle,
        query_batch_size: int,
    ) -> None:
        self._connection = connection
        self.revision = revision
        self._snapshot_handle = snapshot_handle
        self._query_batch_size = query_batch_size
        self._released = False
        self.hydration_sql_batches = 0
        self.hydration_sql_rows = 0
        self.hydration_cache_hits = 0
        self.hydration_cache_misses = 0

    @property
    def index(self) -> RawVectorIndex:
        self._ensure_active()
        return self._snapshot_handle.index

    @property
    def total_count(self) -> int:
        return self.revision.descriptor.ntotal

    def _release(self) -> None:
        self._released = True

    def _ensure_active(self) -> None:
        if self._released:
            raise LeaseReleasedError('snapshot request lease has already been released')

    def eligible_count(self, scope: CompiledScope) -> int:
        self._ensure_active()
        if scope.is_empty:
            return 0
        if scope.is_unfiltered:
            return self.revision.descriptor.ntotal
        row = self._connection.execute(
            f'''
            SELECT count(*)
            FROM {_ELIGIBILITY_FROM}
            WHERE membership.snapshot_id = ?
              AND ({scope.predicate_sql})
            ''',
            (self.revision.snapshot_id, *scope.parameters),
        ).fetchone()
        return int(row[0])

    def eligible_physical_ids(
        self,
        scope: CompiledScope,
        *,
        expected_count: int | None = None,
    ) -> tuple[int, ...]:
        '''Materialize only a previously classified bounded selector scope.'''

        self._ensure_active()
        if scope.is_empty:
            return ()
        if scope.is_unfiltered:
            raise RepositoryError(
                'universal scope must not materialize an N-sized allowed-ID set'
            )
        rows = self._connection.execute(
            f'''
            SELECT membership.faiss_id
            FROM {_ELIGIBILITY_FROM}
            WHERE membership.snapshot_id = ?
              AND ({scope.predicate_sql})
            ORDER BY membership.faiss_id
            ''',
            (self.revision.snapshot_id, *scope.parameters),
        ).fetchall()
        values = tuple(int(row[0]) for row in rows)
        if expected_count is not None and len(values) != expected_count:
            raise SnapshotValidationError(
                'selector eligibility changed inside one request transaction'
            )
        return values

    def filter_candidates(
        self,
        candidates: Sequence[RankedCandidate],
        scope: CompiledScope,
    ) -> tuple[RankedCandidate, ...]:
        '''Reject ineligible direct-search candidates in bounded SQL batches.'''

        self._ensure_active()
        self._validate_candidate_revisions(candidates)
        if not candidates or scope.is_empty:
            return ()
        if scope.is_unfiltered:
            return tuple(candidates)
        _reject_duplicate_physical_ids(candidates)

        eligible: list[RankedCandidate] = []
        for batch in _batches(candidates, self._query_batch_size):
            placeholders = ','.join('?' for _ in batch)
            ids = tuple(candidate.physical_id for candidate in batch)
            rows = self._connection.execute(
                f'''
                SELECT membership.faiss_id
                FROM {_ELIGIBILITY_FROM}
                WHERE membership.snapshot_id = ?
                  AND membership.faiss_id IN ({placeholders})
                  AND ({scope.predicate_sql})
                ''',
                (self.revision.snapshot_id, *ids, *scope.parameters),
            ).fetchall()
            matched = {int(row[0]) for row in rows}
            eligible.extend(
                candidate for candidate in batch if candidate.physical_id in matched
            )
        return tuple(eligible)

    def search_index(
        self,
        query: Any,
        k: int,
        allowed_ids: Sequence[int] | None = None,
    ) -> LeasedSearchBatch:
        '''Search the leased index and bind its columnar results to this revision.'''

        self._ensure_active()
        return LeasedSearchBatch(
            self.revision,
            self.index.search_batch(query, k, allowed_ids=allowed_ids),
        )

    def hydrate_search_batch(
        self,
        batch: LeasedSearchBatch,
        scope: CompiledScope | None = None,
    ) -> tuple[RetrievedChunk, ...]:
        '''Hydrate an opaque batch only under the revision that produced it.'''

        self._ensure_active()
        if batch.revision != self.revision:
            raise CrossSnapshotMembershipError(
                'ranked candidate belongs to a different snapshot revision'
            )
        return self._hydrate_physical_results(
            batch.results.physical_ids,
            batch.results.scores,
            scope,
        )

    def hydrate(
        self,
        candidates: Sequence[RankedCandidate],
        scope: CompiledScope | None = None,
    ) -> tuple[RetrievedChunk, ...]:
        '''Bulk-hydrate bounded batches while retaining the exact FAISS rank.'''

        self._ensure_active()
        self._validate_candidate_revisions(candidates)
        if not candidates:
            return ()
        _reject_duplicate_physical_ids(candidates)
        return self._hydrate_physical_results(
            tuple(candidate.physical_id for candidate in candidates),
            tuple(candidate.score for candidate in candidates),
            scope,
        )

    def _hydrate_physical_results(
        self,
        physical_ids: Sequence[int],
        scores: Sequence[float],
        scope: CompiledScope | None,
    ) -> tuple[RetrievedChunk, ...]:
        if len(physical_ids) != len(scores):
            raise SnapshotValidationError('ranked result columns have different lengths')
        if len(physical_ids) == 0:
            return ()
        cache_enabled = scope is None or scope.is_unfiltered
        payloads: list[_HydrationPayload | None] = (
            self._snapshot_handle.hydration_payloads(physical_ids)
            if cache_enabled
            else [None] * len(physical_ids)
        )
        predicate = ''
        scope_parameters: tuple[object, ...] = ()
        if scope is not None:
            if scope.is_empty:
                raise SnapshotValidationError('cannot hydrate an explicitly empty scope')
            if not scope.is_unfiltered:
                predicate = f' AND ({scope.predicate_sql})'
                scope_parameters = scope.parameters

        pending_indices = tuple(
            index
            for index, payload in enumerate(payloads)
            if payload is None
        )
        self.hydration_cache_hits += len(physical_ids) - len(pending_indices)
        self.hydration_cache_misses += len(pending_indices)
        remembered: dict[int, _HydrationPayload] = {}
        for batch in _batches(pending_indices, self._query_batch_size):
            placeholders = ','.join('?' for _ in batch)
            ids = tuple(int(physical_ids[index]) for index in batch)
            positions = dict(zip(ids, batch))
            self.hydration_sql_batches += 1
            rows = self._connection.execute(
                f'''
                SELECT
                    membership.faiss_id,
                    chunk.chunk_uid,
                    chunk.parent_uid,
                    chunk.profile_id,
                    chunk.child_order,
                    chunk.span_start,
                    chunk.span_end,
                    parent.report_id,
                    substr(
                        parent.content,
                        chunk.span_start + 1,
                        chunk.span_end - chunk.span_start
                    ) AS parent_slice,
                    report.report_uid,
                    report.canonical_relative_path,
                    report.source_sha256,
                    report.retrieval_metadata_sha256,
                    report.report_type,
                    report.report_date,
                    report.target_name,
                    report.title,
                    report.broker
                FROM {_ELIGIBILITY_FROM}
                WHERE membership.snapshot_id = ?
                  AND membership.faiss_id IN ({placeholders})
                  {predicate}
                ''',
                (
                    self.revision.snapshot_id,
                    *ids,
                    *scope_parameters,
                ),
            ).fetchall()
            self.hydration_sql_rows += len(rows)
            for row in rows:
                physical_id = int(row['faiss_id'])
                position = positions.get(physical_id)
                if position is None or payloads[position] is not None:
                    raise SnapshotValidationError(
                        'hydration returned duplicate snapshot membership'
                    )
                payload = _hydration_payload(row)
                payloads[position] = payload
                remembered[physical_id] = payload

        if remembered:
            self._snapshot_handle.remember_hydration_rows(remembered)

        missing = [
            physical_ids[index]
            for index, payload in enumerate(payloads)
            if payload is None
        ]
        if missing:
            raise CrossSnapshotMembershipError(
                'ranked candidates are absent from the leased snapshot/scope: '
                + ', '.join(str(value) for value in missing[:10])
            )

        hydrated: list[RetrievedChunk] = []
        for rank, (physical_id, score, payload) in enumerate(
            zip(physical_ids, scores, payloads),
            1,
        ):
            physical_id = int(physical_id)
            hydrated.append(
                RetrievedChunk(
                    rank,
                    float(score),
                    physical_id,
                    self.revision.snapshot_id,
                    self.revision.publication_generation,
                    payload,
                )
            )
        return tuple(hydrated)

    def tag_results(
        self,
        results: Sequence[SearchResult],
    ) -> tuple[RankedCandidate, ...]:
        self._ensure_active()
        return tuple(
            RankedCandidate(
                snapshot_id=self.revision.snapshot_id,
                publication_generation=self.revision.publication_generation,
                physical_id=result.physical_id,
                score=result.score,
                delta_generation=self.revision.delta_generation,
            )
            for result in results
        )

    def _validate_candidate_revisions(
        self,
        candidates: Sequence[RankedCandidate],
    ) -> None:
        for candidate in candidates:
            if (
                candidate.snapshot_id != self.revision.snapshot_id
                or candidate.publication_generation
                != self.revision.publication_generation
                or candidate.delta_generation != self.revision.delta_generation
            ):
                raise CrossSnapshotMembershipError(
                    'ranked candidate belongs to a different snapshot revision'
                )


@dataclass(frozen=True, slots=True)
class _CompositeSessionArtifact:
    artifact_id: str
    offset: int
    sequence: int
    revision: SnapshotRevision
    handle: CachedSnapshotHandle
    kind: str
    visible_local_ids: frozenset[int] | None = None
    hidden_local_ids: frozenset[int] = frozenset()

    @property
    def local_total(self) -> int:
        return self.revision.descriptor.ntotal

    def contains_virtual_id(self, physical_id: int) -> bool:
        return self.offset < physical_id <= self.offset + self.local_total

    def local_id(self, physical_id: int) -> int:
        return physical_id - self.offset

    def virtual_id(self, local_id: int) -> int:
        return self.offset + local_id

    def is_visible(self, local_id: int) -> bool:
        if local_id in self.hidden_local_ids:
            return False
        return self.visible_local_ids is None or local_id in self.visible_local_ids


class CompositeSnapshotSession(SnapshotSession):
    '''One request pinned to a base snapshot and its committed sparse head.'''

    def __init__(
        self,
        connection: sqlite3.Connection,
        revision: SnapshotRevision,
        base_revision: SnapshotRevision,
        base_handle: CachedSnapshotHandle,
        overlay: ActiveDeltaOverlay,
        delta_handles: tuple[
            tuple[DeltaSegmentRecord, SnapshotRevision, CachedSnapshotHandle], ...
        ],
        query_batch_size: int,
    ) -> None:
        super().__init__(
            connection,
            revision,
            base_handle,
            query_batch_size,
        )
        self._overlay = overlay
        head_by_path = overlay.head_by_path
        hidden_base_ids: set[int] = set()
        head_paths = tuple(sorted(head_by_path))
        for path_batch in _batches(head_paths, query_batch_size):
            placeholders = ','.join('?' for _ in path_batch)
            rows = connection.execute(
                f'''
                SELECT membership.faiss_id
                FROM {_ELIGIBILITY_FROM}
                WHERE membership.snapshot_id = ?
                  AND report.canonical_relative_path IN ({placeholders})
                ''',
                (base_revision.snapshot_id, *path_batch),
            ).fetchall()
            hidden_base_ids.update(int(row[0]) for row in rows)

        artifacts: list[_CompositeSessionArtifact] = [
            _CompositeSessionArtifact(
                artifact_id=base_revision.snapshot_id,
                offset=0,
                sequence=0,
                revision=base_revision,
                handle=base_handle,
                kind='base',
                hidden_local_ids=frozenset(hidden_base_ids),
            )
        ]
        offset = base_revision.descriptor.ntotal
        for segment, artifact_revision, handle in delta_handles:
            rows = connection.execute(
                '''
                SELECT membership.faiss_id, report.canonical_relative_path
                FROM retrieval_delta_membership AS membership
                JOIN retrieval_chunks AS chunk
                  ON chunk.chunk_uid = membership.chunk_uid
                JOIN retrieval_parents AS parent
                  ON parent.parent_uid = chunk.parent_uid
                 AND parent.profile_id = chunk.profile_id
                JOIN reports AS report ON report.report_id = parent.report_id
                WHERE membership.segment_id = ?
                ORDER BY membership.faiss_id
                ''',
                (segment.segment_id,),
            ).fetchall()
            visible = frozenset(
                int(row[0])
                for row in rows
                if (
                    (head := head_by_path.get(str(row[1]))) is not None
                    and head.action == 'upsert'
                    and head.segment_id == segment.segment_id
                )
            )
            artifacts.append(
                _CompositeSessionArtifact(
                    artifact_id=segment.segment_id,
                    offset=offset,
                    sequence=segment.sequence,
                    revision=artifact_revision,
                    handle=handle,
                    kind='delta',
                    visible_local_ids=visible,
                )
            )
            offset += artifact_revision.descriptor.ntotal
        self._artifacts = tuple(artifacts)
        self._artifact_by_id = {
            artifact.artifact_id: artifact for artifact in self._artifacts
        }
        self._logical_total = (
            base_revision.descriptor.ntotal
            - len(hidden_base_ids)
            + sum(
                len(artifact.visible_local_ids or ())
                for artifact in self._artifacts
                if artifact.kind == 'delta'
            )
        )
        self._composite_index: CompositeVectorIndex | None = None

    @property
    def total_count(self) -> int:
        return self._logical_total

    @property
    def index(self) -> CompositeVectorIndex:
        self._ensure_active()
        if self._composite_index is None:
            self._composite_index = CompositeVectorIndex(
                tuple(
                    CompositeArtifact(
                        artifact_id=artifact.artifact_id,
                        offset=artifact.offset,
                        index=artifact.handle.index,
                        visible_local_ids=artifact.visible_local_ids,
                        hidden_local_ids=artifact.hidden_local_ids,
                    )
                    for artifact in self._artifacts
                )
            )
        return self._composite_index

    def eligible_count(self, scope: CompiledScope) -> int:
        self._ensure_active()
        if scope.is_empty:
            return 0
        if scope.is_unfiltered:
            return self.total_count
        return sum(len(self._eligible_local_ids(artifact, scope)) for artifact in self._artifacts)

    def eligible_physical_ids(
        self,
        scope: CompiledScope,
        *,
        expected_count: int | None = None,
    ) -> tuple[int, ...]:
        self._ensure_active()
        if scope.is_empty:
            return ()
        if scope.is_unfiltered:
            raise RepositoryError(
                'universal scope must not materialize an N-sized allowed-ID set'
            )
        values = tuple(
            artifact.virtual_id(local_id)
            for artifact in self._artifacts
            for local_id in self._eligible_local_ids(artifact, scope)
        )
        if expected_count is not None and len(values) != expected_count:
            raise SnapshotValidationError(
                'selector eligibility changed inside one request transaction'
            )
        return values

    def filter_candidates(
        self,
        candidates: Sequence[RankedCandidate],
        scope: CompiledScope,
    ) -> tuple[RankedCandidate, ...]:
        self._ensure_active()
        self._validate_candidate_revisions(candidates)
        if not candidates or scope.is_empty:
            return ()
        _reject_duplicate_physical_ids(candidates)
        if scope.is_unfiltered:
            return tuple(
                candidate
                for candidate in candidates
                if self._artifact_for_virtual_id(candidate.physical_id).is_visible(
                    self._artifact_for_virtual_id(candidate.physical_id).local_id(
                        candidate.physical_id
                    )
                )
            )
        eligible_by_artifact: dict[str, set[int]] = {}
        for artifact in self._artifacts:
            local_candidates = {
                artifact.local_id(candidate.physical_id)
                for candidate in candidates
                if artifact.contains_virtual_id(candidate.physical_id)
                and artifact.is_visible(artifact.local_id(candidate.physical_id))
            }
            if not local_candidates:
                continue
            eligible_by_artifact[artifact.artifact_id] = self._matching_local_ids(
                artifact,
                tuple(sorted(local_candidates)),
                scope,
            )
        return tuple(
            candidate
            for candidate in candidates
            if (
                (artifact := self._artifact_for_virtual_id(candidate.physical_id))
                and artifact.local_id(candidate.physical_id)
                in eligible_by_artifact.get(artifact.artifact_id, set())
            )
        )

    def _hydrate_physical_results(
        self,
        physical_ids: Sequence[int],
        scores: Sequence[float],
        scope: CompiledScope | None,
    ) -> tuple[RetrievedChunk, ...]:
        if len(physical_ids) != len(scores):
            raise SnapshotValidationError('ranked result columns have different lengths')
        if len(physical_ids) == 0:
            return ()
        payloads: list[_HydrationPayload | None] = [None] * len(physical_ids)
        remembered_by_artifact: dict[str, dict[int, _HydrationPayload]] = {}
        for artifact in self._artifacts:
            positions = [
                index
                for index, physical_id in enumerate(physical_ids)
                if artifact.contains_virtual_id(int(physical_id))
            ]
            if not positions:
                continue
            local_ids = [artifact.local_id(int(physical_ids[index])) for index in positions]
            if any(not artifact.is_visible(local_id) for local_id in local_ids):
                raise CrossSnapshotMembershipError(
                    'ranked candidate is hidden in the pinned composite revision'
                )
            cached = (
                artifact.handle.hydration_payloads(local_ids)
                if scope is None or scope.is_unfiltered
                else [None] * len(local_ids)
            )
            for position, payload in zip(positions, cached, strict=True):
                payloads[position] = payload
            pending = [
                (position, local_id)
                for position, local_id, payload in zip(positions, local_ids, cached, strict=True)
                if payload is None
            ]
            self.hydration_cache_hits += len(local_ids) - len(pending)
            self.hydration_cache_misses += len(pending)
            for pending_batch in _batches(pending, self._query_batch_size):
                ids = tuple(local_id for _position, local_id in pending_batch)
                rows = self._select_payload_rows(artifact, ids, scope)
                self.hydration_sql_batches += 1
                self.hydration_sql_rows += len(rows)
                row_by_id = {int(row['faiss_id']): row for row in rows}
                for position, local_id in pending_batch:
                    row = row_by_id.get(local_id)
                    if row is None:
                        continue
                    payload = _hydration_payload(row)
                    payloads[position] = payload
                    remembered_by_artifact.setdefault(artifact.artifact_id, {})[
                        local_id
                    ] = payload
        for artifact_id, remembered in remembered_by_artifact.items():
            self._artifact_by_id[artifact_id].handle.remember_hydration_rows(remembered)
        missing = [
            physical_ids[index]
            for index, payload in enumerate(payloads)
            if payload is None
        ]
        if missing:
            raise CrossSnapshotMembershipError(
                'ranked candidates are absent from the pinned composite scope: '
                + ', '.join(str(value) for value in missing[:10])
            )
        return tuple(
            RetrievedChunk(
                rank,
                float(score),
                int(physical_id),
                self.revision.snapshot_id,
                self.revision.publication_generation,
                payload,
            )
            for rank, (physical_id, score, payload) in enumerate(
                zip(physical_ids, scores, payloads, strict=True),
                1,
            )
        )

    def _eligible_local_ids(
        self,
        artifact: _CompositeSessionArtifact,
        scope: CompiledScope,
    ) -> tuple[int, ...]:
        rows = self._select_membership_rows(artifact, scope)
        return tuple(
            local_id
            for row in rows
            if artifact.is_visible(local_id := int(row[0]))
        )

    def _matching_local_ids(
        self,
        artifact: _CompositeSessionArtifact,
        local_ids: Sequence[int],
        scope: CompiledScope,
    ) -> set[int]:
        matched: set[int] = set()
        for id_batch in _batches(local_ids, self._query_batch_size):
            placeholders = ','.join('?' for _ in id_batch)
            rows = self._connection.execute(
                f'''
                SELECT membership.faiss_id
                FROM {self._eligibility_from(artifact)}
                WHERE membership.{self._membership_key(artifact)} = ?
                  AND membership.faiss_id IN ({placeholders})
                  AND ({scope.predicate_sql})
                ''',
                (
                    artifact.artifact_id,
                    *id_batch,
                    *scope.parameters,
                ),
            ).fetchall()
            matched.update(int(row[0]) for row in rows)
        return matched

    def _select_membership_rows(
        self,
        artifact: _CompositeSessionArtifact,
        scope: CompiledScope,
    ) -> list[sqlite3.Row]:
        return self._connection.execute(
            f'''
            SELECT membership.faiss_id
            FROM {self._eligibility_from(artifact)}
            WHERE membership.{self._membership_key(artifact)} = ?
              AND ({scope.predicate_sql})
            ORDER BY membership.faiss_id
            ''',
            (artifact.artifact_id, *scope.parameters),
        ).fetchall()

    def _select_payload_rows(
        self,
        artifact: _CompositeSessionArtifact,
        local_ids: Sequence[int],
        scope: CompiledScope | None,
    ) -> list[sqlite3.Row]:
        placeholders = ','.join('?' for _ in local_ids)
        predicate = ''
        scope_parameters: tuple[object, ...] = ()
        if scope is not None and not scope.is_unfiltered:
            if scope.is_empty:
                return []
            predicate = f' AND ({scope.predicate_sql})'
            scope_parameters = scope.parameters
        return self._connection.execute(
            f'''
            SELECT
                membership.faiss_id,
                chunk.chunk_uid,
                chunk.parent_uid,
                chunk.profile_id,
                chunk.child_order,
                chunk.span_start,
                chunk.span_end,
                parent.report_id,
                substr(
                    parent.content,
                    chunk.span_start + 1,
                    chunk.span_end - chunk.span_start
                ) AS parent_slice,
                report.report_uid,
                report.canonical_relative_path,
                report.source_sha256,
                report.retrieval_metadata_sha256,
                report.report_type,
                report.report_date,
                report.target_name,
                report.title,
                report.broker
            FROM {self._eligibility_from(artifact)}
            WHERE membership.{self._membership_key(artifact)} = ?
              AND membership.faiss_id IN ({placeholders})
              {predicate}
            ''',
            (
                artifact.artifact_id,
                *local_ids,
                *scope_parameters,
            ),
        ).fetchall()

    @staticmethod
    def _membership_key(artifact: _CompositeSessionArtifact) -> str:
        return 'snapshot_id' if artifact.kind == 'base' else 'segment_id'

    @staticmethod
    def _eligibility_from(artifact: _CompositeSessionArtifact) -> str:
        if artifact.kind == 'base':
            return _ELIGIBILITY_FROM
        return '''
            retrieval_delta_membership AS membership
            JOIN retrieval_chunks AS chunk
              ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent
              ON parent.parent_uid = chunk.parent_uid
             AND parent.profile_id = chunk.profile_id
            JOIN reports AS report
              ON report.report_id = parent.report_id
        '''

    def _artifact_for_virtual_id(self, physical_id: int) -> _CompositeSessionArtifact:
        for artifact in self._artifacts:
            if artifact.contains_virtual_id(physical_id):
                return artifact
        raise CrossSnapshotMembershipError(
            'physical ID is outside the pinned composite revision'
        )


@dataclass(frozen=True, slots=True)
class _PinnedRevision:
    revision: SnapshotRevision
    base_revision: SnapshotRevision
    overlay: ActiveDeltaOverlay
    delta_revisions: tuple[tuple[DeltaSegmentRecord, SnapshotRevision], ...]


class CatalogRepository:
    '''Read-only native catalog repository with request-scoped leases.'''

    def __init__(
        self,
        catalog_path: str | Path,
        *,
        data_root: str | Path | None = None,
        cache: SnapshotCache | None = None,
        query_batch_size: int = DEFAULT_QUERY_BATCH_SIZE,
    ) -> None:
        self.catalog_path = Path(catalog_path).resolve()
        self.data_root = (
            Path(data_root).resolve()
            if data_root is not None
            else self.catalog_path.parent.resolve()
        )
        if (
            not isinstance(query_batch_size, int)
            or isinstance(query_batch_size, bool)
            or query_batch_size <= 0
            or query_batch_size > MAX_SCOPE_VALUES
        ):
            raise ValueError(
                f'query_batch_size must be between 1 and {MAX_SCOPE_VALUES}'
            )
        self.query_batch_size = query_batch_size
        self.cache = (
            cache if cache is not None else shared_snapshot_cache(self.data_root)
        )
        self._connection_lock = threading.RLock()
        self._connections: dict[int, sqlite3.Connection] = {}
        self._revision_cache: dict[tuple[int, str, str], SnapshotRevision] = {}
        self._active_requests = 0
        self._closed = False

    @contextmanager
    def request(
        self,
        *,
        materialize_indexes: bool = True,
    ) -> Iterator[SnapshotSession]:
        '''Acquire exactly one base-plus-delta generation for one read request.'''

        connection, persistent = self._acquire_request_connection()
        session: SnapshotSession | None = None
        try:
            connection.execute('BEGIN')
            pinned = self._read_active_view(connection)
            with ExitStack() as stack:
                base_handle = stack.enter_context(
                    self.cache.lease(
                        pinned.base_revision,
                        validate=lambda: self._validate_revision(
                            connection,
                            pinned.base_revision,
                        ),
                    )
                )
                delta_handles = tuple(
                    (
                        segment,
                        artifact_revision,
                        stack.enter_context(
                            self.cache.lease(
                                artifact_revision,
                                validate=lambda segment=segment,
                                artifact_revision=artifact_revision: self._validate_delta_revision(
                                    connection,
                                    segment,
                                    artifact_revision,
                                ),
                            )
                        ),
                    )
                    for segment, artifact_revision in pinned.delta_revisions
                )
                self._evict_stale_cached_revisions(pinned)
                if materialize_indexes:
                    # A search-capable request can outlive two publications in
                    # a different process. Materialize every pinned artifact
                    # now so later base-GC unlink cannot break its first FAISS
                    # call. Provably empty scopes opt out in the reader.
                    _ = base_handle.index
                    for _segment, _revision, handle in delta_handles:
                        _ = handle.index
                session = (
                    SnapshotSession(
                        connection,
                        pinned.revision,
                        base_handle,
                        self.query_batch_size,
                    )
                    if not pinned.overlay.heads
                    else CompositeSnapshotSession(
                        connection,
                        pinned.revision,
                        pinned.base_revision,
                        base_handle,
                        pinned.overlay,
                        delta_handles,
                        self.query_batch_size,
                    )
                )
                try:
                    yield session
                finally:
                    session._release()
        finally:
            if connection.in_transaction:
                connection.rollback()
            self._release_request_connection(connection, persistent=persistent)

    def lease_active_snapshot(self) -> Any:
        '''Compatibility name for ``request()`` returning the context manager.'''

        return self.request()

    def _evict_stale_cached_revisions(self, pinned: _PinnedRevision) -> None:
        active_artifact_ids = {
            pinned.base_revision.snapshot_id,
            *(
                revision.snapshot_id
                for _segment, revision in pinned.delta_revisions
            ),
        }
        for revision in self.cache.cached_revisions():
            if revision.snapshot_id in active_artifact_ids:
                continue
            try:
                self.cache.evict(revision)
            except SnapshotInUseError:
                # A request that pinned the previous composite revision still
                # owns this handle.  A later request retries after it releases.
                continue

    def close(self) -> None:
        '''Close idle read connections owned by this repository instance.'''

        with self._connection_lock:
            if self._active_requests:
                raise SnapshotInUseError(
                    f'repository still has {self._active_requests} active request(s)'
                )
            if self._closed:
                return
            self._closed = True
            connections = tuple(self._connections.values())
            self._connections.clear()
            self._revision_cache.clear()
        for connection in connections:
            connection.close()

    def __enter__(self) -> 'CatalogRepository':
        with self._connection_lock:
            if self._closed:
                raise RepositoryError('catalog repository is closed')
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _acquire_request_connection(self) -> tuple[sqlite3.Connection, bool]:
        with self._connection_lock:
            if self._closed:
                raise RepositoryError('catalog repository is closed')
            thread_id = threading.get_ident()
            connection = self._connections.get(thread_id)
            persistent = connection is not None and not connection.in_transaction
            if connection is None:
                connection = self._open_read_only_connection()
                self._connections[thread_id] = connection
                persistent = True
            elif not persistent:
                # Preserve independent request transactions for the rare nested
                # request on one thread without sacrificing the normal hot path.
                connection = self._open_read_only_connection()
            self._active_requests += 1
            return connection, persistent

    def _release_request_connection(
        self,
        connection: sqlite3.Connection,
        *,
        persistent: bool,
    ) -> None:
        if not persistent:
            connection.close()
        with self._connection_lock:
            if self._active_requests <= 0:
                raise RepositoryError('repository request accounting is inconsistent')
            self._active_requests -= 1

    def _open_read_only_connection(self) -> sqlite3.Connection:
        if not self.catalog_path.is_file():
            raise SnapshotUnavailableError('native retrieval catalog is missing')
        normalized = self.catalog_path.as_posix()
        uri = f'file:{quote(normalized, safe=":/")}?mode=ro'
        connection = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=5.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            configure_catalog_storage(connection)
            connection.execute('PRAGMA query_only = ON')
            connection.execute('PRAGMA foreign_keys = ON')
            return connection
        except SchemaError as exc:
            connection.close()
            raise SnapshotUnavailableError(
                'native retrieval catalog storage mode is invalid'
            ) from exc

    def _read_active_revision(
        self,
        connection: sqlite3.Connection,
    ) -> SnapshotRevision:
        identity_row = connection.execute(
            '''
            SELECT publication_generation, active_snapshot_id, active_build_id
            FROM retrieval_runtime
            '''
        ).fetchone()
        if identity_row is None:
            raise SnapshotUnavailableError(
                'exactly one active runtime identity is required'
            )
        if (
            identity_row['active_snapshot_id'] is None
            or identity_row['active_build_id'] is None
        ):
            raise SnapshotUnavailableError(
                'exactly one fully complete active snapshot is required'
            )
        identity = (
            int(identity_row['publication_generation']),
            str(identity_row['active_snapshot_id']),
            str(identity_row['active_build_id']),
        )
        with self._connection_lock:
            cached = self._revision_cache.get(identity)
        if cached is not None:
            return cached

        rows = connection.execute(
            '''
            SELECT
                runtime.publication_generation,
                snapshot.snapshot_id,
                snapshot.build_id,
                build.profile_id,
                snapshot.relative_path,
                snapshot.file_sha256,
                snapshot.size_bytes,
                snapshot.dimension,
                snapshot.metric,
                snapshot.ntotal,
                profile.dimension AS profile_dimension,
                profile.metric AS profile_metric
            FROM retrieval_runtime AS runtime
            JOIN retrieval_builds AS build
              ON build.build_id = runtime.active_build_id
             AND build.state = 'fully_complete'
            JOIN embedding_profiles AS profile
              ON profile.profile_id = build.profile_id
            JOIN vector_snapshots AS snapshot
              ON snapshot.snapshot_id = runtime.active_snapshot_id
             AND snapshot.build_id = build.build_id
             AND snapshot.state = 'ready'
            WHERE runtime.runtime_id = 1
            ''',
        ).fetchall()
        if len(rows) != 1:
            raise SnapshotUnavailableError(
                'exactly one fully complete active snapshot is required'
            )
        row = rows[0]
        if identity != (
            int(row['publication_generation']),
            str(row['snapshot_id']),
            str(row['build_id']),
        ):
            raise SnapshotValidationError(
                'active runtime identity changed inside one request transaction'
            )
        if (
            int(row['dimension']) != int(row['profile_dimension'])
            or str(row['metric']) != str(row['profile_metric'])
        ):
            raise SnapshotValidationError(
                'active snapshot descriptor does not match its embedding profile'
            )

        relative_path = PurePosixPath(str(row['relative_path']))
        snapshot_path = (self.data_root.joinpath(*relative_path.parts)).resolve()
        try:
            snapshot_path.relative_to(self.data_root)
        except ValueError as exc:
            raise SnapshotValidationError(
                'snapshot path escapes the selected data root'
            ) from exc

        revision = SnapshotRevision(
            catalog_path=self.catalog_path,
            publication_generation=int(row['publication_generation']),
            snapshot_id=str(row['snapshot_id']),
            build_id=str(row['build_id']),
            profile_id=str(row['profile_id']),
            snapshot_path=snapshot_path,
            descriptor=SnapshotDescriptor(
                sha256=str(row['file_sha256']),
                size_bytes=int(row['size_bytes']),
                dimension=int(row['dimension']),
                metric=str(row['metric']),
                ntotal=int(row['ntotal']),
            ),
        )
        with self._connection_lock:
            self._revision_cache[identity] = revision
            while len(self._revision_cache) > 8:
                self._revision_cache.pop(next(iter(self._revision_cache)))
        return revision

    def _read_active_view(self, connection: sqlite3.Connection) -> _PinnedRevision:
        base_revision = self._read_active_revision(connection)
        overlay = read_active_delta_overlay(
            connection,
            base_snapshot_id=base_revision.snapshot_id,
            base_publication_generation=base_revision.publication_generation,
        )
        if not overlay.heads:
            return _PinnedRevision(
                base_revision,
                base_revision,
                overlay,
                (),
            )
        active_segment_ids = {
            head.segment_id
            for head in overlay.heads
            if head.action == 'upsert'
        }
        delta_revisions: list[tuple[DeltaSegmentRecord, SnapshotRevision]] = []
        for segment in overlay.segments:
            if segment.segment_id not in active_segment_ids or segment.descriptor.ntotal == 0:
                continue
            if (
                segment.descriptor.dimension != base_revision.descriptor.dimension
                or segment.descriptor.metric != base_revision.descriptor.metric
            ):
                raise SnapshotValidationError(
                    'delta segment descriptor does not match the active base profile'
                )
            if segment.relative_path is None:
                raise SnapshotValidationError(
                    'non-empty delta segment is missing its artifact path'
                )
            relative_path = PurePosixPath(segment.relative_path)
            artifact_path = self.data_root.joinpath(*relative_path.parts).resolve()
            try:
                artifact_path.relative_to(self.data_root)
            except ValueError as exc:
                raise SnapshotValidationError(
                    'delta segment path escapes the selected data root'
                ) from exc
            delta_revisions.append(
                (
                    segment,
                    SnapshotRevision(
                        catalog_path=self.catalog_path,
                        publication_generation=base_revision.publication_generation,
                        snapshot_id=segment.segment_id,
                        build_id=base_revision.build_id,
                        profile_id=base_revision.profile_id,
                        snapshot_path=artifact_path,
                        descriptor=segment.descriptor,
                    ),
                )
            )
        revision = SnapshotRevision(
            catalog_path=base_revision.catalog_path,
            publication_generation=base_revision.publication_generation,
            snapshot_id=base_revision.snapshot_id,
            build_id=base_revision.build_id,
            profile_id=base_revision.profile_id,
            snapshot_path=base_revision.snapshot_path,
            descriptor=base_revision.descriptor,
            delta_generation=overlay.generation,
            delta_segment_count=len(overlay.segments),
        )
        return _PinnedRevision(
            revision,
            base_revision,
            overlay,
            tuple(delta_revisions),
        )

    def _validate_membership(
        self,
        connection: sqlite3.Connection,
        revision: SnapshotRevision,
    ) -> None:
        row = connection.execute(
            '''
            SELECT
                count(*) AS membership_count,
                count(DISTINCT membership.faiss_id) AS physical_id_count,
                count(DISTINCT membership.chunk_uid) AS logical_chunk_count,
                count(chunk.chunk_uid) AS chunk_count,
                count(parent.parent_uid) AS parent_count,
                count(report.report_id) AS report_count,
                min(membership.faiss_id) AS minimum_id,
                max(membership.faiss_id) AS maximum_id,
                sum(
                    CASE
                        WHEN chunk.profile_id = ? AND parent.profile_id = ?
                        THEN 0 ELSE 1
                    END
                ) AS profile_mismatches
            FROM snapshot_membership AS membership
            LEFT JOIN retrieval_chunks AS chunk
              ON chunk.chunk_uid = membership.chunk_uid
            LEFT JOIN retrieval_parents AS parent
              ON parent.parent_uid = chunk.parent_uid
             AND parent.profile_id = chunk.profile_id
            LEFT JOIN reports AS report
              ON report.report_id = parent.report_id
            WHERE membership.snapshot_id = ?
            ''',
            (
                revision.profile_id,
                revision.profile_id,
                revision.snapshot_id,
            ),
        ).fetchone()
        count = int(row['membership_count'])
        expected = revision.descriptor.ntotal
        if (
            count != expected
            or int(row['physical_id_count']) != count
            or int(row['logical_chunk_count']) != count
            or int(row['chunk_count']) != count
            or int(row['parent_count']) != count
            or int(row['report_count']) != count
            or int(row['profile_mismatches'] or 0) != 0
        ):
            raise SnapshotValidationError(
                'snapshot membership does not match the active descriptor/catalog'
            )
        if expected == 0:
            if row['minimum_id'] is not None or row['maximum_id'] is not None:
                raise SnapshotValidationError('empty snapshot has physical membership')
        elif int(row['minimum_id']) != 1 or int(row['maximum_id']) != expected:
            raise SnapshotValidationError(
                'snapshot membership physical IDs must be dense 1..N'
            )

    def _validate_revision(
        self,
        connection: sqlite3.Connection,
        revision: SnapshotRevision,
    ) -> None:
        self._validate_snapshot_file(revision)
        self._validate_membership(connection, revision)

    def _validate_delta_revision(
        self,
        connection: sqlite3.Connection,
        segment: DeltaSegmentRecord,
        revision: SnapshotRevision,
    ) -> None:
        row = connection.execute(
            '''
            SELECT base_snapshot_id, base_publication_generation, sequence,
                   relative_path, file_sha256, size_bytes, dimension, metric,
                   ntotal, state
            FROM retrieval_delta_segments
            WHERE segment_id = ?
            ''',
            (segment.segment_id,),
        ).fetchone()
        expected = (
            self._read_active_revision(connection).snapshot_id,
            revision.publication_generation,
            segment.sequence,
            segment.relative_path,
            segment.descriptor.sha256,
            segment.descriptor.size_bytes,
            segment.descriptor.dimension,
            segment.descriptor.metric,
            segment.descriptor.ntotal,
            'ready',
        )
        if row is None or tuple(row) != expected:
            raise SnapshotValidationError(
                'delta segment changed inside the pinned request transaction'
            )
        self._validate_snapshot_file(revision)
        counts = connection.execute(
            '''
            SELECT count(*), count(DISTINCT faiss_id), count(DISTINCT chunk_uid),
                   min(faiss_id), max(faiss_id)
            FROM retrieval_delta_membership
            WHERE segment_id = ?
            ''',
            (segment.segment_id,),
        ).fetchone()
        expected_total = revision.descriptor.ntotal
        if tuple(counts) != (
            expected_total,
            expected_total,
            expected_total,
            1,
            expected_total,
        ):
            raise SnapshotValidationError(
                'delta membership does not match its artifact descriptor'
            )

    @staticmethod
    def _validate_snapshot_file(revision: SnapshotRevision) -> None:
        path = revision.snapshot_path
        if not path.is_file():
            raise SnapshotValidationError('active snapshot file is missing')
        if path.stat().st_size != revision.descriptor.size_bytes:
            raise SnapshotValidationError(
                'active snapshot size does not match its catalog descriptor'
            )
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != revision.descriptor.sha256.lower():
            raise SnapshotValidationError(
                'active snapshot hash does not match its catalog descriptor'
            )


RetrievalRepository = CatalogRepository


def compile_scope_filters(
    scope: SearchScope | Mapping[str, object] | None,
) -> CompiledScope:
    '''Compile current SearchScope fields into SQL plus bound parameters.'''

    if scope is None:
        return _UNFILTERED_SCOPE
    if isinstance(scope, SearchScope):
        values: dict[str, object] = {
            key: value
            for key, value in vars(scope).items()
            if value not in (None, (), False)
        }
    elif isinstance(scope, Mapping):
        values = dict(scope)
    else:
        raise ScopeValidationError('scope must be a mapping, SearchScope, or None')

    if not values:
        return _UNFILTERED_SCOPE
    empty = values.pop('empty', False)
    if not isinstance(empty, bool):
        raise ScopeValidationError('empty must be a boolean')
    if empty:
        return _EMPTY_SCOPE

    prior_scope = values.pop('prior_scope', None)
    if prior_scope is not None:
        if not isinstance(prior_scope, Mapping):
            raise ScopeValidationError('prior_scope must be a mapping')
        prior_file_fields = 0
        for source_key, target_key in (
            ('file_name', 'prior_file_name'),
            ('file_names', 'prior_file_names'),
            ('canonical_relative_path', 'prior_path'),
            ('canonical_relative_paths', 'prior_paths'),
            ('file_path', 'prior_path'),
            ('file_paths', 'prior_paths'),
            ('path', 'prior_path'),
            ('paths', 'prior_paths'),
        ):
            if source_key in prior_scope:
                prior_file_fields += 1
                if target_key in values:
                    raise ScopeValidationError(
                        f'conflicting explicit and prior scope field: {target_key}'
                    )
                values[target_key] = prior_scope[source_key]
        if not prior_file_fields:
            raise ScopeValidationError(
                'prior_scope must contain an explicit file or path scope'
            )

    for ignored in ('scope_source', 'reason', 'matched_section_id'):
        values.pop(ignored, None)

    clauses: list[str] = []
    parameters: list[object] = []
    provably_empty = False

    company = _pop_alias(values, ('company', 'company_name'))
    if company is not None:
        if isinstance(company, bool):
            if not company:
                raise ScopeValidationError(
                    'company=false is ambiguous; use report_type explicitly'
                )
            clauses.append('report.report_type = ?')
            parameters.append('company')
        else:
            clauses.extend(
                ('report.report_type = ?', 'report.target_name = ?')
            )
            parameters.extend(('company', _scope_text(company, 'company')))

    for aliases, column in (
        (('target_name', 'target'), 'report.target_name'),
        (('report_type',), 'report.report_type'),
        (('broker',), 'report.broker'),
        (('report_date', 'date'), 'report.report_date'),
    ):
        scalar = _pop_alias(values, aliases)
        if scalar is not None:
            clauses.append(f'{column} = ?')
            parameters.append(_scope_text(scalar, aliases[0]))

    for aliases, column in (
        (('target_names', 'targets'), 'report.target_name'),
        (('report_types',), 'report.report_type'),
        (('brokers',), 'report.broker'),
    ):
        sequence, present = _pop_sequence(values, aliases)
        if present:
            if not sequence:
                provably_empty = True
            else:
                _append_in_clause(clauses, parameters, column, sequence)

    date_start = _pop_alias(values, ('report_date_start', 'date_start'))
    date_end = _pop_alias(values, ('report_date_end', 'date_end'))
    normalized_start = (
        None if date_start is None else _scope_text(date_start, 'report_date_start')
    )
    normalized_end = (
        None if date_end is None else _scope_text(date_end, 'report_date_end')
    )
    if normalized_start is not None and normalized_end is not None:
        if normalized_start > normalized_end:
            provably_empty = True
    if normalized_start is not None:
        clauses.append('report.report_date >= ?')
        parameters.append(normalized_start)
    if normalized_end is not None:
        clauses.append('report.report_date <= ?')
        parameters.append(normalized_end)

    for aliases in (
        ('canonical_relative_path', 'path', 'file_path'),
        ('prior_path', 'prior_file_path'),
    ):
        path_value = _pop_alias(values, aliases)
        if path_value is not None:
            clauses.append('report.canonical_relative_path = ?')
            parameters.append(_relative_scope_path(path_value, aliases[0]))

    for aliases in (
        ('canonical_relative_paths', 'paths', 'file_paths'),
        ('prior_paths', 'prior_file_paths'),
    ):
        paths, present = _pop_sequence(values, aliases)
        if present:
            if not paths:
                provably_empty = True
            else:
                _append_in_clause(
                    clauses,
                    parameters,
                    'report.canonical_relative_path',
                    tuple(_relative_scope_path(path, aliases[0]) for path in paths),
                )

    for aliases in (
        ('file_name',),
        ('file_names',),
        ('prior_file_name',),
        ('prior_file_names',),
    ):
        if len(aliases) == 1 and aliases[0] in ('file_name', 'prior_file_name'):
            scalar = _pop_alias(values, aliases)
            if scalar is None:
                continue
            file_names = (_scope_text(scalar, aliases[0]),)
            present = True
        else:
            file_names, present = _pop_sequence(values, aliases)
        if not present:
            continue
        if not file_names:
            provably_empty = True
        else:
            _append_file_scope(clauses, parameters, file_names)

    if values:
        raise ScopeValidationError(
            'unsupported scope fields: ' + ', '.join(sorted(str(key) for key in values))
        )
    if len(parameters) > MAX_SCOPE_VALUES:
        raise ScopeValidationError(
            f'scope contains more than {MAX_SCOPE_VALUES} bound values'
        )
    if provably_empty:
        return _EMPTY_SCOPE
    if not clauses:
        return _UNFILTERED_SCOPE
    return CompiledScope(
        ' AND '.join(f'({clause})' for clause in clauses),
        tuple(parameters),
        False,
        False,
    )


compile_search_scope = compile_scope_filters


def _pop_alias(values: dict[str, object], aliases: Sequence[str]) -> object | None:
    present = [alias for alias in aliases if alias in values]
    if len(present) > 1:
        raise ScopeValidationError(
            'conflicting aliases: ' + ', '.join(present)
        )
    if not present:
        return None
    return values.pop(present[0])


def _pop_sequence(
    values: dict[str, object],
    aliases: Sequence[str],
) -> tuple[tuple[str, ...], bool]:
    raw = _pop_alias(values, aliases)
    if raw is None:
        return (), False
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ScopeValidationError(f'{aliases[0]} must be a sequence')
    normalized = tuple(
        dict.fromkeys(_scope_text(value, aliases[0]) for value in raw)
    )
    return normalized, True


def _scope_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScopeValidationError(f'{field_name} must contain non-empty text')
    return value.strip()


def _relative_scope_path(value: object, field_name: str) -> str:
    path = _scope_text(value, field_name).replace('\\', '/')
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in ('', '.', '..') for part in pure.parts)
        or (pure.parts and pure.parts[0].endswith(':'))
    ):
        raise ScopeValidationError(f'{field_name} must be a canonical relative path')
    return pure.as_posix()


def _append_in_clause(
    clauses: list[str],
    parameters: list[object],
    column: str,
    values: Sequence[str],
) -> None:
    if len(values) == 1:
        clauses.append(f'{column} = ?')
    else:
        placeholders = ','.join('?' for _ in values)
        clauses.append(f'{column} IN ({placeholders})')
    parameters.extend(values)


def _append_file_scope(
    clauses: list[str],
    parameters: list[object],
    file_names: Sequence[str],
) -> None:
    alternatives: list[str] = []
    for value in file_names:
        normalized = value.replace('\\', '/')
        if '/' in normalized:
            alternatives.append('report.canonical_relative_path = ?')
            parameters.append(_relative_scope_path(normalized, 'file_name'))
            continue
        if normalized in ('.', '..'):
            raise ScopeValidationError('file_name must name a file')
        alternatives.append(
            '''(
                report.canonical_relative_path = ?
                OR report.canonical_relative_path LIKE ? ESCAPE '\\'
            )'''
        )
        parameters.extend((normalized, f'%/{_escape_like(normalized)}'))
    clauses.append('(' + ' OR '.join(alternatives) + ')')


def _escape_like(value: str) -> str:
    return value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _batches(values: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _reject_duplicate_physical_ids(candidates: Sequence[RankedCandidate]) -> None:
    ids = [candidate.physical_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise SnapshotValidationError('ranked candidates contain duplicate physical IDs')


def _validate_loaded_index(
    index: RawVectorIndex,
    descriptor: SnapshotDescriptor,
) -> None:
    try:
        dimension = int(index.dimension)
        ntotal = int(index.ntotal)
        metric = str(index.metric)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SnapshotValidationError(
            'snapshot loader did not return a valid vector index'
        ) from exc
    if (
        dimension != descriptor.dimension
        or ntotal != descriptor.ntotal
        or metric != descriptor.metric
    ):
        raise SnapshotValidationError(
            'loaded vector index does not match the catalog descriptor'
        )


def _hydration_payload(row: sqlite3.Row) -> _HydrationPayload:
    return _HydrationPayload(
        chunk_uid=str(row['chunk_uid']),
        parent_uid=str(row['parent_uid']),
        report_uid=str(row['report_uid']),
        report_id=int(row['report_id']),
        profile_id=str(row['profile_id']),
        child_order=int(row['child_order']),
        span_start=int(row['span_start']),
        span_end=int(row['span_end']),
        parent_slice=str(row['parent_slice']),
        canonical_relative_path=str(row['canonical_relative_path']),
        source_sha256=str(row['source_sha256']),
        retrieval_metadata_sha256=str(row['retrieval_metadata_sha256']),
        report_type=str(row['report_type']),
        report_date=str(row['report_date']),
        target_name=None if row['target_name'] is None else str(row['target_name']),
        title=str(row['title']),
        broker=str(row['broker']),
    )


__all__ = [
    'CachedSnapshotHandle',
    'CatalogRepository',
    'CompiledScope',
    'CrossSnapshotMembershipError',
    'LeaseReleasedError',
    'RankedCandidate',
    'RepositoryError',
    'RetrievalRepository',
    'RetrievedChunk',
    'ScopeValidationError',
    'SearchScope',
    'SnapshotCache',
    'SnapshotInUseError',
    'SnapshotRevision',
    'SnapshotSession',
    'SnapshotUnavailableError',
    'SnapshotValidationError',
    'compile_scope_filters',
    'compile_search_scope',
    'shared_snapshot_cache',
]
