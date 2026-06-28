"""Monitoring Mode helper입니다."""

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
from src.core.followup_scope import build_answer_scope_index

EVALUATION_DATASET_PATH = BASE_DIR / "tests" / "fixtures" / "evaluation_dataset.json"
MULTITURN_EVALUATION_DATASET_PATH = BASE_DIR / "tests" / "fixtures" / "multiturn_evaluation_dataset.json"
EVALUATION_SNAPSHOT_ROOT = BASE_DIR / "tests" / "fixtures" / "eval_snapshot"
EVALUATION_SNAPSHOT_MANIFEST_PATH = EVALUATION_SNAPSHOT_ROOT / "manifest.json"


def load_evaluation_dataset(path: str | Path = EVALUATION_DATASET_PATH) -> dict[str, Any]:
    """고정 local evaluation dataset을 로드합니다."""
    dataset_path = Path(path)
    return json.loads(dataset_path.read_text(encoding="utf-8-sig"))


def load_multiturn_evaluation_dataset(
    path: str | Path = MULTITURN_EVALUATION_DATASET_PATH,
) -> dict[str, Any]:
    """고정 local multi-turn evaluation dataset을 로드합니다."""
    dataset_path = Path(path)
    return json.loads(dataset_path.read_text(encoding="utf-8-sig"))


def load_evaluation_snapshot_manifest(
    path: str | Path = EVALUATION_SNAPSHOT_MANIFEST_PATH,
) -> dict[str, Any]:
    """고정 evaluation snapshot manifest를 로드합니다."""
    manifest_path = Path(path)
    return json.loads(manifest_path.read_text(encoding="utf-8-sig"))


def _snapshot_check(name: str, passed: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "pass" if passed else "fail", "detail": detail}


def _compare_snapshot_value(
    checks: list[dict[str, str]],
    *,
    name: str,
    dataset_value: Any,
    manifest_value: Any,
) -> None:
    if manifest_value is None:
        checks.append(_snapshot_check(name, True, "manifest value not specified"))
        return
    checks.append(
        _snapshot_check(
            name,
            dataset_value == manifest_value,
            f"dataset={dataset_value!r}, manifest={manifest_value!r}",
        )
    )


def validate_evaluation_snapshot(
    dataset: dict[str, Any],
    manifest: dict[str, Any],
    snapshot_root: str | Path = EVALUATION_SNAPSHOT_ROOT,
) -> dict[str, Any]:
    """dataset metadata와 snapshot 파일이 같은 baseline을 가리키는지 검증합니다."""
    root = Path(snapshot_root)
    database = manifest.get("database") or {}
    vector_db = manifest.get("vector_db") or {}
    db_path = root / str(database.get("path") or "reports.db")
    faiss_dir = root / str(vector_db.get("path") or "vector_db")
    checks: list[dict[str, str]] = []

    _compare_snapshot_value(
        checks,
        name="dataset_name",
        dataset_value=dataset.get("name"),
        manifest_value=manifest.get("dataset_name"),
    )
    _compare_snapshot_value(
        checks,
        name="dataset_version",
        dataset_value=dataset.get("version"),
        manifest_value=manifest.get("dataset_version"),
    )
    generated_from = dataset.get("generated_from") or {}
    _compare_snapshot_value(
        checks,
        name="snapshot_date",
        dataset_value=generated_from.get("snapshot_date"),
        manifest_value=manifest.get("snapshot_date"),
    )
    for field in (
        "source_row_count",
        "embedded_row_count",
        "min_report_date",
        "max_report_date",
    ):
        _compare_snapshot_value(
            checks,
            name=field,
            dataset_value=generated_from.get(field),
            manifest_value=database.get(field),
        )

    checks.append(
        _snapshot_check(
            "snapshot_db_exists",
            db_path.exists(),
            str(db_path),
        )
    )
    checks.append(
        _snapshot_check(
            "vector_dir_exists",
            faiss_dir.exists(),
            str(faiss_dir),
        )
    )
    for file_name in vector_db.get("required_files") or ["index.faiss", "index.pkl"]:
        file_path = faiss_dir / str(file_name)
        checks.append(
            _snapshot_check(
                f"vector_file:{file_name}",
                file_path.exists(),
                str(file_path),
            )
        )

    status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
    return {
        "status": status,
        "checks": checks,
        "snapshot_root": str(root),
        "db_path": str(db_path),
        "faiss_dir": str(faiss_dir),
        "manifest": manifest,
    }


