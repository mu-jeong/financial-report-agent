"""Run the evaluation dataset against a fixed DB/FAISS snapshot.

This script is intentionally separate from the Streamlit process so DB_PATH and
FAISS_DIR can be set before src.graphs.main_graph imports config and builds the
graph.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.monitoring import (
    load_evaluation_dataset,
    load_evaluation_snapshot_manifest,
    run_evaluation_dataset,
    validate_evaluation_snapshot,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Finance LLM evaluation on a fixed snapshot.")
    parser.add_argument("--dataset", default="tests/fixtures/evaluation_dataset.json")
    parser.add_argument("--snapshot-root", default="tests/fixtures/eval_snapshot")
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--output-dir", default="debug/evaluation_runs")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--latency-threshold-seconds", type=float, default=30.0)
    return parser.parse_args(argv)


def build_snapshot_environment(
    validation: dict[str, Any],
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a child-process environment pointing at snapshot data sources."""
    env = dict(os.environ if base_env is None else base_env)
    env["DB_PATH"] = str(validation["db_path"])
    env["FAISS_DIR"] = str(validation["faiss_dir"])
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dataset = load_evaluation_dataset(args.dataset)
    manifest_path = args.manifest or (Path(args.snapshot_root) / "manifest.json")
    try:
        manifest = load_evaluation_snapshot_manifest(manifest_path)
    except FileNotFoundError as exc:
        _print_json({"status": "error", "error": str(exc), "stage": "load_manifest"})
        return 2

    validation = validate_evaluation_snapshot(dataset, manifest, args.snapshot_root)
    if validation["status"] != "pass":
        _print_json({"status": "error", "stage": "validate_snapshot", "validation": validation})
        return 2

    os.environ.update(build_snapshot_environment(validation))

    try:
        # Import after DB_PATH/FAISS_DIR have been set. graph_app is built at import time.
        from src.graphs.main_graph import graph_app

        run = run_evaluation_dataset(
            dataset,
            graph_app.invoke,
            output_dir=args.output_dir,
            selected_case_ids=args.case_id or None,
            latency_threshold_seconds=args.latency_threshold_seconds,
            execution_mode="fixed_snapshot",
            data_source={
                "snapshot_root": validation["snapshot_root"],
                "db_path": validation["db_path"],
                "faiss_dir": validation["faiss_dir"],
                "manifest": manifest,
            },
        )
    except Exception as exc:
        _print_json({"status": "error", "stage": "run_graph", "error": str(exc)})
        return 1
    _print_json({"status": "ok", "json_path": run["json_path"], "run_id": run["run_id"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
