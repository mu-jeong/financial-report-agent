"""Read-only helpers for Monitoring Mode metrics and fixture summaries."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.configs.settings import BASE_DIR

EVALUATION_DATASET_PATH = BASE_DIR / "tests" / "fixtures" / "evaluation_dataset.json"


def load_evaluation_dataset(path: str | Path = EVALUATION_DATASET_PATH) -> dict[str, Any]:
    """Load the fixed local evaluation dataset."""
    dataset_path = Path(path)
    return json.loads(dataset_path.read_text(encoding="utf-8-sig"))


def summarize_evaluation_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    """Return compact Monitoring Mode coverage metrics for the fixed dataset."""
    cases = dataset.get("cases") or []
    case_types = Counter(case.get("type", "unknown") for case in cases)
    dimensions = Counter(
        dimension
        for case in cases
        for dimension in case.get("monitoring_dimensions", [])
    )
    criteria_tags = Counter(
        tag
        for case in cases
        for tag in case.get("criteria_tags", [])
    )
    source_count = sum(len(case.get("expected_sources", [])) for case in cases)
    rdb_case_count = sum(1 for case in cases if case.get("type") == "rdb_aggregate")

    return {
        "name": dataset.get("name"),
        "version": dataset.get("version"),
        "snapshot_date": (dataset.get("generated_from") or {}).get("snapshot_date"),
        "case_count": len(cases),
        "case_types": dict(case_types),
        "monitoring_dimensions": dict(dimensions),
        "criteria_tags": dict(criteria_tags),
        "expected_source_count": source_count,
        "rdb_case_count": rdb_case_count,
        "stability_policy": dataset.get("stability_policy") or {},
    }


def summarize_chat_messages(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize recent chat metadata for Monitoring Mode without exposing full text."""
    assistant_messages = [
        message
        for message in messages
        if message.get("role") == "assistant"
    ]
    statuses = Counter(
        (message.get("metadata") or {}).get("status", "unknown")
        for message in assistant_messages
    )
    routes = Counter(
        (message.get("metadata") or {}).get("route", "unknown")
        for message in assistant_messages
        if (message.get("metadata") or {}).get("status") == "succeeded"
    )
    rerank_counts = [
        len((message.get("metadata") or {}).get("rerank_info") or [])
        for message in assistant_messages
        if (message.get("metadata") or {}).get("status") == "succeeded"
    ]
    latencies = [
        float((message.get("metadata") or {}).get("latency_seconds"))
        for message in assistant_messages
        if isinstance((message.get("metadata") or {}).get("latency_seconds"), (int, float))
    ]

    return {
        "message_count": len(messages),
        "assistant_message_count": len(assistant_messages),
        "statuses": dict(statuses),
        "routes": dict(routes),
        "avg_rerank_source_count": (
            sum(rerank_counts) / len(rerank_counts)
            if rerank_counts
            else 0.0
        ),
        "avg_latency_seconds": (
            sum(latencies) / len(latencies)
            if latencies
            else None
        ),
    }


def build_message_monitoring_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one safe monitoring row per assistant response."""
    rows: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        metadata = message.get("metadata") or {}
        monitoring = metadata.get("monitoring") or {}
        search_scope = metadata.get("search_scope") or {}
        rows.append(
            {
                "created_at": message.get("created_at"),
                "status": metadata.get("status", "unknown"),
                "route": metadata.get("route", "-"),
                "latency_seconds": metadata.get("latency_seconds"),
                "source_count": len(metadata.get("rerank_info") or []),
                "search_filters": search_scope.get("search_filters") or metadata.get("search_filters") or {},
                "rdb_row_count": (monitoring.get("rdb") or {}).get("row_count"),
                "error": metadata.get("error"),
            }
        )
    return rows


def compact_graph_monitoring_metadata(
    *,
    final_state: dict[str, Any],
    latency_seconds: float,
    rerank_info: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build safe per-response monitoring metadata from a completed graph state."""
    route = final_state.get("route")
    metadata: dict[str, Any] = {
        "route": route,
        "latency_seconds": round(latency_seconds, 3),
        "search_filters": final_state.get("search_filters") or {},
        "temporal_context": final_state.get("temporal_context"),
        "selection_context": final_state.get("selection_context"),
        "monitoring": {
            "query_rewrite": {
                "rewritten_query": final_state.get("rewritten_query"),
                "uses_chat_history": final_state.get("uses_chat_history"),
                "followup_scope_intent": final_state.get("followup_scope_intent"),
            },
            "retrieval": {
                "source_count": len(rerank_info),
                "score_summary": _score_summary(rerank_info),
            },
        },
    }
    for section, values in (final_state.get("monitoring_metrics") or {}).items():
        if isinstance(values, dict):
            metadata["monitoring"].setdefault(section, {}).update(values)
    if route == "rdb":
        metadata["monitoring"]["rdb"] = _compact_rdb_metrics(final_state)
    return metadata


def _score_summary(rerank_info: list[dict[str, Any]]) -> dict[str, Any]:
    if not rerank_info:
        return {}
    summary: dict[str, Any] = {"count": len(rerank_info)}
    for key in ["score", "rerank_score", "recency_score", "final_score"]:
        values = [
            float(item[key])
            for item in rerank_info
            if isinstance(item.get(key), (int, float))
        ]
        if values:
            summary[key] = {
                "min": min(values),
                "max": max(values),
                "avg": sum(values) / len(values),
            }
    return summary


def _compact_rdb_metrics(final_state: dict[str, Any]) -> dict[str, Any]:
    existing_metrics = (
        (final_state.get("monitoring_metrics") or {}).get("rdb")
        if isinstance(final_state.get("monitoring_metrics"), dict)
        else {}
    ) or {}
    raw_result = final_state.get("rdb_result")
    row_count = existing_metrics.get("row_count")
    column_count = existing_metrics.get("column_count")
    if isinstance(raw_result, dict):
        rows = raw_result.get("rows") or []
        columns = raw_result.get("columns") or []
        row_count = len(rows)
        column_count = len(columns)
    return {
        "sql_query": existing_metrics.get("sql_query") or final_state.get("sql_query"),
        "row_count": row_count,
        "column_count": column_count,
        "guardrail_blocked": existing_metrics.get("guardrail_blocked"),
        "result_preview": str(raw_result)[:500] if raw_result is not None else None,
    }
