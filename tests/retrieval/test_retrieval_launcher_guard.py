from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from src.configs import config as config_module
from src.retrieval import launcher_guard
from tests.retrieval.test_retrieval_build_service import _native_seed


def test_launcher_guard_reports_validated_native_runtime(tmp_path, monkeypatch, capsys):
    data_root, _sources = _native_seed(tmp_path)
    monkeypatch.setattr(config_module, "DB_PATH", str(data_root / "reports.db"))

    result = launcher_guard.main([])

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "ok"
    assert payload["mode"] == "native"
    assert payload["predecessor_snapshot_id"] is None
    assert payload["write_epoch"] == 0
    assert payload["write_enabled"] is False


def test_launcher_guard_write_mode_fails_closed_at_epoch_zero(
    tmp_path,
    monkeypatch,
    capsys,
):
    data_root, _sources = _native_seed(tmp_path)
    monkeypatch.setattr(config_module, "DB_PATH", str(data_root / "reports.db"))

    result = launcher_guard.main(["--write"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["status"] == "blocked"
    assert payload["error"] == "RetrievalWriteBlocked"
    assert "writes are disabled" in payload["message"]


def test_supported_launchers_guard_before_update_or_graph_import():
    root = Path(__file__).resolve().parents[2]
    gui = (root / "apps" / "gui" / "app.py").read_text(encoding="utf-8")
    run_app = (root / "RUN_APP.bat").read_text(encoding="utf-8")
    quickstart = (root / "quickstart.py").read_text(encoding="utf-8")
    run_quickstart = (root / "RUN_QUICKSTART.bat").read_text(encoding="utf-8")

    assert gui.index("st.stop()") < gui.index("from src.core import data_update_jobs")
    assert gui.index("import streamlit as st") < gui.index(
        "_finish_runtime_smoke(_retrieval_runtime)"
    )
    assert gui.index("from src.graphs import main_graph") < gui.index(
        "_finish_runtime_smoke(_retrieval_runtime)"
    )
    assert run_app.index("src.retrieval.launcher_guard") < run_app.index(
        "streamlit run apps/gui/app.py"
    )
    assert quickstart.index("src.retrieval.launcher_guard") < quickstart.index(
        "src.core.report_crawler"
    )
    assert "quickstart.py %*" in run_quickstart
    assert '"--runtime-smoke"' in run_app
    quickstart_smoke_exit = (
        'if /I "%~1"=="--runtime-smoke" exit /b %EXIT_CODE%'
    )
    assert quickstart_smoke_exit in run_quickstart
    assert run_quickstart.index(quickstart_smoke_exit) < run_quickstart.index(
        "pause > nul"
    )
    assert 'if /I "%~1"=="--runtime-smoke" exit /b 1' in run_app
    assert 'if /I "%~1"=="--runtime-smoke" goto runtime_smoke' in run_app
    assert ":runtime_smoke" in run_app
    assert run_app.index(":runtime_smoke") > run_app.index(
        'if /I "%~1"=="--runtime-smoke" goto runtime_smoke'
    )


def test_gui_runtime_smoke_executes_the_gui_entrypoint(tmp_path):
    data_root, _sources = _native_seed(tmp_path)
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["DB_PATH"] = str(data_root / "reports.db")

    completed = subprocess.run(
        [sys.executable, "apps/gui/app.py", "--runtime-smoke"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "ok"
    assert payload["surface"] == "gui"
    assert payload["mode"] == "native"
    assert payload["write_epoch"] == 0
