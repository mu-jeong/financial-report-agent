"""Persistable topology and task telemetry for compiled LangGraph runs."""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any


GRAPH_SCHEMA_VERSION = 1
GRAPH_TRACE_STATE_KEY = "_graph_observability"

_MAX_MANIFEST_NODES = 128
_MAX_MANIFEST_EDGES = 256
_MAX_NODE_RUNS = 256
_MAX_RESULT_KEYS = 32
_MAX_TEXT_LENGTH = 160
_BOUNDARY_NODE_IDS = {"__start__", "__end__"}

_NODE_LABELS = {
    "__start__": "시작",
    "__end__": "종료",
    "turn_prepare": "질문 준비",
    "query_rewrite": "질문 재작성",
    "search_scope_prepare": "검색 범위 준비",
    "industry_lookup": "산업·기업 조회",
    "search_scope_merge": "검색 범위 병합",
    "scope_selection": "검색 범위 선택",
    "router": "검색 경로 결정",
    "rdb_scope_preflight": "RDB 범위 확인",
    "vectordb_scope_preflight": "Vector DB 범위 확인",
    "rdb_sql_gen_node": "SQL 생성",
    "rdb_execute_node": "RDB 실행",
    "vector_dispatcher": "Vector 검색 방식 결정",
    "vectordb_node": "Vector DB 검색",
    "too_many_targets": "검색 대상 초과 처리",
    "clear_short_term_memory_retry": "검색 범위 재시도 준비",
    "stock_price_tools": "주가 도구 실행",
    "final_response_node": "최종 답변 생성",
    "comparison_prepare": "비교 검색 준비",
    "retrieve_company": "기업별 검색",
    "comparison_fan_in": "비교 검색 병합",
}


def _bounded_text(value: object, *, fallback: str = "") -> str:
    text = str(value or fallback).strip()
    return text[:_MAX_TEXT_LENGTH]


def _node_label(node_id: str, raw_name: object) -> str:
    leaf_id = node_id.rsplit(":", 1)[-1]
    if leaf_id in _NODE_LABELS:
        return _NODE_LABELS[leaf_id]
    name = _bounded_text(raw_name, fallback=leaf_id)
    return name.replace("_", " ") or leaf_id


def _graph_manifest_error(
    graph_app: object,
    *,
    code: str,
    error_type: str,
) -> dict[str, Any]:
    """Return safe failure evidence without persisting exception text."""
    return {
        "graph_id": _bounded_text(
            getattr(graph_app, "name", None),
            fallback="finance_chat",
        ),
        "revision": None,
        "nodes": [],
        "edges": [],
        "capture_error": {
            "code": _bounded_text(code),
            "error_type": _bounded_text(error_type),
        },
    }


