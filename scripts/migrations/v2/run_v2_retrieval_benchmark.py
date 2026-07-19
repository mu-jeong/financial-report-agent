"""Run the declared paired retrieval protocol in three or more fresh processes."""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.migrations.v2.validation.benchmark_runner import (
    BenchmarkFactory,
    process_memory_high_water_bytes,
    run_isolated_cold_probe,
    run_paired_benchmark_process,
)
from src.migrations.v2.validation.benchmark_provenance import (
    BenchmarkProvenanceError,
    build_benchmark_provenance,
    callable_identity,
    normalize_factory_entrypoint,
)
from src.retrieval.identity import canonical_json
from src.migrations.v2.validation.performance import (
    MIN_PROCESS_COUNT,
    MIN_TIMED_SAMPLES,
    MIN_WARMUP_COUNT,
    PerformanceEvidenceError,
    PROTOCOL_PROFILES,
    analyze_benchmark,
    validate_benchmark_protocol_arguments,
    write_benchmark_evidence,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute paired V1/V2 retrieval probes in fresh processes and "
            "write immutable redacted evidence"
        )
    )
    parser.add_argument(
        "--factory",
        required=True,
        help="import path module:function returning BenchmarkFactory",
    )
    parser.add_argument(
        "--protocol-profile",
        required=True,
        choices=PROTOCOL_PROFILES,
        help="epoch_zero or successor_release evidence requirements",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--processes", type=int, default=MIN_PROCESS_COUNT)
    parser.add_argument("--warmups", type=int, default=MIN_WARMUP_COUNT)
    parser.add_argument("--samples", type=int, default=MIN_TIMED_SAMPLES)
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--bootstrap-resamples", type=int, default=10_000)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--process-id", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--cold-engine",
        choices=("v1", "v2"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    if args.worker:
        return _run_worker(args)
    if args.output is None:
        parser.error("--output is required")
    try:
        validate_benchmark_protocol_arguments(
            args.protocol_profile,
            process_count=args.processes,
            warmup_count=args.warmups,
            timed_sample_count=args.samples,
            bootstrap_resamples=args.bootstrap_resamples,
        )
    except PerformanceEvidenceError as exc:
        parser.error(str(exc))
    factory_function = _load_factory(args.factory)
    try:
        provenance = build_benchmark_provenance(
            args.factory,
            factory_function,
            runner_path=Path(__file__),
        )
    except BenchmarkProvenanceError as exc:
        raise PerformanceEvidenceError(
            f"benchmark provenance could not be established: {exc}"
        ) from exc

    process_evidence: list[dict[str, Any]] = []
    environments: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="v2-benchmark-") as temporary:
        temporary_root = Path(temporary)
        for number in range(args.processes):
            process_id = f"process-{number + 1}"
            process_seed = args.seed + number
            cold_start: dict[str, Any] = {"workload": "unfiltered"}
            for engine in ("v1", "v2"):
                cold_output = temporary_root / f"{process_id}-cold-{engine}.json"
                cold_payload = _invoke_worker(
                    args,
                    process_id=f"{process_id}-cold-{engine}",
                    seed=process_seed,
                    worker_output=cold_output,
                    cold_engine=engine,
                )
                if set(cold_payload) != {
                    "provenance",
                    "environment",
                    "cold_start",
                }:
                    raise PerformanceEvidenceError(
                        "cold benchmark worker output is invalid"
                    )
                _require_worker_provenance(
                    cold_payload["provenance"],
                    provenance,
                    process_id=f"{process_id}-cold-{engine}",
                )
                environment = cold_payload["environment"]
                cold = cold_payload["cold_start"]
                if not isinstance(environment, dict) or not isinstance(cold, dict):
                    raise PerformanceEvidenceError(
                        "cold benchmark worker output types are invalid"
                    )
                environments.append(environment)
                cold_start[engine] = cold

            worker_output = temporary_root / f"{process_id}.json"
            payload = _invoke_worker(
                args,
                process_id=process_id,
                seed=process_seed,
                worker_output=worker_output,
            )
            if set(payload) != {"provenance", "environment", "process"}:
                raise PerformanceEvidenceError("benchmark worker output is invalid")
            _require_worker_provenance(
                payload["provenance"],
                provenance,
                process_id=process_id,
            )
            environment = payload["environment"]
            process = payload["process"]
            if not isinstance(environment, dict) or not isinstance(process, dict):
                raise PerformanceEvidenceError("benchmark worker output types are invalid")
            process["cold_start"] = cold_start
            environments.append(environment)
            process_evidence.append(process)

    try:
        final_provenance = build_benchmark_provenance(
            args.factory,
            factory_function,
            runner_path=Path(__file__),
        )
    except BenchmarkProvenanceError as exc:
        raise PerformanceEvidenceError(
            f"final benchmark provenance could not be established: {exc}"
        ) from exc
    _require_worker_provenance(
        final_provenance,
        provenance,
        process_id="leader-final",
    )
    canonical_environments = {canonical_json(value) for value in environments}
    if len(canonical_environments) != 1:
        raise PerformanceEvidenceError(
            "fresh benchmark workers reported different environments"
        )
    raw = {
        "schema_version": 1,
        "kind": "v1_v2_paired_retrieval_samples",
        "protocol_profile": args.protocol_profile,
        "provenance": provenance,
        "environment": environments[0],
        "processes": process_evidence,
    }
    analysis = analyze_benchmark(
        raw,
        bootstrap_resamples=args.bootstrap_resamples,
        bootstrap_seed=args.seed,
    )
    write_benchmark_evidence(args.output, raw, analysis)
    print(
        json.dumps(
            {
                "status": "passed" if analysis["passed"] else "failed",
                "evidence": args.output.name,
                "process_count": len(process_evidence),
            },
            ensure_ascii=False,
        )
    )
    return 0 if analysis["passed"] else 1


def _run_worker(args: argparse.Namespace) -> int:
    if not args.process_id or args.worker_output is None:
        raise PerformanceEvidenceError(
            "benchmark worker requires --process-id and --worker-output"
        )
    factory_started = time.perf_counter_ns()
    factory_function = _load_factory(args.factory)
    requested_engine = args.cold_engine or "paired"
    factory = factory_function(
        process_id=args.process_id,
        seed=args.seed,
        engine=requested_engine,
    )
    factory_init_ns = max(1, time.perf_counter_ns() - factory_started)
    factory_memory_high_water_bytes = process_memory_high_water_bytes()
    if not isinstance(factory, BenchmarkFactory):
        raise PerformanceEvidenceError(
            "benchmark factory must return BenchmarkFactory"
        )
    if factory.engine != requested_engine:
        raise PerformanceEvidenceError(
            "benchmark factory did not honor the requested engine isolation"
        )
    if args.cold_engine is not None:
        probe = factory.v1_probe if args.cold_engine == "v1" else factory.v2_probe
        cold_start = run_isolated_cold_probe(
            process_id=args.process_id,
            engine=args.cold_engine,
            seed=args.seed,
            queries=factory.queries,
            probe=probe,
            factory_init_ns=factory_init_ns,
            factory_memory_high_water_bytes=factory_memory_high_water_bytes,
        )
        payload = {
            "environment": dict(factory.environment),
            "cold_start": cold_start,
        }
    else:
        process = run_paired_benchmark_process(
            process_id=args.process_id,
            queries=factory.queries,
            v1_probe=factory.v1_probe,
            v2_probe=factory.v2_probe,
            warmup_count=args.warmups,
            timed_sample_count=args.samples,
            seed=args.seed,
        )
        payload = {
            "environment": dict(factory.environment),
            "process": process,
        }
    try:
        provenance = build_benchmark_provenance(
            args.factory,
            factory_function,
            runner_path=Path(__file__),
        )
    except BenchmarkProvenanceError as exc:
        raise PerformanceEvidenceError(
            f"benchmark worker provenance could not be established: {exc}"
        ) from exc
    payload["provenance"] = provenance
    args.worker_output.write_text(
        canonical_json(payload) + "\n",
        encoding="utf-8",
    )
    return 0


def _invoke_worker(
    args: argparse.Namespace,
    *,
    process_id: str,
    seed: int,
    worker_output: Path,
    cold_engine: str | None = None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "scripts.migrations.v2.run_v2_retrieval_benchmark",
        "--worker",
        "--factory",
        args.factory,
        "--protocol-profile",
        args.protocol_profile,
        "--process-id",
        process_id,
        "--worker-output",
        str(worker_output),
        "--warmups",
        str(args.warmups),
        "--samples",
        str(args.samples),
        "--seed",
        str(seed),
    ]
    if cold_engine is not None:
        command.extend(("--cold-engine", cold_engine))
    completed = subprocess.run(
        command,
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise PerformanceEvidenceError(
            f"fresh benchmark worker {process_id} failed: {detail}"
        )
    try:
        payload = json.loads(worker_output.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PerformanceEvidenceError("benchmark worker output is unreadable") from exc
    if not isinstance(payload, dict):
        raise PerformanceEvidenceError("benchmark worker output must be an object")
    return payload


def _load_factory(value: str):
    try:
        normalized = normalize_factory_entrypoint(value)
    except BenchmarkProvenanceError as exc:
        raise PerformanceEvidenceError(str(exc)) from exc
    module_name, _separator, attribute = normalized.partition(":")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise PerformanceEvidenceError("benchmark factory is not callable")
    try:
        identity = callable_identity(factory)
    except BenchmarkProvenanceError as exc:
        raise PerformanceEvidenceError(str(exc)) from exc
    if identity != normalized:
        raise PerformanceEvidenceError(
            "benchmark factory entrypoint does not match the resolved adapter"
        )
    return factory


def _require_worker_provenance(
    value: Any,
    expected: dict[str, Any],
    *,
    process_id: str,
) -> None:
    if not isinstance(value, dict) or canonical_json(value) != canonical_json(expected):
        raise PerformanceEvidenceError(
            f"fresh benchmark worker {process_id} used different code or interpreter"
        )


if __name__ == "__main__":
    raise SystemExit(main())
