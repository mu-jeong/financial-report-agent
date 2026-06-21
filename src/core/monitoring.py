"""Read-only helpers for Monitoring Mode metrics and fixture summaries."""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.utils.citations import extract_citation_ranks, group_sources_by_document

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


def _source_names(items: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("file_name") or "").strip() for item in items if item.get("file_name")}


def _expected_source_hit(expected_sources: list[dict[str, Any]], actual_sources: list[dict[str, Any]]) -> tuple[bool, int | None]:
    if not expected_sources:
        return True, None
    expected_names = _source_names(expected_sources)
    if not expected_names:
        return True, None
    for index, source in enumerate(actual_sources, 1):
        if str(source.get("file_name") or "").strip() in expected_names:
            return True, index
    return False, None


def _filters_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in (expected or {}).items())


def _citation_valid(answer: str, sources: list[dict[str, Any]]) -> bool:
    source_count = len(group_sources_by_document(sources))
    cited = extract_citation_ranks(answer or "", source_count=None)
    if not cited:
        return source_count == 0
    return all(1 <= rank <= source_count for rank in cited)


def evaluate_dataset_case_result(
    case: dict[str, Any],
    final_state: dict[str, Any],
    *,
    latency_seconds: float,
    latency_threshold_seconds: float = 30.0,
) -> dict[str, Any]:
    """Score one fixed evaluation case against graph output."""
    sources = final_state.get("rerank_info") or final_state.get("rdb_sources") or []
    route_pass = final_state.get("route") == case.get("expected_route")
    filter_pass = _filters_match(case.get("expected_filters") or {}, final_state.get("search_filters") or {})
    source_hit, hit_at_k = _expected_source_hit(case.get("expected_sources") or [], sources)
    citation_valid = _citation_valid(str(final_state.get("generation") or ""), sources)
    latency_pass = latency_seconds <= latency_threshold_seconds
    no_result = bool(final_state.get("no_vector_results"))
    passed = route_pass and filter_pass and source_hit and citation_valid and latency_pass and not no_result
    return {
        "case_id": case.get("id"),
        "question": case.get("question"),
        "status": "pass" if passed else "fail",
        "actual_route": final_state.get("route"),
        "expected_route": case.get("expected_route"),
        "route_pass": route_pass,
        "filter_pass": filter_pass,
        "source_hit": source_hit,
        "hit_at_k": hit_at_k,
        "citation_valid": citation_valid,
        "latency_seconds": round(latency_seconds, 3),
        "latency_pass": latency_pass,
        "no_result": no_result,
        "actual_filters": final_state.get("search_filters") or {},
        "source_count": len(sources),
    }


def _summarize_eval_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    case_count = len(results)
    passed = sum(1 for result in results if result.get("status") == "pass")
    latencies = [float(result["latency_seconds"]) for result in results if isinstance(result.get("latency_seconds"), (int, float))]
    return {
        "case_count": case_count,
        "passed": passed,
        "failed": case_count - passed,
        "pass_rate": passed / case_count if case_count else 0.0,
        "route_pass_rate": sum(1 for result in results if result.get("route_pass")) / case_count if case_count else 0.0,
        "filter_pass_rate": sum(1 for result in results if result.get("filter_pass")) / case_count if case_count else 0.0,
        "source_hit_rate": sum(1 for result in results if result.get("source_hit")) / case_count if case_count else 0.0,
        "citation_valid_rate": sum(1 for result in results if result.get("citation_valid")) / case_count if case_count else 0.0,
        "no_result_rate": sum(1 for result in results if result.get("no_result")) / case_count if case_count else 0.0,
        "avg_latency_seconds": sum(latencies) / len(latencies) if latencies else None,
    }



