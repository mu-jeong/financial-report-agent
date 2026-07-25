"""Evaluator for the Streamlit first-render latency and warmup UX contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RESULT_PREFIX = "__GUI_STARTUP_RESULT__="


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


first_render_seconds, first_full_run_seconds = measured_run()
second_render_seconds, second_full_run_seconds = measured_run()

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
                "DB_PATH": str(temp_root / "reports.db"),
                "FAISS_DIR": str(temp_root / "vector_db"),
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


def main() -> int:
    try:
        ui_result, _ = _run_app_test()
    except Exception as exc:
        print(json.dumps({"status": "fail", "app_test_error": str(exc)}, ensure_ascii=False))
        return 1

    regression = _run_regression_tests()
    combined_status = " ".join(ui_result["status_texts"])
    checks = {
        "cold_first_render_lte_5s": ui_result["first_render_seconds"] <= 5.0,
        "second_rerun_lte_2s": ui_result["second_render_seconds"] <= 2.0,
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
