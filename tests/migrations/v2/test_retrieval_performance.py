from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from src.migrations.v2.validation import performance as performance_module
from src.migrations.v2.validation.benchmark_provenance import (
    BenchmarkProvenanceError,
    verify_current_benchmark_provenance,
)
from src.migrations.v2.validation.performance import (
    PerformanceEvidenceError,
    REQUIRED_WORKLOADS,
    analyze_benchmark,
    write_benchmark_evidence,
)
from src.migrations.v2.validation.benchmark_runner import (
    FixedBenchmarkQuery,
    ProbeTelemetry,
    run_paired_benchmark_process,
)


def _query_id(number: int) -> str:
    return hashlib.sha256(f"fixed-query-{number}".encode("utf-8")).hexdigest()


def _cold_measurement(strategy: str) -> dict:
    return {
        "total_ns": 1_000_000,
        "sql_ns": 0 if strategy == "compatibility" else 25_000,
        "sql_rows": 0 if strategy == "compatibility" else 5,
        "strategy": strategy,
        "faiss_ns": 300_000,
        "faiss_calls": 1,
        "faiss_candidates": 10,
        "hydration_batches": 1,
        "hydration_rows": 5,
        "hydration_cache_hits": 0,
        "hydration_cache_misses": 5,
        "rerank_ns": 100_000,
        "memory_high_water_bytes": 100_000_000,
        "lease_ns": 0 if strategy == "compatibility" else 700_000,
    }


def _isolated_cold(engine: str, process_number: int) -> dict:
    strategy = "compatibility" if engine == "v1" else "direct"
    measurement = _cold_measurement(strategy)
    factory_init_ns = 50_000_000 + process_number * 1_000
    return {
        "engine": engine,
        "process_id": f"process-{process_number + 1}-cold-{engine}",
        "seed": 20260716 + process_number,
        "workload": "unfiltered",
        "query_id": _query_id(0),
        "factory_init_ns": factory_init_ns,
        "factory_memory_high_water_bytes": 90_000_000,
        "total_cold_ns": factory_init_ns + measurement["total_ns"],
        "probe": measurement,
    }


def _evidence(*, ratio: float = 1.04, slow_process: int | None = None):
    processes = []
    for process_number in range(3):
        workloads = {}
        process_ratio = 1.20 if process_number == slow_process else ratio
        for workload_number, workload in enumerate(REQUIRED_WORKLOADS):
            baseline = [
                1_000_000 + workload_number * 10_000 + (sample % 17) * 1_000
                for sample in range(200)
            ]
            native = [int(value * process_ratio) for value in baseline]
            sample_count = len(baseline)
            v2_strategy = {
                "unfiltered": "direct",
                "empty": "empty",
                "narrow": "selector",
                "broad": "adaptive",
                "near_universe": "adaptive",
                "prior_scope": "selector",
            }[workload]
            empty = workload == "empty"
            samples = {
                "warmup_count": 10,
                "query_ids": [_query_id(sample % 30) for sample in range(200)],
                "v1_ns": baseline,
                "v2_ns": native,
            }
            for engine, strategy in (("v1", "compatibility"), ("v2", v2_strategy)):
                samples.update(
                    {
                        f"{engine}_sql_ns": [0 if engine == "v1" else 25_000]
                        * sample_count,
                        f"{engine}_sql_rows": [0 if engine == "v1" else (0 if empty else 5)]
                        * sample_count,
                        f"{engine}_strategy": [strategy] * sample_count,
                        f"{engine}_faiss_ns": [0 if empty and engine == "v2" else 300_000]
                        * sample_count,
                        f"{engine}_faiss_calls": [0 if empty and engine == "v2" else 1]
                        * sample_count,
                        f"{engine}_faiss_candidates": [0 if empty and engine == "v2" else 10]
                        * sample_count,
                        f"{engine}_hydration_batches": [0 if empty else 1] * sample_count,
                        f"{engine}_hydration_rows": [0 if empty else 5] * sample_count,
                        f"{engine}_hydration_cache_hits": [0] * sample_count,
                        f"{engine}_hydration_cache_misses": [
                            0 if empty or engine == "v1" else 5
                        ]
                        * sample_count,
                        f"{engine}_rerank_ns": [100_000] * sample_count,
                        f"{engine}_memory_high_water_bytes": [100_000_000]
                        * sample_count,
                        f"{engine}_lease_ns": [0 if engine == "v1" else 700_000]
                        * sample_count,
                    }
                )
            workloads[workload] = samples
        processes.append(
            {
                "process_id": f"process-{process_number + 1}",
                "seed": 20260716 + process_number,
                "cold_start": {
                    "workload": "unfiltered",
                    "v1": _isolated_cold("v1", process_number),
                    "v2": _isolated_cold("v2", process_number),
                },
                "workloads": workloads,
            }
        )
    return {
        "schema_version": 1,
        "kind": "v1_v2_paired_retrieval_samples",
        "protocol_profile": "epoch_zero",
        "provenance": {
            "schema_version": 1,
            "kind": "v2_retrieval_benchmark_provenance",
            "factory_entrypoint": "tests.migrations.v2.fixtures_factory.performance_probe:create_factory",
            "adapter_callable": "tests.migrations.v2.fixtures_factory.performance_probe:create_factory",
            "adapter_module_sha256": "1" * 64,
            "runner_entrypoint": "scripts.migrations.v2.run_v2_retrieval_benchmark:main",
            "runner_module_sha256": "2" * 64,
            "runtime_code_layout_sha256": "3" * 64,
            "runtime_code_file_count": 1,
            "layout_algorithm": "sha256-canonical-runtime-python-v1",
            "interpreter": {
                "implementation": "cpython",
                "version": "3.10.11",
                "cache_tag": "cpython-310",
                "executable_sha256": "4" * 64,
            },
        },
        "environment": {
            "os": "windows",
            "faiss_version": "fixture",
            "cache_state": "warm",
            "seed": 20260716,
        },
        "processes": processes,
    }