def select_evaluation_cases(
    dataset: dict[str, Any],
    selected_case_ids: list[str] | tuple[str, ...] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return evaluation cases selected by explicit ids, preserving selection order."""
    cases = list(dataset.get("cases") or [])
    if not selected_case_ids:
        return cases
    case_by_id = {str(case.get("id")): case for case in cases}
    return [case_by_id[case_id] for case_id in selected_case_ids if case_id in case_by_id]

def run_evaluation_dataset(
    dataset: dict[str, Any],
    invoke_fn: Callable[..., dict[str, Any]],
    *,
    output_dir: str | Path,
    limit: int | None = None,
    selected_case_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    latency_threshold_seconds: float = 30.0,
) -> dict[str, Any]:
    """Run the fixed dataset through the graph and persist a JSON experiment run."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    cases = select_evaluation_cases(dataset, selected_case_ids)
    if limit:
        cases = cases[:limit]
    results: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        final_state = invoke_fn(
            {"question": case.get("question", ""), "chat_history": []},
            config={"configurable": {"thread_id": f"monitoring_eval_{run_id}_{case.get('id', 'case')}"}},
        )
        latency = time.perf_counter() - started
        results.append(
            evaluate_dataset_case_result(
                case,
                final_state,
                latency_seconds=latency,
                latency_threshold_seconds=latency_threshold_seconds,
            )
        )
    run = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_name": dataset.get("name"),
        "dataset_version": dataset.get("version"),
        "selected_case_ids": [case.get("id") for case in cases],
        "summary": _summarize_eval_results(results),
        "results": results,
    }
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    json_path = output_path / f"evaluation_run_{run_id}.json"
    json_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    run["json_path"] = str(json_path)
    return run


