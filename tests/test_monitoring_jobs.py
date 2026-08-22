from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from apps.gui import monitoring_jobs


@pytest.fixture(autouse=True)
def _clear_monitoring_jobs() -> None:
    monitoring_jobs.clear()
    yield
    monitoring_jobs.clear()


def _wait_for_terminal(job_key: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = monitoring_jobs.get_job(job_key)
        if snapshot and snapshot["state"] != "running":
            return snapshot
        time.sleep(0.005)
    raise AssertionError(f"monitoring job did not finish: {job_key}")


def test_start_job_returns_while_work_continues_in_background():
    entered = threading.Event()
    release = threading.Event()

    def target(*, value: int) -> dict:
        entered.set()
        assert release.wait(timeout=2)
        return {"value": value}

    job_id, started = monitoring_jobs.start_job(
        "evaluation",
        target,
        kwargs={"value": 7},
    )

    assert started is True
    assert entered.wait(timeout=1)
    assert monitoring_jobs.get_job("evaluation") == {
        "job_id": job_id,
        "key": "evaluation",
        "state": "running",
        "result": None,
        "error": None,
    }

    release.set()
    assert _wait_for_terminal("evaluation") == {
        "job_id": job_id,
        "key": "evaluation",
        "state": "succeeded",
        "result": {"value": 7},
        "error": None,
    }


def test_duplicate_running_job_is_not_started_twice():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def target() -> dict:
        calls.append("run")
        entered.set()
        assert release.wait(timeout=2)
        return {"ok": True}

    first_id, first_started = monitoring_jobs.start_job("evaluation", target)
    assert entered.wait(timeout=1)
    second_id, second_started = monitoring_jobs.start_job("evaluation", target)

    assert first_started is True
    assert second_started is False
    assert second_id == first_id
    assert calls == ["run"]

    release.set()
    assert _wait_for_terminal("evaluation")["state"] == "succeeded"


def test_failed_job_records_error_without_raising_on_ui_thread():
    def target() -> dict:
        raise RuntimeError("evaluation exploded")

    _job_id, started = monitoring_jobs.start_job("evaluation", target)

    assert started is True
    snapshot = _wait_for_terminal("evaluation")
    assert snapshot["state"] == "failed"
    assert snapshot["result"] is None
    assert snapshot["error"] == "RuntimeError: evaluation exploded"


def test_job_results_are_returned_as_mutation_isolated_snapshots():
    monitoring_jobs.start_job("evaluation", lambda: {"nested": {"value": 1}})
    first = _wait_for_terminal("evaluation")
    first["result"]["nested"]["value"] = 999

    second = monitoring_jobs.get_job("evaluation")

    assert second["result"]["nested"]["value"] == 1


def test_acknowledging_terminal_job_releases_it_without_disrupting_active_job():
    release = threading.Event()

    def active_target() -> dict:
        assert release.wait(timeout=2)
        return {"active": True}

    active_id, _started = monitoring_jobs.start_job("active", active_target)
    terminal_id, _started = monitoring_jobs.start_job(
        "terminal",
        lambda: {"large_result": "x" * 10_000},
    )
    assert _wait_for_terminal("terminal")["state"] == "succeeded"

    assert monitoring_jobs.acknowledge_job("terminal", terminal_id) is True
    assert monitoring_jobs.get_job("terminal") is None
    assert monitoring_jobs.acknowledge_job("active", active_id) is False
    assert monitoring_jobs.get_job("active")["state"] == "running"

    release.set()
    assert _wait_for_terminal("active")["state"] == "succeeded"


def test_terminal_job_retention_is_bounded():
    total_jobs = monitoring_jobs._MAX_RETAINED_TERMINAL_JOBS + 3
    for index in range(total_jobs):
        key = f"terminal-{index}"
        monitoring_jobs.start_job(key, lambda: {"value": "x" * 1_000})
        assert _wait_for_terminal(key)["state"] == "succeeded"

    retained = [
        monitoring_jobs.get_job(f"terminal-{index}")
        for index in range(total_jobs)
    ]

    assert sum(job is not None for job in retained) == (
        monitoring_jobs._MAX_RETAINED_TERMINAL_JOBS
    )


def test_failed_job_renderer_persists_error_and_enables_immediate_retry(
    monkeypatch,
):
    class RerunRequested(Exception):
        pass

    session_state = {}
    errors = []
    running_messages = []

    def request_rerun(*, scope: str) -> None:
        assert scope == "app"
        raise RerunRequested

    fake_streamlit = SimpleNamespace(
        session_state=session_state,
        fragment=lambda **_kwargs: lambda function: function,
        info=running_messages.append,
        error=errors.append,
        success=lambda _message: None,
        toast=lambda *_args, **_kwargs: None,
        rerun=request_rerun,
    )
    monkeypatch.setattr(monitoring_jobs, "st", fake_streamlit)
    renderer = monitoring_jobs._build_status_renderer()
    job_key = "evaluation:session-1"

    def fail() -> dict:
        raise RuntimeError("evaluation exploded")

    monitoring_jobs.start_job(job_key, fail)
    assert _wait_for_terminal(job_key)["state"] == "failed"

    with pytest.raises(RerunRequested):
        renderer(
            job_key,
            result_state_key="latest_evaluation_run",
            running_message="running",
            success_message="finished",
            failure_prefix="Evaluation run failed",
        )

    assert monitoring_jobs.get_job(job_key) is None
    renderer(
        job_key,
        result_state_key="latest_evaluation_run",
        running_message="running",
        success_message="finished",
        failure_prefix="Evaluation run failed",
    )
    assert errors == [
        "Evaluation run failed: RuntimeError: evaluation exploded"
    ]

    release = threading.Event()
    _retry_id, retry_started = monitoring_jobs.start_job(
        job_key,
        lambda: {"retried": release.wait(timeout=2)},
    )
    assert retry_started is True
    renderer(
        job_key,
        result_state_key="latest_evaluation_run",
        running_message="running",
        success_message="finished",
        failure_prefix="Evaluation run failed",
    )
    assert running_messages == ["running"]
    assert not any("failure" in key for key in session_state)

    release.set()
    assert _wait_for_terminal(job_key)["state"] == "succeeded"


def test_evaluation_job_forwards_existing_runner_contract(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(dataset, invoke_graph, **kwargs):
        calls.append((dataset, invoke_graph, kwargs))
        return {"run_id": "run-1"}

    invoke_graph = object()
    monkeypatch.setattr(monitoring_jobs.monitoring, "run_evaluation_dataset", fake_run)

    monitoring_jobs.start_evaluation_job(
        "evaluation",
        dataset={"cases": [{"id": "case-1"}]},
        invoke_graph=invoke_graph,
        output_dir=tmp_path,
        selected_case_ids=["case-1"],
        execution_mode="native_v2",
        data_source={"mode": "native_v2"},
        latency_threshold_seconds=12.0,
    )
    snapshot = _wait_for_terminal("evaluation")

    assert snapshot["result"] == {"run_id": "run-1"}
    assert calls == [
        (
            {"cases": [{"id": "case-1"}]},
            invoke_graph,
            {
                "output_dir": tmp_path,
                "selected_case_ids": ["case-1"],
                "execution_mode": "native_v2",
                "data_source": {"mode": "native_v2"},
                "latency_threshold_seconds": 12.0,
            },
        )
    ]


def test_long_evaluation_buttons_only_enqueue_background_jobs():
    source = Path("apps/gui/monitoring_views.py").read_text(encoding="utf-8-sig")

    assert "from apps.gui import monitoring_jobs" in source
    assert "monitoring.run_evaluation_dataset(" not in source
    assert source.count("monitoring_jobs.start_evaluation_job(") == 2
    assert source.count("monitoring_jobs.session_job_key(") == 1
    assert source.count("monitoring_jobs.render_job_status(") == 1
    job_source = Path("apps/gui/monitoring_jobs.py").read_text(encoding="utf-8-sig")
    assert "@st.fragment(run_every=1)" in job_source

    app_source = Path("apps/gui/app.py").read_text(encoding="utf-8-sig")
    assert '"src.core.monitoring"' not in app_source

    search_source = Path("apps/gui/search_engine.py").read_text(encoding="utf-8-sig")
    assert '"invoke_lock"' not in search_source


def test_session_job_keys_are_stable_per_owner_and_isolated_between_sessions(
    monkeypatch,
):
    first_session = {}
    monkeypatch.setattr(
        monitoring_jobs,
        "st",
        SimpleNamespace(session_state=first_session),
    )
    first = monitoring_jobs.session_job_key("evaluation")

    assert monitoring_jobs.session_job_key("evaluation") == first

    second_session = {}
    monkeypatch.setattr(
        monitoring_jobs,
        "st",
        SimpleNamespace(session_state=second_session),
    )
    second = monitoring_jobs.session_job_key("evaluation")

    assert first != second
    assert first.startswith("evaluation:")
    assert second.startswith("evaluation:")