def _successor_evidence():
    evidence = _evidence()
    evidence["protocol_profile"] = "successor_release"
    evidence["environment"].update(
        {
            "benchmark_pair": "native_predecessor_vs_native_successor",
            "baseline_snapshot_id": "1" * 64,
            "baseline_snapshot_sha256": "2" * 64,
            "baseline_ntotal": 384,
            "candidate_snapshot_id": "3" * 64,
            "candidate_snapshot_sha256": "4" * 64,
            "candidate_ntotal": 369,
            "write_epoch": 1,
            "v1_fallback_open": False,
        }
    )
    for process in evidence["processes"]:
        process["cold_start"]["v1"]["probe"] = _cold_measurement("direct")
        process["cold_start"]["v1"]["total_cold_ns"] = (
            process["cold_start"]["v1"]["factory_init_ns"]
            + process["cold_start"]["v1"]["probe"]["total_ns"]
        )
        for samples in process["workloads"].values():
            for field in (
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
            ):
                samples[f"v1_{field}"] = list(samples[f"v2_{field}"])
    return evidence


def test_all_six_workloads_pass_the_paired_bootstrap_gate():
    analysis = analyze_benchmark(_evidence(), bootstrap_resamples=500)

    assert analysis["passed"] is True
    assert analysis["protocol"]["profile"] == "epoch_zero"
    assert analysis["protocol"]["process_count_policy"] == "minimum"
    assert analysis["protocol"][
        "minimum_timed_samples_per_process_workload"
    ] == 200
    assert analysis["protocol"]["minimum_bootstrap_resamples"] == 500
    assert set(analysis["workloads"]) == set(REQUIRED_WORKLOADS)
    for result in analysis["workloads"].values():
        assert result["paired_bootstrap_ci_95"][1] <= 1.10
        assert result["passed"] is True


def test_one_process_above_the_individual_cap_fails_each_workload():
    analysis = analyze_benchmark(
        _evidence(slow_process=1),
        bootstrap_resamples=500,
    )

    assert analysis["passed"] is False
    assert all(not result["passed"] for result in analysis["workloads"].values())
    assert all(
        result["processes"][1]["ratio"] > 1.15
        for result in analysis["workloads"].values()
    )


