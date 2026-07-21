'''Native V2 reader with bounded selector and adaptive broad-scope search.'''

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

import numpy as np

from src.retrieval.repository import (
    CatalogRepository,
    CompiledScope,
    RetrievedChunk,
    SearchScope,
    SnapshotRevision,
    SnapshotSession,
    compile_scope_filters,
)


class SearchStrategy(str, Enum):
    EMPTY = 'empty'
    DIRECT = 'direct'
    SELECTOR = 'selector'
    ADAPTIVE = 'adaptive'


@dataclass(frozen=True, slots=True)
class SearchTimings:
    '''Nanosecond timings that contain no query text or report content.'''

    scope_compile_ns: int
    eligibility_ns: int
    faiss_ns: int
    hydration_ns: int
    lease_ns: int
    total_ns: int


@dataclass(frozen=True, slots=True)
class SearchResponse:
    '''Results plus evidence about the bounded strategy used for the request.'''

    revision: SnapshotRevision
    strategy: SearchStrategy
    eligible_count: int
    snapshot_total: int
    faiss_calls: int
    faiss_fetch_k: int
    candidate_count: int
    hydration_batches: int
    hydration_rows: int
    hydration_cache_hits: int
    hydration_cache_misses: int
    results: tuple[RetrievedChunk, ...]
    timings: SearchTimings | None = None

    @property
    def chunks(self) -> tuple[RetrievedChunk, ...]:
        return self.results

    @property
    def hits(self) -> tuple[RetrievedChunk, ...]:
        return self.results


