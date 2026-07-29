"""Run one approved schema-v2 candidate against a fixed evaluation snapshot.

This entry point is intended to run in a separate Python process.  DB_PATH and
FAISS_DIR are set before importing the graph, whose application object is built
at import time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.monitoring import (
    CandidateValidationError,
    build_candidate_evaluation_case,
    load_evaluation_dataset,
    load_evaluation_snapshot_manifest,
    load_regression_candidate,
    run_candidate_evaluation,
    validate_evaluation_snapshot,
)
from src.core.reproduction_manifest import (
    build_runtime_reproduction_manifest,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one approved candidate on a fixed evaluation snapshot."
    )
    parser.add_argument("--candidate", required=True)
    parser.add_argument(
        "--dataset", default="tests/fixtures/evaluation_dataset.json"
    )
    parser.add_argument(
        "--snapshot-root", default="tests/fixtures/eval_snapshot"
    )
    parser.add_argument("--manifest", default=None)
    parser.add_argument(
        "--output-dir", default="debug/candidate_evaluation_runs"
    )
    parser.add_argument(
        "--run-kind",
        required=True,
        choices=("baseline", "verification"),
    )
    parser.add_argument(
        "--latency-threshold-seconds",
        type=float,
        default=30.0,
    )
    return parser.parse_args(argv)


def build_snapshot_environment(
    validation: Mapping[str, Any],
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment that points graph startup at the fixed snapshot."""
    env = dict(os.environ if base_env is None else base_env)
    env["DB_PATH"] = str(validation["db_path"])
    env["FAISS_DIR"] = str(validation["faiss_dir"])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def build_manifest_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Select only stable snapshot identity fields from a manifest."""
    database = manifest.get("database")
    vector_db = manifest.get("vector_db")
    database = database if isinstance(database, Mapping) else {}
    vector_db = vector_db if isinstance(vector_db, Mapping) else {}
    return {
        "name": manifest.get("name"),
        "version": manifest.get("version"),
        "dataset_name": manifest.get("dataset_name"),
        "dataset_version": manifest.get("dataset_version"),
        "snapshot_date": manifest.get("snapshot_date"),
        "database": {
            key: database.get(key)
            for key in (
                "path",
                "table",
                "source_row_count",
                "embedded_row_count",
                "min_report_date",
                "max_report_date",
            )
        },
        "vector_db": {
            key: vector_db.get(key)
            for key in ("path", "required_files", "file_sizes")
        },
    }


def manifest_config_fingerprint(manifest: Mapping[str, Any]) -> str:
    """Hash canonical JSON for the allowlisted manifest identity."""
    encoded = json.dumps(
        build_manifest_identity(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_candidate_provenance(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the exact provenance contract required by candidate evaluation."""
    snapshot_name = str(manifest.get("snapshot_id") or manifest.get("name") or "")
    snapshot_version = manifest.get("version")
    snapshot_id = (
        f"{snapshot_name}:{snapshot_version}"
        if snapshot_name and snapshot_version is not None
        else snapshot_name or None
    )
    data_revision = str(
        manifest.get("data_revision")
        or manifest.get("snapshot_date")
        or snapshot_id
        or ""
    )
    return {
        "backend_mode": "fixed_snapshot",
        "snapshot_id": snapshot_id,
        "snapshot_available": True,
        "data_revision": data_revision,
        "config_fingerprint": manifest_config_fingerprint(manifest),
    }


