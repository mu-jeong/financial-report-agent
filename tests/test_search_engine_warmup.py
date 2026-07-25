"""Process-local lazy search-engine warmup contracts."""

from __future__ import annotations

import threading

from apps.gui import search_engine


def test_warmup_starts_one_background_import_and_reuses_ready_graph(monkeypatch):
    registry = search_engine._new_search_engine_registry()
    monkeypatch.setattr(search_engine, "_search_engine_registry", lambda: registry)
    monkeypatch.setattr(search_engine, "POST_RENDER_WARMUP_DELAY_SECONDS", 0)

    import_started = threading.Event()
    allow_import = threading.Event()

    class FakeGraph:
        @staticmethod
        def invoke(graph_input, *, config):
            return {"graph_input": graph_input, "config": config}

    graph_app = FakeGraph()
    load_calls: list[bool] = []

    def load_graph_app():
        load_calls.append(True)
        import_started.set()
        assert allow_import.wait(timeout=2)
        return graph_app

    monkeypatch.setattr(search_engine, "_import_graph_app", load_graph_app)

    first = search_engine.start_search_engine_warmup()
    assert first["state"] == "warming"
    assert not import_started.wait(timeout=0.05)
    search_engine.release_background_warmup()
    assert import_started.wait(timeout=2)

    second = search_engine.start_search_engine_warmup()
    assert second["state"] == "warming"
    assert len(load_calls) == 1

    allow_import.set()
    registry["worker"].join(timeout=2)

    assert search_engine.get_search_engine_status()["state"] == "ready"
    assert search_engine.wait_for_search_engine(timeout_seconds=0.1) is graph_app
    assert len(load_calls) == 1


def test_failed_warmup_can_retry_and_queued_invoke_runs_automatically(monkeypatch):
    registry = search_engine._new_search_engine_registry()
    monkeypatch.setattr(search_engine, "_search_engine_registry", lambda: registry)
    monkeypatch.setattr(search_engine, "POST_RENDER_WARMUP_DELAY_SECONDS", 0)

    class FakeGraph:
        def __init__(self):
            self.calls: list[tuple] = []

        def invoke(self, graph_input, *, config):
            self.calls.append((graph_input, config))
            return {"generation": "완료"}

    graph_app = FakeGraph()
    load_attempts = iter([RuntimeError("first load failed"), graph_app])

    def load_graph_app():
        result = next(load_attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(search_engine, "_import_graph_app", load_graph_app)

    search_engine.start_search_engine_warmup()
    search_engine.release_background_warmup()
    registry["worker"].join(timeout=2)
    assert search_engine.get_search_engine_status()["state"] == "failed"

    monkeypatch.setattr(search_engine, "POST_RENDER_WARMUP_DELAY_SECONDS", 60)
    result = search_engine.invoke_graph(
        {"question": "질문"},
        config={"configurable": {"thread_id": "thread-1"}},
        timeout_seconds=2,
    )

    assert result == {"generation": "완료"}
    assert search_engine.get_search_engine_status()["state"] == "ready"
    assert graph_app.calls == [
        (
            {"question": "질문"},
            {"configurable": {"thread_id": "thread-1"}},
        )
    ]
