from __future__ import annotations

import json
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.core.graph_observability import (
    GRAPH_SCHEMA_VERSION,
    GRAPH_TRACE_STATE_KEY,
    build_graph_manifest,
    invoke_graph_with_observability,
)


class _State(TypedDict, total=False):
    question: str
    value: int
    answer: str


def _compiled_fixture_graph():
    def prepare(state: _State) -> dict:
        return {"value": int(state.get("value") or 0) + 1}

    def answer(state: _State) -> dict:
        return {
            "value": int(state.get("value") or 0) * 2,
            "answer": "sensitive-answer",
        }

    workflow = StateGraph(_State)
    workflow.add_node("prepare", prepare)
    workflow.add_node("answer", answer)
    workflow.add_edge(START, "prepare")
    workflow.add_edge("prepare", "answer")
    workflow.add_edge("answer", END)
    return workflow.compile(checkpointer=MemorySaver(), name="fixture_graph")


def test_graph_manifest_comes_from_compiled_topology_with_stable_revision():
    graph = _compiled_fixture_graph()

    first = build_graph_manifest(graph)
    second = build_graph_manifest(graph)

    assert first == second
    assert first["graph_id"] == "fixture_graph"
    assert len(first["revision"]) == 64
    assert [node["id"] for node in first["nodes"]] == [
        "__start__",
        "prepare",
        "answer",
        "__end__",
    ]
    assert first["edges"] == [
        {"source": "__start__", "target": "prepare", "conditional": False},
        {"source": "prepare", "target": "answer", "conditional": False},
        {"source": "answer", "target": "__end__", "conditional": False},
    ]


def test_topology_capture_failure_is_persisted_without_exception_text():
    class BrokenTopologyGraph:
        name = "broken_topology"

        @staticmethod
        def get_graph(*, xray=False):
            assert xray is True
            raise RuntimeError("sensitive topology failure")

        @staticmethod
        def stream(_graph_input, *, config, **_kwargs):
            assert config["configurable"]["thread_id"] == "broken-topology"
            yield ((), "values", {"answer": "answer survives"})

    final_state = invoke_graph_with_observability(
        BrokenTopologyGraph(),
        {"question": "question survives"},
        config={"configurable": {"thread_id": "broken-topology"}},
    )

    trace = final_state[GRAPH_TRACE_STATE_KEY]
    assert final_state["answer"] == "answer survives"
    assert trace["graph_manifest"] == {
        "graph_id": "broken_topology",
        "revision": None,
        "nodes": [],
        "edges": [],
        "capture_error": {
            "code": "topology_capture_failed",
            "error_type": "RuntimeError",
        },
    }
    serialized_trace = json.dumps(trace, ensure_ascii=False)
    assert "sensitive topology failure" not in serialized_trace
    assert "question survives" not in serialized_trace


def test_invoke_only_graph_remains_a_scoped_compatibility_path():
    class InvokeOnlyGraph:
        @staticmethod
        def invoke(graph_input, *, config):
            return {
                "answer": graph_input["question"],
                "thread_id": config["configurable"]["thread_id"],
            }

    final_state = invoke_graph_with_observability(
        InvokeOnlyGraph(),
        {"question": "compatibility"},
        config={"configurable": {"thread_id": "invoke-only"}},
    )

    assert final_state == {
        "answer": "compatibility",
        "thread_id": "invoke-only",
    }
    assert GRAPH_TRACE_STATE_KEY not in final_state


def test_missing_task_stream_is_persisted_as_observability_failure():
    compiled_graph = _compiled_fixture_graph()

    class TopologyInvokeGraph:
        name = "topology_invoke"

        @staticmethod
        def get_graph(*, xray=False):
            return compiled_graph.get_graph(xray=xray)

        @staticmethod
        def invoke(graph_input, *, config):
            return compiled_graph.invoke(graph_input, config=config)

    final_state = invoke_graph_with_observability(
        TopologyInvokeGraph(),
        {"question": "fallback", "value": 2},
        config={"configurable": {"thread_id": "missing-stream"}},
    )

    trace = final_state[GRAPH_TRACE_STATE_KEY]
    assert final_state["answer"] == "sensitive-answer"
    assert trace["node_runs"] == []
    assert trace["graph_manifest"]["capture_error"] == {
        "code": "task_stream_unavailable",
        "error_type": "MissingStream",
    }


