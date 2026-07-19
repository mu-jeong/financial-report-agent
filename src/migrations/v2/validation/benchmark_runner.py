"""Execute paired V1/V2 retrieval probes without retaining query payloads."""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from src.migrations.v2.validation.performance import (
    MIN_FIXED_QUERIES,
    MIN_TIMED_SAMPLES,
    MIN_WARMUP_COUNT,
    REQUIRED_WORKLOADS,
    TELEMETRY_FIELDS,
    PerformanceEvidenceError,
)


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


@dataclass(frozen=True)
class FixedBenchmarkQuery:
    """Opaque evidence identity plus an in-memory payload never written out."""

    query_id: str
    payload: Any

    def __post_init__(self) -> None:
        if not isinstance(self.query_id, str) or not _HEX_DIGEST.fullmatch(
            self.query_id
        ):
            raise PerformanceEvidenceError(
                "benchmark query_id must be an opaque lowercase SHA-256 digest"
            )


@dataclass(frozen=True)
class ProbeTelemetry:
    """Non-sensitive per-search counters supplied by a concrete reader adapter."""

    sql_ns: int
    sql_rows: int
    strategy: str
    faiss_ns: int
    faiss_calls: int
    faiss_candidates: int
    hydration_batches: int
    hydration_rows: int
    rerank_ns: int
    lease_ns: int
    hydration_cache_hits: int = 0
    hydration_cache_misses: int = 0

    def __post_init__(self) -> None:
        for field, value in vars(self).items():
            if field == "strategy":
                if not isinstance(value, str) or not _TOKEN.fullmatch(value):
                    raise PerformanceEvidenceError(
                        "probe strategy must be a stable lowercase token"
                    )
            elif (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise PerformanceEvidenceError(
                    f"probe telemetry {field} must be a non-negative integer"
                )

    def to_dict(self) -> dict[str, int | str]:
        return dict(vars(self))


Probe = Callable[[str, Any], ProbeTelemetry]
MemorySampler = Callable[[], int]


@dataclass(frozen=True)
class BenchmarkFactory:
    """Fresh-process adapter returned by a benchmark factory function."""

    queries: Mapping[str, Sequence[FixedBenchmarkQuery]]
    v1_probe: Probe
    v2_probe: Probe
    environment: Mapping[str, Any]
    engine: str = "paired"

    def __post_init__(self) -> None:
        if self.engine not in {"paired", "v1", "v2"}:
            raise PerformanceEvidenceError("benchmark factory engine is invalid")


def run_paired_benchmark_process(
    *,
    process_id: str,
    queries: Mapping[str, Sequence[FixedBenchmarkQuery]],
    v1_probe: Probe,
    v2_probe: Probe,
    warmup_count: int = MIN_WARMUP_COUNT,
    timed_sample_count: int = MIN_TIMED_SAMPLES,
    seed: int = 20260716,
    memory_sampler: MemorySampler | None = None,
) -> dict[str, Any]:
    """Run every required workload in one process and retain paired raw samples.

    Probe order is deterministically randomized per pair to reduce order bias.
    Only opaque query IDs, timings, counters, and strategy tokens are returned.
    """

    if not isinstance(process_id, str) or not process_id.strip():
        raise PerformanceEvidenceError("process_id must be a non-empty string")
    if set(queries) != set(REQUIRED_WORKLOADS):
        raise PerformanceEvidenceError(
            "benchmark runner requires exactly the declared workload classes"
        )
    if warmup_count < MIN_WARMUP_COUNT:
        raise PerformanceEvidenceError(
            f"benchmark runner requires at least {MIN_WARMUP_COUNT} warmups"
        )
    if timed_sample_count < MIN_TIMED_SAMPLES:
        raise PerformanceEvidenceError(
            f"benchmark runner requires at least {MIN_TIMED_SAMPLES} timed samples"
        )
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise PerformanceEvidenceError("benchmark seed must be an integer")
    sample_memory = memory_sampler or process_memory_high_water_bytes
    generator = np.random.default_rng(seed)
    fixed_by_workload: dict[str, tuple[FixedBenchmarkQuery, ...]] = {}
    for workload in REQUIRED_WORKLOADS:
        fixed_queries = tuple(queries[workload])
        query_ids = {query.query_id for query in fixed_queries}
        if len(query_ids) < MIN_FIXED_QUERIES:
            raise PerformanceEvidenceError(
                f"{workload} needs at least {MIN_FIXED_QUERIES} fixed opaque queries"
            )
        fixed_by_workload[workload] = fixed_queries

    workload_results: dict[str, Any] = {}
    for workload in REQUIRED_WORKLOADS:
        fixed_queries = fixed_by_workload[workload]

        for number in range(warmup_count):
            query = fixed_queries[number % len(fixed_queries)]
            if bool(generator.integers(0, 2)):
                v1_probe(workload, query.payload)
                v2_probe(workload, query.payload)
            else:
                v2_probe(workload, query.payload)
                v1_probe(workload, query.payload)

        samples = _empty_samples(warmup_count)
        for number in range(timed_sample_count):
            query = fixed_queries[number % len(fixed_queries)]
            if bool(generator.integers(0, 2)):
                v1 = _measure_probe(v1_probe, workload, query.payload, sample_memory)
                v2 = _measure_probe(v2_probe, workload, query.payload, sample_memory)
            else:
                v2 = _measure_probe(v2_probe, workload, query.payload, sample_memory)
                v1 = _measure_probe(v1_probe, workload, query.payload, sample_memory)
            samples["query_ids"].append(query.query_id)
            _append_measurement(samples, "v1", v1)
            _append_measurement(samples, "v2", v2)
        workload_results[workload] = samples

    return {
        "process_id": process_id,
        "seed": seed,
        "workloads": workload_results,
    }


def run_isolated_cold_probe(
    *,
    process_id: str,
    engine: str,
    seed: int,
    queries: Mapping[str, Sequence[FixedBenchmarkQuery]],
    probe: Probe,
    factory_init_ns: int,
    factory_memory_high_water_bytes: int,
    memory_sampler: MemorySampler | None = None,
) -> dict[str, Any]:
    """Measure one engine's initialization and first probe in its own process."""

    if not isinstance(process_id, str) or not process_id.strip():
        raise PerformanceEvidenceError("cold process_id must be a non-empty string")
    if engine not in {"v1", "v2"}:
        raise PerformanceEvidenceError("cold benchmark engine must be v1 or v2")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise PerformanceEvidenceError("cold benchmark seed must be an integer")
    for label, value in (
        ("factory_init_ns", factory_init_ns),
        ("factory_memory_high_water_bytes", factory_memory_high_water_bytes),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PerformanceEvidenceError(
                f"cold benchmark {label} must be a positive integer"
            )
    if set(queries) != set(REQUIRED_WORKLOADS):
        raise PerformanceEvidenceError(
            "cold benchmark requires exactly the declared workload classes"
        )
    fixed_queries = tuple(queries["unfiltered"])
    if len({query.query_id for query in fixed_queries}) < MIN_FIXED_QUERIES:
        raise PerformanceEvidenceError(
            f"cold benchmark needs at least {MIN_FIXED_QUERIES} fixed opaque queries"
        )
    query = fixed_queries[0]
    measurement = _measure_probe(
        probe,
        "unfiltered",
        query.payload,
        memory_sampler or process_memory_high_water_bytes,
    )
    return {
        "engine": engine,
        "process_id": process_id,
        "seed": seed,
        "workload": "unfiltered",
        "query_id": query.query_id,
        "factory_init_ns": factory_init_ns,
        "factory_memory_high_water_bytes": factory_memory_high_water_bytes,
        "total_cold_ns": factory_init_ns + int(measurement["total_ns"]),
        "probe": measurement,
    }


def process_memory_high_water_bytes() -> int:
    """Return the process peak working set using only the standard library."""

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        success = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        if not success or counters.PeakWorkingSetSize <= 0:
            raise PerformanceEvidenceError("Windows process memory high-water is unavailable")
        return int(counters.PeakWorkingSetSize)

    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform != "darwin":
            peak *= 1024
    except (ImportError, OSError, ValueError) as exc:
        raise PerformanceEvidenceError(
            "process memory high-water is unavailable"
        ) from exc
    if peak <= 0:
        raise PerformanceEvidenceError("process memory high-water is invalid")
    return peak


def _measure_probe(
    probe: Probe,
    workload: str,
    payload: Any,
    memory_sampler: MemorySampler,
) -> dict[str, int | str]:
    started = time.perf_counter_ns()
    telemetry = probe(workload, payload)
    total_ns = max(1, time.perf_counter_ns() - started)
    if not isinstance(telemetry, ProbeTelemetry):
        raise PerformanceEvidenceError("benchmark probe must return ProbeTelemetry")
    memory = memory_sampler()
    if not isinstance(memory, int) or isinstance(memory, bool) or memory <= 0:
        raise PerformanceEvidenceError(
            "memory sampler must return a positive integer byte count"
        )
    return {
        "total_ns": total_ns,
        **telemetry.to_dict(),
        "memory_high_water_bytes": memory,
    }


def _empty_samples(warmup_count: int) -> dict[str, Any]:
    samples: dict[str, Any] = {
        "warmup_count": warmup_count,
        "query_ids": [],
        "v1_ns": [],
        "v2_ns": [],
    }
    for engine in ("v1", "v2"):
        for field in TELEMETRY_FIELDS:
            samples[f"{engine}_{field}"] = []
    return samples


def _append_measurement(
    samples: dict[str, Any],
    engine: str,
    measurement: Mapping[str, int | str],
) -> None:
    samples[f"{engine}_ns"].append(measurement["total_ns"])
    for field in TELEMETRY_FIELDS:
        samples[f"{engine}_{field}"].append(measurement[field])


__all__ = [
    "BenchmarkFactory",
    "FixedBenchmarkQuery",
    "Probe",
    "ProbeTelemetry",
    "process_memory_high_water_bytes",
    "run_isolated_cold_probe",
    "run_paired_benchmark_process",
]