class NativeRetrievalReader:
    '''Search one leased revision from eligibility through native hydration.'''

    def __init__(
        self,
        repository: CatalogRepository,
        *,
        selector_max_ids: int = 2048,
        selector_max_fraction: float = 0.25,
        adaptive_growth: float = 2.0,
        adaptive_initial_multiplier: int = 2,
    ) -> None:
        if (
            not isinstance(selector_max_ids, int)
            or isinstance(selector_max_ids, bool)
            or selector_max_ids <= 0
        ):
            raise ValueError('selector_max_ids must be a positive integer')
        if not 0.0 < selector_max_fraction < 1.0:
            raise ValueError('selector_max_fraction must be between zero and one')
        if adaptive_growth <= 1.0:
            raise ValueError('adaptive_growth must be greater than one')
        if (
            not isinstance(adaptive_initial_multiplier, int)
            or isinstance(adaptive_initial_multiplier, bool)
            or adaptive_initial_multiplier <= 0
        ):
            raise ValueError(
                'adaptive_initial_multiplier must be a positive integer'
            )
        self.repository = repository
        self.selector_max_ids = selector_max_ids
        self.selector_max_fraction = selector_max_fraction
        self.adaptive_growth = float(adaptive_growth)
        self.adaptive_initial_multiplier = adaptive_initial_multiplier

    def search(
        self,
        query: np.ndarray,
        k: int,
        scope: SearchScope | Mapping[str, object] | None = None,
    ) -> SearchResponse:
        if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
            raise ValueError('k must be a positive integer')
        total_started = time.perf_counter_ns()
        compile_started = time.perf_counter_ns()
        compiled = compile_scope_filters(scope)
        scope_compile_ns = time.perf_counter_ns() - compile_started
        counters = {'eligibility_ns': 0, 'faiss_ns': 0, 'hydration_ns': 0}

        lease_started = time.perf_counter_ns()
        with self.repository.request() as session:
            eligibility_started = time.perf_counter_ns()
            eligible_count = session.eligible_count(compiled)
            counters['eligibility_ns'] += time.perf_counter_ns() - eligibility_started
            strategy = self._choose_strategy(
                compiled,
                eligible_count,
                session.revision.descriptor.ntotal,
            )
            if strategy is SearchStrategy.EMPTY:
                response = self._response(
                    session,
                    strategy,
                    eligible_count,
                    faiss_calls=0,
                    faiss_fetch_k=0,
                    candidate_count=0,
                    results=(),
                )
            elif strategy is SearchStrategy.DIRECT:
                faiss_started = time.perf_counter_ns()
                candidates = session.search_index(query, k, allowed_ids=None)
                counters['faiss_ns'] += time.perf_counter_ns() - faiss_started
                hydration_started = time.perf_counter_ns()
                # Every direct candidate is eligible: either the scope is
                # unfiltered or the same transaction proved it covers N rows.
                results = session.hydrate_search_batch(candidates)
                counters['hydration_ns'] += time.perf_counter_ns() - hydration_started
                response = self._response(
                    session,
                    strategy,
                    eligible_count,
                    faiss_calls=1,
                    faiss_fetch_k=k,
                    candidate_count=len(candidates),
                    results=results,
                )
            elif strategy is SearchStrategy.SELECTOR:
                eligibility_started = time.perf_counter_ns()
                allowed_ids = session.eligible_physical_ids(
                    compiled,
                    expected_count=eligible_count,
                )
                counters['eligibility_ns'] += (
                    time.perf_counter_ns() - eligibility_started
                )
                faiss_started = time.perf_counter_ns()
                candidates = session.search_index(
                    query,
                    k,
                    allowed_ids=allowed_ids,
                )
                counters['faiss_ns'] += time.perf_counter_ns() - faiss_started
                hydration_started = time.perf_counter_ns()
                # The selector was compiled from this session's eligible IDs.
                results = session.hydrate_search_batch(candidates)
                counters['hydration_ns'] += time.perf_counter_ns() - hydration_started
                response = self._response(
                    session,
                    strategy,
                    eligible_count,
                    faiss_calls=1,
                    faiss_fetch_k=k,
                    candidate_count=len(candidates),
                    results=results,
                )
            else:
                response = self._adaptive_search(
                    session,
                    query,
                    k,
                    compiled,
                    eligible_count,
                    counters,
                )
        lease_ns = time.perf_counter_ns() - lease_started
        object.__setattr__(
            response,
            'timings',
            SearchTimings(
                scope_compile_ns,
                counters['eligibility_ns'],
                counters['faiss_ns'],
                counters['hydration_ns'],
                lease_ns,
                time.perf_counter_ns() - total_started,
            ),
        )
        return response

    def _choose_strategy(
        self,
        scope: CompiledScope,
        eligible_count: int,
        snapshot_total: int,
    ) -> SearchStrategy:
        if scope.is_empty or eligible_count == 0 or snapshot_total == 0:
            return SearchStrategy.EMPTY
        if scope.is_unfiltered or eligible_count == snapshot_total:
            return SearchStrategy.DIRECT
        fraction = eligible_count / snapshot_total
        if (
            eligible_count <= self.selector_max_ids
            and fraction <= self.selector_max_fraction
        ):
            return SearchStrategy.SELECTOR
        return SearchStrategy.ADAPTIVE

    def _adaptive_search(
        self,
        session: SnapshotSession,
        query: np.ndarray,
        k: int,
        scope: CompiledScope,
        eligible_count: int,
        counters: dict[str, int],
    ) -> SearchResponse:
        total = session.revision.descriptor.ntotal
        target = min(k, eligible_count)
        fetch_k = min(
            total,
            max(k, k * self.adaptive_initial_multiplier),
        )
        faiss_calls = 0
        filtered = ()
        tagged = ()

        while True:
            faiss_started = time.perf_counter_ns()
            raw = session.index.search(query, fetch_k, allowed_ids=None)
            counters['faiss_ns'] += time.perf_counter_ns() - faiss_started
            faiss_calls += 1
            tagged = session.tag_results(raw)
            eligibility_started = time.perf_counter_ns()
            filtered = session.filter_candidates(tagged, scope)
            counters['eligibility_ns'] += (
                time.perf_counter_ns() - eligibility_started
            )
            if len(filtered) >= target or fetch_k >= total:
                break
            next_fetch = min(
                total,
                max(fetch_k + 1, int(math.ceil(fetch_k * self.adaptive_growth))),
            )
            if next_fetch == fetch_k:
                break
            fetch_k = next_fetch

        selected = filtered[:target]
        hydration_started = time.perf_counter_ns()
        # ``filter_candidates`` proved eligibility in this same transaction.
        results = session.hydrate(selected)
        counters['hydration_ns'] += time.perf_counter_ns() - hydration_started
        return self._response(
            session,
            SearchStrategy.ADAPTIVE,
            eligible_count,
            faiss_calls=faiss_calls,
            faiss_fetch_k=fetch_k,
            candidate_count=len(tagged),
            results=results,
        )

    def _response(
        self,
        session: SnapshotSession,
        strategy: SearchStrategy,
        eligible_count: int,
        *,
        faiss_calls: int,
        faiss_fetch_k: int,
        candidate_count: int,
        results: tuple[RetrievedChunk, ...],
    ) -> SearchResponse:
        return SearchResponse(
            revision=session.revision,
            strategy=strategy,
            eligible_count=eligible_count,
            snapshot_total=session.revision.descriptor.ntotal,
            faiss_calls=faiss_calls,
            faiss_fetch_k=faiss_fetch_k,
            candidate_count=candidate_count,
            hydration_batches=session.hydration_sql_batches,
            hydration_rows=session.hydration_sql_rows,
            hydration_cache_hits=session.hydration_cache_hits,
            hydration_cache_misses=session.hydration_cache_misses,
            results=results,
        )


NativeV2Reader = NativeRetrievalReader
V2Reader = NativeRetrievalReader


__all__ = [
    'NativeRetrievalReader',
    'NativeV2Reader',
    'SearchResponse',
    'SearchStrategy',
    'SearchTimings',
    'V2Reader',
]
