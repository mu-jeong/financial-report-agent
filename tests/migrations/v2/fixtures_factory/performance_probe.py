"""Fresh-process synthetic adapter used to prove the benchmark runner itself."""

from __future__ import annotations

import hashlib

from src.migrations.v2.validation.benchmark_runner import (
    BenchmarkFactory,
    FixedBenchmarkQuery,
    ProbeTelemetry,
)
from src.migrations.v2.validation.performance import REQUIRED_WORKLOADS


_STRATEGIES = {
    "unfiltered": "direct",
    "empty": "empty",
    "narrow": "selector",
    "broad": "adaptive",
    "near_universe": "adaptive",
    "prior_scope": "selector",
}


def create_factory(
    *,
    process_id: str,
    seed: int,
    engine: str = "paired",
) -> BenchmarkFactory:
    del process_id, seed
    queries = {
        workload: tuple(
            FixedBenchmarkQuery(
                hashlib.sha256(f"fixture-query-{number}".encode()).hexdigest(),
                number,
            )
            for number in range(30)
        )
        for workload in REQUIRED_WORKLOADS
    }
    return BenchmarkFactory(
        queries=queries,
        v1_probe=_v1_probe,
        v2_probe=_v2_probe,
        environment={
            "os": "fixture",
            "runtime": "synthetic-probe-v1",
            "cache_state": "warm",
        },
        engine=engine,
    )


def _v1_probe(workload: str, payload: int) -> ProbeTelemetry:
    _burn(payload, 4_000)
    return _telemetry(workload, engine="v1")


def _v2_probe(workload: str, payload: int) -> ProbeTelemetry:
    _burn(payload, 200)
    return _telemetry(workload, engine="v2")


def _burn(payload: int, count: int) -> int:
    value = payload
    for number in range(count):
        value = (value * 33 + number) & 0xFFFFFFFF
    return value


def _telemetry(workload: str, *, engine: str) -> ProbeTelemetry:
    empty = workload == "empty"
    native_empty = empty and engine == "v2"
    return ProbeTelemetry(
        sql_ns=0 if engine == "v1" else 10,
        sql_rows=0 if empty else 5,
        strategy="compatibility" if engine == "v1" else _STRATEGIES[workload],
        faiss_ns=0 if native_empty else 20,
        faiss_calls=0 if native_empty else 1,
        faiss_candidates=0 if native_empty else 10,
        hydration_batches=0 if empty else 1,
        hydration_rows=0 if empty else 5,
        rerank_ns=3,
        lease_ns=0 if engine == "v1" else 40,
    )
