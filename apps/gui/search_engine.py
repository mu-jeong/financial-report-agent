"""Process-local, non-blocking initialization for the search graph."""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Any

LOGGER = logging.getLogger(__name__)
DEFAULT_WARMUP_TIMEOUT_SECONDS = 120.0


class SearchEngineUnavailable(RuntimeError):
    """Raised when the lazily initialized search graph is unavailable."""


def _new_search_engine_registry() -> dict[str, Any]:
    return {
        "state": "idle",
        "graph_app": None,
        "retrieval_runtime": None,
        "error": None,
        "started_at": None,
        "ready_at": None,
        "lock": threading.RLock(),
        "ready_event": threading.Event(),
        "ui_ready_event": threading.Event(),
        "demand_event": threading.Event(),
        "ui_render_generation": 0,
        "worker": None,
    }


_PROCESS_REGISTRY = _new_search_engine_registry()


def _search_engine_registry() -> dict[str, Any]:
    """Keep one graph and one warmup worker for the Streamlit process."""
    return _PROCESS_REGISTRY


def _status_snapshot(registry: dict[str, Any]) -> dict[str, Any]:
    with registry["lock"]:
        error = registry["error"]
        return {
            "state": registry["state"],
            "started_at": registry["started_at"],
            "ready_at": registry["ready_at"],
            "failure_type": type(error).__name__ if error is not None else None,
            "ui_render_generation": registry["ui_render_generation"],
        }


def get_search_engine_status() -> dict[str, Any]:
    return _status_snapshot(_search_engine_registry())


def get_retrieval_runtime_provenance() -> dict[str, Any] | None:
    """Return the runtime identity validated by the warmup bootstrap."""

    registry = _search_engine_registry()
    with registry["lock"]:
        value = registry.get("retrieval_runtime")
        return dict(value) if isinstance(value, dict) else None


def _import_graph_app():
    """Validate retrieval, then import the expensive graph in the worker."""
    config_module = importlib.import_module("src.configs.config")
    bootstrap_module = importlib.import_module("src.retrieval.bootstrap")
    selection = bootstrap_module.reconcile_and_inspect_runtime(
        config_module.DATA_ROOT,
        allow_live_writer_read=True,
        prefer_fast_read=True,
    )
    registry = _search_engine_registry()
    with registry["lock"]:
        registry["retrieval_runtime"] = {
            "mode": selection.mode,
            "active_snapshot_id": selection.active_snapshot_id,
            "active_build_id": selection.active_build_id,
            "publication_generation": selection.publication_generation,
            "write_epoch": selection.write_epoch,
            "degraded": selection.degraded,
            "initialization_state": selection.initialization_state,
        }
    module = importlib.import_module("src.graphs.main_graph")
    graph_app = module.graph_app
    if not callable(getattr(graph_app, "invoke", None)):
        raise TypeError("src.graphs.main_graph.graph_app must expose invoke()")
    return graph_app


def _load_search_engine(registry: dict[str, Any]) -> None:
    # Do not compete with the first Streamlit render. A submitted question
    # bypasses the render gate so its worker can proceed immediately.
    while not registry["demand_event"].is_set():
        if registry["ui_ready_event"].wait(timeout=0.05):
            break
    try:
        graph_app = _import_graph_app()
    except Exception as exc:
        LOGGER.exception("Search-engine background warmup failed")
        with registry["lock"]:
            registry["state"] = "failed"
            registry["error"] = exc
            registry["ready_at"] = None
            registry["ready_event"].set()
        return

    with registry["lock"]:
        registry["graph_app"] = graph_app
        registry["error"] = None
        registry["state"] = "ready"
        registry["ready_at"] = time.time()
        registry["ready_event"].set()


def start_search_engine_warmup(*, retry: bool = False) -> dict[str, Any]:
    """Start one daemon warmup worker and return immediately."""
    registry = _search_engine_registry()
    with registry["lock"]:
        state = registry["state"]
        if state in {"warming", "ready"} or (state == "failed" and not retry):
            return _status_snapshot(registry)

        registry["state"] = "warming"
        registry["graph_app"] = None
        registry["retrieval_runtime"] = None
        registry["error"] = None
        registry["started_at"] = time.time()
        registry["ready_at"] = None
        registry["ready_event"].clear()
        registry["ui_ready_event"].clear()
        registry["demand_event"].clear()
        worker = threading.Thread(
            target=_load_search_engine,
            args=(registry,),
            name="search-engine-warmup",
            daemon=True,
        )
        registry["worker"] = worker

    try:
        worker.start()
    except Exception as exc:
        LOGGER.exception("Unable to start search-engine warmup worker")
        with registry["lock"]:
            registry["state"] = "failed"
            registry["error"] = exc
            registry["ready_event"].set()
    return _status_snapshot(registry)


def release_background_warmup() -> dict[str, Any]:
    """Signal that the first UI script run has emitted all of its elements."""
    registry = _search_engine_registry()
    with registry["lock"]:
        registry["ui_render_generation"] += 1
        registry["ui_ready_event"].set()
    return _status_snapshot(registry)


def retry_search_engine_warmup() -> dict[str, Any]:
    status = start_search_engine_warmup(retry=True)
    _search_engine_registry()["demand_event"].set()
    return status


def wait_for_search_engine(
    *,
    timeout_seconds: float = DEFAULT_WARMUP_TIMEOUT_SECONDS,
    retry_failed: bool = False,
):
    """Wait outside the UI thread until the shared graph is ready."""
    status = start_search_engine_warmup(retry=retry_failed)
    registry = _search_engine_registry()
    registry["demand_event"].set()
    if status["state"] != "ready":
        if not registry["ready_event"].wait(timeout=max(0.0, timeout_seconds)):
            raise SearchEngineUnavailable(
                "Search engine initialization timed out."
            )

    with registry["lock"]:
        if registry["state"] == "ready" and registry["graph_app"] is not None:
            return registry["graph_app"]
        error = registry["error"]

    unavailable = SearchEngineUnavailable("Search engine initialization failed.")
    if error is not None:
        raise unavailable from error
    raise unavailable


def invoke_graph(
    graph_input: dict,
    *,
    config: dict,
    timeout_seconds: float = DEFAULT_WARMUP_TIMEOUT_SECONDS,
) -> dict:
    """Invoke the shared graph, retrying one prior failed warmup."""
    graph_app = wait_for_search_engine(
        timeout_seconds=timeout_seconds,
        retry_failed=True,
    )
    return graph_app.invoke(graph_input, config=config)
