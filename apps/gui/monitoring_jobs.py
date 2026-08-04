"""Process-local background jobs for long-running Monitoring actions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

import streamlit as st

from src.core import monitoring


_REGISTRY: dict[str, Any] = {
    "jobs": {},
    "threads": {},
    "lock": threading.RLock(),
}
_STATUS_RENDERER: Callable[..., None] | None = None
_SESSION_OWNER_KEY = "_monitoring_job_owner_id"
_MAX_RETAINED_TERMINAL_JOBS = 8


def _prune_terminal_jobs_locked() -> None:
    terminal_jobs = sorted(
        (
            (float(job.get("finished_at", 0.0)), key)
            for key, job in _REGISTRY["jobs"].items()
            if job["state"] != "running"
        ),
        key=lambda item: item[0],
    )
    for _finished_at, key in terminal_jobs[:-_MAX_RETAINED_TERMINAL_JOBS]:
        _REGISTRY["jobs"].pop(key, None)
        _REGISTRY["threads"].pop(key, None)


def _snapshot(job: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(
        {
            "job_id": job["job_id"],
            "key": job["key"],
            "state": job["state"],
            "result": job["result"],
            "error": job["error"],
        }
    )


def _run_job(
    *,
    key: str,
    job_id: str,
    target: Callable[..., dict[str, Any]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    try:
        result = target(*args, **kwargs)
    except Exception as exc:
        with _REGISTRY["lock"]:
            job = _REGISTRY["jobs"].get(key)
            if job and job["job_id"] == job_id:
                job.update(
                    state="failed",
                    result=None,
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=time.monotonic(),
                )
                _REGISTRY["threads"].pop(key, None)
                _prune_terminal_jobs_locked()
        return

    with _REGISTRY["lock"]:
        job = _REGISTRY["jobs"].get(key)
        if job and job["job_id"] == job_id:
            job.update(
                state="succeeded",
                result=deepcopy(result),
                error=None,
                finished_at=time.monotonic(),
            )
            _REGISTRY["threads"].pop(key, None)
            _prune_terminal_jobs_locked()


def start_job(
    key: str,
    target: Callable[..., dict[str, Any]],
    *,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Start once for ``key`` and return immediately with ``(id, started)``."""

    with _REGISTRY["lock"]:
        current = _REGISTRY["jobs"].get(key)
        if current and current["state"] == "running":
            return str(current["job_id"]), False

        job_id = uuid.uuid4().hex
        _REGISTRY["jobs"][key] = {
            "job_id": job_id,
            "key": key,
            "state": "running",
            "result": None,
            "error": None,
        }
        worker = threading.Thread(
            target=_run_job,
            kwargs={
                "key": key,
                "job_id": job_id,
                "target": target,
                "args": args,
                "kwargs": dict(kwargs or {}),
            },
            name=f"monitoring-{key}-{job_id[:8]}",
            daemon=True,
        )
        _REGISTRY["threads"][key] = worker

    try:
        worker.start()
    except Exception as exc:
        with _REGISTRY["lock"]:
            job = _REGISTRY["jobs"].get(key)
            if job and job["job_id"] == job_id:
                job.update(
                    state="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=time.monotonic(),
                )
                _REGISTRY["threads"].pop(key, None)
                _prune_terminal_jobs_locked()
        return job_id, False
    return job_id, True


def start_evaluation_job(
    key: str,
    *,
    dataset: dict[str, Any],
    invoke_graph: Callable[..., dict[str, Any]],
    output_dir: str | Path,
    selected_case_ids: list[str],
    execution_mode: str,
    data_source: dict[str, Any],
    latency_threshold_seconds: float | None = None,
) -> tuple[str, bool]:
    """Run the existing evaluation contract outside the Streamlit thread."""

    options: dict[str, Any] = {
        "output_dir": output_dir,
        "selected_case_ids": list(selected_case_ids),
        "execution_mode": execution_mode,
        "data_source": deepcopy(data_source),
    }
    if latency_threshold_seconds is not None:
        options["latency_threshold_seconds"] = float(latency_threshold_seconds)
    return start_job(
        key,
        monitoring.run_evaluation_dataset,
        args=(deepcopy(dataset), invoke_graph),
        kwargs=options,
    )


def get_job(key: str) -> dict[str, Any] | None:
    with _REGISTRY["lock"]:
        job = _REGISTRY["jobs"].get(key)
        return _snapshot(job) if job else None


def acknowledge_job(key: str, job_id: str) -> bool:
    """Forget one terminal job after its result has reached session state."""

    with _REGISTRY["lock"]:
        job = _REGISTRY["jobs"].get(key)
        if (
            not job
            or job["job_id"] != job_id
            or job["state"] == "running"
        ):
            return False
        _REGISTRY["jobs"].pop(key, None)
        _REGISTRY["threads"].pop(key, None)
        return True


def session_job_key(base_key: str) -> str:
    """Namespace UI jobs to one Streamlit session."""

    owner_id = st.session_state.get(_SESSION_OWNER_KEY)
    if not isinstance(owner_id, str) or not owner_id:
        owner_id = uuid.uuid4().hex
        st.session_state[_SESSION_OWNER_KEY] = owner_id
    return f"{base_key}:{owner_id}"


def _build_status_renderer() -> Callable[..., None]:
    @st.fragment(run_every=1)
    def render(
        job_key: str,
        *,
        result_state_key: str,
        running_message: str,
        success_message: str,
        failure_prefix: str,
    ) -> None:
        failure_state_key = f"_monitoring_job_failure_{job_key}"
        job = get_job(job_key)
        if not job:
            persisted_failure = st.session_state.get(failure_state_key)
            if persisted_failure:
                st.error(persisted_failure)
            return
        if job["state"] == "running":
            st.session_state.pop(failure_state_key, None)
            st.info(running_message)
            return
        if job["state"] == "failed":
            st.session_state[failure_state_key] = (
                f"{failure_prefix}: {job.get('error') or 'unknown error'}"
            )
            acknowledge_job(job_key, job["job_id"])
            st.rerun(scope="app")
            return

        st.session_state.pop(failure_state_key, None)
        handled_key = f"_handled_monitoring_job_{job_key}"
        if st.session_state.get(handled_key) != job["job_id"]:
            st.session_state[result_state_key] = job["result"]
            st.session_state[handled_key] = job["job_id"]
            acknowledge_job(job_key, job["job_id"])
            st.toast(success_message, icon="✅")
            st.rerun(scope="app")
        acknowledge_job(job_key, job["job_id"])
        st.success(success_message)

    return render


def render_job_status(
    job_key: str,
    *,
    result_state_key: str,
    running_message: str,
    success_message: str,
    failure_prefix: str,
) -> None:
    """Poll one job without recreating the fragment on every app rerun."""

    global _STATUS_RENDERER
    with _REGISTRY["lock"]:
        if _STATUS_RENDERER is None:
            _STATUS_RENDERER = _build_status_renderer()
        renderer = _STATUS_RENDERER
    renderer(
        job_key,
        result_state_key=result_state_key,
        running_message=running_message,
        success_message=success_message,
        failure_prefix=failure_prefix,
    )


def clear() -> None:
    """Clear completed process-local state; intended for tests and recovery."""

    with _REGISTRY["lock"]:
        _REGISTRY["jobs"].clear()
        _REGISTRY["threads"].clear()