def summarize_evaluation_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    """고정 dataset의 Monitoring Mode coverage metric을 반환합니다."""
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
    """본문 전체를 노출하지 않고 chat metadata를 요약합니다."""
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
    """assistant 응답마다 monitoring row를 만듭니다."""
    rows: list[dict[str, Any]] = []
    latest_user_question = ""
    for message in messages:
        if message.get("role") == "user":
            latest_user_question = str(message.get("content") or "")
            continue
        if message.get("role") != "assistant":
            continue
        metadata = message.get("metadata") or {}
        monitoring = metadata.get("monitoring") or {}
        search_scope = metadata.get("search_scope") or {}
        rerank_info = metadata.get("rerank_info") or []
        search_filters = search_scope.get("search_filters") or metadata.get("search_filters") or {}
        scope_decision = metadata.get("scope_decision") or {}
        route = metadata.get("route", "-")
        created_at = message.get("created_at")
        source_names = _ordered_file_names(rerank_info)
        rows.append(
            {
                "message_id": message.get("id"),
                "created_at": created_at,
                "user_question_preview": _safe_preview(metadata.get("question") or latest_user_question),
                "assistant_preview": _safe_preview(message.get("content")),
                "status": metadata.get("status", "unknown"),
                "route": route,
                "latency_seconds": metadata.get("latency_seconds"),
                "source_count": len(group_sources_by_document(rerank_info)),
                "search_filters": search_filters,
                "scope_source": metadata.get("scope_source") or search_scope.get("scope_source"),
                "scope_decision_reason": scope_decision.get("reason"),
                "no_vector_results": bool(metadata.get("no_vector_results")),
                "selected_file_names": source_names,
                "rdb_row_count": (monitoring.get("rdb") or {}).get("row_count"),
                "error": metadata.get("error"),
                "label": _response_label(created_at, route, metadata.get("question") or latest_user_question, len(group_sources_by_document(rerank_info)), metadata.get("latency_seconds")),
            }
        )
    return rows


def _safe_preview(value: Any, max_chars: int = 120) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1] + "…"


def _response_label(created_at: Any, route: Any, question: Any, source_count: int, latency_seconds: Any) -> str:
    latency = f" · {float(latency_seconds):.1f}s" if isinstance(latency_seconds, (int, float)) else ""
    return f"{created_at or '-'} · {route or '-'} · {_safe_preview(question, 48)} · {source_count} sources{latency}"