def _manifest_from_drawable(
    graph_app: object,
    drawable: object,
) -> dict[str, Any]:
    raw_nodes = getattr(drawable, "nodes", None)
    raw_edges = getattr(drawable, "edges", ())
    if not isinstance(raw_nodes, Mapping):
        raise TypeError("Graph topology nodes must be a mapping.")
    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for order, (raw_id, raw_node) in enumerate(raw_nodes.items()):
        if len(nodes) >= _MAX_MANIFEST_NODES:
            break
        node_id = _bounded_text(raw_id)
        if not node_id or node_id in node_ids:
            continue
        node_ids.add(node_id)
        raw_name = getattr(raw_node, "name", None)
        group = node_id.rsplit(":", 1)[0] if ":" in node_id else None
        node = {
            "id": node_id,
            "label": _node_label(node_id, raw_name),
            "kind": "boundary" if node_id in _BOUNDARY_NODE_IDS else "task",
            "order": order,
        }
        if group:
            node["group"] = group
        nodes.append(node)

    edges: list[dict[str, Any]] = []
    for raw_edge in raw_edges:
        if len(edges) >= _MAX_MANIFEST_EDGES:
            break
        source = _bounded_text(getattr(raw_edge, "source", None))
        target = _bounded_text(getattr(raw_edge, "target", None))
        if source not in node_ids or target not in node_ids:
            continue
        edge = {
            "source": source,
            "target": target,
            "conditional": bool(getattr(raw_edge, "conditional", False)),
        }
        if edge not in edges:
            edges.append(edge)

    if not nodes:
        raise ValueError("Graph topology does not contain nodes.")
    revision_payload = {"nodes": nodes, "edges": edges}
    revision = hashlib.sha256(
        json.dumps(
            revision_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    graph_name = _bounded_text(getattr(graph_app, "name", None), fallback="finance_chat")
    return {
        "graph_id": graph_name,
        "revision": revision,
        "nodes": nodes,
        "edges": edges,
    }


def build_graph_manifest(graph_app: object) -> dict[str, Any]:
    """Read topology or return explicit, value-free capture failure evidence."""

    get_graph = getattr(graph_app, "get_graph", None)
    if not callable(get_graph):
        return {}
    try:
        return _manifest_from_drawable(graph_app, get_graph(xray=True))
    except Exception as exc:
        return _graph_manifest_error(
            graph_app,
            code="topology_capture_failed",
            error_type=type(exc).__name__,
        )


def _stream_event_parts(event: object) -> tuple[tuple[object, ...], str, object] | None:
    if isinstance(event, Mapping):
        mode = event.get("type")
        if isinstance(mode, str) and "data" in event:
            namespace = event.get("ns")
            return (
                tuple(namespace) if isinstance(namespace, Sequence) else (),
                mode,
                event.get("data"),
            )
        return None
    if not isinstance(event, tuple):
        return None
    if len(event) == 3 and isinstance(event[1], str):
        namespace = event[0]
        return (
            tuple(namespace) if isinstance(namespace, Sequence) else (),
            event[1],
            event[2],
        )
    if len(event) == 2 and isinstance(event[0], str):
        return (), event[0], event[1]
    return None


def _namespace_node_id(
    namespace: tuple[object, ...],
    task_name: str,
    manifest_node_ids: set[str],
) -> str | None:
    parent_ids = []
    for raw_part in namespace:
        part = _bounded_text(raw_part)
        if not part:
            continue
        parent_ids.append(part.rsplit(":", 1)[0] if ":" in part else part)
    candidate = ":".join([*parent_ids, task_name]) if parent_ids else task_name
    if candidate in manifest_node_ids:
        return candidate
    if task_name in manifest_node_ids:
        return task_name
    return None


class NodeRunCollector:
    """Collect bounded, value-free task telemetry from LangGraph stream events."""
    def __init__(
        self,
        manifest: Mapping[str, Any],
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.manifest = dict(manifest)
        self.clock = clock
        self.node_ids = {
            str(node.get("id"))
            for node in self.manifest.get("nodes", ())
            if isinstance(node, Mapping) and node.get("id")
        }
        self.trace_started_at = self.clock()
        self.started: dict[str, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.invocations: defaultdict[str, int] = defaultdict(int)
        self.sequence = 0
        self.final_state: dict[str, Any] | None = None

    def observe(self, event: object) -> None:
        parts = _stream_event_parts(event)
        if parts is None:
            return
        namespace, mode, payload = parts
        if mode == "values" and not namespace and isinstance(payload, Mapping):
            self.final_state = dict(payload)
            return
        if mode != "tasks" or not isinstance(payload, Mapping):
            return

        task_id = _bounded_text(payload.get("id"))
        task_name = _bounded_text(payload.get("name"))
        if not task_id or not task_name:
            return
        node_id = _namespace_node_id(namespace, task_name, self.node_ids)
        if node_id is None:
            return
        observed_at = self.clock()
        if "input" in payload and "result" not in payload and "error" not in payload:
            if len(self.runs) + len(self.started) >= _MAX_NODE_RUNS:
                return
            self.sequence += 1
            self.invocations[node_id] += 1
            self.started[task_id] = {
                "run_id": task_id,
                "node_id": node_id,
                "sequence": self.sequence,
                "invocation_index": self.invocations[node_id],
                "started_offset_seconds": round(
                    max(0.0, observed_at - self.trace_started_at),
                    6,
                ),
                "started_at": observed_at,
            }
            return

        started = self.started.pop(task_id, None)
        if started is None or len(self.runs) >= _MAX_NODE_RUNS:
            return
        error = payload.get("error")
        interrupts = payload.get("interrupts")
        status = "failed" if error else "interrupted" if interrupts else "completed"
        result = payload.get("result")
        result_keys = (
            [_bounded_text(key) for key in list(result)[:_MAX_RESULT_KEYS]]
            if isinstance(result, Mapping)
            else []
        )
        run = {
            key: value for key, value in started.items() if key != "started_at"
        }
        run.update(
            {
                "status": status,
                "duration_seconds": round(
                    max(0.0, observed_at - float(started["started_at"])),
                    6,
                ),
                "ended_offset_seconds": round(
                    max(0.0, observed_at - self.trace_started_at),
                    6,
                ),
                "result_keys": result_keys,
            }
        )
        if error:
            run["error_type"] = type(error).__name__
        self.runs.append(run)

    def snapshot(self, *, overall_status: str) -> dict[str, Any]:
        observed_at = self.clock()
        pending_status = {
            "failed": "failed",
            "running": "running",
        }.get(overall_status, "interrupted")
        pending_runs = []
        for started in self.started.values():
            run = {
                key: value for key, value in started.items() if key != "started_at"
            }
            run.update(
                {
                    "status": pending_status,
                    "duration_seconds": round(
                        max(0.0, observed_at - float(started["started_at"])),
                        6,
                    ),
                    "ended_offset_seconds": round(
                        max(0.0, observed_at - self.trace_started_at),
                        6,
                    ),
                    "result_keys": [],
                }
            )
            pending_runs.append(run)
        node_runs = sorted(
            [*self.runs, *pending_runs],
            key=lambda run: (
                int(run.get("sequence") or 0),
                int(run.get("invocation_index") or 0),
            ),
        )[:_MAX_NODE_RUNS]
        return {
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "graph_manifest": self.manifest,
            "node_runs": node_runs,
        }


def invoke_graph_with_observability(
    graph_app: object,
    graph_input: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Invoke a graph and attach bounded topology/task telemetry to its result."""

    manifest = build_graph_manifest(graph_app)
    stream = getattr(graph_app, "stream", None)
    if not manifest and callable(stream):
        manifest = _graph_manifest_error(
            graph_app,
            code="topology_interface_unavailable",
            error_type="MissingGetGraph",
        )
    if not callable(stream):
        invoke = getattr(graph_app, "invoke", None)
        if not callable(invoke):
            raise TypeError("Chat graph must expose invoke() or stream().")
        result = invoke(dict(graph_input), config=dict(config))
        if not isinstance(result, Mapping):
            raise TypeError("Chat graph returned a non-dictionary result.")
        final_state = dict(result)
        if manifest:
            fallback_manifest = dict(manifest)
            fallback_manifest.setdefault(
                "capture_error",
                {
                    "code": "task_stream_unavailable",
                    "error_type": "MissingStream",
                },
            )
            final_state[GRAPH_TRACE_STATE_KEY] = {
                "graph_schema_version": GRAPH_SCHEMA_VERSION,
                "graph_manifest": fallback_manifest,
                "node_runs": [],
            }
        return final_state

    collector = NodeRunCollector(manifest)
    try:
        for event in stream(
            dict(graph_input),
            config=dict(config),
            stream_mode=["values", "tasks"],
            subgraphs=True,
        ):
            collector.observe(event)
    except Exception as exc:
        try:
            exc.graph_trace = collector.snapshot(overall_status="failed")
        except Exception:
            pass
        raise

    if collector.final_state is None:
        error = RuntimeError("Chat graph stream completed without a final state.")
        error.graph_trace = collector.snapshot(overall_status="failed")
        raise error
    final_state = dict(collector.final_state)
    final_state[GRAPH_TRACE_STATE_KEY] = collector.snapshot(
        overall_status="succeeded"
    )
    return final_state
