import os
import subprocess
import sys

from scripts.run_evaluation_snapshot import build_snapshot_environment, parse_args


def test_build_snapshot_environment_sets_data_source_paths_without_mutating_base_env(tmp_path):
    db_path = tmp_path / "reports.db"
    faiss_dir = tmp_path / "vector_db"
    validation = {"db_path": str(db_path), "faiss_dir": str(faiss_dir)}
    base_env = {"PYTHONPATH": "src", "DB_PATH": "old.db"}

    env = build_snapshot_environment(validation, base_env=base_env)

    assert env["DB_PATH"] == str(db_path)
    assert env["FAISS_DIR"] == str(faiss_dir)
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert base_env["DB_PATH"] == "old.db"


def test_parse_args_accepts_multiple_case_ids():
    args = parse_args(
        [
            "--dataset",
            "tests/fixtures/evaluation_dataset.json",
            "--snapshot-root",
            "tests/fixtures/eval_snapshot",
            "--output-dir",
            "debug/evaluation_runs",
            "--case-id",
            "case-a",
            "--case-id",
            "case-b",
            "--latency-threshold-seconds",
            "12.5",
        ]
    )

    assert args.case_id == ["case-a", "case-b"]
    assert args.latency_threshold_seconds == 12.5


def test_runner_script_executes_from_file_path_with_json_error(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_evaluation_snapshot.py",
            "--dataset",
            "tests/fixtures/evaluation_dataset.json",
            "--snapshot-root",
            str(tmp_path / "missing_snapshot"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert completed.returncode == 2
    assert '"stage": "load_manifest"' in completed.stdout
    assert "ModuleNotFoundError" not in completed.stderr