def snapshot_index_revision(manifest: Mapping[str, Any]) -> str:
    """Return a stable, safe index revision from snapshot metadata."""

    vector_db = manifest.get("vector_db")
    vector_db = vector_db if isinstance(vector_db, Mapping) else {}
    explicit = vector_db.get("index_revision") or manifest.get(
        "index_revision"
    )
    if explicit:
        return str(explicit)
    encoded = json.dumps(
        {
            "snapshot": build_manifest_identity(manifest),
            "vector_db": vector_db,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def add_candidate_reproduction_manifest(
    candidate: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach current fingerprints only for the new candidate contract."""

    result = dict(provenance)
    if int(candidate.get("contract_schema_version") or 1) < 2:
        return result
    if manifest is None:
        result["reproduction_manifest"] = candidate.get(
            "reproduction_manifest"
        )
        return result
    result["reproduction_manifest"] = (
        build_runtime_reproduction_manifest(
            repo_root=REPO_ROOT,
            data_revision=str(result.get("data_revision") or ""),
            index_revision=snapshot_index_revision(manifest),
        )
    )
    return result


def validate_candidate_for_run(
    candidate: Mapping[str, Any],
    *,
    run_kind: str,
) -> None:
    """Reject legacy, unapproved, or lifecycle-ineligible candidates early."""
    if candidate.get("integrity_status") != "valid":
        raise CandidateValidationError(
            "candidate must be a valid persisted schema-v2 artifact"
        )
    build_candidate_evaluation_case(candidate)
    required_status = "ready" if run_kind == "baseline" else "fixing"
    if candidate.get("triage_status") != required_status:
        raise CandidateValidationError(
            f"{run_kind} is not available in {candidate.get('triage_status')}"
        )


def _print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, allow_nan=False))


def _error(stage: str, exc: Exception) -> int:
    _print_json(
        {
            "status": "error",
            "stage": stage,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    )
    return 2


def record_blocked_snapshot_attempt(
    candidate: Mapping[str, Any],
    *,
    args: argparse.Namespace,
    stage: str,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist an auditable blocked attempt without importing or invoking graph."""
    if manifest is None:
        provenance = {
            "backend_mode": "fixed_snapshot",
            "snapshot_id": None,
            "snapshot_available": False,
            "data_revision": "snapshot-unavailable",
            "config_fingerprint": hashlib.sha256(
                f"fixed_snapshot:{stage}".encode("utf-8")
            ).hexdigest(),
        }
    else:
        provenance = build_candidate_provenance(manifest)
        provenance["snapshot_available"] = False
        provenance["data_revision"] = (
            str(provenance.get("data_revision") or "")
            or "snapshot-unavailable"
        )
    provenance = add_candidate_reproduction_manifest(
        candidate,
        provenance,
        manifest=manifest,
    )

    def graph_must_not_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("blocked snapshot attempt invoked graph")

    return run_candidate_evaluation(
        candidate,
        graph_must_not_run,
        output_dir=args.output_dir,
        run_kind=args.run_kind,
        provenance=provenance,
        latency_threshold_seconds=args.latency_threshold_seconds,
    )


def _blocked_error(
    candidate: Mapping[str, Any],
    *,
    args: argparse.Namespace,
    stage: str,
    error_type: str,
    manifest: Mapping[str, Any] | None = None,
) -> int:
    try:
        run = record_blocked_snapshot_attempt(
            candidate,
            args=args,
            stage=stage,
            manifest=manifest,
        )
    except Exception as exc:
        return _error("record_blocked_snapshot", exc)
    _print_json(
        {
            "status": "error",
            "stage": stage,
            "error_type": error_type,
            "json_path": run["json_path"],
            "run_id": run["run_id"],
            "run_status": run["run_status"],
            "blocked_reason": run["blocked_reason"],
        }
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        candidate = load_regression_candidate(args.candidate)
        validate_candidate_for_run(candidate, run_kind=args.run_kind)
    except Exception as exc:
        return _error("load_candidate", exc)

    try:
        dataset = load_evaluation_dataset(args.dataset)
    except Exception as exc:
        return _error("load_dataset", exc)

    manifest_path = args.manifest or (
        Path(args.snapshot_root) / "manifest.json"
    )
    try:
        manifest = load_evaluation_snapshot_manifest(manifest_path)
    except Exception as exc:
        return _blocked_error(
            candidate,
            args=args,
            stage="load_manifest",
            error_type=type(exc).__name__,
        )

    try:
        validation = validate_evaluation_snapshot(
            dataset, manifest, args.snapshot_root
        )
    except Exception as exc:
        return _blocked_error(
            candidate,
            args=args,
            stage="validate_snapshot",
            error_type=type(exc).__name__,
            manifest=manifest,
        )
    if validation.get("status") != "pass":
        return _blocked_error(
            candidate,
            args=args,
            stage="validate_snapshot",
            error_type="SnapshotValidationError",
            manifest=manifest,
        )

    os.environ.update(build_snapshot_environment(validation))
    provenance = add_candidate_reproduction_manifest(
        candidate,
        build_candidate_provenance(manifest),
        manifest=manifest,
    )

    try:
        # Import only after the fixed snapshot environment is installed.
        from src.graphs.main_graph import graph_app

        run = run_candidate_evaluation(
            candidate,
            graph_app.invoke,
            output_dir=args.output_dir,
            run_kind=args.run_kind,
            provenance=provenance,
            latency_threshold_seconds=args.latency_threshold_seconds,
        )
    except Exception as exc:
        return _error("run_graph", exc)

    _print_json(
        {
            "status": "ok",
            "json_path": run["json_path"],
            "run_id": run["run_id"],
            "run_status": run["run_status"],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