def build_evaluation_failure_actions(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build actionable next-step guidance for failed evaluation cases."""
    rows: list[dict[str, Any]] = []
    for result in results:
        if result.get("status") != "fail":
            continue
        actions: list[str] = []
        if result.get("route_pass") is False:
            actions.append("router/query classification")
        if result.get("filter_pass") is False:
            actions.append("metadata filter extraction")
        if result.get("source_hit") is False:
            actions.append("retrieval index, chunking, or rerank")
        if result.get("citation_valid") is False:
            actions.append("citation generation/removal")
        if result.get("latency_pass") is False:
            actions.append("latency budget")
        if result.get("no_result"):
            actions.append("retry with broader filters or update data")
        rows.append(
            {
                "case_id": result.get("case_id"),
                "question": result.get("question"),
                "failed_checks": ", ".join(
                    key
                    for key in [
                        "route_pass",
                        "filter_pass",
                        "source_hit",
                        "citation_valid",
                        "latency_pass",
                    ]
                    if result.get(key) is False
                ),
                "recommended_actions": "; ".join(actions),
            }
        )
    return rows


def compare_evaluation_runs(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Return summary deltas between two saved experiment runs."""
    if not previous:
        return {}
    current_summary = current.get("summary") or {}
    previous_summary = previous.get("summary") or {}
    return {
        "passed_delta": current_summary.get("passed", 0) - previous_summary.get("passed", 0),
        "failed_delta": current_summary.get("failed", 0) - previous_summary.get("failed", 0),
        "avg_latency_delta": _delta(current_summary.get("avg_latency_seconds"), previous_summary.get("avg_latency_seconds")),
        "source_hit_rate_delta": _delta(current_summary.get("source_hit_rate"), previous_summary.get("source_hit_rate")),
        "citation_valid_rate_delta": _delta(current_summary.get("citation_valid_rate"), previous_summary.get("citation_valid_rate")),
        "no_result_rate_delta": _delta(current_summary.get("no_result_rate"), previous_summary.get("no_result_rate")),
    }


def summarize_all_chat_threads(thread_messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize persisted chat quality signals across all threads."""
    assistant_rows: list[dict[str, Any]] = []
    recent_failures: list[dict[str, Any]] = []
    for entry in thread_messages:
        thread = entry.get("thread") or {}
        for message in entry.get("messages") or []:
            if message.get("role") != "assistant":
                continue
            metadata = message.get("metadata") or {}
            row = {
                "thread_id": thread.get("id"),
                "thread_name": thread.get("name"),
                "created_at": message.get("created_at"),
                "status": metadata.get("status", "unknown"),
                "route": metadata.get("route"),
                "latency_seconds": metadata.get("latency_seconds"),
                "no_vector_results": bool(metadata.get("no_vector_results")),
                "error": metadata.get("error"),
            }
            assistant_rows.append(row)
            if row["status"] == "failed":
                recent_failures.append(row)
    statuses = Counter(row["status"] for row in assistant_rows)
    routes = Counter(row["route"] for row in assistant_rows if row["status"] == "succeeded" and row.get("route"))
    latencies = sorted(float(row["latency_seconds"]) for row in assistant_rows if isinstance(row.get("latency_seconds"), (int, float)))
    succeeded_count = statuses.get("succeeded", 0)
    no_result_count = sum(1 for row in assistant_rows if row["status"] == "succeeded" and row.get("no_vector_results"))
    total = len(assistant_rows)
    return {
        "thread_count": len(thread_messages),
        "assistant_message_count": total,
        "statuses": dict(statuses),
        "routes": dict(routes),
        "failure_rate": statuses.get("failed", 0) / total if total else 0.0,
        "no_result_rate": no_result_count / succeeded_count if succeeded_count else 0.0,
        "avg_latency_seconds": sum(latencies) / len(latencies) if latencies else None,
        "p95_latency_seconds": _percentile(latencies, 0.95) if latencies else None,
        "recent_failures": sorted(recent_failures, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:10],
    }


def summarize_issue_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "report_count": len(reports),
        "categories": dict(Counter(report.get("category") or "unknown" for report in reports)),
        "thread_count": len({report.get("thread_id") for report in reports if report.get("thread_id")}),
    }


def build_issue_report_rows(reports: list[dict[str, Any]], *, thread_names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    thread_names = thread_names or {}
    return [
        {
            "created_at": report.get("created_at"),
            "id": report.get("id"),
            "category": report.get("category"),
            "thread_id": str(report.get("thread_id") or ""),
            "thread_name": thread_names.get(str(report.get("thread_id") or ""), "-"),
            "file_path": report.get("file_path"),
            "preview": _issue_report_preview(report.get("content") or ""),
        }
        for report in reports
    ]


def promote_issue_report_to_eval_candidate(
    report: dict[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Persist an issue report as a regression/evaluation candidate artifact."""
    candidate_id = f"candidate_{report.get('id') or uuid.uuid4().hex[:8]}"
    candidate = {
        "id": candidate_id,
        "status": "candidate",
        "source": "issue_report",
        "source_report_id": report.get("id"),
        "thread_id": report.get("thread_id"),
        "category": report.get("category"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file_path": report.get("file_path"),
        "preview": _issue_report_preview(report.get("content") or "", max_chars=500),
        "recommended_next_step": "convert_to_evaluation_dataset_case",
    }
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    json_path = output_path / f"{candidate_id}.json"
    json_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    candidate["json_path"] = str(json_path)
    return candidate


def summarize_data_integrity(status: dict[str, Any]) -> dict[str, Any]:
    db = status.get("db") or {}
    vector = status.get("vector_db") or {}
    total = int(db.get("total_reports") or 0)
    embedded = int(db.get("embedded_reports") or 0)
    pending = int(db.get("pending_reports") or 0)
    downloaded = int(status.get("downloaded_pdfs") or 0)
    checks = {
        "faiss_index": {"status": "pass" if vector.get("has_faiss_index") else "fail", "detail": "FAISS index present" if vector.get("has_faiss_index") else "FAISS index missing"},
        "embedding_backlog": {"status": "pass" if pending == 0 else "warning", "detail": f"{pending} pending reports"},
        "pdf_vs_db": {"status": "pass" if downloaded >= embedded else "warning", "detail": f"{downloaded} PDFs for {embedded} embedded reports"},
        "search_coverage": {"status": "pass" if total == 0 or embedded / total >= 0.95 else "warning", "detail": f"{embedded}/{total} reports embedded"},
    }
    return {"checks": checks, "pass_count": sum(1 for check in checks.values() if check["status"] == "pass"), "warning_count": sum(1 for check in checks.values() if check["status"] == "warning"), "fail_count": sum(1 for check in checks.values() if check["status"] == "fail")}


def build_monitoring_tab_labels() -> list[str]:
    return ["데이터/설정", "실험 실행", "고정 테스트셋", "Parsing engines", "전체 Monitoring", "Chat Monitoring", "Issue reports"]


def build_monitoring_page_labels() -> list[str]:
    """Return top-level pages shown when Monitoring Mode is enabled."""
    return ["Chat", "전체 Monitoring"]


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


def _delta(current: Any, previous: Any) -> float | None:
    if current is None or previous is None:
        return None
    return float(current) - float(previous)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
    index = max(0, min(len(values) - 1, round((len(values) - 1) * percentile)))
    return values[index]


def _issue_report_preview(content: str, max_chars: int = 120) -> str:
    if "Finance LLM 문제 신고" not in content:
        return ""
    compact = " ".join(line.strip() for line in content.splitlines() if line.strip())
    return compact[:max_chars]


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