def _resize_timed_samples(evidence, sample_count):
    for process in evidence["processes"]:
        for samples in process["workloads"].values():
            for field, values in samples.items():
                if field == "warmup_count":
                    continue
                samples[field] = [
                    values[number % len(values)] for number in range(sample_count)
                ]


def test_successor_pair_requires_and_accepts_two_native_snapshot_identities(
    monkeypatch,
):
    evidence = _successor_evidence()
    _resize_timed_samples(evidence, 4_000)
    monkeypatch.setattr(
        performance_module,
        "_paired_bootstrap_p95_ratio",
        lambda *_args, **_kwargs: (1.0, 1.0),
    )

    analysis = analyze_benchmark(evidence, bootstrap_resamples=10_000)

    assert analysis["benchmark_pair"] == (
        "native_predecessor_vs_native_successor"
    )
    assert analysis["passed"] is True
    assert analysis["protocol"] == {
        "profile": "successor_release",
        "required_workloads": list(REQUIRED_WORKLOADS),
        "minimum_processes": 3,
        "process_count_policy": "exact",
        "minimum_warmups": 10,
        "minimum_timed_samples_per_process_workload": 4_000,
        "minimum_fixed_queries_per_workload": 30,
        "minimum_bootstrap_resamples": 10_000,
        "bootstrap_resamples": 10_000,
        "bootstrap_seed": 20260716,
    }

    evidence["environment"]["candidate_snapshot_id"] = "1" * 64
    with pytest.raises(PerformanceEvidenceError, match="distinct snapshots"):
        analyze_benchmark(evidence, bootstrap_resamples=10_000)


def test_successor_release_profile_rejects_legacy_protocol_counts():
    evidence = _successor_evidence()

    with pytest.raises(PerformanceEvidenceError, match="10,000 bootstrap"):
        analyze_benchmark(evidence, bootstrap_resamples=500)

    with pytest.raises(PerformanceEvidenceError, match="4,000 timed samples"):
        analyze_benchmark(evidence, bootstrap_resamples=10_000)

    evidence = _successor_evidence()
    evidence["processes"][0]["workloads"]["unfiltered"]["warmup_count"] = 9
    with pytest.raises(PerformanceEvidenceError, match="at least 10 warmups"):
        analyze_benchmark(evidence, bootstrap_resamples=10_000)

    evidence = _successor_evidence()
    _resize_timed_samples(evidence, 4_000)
    evidence["processes"][0]["workloads"]["unfiltered"]["query_ids"] = [
        _query_id(number % 29) for number in range(4_000)
    ]
    with pytest.raises(PerformanceEvidenceError, match="at least 30 fixed queries"):
        analyze_benchmark(evidence, bootstrap_resamples=10_000)

    evidence = _successor_evidence()
    fourth = deepcopy(evidence["processes"][-1])
    fourth["process_id"] = "process-4"
    fourth["seed"] += 1
    for engine in ("v1", "v2"):
        fourth["cold_start"][engine]["process_id"] = f"process-4-cold-{engine}"
        fourth["cold_start"][engine]["seed"] = fourth["seed"]
    evidence["processes"].append(fourth)
    with pytest.raises(PerformanceEvidenceError, match="exactly 3 fresh processes"):
        analyze_benchmark(evidence, bootstrap_resamples=10_000)


def test_benchmark_evidence_requires_explicit_profile_and_runner_provenance():
    evidence = _evidence()
    del evidence["protocol_profile"]
    with pytest.raises(PerformanceEvidenceError, match="protocol profile"):
        analyze_benchmark(evidence, bootstrap_resamples=500)

    evidence = _evidence()
    del evidence["provenance"]
    with pytest.raises(PerformanceEvidenceError, match="provenance"):
        analyze_benchmark(evidence, bootstrap_resamples=500)

    evidence = _evidence()
    evidence["provenance"]["adapter_callable"] = (
        "tests.migrations.v2.fixtures_factory.performance_probe:other_factory"
    )
    with pytest.raises(PerformanceEvidenceError, match="identities differ"):
        analyze_benchmark(evidence, bootstrap_resamples=500)


