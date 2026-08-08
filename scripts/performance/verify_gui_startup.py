"""Evaluator for the Streamlit rerun latency and status-cache contract."""

from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RESULT_PREFIX = "__GUI_STARTUP_RESULT__="
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


APP_TEST_PROGRAM = r"""
import json
import threading
import time

from streamlit.testing.v1 import AppTest
from apps.gui import search_engine

app = AppTest.from_file("apps/gui/app.py", default_timeout=45)

def measured_run():
    initial_generation = search_engine.get_search_engine_status()[
        "ui_render_generation"
    ]
    marker = {}
    marker_seen = threading.Event()
    stop_observer = threading.Event()
    started = time.perf_counter()

    def observe_ui_release():
        while not stop_observer.wait(timeout=0.002):
            generation = search_engine.get_search_engine_status()[
                "ui_render_generation"
            ]
            if generation > initial_generation:
                marker["seconds"] = time.perf_counter() - started
                marker_seen.set()
                return

    observer = threading.Thread(
        target=observe_ui_release,
        name="gui-startup-evaluator",
        daemon=True,
    )
    observer.start()
    app.run()
    full_run_seconds = time.perf_counter() - started
    marker_seen.wait(timeout=0.1)
    stop_observer.set()
    observer.join(timeout=0.1)
    return marker.get("seconds", full_run_seconds), full_run_seconds


measurements = [measured_run() for _ in range(6)]
first_render_seconds, first_full_run_seconds = measurements[0]
warm_measurements = measurements[1:]
second_render_seconds, second_full_run_seconds = warm_measurements[0]
warm_full_run_seconds = [measurement[1] for measurement in warm_measurements]

status_texts = []
for attribute in ("info", "caption", "success", "error"):
    for element in getattr(app, attribute, []):
        value = getattr(element, "value", "")
        if value:
            status_texts.append(str(value))

payload = {
    "first_render_seconds": round(first_render_seconds, 3),
    "first_full_run_seconds": round(first_full_run_seconds, 3),
    "second_render_seconds": round(second_render_seconds, 3),
    "second_full_run_seconds": round(second_full_run_seconds, 3),
    "warm_full_run_seconds": [round(value, 3) for value in warm_full_run_seconds],
    "warm_full_run_median_seconds": round(
        sorted(warm_full_run_seconds)[len(warm_full_run_seconds) // 2],
        3,
    ),
    "warm_full_run_max_seconds": round(max(warm_full_run_seconds), 3),
    "exception_count": len(app.exception),
    "chat_input_count": len(app.chat_input),
    "status_texts": status_texts,
}
print("__GUI_STARTUP_RESULT__=" + json.dumps(payload, ensure_ascii=False))
"""