def test_graph_invocation_records_bounded_node_runs_without_state_values():
    graph = _compiled_fixture_graph()

    final_state = invoke_graph_with_observability(
        graph,
        {"question": "sensitive-question", "value": 2},
        config={"configurable": {"thread_id": "trace-test"}},
    )

    assert final_state["answer"] == "sensitive-answer"
    trace = final_state[GRAPH_TRACE_STATE_KEY]
    assert trace["graph_schema_version"] == GRAPH_SCHEMA_VERSION
    assert [run["node_id"] for run in trace["node_runs"]] == [
        "prepare",
        "answer",
    ]
    assert all(run["status"] == "completed" for run in trace["node_runs"])
    assert all(run["duration_seconds"] >= 0 for run in trace["node_runs"])
    assert all(run["invocation_index"] == 1 for run in trace["node_runs"])
    assert all("attempt" not in run for run in trace["node_runs"])
    assert all(
        run["ended_offset_seconds"] >= run["started_offset_seconds"]
        for run in trace["node_runs"]
    )
    assert trace["node_runs"][0]["result_keys"] == ["value"]
    serialized_trace = json.dumps(trace, ensure_ascii=False)
    assert "sensitive-question" not in serialized_trace
    assert "sensitive-answer" not in serialized_trace
    assert '"input"' not in serialized_trace
    assert '"result"' not in serialized_trace


def test_default_lazy_chat_graph_adapter_uses_observable_compiled_graph(monkeypatch):
    from apps.gui import chat_jobs

    compiled_graph = _compiled_fixture_graph()
    monkeypatch.setattr(
        chat_jobs.search_engine,
        "wait_for_search_engine",
        lambda **_kwargs: compiled_graph,
    )

    final_state = invoke_graph_with_observability(
        chat_jobs.graph_app,
        {"question": "adapter-question", "value": 2},
        config={"configurable": {"thread_id": "lazy-adapter-test"}},
    )

    trace = final_state[GRAPH_TRACE_STATE_KEY]
    assert final_state["answer"] == "sensitive-answer"
    assert trace["graph_manifest"]["graph_id"] == "finance_chat"
    assert [run["node_id"] for run in trace["node_runs"]] == [
        "prepare",
        "answer",
    ]


def test_nested_graph_node_runs_match_xray_manifest_node_ids():
    def inner_prepare(state: _State) -> dict:
        return {"value": int(state.get("value") or 0) + 1}

    def inner_answer(state: _State) -> dict:
        return {"value": int(state.get("value") or 0) * 2}

    inner = StateGraph(_State)
    inner.add_node("inner_prepare", inner_prepare)
    inner.add_node("inner_answer", inner_answer)
    inner.add_edge(START, "inner_prepare")
    inner.add_edge("inner_prepare", "inner_answer")
    inner.add_edge("inner_answer", END)
    inner_app = inner.compile(name="inner")

    outer = StateGraph(_State)
    outer.add_node("nested", inner_app)
    outer.add_edge(START, "nested")
    outer.add_edge("nested", END)
    graph = outer.compile(checkpointer=MemorySaver(), name="outer")

    final_state = invoke_graph_with_observability(
        graph,
        {"value": 1},
        config={"configurable": {"thread_id": "nested-test"}},
    )

    trace = final_state[GRAPH_TRACE_STATE_KEY]
    manifest_ids = {node["id"] for node in trace["graph_manifest"]["nodes"]}
    run_ids = {run["node_id"] for run in trace["node_runs"]}
    assert run_ids == {"nested:inner_prepare", "nested:inner_answer"}
    assert run_ids <= manifest_ids


def test_failed_graph_attaches_completed_and_failed_node_runs_to_error():
    def prepare(state: _State) -> dict:
        return {"value": int(state.get("value") or 0) + 1}

    def fail(_state: _State) -> dict:
        raise ValueError("sensitive failure detail")

    workflow = StateGraph(_State)
    workflow.add_node("prepare", prepare)
    workflow.add_node("fail", fail)
    workflow.add_edge(START, "prepare")
    workflow.add_edge("prepare", "fail")
    graph = workflow.compile(checkpointer=MemorySaver(), name="failure_graph")

    with pytest.raises(ValueError) as exc_info:
        invoke_graph_with_observability(
            graph,
            {"value": 1},
            config={"configurable": {"thread_id": "failure-test"}},
        )

    trace = exc_info.value.graph_trace
    assert [run["node_id"] for run in trace["node_runs"]] == ["prepare", "fail"]
    assert [run["status"] for run in trace["node_runs"]] == [
        "completed",
        "failed",
    ]
    assert "sensitive failure detail" not in json.dumps(trace, ensure_ascii=False)