def test_process_seeds_must_be_retained_and_unique():
    evidence = _evidence()
    evidence["processes"][1]["seed"] = evidence["processes"][0]["seed"]

    with pytest.raises(PerformanceEvidenceError, match="seed values must be unique"):
        analyze_benchmark(evidence, bootstrap_resamples=500)


def test_cold_start_measurement_is_required_and_validated_separately():
    evidence = _evidence()
    evidence["processes"][0]["cold_start"]["v1"]["factory_init_ns"] = 0

    with pytest.raises(PerformanceEvidenceError, match="factory_init_ns must be positive"):
        analyze_benchmark(evidence, bootstrap_resamples=500)


def test_protocol_rejects_too_few_samples_and_nonopaque_query_ids():
    evidence = _evidence()
    samples = evidence["processes"][0]["workloads"]["unfiltered"]
    samples["query_ids"] = samples["query_ids"][:199]
    samples["v1_ns"] = samples["v1_ns"][:199]
    samples["v2_ns"] = samples["v2_ns"][:199]
    for key in list(samples):
        if key not in {"warmup_count", "query_ids", "v1_ns", "v2_ns"}:
            samples[key] = samples[key][:199]
    with pytest.raises(PerformanceEvidenceError, match="at least 200"):
        analyze_benchmark(evidence, bootstrap_resamples=500)

    evidence = _evidence()
    evidence["processes"][0]["workloads"]["empty"]["query_ids"][0] = (
        "raw user query"
    )
    with pytest.raises(PerformanceEvidenceError, match="opaque"):
        analyze_benchmark(evidence, bootstrap_resamples=500)


def test_protocol_rejects_missing_telemetry_and_empty_faiss_calls():
    evidence = _evidence()
    del evidence["processes"][0]["workloads"]["narrow"]["v2_sql_ns"]
    with pytest.raises(PerformanceEvidenceError, match="sample fields"):
        analyze_benchmark(evidence, bootstrap_resamples=500)

    evidence = _evidence()
    evidence["processes"][0]["workloads"]["empty"]["v2_faiss_calls"][0] = 1
    with pytest.raises(PerformanceEvidenceError, match="zero native FAISS"):
        analyze_benchmark(evidence, bootstrap_resamples=500)

    evidence = _evidence()
    evidence["processes"][0]["workloads"]["broad"]["v2_strategy"] = [
        "selector"
    ] * 200
    with pytest.raises(PerformanceEvidenceError, match="adaptive search"):
        analyze_benchmark(evidence, bootstrap_resamples=500)


def test_evidence_writer_retains_raw_samples_but_rejects_absolute_paths(tmp_path):
    evidence = _evidence()
    analysis = analyze_benchmark(evidence, bootstrap_resamples=500)
    output = write_benchmark_evidence(tmp_path / "benchmark.json", evidence, analysis)
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["raw"] == evidence
    assert payload["analysis"]["passed"] is True
    assert output.stat().st_mode & stat.S_IWUSR == 0

    unsafe = _evidence()
    unsafe["environment"]["data_root"] = "C:\\Users\\person\\private"
    with pytest.raises(PerformanceEvidenceError, match="absolute paths"):
        analyze_benchmark(unsafe, bootstrap_resamples=500)