def _run_app_test() -> tuple[dict, str]:
    with tempfile.TemporaryDirectory(prefix="finance-llm-gui-perf-") as temp_dir:
        temp_root = Path(temp_dir)
        environment = os.environ.copy()
        environment.update(
            {
                "DATA_ROOT": str(temp_root),
                "CONVERSATION_DB_PATH": str(temp_root / "conversations.db"),
                "SAVE_DIR": str(temp_root / "downloaded"),
                "REPORT_PDF_DIR": str(temp_root / "downloaded"),
                "MONITORING_MODE": "false",
                "PYTHONUTF8": "1",
            }
        )
        completed = subprocess.run(
            [sys.executable, "-c", APP_TEST_PROGRAM],
            cwd=REPOSITORY_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
    payload = None
    for line in completed.stdout.splitlines():
        if line.startswith(RESULT_PREFIX):
            payload = json.loads(line.removeprefix(RESULT_PREFIX))
    diagnostics = "\n".join(
        part
        for part in (
            completed.stdout.strip(),
            completed.stderr.strip(),
        )
        if part
    )
    if completed.returncode != 0 or payload is None:
        raise RuntimeError(
            f"AppTest failed with exit code {completed.returncode}:\n{diagnostics}"
        )
    return payload, diagnostics


def _run_regression_tests() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_search_engine_warmup.py",
            "tests/test_gui_view_contracts.py",
            "tests/test_chat_ui_helpers.py",
            "tests/test_gui_status_cache.py",
            "tests/test_monitoring_jobs.py",
            "tests/test_data_update_jobs.py",
            "-q",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def _run_native_status_cache_probe() -> dict:
    from apps.gui import status_cache

    with tempfile.TemporaryDirectory(prefix="finance-llm-native-cache-") as temp_dir:
        data_root = Path(temp_dir) / "data"
        catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
        save_dir = data_root / "downloaded"
        catalog.parent.mkdir(parents=True)
        save_dir.mkdir()
        with closing(sqlite3.connect(catalog)) as connection:
            with connection:
                connection.executescript(
                    """
                CREATE TABLE retrieval_runtime (
                    runtime_id INTEGER PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    active_snapshot_id TEXT,
                    active_build_id TEXT,
                    predecessor_snapshot_id TEXT,
                    publication_generation INTEGER NOT NULL,
                    write_epoch INTEGER NOT NULL,
                    degraded INTEGER NOT NULL,
                    write_enabled INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO retrieval_runtime VALUES (
                    1, 3, 'snapshot-1', 'build-1', NULL,
                    1, 1, 0, 1,
                    '2026-08-04T00:00:00Z', '2026-08-04T00:00:00Z'
                );
                    """
                )

        evidence_root = data_root / "retrieval" / "v2" / "evidence"
        for generation in range(52):
            publication = evidence_root / f"publication-{generation:03d}"
            publication.mkdir(parents=True)
            (publication / "commit-intent.json").write_text(
                json.dumps({"generation": generation}),
                encoding="utf-8",
            )
            (publication / "committed-floor.json").write_text(
                json.dumps({"generation": generation}),
                encoding="utf-8",
            )

        calls: list[int] = []
        original_loader = status_cache.status_module.get_data_status
        original_job_reader = status_cache.data_update_jobs.read_status

        def fake_loader(**_kwargs):
            calls.append(len(calls) + 1)
            return {"load_count": len(calls)}

        try:
            status_cache.status_module.get_data_status = fake_loader
            status_cache.data_update_jobs.read_status = lambda: None
            status_cache.clear()
            arguments = {
                "save_dir": str(save_dir),
                "data_root": str(data_root),
            }
            first = status_cache.get_data_status(**arguments)
            second = status_cache.get_data_status(**arguments)
            with closing(sqlite3.connect(catalog)) as writer:
                with writer:
                    writer.execute("PRAGMA journal_mode = WAL")
                    writer.execute(
                        """
                    UPDATE retrieval_runtime
                    SET write_epoch = 2,
                        updated_at = '2026-08-04T00:00:01Z'
                    WHERE runtime_id = 1
                        """
                    )
            third = status_cache.get_data_status(**arguments)
            revision_seconds = []
            for _ in range(5):
                started = time.perf_counter()
                status_cache._status_revision(
                    data_root=str(data_root),
                )
                revision_seconds.append(time.perf_counter() - started)
        finally:
            status_cache.status_module.get_data_status = original_loader
            status_cache.data_update_jobs.read_status = original_job_reader
            status_cache.clear()

    return {
        "loader_calls": len(calls),
        "first_load_count": first["load_count"],
        "second_load_count": second["load_count"],
        "third_load_count": third["load_count"],
        "revision_median_seconds": round(statistics.median(revision_seconds), 4),
        "revision_max_seconds": round(max(revision_seconds), 4),
    }


def main() -> int:
    try:
        ui_result, _ = _run_app_test()
    except Exception as exc:
        print(json.dumps({"status": "fail", "app_test_error": str(exc)}, ensure_ascii=False))
        return 1

    try:
        cache_probe = _run_native_status_cache_probe()
    except Exception as exc:
        print(json.dumps({"status": "fail", "cache_probe_error": str(exc)}, ensure_ascii=False))
        return 1

    regression = _run_regression_tests()
    combined_status = " ".join(ui_result["status_texts"])
    checks = {
        "cold_first_render_lte_5s": ui_result["first_render_seconds"] <= 5.0,
        "warm_full_rerun_max_lte_500ms": (
            ui_result["warm_full_run_max_seconds"] <= 0.5
        ),
        "native_cache_hits_then_wal_invalidates": (
            cache_probe["loader_calls"] == 2
            and cache_probe["first_load_count"] == 1
            and cache_probe["second_load_count"] == 1
            and cache_probe["third_load_count"] == 2
        ),
        "native_revision_median_lte_150ms": (
            cache_probe["revision_median_seconds"] <= 0.15
        ),
        "native_revision_max_lte_150ms": (
            cache_probe["revision_max_seconds"] <= 0.15
        ),
        "no_app_test_exceptions": ui_result["exception_count"] == 0,
        "chat_input_rendered_once": ui_result["chat_input_count"] == 1,
        "engine_preparation_state_visible": (
            "검색 엔진 준비" in combined_status
            or "검색 엔진을 준비" in combined_status
        ),
        "targeted_regressions_pass": regression.returncode == 0,
    }
    payload = {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "ui": ui_result,
        "native_cache_probe": cache_probe,
        "pytest_returncode": regression.returncode,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if regression.stdout:
        print(regression.stdout)
    if regression.stderr:
        print(regression.stderr, file=sys.stderr)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
