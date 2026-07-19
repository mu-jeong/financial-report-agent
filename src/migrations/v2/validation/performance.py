"""Deterministic analysis for the V1/V2 paired retrieval latency gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from src.migrations.v2.validation.benchmark_provenance import (
    BenchmarkProvenanceError,
    validate_benchmark_provenance,
)
from src.retrieval.identity import canonical_json


REQUIRED_WORKLOADS = (
    "unfiltered",
    "empty",
    "narrow",
    "broad",
    "near_universe",
    "prior_scope",
)
MIN_PROCESS_COUNT = 3
MIN_WARMUP_COUNT = 10
MIN_TIMED_SAMPLES = 200
MIN_SUCCESSOR_TIMED_SAMPLES = 4_000
MIN_FIXED_QUERIES = 30
MIN_BOOTSTRAP_RESAMPLES = 500
MIN_SUCCESSOR_BOOTSTRAP_RESAMPLES = 10_000
EPOCH_ZERO_PROTOCOL_PROFILE = "epoch_zero"
SUCCESSOR_RELEASE_PROTOCOL_PROFILE = "successor_release"
PROTOCOL_PROFILES = (
    EPOCH_ZERO_PROTOCOL_PROFILE,
    SUCCESSOR_RELEASE_PROTOCOL_PROFILE,
)
MAX_COMBINED_CI_RATIO = 1.10
MAX_PROCESS_RATIO = 1.15
_ABSOLUTE_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
TELEMETRY_FIELDS = (
    "sql_ns",
    "sql_rows",
    "strategy",
    "faiss_ns",
    "faiss_calls",
    "faiss_candidates",
    "hydration_batches",
    "hydration_rows",
    "hydration_cache_hits",
    "hydration_cache_misses",
    "rerank_ns",
    "memory_high_water_bytes",
    "lease_ns",
)
_INTEGER_TELEMETRY_FIELDS = tuple(
    field for field in TELEMETRY_FIELDS if field != "strategy"
)
_V2_STRATEGIES = frozenset({"empty", "direct", "selector", "adaptive"})
_STRATEGY_TOKEN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_DEFAULT_BENCHMARK_PAIR = "v1_compatibility_vs_native"
_SUCCESSOR_BENCHMARK_PAIR = "native_predecessor_vs_native_successor"


class PerformanceEvidenceError(ValueError):
    """Raised when benchmark evidence cannot satisfy the declared protocol."""


def analyze_benchmark(
    evidence: Mapping[str, Any],
    *,
    bootstrap_resamples: int = 10_000,
    bootstrap_seed: int = 20260716,
) -> dict[str, Any]:
    """Validate raw paired samples and evaluate every workload independently."""

    if not isinstance(evidence, Mapping):
        raise PerformanceEvidenceError("benchmark evidence must be an object")
    profile = _protocol_profile(evidence)
    raw_processes = evidence.get("processes")
    _validate_protocol_counts(
        profile,
        process_count=len(raw_processes) if isinstance(raw_processes, list) else 0,
        warmup_count=None,
        timed_sample_count=None,
        bootstrap_resamples=bootstrap_resamples,
    )
    _validate_top_level(evidence, profile)
    benchmark_pair = _benchmark_pair(evidence)
    expected_pair = (
        _SUCCESSOR_BENCHMARK_PAIR
        if profile == SUCCESSOR_RELEASE_PROTOCOL_PROFILE
        else _DEFAULT_BENCHMARK_PAIR
    )
    if benchmark_pair != expected_pair:
        raise PerformanceEvidenceError(
            f"benchmark protocol profile {profile} does not match {benchmark_pair}"
        )
    minimum_samples = _minimum_timed_samples(profile)
    processes = evidence["processes"]
    workload_results: dict[str, Any] = {}
    for workload in REQUIRED_WORKLOADS:
        baseline_parts: list[np.ndarray] = []
        native_parts: list[np.ndarray] = []
        process_results: list[dict[str, Any]] = []
        for process in processes:
            samples = process["workloads"][workload]
            _validate_samples(
                samples,
                workload,
                str(process["process_id"]),
                benchmark_pair,
                minimum_samples,
            )
            baseline = np.asarray(samples["v1_ns"], dtype=np.float64)
            native = np.asarray(samples["v2_ns"], dtype=np.float64)
            baseline_p95 = _p95(baseline)
            native_p95 = _p95(native)
            ratio = native_p95 / baseline_p95
            process_results.append(
                {
                    "process_id": str(process["process_id"]),
                    "sample_count": int(baseline.size),
                    "query_count": len(set(samples["query_ids"])),
                    "v1_p95_ns": baseline_p95,
                    "v2_p95_ns": native_p95,
                    "ratio": ratio,
                    "passed": ratio <= MAX_PROCESS_RATIO,
                }
            )
            baseline_parts.append(baseline)
            native_parts.append(native)

        combined_v1 = np.concatenate(baseline_parts)
        combined_v2 = np.concatenate(native_parts)
        workload_seed = bootstrap_seed + int.from_bytes(
            hashlib.sha256(workload.encode("utf-8")).digest()[:4],
            "big",
        )
        ci_low, ci_high = _paired_bootstrap_p95_ratio(
            combined_v1,
            combined_v2,
            resamples=bootstrap_resamples,
            seed=workload_seed,
        )
        combined_ratio = _p95(combined_v2) / _p95(combined_v1)
        passed = ci_high <= MAX_COMBINED_CI_RATIO and all(
            process["passed"] for process in process_results
        )
        workload_results[workload] = {
            "sample_count": int(combined_v1.size),
            "v1_p95_ns": _p95(combined_v1),
            "v2_p95_ns": _p95(combined_v2),
            "ratio": combined_ratio,
            "paired_bootstrap_ci_95": [ci_low, ci_high],
            "max_allowed_ci_upper": MAX_COMBINED_CI_RATIO,
            "max_allowed_process_ratio": MAX_PROCESS_RATIO,
            "processes": process_results,
            "passed": passed,
        }

    return {
        "schema_version": 1,
        "kind": "v2_retrieval_performance_analysis",
        "benchmark_pair": benchmark_pair,
        "protocol": {
            "profile": profile,
            "required_workloads": list(REQUIRED_WORKLOADS),
            "minimum_processes": MIN_PROCESS_COUNT,
            "process_count_policy": (
                "exact"
                if profile == SUCCESSOR_RELEASE_PROTOCOL_PROFILE
                else "minimum"
            ),
            "minimum_warmups": MIN_WARMUP_COUNT,
            "minimum_timed_samples_per_process_workload": minimum_samples,
            "minimum_fixed_queries_per_workload": MIN_FIXED_QUERIES,
            "minimum_bootstrap_resamples": _minimum_bootstrap_resamples(profile),
            "bootstrap_resamples": bootstrap_resamples,
            "bootstrap_seed": bootstrap_seed,
        },
        "workloads": workload_results,
        "passed": all(result["passed"] for result in workload_results.values()),
    }


def write_benchmark_evidence(
    path: str | Path,
    raw_evidence: Mapping[str, Any],
    analysis: Mapping[str, Any],
) -> Path:
    """Persist raw timings plus analysis without paths, query text, or secrets."""

    _validate_top_level(raw_evidence, _protocol_profile(raw_evidence))
    if analysis.get("kind") != "v2_retrieval_performance_analysis":
        raise PerformanceEvidenceError("performance analysis kind is invalid")
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"benchmark evidence already exists: {target.name}")
    payload = {
        "schema_version": 1,
        "kind": "v2_retrieval_performance_evidence",
        "raw": dict(raw_evidence),
        "analysis": dict(analysis),
    }
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".benchmark-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        temporary.unlink()
        os.chmod(target, stat.S_IREAD)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_benchmark_evidence(path: str | Path) -> Mapping[str, Any]:
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise PerformanceEvidenceError("benchmark input is missing or unsafe")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PerformanceEvidenceError("benchmark input is unreadable") from exc
    if not isinstance(value, dict):
        raise PerformanceEvidenceError("benchmark input must be a JSON object")
    return value


def _validate_top_level(evidence: Mapping[str, Any], profile: str) -> None:
    if "provenance" not in evidence:
        raise PerformanceEvidenceError("benchmark provenance is required")
    if set(evidence) != {
        "schema_version",
        "kind",
        "protocol_profile",
        "provenance",
        "environment",
        "processes",
    }:
        raise PerformanceEvidenceError("benchmark evidence fields are invalid")
    if evidence.get("schema_version") != 1:
        raise PerformanceEvidenceError("benchmark schema_version must be 1")
    if evidence.get("kind") != "v1_v2_paired_retrieval_samples":
        raise PerformanceEvidenceError("benchmark evidence kind is invalid")
    try:
        validate_benchmark_provenance(evidence.get("provenance"))
    except BenchmarkProvenanceError as exc:
        raise PerformanceEvidenceError(f"benchmark provenance is invalid: {exc}") from exc
    environment = evidence.get("environment")
    if not isinstance(environment, dict) or not environment:
        raise PerformanceEvidenceError("redacted benchmark environment is required")
    _validate_redacted_value(environment, field="environment")
    benchmark_pair = _benchmark_pair(evidence)
    processes = evidence.get("processes")
    if not isinstance(processes, list):
        raise PerformanceEvidenceError("benchmark processes must be an array")
    if profile == SUCCESSOR_RELEASE_PROTOCOL_PROFILE and len(processes) != 3:
        raise PerformanceEvidenceError(
            "successor release requires exactly 3 fresh processes"
        )
    if len(processes) < MIN_PROCESS_COUNT:
        raise PerformanceEvidenceError(
            f"at least {MIN_PROCESS_COUNT} fresh processes are required"
        )
    process_ids: set[str] = set()
    process_seeds: set[int] = set()
    for process in processes:
        if not isinstance(process, dict):
            raise PerformanceEvidenceError("process evidence must be an object")
        if set(process) != {"process_id", "seed", "cold_start", "workloads"}:
            raise PerformanceEvidenceError("process evidence fields are invalid")
        process_id = process.get("process_id")
        if not isinstance(process_id, str) or not process_id.strip():
            raise PerformanceEvidenceError("process_id must be a non-empty string")
        if process_id in process_ids:
            raise PerformanceEvidenceError("process_id values must be unique")
        process_ids.add(process_id)
        seed = process.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise PerformanceEvidenceError("process seed must be an integer")
        if seed in process_seeds:
            raise PerformanceEvidenceError("process seed values must be unique")
        process_seeds.add(seed)
        _validate_cold_start(
            process.get("cold_start"),
            process_id,
            seed,
            benchmark_pair,
        )
        workloads = process.get("workloads")
        if not isinstance(workloads, dict) or set(workloads) != set(REQUIRED_WORKLOADS):
            raise PerformanceEvidenceError(
                "each process must contain exactly the required workloads"
            )


def _validate_cold_start(
    value: Any,
    process_id: str,
    process_seed: int,
    benchmark_pair: str,
) -> None:
    if not isinstance(value, dict) or set(value) != {"workload", "v1", "v2"}:
        raise PerformanceEvidenceError(f"{process_id} cold-start fields are invalid")
    if value["workload"] != "unfiltered":
        raise PerformanceEvidenceError(
            f"{process_id} cold-start workload must be unfiltered"
        )
    query_ids: set[str] = set()
    for engine in ("v1", "v2"):
        cold = value[engine]
        if not isinstance(cold, dict) or set(cold) != {
            "engine",
            "process_id",
            "seed",
            "workload",
            "query_id",
            "factory_init_ns",
            "factory_memory_high_water_bytes",
            "total_cold_ns",
            "probe",
        }:
            raise PerformanceEvidenceError(
                f"{process_id} isolated cold-start {engine} fields are invalid"
            )
        if (
            cold["engine"] != engine
            or cold["process_id"] != f"{process_id}-cold-{engine}"
            or cold["seed"] != process_seed
            or cold["workload"] != "unfiltered"
        ):
            raise PerformanceEvidenceError(
                f"{process_id} isolated cold-start {engine} identity is invalid"
            )
        _validate_query_id(cold["query_id"])
        query_ids.add(cold["query_id"])
        for field in ("factory_init_ns", "factory_memory_high_water_bytes"):
            metric = cold[field]
            if not isinstance(metric, int) or isinstance(metric, bool) or metric <= 0:
                raise PerformanceEvidenceError(
                    f"{process_id} cold-start {engine} {field} must be positive"
                )
        measurement = cold["probe"]
        expected_fields = {"total_ns", *TELEMETRY_FIELDS}
        if not isinstance(measurement, dict) or set(measurement) != expected_fields:
            raise PerformanceEvidenceError(
                f"{process_id} cold-start {engine} measurement fields are invalid"
            )
        if (
            not isinstance(measurement["total_ns"], int)
            or isinstance(measurement["total_ns"], bool)
            or measurement["total_ns"] <= 0
        ):
            raise PerformanceEvidenceError(
                f"{process_id} cold-start {engine} total_ns must be positive"
            )
        for field in _INTEGER_TELEMETRY_FIELDS:
            metric = measurement[field]
            minimum = 1 if field == "memory_high_water_bytes" else 0
            if (
                not isinstance(metric, int)
                or isinstance(metric, bool)
                or metric < minimum
            ):
                raise PerformanceEvidenceError(
                    f"{process_id} cold-start {engine}_{field} is invalid"
                )
        strategy = measurement["strategy"]
        if not isinstance(strategy, str) or not _STRATEGY_TOKEN.fullmatch(strategy):
            raise PerformanceEvidenceError(
                f"{process_id} cold-start {engine} strategy is invalid"
            )
        total_cold_ns = cold["total_cold_ns"]
        if (
            not isinstance(total_cold_ns, int)
            or isinstance(total_cold_ns, bool)
            or total_cold_ns
            != cold["factory_init_ns"] + measurement["total_ns"]
        ):
            raise PerformanceEvidenceError(
                f"{process_id} cold-start {engine} total is invalid"
            )
    if len(query_ids) != 1:
        raise PerformanceEvidenceError(
            f"{process_id} isolated cold-start probes used different queries"
        )
    expected_baseline = (
        "direct"
        if benchmark_pair == _SUCCESSOR_BENCHMARK_PAIR
        else "compatibility"
    )
    if value["v1"]["probe"]["strategy"] != expected_baseline:
        raise PerformanceEvidenceError(
            f"{process_id} cold-start baseline strategy must be {expected_baseline}"
        )
    if value["v2"]["probe"]["strategy"] != "direct":
        raise PerformanceEvidenceError(
            f"{process_id} cold-start V2 strategy must be direct"
        )


def _validate_samples(
    samples: Any,
    workload: str,
    process_id: str,
    benchmark_pair: str,
    minimum_samples: int,
) -> None:
    if not isinstance(samples, dict):
        raise PerformanceEvidenceError(
            f"{process_id}/{workload} samples must be an object"
        )
    required = {"warmup_count", "query_ids", "v1_ns", "v2_ns"}
    required.update(
        f"{engine}_{field}"
        for engine in ("v1", "v2")
        for field in TELEMETRY_FIELDS
    )
    if set(samples) != required:
        raise PerformanceEvidenceError(
            f"{process_id}/{workload} sample fields are invalid"
        )
    warmups = samples["warmup_count"]
    if (
        not isinstance(warmups, int)
        or isinstance(warmups, bool)
        or warmups < MIN_WARMUP_COUNT
    ):
        raise PerformanceEvidenceError(
            f"{process_id}/{workload} needs at least {MIN_WARMUP_COUNT} warmups"
        )
    query_ids = samples["query_ids"]
    baseline = samples["v1_ns"]
    native = samples["v2_ns"]
    sample_arrays = [
        samples[f"{engine}_{field}"]
        for engine in ("v1", "v2")
        for field in TELEMETRY_FIELDS
    ]
    if not all(
        isinstance(values, list)
        for values in (query_ids, baseline, native, *sample_arrays)
    ):
        raise PerformanceEvidenceError("timed samples must be JSON arrays")
    if len({len(values) for values in (query_ids, baseline, native, *sample_arrays)}) != 1:
        raise PerformanceEvidenceError("paired sample arrays must have equal length")
    if len(query_ids) < minimum_samples:
        raise PerformanceEvidenceError(
            f"{process_id}/{workload} needs at least {minimum_samples:,} timed samples"
        )
    if len(set(query_ids)) < MIN_FIXED_QUERIES:
        raise PerformanceEvidenceError(
            f"{process_id}/{workload} needs at least {MIN_FIXED_QUERIES} fixed queries"
        )
    for query_id in query_ids:
        _validate_query_id(query_id)
    for values, label in ((baseline, "v1_ns"), (native, "v2_ns")):
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for value in values
        ):
            raise PerformanceEvidenceError(
                f"{process_id}/{workload} {label} must contain positive integers"
            )
    for engine in ("v1", "v2"):
        for field in _INTEGER_TELEMETRY_FIELDS:
            values = samples[f"{engine}_{field}"]
            minimum = 1 if field == "memory_high_water_bytes" else 0
            if any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < minimum
                for value in values
            ):
                raise PerformanceEvidenceError(
                    f"{process_id}/{workload} {engine}_{field} contains invalid values"
                )
        strategies = samples[f"{engine}_strategy"]
        if any(
            not isinstance(value, str) or not _STRATEGY_TOKEN.fullmatch(value)
            for value in strategies
        ):
            raise PerformanceEvidenceError(
                f"{process_id}/{workload} {engine}_strategy contains invalid values"
            )
        is_native = engine == "v2" or (
            engine == "v1" and benchmark_pair == _SUCCESSOR_BENCHMARK_PAIR
        )
        if is_native and not set(strategies).issubset(_V2_STRATEGIES):
            raise PerformanceEvidenceError(
                f"{process_id}/{workload} {engine}_strategy is not a native strategy"
            )

    native_engines = (
        ("v1", "v2")
        if benchmark_pair == _SUCCESSOR_BENCHMARK_PAIR
        else ("v2",)
    )
    for engine in native_engines:
        _validate_native_workload(samples, workload, process_id, engine)


def _validate_native_workload(
    samples: Mapping[str, Any],
    workload: str,
    process_id: str,
    engine: str,
) -> None:
    prefix = f"{engine}_"
    if workload == "empty":
        if (
            set(samples[f"{prefix}strategy"]) != {"empty"}
            or any(samples[f"{prefix}faiss_calls"])
            or any(samples[f"{prefix}faiss_ns"])
            or any(samples[f"{prefix}faiss_candidates"])
            or any(samples[f"{prefix}hydration_batches"])
            or any(samples[f"{prefix}hydration_rows"])
            or any(samples[f"{prefix}hydration_cache_hits"])
            or any(samples[f"{prefix}hydration_cache_misses"])
        ):
            raise PerformanceEvidenceError(
                f"{process_id}/empty must make zero native FAISS/hydration calls"
            )
    strategies = set(samples[f"{prefix}strategy"])
    if workload == "unfiltered" and strategies != {"direct"}:
        raise PerformanceEvidenceError(
            f"{process_id}/unfiltered must use direct native search"
        )
    if workload == "narrow" and strategies != {"selector"}:
        raise PerformanceEvidenceError(
            f"{process_id}/narrow must exercise native selector search"
        )
    if workload in {"broad", "near_universe"} and strategies != {"adaptive"}:
        raise PerformanceEvidenceError(
            f"{process_id}/{workload} must exercise adaptive search without an ID universe"
        )
    if workload == "prior_scope" and not strategies.issubset(
        {"selector", "adaptive"}
    ):
        raise PerformanceEvidenceError(
            f"{process_id}/prior_scope must exercise a bounded filtered strategy"
        )


def _benchmark_pair(evidence: Mapping[str, Any]) -> str:
    environment = evidence.get("environment")
    if not isinstance(environment, Mapping):
        return _DEFAULT_BENCHMARK_PAIR
    pair = environment.get("benchmark_pair", _DEFAULT_BENCHMARK_PAIR)
    if pair == _DEFAULT_BENCHMARK_PAIR:
        return pair
    if pair != _SUCCESSOR_BENCHMARK_PAIR:
        raise PerformanceEvidenceError("benchmark_pair is invalid")
    required_digests = (
        "baseline_snapshot_id",
        "baseline_snapshot_sha256",
        "candidate_snapshot_id",
        "candidate_snapshot_sha256",
    )
    for field in required_digests:
        try:
            _validate_query_id(environment.get(field))
        except PerformanceEvidenceError as exc:
            raise PerformanceEvidenceError(
                f"successor benchmark {field} is invalid"
            ) from exc
    if environment["baseline_snapshot_id"] == environment["candidate_snapshot_id"]:
        raise PerformanceEvidenceError(
            "successor benchmark requires distinct snapshots"
        )
    for field in ("baseline_ntotal", "candidate_ntotal"):
        value = environment.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise PerformanceEvidenceError(
                f"successor benchmark {field} must be positive"
            )
    write_epoch = environment.get("write_epoch")
    if (
        not isinstance(write_epoch, int)
        or isinstance(write_epoch, bool)
        or write_epoch <= 0
        or environment.get("v1_fallback_open") is not False
    ):
        raise PerformanceEvidenceError(
            "successor benchmark requires closed fallback after epoch zero"
        )
    return pair


def _protocol_profile(evidence: Mapping[str, Any]) -> str:
    profile = evidence.get("protocol_profile")
    if profile not in PROTOCOL_PROFILES:
        raise PerformanceEvidenceError(
            "benchmark protocol profile must be epoch_zero or successor_release"
        )
    return str(profile)


def _minimum_timed_samples(profile: str) -> int:
    return (
        MIN_SUCCESSOR_TIMED_SAMPLES
        if profile == SUCCESSOR_RELEASE_PROTOCOL_PROFILE
        else MIN_TIMED_SAMPLES
    )


def _minimum_bootstrap_resamples(profile: str) -> int:
    return (
        MIN_SUCCESSOR_BOOTSTRAP_RESAMPLES
        if profile == SUCCESSOR_RELEASE_PROTOCOL_PROFILE
        else MIN_BOOTSTRAP_RESAMPLES
    )


def _validate_protocol_counts(
    profile: str,
    *,
    process_count: int,
    warmup_count: int | None,
    timed_sample_count: int | None,
    bootstrap_resamples: int,
) -> None:
    if profile not in PROTOCOL_PROFILES:
        raise PerformanceEvidenceError("benchmark protocol profile is invalid")
    if (
        not isinstance(bootstrap_resamples, int)
        or isinstance(bootstrap_resamples, bool)
        or bootstrap_resamples < _minimum_bootstrap_resamples(profile)
    ):
        minimum = _minimum_bootstrap_resamples(profile)
        raise PerformanceEvidenceError(
            f"{profile} requires at least {minimum:,} bootstrap resamples"
        )
    if profile == SUCCESSOR_RELEASE_PROTOCOL_PROFILE and process_count != 3:
        raise PerformanceEvidenceError(
            "successor release requires exactly 3 fresh processes"
        )
    if process_count < MIN_PROCESS_COUNT:
        raise PerformanceEvidenceError(
            f"benchmark requires at least {MIN_PROCESS_COUNT} fresh processes"
        )
    if warmup_count is not None and warmup_count < MIN_WARMUP_COUNT:
        raise PerformanceEvidenceError(
            f"benchmark requires at least {MIN_WARMUP_COUNT} warmups"
        )
    minimum_samples = _minimum_timed_samples(profile)
    if timed_sample_count is not None and timed_sample_count < minimum_samples:
        raise PerformanceEvidenceError(
            f"{profile} requires at least {minimum_samples:,} timed samples "
            "per process/workload"
        )


def validate_benchmark_protocol_arguments(
    profile: str,
    *,
    process_count: int,
    warmup_count: int,
    timed_sample_count: int,
    bootstrap_resamples: int,
) -> None:
    """Validate CLI-level counts before expensive worker execution."""

    _validate_protocol_counts(
        profile,
        process_count=process_count,
        warmup_count=warmup_count,
        timed_sample_count=timed_sample_count,
        bootstrap_resamples=bootstrap_resamples,
    )


def _validate_query_id(value: Any) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PerformanceEvidenceError(
            "query_ids must be opaque lowercase SHA-256 digests"
        )


def _validate_redacted_value(value: Any, *, field: str) -> None:
    lowered = field.lower()
    if any(token in lowered for token in ("secret", "token", "password", "query_text")):
        raise PerformanceEvidenceError("benchmark environment contains a sensitive field")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise PerformanceEvidenceError("environment keys must be strings")
            _validate_redacted_value(child, field=key)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _validate_redacted_value(child, field=field)
        return
    if isinstance(value, str):
        if (
            _ABSOLUTE_WINDOWS_PATH.match(value)
            or value.startswith(("/", "\\\\"))
            or "file://" in value.lower()
        ):
            raise PerformanceEvidenceError(
                "benchmark environment must not contain absolute paths"
            )
        return
    if value is not None and not isinstance(value, (int, float, bool)):
        raise PerformanceEvidenceError("benchmark environment value is not JSON-safe")


def _p95(values: np.ndarray) -> float:
    result = float(np.percentile(values, 95))
    if not np.isfinite(result) or result <= 0:
        raise PerformanceEvidenceError("p95 latency must be finite and positive")
    return result


def _paired_bootstrap_p95_ratio(
    baseline: np.ndarray,
    native: np.ndarray,
    *,
    resamples: int,
    seed: int,
) -> tuple[float, float]:
    if baseline.shape != native.shape or baseline.ndim != 1 or baseline.size == 0:
        raise PerformanceEvidenceError("paired bootstrap inputs are invalid")
    generator = np.random.default_rng(seed)
    ratios: list[np.ndarray] = []
    remaining = resamples
    # Chunking bounds peak memory for release-sized samples.
    while remaining:
        count = min(remaining, 512)
        indices = generator.integers(
            0,
            baseline.size,
            size=(count, baseline.size),
            endpoint=False,
        )
        baseline_p95 = np.percentile(baseline[indices], 95, axis=1)
        native_p95 = np.percentile(native[indices], 95, axis=1)
        ratios.append(native_p95 / baseline_p95)
        remaining -= count
    values = np.concatenate(ratios)
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


__all__ = [
    "MAX_COMBINED_CI_RATIO",
    "MAX_PROCESS_RATIO",
    "EPOCH_ZERO_PROTOCOL_PROFILE",
    "MIN_BOOTSTRAP_RESAMPLES",
    "MIN_FIXED_QUERIES",
    "MIN_PROCESS_COUNT",
    "MIN_SUCCESSOR_BOOTSTRAP_RESAMPLES",
    "MIN_SUCCESSOR_TIMED_SAMPLES",
    "MIN_TIMED_SAMPLES",
    "MIN_WARMUP_COUNT",
    "PerformanceEvidenceError",
    "PROTOCOL_PROFILES",
    "REQUIRED_WORKLOADS",
    "SUCCESSOR_RELEASE_PROTOCOL_PROFILE",
    "TELEMETRY_FIELDS",
    "analyze_benchmark",
    "load_benchmark_evidence",
    "validate_benchmark_protocol_arguments",
    "write_benchmark_evidence",
]