def test_paired_runner_executes_probes_and_never_retains_query_payloads():
    calls = {"v1": 0, "v2": 0}
    strategies = {
        "unfiltered": "direct",
        "empty": "empty",
        "narrow": "selector",
        "broad": "adaptive",
        "near_universe": "adaptive",
        "prior_scope": "selector",
    }

    def probe(engine, workload, payload):
        calls[engine] += 1
        assert payload.startswith("secret query payload")
        empty = workload == "empty"
        return ProbeTelemetry(
            sql_ns=10,
            sql_rows=0 if empty else 5,
            strategy="compatibility" if engine == "v1" else strategies[workload],
            faiss_ns=0 if empty and engine == "v2" else 20,
            faiss_calls=0 if empty and engine == "v2" else 1,
            faiss_candidates=0 if empty and engine == "v2" else 10,
            hydration_batches=0 if empty else 1,
            hydration_rows=0 if empty else 5,
            rerank_ns=3,
            lease_ns=0 if engine == "v1" else 40,
        )

    queries = {
        workload: [
            FixedBenchmarkQuery(
                _query_id(number),
                f"secret query payload {number}",
            )
            for number in range(30)
        ]
        for workload in REQUIRED_WORKLOADS
    }
    process = run_paired_benchmark_process(
        process_id="process-1",
        queries=queries,
        v1_probe=lambda workload, payload: probe("v1", workload, payload),
        v2_probe=lambda workload, payload: probe("v2", workload, payload),
        memory_sampler=lambda: 123_456_789,
    )

    encoded = json.dumps(process)
    assert "secret query payload" not in encoded
    assert calls == {"v1": 6 * 210, "v2": 6 * 210}
    assert process["seed"] == 20260716
    assert "cold_start" not in process
    assert len(process["workloads"]["narrow"]["v2_sql_ns"]) == 200

    processes = []
    for number in range(3):
        item = deepcopy(process)
        item["process_id"] = f"process-{number + 1}"
        item["seed"] = 20260716 + number
        item["cold_start"] = {
            "workload": "unfiltered",
            "v1": _isolated_cold("v1", number),
            "v2": _isolated_cold("v2", number),
        }
        processes.append(item)
    analysis = analyze_benchmark(
        {
            "schema_version": 1,
            "kind": "v1_v2_paired_retrieval_samples",
            "protocol_profile": "epoch_zero",
            "provenance": deepcopy(_evidence()["provenance"]),
            "environment": {"os": "fixture", "cache_state": "warm"},
            "processes": processes,
        },
        bootstrap_resamples=500,
    )
    assert set(analysis["workloads"]) == set(REQUIRED_WORKLOADS)


@pytest.mark.slow
def test_cli_runner_uses_three_fresh_processes_and_writes_gate_evidence(tmp_path):
    output = tmp_path / "paired-benchmark.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.migrations.v2.run_v2_retrieval_benchmark",
            "--factory",
            "tests.migrations.v2.fixtures_factory.performance_probe:create_factory",
            "--protocol-profile",
            "epoch_zero",
            "--output",
            str(output),
            "--bootstrap-resamples",
            "500",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["analysis"]["passed"] is True
    provenance = evidence["raw"]["provenance"]
    assert provenance["factory_entrypoint"] == (
        "tests.migrations.v2.fixtures_factory.performance_probe:create_factory"
    )
    assert provenance["adapter_callable"] == provenance["factory_entrypoint"]
    assert provenance["runner_entrypoint"] == (
        "scripts.migrations.v2.run_v2_retrieval_benchmark:main"
    )
    assert provenance["runtime_code_file_count"] > 1
    assert len(provenance["runtime_code_layout_sha256"]) == 64
    assert len(provenance["interpreter"]["executable_sha256"]) == 64
    assert provenance["runner_module_sha256"] == hashlib.sha256(
        (
            Path(__file__).parents[3]
            / "scripts"
            / "migrations"
            / "v2"
            / "run_v2_retrieval_benchmark.py"
        )
        .read_bytes()
    ).hexdigest()
    verify_current_benchmark_provenance(
        provenance,
        runner_path=Path(__file__).parents[3]
        / "scripts"
        / "migrations"
        / "v2"
        / "run_v2_retrieval_benchmark.py",
    )
    tampered = deepcopy(provenance)
    tampered["runtime_code_layout_sha256"] = "f" * 64
    with pytest.raises(BenchmarkProvenanceError, match="does not match"):
        verify_current_benchmark_provenance(
            tampered,
            runner_path=Path(__file__).parents[3]
            / "scripts"
            / "migrations"
            / "v2"
            / "run_v2_retrieval_benchmark.py",
        )
    assert len(evidence["raw"]["processes"]) == 3
    assert [
        process["seed"] for process in evidence["raw"]["processes"]
    ] == [20260716, 20260717, 20260718]
    assert all(
        process["cold_start"][engine]["factory_init_ns"] > 0
        for process in evidence["raw"]["processes"]
        for engine in ("v1", "v2")
    )
    assert "fixture-query" not in output.read_text(encoding="utf-8")