def _ordered_file_names(items: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        file_name = str((item or {}).get("file_name") or "").strip()
        if file_name and file_name != "-" and file_name not in seen:
            seen.add(file_name)
            names.append(file_name)
    return names


def _message_metadata(message: dict[str, Any] | None) -> dict[str, Any]:
    return (message or {}).get("metadata") or {}


def _metadata_search_filters(metadata: dict[str, Any]) -> dict[str, Any]:
    search_scope = metadata.get("search_scope") or {}
    return search_scope.get("search_filters") or metadata.get("search_filters") or {}


def _metadata_retrieval(metadata: dict[str, Any]) -> dict[str, Any]:
    return (metadata.get("monitoring") or {}).get("retrieval") or {}


def _metadata_query_rewrite(metadata: dict[str, Any]) -> dict[str, Any]:
    return (metadata.get("monitoring") or {}).get("query_rewrite") or {}


def _scope_file_count(scope: dict[str, Any] | None) -> int:
    if not isinstance(scope, dict):
        return 0
    if isinstance(scope.get("file_count"), int):
        return int(scope["file_count"])
    file_names = scope.get("file_names") or []
    return len(file_names) if isinstance(file_names, list) else 0


def _compact_scope_for_state_trace(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    """State trace에서 볼 핵심 scope만 작게 보존합니다."""
    if not isinstance(scope, dict):
        return None
    compact: dict[str, Any] = {
        "route": scope.get("route"),
        "search_filters": scope.get("search_filters") or {},
        "temporal_context": scope.get("temporal_context"),
        "scope_source": scope.get("scope_source"),
        "file_count": _scope_file_count(scope),
    }
    file_names = scope.get("file_names") or []
    if isinstance(file_names, list):
        compact["file_names"] = file_names[:10]
        if len(file_names) > 10:
            compact["file_names_truncated"] = len(file_names) - 10
    answer_scope_index = scope.get("answer_scope_index") or {}
    sections = answer_scope_index.get("sections") if isinstance(answer_scope_index, dict) else None
    if isinstance(sections, list):
        compact["answer_scope_sections"] = [
            {
                "id": section.get("id"),
                "label": section.get("label"),
                "filters": section.get("filters") or {},
                "file_count": _scope_file_count(section),
                "file_names": (section.get("file_names") or [])[:10],
            }
            for section in sections
            if isinstance(section, dict)
        ]
    return compact


def _build_state_transition_trace(metadata: dict[str, Any]) -> dict[str, Any]:
    monitoring = metadata.get("monitoring") or {}
    state_trace = monitoring.get("state_trace") or {}
    if not isinstance(state_trace, dict):
        state_trace = {}
    state_trace_input = state_trace.get("input", {})
    if not isinstance(state_trace_input, dict):
        state_trace_input = {}
    prior_scope = state_trace.get("input", {}).get("prior_search_scope") if isinstance(state_trace, dict) else None
    current_scope = metadata.get("search_scope")
    query_rewrite = _metadata_query_rewrite(metadata)
    retrieval = _metadata_retrieval(metadata)
    followup_scope_intent = query_rewrite.get("followup_scope_intent")
    if followup_scope_intent is None:
        followup_scope_intent = metadata.get("followup_scope_intent")
    return {
        "input": {
            "question": metadata.get("question") or state_trace_input.get("question"),
            "prior_search_scope": prior_scope,
            "prior_search_scope_file_count": _scope_file_count(prior_scope),
        },
        "after_query_rewrite": {
            "rewritten_query": query_rewrite.get("rewritten_query"),
            "uses_chat_history": query_rewrite.get("uses_chat_history"),
            "followup_scope_intent": followup_scope_intent,
        },
        "after_search_scope": {
            "search_filters": _metadata_search_filters(metadata),
            "temporal_context": metadata.get("temporal_context"),
            "scope_source": metadata.get("scope_source"),
            "scope_decision": metadata.get("scope_decision"),
            "search_scope": _compact_scope_for_state_trace(current_scope),
            "search_scope_file_count": _scope_file_count(current_scope),
        },
        "after_routing": {
            "route": metadata.get("route"),
            "routing_context": monitoring.get("routing") or metadata.get("routing_context"),
        },
        "after_retrieval": {
            "candidate_count_after_filter": retrieval.get("candidate_count_after_filter"),
            "document_coverage_applied": retrieval.get("document_coverage_applied"),
            "document_coverage_reason": retrieval.get("document_coverage_reason"),
            "prior_scope_required_file_count": retrieval.get("prior_scope_required_file_count"),
            "prior_scope_required_file_names": retrieval.get("prior_scope_required_file_names"),
            "prior_scope_required_file_names_missing_after_filter": retrieval.get("prior_scope_required_file_names_missing_after_filter"),
            "selected_file_names": retrieval.get("selected_file_names"),
        },
        "suspect_transitions": {
            "prior_scope_files_dropped": _scope_file_count(prior_scope) > _scope_file_count(current_scope),
        },
    }


def build_message_trace_detail(message: dict[str, Any], *, user_question: str | None = None) -> dict[str, Any]:
    """assistant 응답을 Chat Monitoring용 trace section으로 나눕니다."""
    metadata = _message_metadata(message)
    monitoring = metadata.get("monitoring") or {}
    retrieval = _metadata_retrieval(metadata)
    query_rewrite = _metadata_query_rewrite(metadata)
    rerank_info = metadata.get("rerank_info") or []
    source_count = len(group_sources_by_document(rerank_info))
    answer = str(message.get("content") or "")
    citation_ranks = sorted(extract_citation_ranks(answer, source_count=None))
    return {
        "query_rewrite": {
            "original_question": user_question or metadata.get("question"),
            "rewritten_query": query_rewrite.get("rewritten_query"),
            "uses_chat_history": query_rewrite.get("uses_chat_history"),
            "followup_scope_intent": query_rewrite.get("followup_scope_intent") or metadata.get("followup_scope_intent"),
            "scope_source": metadata.get("scope_source"),
            "scope_decision": metadata.get("scope_decision"),
        },
        "scope": {
            "search_filters": _metadata_search_filters(metadata),
            "temporal_context": metadata.get("temporal_context"),
            "selection_context": metadata.get("selection_context"),
            "industry_lookup_context": metadata.get("industry_lookup_context"),
            "search_scope": metadata.get("search_scope"),
        },
        "routing": {
            "route": metadata.get("route"),
            "routing_context": monitoring.get("routing") or metadata.get("routing_context"),
            "route_hint": (monitoring.get("routing") or {}).get("route_hint"),
            "has_vector_intent": (monitoring.get("routing") or {}).get("has_vector_intent"),
            "full_period_request": (monitoring.get("routing") or {}).get("full_period_request"),
        },
        "state_transitions": _build_state_transition_trace(metadata),
        "retrieval": retrieval,
        "sources": rerank_info,
        "answer": {
            "assistant_preview": _safe_preview(answer, 500),
            "source_count": source_count,
            "citation_ranks_used": citation_ranks,
            "citation_valid": _citation_valid(answer, rerank_info),
        },
    }


def build_message_trace_summary(
    detail: dict[str, Any],
    *,
    diff: dict[str, Any] | None = None,
    hints: list[str] | None = None,
) -> dict[str, Any]:
    """trace detail을 Chat Monitoring overview로 평탄화합니다."""
    query_rewrite = detail.get("query_rewrite") or {}
    scope = detail.get("scope") or {}
    routing = detail.get("routing") or {}
    retrieval = detail.get("retrieval") or {}
    answer = detail.get("answer") or {}
    scope_decision = query_rewrite.get("scope_decision") or {}
    industry_lookup = scope.get("industry_lookup_context") or {}
    return {
        "original_question": query_rewrite.get("original_question"),
        "rewritten_query": query_rewrite.get("rewritten_query"),
        "followup_scope_intent": query_rewrite.get("followup_scope_intent"),
        "route": routing.get("route"),
        "scope_source": query_rewrite.get("scope_source"),
        "scope_reason": scope_decision.get("reason"),
        "industry_term": scope_decision.get("industry_term") or industry_lookup.get("term"),
        "search_filters": scope.get("search_filters") or {},
        "candidate_count_after_filter": retrieval.get("candidate_count_after_filter"),
        "source_count": answer.get("source_count") or retrieval.get("source_count"),
        "prior_scope_file_count": (detail.get("state_transitions") or {}).get("input", {}).get("prior_search_scope_file_count"),
        "search_scope_file_count": (detail.get("state_transitions") or {}).get("after_search_scope", {}).get("search_scope_file_count"),
        "citation_valid": answer.get("citation_valid"),
        "debug_hint_count": len(hints or []),
        "diff_available": bool(diff),
    }


def _dict_diff(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    keys = set(current) | set(previous)
    kept = {key: current[key] for key in keys if key in current and key in previous and current[key] == previous[key]}
    added = {key: current[key] for key in keys if key in current and key not in previous}
    removed = {key: previous[key] for key in keys if key in previous and key not in current}
    changed = {key: {"previous": previous[key], "current": current[key]} for key in keys if key in current and key in previous and current[key] != previous[key]}
    return {"kept": kept, "added": added, "removed": removed, "changed": changed}


def build_response_diff(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    """선택한 assistant 응답을 직전 성공 응답과 비교합니다."""
    if not previous:
        return {}
    current_metadata = _message_metadata(current)
    previous_metadata = _message_metadata(previous)
    current_retrieval = _metadata_retrieval(current_metadata)
    previous_retrieval = _metadata_retrieval(previous_metadata)
    current_files = set(_ordered_file_names(current_metadata.get("rerank_info") or []))
    previous_files = set(_ordered_file_names(previous_metadata.get("rerank_info") or []))
    return {
        "rewritten_query_changed": _metadata_query_rewrite(current_metadata).get("rewritten_query") != _metadata_query_rewrite(previous_metadata).get("rewritten_query"),
        "route_changed": current_metadata.get("route") != previous_metadata.get("route"),
        "search_filters": _dict_diff(_metadata_search_filters(current_metadata), _metadata_search_filters(previous_metadata)),
        "temporal_context_changed": current_metadata.get("temporal_context") != previous_metadata.get("temporal_context"),
        "scope_source_changed": current_metadata.get("scope_source") != previous_metadata.get("scope_source"),
        "scope_decision_changed": current_metadata.get("scope_decision") != previous_metadata.get("scope_decision"),
        "state": {
            "prior_to_current_file_count": {
                "input_prior_search_scope": _scope_file_count(((current_metadata.get("monitoring") or {}).get("state_trace") or {}).get("input", {}).get("prior_search_scope")),
                "current_search_scope": _scope_file_count(current_metadata.get("search_scope")),
            },
            "search_scope_file_count_delta_vs_previous": _scope_file_count(current_metadata.get("search_scope")) - _scope_file_count(previous_metadata.get("search_scope")),
        },
        "sources": {
            "previous_count": len(previous_files),
            "current_count": len(current_files),
            "count_delta": len(current_files) - len(previous_files),
            "added": sorted(current_files - previous_files),
            "removed": sorted(previous_files - current_files),
        },
        "retrieval": {
            "candidate_count_after_filter_delta": _delta(
                current_retrieval.get("candidate_count_after_filter"),
                previous_retrieval.get("candidate_count_after_filter"),
            )
        },
    }


def build_chat_trace_debug_hints(
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    user_question: str | None = None,
) -> list[str]:
    """Chat Monitoring 실패 pattern에 대한 rule 기반 hint를 반환합니다."""
    hints: list[str] = []
    metadata = _message_metadata(current)
    previous_metadata = _message_metadata(previous)
    filters = _metadata_search_filters(metadata)
    previous_filters = _metadata_search_filters(previous_metadata)
    retrieval = _metadata_retrieval(metadata)
    question = str(user_question or metadata.get("question") or "")
    followup_intent = bool(metadata.get("followup_scope_intent") or _metadata_query_rewrite(metadata).get("followup_scope_intent"))
    if followup_intent and metadata.get("scope_source") != "prior_search_scope" and not metadata.get("scope_decision"):
        hints.append("⚠️ 후속 질문 가능성이 있지만 prior_search_scope가 사용되지 않았습니다.")
    for key in ("report_date_start", "report_date_end"):
        if previous_filters.get(key) and not filters.get(key):
            hints.append("⚠️ 직전 응답에는 날짜 필터가 있었는데 현재 응답에서 날짜 필터가 사라졌습니다.")
            break
    file_names = _ordered_file_names(metadata.get("rerank_info") or [])
    if len(metadata.get("rerank_info") or []) > 1 and len(set(file_names)) <= 1:
        hints.append("⚠️ 복수 문서 요청처럼 보이지만 selected source가 1개 문서에 편중되어 있습니다.")
    if retrieval.get("candidate_count_after_filter") == 0:
        hints.append("⚠️ candidate_count_after_filter=0입니다. metadata filter가 과도할 수 있습니다.")
    if metadata.get("route") == "rdb" and any(keyword in question for keyword in ("주요 내용", "요약", "리스크", "투자포인트", "투자 포인트")):
        hints.append("⚠️ route=rdb인데 질문에 주요 내용/요약/리스크/투자포인트가 포함되어 있습니다.")
    multi_doc_terms = ("전체", "각각", "리포트들", "목록", "비교", "모두", "여러")
    if any(term in question for term in multi_doc_terms) and retrieval.get("document_coverage_applied") is False:
        hints.append("⚠️ document_coverage_applied=False인데 질문에 전체/각각/리포트들이 포함되어 있습니다.")
    return list(dict.fromkeys(hints))


def previous_successful_assistant(messages: list[dict[str, Any]], selected_message_id: Any) -> dict[str, Any] | None:
    """selected_message_id 앞의 성공한 assistant 응답을 반환합니다."""
    previous: dict[str, Any] | None = None
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if message.get("id") == selected_message_id:
            return previous
        if (_message_metadata(message)).get("status") == "succeeded":
            previous = message
    return previous


def user_question_before_message(messages: list[dict[str, Any]], selected_message_id: Any) -> str | None:
    """선택한 assistant 응답 앞의 user 질문을 반환합니다."""
    latest_user: str | None = None
    for message in messages:
        if message.get("id") == selected_message_id:
            return latest_user
        if message.get("role") == "user":
            latest_user = str(message.get("content") or "")
    return latest_user


def _compact_trace_message(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not message:
        return None
    metadata = _message_metadata(message)
    return {
        "id": message.get("id"),
        "role": message.get("role"),
        "created_at": message.get("created_at"),
        "content_preview": _safe_preview(message.get("content"), 500),
        "metadata": {
            "status": metadata.get("status"),
            "route": metadata.get("route"),
            "search_filters": _metadata_search_filters(metadata),
            "scope_source": metadata.get("scope_source"),
            "scope_decision": metadata.get("scope_decision"),
            "retrieval": _metadata_retrieval(metadata),
            "source_files": _ordered_file_names(metadata.get("rerank_info") or []),
        },
    }


def build_chat_trace_issue_context(
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    selected_message_id: Any,
) -> dict[str, Any]:
    """선택한 Chat Monitoring trace로 issue report context를 만듭니다."""
    selected = next((message for message in messages if message.get("id") == selected_message_id), None)
    previous = previous_successful_assistant(messages, selected_message_id)
    selected_question = user_question_before_message(messages, selected_message_id)
    return {
        "thread_id": thread.get("id"),
        "thread_name": thread.get("name"),
        "submitted_from": "chat_monitoring_trace",
        "selected_user_question": selected_question,
        "selected_message": _compact_trace_message(selected),
        "previous_message": _compact_trace_message(previous),
        "trace_detail": build_message_trace_detail(selected or {}, user_question=selected_question),
        "diff": build_response_diff(selected or {}, previous),
        "debug_hints": build_chat_trace_debug_hints(selected or {}, previous, user_question=selected_question),
    }


def build_reusable_search_scope(final_state: dict[str, Any]) -> dict[str, Any] | None:
    """완료된 graph state에서 재사용 가능한 retrieval scope를 만듭니다."""
    search_filters = dict(final_state.get("search_filters") or {})
    temporal_context = final_state.get("temporal_context")
    sources = final_state.get("rerank_info") or final_state.get("rdb_sources") or []
    file_names: list[str] = []
    seen_file_names: set[str] = set()
    for source in sources:
        file_name = (source or {}).get("file_name")
        if file_name and file_name != "-" and file_name not in seen_file_names:
            seen_file_names.add(str(file_name))
            file_names.append(str(file_name))

    if not search_filters and not temporal_context and not file_names:
        return None

    scope: dict[str, Any] = {
        "route": final_state.get("route"),
        "search_filters": search_filters,
        "temporal_context": temporal_context,
        "scope_source": final_state.get("scope_source"),
    }
    if file_names:
        scope["file_names"] = file_names
    scope["answer_scope_index"] = build_answer_scope_index(scope, sources)
    return scope


def _expected_state_value(final_state: dict[str, Any], key: str) -> Any:
    if key == "scope_decision_reason":
        return (final_state.get("scope_decision") or {}).get("reason")
    if key == "scope_decision_matched_section_id":
        return (final_state.get("scope_decision") or {}).get("matched_section_id")
    return final_state.get(key)


def _expected_state_matches(expected_state: dict[str, Any], final_state: dict[str, Any]) -> tuple[bool, dict[str, dict[str, Any]]]:
    mismatches: dict[str, dict[str, Any]] = {}
    for key, expected_value in (expected_state or {}).items():
        actual_value = _expected_state_value(final_state, key)
        if actual_value != expected_value:
            mismatches[key] = {"expected": expected_value, "actual": actual_value}
    return not mismatches, mismatches


def evaluate_multiturn_turn_result(
    turn: dict[str, Any],
    final_state: dict[str, Any],
    *,
    latency_seconds: float,
    latency_threshold_seconds: float = 30.0,
    input_had_chat_history: bool = False,
    input_had_prior_search_scope: bool = False,
) -> dict[str, Any]:
    """multi-turn evaluation turn을 graph output과 input context 기준으로 채점합니다."""
    result = evaluate_dataset_case_result(
        turn,
        final_state,
        latency_seconds=latency_seconds,
        latency_threshold_seconds=latency_threshold_seconds,
    )
    expected_input = turn.get("expected_input") or {}
    chat_history_pass = (
        True
        if "chat_history" not in expected_input
        else input_had_chat_history == bool(expected_input.get("chat_history"))
    )
    prior_scope_pass = (
        True
        if "prior_search_scope" not in expected_input
        else input_had_prior_search_scope == bool(expected_input.get("prior_search_scope"))
    )
    state_pass, state_mismatches = _expected_state_matches(turn.get("expected_state") or {}, final_state)
    passed = result["status"] == "pass" and chat_history_pass and prior_scope_pass and state_pass
    result.update(
        {
            "status": "pass" if passed else "fail",
            "chat_history_pass": chat_history_pass,
            "prior_scope_pass": prior_scope_pass,
            "expected_state_pass": state_pass,
            "expected_state_mismatches": state_mismatches,
            "actual_state": {
                "uses_chat_history": final_state.get("uses_chat_history"),
                "followup_scope_intent": final_state.get("followup_scope_intent"),
                "scope_source": final_state.get("scope_source"),
                "scope_decision_reason": (final_state.get("scope_decision") or {}).get("reason"),
                "scope_decision_matched_section_id": (final_state.get("scope_decision") or {}).get("matched_section_id"),
            },
        }
    )
    return result


def _summarize_multiturn_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    case_count = len(results)
    turn_results = [turn for result in results for turn in result.get("turn_results", [])]
    turn_count = len(turn_results)
    passed = sum(1 for result in results if result.get("status") == "pass")
    turn_passed = sum(1 for turn in turn_results if turn.get("status") == "pass")
    base_summary = _summarize_eval_results(turn_results)
    base_summary.update(
        {
            "case_count": case_count,
            "turn_count": turn_count,
            "passed": passed,
            "failed": case_count - passed,
            "pass_rate": passed / case_count if case_count else 0.0,
            "turn_passed": turn_passed,
            "turn_failed": turn_count - turn_passed,
            "turn_pass_rate": turn_passed / turn_count if turn_count else 0.0,
        }
    )
    return base_summary


def run_multiturn_evaluation_dataset(
    dataset: dict[str, Any],
    invoke_fn: Callable[..., dict[str, Any]],
    *,
    output_dir: str | Path,
    limit: int | None = None,
    selected_case_ids: list[str] | tuple[str, ...] | set[str] | None = None,
    latency_threshold_seconds: float = 30.0,
    execution_mode: str = "current_data",
    data_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """prior retrieval scope를 이어가며 multi-turn case를 실행합니다."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    cases = select_evaluation_cases(dataset, selected_case_ids)
    if limit:
        cases = cases[:limit]
    results: list[dict[str, Any]] = []
    for case in cases:
        case_id = case.get("id", "case")
        thread_id = f"monitoring_multiturn_eval_{run_id}_{case_id}"
        prior_search_scope: dict[str, Any] | None = None
        turn_results: list[dict[str, Any]] = []
        for turn_index, turn in enumerate(case.get("turns") or [], 1):
            payload: dict[str, Any] = {
                "question": turn.get("question", ""),
            }
            input_had_chat_history = False
            input_had_prior_search_scope = prior_search_scope is not None
            if prior_search_scope is not None:
                payload["prior_search_scope"] = prior_search_scope
            started = time.perf_counter()
            final_state = invoke_fn(
                payload,
                config={"configurable": {"thread_id": thread_id}},
            )
            latency = time.perf_counter() - started
            turn_result = evaluate_multiturn_turn_result(
                turn,
                final_state,
                latency_seconds=latency,
                latency_threshold_seconds=latency_threshold_seconds,
                input_had_chat_history=input_had_chat_history,
                input_had_prior_search_scope=input_had_prior_search_scope,
            )
            turn_result["turn_index"] = turn_index
            turn_results.append(turn_result)
            prior_search_scope = build_reusable_search_scope(final_state)
        results.append(
            {
                "case_id": case_id,
                "description": case.get("description"),
                "status": "pass" if turn_results and all(turn.get("status") == "pass" for turn in turn_results) else "fail",
                "turn_count": len(turn_results),
                "turn_results": turn_results,
            }
        )
    run = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_name": dataset.get("name"),
        "dataset_version": dataset.get("version"),
        "evaluation_type": "multiturn",
        "execution_mode": execution_mode,
        "data_source": data_source or {},
        "selected_case_ids": [case.get("id") for case in cases],
        "summary": _summarize_multiturn_results(results),
        "results": results,
    }
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    json_path = output_path / f"multiturn_evaluation_run_{run_id}.json"
    json_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    run["json_path"] = str(json_path)
    return run


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
    """fixed evaluation case를 graph output 기준으로 채점합니다."""
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
    """명시된 id로 선택한 evaluation case를 선택 순서대로 반환합니다."""
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
    execution_mode: str = "current_data",
    data_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """고정 dataset을 graph로 실행하고 JSON experiment run을 저장합니다."""
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    cases = select_evaluation_cases(dataset, selected_case_ids)
    if limit:
        cases = cases[:limit]
    results: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        final_state = invoke_fn(
            {"question": case.get("question", "")},
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
        "execution_mode": execution_mode,
        "data_source": data_source or {},
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
    """실패한 evaluation case의 next-step guidance를 만듭니다."""
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
    """저장된 두 experiment run 사이의 summary delta를 반환합니다."""
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


def filter_evaluation_runs_by_mode(
    runs: list[dict[str, Any]],
    execution_mode: str | None,
) -> list[dict[str, Any]]:
    """execution mode와 일치하는 저장된 evaluation run을 반환합니다."""
    if not execution_mode:
        return runs
    return [
        run
        for run in runs
        if (run.get("execution_mode") or "current_data") == execution_mode
    ]


def summarize_all_chat_threads(thread_messages: list[dict[str, Any]]) -> dict[str, Any]:
    """모든 thread에 저장된 chat 품질 signal을 요약합니다."""
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


def infer_issue_impact_area(report: dict[str, Any]) -> str:
    """issue report의 trace/hint/category에서 운영 triage 영역을 추정합니다."""
    context = report.get("context") or {}
    hints_text = "\n".join(str(hint) for hint in context.get("debug_hints") or [])
    content = "\n".join(
        str(value or "")
        for value in [
            report.get("category"),
            report.get("description"),
            report.get("content"),
            hints_text,
        ]
    )
    if any(term in content for term in ("날짜 필터", "필터", "scope", "prior_search_scope", "검색 범위")):
        return "filter_scope"
    if any(term in content for term in ("route", "라우팅", "router", "RDB")):
        return "routing"
    if any(term in content for term in ("source", "출처", "문서 편중", "coverage", "candidate_count", "no-result")):
        return "retrieval_source"
    if any(term in content for term in ("citation", "인용", "참조")):
        return "citation"
    if any(term in content for term in ("latency", "지연", "느림", "멈춤")):
        return "latency"
    if any(term in content for term in ("화면", "사용성", "UI")):
        return "ui"
    return "answer_quality"


def _draft_expected_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources or []:
        file_name = str((source or {}).get("file_name") or "").strip()
        if not file_name or file_name == "-" or file_name in seen:
            continue
        seen.add(file_name)
        row = {"file_name": file_name}
        if source.get("report_type"):
            row["report_type"] = source.get("report_type")
        expected.append(row)
    return expected


def build_eval_case_draft_from_issue_report(report: dict[str, Any]) -> dict[str, Any] | None:
    """Chat Monitoring trace issue를 운영자가 수정 가능한 evaluation case 초안으로 변환합니다."""
    context = report.get("context") or {}
    trace = context.get("trace_detail") or {}
    if not trace:
        return None
    query_rewrite = trace.get("query_rewrite") or {}
    scope = trace.get("scope") or {}
    routing = trace.get("routing") or {}
    route = routing.get("route")
    question = context.get("selected_user_question") or query_rewrite.get("original_question")
    if not question or not route:
        return None

    scope_decision = query_rewrite.get("scope_decision") or {}
    impact_area = infer_issue_impact_area(report)
    expected_state: dict[str, Any] = {}
    if query_rewrite.get("followup_scope_intent") is not None:
        expected_state["followup_scope_intent"] = query_rewrite.get("followup_scope_intent")
    if query_rewrite.get("scope_source") is not None:
        expected_state["scope_source"] = query_rewrite.get("scope_source")
    if scope_decision.get("reason") is not None:
        expected_state["scope_decision_reason"] = scope_decision.get("reason")
    if scope_decision.get("matched_section_id") is not None:
        expected_state["scope_decision_matched_section_id"] = scope_decision.get("matched_section_id")

    monitoring_dimensions = list(
        dict.fromkeys(
            [
                impact_area,
                "routing",
                "filter" if scope.get("search_filters") else "scope",
                "retrieval" if route == "vectordb" else "rdb",
            ]
        )
    )
    draft = {
        "id": f"issue_{report.get('id') or uuid.uuid4().hex[:8]}",
        "type": "vectordb_retrieval" if route == "vectordb" else "rdb_aggregate",
        "question": question,
        "expected_route": route,
        "expected_filters": scope.get("search_filters") or {},
        "expected_sources": _draft_expected_sources(trace.get("sources") or []),
        "expected_state": expected_state,
        "criteria_tags": ["issue_report_regression"],
        "monitoring_dimensions": monitoring_dimensions,
        "checks": ["route_pass", "filter_pass", "source_hit", "citation_valid"],
        "source_issue_report_id": report.get("id"),
        "review_required": True,
    }
    return draft


def list_regression_candidates(candidate_dir: str | Path) -> list[dict[str, Any]]:
    """저장된 regression candidate artifact를 최신순으로 반환합니다."""
    root = Path(candidate_dir)
    if not root.exists():
        return []
    candidates: list[dict[str, Any]] = []
    for path in root.glob("candidate_*.json"):
        try:
            candidate = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        candidate.setdefault("json_path", str(path))
        candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda candidate: (str(candidate.get("created_at") or ""), str(candidate.get("id") or "")),
        reverse=True,
    )


def build_regression_candidate_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Regression candidate table에 표시할 안전한 요약 row를 만듭니다."""
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        draft = candidate.get("eval_case_draft") or {}
        rows.append(
            {
                "id": candidate.get("id"),
                "created_at": candidate.get("created_at"),
                "triage_status": candidate.get("triage_status", "new"),
                "operator_decision": candidate.get("operator_decision", "unreviewed"),
                "severity": candidate.get("severity", "untriaged"),
                "impact_area": candidate.get("impact_area"),
                "category": candidate.get("category"),
                "thread_id": candidate.get("thread_id"),
                "has_eval_case_draft": bool(draft),
                "draft_question": _safe_preview(draft.get("question"), 100),
                "expected_route": draft.get("expected_route"),
                "expected_filters": draft.get("expected_filters") or {},
                "expected_source_count": len(draft.get("expected_sources") or []),
                "recommended_next_step": candidate.get("recommended_next_step"),
                "json_path": candidate.get("json_path"),
            }
        )
    return rows


def build_regression_candidate_dataset(
    candidates: list[dict[str, Any]],
    selected_candidate_ids: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any]:
    """선택한 candidate draft를 evaluation runner가 사용할 dataset 형태로 변환합니다."""
    selected = set(selected_candidate_ids or [])
    cases = [
        candidate["eval_case_draft"]
        for candidate in candidates
        if candidate.get("eval_case_draft") and (not selected or candidate.get("id") in selected)
    ]
    return {
        "name": "finance_llm_regression_candidate_dataset",
        "version": 1,
        "description": "Issue report regression candidate에서 생성한 임시 evaluation dataset입니다. 정식 fixture 반영 전 운영자 검토가 필요합니다.",
        "cases": cases,
    }


def promote_issue_report_to_eval_candidate(
    report: dict[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    """issue report를 regression/evaluation candidate artifact로 저장합니다."""
    candidate_id = f"candidate_{report.get('id') or uuid.uuid4().hex[:8]}"
    eval_case_draft = build_eval_case_draft_from_issue_report(report)
    candidate = {
        "id": candidate_id,
        "status": "candidate",
        "triage_status": "new",
        "operator_decision": "unreviewed",
        "severity": "untriaged",
        "impact_area": infer_issue_impact_area(report),
        "source": "issue_report",
        "source_report_id": report.get("id"),
        "thread_id": report.get("thread_id"),
        "category": report.get("category"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_file_path": report.get("file_path"),
        "source_json_path": report.get("json_path"),
        "preview": _issue_report_preview(report.get("content") or "", max_chars=500),
        "recommended_next_step": "review_eval_case_draft" if eval_case_draft else "convert_to_evaluation_dataset_case",
    }
    if eval_case_draft:
        candidate["eval_case_draft"] = eval_case_draft
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
    return [
        "데이터/설정",
        "미임베딩 문서",
        "실험 실행",
        "고정 테스트셋",
        "Parsing engines",
        "전체 Monitoring",
        "Chat Monitoring",
        "Issue reports",
    ]


def build_monitoring_page_labels() -> list[str]:
    """Monitoring Mode가 켜졌을 때 보여줄 top-level page를 반환합니다."""
    return ["Chat", "전체 Monitoring"]


def compact_graph_monitoring_metadata(
    *,
    final_state: dict[str, Any],
    latency_seconds: float,
    rerank_info: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """완료된 graph state에서 응답별 monitoring metadata를 만듭니다."""
    route = final_state.get("route")
    rerank_info = rerank_info or []
    metadata: dict[str, Any] = {
        "route": route,
        "latency_seconds": round(latency_seconds, 3),
        "search_filters": final_state.get("search_filters") or {},
        "temporal_context": final_state.get("temporal_context"),
        "selection_context": final_state.get("selection_context"),
        "scope_source": final_state.get("scope_source"),
        "routing_context": final_state.get("routing_context"),
        "scope_decision": final_state.get("scope_decision"),
        "industry_lookup_context": final_state.get("industry_lookup_context"),
        "monitoring": {
            "query_rewrite": {
                "rewritten_query": final_state.get("rewritten_query"),
                "uses_chat_history": final_state.get("uses_chat_history"),
                "followup_scope_intent": final_state.get("followup_scope_intent"),
            },
            "state_trace": {
                "input": {
                    "question": final_state.get("question"),
                    "prior_search_scope": _compact_scope_for_state_trace(final_state.get("prior_search_scope")),
                    "active_scope": _compact_scope_for_state_trace(final_state.get("active_scope")),
                }
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
