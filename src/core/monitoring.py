"""Monitoring Mode helper입니다."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from src.utils.citations import extract_citation_ranks, group_sources_by_document

from src.configs.settings import BASE_DIR
from src.core import artifact_io
from src.core import reproduction_manifest as reproduction_manifest_module
from src.core.app_version import get_app_version
from src.core.artifact_io import (
    is_safe_artifact_identifier,
    safe_artifact_token,
    strict_json_loads,
)
from src.core.answer_requirements import (
    AnswerRequirementValidationError,
    canonicalize_answer_requirements,
    evaluate_answer_requirements,
)
from src.core.followup_scope import build_answer_scope_index

EVALUATION_DATASET_PATH = BASE_DIR / "tests" / "fixtures" / "evaluation_dataset.json"
MULTITURN_EVALUATION_DATASET_PATH = BASE_DIR / "tests" / "fixtures" / "multiturn_evaluation_dataset.json"

_CANDIDATE_WRITE_LOCK = threading.RLock()
_AUTOMATIC_CHECKS = {
    "answer_requirements_pass",
    "route_pass",
    "filter_pass",
    "source_hit",
    "citation_valid",
    "latency_pass",
    "no_result_absent",
    "expected_state_pass",
}
_MANUAL_CHECK = "manual_assertions_pass"
_PERFORMANCE_CHECK = "performance_p95_pass"
_CORRECTNESS_CHECKS = (
    _AUTOMATIC_CHECKS - {"latency_pass"}
) | {_MANUAL_CHECK}
_NATIVE_V2_DATA_SOURCE_FIELDS = {
    "backend_mode",
    "runtime_mode",
    "snapshot_id",
    "build_id",
    "profile_hash",
    "publication_generation",
    "write_epoch",
    "degraded",
}
_ALL_HARD_CHECKS = _AUTOMATIC_CHECKS | {
    _MANUAL_CHECK,
    _PERFORMANCE_CHECK,
}
_SOFT_OBJECTIVES = {
    "latency_p95",
    "answer_conciseness",
    "answer_depth",
}
_QUALITY_PROFILE_ALIASES = {
    "accuracy": "accuracy_first",
    "speed": "speed_first",
}
QUALITY_PROFILE_RULES = {
    "accuracy_first": {
        "label": "정확성 우선",
        "default_performance_budget": {
            "max_p95_seconds": 30.0,
            "min_runs": 3,
            "warmup_runs": 0,
            "enforcement": "soft",
        },
        "default_soft_objectives": ["latency_p95"],
    },
    "balanced": {
        "label": "균형형",
        "default_performance_budget": {
            "max_p95_seconds": 20.0,
            "min_runs": 3,
            "warmup_runs": 0,
            "enforcement": "soft",
        },
        "default_soft_objectives": ["latency_p95"],
    },
    "speed_first": {
        "label": "속도 우선",
        "default_performance_budget": {
            "max_p95_seconds": 10.0,
            "min_runs": 5,
            "warmup_runs": 1,
            "enforcement": "hard",
        },
        "default_soft_objectives": ["answer_conciseness"],
    },
}
_EVALUATION_PROFILES = {
    "accuracy_first": [
        "route_pass",
        "filter_pass",
        "source_hit",
        "citation_valid",
        "no_result_absent",
        "expected_state_pass",
    ],
    "balanced": [
        "route_pass",
        "filter_pass",
        "source_hit",
        "citation_valid",
        "latency_pass",
        "no_result_absent",
    ],
    "speed_first": [
        "route_pass",
        "filter_pass",
        "source_hit",
        "latency_pass",
    ],
}
_VERIFICATION_TYPES = {
    "graph_contract",
    "mixed",
    "error_absence",
    "latency_budget",
    "manual_answer_quality",
    "manual_ui",
}
_IMPACT_AREAS = {
    "routing",
    "filter_scope",
    "retrieval_source",
    "citation",
    "latency",
    "ui",
    "answer_quality",
}
_TERMINAL_CANDIDATE_STATUSES = {
    "closed",
    "duplicate",
    "rejected",
    "not_reproducible",
}


class CandidateLoadError(RuntimeError):
    """Raised when a persisted regression candidate is malformed or corrupt."""


class CandidateValidationError(ValueError):
    """Raised when candidate data does not satisfy the public contract."""


class CandidateConflictError(RuntimeError):
    """Raised when a compare-and-swap update observes a newer candidate."""


class CandidateTransitionError(ValueError):
    """Raised when a lifecycle transition does not satisfy its gate."""


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


def _message_has_native_v2_provenance(message: Mapping[str, Any]) -> bool:
    metadata = message.get("metadata")
    runtime = (
        metadata.get("retrieval_runtime")
        if isinstance(metadata, Mapping)
        else None
    )
    if not isinstance(runtime, Mapping):
        return False
    generation = runtime.get("publication_generation")
    write_epoch = runtime.get("write_epoch")
    return (
        runtime.get("mode") == "native"
        and isinstance(runtime.get("active_snapshot_id"), str)
        and bool(runtime.get("active_snapshot_id"))
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation > 0
        and isinstance(write_epoch, int)
        and not isinstance(write_epoch, bool)
        and write_epoch > 0
        and runtime.get("degraded") is False
    )


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
    selected_source_counts = [
        len(_metadata_selected_sources(message.get("metadata") or {}))
        for message in assistant_messages
        if (message.get("metadata") or {}).get("status") == "succeeded"
    ]
    latencies = [
        float((message.get("metadata") or {}).get("latency_seconds"))
        for message in assistant_messages
        if (
            _message_has_native_v2_provenance(message)
            and isinstance(
                (message.get("metadata") or {}).get("latency_seconds"),
                (int, float),
            )
        )
    ]
    latencies.sort()

    return {
        "message_count": len(messages),
        "assistant_message_count": len(assistant_messages),
        "statuses": dict(statuses),
        "routes": dict(routes),
        "avg_selected_source_count": (
            sum(selected_source_counts) / len(selected_source_counts)
            if selected_source_counts
            else 0.0
        ),
        "avg_rerank_source_count": (
            sum(selected_source_counts) / len(selected_source_counts)
            if selected_source_counts
            else 0.0
        ),
        "avg_latency_seconds": (
            sum(latencies) / len(latencies)
            if latencies
            else None
        ),
        "p95_latency_seconds": (
            _percentile(latencies, 0.95) if latencies else None
        ),
        "latency_sample_count": len(latencies),
    }


def _duration_seconds(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        return None
    return duration


def _duration_seconds_from_ns(value: Any) -> float | None:
    duration_ns = _duration_seconds(value)
    return None if duration_ns is None else duration_ns / 1_000_000_000


def build_chat_latency_rows(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return successful Native V2 response timings for one conversation."""

    rows: list[dict[str, Any]] = []
    for message in messages:
        if (
            message.get("role") != "assistant"
            or not _message_has_native_v2_provenance(message)
        ):
            continue
        metadata = message.get("metadata") or {}
        if metadata.get("status") != "succeeded":
            continue
        route = metadata.get("route")
        monitoring = metadata.get("monitoring") or {}
        rdb = monitoring.get("rdb") or {}
        retrieval = monitoring.get("retrieval") or {}
        response_seconds = _duration_seconds(metadata.get("latency_seconds"))
        rdb_seconds = (
            _duration_seconds_from_ns(rdb.get("query_ns"))
            if route == "rdb"
            else None
        )
        vector_seconds = (
            _duration_seconds_from_ns(retrieval.get("native_total_ns"))
            if route == "vectordb"
            else None
        )
        if response_seconds is None and rdb_seconds is None and vector_seconds is None:
            continue
        rows.append(
            {
                "created_at": message.get("created_at"),
                "route": route,
                "response_seconds": response_seconds,
                "rdb_seconds": rdb_seconds,
                "vector_seconds": vector_seconds,
            }
        )
    return rows


def summarize_chat_latency_metrics(
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize current-thread response and backend durations."""

    rows = build_chat_latency_rows(messages)
    response_values = [
        row["response_seconds"]
        for row in rows
        if row["response_seconds"] is not None
    ]
    rdb_values = [
        row["rdb_seconds"] for row in rows if row["rdb_seconds"] is not None
    ]
    vector_values = [
        row["vector_seconds"]
        for row in rows
        if row["vector_seconds"] is not None
    ]
    return {
        "latest_response_seconds": (
            response_values[-1] if response_values else None
        ),
        "avg_response_seconds": (
            sum(response_values) / len(response_values)
            if response_values
            else None
        ),
        "response_sample_count": len(response_values),
        "avg_rdb_seconds": (
            sum(rdb_values) / len(rdb_values) if rdb_values else None
        ),
        "rdb_sample_count": len(rdb_values),
        "avg_vector_seconds": (
            sum(vector_values) / len(vector_values)
            if vector_values
            else None
        ),
        "vector_sample_count": len(vector_values),
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
        selected_sources = _metadata_selected_sources(metadata)
        search_filters = search_scope.get("search_filters") or metadata.get("search_filters") or {}
        scope_decision = metadata.get("scope_decision") or {}
        route = metadata.get("route", "-")
        created_at = message.get("created_at")
        source_names = _ordered_file_names(selected_sources)
        vector_sources = selected_sources if route == "vectordb" else []
        rdb_sources = selected_sources if route == "rdb" else []
        used_chunks = _build_used_chunk_rows(vector_sources, [])
        used_documents = _build_used_document_rows(used_chunks)
        rdb_evidence = _build_rdb_evidence_rows(rdb_sources)
        source_count = len(used_documents) + len(rdb_evidence)
        answer = str(message.get("content") or "")
        citation_ranks = (
            sorted(extract_citation_ranks(answer, source_count=None))
            if route == "vectordb"
            else []
        )
        citation_valid = (
            _citation_valid(answer, vector_sources)
            if route == "vectordb"
            else None
        )
        retrieval_k = _build_retrieval_k_trace(metadata)
        grounding = _build_grounding_trace(
            metadata,
            selected_sources=selected_sources,
            citation_ranks=citation_ranks,
            citation_valid=citation_valid,
        )
        state_status = _build_state_status_trace(metadata)
        rows.append(
            {
                "message_id": message.get("id"),
                "created_at": created_at,
                "user_question_preview": _safe_preview(metadata.get("question") or latest_user_question),
                "assistant_preview": _safe_preview(message.get("content")),
                "status": metadata.get("status", "unknown"),
                "route": route,
                "latency_seconds": metadata.get("latency_seconds"),
                "source_count": source_count,
                "document_count": len(used_documents),
                "chunk_count": len(used_chunks),
                "rdb_evidence_count": len(rdb_evidence),
                "state_status": state_status.get("overall"),
                "grounding_status": grounding.get("status"),
                "configured_top_k": retrieval_k.get("configured_top_k"),
                "requested_k": retrieval_k.get("requested_k"),
                "fetch_k": retrieval_k.get("fetch_k"),
                "context_count": retrieval_k.get("context_count"),
                "search_filters": search_filters,
                "scope_source": metadata.get("scope_source") or search_scope.get("scope_source"),
                "scope_decision_reason": scope_decision.get("reason"),
                "no_vector_results": bool(metadata.get("no_vector_results")),
                "selected_file_names": source_names,
                "rdb_row_count": (monitoring.get("rdb") or {}).get("row_count"),
                "error": metadata.get("error"),
                "label": _response_label(created_at, route, metadata.get("question") or latest_user_question, source_count, metadata.get("latency_seconds")),
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


def _metadata_selected_sources(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Return persisted selected sources, with legacy rerank_info fallback."""
    sources = metadata.get("selected_sources")
    if isinstance(sources, list):
        return sources
    legacy_sources = metadata.get("rerank_info")
    return legacy_sources if isinstance(legacy_sources, list) else []


def _message_metadata(message: dict[str, Any] | None) -> dict[str, Any]:
    return (message or {}).get("metadata") or {}


def _metadata_search_filters(metadata: dict[str, Any]) -> dict[str, Any]:
    search_scope = metadata.get("search_scope") or {}
    return search_scope.get("search_filters") or metadata.get("search_filters") or {}


def _metadata_retrieval(metadata: dict[str, Any]) -> dict[str, Any]:
    return (metadata.get("monitoring") or {}).get("retrieval") or {}


def _metadata_query_rewrite(metadata: dict[str, Any]) -> dict[str, Any]:
    return (metadata.get("monitoring") or {}).get("query_rewrite") or {}


def _nonnegative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _first_measured_int(*values: Any) -> int | None:
    for value in values:
        measured = _nonnegative_int(value)
        if measured is not None:
            return measured
    return None


def _build_turn_timing_trace(metadata: dict[str, Any]) -> dict[str, Any]:
    monitoring = metadata.get("monitoring") or {}
    retrieval = _metadata_retrieval(metadata)
    rdb = monitoring.get("rdb") or {}
    total_seconds = _duration_seconds(metadata.get("latency_seconds"))
    if total_seconds is None:
        total_seconds = _duration_seconds((monitoring.get("timing") or {}).get("total_seconds"))
    rdb_seconds = _duration_seconds_from_ns(rdb.get("query_ns"))
    vector_seconds = _duration_seconds_from_ns(retrieval.get("native_total_ns"))
    vector_stages = {
        "scope_compile": _duration_seconds_from_ns(retrieval.get("native_scope_compile_ns")),
        "eligibility": _duration_seconds_from_ns(retrieval.get("native_eligibility_ns")),
        "faiss": _duration_seconds_from_ns(retrieval.get("native_faiss_ns")),
        "hydration": _duration_seconds_from_ns(retrieval.get("native_hydration_ns")),
        "lease": _duration_seconds_from_ns(retrieval.get("native_lease_ns")),
    }
    measured = any(
        value is not None
        for value in (total_seconds, rdb_seconds, vector_seconds, *vector_stages.values())
    )
    return {
        "status": "measured" if measured else "not_measured",
        "total_seconds": total_seconds,
        "rdb_query_seconds": rdb_seconds,
        "vector_search_seconds": vector_seconds,
        "vector_stage_seconds": vector_stages,
    }


def _build_retrieval_k_trace(metadata: dict[str, Any]) -> dict[str, Any]:
    if metadata.get("route") != "vectordb":
        return {
            "status": "not_applicable",
            "configured_top_k": None,
            "requested_k": None,
            "fetch_k": None,
            "candidate_count_before_filter": None,
            "candidate_count_after_filter": None,
            "context_count": None,
        }

    retrieval = _metadata_retrieval(metadata)
    values = {
        "configured_top_k": _nonnegative_int(retrieval.get("search_top_k")),
        "requested_k": _nonnegative_int(retrieval.get("requested_k")),
        "fetch_k": _nonnegative_int(retrieval.get("fetch_k")),
        "candidate_count_before_filter": _first_measured_int(
            retrieval.get("candidate_count_before_filter"),
            retrieval.get("native_candidate_count"),
        ),
        "candidate_count_after_filter": _nonnegative_int(
            retrieval.get("candidate_count_after_filter")
        ),
        "context_count": _first_measured_int(
            retrieval.get("selected_source_count"),
            retrieval.get("source_count"),
        ),
    }
    if any(value is not None for value in values.values()):
        status = "measured"
    else:
        status = "not_measured"
    return {"status": status, **values}


def _build_used_chunk_rows(
    selected_sources: list[dict[str, Any]],
    citation_ranks: list[int],
) -> list[dict[str, Any]]:
    cited = set(citation_ranks)
    rows: list[dict[str, Any]] = []
    for fallback_rank, raw_source in enumerate(selected_sources, 1):
        source = raw_source if isinstance(raw_source, dict) else {}
        rank = _first_measured_int(source.get("rank"), fallback_rank)
        chunk_uid = _stable_identity_value(source.get("chunk_uid"))
        report_uid = _stable_identity_value(source.get("report_uid"))
        rows.append(
            {
                "rank": rank,
                "identity_status": (
                    "measured" if chunk_uid and report_uid else "not_measured"
                ),
                "chunk_uid": chunk_uid,
                "parent_uid": source.get("parent_uid"),
                "report_uid": report_uid,
                "file_name": source.get("file_name"),
                "target_name": source.get("target_name"),
                "report_date": source.get("report_date"),
                "title": source.get("title"),
                "broker": source.get("broker"),
                "report_type": source.get("report_type"),
                "child_index": _nonnegative_int(source.get("child_index")),
                "span_start": _nonnegative_int(source.get("span_start")),
                "span_end": _nonnegative_int(source.get("span_end")),
                "score": source.get("score"),
                "rerank_score": source.get("rerank_score"),
                "recency_score": source.get("recency_score"),
                "final_score": source.get("final_score"),
                "cited": rank in cited,
            }
        )
    return rows


def _build_used_document_rows(
    chunk_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    documents: dict[tuple[str, str], dict[str, Any]] = {}
    for position, chunk in enumerate(chunk_rows, 1):
        document_uid = _stable_identity_value(chunk.get("report_uid"))
        file_name = _stable_identity_value(chunk.get("file_name"))
        if document_uid:
            grouping_key = ("report_uid", document_uid)
        elif file_name:
            grouping_key = ("legacy_file_name", file_name)
        else:
            grouping_key = ("unknown_position", str(position))
        row = documents.get(grouping_key)
        if row is None:
            row = {
                "document_uid": document_uid,
                "identity_status": "measured" if document_uid else "not_measured",
                "file_name": chunk.get("file_name"),
                "target_name": chunk.get("target_name"),
                "report_date": chunk.get("report_date"),
                "title": chunk.get("title"),
                "broker": chunk.get("broker"),
                "report_type": chunk.get("report_type"),
                "best_rank": chunk.get("rank"),
                "chunk_count": 0,
                "cited_chunk_count": 0,
            }
            documents[grouping_key] = row
        row["chunk_count"] += 1
        if chunk.get("cited"):
            row["cited_chunk_count"] += 1
        rank = _nonnegative_int(chunk.get("rank"))
        best_rank = _nonnegative_int(row.get("best_rank"))
        if rank is not None and (best_rank is None or rank < best_rank):
            row["best_rank"] = rank
    return list(documents.values())


def _build_rdb_evidence_rows(
    selected_sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return document metadata for RDB evidence without labelling it as chunks."""

    rows: list[dict[str, Any]] = []
    for fallback_rank, raw_source in enumerate(selected_sources, 1):
        source = raw_source if isinstance(raw_source, dict) else {}
        document_uid = _stable_identity_value(source.get("report_uid"))
        rows.append(
            {
                "rank": _first_measured_int(source.get("rank"), fallback_rank),
                "document_uid": document_uid,
                "identity_status": "measured" if document_uid else "not_measured",
                "file_name": source.get("file_name"),
                "target_name": source.get("target_name"),
                "report_date": source.get("report_date"),
                "title": source.get("title"),
                "broker": source.get("broker"),
                "report_type": source.get("report_type"),
            }
        )
    return rows


def _stable_identity_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and normalized != "-" else None


def _source_identity_status(selected_sources: list[dict[str, Any]]) -> str:
    if not selected_sources:
        return "not_applicable"
    measured = sum(
        1
        for source in selected_sources
        if _stable_identity_value((source or {}).get("chunk_uid"))
        and _stable_identity_value((source or {}).get("report_uid"))
    )
    if measured == len(selected_sources):
        return "measured"
    if measured:
        return "partial"
    return "not_measured"


def _build_grounding_trace(
    metadata: dict[str, Any],
    *,
    selected_sources: list[dict[str, Any]],
    citation_ranks: list[int],
    citation_valid: bool | None,
) -> dict[str, Any]:
    overall = metadata.get("status", "unknown")
    route = metadata.get("route")
    reason_codes: list[str] = []
    source_identity_status = (
        _source_identity_status(selected_sources)
        if route == "vectordb"
        else "not_applicable"
    )
    if overall != "succeeded":
        status = "not_evaluated"
        reason_codes.append("response_not_succeeded")
    elif route == "vectordb" and metadata.get("no_vector_results"):
        status = "not_applicable"
        reason_codes.append("no_vector_results")
    elif route == "vectordb" and not selected_sources:
        status = "unavailable"
        reason_codes.append("no_context_sources")
    elif (
        route == "vectordb"
        and citation_ranks
        and citation_valid
        and source_identity_status == "measured"
    ):
        status = "linked"
        reason_codes.append("valid_citation_to_selected_source")
    elif route == "vectordb" and citation_ranks and citation_valid:
        status = "partial"
        reason_codes.extend(
            [
                "valid_citation_rank_without_stable_source_identity",
                f"source_identity_{source_identity_status}",
            ]
        )
    elif route == "vectordb":
        status = "partial"
        reason_codes.append(
            "citation_missing" if not citation_ranks else "citation_out_of_range"
        )
    elif route == "rdb":
        rdb = (metadata.get("monitoring") or {}).get("rdb") or {}
        row_count = _nonnegative_int(rdb.get("row_count"))
        if rdb.get("guardrail_blocked"):
            status = "unavailable"
            reason_codes.append("rdb_guardrail_blocked")
        elif row_count == 0:
            status = "not_applicable"
            reason_codes.append("rdb_no_rows")
        elif row_count is not None:
            status = "linked"
            reason_codes.append("rdb_result_available")
        else:
            status = "not_measured"
            reason_codes.append("rdb_evidence_not_measured")
    else:
        status = "not_measured"
        reason_codes.append("route_not_measured")
    return {
        "status": status,
        "reason_codes": reason_codes,
        "semantic_review_status": "not_evaluated",
        "source_identity_status": source_identity_status,
        "citation_valid": citation_valid if selected_sources else None,
    }


def _build_state_status_trace(metadata: dict[str, Any]) -> dict[str, Any]:
    monitoring = metadata.get("monitoring") or {}
    query_rewrite = _metadata_query_rewrite(metadata)
    retrieval = _metadata_retrieval(metadata)
    route = metadata.get("route")
    state_trace = monitoring.get("state_trace") or {}
    state_snapshot = monitoring.get("state_snapshot") or {}
    available_keys = set(state_snapshot.get("available_keys") or [])
    overall = metadata.get("status", "unknown")
    if overall == "succeeded":
        answer_status = "completed"
    elif overall == "failed":
        answer_status = "failed"
    elif overall == "running":
        answer_status = "running"
    else:
        answer_status = "not_measured"
    if route == "vectordb" and metadata.get("no_vector_results"):
        retrieval_status = "no_results"
    elif route == "vectordb" and retrieval:
        retrieval_status = "completed"
    elif route == "rdb" and monitoring.get("rdb"):
        retrieval_status = "completed"
    elif route in {"vectordb", "rdb"}:
        retrieval_status = "not_measured"
    else:
        retrieval_status = "not_applicable"
    return {
        "overall": overall,
        "stages": {
            "input": "completed" if metadata.get("question") else "not_measured",
            "query_rewrite": (
                "completed"
                if query_rewrite.get("rewritten_query") is not None
                or "rewritten_query" in available_keys
                else "not_measured"
            ),
            "search_scope": (
                "completed"
                if bool(metadata.get("search_scope"))
                or bool(metadata.get("scope_source"))
                or bool(metadata.get("scope_decision"))
                or "search_filters" in available_keys
                or bool((state_trace.get("after_search_scope") or {}))
                else "not_measured"
            ),
            "routing": (
                "completed"
                if route in {"vectordb", "rdb"}
                and (
                    state_snapshot.get("route") == route
                    or "route" in available_keys
                    or bool(monitoring.get("routing"))
                    or bool(metadata.get("routing_context"))
                )
                else "not_measured"
            ),
            "retrieval": retrieval_status,
            "answer": answer_status,
        },
        "snapshot": state_snapshot,
    }


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
    selected_sources = _metadata_selected_sources(metadata)
    route = metadata.get("route")
    vector_sources = selected_sources if route == "vectordb" else []
    rdb_sources = selected_sources if route == "rdb" else []
    source_count = (
        len(group_sources_by_document(vector_sources))
        if route == "vectordb"
        else len(rdb_sources)
    )
    answer = str(message.get("content") or "")
    citation_ranks = (
        sorted(extract_citation_ranks(answer, source_count=None))
        if route == "vectordb"
        else []
    )
    citation_valid = (
        _citation_valid(answer, vector_sources) if route == "vectordb" else None
    )
    used_chunks = _build_used_chunk_rows(vector_sources, citation_ranks)
    used_documents = _build_used_document_rows(used_chunks)
    rdb_evidence = _build_rdb_evidence_rows(rdb_sources)
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
        "state_status": _build_state_status_trace(metadata),
        "timing": _build_turn_timing_trace(metadata),
        "retrieval_k": _build_retrieval_k_trace(metadata),
        "grounding": _build_grounding_trace(
            metadata,
            selected_sources=selected_sources,
            citation_ranks=citation_ranks,
            citation_valid=citation_valid,
        ),
        "retrieval": retrieval,
        "sources": used_chunks,
        "used_chunks": used_chunks,
        "used_documents": used_documents,
        "rdb_evidence": rdb_evidence,
        "answer": {
            "assistant_preview": _safe_preview(answer, 500),
            "source_count": source_count,
            "citation_ranks_used": citation_ranks,
            "citation_valid": citation_valid,
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
    retrieval_k = detail.get("retrieval_k") or {}
    state_status = detail.get("state_status") or {}
    grounding = detail.get("grounding") or {}
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
        "source_count": (
            answer.get("source_count")
            if answer.get("source_count") is not None
            else retrieval.get("source_count")
        ),
        "prior_scope_file_count": (detail.get("state_transitions") or {}).get("input", {}).get("prior_search_scope_file_count"),
        "search_scope_file_count": (detail.get("state_transitions") or {}).get("after_search_scope", {}).get("search_scope_file_count"),
        "citation_valid": answer.get("citation_valid"),
        "state_status": state_status.get("overall"),
        "grounding_status": grounding.get("status"),
        "source_identity_status": grounding.get("source_identity_status"),
        "configured_top_k": retrieval_k.get("configured_top_k"),
        "requested_k": retrieval_k.get("requested_k"),
        "fetch_k": retrieval_k.get("fetch_k"),
        "context_count": retrieval_k.get("context_count"),
        "used_chunk_count": len(detail.get("used_chunks") or []),
        "used_document_count": len(detail.get("used_documents") or []),
        "rdb_evidence_count": len(detail.get("rdb_evidence") or []),
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
    current_files = set(_ordered_file_names(_metadata_selected_sources(current_metadata)))
    previous_files = set(_ordered_file_names(_metadata_selected_sources(previous_metadata)))
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
    selected_sources = _metadata_selected_sources(metadata)
    file_names = _ordered_file_names(selected_sources)
    if len(selected_sources) > 1 and len(set(file_names)) <= 1:
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
        if str(message.get("id")) == str(selected_message_id):
            return previous
        if (_message_metadata(message)).get("status") == "succeeded":
            previous = message
    return previous


def user_question_before_message(messages: list[dict[str, Any]], selected_message_id: Any) -> str | None:
    """선택한 assistant 응답 앞의 user 질문을 반환합니다."""
    latest_user: str | None = None
    for message in messages:
        if str(message.get("id")) == str(selected_message_id):
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
            "source_files": _ordered_file_names(_metadata_selected_sources(metadata)),
        },
    }


def build_chat_trace_issue_context(
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    selected_message_id: Any,
) -> dict[str, Any]:
    """선택한 Chat Monitoring trace로 issue report context를 만듭니다."""
    selected = next(
        (
            message
            for message in messages
            if str(message.get("id"))
            == str(selected_message_id)
        ),
        None,
    )
    previous = previous_successful_assistant(messages, selected_message_id)
    selected_question = user_question_before_message(messages, selected_message_id)
    trace_detail = build_message_trace_detail(
        selected or {},
        user_question=selected_question,
    )
    state_input = (
        (trace_detail.get("state_transitions") or {}).get("input")
        or {}
    )
    prior_scope = _normalize_prior_search_scope(
        state_input.get("prior_search_scope")
    )
    followup_intent = bool(
        (trace_detail.get("query_rewrite") or {}).get(
            "followup_scope_intent"
        )
    )
    reproduction_input: dict[str, Any] = {
        "question": str(selected_question or "").strip(),
    }
    if prior_scope:
        reproduction_input["prior_search_scope"] = prior_scope
    if followup_intent:
        reproduction_input["requires_prior_scope"] = True
    return {
        "thread_id": thread.get("id"),
        "thread_name": thread.get("name"),
        "submitted_from": "chat_monitoring_trace",
        "selected_user_question": selected_question,
        "reproduction_input": reproduction_input,
        "selected_message": _compact_trace_message(selected),
        "previous_message": _compact_trace_message(previous),
        "trace_detail": trace_detail,
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
        "schema_version": 2,
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
    run["run_hash"] = compute_evaluation_run_hash(run)
    return _persist_evaluation_run(json_path, run)


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


def _resolve_evaluation_profile(profile: str | None) -> str:
    normalized = _QUALITY_PROFILE_ALIASES.get(
        str(profile or "accuracy_first").strip(),
        str(profile or "accuracy_first").strip(),
    )
    if normalized not in _EVALUATION_PROFILES:
        raise CandidateValidationError(f"invalid evaluation profile: {profile}")
    return normalized


def _resolve_case_active_checks(
    case: dict[str, Any],
    *,
    evaluation_profile: str | None,
) -> list[str]:
    if isinstance(case.get("active_checks"), list):
        active_checks = list(case.get("active_checks"))
        if not active_checks:
            raise CandidateValidationError("active_checks must not be empty")
        return active_checks

    fallback_checks = list(
        _EVALUATION_PROFILES[_resolve_evaluation_profile(evaluation_profile)]
    )
    if isinstance(case.get("checks"), list) and case["checks"]:
        checks = list(case["checks"])
        if not checks:
            return fallback_checks
        unknown = set(checks) - _AUTOMATIC_CHECKS
        if unknown:
            raise CandidateValidationError(
                f"checks contains unsupported values: {sorted(unknown)}"
            )
        return checks
    return fallback_checks


def _filters_match(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return all(actual.get(key) == value for key, value in (expected or {}).items())


def _citation_valid(answer: str, sources: list[dict[str, Any]]) -> bool:
    cited = extract_citation_ranks(answer or "", source_count=None)
    if not cited:
        return len(sources) == 0
    available_ranks: set[int] = set()
    for fallback_rank, source in enumerate(sources, 1):
        try:
            rank = int((source or {}).get("rank", fallback_rank))
        except (TypeError, ValueError):
            rank = fallback_rank
        if rank > 0:
            available_ranks.add(rank)
    return cited.issubset(available_ranks)


def evaluate_dataset_case_result(
    case: dict[str, Any],
    final_state: dict[str, Any],
    *,
    latency_seconds: float,
    latency_threshold_seconds: float = 30.0,
    evaluation_profile: str | None = None,
) -> dict[str, Any]:
    """fixed evaluation case를 graph output 기준으로 채점합니다."""
    sources = final_state.get("rerank_info") or final_state.get("rdb_sources") or []
    route_pass = final_state.get("route") == case.get("expected_route")
    filter_pass = _filters_match(case.get("expected_filters") or {}, final_state.get("search_filters") or {})
    source_hit, hit_at_k = _expected_source_hit(case.get("expected_sources") or [], sources)
    citation_valid = _citation_valid(str(final_state.get("generation") or ""), sources)
    latency_pass = latency_seconds <= latency_threshold_seconds
    no_result = bool(final_state.get("no_vector_results"))
    no_result_absent = not no_result
    expected_state = case.get("expected_state") or {}
    expected_state_pass = _expected_state_matches(expected_state, final_state)[0]
    try:
        answer_requirement_evaluation = evaluate_answer_requirements(
            case.get("expected_answer_requirements"),
            answer=str(final_state.get("generation") or ""),
            sources=[
                source for source in sources if isinstance(source, Mapping)
            ],
        )
    except AnswerRequirementValidationError as exc:
        raise CandidateValidationError(
            f"expected_answer_requirements is invalid: {exc}"
        ) from exc
    answer_requirements_pass = answer_requirement_evaluation["passed"]
    check_results = {
        "answer_requirements_pass": answer_requirements_pass,
        "route_pass": route_pass,
        "filter_pass": filter_pass,
        "source_hit": source_hit,
        "citation_valid": citation_valid,
        "latency_pass": latency_pass,
        "no_result_absent": no_result_absent,
        "expected_state_pass": expected_state_pass,
    }
    active_checks = _resolve_case_active_checks(
        case,
        evaluation_profile=evaluation_profile,
    )
    if "active_checks" in case:
        unknown = set(active_checks) - _AUTOMATIC_CHECKS
        if unknown:
            raise CandidateValidationError(
                f"unsupported active checks: {sorted(unknown)}"
            )
        prerequisites = {
            "answer_requirements_pass": case.get(
                "expected_answer_requirements"
            ),
            "route_pass": case.get("expected_route"),
            "filter_pass": case.get("expected_filters"),
            "source_hit": case.get("expected_sources"),
            "expected_state_pass": case.get("expected_state"),
        }
        missing = [
            check
            for check, expected in prerequisites.items()
            if check in active_checks and not expected
        ]
        if missing:
            raise CandidateValidationError(
                f"active checks require expected values: {missing}"
            )
    failed_checks = [
        check for check in active_checks if check_results.get(check) is not True
    ]
    correctness_checks = [
        check for check in active_checks if check in _CORRECTNESS_CHECKS
    ]
    accuracy_pass = (
        all(check_results.get(check) is True for check in correctness_checks)
        if correctness_checks
        else None
    )
    passed = not failed_checks
    return {
        "case_id": case.get("id"),
        "question": case.get("question"),
        "status": "pass" if passed else "fail",
        "active_checks": active_checks,
        "check_results": check_results,
        "failed_checks": failed_checks,
        "accuracy_pass": accuracy_pass,
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
        "no_result_absent": no_result_absent,
        "expected_state_pass": expected_state_pass,
        "answer_requirements_pass": answer_requirements_pass,
        "answer_requirement_results": answer_requirement_evaluation[
            "results"
        ],
        "actual_filters": final_state.get("search_filters") or {},
        "source_count": len(sources),
    }


def _summarize_correctness_results(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    unavailable = {
        "accuracy_rate": None,
        "passed": 0,
        "case_count": 0,
        "measured": False,
    }
    outcomes: list[bool] = []
    for result in results:
        if not isinstance(result, Mapping):
            continue
        accuracy_checks = [
            str(check)
            for check in result.get("active_checks") or []
            if str(check) in _CORRECTNESS_CHECKS
        ]
        if not accuracy_checks:
            continue
        stored_outcome = result.get("accuracy_pass")
        if isinstance(stored_outcome, bool):
            outcomes.append(stored_outcome)
            continue
        check_results = result.get("check_results")
        check_results = (
            check_results if isinstance(check_results, Mapping) else result
        )
        outcomes.append(
            all(check_results.get(check) is True for check in accuracy_checks)
        )

    if not outcomes:
        return unavailable
    passed = sum(outcomes)
    return {
        "accuracy_rate": passed / len(outcomes),
        "passed": passed,
        "case_count": len(outcomes),
        "measured": True,
    }


def is_native_v2_evaluation_data_source(value: Any) -> bool:
    """Return whether run provenance identifies one successor V2 revision."""

    if not isinstance(value, Mapping):
        return False
    if not _NATIVE_V2_DATA_SOURCE_FIELDS.issubset(value):
        return False
    generation = value.get("publication_generation")
    write_epoch = value.get("write_epoch")
    return (
        value.get("backend_mode") == "native_v2"
        and value.get("runtime_mode") == "native"
        and isinstance(value.get("snapshot_id"), str)
        and bool(value.get("snapshot_id"))
        and isinstance(value.get("build_id"), str)
        and bool(value.get("build_id"))
        and isinstance(value.get("profile_hash"), str)
        and bool(value.get("profile_hash"))
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation > 0
        and isinstance(write_epoch, int)
        and not isinstance(write_epoch, bool)
        and write_epoch > 0
        and value.get("degraded") is False
    )


def build_native_v2_evaluation_data_source(
    status: Mapping[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    """Pin authoritative successor-V2 identity for an evaluation run."""

    retrieval = status.get("retrieval")
    if not isinstance(retrieval, Mapping):
        raise CandidateValidationError("Native V2 runtime status is unavailable")
    source = {
        "backend_mode": "native_v2",
        "runtime_mode": retrieval.get("mode"),
        "snapshot_id": retrieval.get("active_snapshot_id"),
        "build_id": retrieval.get("active_build_id"),
        "profile_hash": retrieval.get("profile_hash"),
        "publication_generation": retrieval.get("publication_generation"),
        "write_epoch": retrieval.get("write_epoch"),
        "degraded": retrieval.get("degraded"),
        **extra,
    }
    if not is_native_v2_evaluation_data_source(source):
        raise CandidateValidationError(
            "evaluation requires an active successor Native V2 revision"
        )
    return source


def _native_v2_result_matches_data_source(
    final_state: Mapping[str, Any],
    data_source: Mapping[str, Any],
) -> bool:
    monitoring_metrics = final_state.get("monitoring_metrics")
    retrieval = (
        monitoring_metrics.get("retrieval")
        if isinstance(monitoring_metrics, Mapping)
        else None
    )
    if not isinstance(retrieval, Mapping):
        return False
    generation = retrieval.get("publication_generation")
    return (
        retrieval.get("runtime_mode") == "native"
        and retrieval.get("snapshot_id") == data_source.get("snapshot_id")
        and generation == data_source.get("publication_generation")
    )


def is_verified_native_v2_evaluation_run(run: Any) -> bool:
    """Verify hash, mode, and authoritative Native V2 provenance together."""

    if not isinstance(run, Mapping):
        return False
    if (
        run.get("schema_version") != 2
        or run.get("execution_mode") != "native_v2"
        or run.get("integrity_status") != "valid"
        or not is_native_v2_evaluation_data_source(run.get("data_source"))
    ):
        return False
    try:
        return run.get("run_hash") == compute_evaluation_run_hash(run)
    except CandidateValidationError:
        return False


def summarize_evaluation_accuracy(
    run: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Summarize correctness only from an attested successor-V2 run."""

    unavailable = {
        "accuracy_rate": None,
        "passed": 0,
        "case_count": 0,
        "measured": False,
    }
    if not is_verified_native_v2_evaluation_run(run):
        return unavailable
    assert isinstance(run, Mapping)
    results = run.get("results")
    if not isinstance(results, Sequence):
        return unavailable
    return _summarize_correctness_results(results)


def _summarize_eval_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    case_count = len(results)
    passed = sum(1 for result in results if result.get("status") == "pass")
    latencies = [float(result["latency_seconds"]) for result in results if isinstance(result.get("latency_seconds"), (int, float))]
    summary = {
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
    accuracy = _summarize_correctness_results(results)
    summary.update(
        {
            "accuracy_rate": accuracy["accuracy_rate"],
            "accuracy_passed": accuracy["passed"],
            "accuracy_case_count": accuracy["case_count"],
        }
    )
    return summary



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
    evaluation_profile: str | None = None,
    execution_mode: str = "current_data",
    data_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """고정 dataset을 graph로 실행하고 JSON experiment run을 저장합니다."""
    requires_native_v2 = execution_mode == "native_v2" or execution_mode.endswith(
        "_native_v2"
    )
    if requires_native_v2 and not is_native_v2_evaluation_data_source(data_source):
        raise CandidateValidationError(
            "Native V2 evaluation provenance is missing or unavailable"
        )
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
                evaluation_profile=evaluation_profile,
            )
        )
        if requires_native_v2 and not _native_v2_result_matches_data_source(
            final_state,
            data_source or {},
        ):
            raise CandidateValidationError(
                "evaluation result does not match the pinned Native V2 revision"
            )
    run = {
        "schema_version": 2,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset_name": dataset.get("name"),
        "dataset_version": dataset.get("version"),
        "evaluation_profile": _resolve_evaluation_profile(evaluation_profile),
        "execution_mode": execution_mode,
        "data_source": data_source or {},
        "selected_case_ids": [case.get("id") for case in cases],
        "summary": _summarize_eval_results(results),
        "results": results,
    }
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    json_path = output_path / f"evaluation_run_{run_id}.json"
    run["run_hash"] = compute_evaluation_run_hash(run)
    return _persist_evaluation_run(json_path, run)


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


def summarize_incident_metric(
    *,
    incident_count: int,
    sample_count: int,
    automatic_decision_min_samples: int = 20,
) -> dict[str, Any]:
    """Keep every incident actionable while separating low-sample statistics."""

    if (
        isinstance(incident_count, bool)
        or isinstance(sample_count, bool)
        or incident_count < 0
        or sample_count < 0
        or incident_count > sample_count
        or automatic_decision_min_samples < 1
    ):
        raise ValueError("incident metric counts are invalid")
    low_sample = sample_count < automatic_decision_min_samples
    return {
        "incident_count": incident_count,
        "sample_count": sample_count,
        "rate": (
            incident_count / sample_count
            if sample_count
            else None
        ),
        "low_sample": low_sample,
        "improvement_eligible": incident_count > 0,
        "automatic_decision_allowed": not low_sample,
        "policy": (
            "use every incident for triage; do not auto-block from "
            "a low-sample aggregate alone"
        ),
    }


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
                "native_v2_provenance": _message_has_native_v2_provenance(
                    message
                ),
            }
            assistant_rows.append(row)
            if row["status"] == "failed":
                recent_failures.append(row)
    statuses = Counter(row["status"] for row in assistant_rows)
    routes = Counter(row["route"] for row in assistant_rows if row["status"] == "succeeded" and row.get("route"))
    latencies = sorted(
        float(row["latency_seconds"])
        for row in assistant_rows
        if (
            row.get("native_v2_provenance") is True
            and isinstance(row.get("latency_seconds"), (int, float))
        )
    )
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
        "latency_sample_count": len(latencies),
        "failure_evidence": summarize_incident_metric(
            incident_count=statuses.get("failed", 0),
            sample_count=total,
        ),
        "no_result_evidence": summarize_incident_metric(
            incident_count=no_result_count,
            sample_count=succeeded_count,
        ),
        "recent_failures": sorted(recent_failures, key=lambda row: str(row.get("created_at") or ""), reverse=True)[:10],
    }


def summarize_issue_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "report_count": len(reports),
        "categories": dict(Counter(report.get("category") or "unknown" for report in reports)),
        "thread_count": len({report.get("thread_id") for report in reports if report.get("thread_id")}),
    }


def _conversation_draft_inputs(report: dict[str, Any]) -> dict[str, Any] | None:
    messages = (report.get("context") or {}).get("conversation_messages") or []
    last_user: dict[str, Any] | None = None
    selected_user: dict[str, Any] | None = None
    selected_assistant: dict[str, Any] | None = None
    for message in messages:
        role = message.get("role")
        if role == "user":
            last_user = message
        elif role == "assistant" and last_user:
            selected_user = last_user
            selected_assistant = message
    if not selected_user or not selected_assistant:
        return None
    metadata = selected_assistant.get("metadata") or {}
    route = metadata.get("route")
    search_scope = metadata.get("search_scope") or {}
    filters = search_scope.get("search_filters") or metadata.get("search_filters") or {}
    question = str(selected_user.get("content") or "").strip()
    if not question or not route:
        return None
    sources = _metadata_selected_sources(metadata)
    if not sources and search_scope.get("file_names"):
        sources = [{"file_name": file_name} for file_name in search_scope.get("file_names") or []]
    return {"question": question, "route": route, "filters": filters, "sources": sources}


def _observed_draft_inputs(
    report: Mapping[str, Any],
) -> dict[str, Any] | None:
    observed = report.get("observed")
    if not isinstance(observed, Mapping):
        return None
    actual = observed.get("actual")
    if not isinstance(actual, Mapping):
        return None
    reproduction_input = observed.get("reproduction_input")
    if not isinstance(reproduction_input, Mapping):
        reproduction_input = {}
    question = str(
        observed.get("user_question")
        or reproduction_input.get("question")
        or ""
    ).strip()
    route = actual.get("route")
    if not question or route not in {"vectordb", "rdb"}:
        return None
    filters = actual.get("filters")
    sources = actual.get("sources")
    state = actual.get("state")
    return {
        "question": question,
        "route": route,
        "filters": dict(filters) if isinstance(filters, Mapping) else {},
        "sources": [
            dict(source)
            for source in sources or []
            if isinstance(source, Mapping)
        ]
        if isinstance(sources, list)
        else [],
        "state": dict(state) if isinstance(state, Mapping) else {},
    }


def classify_issue_report_draft_readiness(report: dict[str, Any]) -> dict[str, str]:
    """Describe whether an issue report can become an executable regression draft."""
    context = report.get("context") or {}
    if context.get("trace_detail"):
        return {"status": "trace_ready", "recommended_next_step": "Promote to regression candidate"}
    if _observed_draft_inputs(report):
        return {
            "status": "observed_ready",
            "recommended_next_step": "Promote to regression candidate",
        }
    if _conversation_draft_inputs(report):
        return {"status": "conversation_ready", "recommended_next_step": "Promote to regression candidate"}
    if report.get("content"):
        return {"status": "raw_text_only", "recommended_next_step": "Manual eval case review needed"}
    return {"status": "needs_manual_review", "recommended_next_step": "Manual eval case review needed"}


def build_issue_report_rows(reports: list[dict[str, Any]], *, thread_names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    thread_names = thread_names or {}
    rows: list[dict[str, Any]] = []
    for report in reports:
        readiness = classify_issue_report_draft_readiness(report)
        rows.append({
            "created_at": report.get("created_at"),
            "id": report.get("id"),
            "category": report.get("category"),
            "source": report.get("source") or "local_chat",
            "app_version": report.get("app_version"),
            "draft_readiness": readiness["status"],
            "recommended_next_step": readiness["recommended_next_step"],
            "thread_id": str(report.get("thread_id") or ""),
            "thread_name": thread_names.get(str(report.get("thread_id") or ""), "-"),
            "file_path": report.get("file_path"),
            "preview": _issue_report_preview(report.get("content") or ""),
        })
    return rows


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
        observed_inputs = _observed_draft_inputs(report)
        conversation_inputs = (
            None
            if observed_inputs is not None
            else _conversation_draft_inputs(report)
        )
        draft_inputs = observed_inputs or conversation_inputs
        if not draft_inputs:
            return None
        route = draft_inputs["route"]
        impact_area = infer_issue_impact_area(report)
        expected_state = dict(draft_inputs.get("state") or {})
        if conversation_inputs is not None:
            expected_state.setdefault(
                "draft_source",
                "conversation_messages",
            )
        return {
            "id": f"issue_{report.get('id') or uuid.uuid4().hex[:8]}",
            "type": "vectordb_retrieval" if route == "vectordb" else "rdb_aggregate",
            "question": draft_inputs["question"],
            "expected_route": route,
            "expected_filters": draft_inputs["filters"],
            "expected_sources": _draft_expected_sources(draft_inputs["sources"]),
            "expected_state": expected_state,
            "criteria_tags": ["issue_report_regression", "email_import"],
            "monitoring_dimensions": list(dict.fromkeys([impact_area, "routing", "filter", "retrieval" if route == "vectordb" else "rdb"])),
            "checks": ["route_pass", "filter_pass", "source_hit", "citation_valid"],
            "source_issue_report_id": report.get("id"),
            "review_required": True,
        }
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


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_expected(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, Mapping):
        raise CandidateValidationError("expected must be an object")
    allowed = {
        "route",
        "filters",
        "sources",
        "state",
        "manual_assertions",
        "answer_requirements",
    }
    unknown = set(value) - allowed
    if unknown:
        raise CandidateValidationError(f"unsupported expected fields: {sorted(unknown)}")
    filters = value.get("filters", {})
    sources = value.get("sources", [])
    state = value.get("state", {})
    assertions = value.get("manual_assertions", [])
    raw_answer_requirements = value.get("answer_requirements", [])
    filters = {} if filters is None else filters
    sources = [] if sources is None else sources
    state = {} if state is None else state
    assertions = [] if assertions is None else assertions
    try:
        answer_requirements = canonicalize_answer_requirements(
            raw_answer_requirements
        )
    except AnswerRequirementValidationError as exc:
        raise CandidateValidationError(
            f"expected.answer_requirements is invalid: {exc}"
        ) from exc
    if not isinstance(filters, Mapping):
        raise CandidateValidationError("expected.filters must be an object")
    if not isinstance(sources, list) or any(
        not isinstance(source, Mapping) for source in sources
    ):
        raise CandidateValidationError("expected.sources must be a list of objects")
    if not isinstance(state, Mapping):
        raise CandidateValidationError("expected.state must be an object")
    if not isinstance(assertions, list) or any(
        not isinstance(assertion, Mapping) for assertion in assertions
    ):
        raise CandidateValidationError(
            "expected.manual_assertions must be a list of objects"
        )

    allowed_filters = {
        "target_name",
        "report_type",
        "report_types",
        "report_date",
        "report_date_start",
        "report_date_end",
        "broker",
        "file_names",
    }
    allowed_source = {"file_name", "report_type"}
    allowed_state = {
        "followup_scope_intent",
        "scope_source",
        "scope_decision_reason",
        "scope_decision_matched_section_id",
    }
    if set(filters) - allowed_filters:
        raise CandidateValidationError("expected.filters contains unsupported keys")
    if any(set(source) - allowed_source for source in sources):
        raise CandidateValidationError("expected.sources contains unsupported keys")
    if set(state) - allowed_state:
        raise CandidateValidationError("expected.state contains unsupported keys")
    for key, item in filters.items():
        if key == "file_names":
            if not isinstance(item, list) or any(
                not isinstance(file_name, str) for file_name in item
            ):
                raise CandidateValidationError(
                    "expected.filters.file_names is invalid"
                )
        elif item is not None and not isinstance(item, str):
            raise CandidateValidationError(
                f"expected.filters.{key} is invalid"
            )
    for source in sources:
        if any(
            item is not None and not isinstance(item, str)
            for item in source.values()
        ):
            raise CandidateValidationError(
                "expected.sources value is invalid"
            )
    for key, item in state.items():
        if item is not None and not isinstance(item, str):
            raise CandidateValidationError(
                f"expected.state.{key} is invalid"
            )

    normalized_assertions: list[dict[str, Any]] = []
    assertion_ids: set[str] = set()
    for assertion in assertions:
        if set(assertion) - {"id", "text"}:
            raise CandidateValidationError(
                "expected.manual_assertions contains unsupported keys"
            )
        assertion_id = str(assertion.get("id") or "")
        if not is_safe_artifact_identifier(assertion_id):
            raise CandidateValidationError("manual assertion id is unsafe")
        if assertion_id in assertion_ids:
            raise CandidateValidationError(
                "manual assertion ids must be unique"
            )
        assertion_ids.add(assertion_id)
        text = str(assertion.get("text") or "").strip()
        if not text:
            raise CandidateValidationError("manual assertion text is required")
        normalized_assertions.append({"id": assertion_id, "text": text})

    route = value.get("route")
    if route not in (None, "vectordb", "rdb"):
        raise CandidateValidationError("expected.route is invalid")
    result = {
        "route": route,
        "filters": dict(filters),
        "sources": [dict(source) for source in sources],
        "state": dict(state),
        "manual_assertions": normalized_assertions,
    }
    if answer_requirements:
        result["answer_requirements"] = answer_requirements
    return result


def _canonical_quality_profile(value: Any) -> str:
    profile = str(value or "balanced").strip()
    profile = _QUALITY_PROFILE_ALIASES.get(profile, profile)
    if profile not in QUALITY_PROFILE_RULES:
        raise CandidateValidationError("quality_profile is invalid")
    return profile


def _canonical_performance_budget(
    value: Any,
    *,
    quality_profile: str,
) -> dict[str, Any]:
    defaults = dict(
        QUALITY_PROFILE_RULES[quality_profile][
            "default_performance_budget"
        ]
    )
    if value in (None, {}):
        return defaults
    if not isinstance(value, Mapping):
        raise CandidateValidationError(
            "performance_budget must be an object"
        )
    allowed = {
        "max_p95_seconds",
        "min_runs",
        "warmup_runs",
        "enforcement",
    }
    unknown = set(value) - allowed
    if unknown:
        raise CandidateValidationError(
            f"unsupported performance budget fields: {sorted(unknown)}"
        )
    budget = {**defaults, **dict(value)}
    max_p95 = budget.get("max_p95_seconds")
    min_runs = budget.get("min_runs")
    warmup_runs = budget.get("warmup_runs")
    enforcement = budget.get("enforcement")
    if (
        isinstance(max_p95, bool)
        or not isinstance(max_p95, (int, float))
        or not 0 < float(max_p95) <= 600
    ):
        raise CandidateValidationError(
            "performance_budget.max_p95_seconds is invalid"
        )
    if (
        isinstance(min_runs, bool)
        or not isinstance(min_runs, int)
        or not 1 <= min_runs <= 50
    ):
        raise CandidateValidationError(
            "performance_budget.min_runs is invalid"
        )
    if (
        isinstance(warmup_runs, bool)
        or not isinstance(warmup_runs, int)
        or not 0 <= warmup_runs < min_runs
    ):
        raise CandidateValidationError(
            "performance_budget.warmup_runs is invalid"
        )
    if enforcement not in {"hard", "soft"}:
        raise CandidateValidationError(
            "performance_budget.enforcement is invalid"
        )
    if quality_profile == "speed_first" and enforcement != "hard":
        raise CandidateValidationError(
            "speed_first requires a hard performance budget"
        )
    return {
        "max_p95_seconds": float(max_p95),
        "min_runs": min_runs,
        "warmup_runs": warmup_runs,
        "enforcement": enforcement,
    }


def _canonical_validation_plan(
    value: Any,
    *,
    active_checks: Sequence[str] | None,
    verification_type: str | None,
    quality_profile: Any = None,
) -> dict[str, Any]:
    plan_value = value if isinstance(value, Mapping) else {}
    if value not in (None, {}) and not isinstance(value, Mapping):
        raise CandidateValidationError("validation_plan must be an object")
    allowed = {
        "schema_version",
        "quality_profile",
        "hard_checks",
        "soft_objectives",
        "performance_budget",
    }
    unknown = set(plan_value) - allowed
    if unknown:
        raise CandidateValidationError(
            f"unsupported validation plan fields: {sorted(unknown)}"
        )
    if plan_value.get("schema_version", 1) != 1:
        raise CandidateValidationError(
            "validation_plan schema_version is invalid"
        )
    profile = _canonical_quality_profile(
        plan_value.get("quality_profile", quality_profile)
    )
    hard_checks_value = plan_value.get("hard_checks", active_checks or [])
    if not isinstance(hard_checks_value, (list, tuple)) or any(
        not isinstance(check, str) for check in hard_checks_value
    ):
        raise CandidateValidationError(
            "validation_plan.hard_checks must be a string list"
        )
    hard_checks = list(hard_checks_value)
    if len(set(hard_checks)) != len(hard_checks):
        raise CandidateValidationError(
            "validation_plan.hard_checks must be unique"
        )
    unknown_checks = set(hard_checks) - _ALL_HARD_CHECKS
    if unknown_checks:
        raise CandidateValidationError(
            f"unsupported hard checks: {sorted(unknown_checks)}"
        )

    soft_value = plan_value.get(
        "soft_objectives",
        QUALITY_PROFILE_RULES[profile]["default_soft_objectives"],
    )
    if not isinstance(soft_value, (list, tuple)) or any(
        not isinstance(item, str) for item in soft_value
    ):
        raise CandidateValidationError(
            "validation_plan.soft_objectives must be a string list"
        )
    soft_objectives = list(soft_value)
    if len(set(soft_objectives)) != len(soft_objectives):
        raise CandidateValidationError(
            "validation_plan.soft_objectives must be unique"
        )
    unknown_objectives = set(soft_objectives) - _SOFT_OBJECTIVES
    if unknown_objectives:
        raise CandidateValidationError(
            f"unsupported soft objectives: {sorted(unknown_objectives)}"
        )
    budget = _canonical_performance_budget(
        plan_value.get("performance_budget"),
        quality_profile=profile,
    )
    if budget["enforcement"] == "hard":
        if hard_checks and _PERFORMANCE_CHECK not in hard_checks:
            raise CandidateValidationError(
                "hard performance budget requires performance_p95_pass"
            )
    elif _PERFORMANCE_CHECK in hard_checks:
        raise CandidateValidationError(
            "performance_p95_pass requires a hard performance budget"
        )
    if budget["enforcement"] == "soft" and "latency_p95" not in soft_objectives:
        soft_objectives.append("latency_p95")

    preferred_type = str(verification_type or "graph_contract")
    has_automatic = bool(set(hard_checks) & _AUTOMATIC_CHECKS)
    has_manual = _MANUAL_CHECK in hard_checks
    has_performance = _PERFORMANCE_CHECK in hard_checks
    if has_performance and not has_automatic:
        raise CandidateValidationError(
            "performance validation requires an automatic graph check"
        )
    if has_automatic and has_manual:
        normalized_type = "mixed"
    elif has_manual:
        normalized_type = (
            preferred_type
            if preferred_type in {"manual_answer_quality", "manual_ui"}
            else "manual_answer_quality"
        )
    elif has_automatic or has_performance:
        normalized_type = "graph_contract"
    else:
        normalized_type = preferred_type
    return {
        "schema_version": 1,
        "quality_profile": profile,
        "hard_checks": hard_checks,
        "soft_objectives": soft_objectives,
        "performance_budget": budget,
        "verification_type": normalized_type,
    }


def build_validation_plan(
    quality_profile: str,
    *,
    hard_checks: Sequence[str],
    soft_objectives: Sequence[str] | None = None,
    performance_budget: Mapping[str, Any] | None = None,
    verification_type: str | None = None,
) -> dict[str, Any]:
    """Build and validate one profile-aware candidate verification plan."""

    value: dict[str, Any] = {
        "schema_version": 1,
        "quality_profile": quality_profile,
        "hard_checks": list(hard_checks),
    }
    if soft_objectives is not None:
        value["soft_objectives"] = list(soft_objectives)
    if performance_budget is not None:
        value["performance_budget"] = dict(performance_budget)
    plan = _canonical_validation_plan(
        value,
        active_checks=hard_checks,
        verification_type=verification_type,
        quality_profile=quality_profile,
    )
    return {
        key: plan[key]
        for key in (
            "schema_version",
            "quality_profile",
            "hard_checks",
            "soft_objectives",
            "performance_budget",
        )
    }


def _candidate_validation_plan(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    if int(candidate.get("contract_schema_version") or 1) < 2:
        profile = _canonical_quality_profile(
            candidate.get("quality_profile")
        )
        hard_checks = list(candidate.get("active_checks") or [])
        return {
            "schema_version": 1,
            "quality_profile": profile,
            "hard_checks": hard_checks,
            "soft_objectives": list(
                QUALITY_PROFILE_RULES[profile][
                    "default_soft_objectives"
                ]
            ),
            "performance_budget": _canonical_performance_budget(
                None,
                quality_profile=profile,
            ),
            "verification_type": str(
                candidate.get("verification_type") or "graph_contract"
            ),
        }
    return _canonical_validation_plan(
        candidate.get("validation_plan"),
        active_checks=list(candidate.get("active_checks") or []),
        verification_type=str(
            candidate.get("verification_type") or "graph_contract"
        ),
        quality_profile=candidate.get("quality_profile"),
    )


def _candidate_automatic_checks(
    candidate: Mapping[str, Any],
) -> list[str]:
    return [
        check
        for check in _candidate_validation_plan(candidate)["hard_checks"]
        if check in _AUTOMATIC_CHECKS
    ]


def _candidate_requires_manual(candidate: Mapping[str, Any]) -> bool:
    return (
        _MANUAL_CHECK
        in _candidate_validation_plan(candidate)["hard_checks"]
    )


def compute_candidate_hash(candidate: Mapping[str, Any]) -> str:
    """Hash only fields that define the current reproduction contract."""
    observed = candidate.get("observed")
    if not isinstance(observed, Mapping):
        observed = {}
    payload = {
        "id": candidate.get("id"),
        "contract_revision": int(candidate.get("contract_revision") or 0),
        "reproduction_input": observed.get("reproduction_input") or {},
        "expected": candidate.get("expected") or {},
        "active_checks": list(candidate.get("active_checks") or []),
        "verification_type": candidate.get("verification_type"),
    }
    contract_schema_version = _candidate_revision(
        candidate.get("contract_schema_version", 1),
        "contract_schema_version",
    )
    if contract_schema_version >= 2:
        plan = _candidate_validation_plan(candidate)
        payload.update(
            {
                "contract_schema_version": contract_schema_version,
                "validation_plan": {
                    key: plan[key]
                    for key in (
                        "schema_version",
                        "quality_profile",
                        "hard_checks",
                        "soft_objectives",
                        "performance_budget",
                    )
                },
                "reproduction_manifest": candidate.get(
                    "reproduction_manifest"
                )
                or {},
            }
        )
    try:
        return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(
            "candidate contains a non-finite or unsupported JSON value"
        ) from exc


def _candidate_revision(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CandidateValidationError(f"{field} must be a non-negative integer")
    try:
        revision = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(
            f"{field} must be a non-negative integer"
        ) from exc
    if revision < 0:
        raise CandidateValidationError(f"{field} must be a non-negative integer")
    return revision


def canonicalize_regression_candidate(
    payload: Mapping[str, Any],
    *,
    json_path: str | None = None,
) -> dict[str, Any]:
    """Return a schema-v2 candidate view while preserving unknown top-level fields."""
    candidate = dict(payload)
    candidate_id = str(candidate.get("id") or "").strip()
    if not candidate_id:
        raise CandidateValidationError("candidate id is missing")

    source_refs_value = candidate.get("source_refs")
    if isinstance(source_refs_value, list):
        source_refs = [
            dict(ref) for ref in source_refs_value if isinstance(ref, Mapping)
        ]
    elif candidate.get("source_report_id"):
        source_refs = [
            {"kind": "report", "id": str(candidate.get("source_report_id"))}
        ]
    else:
        source_refs = []

    observed_value = candidate.get("observed")
    observed = dict(observed_value) if isinstance(observed_value, Mapping) else {}
    legacy_draft = candidate.get("eval_case_draft")
    if isinstance(legacy_draft, Mapping):
        observed.setdefault("legacy_eval_draft", dict(legacy_draft))
        observed["reproduction_input"] = _normalize_reproduction_input(
            observed.get("reproduction_input")
        )
        observed["reproduction_input"].setdefault(
            "question",
            str(legacy_draft.get("question") or ""),
        )
        observed.setdefault(
            "actual",
            {
                "route": legacy_draft.get("expected_route"),
                "filters": dict(legacy_draft.get("expected_filters") or {}),
                "sources": [
                    dict(source)
                    for source in legacy_draft.get("expected_sources") or []
                    if isinstance(source, Mapping)
                ],
                "state": dict(legacy_draft.get("expected_state") or {}),
            },
        )
    observed["reproduction_input"] = _normalize_reproduction_input(
        observed.get("reproduction_input")
    )
    observed.setdefault("actual", {})

    evidence_value = candidate.get("evidence")
    evidence = dict(evidence_value) if isinstance(evidence_value, Mapping) else {}
    for key in (
        "baseline_runs",
        "verification_runs",
        "manual_reproductions",
        "manual_verifications",
    ):
        evidence[key] = [
            dict(item)
            for item in evidence.get(key) or []
            if isinstance(item, Mapping)
        ]

    contract_schema_version = _candidate_revision(
        candidate.get("contract_schema_version", 1),
        "contract_schema_version",
    )
    if contract_schema_version not in {1, 2}:
        raise CandidateValidationError(
            "contract_schema_version is invalid"
        )
    raw_verification_type = (
        candidate.get("verification_type") or "graph_contract"
    )
    if contract_schema_version >= 2:
        validation_plan = _canonical_validation_plan(
            candidate.get("validation_plan"),
            active_checks=list(candidate.get("active_checks") or []),
            verification_type=str(raw_verification_type),
            quality_profile=candidate.get("quality_profile"),
        )
    else:
        profile = _canonical_quality_profile(
            candidate.get("quality_profile")
        )
        validation_plan = {
            "schema_version": 1,
            "quality_profile": profile,
            "hard_checks": list(candidate.get("active_checks") or []),
            "soft_objectives": list(
                QUALITY_PROFILE_RULES[profile][
                    "default_soft_objectives"
                ]
            ),
            "performance_budget": _canonical_performance_budget(
                None,
                quality_profile=profile,
            ),
            "verification_type": str(raw_verification_type),
        }
    stored_validation_plan = {
        key: validation_plan[key]
        for key in (
            "schema_version",
            "quality_profile",
            "hard_checks",
            "soft_objectives",
            "performance_budget",
        )
    }
    reproduction_manifest: dict[str, Any] | None = None
    if contract_schema_version >= 2:
        try:
            reproduction_manifest = (
                reproduction_manifest_module.canonicalize_reproduction_manifest(
                    candidate.get("reproduction_manifest")
                )
            )
        except reproduction_manifest_module.ReproductionManifestError as exc:
            raise CandidateValidationError(str(exc)) from exc

    created_at = str(candidate.get("created_at") or _utc_iso_now())
    candidate.update(
        {
            "schema_version": 2,
            "contract_schema_version": contract_schema_version,
            "id": candidate_id,
            "source_refs": source_refs,
            "source_snapshots": [
                dict(item)
                for item in candidate.get("source_snapshots") or []
                if isinstance(item, Mapping)
            ],
            "created_at": created_at,
            "updated_at": str(candidate.get("updated_at") or created_at),
            "record_revision": _candidate_revision(
                candidate.get("record_revision"),
                "record_revision",
            ),
            "contract_revision": _candidate_revision(
                candidate.get("contract_revision"),
                "contract_revision",
            ),
            "triage_status": candidate.get("triage_status") or "new",
            "severity": candidate.get("severity") or "untriaged",
            "impact_area": candidate.get("impact_area"),
            "impact_summary": str(candidate.get("impact_summary") or ""),
            "operator_decision": candidate.get("operator_decision") or "unreviewed",
            "observed": observed,
            # A legacy draft is observed behavior, never an approved expectation.
            "expected": _canonical_expected(candidate.get("expected")),
            "expected_approved_at": candidate.get("expected_approved_at"),
            "expected_approved_by": candidate.get("expected_approved_by"),
            "quality_profile": validation_plan["quality_profile"],
            "validation_plan": stored_validation_plan,
            "reproduction_manifest": reproduction_manifest,
            "active_checks": list(validation_plan["hard_checks"]),
            "verification_type": (
                validation_plan["verification_type"]
                if contract_schema_version >= 2
                else raw_verification_type
            ),
            "evidence": evidence,
            "handoffs": [
                dict(item)
                for item in candidate.get("handoffs") or []
                if isinstance(item, Mapping)
            ],
            "fixed_in_version": candidate.get("fixed_in_version"),
            "closure_reason": candidate.get("closure_reason"),
            "duplicate_of": candidate.get("duplicate_of"),
            "suite_case_id": candidate.get("suite_case_id"),
            "suite_exclusion_reason": candidate.get("suite_exclusion_reason"),
            "history": [
                dict(item)
                for item in candidate.get("history") or []
                if isinstance(item, Mapping)
            ],
        }
    )
    try:
        _canonical_json_bytes(candidate)
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(
            "candidate contains a non-finite or unsupported JSON value"
        ) from exc
    candidate["candidate_hash"] = compute_candidate_hash(candidate)
    if json_path is not None:
        candidate["json_path"] = json_path
    return candidate


def _candidate_storage_payload(candidate: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(candidate)
    for key in ("json_path", "integrity_status", "warnings"):
        payload.pop(key, None)
    if int(payload.get("contract_schema_version") or 1) < 2:
        for key in (
            "contract_schema_version",
            "quality_profile",
            "validation_plan",
            "reproduction_manifest",
        ):
            payload.pop(key, None)
    return payload


def load_regression_candidate(path: str | Path) -> dict[str, Any]:
    """Load and verify a persisted candidate."""
    candidate_path = Path(path)
    try:
        payload = strict_json_loads(
            candidate_path.read_text(encoding="utf-8-sig")
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise CandidateLoadError(f"cannot load candidate: {candidate_path}") from exc
    if not isinstance(payload, Mapping):
        raise CandidateLoadError("candidate JSON root must be an object")
    persisted_hash = payload.get("candidate_hash")
    try:
        candidate = canonicalize_regression_candidate(
            payload,
            json_path=str(candidate_path),
        )
    except CandidateValidationError as exc:
        raise CandidateLoadError(str(exc)) from exc
    if payload.get("schema_version") == 2:
        if not persisted_hash:
            raise CandidateLoadError("candidate_hash_missing")
        if persisted_hash != candidate["candidate_hash"]:
            raise CandidateLoadError("candidate_hash_mismatch")
    candidate["integrity_status"] = (
        "valid" if payload.get("schema_version") == 2 else "legacy_unverified"
    )
    return candidate


def list_regression_candidate_artifacts(
    candidate_dir: str | Path,
) -> dict[str, Any]:
    """List valid candidates and explicit warnings for corrupt artifacts."""
    root = Path(candidate_dir)
    if not root.exists():
        return {"items": [], "warnings": []}
    candidates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        try:
            candidate = load_regression_candidate(path)
        except CandidateLoadError as exc:
            if "candidate_hash_mismatch" in str(exc):
                code = "candidate_hash_mismatch"
            elif "candidate_hash_missing" in str(exc):
                code = "candidate_hash_missing"
            else:
                code = "malformed_json"
            warnings.append({"code": code, "path": str(path), "blocking": True})
            continue
        if not candidate.get("source_refs"):
            warnings.append(
                {"code": "missing_source", "path": str(path), "blocking": False}
            )
        candidates.append(candidate)
    return {
        "items": sorted(
            candidates,
            key=lambda candidate: (
                str(candidate.get("created_at") or ""),
                str(candidate.get("id") or ""),
            ),
            reverse=True,
        ),
        "warnings": warnings,
    }


def list_v2_regression_candidate_artifacts(
    candidate_dir: str | Path,
) -> dict[str, Any]:
    """List only schema-v2/contract-v2 candidates for active monitoring."""

    root = Path(candidate_dir)
    if not root.exists():
        return {"items": [], "warnings": []}
    candidates: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        try:
            payload = strict_json_loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            warnings.append(
                {"code": "malformed_json", "path": str(path), "blocking": True}
            )
            continue
        if not isinstance(payload, Mapping):
            warnings.append(
                {"code": "malformed_json", "path": str(path), "blocking": True}
            )
            continue
        if (
            payload.get("schema_version") != 2
            or payload.get("contract_schema_version") != 2
        ):
            continue
        try:
            candidate = load_regression_candidate(path)
        except CandidateLoadError as exc:
            if "candidate_hash_mismatch" in str(exc):
                code = "candidate_hash_mismatch"
            elif "candidate_hash_missing" in str(exc):
                code = "candidate_hash_missing"
            else:
                code = "malformed_json"
            warnings.append(
                {"code": code, "path": str(path), "blocking": True}
            )
            continue
        if not candidate.get("source_refs"):
            warnings.append(
                {"code": "missing_source", "path": str(path), "blocking": False}
            )
        candidates.append(candidate)
    return {
        "items": sorted(
            candidates,
            key=lambda candidate: (
                str(candidate.get("created_at") or ""),
                str(candidate.get("id") or ""),
            ),
            reverse=True,
        ),
        "warnings": warnings,
    }


def _persist_candidate(path: Path, candidate: Mapping[str, Any]) -> dict[str, Any]:
    stored = _candidate_storage_payload(candidate)
    artifact_io.atomic_write_json(path, stored)
    result = dict(candidate)
    result["json_path"] = str(path)
    result["integrity_status"] = "valid"
    return result


def _candidate_history_event(
    candidate: Mapping[str, Any],
    *,
    event_type: str,
    from_status: str,
    to_status: str,
    changed_fields: Sequence[str],
    reason: str,
    actor: str,
) -> dict[str, Any]:
    return {
        "event_id": f"event_{uuid.uuid4().hex}",
        "event_type": event_type,
        "from_status": from_status,
        "to_status": to_status,
        "record_revision": int(candidate["record_revision"]),
        "contract_revision": int(candidate["contract_revision"]),
        "changed_fields": list(changed_fields),
        "reason": reason,
        "actor": actor,
        "created_at": candidate["updated_at"],
    }


def _require_candidate_revision(
    candidate: Mapping[str, Any],
    expected_record_revision: int,
) -> None:
    if int(candidate.get("record_revision") or 0) != int(expected_record_revision):
        raise CandidateConflictError(
            "candidate record revision changed; reload before saving"
        )


def _validate_active_checks(candidate: Mapping[str, Any]) -> None:
    verification_type = candidate.get("verification_type")
    if verification_type not in _VERIFICATION_TYPES:
        raise CandidateValidationError("verification_type is invalid")
    expected = candidate.get("expected") or {}
    plan = _candidate_validation_plan(candidate)
    active_checks = list(plan["hard_checks"])
    if not active_checks:
        raise CandidateValidationError("at least one active check is required")
    if any(not isinstance(check, str) for check in active_checks):
        raise CandidateValidationError("active_checks must be a string list")
    if len(set(active_checks)) != len(active_checks):
        raise CandidateValidationError("active_checks must be unique")
    unknown = set(active_checks) - _ALL_HARD_CHECKS
    if unknown:
        raise CandidateValidationError(
            f"unsupported active checks: {sorted(unknown)}"
        )
    if not set(active_checks) & _CORRECTNESS_CHECKS:
        raise CandidateValidationError(
            "at least one correctness or safety hard check is required"
        )

    automatic_checks = [
        check for check in active_checks if check in _AUTOMATIC_CHECKS
    ]
    requires_manual = _MANUAL_CHECK in active_checks
    prerequisites = {
        "answer_requirements_pass": expected.get("answer_requirements"),
        "route_pass": expected.get("route"),
        "filter_pass": expected.get("filters"),
        "source_hit": expected.get("sources"),
        "expected_state_pass": expected.get("state"),
    }
    missing = [
        check
        for check, value in prerequisites.items()
        if check in automatic_checks and not value
    ]
    if missing:
        raise CandidateValidationError(
            f"active checks require expected values: {missing}"
        )
    if requires_manual and not expected.get("manual_assertions"):
        raise CandidateValidationError("manual assertions are required")

    if int(candidate.get("contract_schema_version") or 1) < 2:
        if verification_type == "graph_contract":
            if set(active_checks) - _AUTOMATIC_CHECKS:
                raise CandidateValidationError(
                    "legacy graph verification supports automatic checks only"
                )
        elif verification_type in {
            "manual_answer_quality",
            "manual_ui",
        }:
            if active_checks != [_MANUAL_CHECK]:
                raise CandidateValidationError(
                    "manual verification requires manual_assertions_pass only"
                )
        else:
            raise CandidateValidationError(
                f"{verification_type} execution is not available"
            )
    else:
        expected_type = plan["verification_type"]
        if verification_type != expected_type:
            raise CandidateValidationError(
                "verification_type does not match validation_plan"
            )

    reproduction_input = (candidate.get("observed") or {}).get(
        "reproduction_input"
    ) or {}
    automatic_required = bool(automatic_checks)
    if automatic_required and not str(
        reproduction_input.get("question") or ""
    ).strip():
        raise CandidateValidationError("reproduction question is required")
    if (
        requires_manual
        and not automatic_required
        and not str(
            reproduction_input.get("scenario")
            or reproduction_input.get("question")
            or ""
        ).strip()
    ):
        raise CandidateValidationError(
            "manual reproduction scenario is required"
        )


def assess_candidate_reproduction_readiness(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Return explicit blockers for an automatic or manual reproduction."""

    canonical = canonicalize_regression_candidate(candidate)
    reproduction_input = (canonical.get("observed") or {}).get(
        "reproduction_input"
    ) or {}
    expected_state = (canonical.get("expected") or {}).get("state") or {}
    actual_state = (
        (canonical.get("observed") or {}).get("actual") or {}
    ).get("state") or {}
    automatic_required = _candidate_requires_automatic(canonical)
    manual_required = _candidate_requires_manual(canonical)
    requires_prior_scope = automatic_required and bool(
        reproduction_input.get("requires_prior_scope")
        or str(expected_state.get("followup_scope_intent") or "").lower()
        in {"true", "1", "yes"}
        or str(actual_state.get("followup_scope_intent") or "").lower()
        in {"true", "1", "yes"}
    )
    missing_fields: list[str] = []
    if automatic_required and not str(
        reproduction_input.get("question") or ""
    ).strip():
        missing_fields.append("reproduction_input.question")
    if (
        manual_required
        and not automatic_required
        and not str(
            reproduction_input.get("scenario")
            or reproduction_input.get("question")
            or ""
        ).strip()
    ):
        missing_fields.append("reproduction_input.scenario")
    if requires_prior_scope and not reproduction_input.get(
        "prior_search_scope"
    ):
        missing_fields.append(
            "reproduction_input.prior_search_scope"
        )
    if int(canonical.get("contract_schema_version") or 1) >= 2:
        manifest = canonical.get("reproduction_manifest")
        try:
            reproduction_manifest_module.require_complete_reproduction_manifest(
                manifest
            )
        except reproduction_manifest_module.ReproductionManifestError:
            missing_fields.append("reproduction_manifest")
    return {
        "ready": not missing_fields,
        "requires_prior_scope": requires_prior_scope,
        "missing_fields": missing_fields,
        "reason": (
            ""
            if not missing_fields
            else "missing reproduction information: "
            + ", ".join(missing_fields)
        ),
    }


def _require_candidate_reproduction_readiness(
    candidate: Mapping[str, Any],
) -> None:
    readiness = assess_candidate_reproduction_readiness(candidate)
    if not readiness["ready"]:
        raise CandidateValidationError(str(readiness["reason"]))


def update_regression_candidate(
    path: str | Path,
    *,
    expected_record_revision: int,
    changes: Mapping[str, Any],
    actor: str = "local_operator",
    reason: str,
) -> dict[str, Any]:
    """Apply a compare-and-swap candidate update."""
    candidate_path = Path(path)
    if not str(reason or "").strip():
        raise CandidateValidationError("update reason is required")
    if not isinstance(changes, Mapping):
        raise CandidateValidationError("changes must be an object")
    allowed = {
        "severity",
        "impact_area",
        "impact_summary",
        "operator_decision",
        "observed",
        "expected",
        "active_checks",
        "verification_type",
        "validation_plan",
        "reproduction_manifest",
        "duplicate_of",
        "fixed_in_version",
        "closure_reason",
        "suite_case_id",
        "suite_exclusion_reason",
    }
    unknown = set(changes) - allowed
    if unknown or any("." in str(key) for key in changes):
        raise CandidateValidationError(f"unsupported candidate fields: {sorted(unknown)}")

    with _CANDIDATE_WRITE_LOCK:
        candidate = load_regression_candidate(candidate_path)
        _require_candidate_revision(candidate, expected_record_revision)
        status = str(candidate.get("triage_status"))
        if status in _TERMINAL_CANDIDATE_STATUSES:
            raise CandidateValidationError("terminal candidate must be reopened first")

        next_candidate = dict(candidate)
        changed_fields: list[str] = []
        contract_changed = False
        record_fields = {"severity", "impact_area", "impact_summary"}
        closure_fields = {
            "fixed_in_version",
            "closure_reason",
            "suite_case_id",
            "suite_exclusion_reason",
        }
        for key, raw_value in changes.items():
            value = raw_value
            if key in record_fields:
                if status not in {
                    "new",
                    "triaged",
                    "needs_expectation",
                    "ready",
                    "reproduced",
                    "fixing",
                    "verified",
                }:
                    raise CandidateValidationError(f"{key} is not editable in {status}")
                if key == "severity" and value not in {
                    "untriaged",
                    "S1",
                    "S2",
                    "S3",
                    "S4",
                }:
                    raise CandidateValidationError("severity is invalid")
                if key == "impact_area" and value not in _IMPACT_AREAS:
                    raise CandidateValidationError("impact_area is invalid")
            elif key == "operator_decision":
                if status not in {"new", "triaged", "needs_expectation", "ready"}:
                    raise CandidateValidationError(
                        "operator_decision is no longer editable"
                    )
                if value not in {
                    "unreviewed",
                    "accepted",
                    "needs_info",
                    "duplicate",
                    "rejected",
                }:
                    raise CandidateValidationError("operator_decision is invalid")
            elif key == "observed":
                if status not in {
                    "needs_expectation",
                    "ready",
                    "reproduced",
                    "fixing",
                    "verified",
                }:
                    raise CandidateValidationError(
                        "reproduction input cannot be edited in this state"
                    )
                if not isinstance(value, Mapping) or set(value) != {
                    "reproduction_input"
                }:
                    raise CandidateValidationError(
                        "observed update may replace reproduction_input only"
                    )
                reproduction_input = value.get("reproduction_input")
                if not isinstance(reproduction_input, Mapping):
                    raise CandidateValidationError(
                        "reproduction_input must be an object"
                    )
                observed = dict(candidate.get("observed") or {})
                observed["reproduction_input"] = dict(reproduction_input)
                value = observed
                contract_changed = contract_changed or value != candidate.get(key)
            elif key == "expected":
                if status not in {
                    "needs_expectation",
                    "ready",
                    "reproduced",
                    "fixing",
                    "verified",
                }:
                    raise CandidateValidationError(
                        "expected cannot be edited in this state"
                    )
                value = _canonical_expected(value)
                contract_changed = contract_changed or value != candidate.get(key)
            elif key == "active_checks":
                if status not in {
                    "needs_expectation",
                    "ready",
                    "reproduced",
                    "fixing",
                    "verified",
                }:
                    raise CandidateValidationError(
                        "active_checks cannot be edited in this state"
                    )
                if not isinstance(value, (list, tuple)) or any(
                    not isinstance(check, str) for check in value
                ):
                    raise CandidateValidationError("active_checks must be a string list")
                value = list(value)
                if int(
                    next_candidate.get("contract_schema_version") or 1
                ) >= 2:
                    raw_plan = dict(
                        next_candidate.get("validation_plan") or {}
                    )
                    raw_plan["hard_checks"] = value
                    normalized_plan = _canonical_validation_plan(
                        raw_plan,
                        active_checks=value,
                        verification_type=str(
                            next_candidate.get("verification_type")
                            or "graph_contract"
                        ),
                        quality_profile=next_candidate.get(
                            "quality_profile"
                        ),
                    )
                    next_candidate["validation_plan"] = {
                        plan_key: normalized_plan[plan_key]
                        for plan_key in (
                            "schema_version",
                            "quality_profile",
                            "hard_checks",
                            "soft_objectives",
                            "performance_budget",
                        )
                    }
                    next_candidate["quality_profile"] = normalized_plan[
                        "quality_profile"
                    ]
                    next_candidate["verification_type"] = normalized_plan[
                        "verification_type"
                    ]
                    changed_fields.extend(
                        [
                            "validation_plan",
                            "quality_profile",
                            "verification_type",
                        ]
                    )
                contract_changed = contract_changed or value != candidate.get(key)
            elif key == "verification_type":
                if status not in {
                    "needs_expectation",
                    "ready",
                    "reproduced",
                    "fixing",
                    "verified",
                }:
                    raise CandidateValidationError(
                        "verification_type cannot be edited in this state"
                    )
                if value not in _VERIFICATION_TYPES:
                    raise CandidateValidationError("verification_type is invalid")
                if int(
                    next_candidate.get("contract_schema_version") or 1
                ) >= 2:
                    normalized_plan = _canonical_validation_plan(
                        next_candidate.get("validation_plan"),
                        active_checks=list(
                            next_candidate.get("active_checks") or []
                        ),
                        verification_type=str(value),
                        quality_profile=next_candidate.get(
                            "quality_profile"
                        ),
                    )
                    value = normalized_plan["verification_type"]
                contract_changed = contract_changed or value != candidate.get(key)
            elif key == "validation_plan":
                if status not in {
                    "needs_expectation",
                    "ready",
                    "reproduced",
                    "fixing",
                    "verified",
                }:
                    raise CandidateValidationError(
                        "validation_plan cannot be edited in this state"
                    )
                normalized_plan = _canonical_validation_plan(
                    value,
                    active_checks=list(
                        next_candidate.get("active_checks") or []
                    ),
                    verification_type=str(
                        next_candidate.get("verification_type")
                        or "graph_contract"
                    ),
                    quality_profile=next_candidate.get(
                        "quality_profile"
                    ),
                )
                value = {
                    plan_key: normalized_plan[plan_key]
                    for plan_key in (
                        "schema_version",
                        "quality_profile",
                        "hard_checks",
                        "soft_objectives",
                        "performance_budget",
                    )
                }
                next_candidate["contract_schema_version"] = 2
                if not next_candidate.get("reproduction_manifest"):
                    next_candidate["reproduction_manifest"] = (
                        reproduction_manifest_module
                        .build_runtime_reproduction_manifest(
                            data_revision=None,
                            index_revision=None,
                        )
                    )
                    changed_fields.append("reproduction_manifest")
                next_candidate["quality_profile"] = normalized_plan[
                    "quality_profile"
                ]
                next_candidate["active_checks"] = list(
                    normalized_plan["hard_checks"]
                )
                next_candidate["verification_type"] = normalized_plan[
                    "verification_type"
                ]
                changed_fields.extend(
                    [
                        "contract_schema_version",
                        "quality_profile",
                        "active_checks",
                        "verification_type",
                    ]
                )
                contract_changed = contract_changed or (
                    value != candidate.get(key)
                )
            elif key == "reproduction_manifest":
                if status not in {
                    "needs_expectation",
                    "ready",
                    "reproduced",
                    "fixing",
                    "verified",
                }:
                    raise CandidateValidationError(
                        "reproduction_manifest cannot be edited in this state"
                    )
                try:
                    value = (
                        reproduction_manifest_module
                        .canonicalize_reproduction_manifest(value)
                    )
                except (
                    reproduction_manifest_module.ReproductionManifestError
                ) as exc:
                    raise CandidateValidationError(str(exc)) from exc
                next_candidate["contract_schema_version"] = 2
                changed_fields.append("contract_schema_version")
                contract_changed = contract_changed or (
                    value != candidate.get(key)
                )
            elif key == "duplicate_of":
                if status not in {"new", "triaged", "needs_expectation", "ready"}:
                    raise CandidateValidationError("duplicate target is not editable")
            elif key in closure_fields:
                if status != "verified":
                    raise CandidateValidationError(
                        f"{key} can be set only after verification"
                    )

            if next_candidate.get(key) != value:
                next_candidate[key] = value
                changed_fields.append(key)

        if not changed_fields:
            return candidate
        from_status = status
        if contract_changed:
            next_candidate["expected_approved_at"] = None
            next_candidate["expected_approved_by"] = None
            next_candidate["contract_revision"] = (
                int(candidate["contract_revision"]) + 1
            )
            changed_fields.extend(
                ["expected_approved_at", "expected_approved_by", "contract_revision"]
            )
            if status in {"ready", "reproduced", "fixing", "verified"}:
                next_candidate["triage_status"] = "needs_expectation"
                changed_fields.append("triage_status")

        next_candidate["record_revision"] = int(candidate["record_revision"]) + 1
        next_candidate["updated_at"] = _utc_iso_now()
        next_candidate["candidate_hash"] = compute_candidate_hash(next_candidate)
        next_candidate["history"] = list(candidate.get("history") or []) + [
            _candidate_history_event(
                next_candidate,
                event_type="candidate_updated",
                from_status=from_status,
                to_status=str(next_candidate["triage_status"]),
                changed_fields=list(dict.fromkeys(changed_fields)),
                reason=reason,
                actor=actor,
            )
        ]
        return _persist_candidate(candidate_path, next_candidate)


def approve_candidate_expectation(
    path: str | Path,
    *,
    expected_record_revision: int,
    actor: str = "local_operator",
    reason: str,
) -> dict[str, Any]:
    """Approve the current expectation and start a new verification epoch."""
    candidate_path = Path(path)
    if not str(reason or "").strip():
        raise CandidateValidationError("approval reason is required")
    with _CANDIDATE_WRITE_LOCK:
        candidate = load_regression_candidate(candidate_path)
        _require_candidate_revision(candidate, expected_record_revision)
        if candidate.get("triage_status") != "needs_expectation":
            raise CandidateTransitionError(
                "expectation can be approved only from needs_expectation"
            )
        _validate_active_checks(candidate)
        next_candidate = dict(candidate)
        next_candidate["expected_approved_at"] = _utc_iso_now()
        next_candidate["expected_approved_by"] = actor
        next_candidate["record_revision"] = int(candidate["record_revision"]) + 1
        next_candidate["contract_revision"] = (
            int(candidate["contract_revision"]) + 1
        )
        next_candidate["updated_at"] = _utc_iso_now()
        next_candidate["candidate_hash"] = compute_candidate_hash(next_candidate)
        next_candidate["history"] = list(candidate.get("history") or []) + [
            _candidate_history_event(
                next_candidate,
                event_type="expectation_approved",
                from_status="needs_expectation",
                to_status="needs_expectation",
                changed_fields=[
                    "expected_approved_at",
                    "expected_approved_by",
                    "contract_revision",
                ],
                reason=reason,
                actor=actor,
            )
        ]
        return _persist_candidate(candidate_path, next_candidate)


def revoke_candidate_expectation(
    path: str | Path,
    *,
    expected_record_revision: int,
    actor: str = "local_operator",
    reason: str,
) -> dict[str, Any]:
    """Revoke approval and make all prior evidence stale."""
    candidate_path = Path(path)
    if not str(reason or "").strip():
        raise CandidateValidationError("revocation reason is required")
    with _CANDIDATE_WRITE_LOCK:
        candidate = load_regression_candidate(candidate_path)
        _require_candidate_revision(candidate, expected_record_revision)
        if candidate.get("triage_status") not in {
            "ready",
            "reproduced",
            "fixing",
            "verified",
        }:
            raise CandidateTransitionError("expectation cannot be revoked now")
        from_status = str(candidate["triage_status"])
        next_candidate = dict(candidate)
        next_candidate.update(
            {
                "expected_approved_at": None,
                "expected_approved_by": None,
                "triage_status": "needs_expectation",
                "record_revision": int(candidate["record_revision"]) + 1,
                "contract_revision": int(candidate["contract_revision"]) + 1,
                "updated_at": _utc_iso_now(),
            }
        )
        next_candidate["candidate_hash"] = compute_candidate_hash(next_candidate)
        next_candidate["history"] = list(candidate.get("history") or []) + [
            _candidate_history_event(
                next_candidate,
                event_type="expectation_revoked",
                from_status=from_status,
                to_status="needs_expectation",
                changed_fields=[
                    "expected_approved_at",
                    "expected_approved_by",
                    "triage_status",
                    "contract_revision",
                ],
                reason=reason,
                actor=actor,
            )
        ]
        return _persist_candidate(candidate_path, next_candidate)


def is_current_candidate_contract(
    candidate: Mapping[str, Any],
    artifact: Mapping[str, Any],
) -> bool:
    """Return whether an artifact belongs to the candidate's current contract."""
    return (
        artifact.get("candidate_id") == candidate.get("id")
        and int(artifact.get("contract_revision") or -1)
        == int(candidate.get("contract_revision") or 0)
        and artifact.get("candidate_hash") == candidate.get("candidate_hash")
    )


def _run_result_status(run: Mapping[str, Any]) -> str | None:
    summary = run.get("summary")
    if isinstance(summary, Mapping) and summary.get("status") in {
        "pass",
        "fail",
    }:
        return str(summary["status"])
    results = run.get("results") or []
    if isinstance(results, list) and results and isinstance(results[0], Mapping):
        return str(results[0].get("status") or "")
    if isinstance(summary, Mapping):
        if int(summary.get("failed") or 0) > 0:
            return "fail"
        if int(summary.get("passed") or 0) > 0:
            return "pass"
    return None


def _summarize_candidate_run_results(
    results: list[dict[str, Any]],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    plan = _candidate_validation_plan(candidate)
    evaluation_checks = _candidate_automatic_checks(candidate)
    summary = _summarize_eval_results(results)
    latencies = sorted(
        float(result["latency_seconds"])
        for result in results
        if isinstance(result.get("latency_seconds"), (int, float))
    )
    p95 = (
        round(_percentile(latencies, 0.95), 3)
        if latencies
        else None
    )
    failed_set = {
        check
        for check in evaluation_checks
        if any(result.get(check) is not True for result in results)
    }
    budget = plan["performance_budget"]
    performance_pass = (
        p95 is not None
        and p95 <= float(budget["max_p95_seconds"])
    )
    if (
        _PERFORMANCE_CHECK in plan["hard_checks"]
        and not performance_pass
    ):
        failed_set.add(_PERFORMANCE_CHECK)
    hard_failed_checks = [
        check
        for check in plan["hard_checks"]
        if check in failed_set
    ]
    soft_results: dict[str, Any] = {}
    for objective in plan["soft_objectives"]:
        if objective == "latency_p95":
            soft_results[objective] = {
                "measured": p95 is not None,
                "value": p95,
                "target_max_seconds": budget[
                    "max_p95_seconds"
                ],
                "met": performance_pass,
            }
        else:
            soft_results[objective] = {
                "measured": False,
                "value": None,
                "met": None,
            }
    summary.update(
        {
            "status": (
                "fail" if hard_failed_checks else "pass"
            ),
            "sample_count": len(results),
            "p95_latency_seconds": p95,
            "performance_budget_seconds": budget[
                "max_p95_seconds"
            ],
            "performance_p95_pass": performance_pass,
            "hard_failed_checks": hard_failed_checks,
            "soft_objectives": soft_results,
        }
    )
    return summary


def validate_completed_candidate_run_evidence(
    run: Mapping[str, Any],
) -> None:
    """Reject internally contradictory completed-run evidence."""
    results = run.get("results")
    summary = run.get("summary")
    provenance = run.get("provenance")
    active_checks = list(run.get("active_checks") or [])
    if (
        not isinstance(provenance, Mapping)
        or provenance.get("snapshot_available") is not True
        or not isinstance(results, list)
        or not results
        or any(not isinstance(result, Mapping) for result in results)
        or not isinstance(summary, Mapping)
    ):
        raise CandidateValidationError(
            "candidate run outcome is inconsistent"
        )
    contract_schema_version = int(
        run.get("contract_schema_version") or 1
    )
    evaluation_checks = list(
        run.get("evaluation_checks") or active_checks
    )
    for result in results:
        status = result.get("status")
        failed_checks = result.get("failed_checks")
        expected_failed_checks = [
            check
            for check in evaluation_checks
            if result.get(check) is not True
        ]
        if (
            status not in {"pass", "fail"}
            or not isinstance(failed_checks, list)
            or any(
                not isinstance(check, str)
                for check in failed_checks
            )
            or result.get("case_id") != run.get("candidate_id")
            or failed_checks != expected_failed_checks
            or (status == "pass") != (not failed_checks)
        ):
            raise CandidateValidationError(
                "candidate run outcome is inconsistent"
            )
    passed_count = sum(
        1 for result in results if result.get("status") == "pass"
    )
    case_count = summary.get(
        "case_count",
        summary.get("total"),
    )
    if (
        case_count != len(results)
        or (
            "case_count" in summary
            and "total" in summary
            and summary.get("case_count") != summary.get("total")
        )
        or summary.get("passed") != passed_count
        or summary.get("failed") != len(results) - passed_count
    ):
        raise CandidateValidationError(
            "candidate run outcome is inconsistent"
        )
    if contract_schema_version < 2:
        if len(results) != 1:
            raise CandidateValidationError(
                "legacy candidate run must contain one result"
            )
        return

    validation_plan = run.get("validation_plan")
    if not isinstance(validation_plan, Mapping):
        raise CandidateValidationError(
            "candidate run validation plan is missing"
        )
    plan = _canonical_validation_plan(
        validation_plan,
        active_checks=active_checks,
        verification_type=None,
        quality_profile=run.get("quality_profile"),
    )
    if active_checks != list(plan["hard_checks"]):
        raise CandidateValidationError(
            "candidate run hard checks are inconsistent"
        )
    expected_evaluation_checks = [
        check
        for check in active_checks
        if check in _AUTOMATIC_CHECKS
    ]
    if evaluation_checks != expected_evaluation_checks:
        raise CandidateValidationError(
            "candidate run evaluation checks are inconsistent"
        )
    budget = plan["performance_budget"]
    if len(results) != budget["min_runs"]:
        raise CandidateValidationError(
            "candidate run sample count is inconsistent"
        )
    latencies = sorted(
        float(result["latency_seconds"])
        for result in results
        if isinstance(result.get("latency_seconds"), (int, float))
    )
    if len(latencies) != len(results):
        raise CandidateValidationError(
            "candidate run latency evidence is incomplete"
        )
    p95 = round(_percentile(latencies, 0.95), 3)
    performance_pass = p95 <= budget["max_p95_seconds"]
    hard_failed = {
        check
        for check in evaluation_checks
        if any(result.get(check) is not True for result in results)
    }
    if (
        _PERFORMANCE_CHECK in active_checks
        and not performance_pass
    ):
        hard_failed.add(_PERFORMANCE_CHECK)
    expected_hard_failed = [
        check for check in active_checks if check in hard_failed
    ]
    if (
        summary.get("sample_count") != len(results)
        or summary.get("p95_latency_seconds") != p95
        or summary.get("performance_budget_seconds")
        != budget["max_p95_seconds"]
        or summary.get("performance_p95_pass")
        is not performance_pass
        or summary.get("hard_failed_checks")
        != expected_hard_failed
        or summary.get("status")
        != ("fail" if expected_hard_failed else "pass")
        or not isinstance(summary.get("soft_objectives"), Mapping)
    ):
        raise CandidateValidationError(
            "candidate run aggregate evidence is inconsistent"
        )


def record_candidate_run(
    path: str | Path,
    *,
    run: Mapping[str, Any],
    run_kind: str,
    expected_record_revision: int,
    expected_contract_revision: int,
    expected_candidate_hash: str,
    actor: str = "local_operator",
) -> dict[str, Any]:
    """Attach an integrity-valid completed baseline or verification run."""
    if run_kind not in {"baseline", "verification"}:
        raise CandidateValidationError("run_kind is invalid")
    candidate_path = Path(path)
    with _CANDIDATE_WRITE_LOCK:
        candidate = load_regression_candidate(candidate_path)
        _require_candidate_revision(candidate, expected_record_revision)
        run_path_value = run.get("json_path")
        if not str(run_path_value or "").strip():
            raise CandidateValidationError(
                "persisted run artifact path is required"
            )
        loaded_run = load_evaluation_run(str(run_path_value))
        if (
            loaded_run.get("integrity_status") != "valid"
            or loaded_run.get("run_id") != run.get("run_id")
            or loaded_run.get("run_hash") != run.get("run_hash")
        ):
            raise CandidateValidationError(
                "persisted run does not match the supplied run"
            )
        validated_run = loaded_run
        if (
            int(candidate["contract_revision"]) != int(expected_contract_revision)
            or candidate["candidate_hash"] != expected_candidate_hash
            or not is_current_candidate_contract(candidate, validated_run)
        ):
            raise CandidateConflictError("run belongs to a stale candidate contract")
        if (
            validated_run.get("run_status") != "completed"
            or not validated_run.get("run_hash")
            or validated_run.get("run_kind") != run_kind
        ):
            raise CandidateValidationError("only completed hashed runs may be attached")
        provenance = validated_run.get("provenance")
        config_fingerprint = (
            provenance.get("config_fingerprint")
            if isinstance(provenance, Mapping)
            else None
        )
        try:
            normalized_provenance, provenance_blocker = (
                _validate_candidate_run_provenance(
                    candidate,
                    provenance,
                    run_kind=run_kind,
                )
            )
        except CandidateValidationError as exc:
            raise CandidateValidationError(
                "candidate run provenance is invalid"
            ) from exc
        if (
            not isinstance(provenance, Mapping)
            or normalized_provenance != dict(provenance)
            or provenance_blocker is not None
            or not isinstance(provenance.get("backend_mode"), str)
            or not provenance.get("backend_mode")
            or provenance.get("snapshot_available") is not True
            or provenance.get("snapshot_id") is not None
            and not isinstance(provenance.get("snapshot_id"), str)
            or not isinstance(provenance.get("data_revision"), str)
            or not provenance.get("data_revision")
            or not isinstance(config_fingerprint, str)
            or len(config_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in config_fingerprint
            )
            or validated_run.get("expected_approved_at")
            != candidate.get("expected_approved_at")
            or list(validated_run.get("active_checks") or [])
            != list(candidate.get("active_checks") or [])
            or int(
                validated_run.get("contract_schema_version") or 1
            )
            != int(candidate.get("contract_schema_version") or 1)
            or (
                int(candidate.get("contract_schema_version") or 1)
                >= 2
                and (
                    validated_run.get("validation_plan")
                    != candidate.get("validation_plan")
                    or validated_run.get(
                        "reproduction_manifest_hash"
                    )
                    != (
                        provenance.get("reproduction_manifest")
                        or {}
                    ).get("manifest_hash")
                )
            )
        ):
            raise CandidateValidationError(
                "candidate run provenance is invalid"
            )
        validate_completed_candidate_run_evidence(validated_run)
        required_status = "ready" if run_kind == "baseline" else "fixing"
        if candidate.get("triage_status") != required_status:
            raise CandidateValidationError(
                f"{run_kind} run cannot be attached in this state"
            )
        summary = validated_run.get("summary")
        summary = summary if isinstance(summary, Mapping) else {}
        results = validated_run.get("results")
        first_result = (
            results[0]
            if isinstance(results, list)
            and results
            and isinstance(results[0], Mapping)
            else {}
        )
        reference = {
            "run_id": validated_run.get("run_id"),
            "run_hash": validated_run.get("run_hash"),
            "run_kind": run_kind,
            "status": _run_result_status(validated_run),
            "artifact_path": validated_run.get("json_path"),
            "candidate_id": candidate["id"],
            "contract_revision": candidate["contract_revision"],
            "candidate_hash": candidate["candidate_hash"],
            "created_at": validated_run.get("created_at"),
            "failed_checks": list(
                summary.get("hard_failed_checks")
                or first_result.get("failed_checks")
                or []
            ),
            "sample_count": summary.get(
                "sample_count",
                summary.get("case_count"),
            ),
            "p95_latency_seconds": summary.get(
                "p95_latency_seconds"
            ),
        }
        evidence = dict(candidate.get("evidence") or {})
        evidence_key = (
            "baseline_runs" if run_kind == "baseline" else "verification_runs"
        )
        existing = list(evidence.get(evidence_key) or [])
        if any(item.get("run_hash") == reference["run_hash"] for item in existing):
            return candidate
        evidence[evidence_key] = existing + [reference]
        next_candidate = dict(candidate)
        next_candidate["evidence"] = evidence
        next_candidate["record_revision"] = int(candidate["record_revision"]) + 1
        next_candidate["updated_at"] = _utc_iso_now()
        next_candidate["history"] = list(candidate.get("history") or []) + [
            _candidate_history_event(
                next_candidate,
                event_type=f"{run_kind}_run_recorded",
                from_status=str(candidate["triage_status"]),
                to_status=str(candidate["triage_status"]),
                changed_fields=[f"evidence.{evidence_key}"],
                reason=f"{run_kind} run {reference['run_id']} recorded",
                actor=actor,
            )
        ]
        return _persist_candidate(candidate_path, next_candidate)


def record_candidate_handoff(
    path: str | Path,
    *,
    handoff: Mapping[str, Any],
    expected_record_revision: int,
    expected_contract_revision: int,
    expected_candidate_hash: str,
    actor: str = "local_operator",
) -> dict[str, Any]:
    """Attach a validated Codex handoff for the exact current failed baseline."""
    if not isinstance(handoff, Mapping):
        raise CandidateValidationError("handoff must be an object")
    manifest_path_value = handoff.get("manifest_path")
    if not str(manifest_path_value or "").strip():
        raise CandidateValidationError("handoff manifest path is required")

    # Imported lazily because the handoff module reuses candidate/run validators
    # from this module.
    from src.core.feedback_handoff import (
        FeedbackHandoffError,
        build_codex_handoff_payload,
        validate_codex_handoff_artifacts,
    )

    candidate_path = Path(path)
    with _CANDIDATE_WRITE_LOCK:
        candidate = load_regression_candidate(candidate_path)
        _require_candidate_revision(candidate, expected_record_revision)
        if (
            int(candidate["contract_revision"]) != int(expected_contract_revision)
            or candidate["candidate_hash"] != expected_candidate_hash
        ):
            raise CandidateConflictError(
                "handoff belongs to a stale candidate contract"
            )
        if candidate.get("triage_status") not in {"reproduced", "fixing"}:
            raise CandidateValidationError(
                "handoff can be attached only while reproduced or fixing"
            )
        if not _candidate_requires_automatic(candidate):
            raise CandidateValidationError(
                "handoff requires automatic baseline evidence"
            )

        manifest_path = Path(str(manifest_path_value))
        output_root = manifest_path.parent.parent
        try:
            validated_handoff = validate_codex_handoff_artifacts(
                manifest_path,
                output_root=output_root,
            )
        except FeedbackHandoffError as exc:
            raise CandidateValidationError(
                f"handoff artifact is invalid: {exc.code}"
            ) from exc

        identity_fields = (
            "handoff_id",
            "candidate_id",
            "contract_revision",
            "candidate_hash",
            "baseline_run_id",
            "baseline_run_hash",
            "payload_sha256",
            "markdown_sha256",
            "manifest_sha256",
        )
        if any(
            handoff.get(key) != validated_handoff.get(key)
            for key in identity_fields
        ):
            raise CandidateValidationError(
                "persisted handoff does not match the supplied handoff"
            )
        if (
            validated_handoff.get("handoff_schema_version")
            not in {1, 2}
            or validated_handoff.get("kind")
            != "finance_llm_codex_handoff"
            or validated_handoff.get("candidate_id")
            != safe_artifact_token(str(candidate["id"]))
            or int(validated_handoff.get("contract_revision") or -1)
            != int(candidate["contract_revision"])
            or validated_handoff.get("candidate_hash")
            != candidate["candidate_hash"]
        ):
            raise CandidateConflictError(
                "handoff belongs to a stale candidate contract"
            )

        baseline_ref = next(
            (
                ref
                for ref in (candidate.get("evidence") or {}).get(
                    "baseline_runs"
                )
                or []
                if ref.get("run_id")
                == validated_handoff.get("baseline_run_id")
                and ref.get("run_hash")
                == validated_handoff.get("baseline_run_hash")
                and ref.get("status") == "fail"
                and is_current_candidate_contract(candidate, ref)
            ),
            None,
        )
        if not baseline_ref or not baseline_ref.get("artifact_path"):
            raise CandidateValidationError(
                "handoff baseline is not linked to the candidate"
            )
        try:
            baseline_run = load_evaluation_run(
                str(baseline_ref["artifact_path"])
            )
        except CandidateLoadError as exc:
            raise CandidateValidationError(
                "handoff baseline artifact is invalid"
            ) from exc
        if (
            baseline_run.get("integrity_status") != "valid"
            or baseline_run.get("run_id")
            != validated_handoff.get("baseline_run_id")
            or baseline_run.get("run_hash")
            != validated_handoff.get("baseline_run_hash")
            or baseline_run.get("run_kind") != "baseline"
            or baseline_run.get("run_status") != "completed"
            or _run_result_status(baseline_run) != "fail"
            or not is_current_candidate_contract(candidate, baseline_run)
        ):
            raise CandidateValidationError(
                "handoff baseline artifact does not match the current failure"
            )

        try:
            rebuilt_payload = build_codex_handoff_payload(
                candidate,
                baseline_run,
                verification_commands=validated_handoff["payload"][
                    "verification"
                ]["commands"],
            )
        except FeedbackHandoffError as exc:
            raise CandidateValidationError(
                f"handoff content is stale: {exc.code}"
            ) from exc
        rebuilt_payload_hash = hashlib.sha256(
            _canonical_json_bytes(rebuilt_payload)
        ).hexdigest()
        if rebuilt_payload_hash != validated_handoff.get("payload_sha256"):
            raise CandidateValidationError("handoff content is stale")

        reference = {
            "handoff_id": validated_handoff["handoff_id"],
            "contract_revision": candidate["contract_revision"],
            "candidate_hash": candidate["candidate_hash"],
            "baseline_run_id": validated_handoff["baseline_run_id"],
            "baseline_run_hash": validated_handoff["baseline_run_hash"],
            "manifest_path": validated_handoff["manifest_path"],
            "created_at": validated_handoff["created_at"],
            "payload_sha256": validated_handoff["payload_sha256"],
            "markdown_sha256": validated_handoff["markdown_sha256"],
            "manifest_sha256": validated_handoff["manifest_sha256"],
            "approved_by": validated_handoff["approved_by"],
        }
        existing = list(candidate.get("handoffs") or [])
        if any(
            item.get("manifest_sha256") == reference["manifest_sha256"]
            for item in existing
        ):
            return candidate

        next_candidate = dict(candidate)
        next_candidate["handoffs"] = existing + [reference]
        next_candidate["record_revision"] = (
            int(candidate["record_revision"]) + 1
        )
        next_candidate["updated_at"] = _utc_iso_now()
        next_candidate["history"] = list(candidate.get("history") or []) + [
            _candidate_history_event(
                next_candidate,
                event_type="handoff_recorded",
                from_status=str(candidate["triage_status"]),
                to_status=str(candidate["triage_status"]),
                changed_fields=["handoffs"],
                reason=(
                    f"Codex handoff {reference['handoff_id']} recorded for "
                    f"baseline {reference['baseline_run_id']}"
                ),
                actor=actor,
            )
        ]
        return _persist_candidate(candidate_path, next_candidate)


def record_candidate_manual_evidence(
    path: str | Path,
    *,
    evidence_kind: str,
    checklist_results: list[Mapping[str, Any]],
    expected_record_revision: int,
    expected_contract_revision: int,
    expected_candidate_hash: str,
    actor: str = "local_operator",
    reason: str,
) -> dict[str, Any]:
    """Record approved manual reproduction or verification evidence."""
    if evidence_kind not in {"manual_reproduction", "manual_verification"}:
        raise CandidateValidationError("manual evidence kind is invalid")
    reason = str(reason or "").strip()
    if not reason:
        raise CandidateValidationError("manual evidence reason is required")
    if not isinstance(checklist_results, list) or any(
        not isinstance(item, Mapping) for item in checklist_results
    ):
        raise CandidateValidationError("manual checklist must be a list of objects")
    candidate_path = Path(path)
    with _CANDIDATE_WRITE_LOCK:
        candidate = load_regression_candidate(candidate_path)
        _require_candidate_revision(candidate, expected_record_revision)
        if (
            int(candidate["contract_revision"]) != int(expected_contract_revision)
            or candidate["candidate_hash"] != expected_candidate_hash
        ):
            raise CandidateConflictError("manual evidence belongs to a stale contract")
        if not candidate.get("expected_approved_at"):
            raise CandidateValidationError(
                "manual evidence requires an approved expectation"
            )
        if not _candidate_requires_manual(candidate):
            raise CandidateValidationError(
                "manual evidence is not part of the current validation plan"
            )
        required_status = (
            "ready" if evidence_kind == "manual_reproduction" else "fixing"
        )
        if candidate.get("triage_status") != required_status:
            raise CandidateValidationError(
                "manual evidence cannot be recorded in this state"
            )
        assertions = (candidate.get("expected") or {}).get("manual_assertions") or []
        expected_ids = {str(item.get("id")) for item in assertions}
        actual_ids = {str(item.get("assertion_id")) for item in checklist_results}
        if (
            not expected_ids
            or actual_ids != expected_ids
            or len(checklist_results) != len(expected_ids)
        ):
            raise CandidateValidationError(
                "manual checklist does not match approved assertions"
            )
        normalized_results: list[dict[str, Any]] = []
        for item in checklist_results:
            if set(item) - {"assertion_id", "passed", "note"}:
                raise CandidateValidationError("manual checklist shape is invalid")
            if not isinstance(item.get("passed"), bool):
                raise CandidateValidationError("manual checklist passed must be bool")
            normalized_results.append(
                {
                    "assertion_id": str(item["assertion_id"]),
                    "passed": item["passed"],
                    "note": str(item.get("note") or ""),
                }
            )
        all_passed = all(item["passed"] for item in normalized_results)
        outcome = (
            ("not_reproduced" if all_passed else "reproduced")
            if evidence_kind == "manual_reproduction"
            else ("passed" if all_passed else "failed")
        )
        evidence_record = {
            "evidence_id": f"manual_{uuid.uuid4().hex}",
            "evidence_kind": evidence_kind,
            "outcome": outcome,
            "contract_revision": candidate["contract_revision"],
            "candidate_hash": candidate["candidate_hash"],
            "assertion_results": normalized_results,
            "approved_by": actor,
            "reason": reason,
            "created_at": _utc_iso_now(),
        }
        evidence = dict(candidate.get("evidence") or {})
        evidence_key = (
            "manual_reproductions"
            if evidence_kind == "manual_reproduction"
            else "manual_verifications"
        )
        evidence[evidence_key] = list(evidence.get(evidence_key) or []) + [
            evidence_record
        ]
        next_candidate = dict(candidate)
        next_candidate["evidence"] = evidence
        next_candidate["record_revision"] = int(candidate["record_revision"]) + 1
        next_candidate["updated_at"] = _utc_iso_now()
        next_candidate["history"] = list(candidate.get("history") or []) + [
            _candidate_history_event(
                next_candidate,
                event_type=f"{evidence_kind}_recorded",
                from_status=str(candidate["triage_status"]),
                to_status=str(candidate["triage_status"]),
                changed_fields=[f"evidence.{evidence_key}"],
                reason=reason,
                actor=actor,
            )
        ]
        return _persist_candidate(candidate_path, next_candidate)


def _current_evidence_outcome(
    candidate: Mapping[str, Any],
    key: str,
    allowed_outcomes: set[str],
) -> bool:
    evidence = candidate.get("evidence") or {}
    for item in reversed(evidence.get(key) or []):
        checked_item: Mapping[str, Any] = item
        if (
            isinstance(item, Mapping)
            and key in {"baseline_runs", "verification_runs"}
            and item.get("artifact_path")
        ):
            try:
                loaded = load_evaluation_run(str(item["artifact_path"]))
            except CandidateLoadError:
                continue
            if (
                loaded.get("integrity_status") != "valid"
                or loaded.get("run_id") != item.get("run_id")
                or loaded.get("run_hash") != item.get("run_hash")
            ):
                continue
            try:
                validate_completed_candidate_run_evidence(loaded)
            except CandidateValidationError:
                continue
            checked_item = loaded
        if (
            isinstance(checked_item, Mapping)
            and checked_item.get("candidate_id", candidate.get("id"))
            == candidate.get("id")
            and int(checked_item.get("contract_revision") or -1)
            == int(candidate.get("contract_revision") or 0)
            and checked_item.get("candidate_hash") == candidate.get("candidate_hash")
            and (
                _run_result_status(checked_item)
                if key in {"baseline_runs", "verification_runs"}
                else checked_item.get("outcome")
            )
            in allowed_outcomes
        ):
            return True
    return False


def _candidate_requires_automatic(candidate: Mapping[str, Any]) -> bool:
    return bool(_candidate_automatic_checks(candidate))


def _candidate_reproduction_evidence_satisfied(
    candidate: Mapping[str, Any],
    *,
    reproduced: bool,
) -> bool:
    automatic_required = _candidate_requires_automatic(candidate)
    manual_required = _candidate_requires_manual(candidate)
    automatic_ok = _current_evidence_outcome(
        candidate,
        "baseline_runs",
        {"fail" if reproduced else "pass"},
    )
    manual_ok = _current_evidence_outcome(
        candidate,
        "manual_reproductions",
        {"reproduced" if reproduced else "not_reproduced"},
    )
    return (
        (not automatic_required or automatic_ok)
        and (not manual_required or manual_ok)
        and (automatic_required or manual_required)
    )


def _candidate_verification_evidence_satisfied(
    candidate: Mapping[str, Any],
) -> bool:
    automatic_required = _candidate_requires_automatic(candidate)
    manual_required = _candidate_requires_manual(candidate)
    return (
        (
            not automatic_required
            or _current_evidence_outcome(
                candidate,
                "verification_runs",
                {"pass"},
            )
        )
        and (
            not manual_required
            or _current_evidence_outcome(
                candidate,
                "manual_verifications",
                {"passed"},
            )
        )
        and (automatic_required or manual_required)
    )


def transition_regression_candidate(
    path: str | Path,
    *,
    to_status: str,
    expected_record_revision: int,
    actor: str = "local_operator",
    reason: str,
) -> dict[str, Any]:
    """Move a candidate through the explicit lifecycle after checking evidence."""
    candidate_path = Path(path)
    reason = str(reason or "").strip()
    with _CANDIDATE_WRITE_LOCK:
        candidate = load_regression_candidate(candidate_path)
        _require_candidate_revision(candidate, expected_record_revision)
        from_status = str(candidate.get("triage_status"))
        allowed_pairs = {
            ("new", "triaged"),
            ("triaged", "needs_expectation"),
            ("needs_expectation", "ready"),
            ("ready", "reproduced"),
            ("reproduced", "fixing"),
            ("fixing", "verified"),
            ("verified", "closed"),
            ("closed", "triaged"),
        }
        early_statuses = {"new", "triaged", "needs_expectation", "ready"}
        if (from_status, to_status) not in allowed_pairs:
            if not (
                from_status in early_statuses
                and to_status in {"duplicate", "rejected"}
            ) and not (from_status == "ready" and to_status == "not_reproducible"):
                raise CandidateTransitionError(
                    f"transition {from_status} -> {to_status} is not allowed"
                )

        if to_status == "triaged" and from_status == "new":
            if (
                candidate.get("severity") not in {"S1", "S2", "S3", "S4"}
                or candidate.get("impact_area") not in _IMPACT_AREAS
                or not str(candidate.get("impact_summary") or "").strip()
                or candidate.get("operator_decision") == "unreviewed"
            ):
                raise CandidateTransitionError("triage fields are incomplete")
        elif to_status == "needs_expectation" and from_status == "triaged":
            if candidate.get("operator_decision") != "accepted":
                raise CandidateTransitionError("candidate must be accepted")
        elif to_status == "ready":
            _validate_active_checks(candidate)
            _require_candidate_reproduction_readiness(candidate)
            if not candidate.get("expected_approved_at"):
                raise CandidateTransitionError("expected behavior is not approved")
        elif to_status == "reproduced":
            reproduced = _candidate_reproduction_evidence_satisfied(
                candidate,
                reproduced=True,
            )
            if not reproduced:
                raise CandidateTransitionError(
                    "all current automatic and manual reproduction evidence is required"
                )
        elif to_status == "fixing" and not reason:
            raise CandidateTransitionError("fix start reason is required")
        elif to_status == "verified":
            verified = _candidate_verification_evidence_satisfied(
                candidate
            )
            if not verified:
                raise CandidateTransitionError(
                    "all current automatic and manual verification evidence is required"
                )
        elif to_status == "closed":
            if (
                not str(candidate.get("fixed_in_version") or "").strip()
                or not str(candidate.get("closure_reason") or "").strip()
                or not (
                    candidate.get("suite_case_id")
                    or candidate.get("suite_exclusion_reason")
                )
            ):
                raise CandidateTransitionError("closure evidence is incomplete")
        elif to_status == "duplicate":
            duplicate_id = str(candidate.get("duplicate_of") or "")
            if not duplicate_id or duplicate_id == candidate.get("id") or not reason:
                raise CandidateTransitionError(
                    "a different duplicate target and reason are required"
                )
            duplicate_path = (
                candidate_path.parent
                / f"{safe_artifact_token(duplicate_id)}.json"
            )
            try:
                duplicate_candidate = load_regression_candidate(duplicate_path)
            except CandidateLoadError as exc:
                raise CandidateTransitionError(
                    "duplicate target is missing or invalid"
                ) from exc
            if duplicate_candidate.get("id") != duplicate_id:
                raise CandidateTransitionError(
                    "duplicate target identity does not match"
                )
        elif to_status == "rejected":
            if candidate.get("operator_decision") != "rejected" or not reason:
                raise CandidateTransitionError("rejection decision and reason are required")
        elif to_status == "not_reproducible":
            not_reproduced = _candidate_reproduction_evidence_satisfied(
                candidate,
                reproduced=False,
            )
            if not not_reproduced or not reason:
                raise CandidateTransitionError(
                    "current non-reproducing evidence and reason are required"
                )

        next_candidate = dict(candidate)
        changed_fields = ["triage_status"]
        next_candidate["triage_status"] = to_status
        next_candidate["record_revision"] = int(candidate["record_revision"]) + 1
        next_candidate["updated_at"] = _utc_iso_now()
        if from_status == "closed" and to_status == "triaged":
            if not reason:
                raise CandidateTransitionError("reopen reason is required")
            for key in (
                "expected_approved_at",
                "expected_approved_by",
                "fixed_in_version",
                "closure_reason",
                "suite_case_id",
                "suite_exclusion_reason",
            ):
                next_candidate[key] = None
                changed_fields.append(key)
            next_candidate["contract_revision"] = (
                int(candidate["contract_revision"]) + 1
            )
            changed_fields.append("contract_revision")
            next_candidate["candidate_hash"] = compute_candidate_hash(next_candidate)

        next_candidate["history"] = list(candidate.get("history") or []) + [
            _candidate_history_event(
                next_candidate,
                event_type="candidate_transitioned",
                from_status=from_status,
                to_status=to_status,
                changed_fields=changed_fields,
                reason=reason,
                actor=actor,
            )
        ]
        return _persist_candidate(candidate_path, next_candidate)


def build_candidate_action_state(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Describe operator actions without duplicating lifecycle rules in the UI."""
    canonical = canonicalize_regression_candidate(candidate)
    status = str(canonical.get("triage_status"))
    verification_type = canonical.get("verification_type")
    expectation_valid = True
    expectation_error = ""
    try:
        _validate_active_checks(canonical)
    except CandidateValidationError as exc:
        expectation_valid = False
        expectation_error = str(exc)
    reproduction_readiness = assess_candidate_reproduction_readiness(
        canonical
    )
    automatic_required = _candidate_requires_automatic(canonical)
    manual_required = _candidate_requires_manual(canonical)

    actions = {
        "save_triage": {"enabled": status == "new", "reason": ""},
        "mark_triaged": {
            "enabled": status == "new"
            and canonical.get("severity") in {"S1", "S2", "S3", "S4"}
            and canonical.get("impact_area") in _IMPACT_AREAS
            and bool(str(canonical.get("impact_summary") or "").strip())
            and canonical.get("operator_decision") != "unreviewed",
            "reason": "분류 필드를 먼저 저장해야 합니다.",
        },
        "request_expectation": {
            "enabled": status == "triaged"
            and canonical.get("operator_decision") == "accepted",
            "reason": "처리 결정이 accepted여야 합니다.",
        },
        "save_contract": {
            "enabled": status
            in {"needs_expectation", "ready", "reproduced", "fixing", "verified"},
            "reason": "",
        },
        "approve_expectation": {
            "enabled": status == "needs_expectation" and expectation_valid,
            "reason": expectation_error,
        },
        "mark_ready": {
            "enabled": status == "needs_expectation"
            and expectation_valid
            and bool(canonical.get("expected_approved_at"))
            and reproduction_readiness["ready"],
            "reason": (
                str(reproduction_readiness["reason"])
                if not reproduction_readiness["ready"]
                else "기대 결과 승인과 유효한 검사 항목이 필요합니다."
            ),
        },
        "run_baseline": {
            "enabled": status == "ready"
            and automatic_required,
            "reason": "자동 경성 검사가 있는 후보만 수정 전 실행할 수 있습니다.",
        },
        "record_manual_reproduction": {
            "enabled": status == "ready"
            and manual_required,
            "reason": "",
        },
        "mark_reproduced": {
            "enabled": status == "ready"
            and _candidate_reproduction_evidence_satisfied(
                canonical,
                reproduced=True,
            ),
            "reason": "현재 계약의 모든 자동·수동 재현 증거가 필요합니다.",
        },
        "mark_not_reproducible": {
            "enabled": status == "ready"
            and _candidate_reproduction_evidence_satisfied(
                canonical,
                reproduced=False,
            ),
            "reason": "현재 계약의 모든 자동·수동 비재현 증거가 필요합니다.",
        },
        "create_handoff": {
            "enabled": status in {"reproduced", "fixing"}
            and automatic_required
            and _current_evidence_outcome(
                canonical, "baseline_runs", {"fail"}
            ),
            "reason": "현재 계약의 수정 전 실패가 필요합니다.",
        },
        "start_fixing": {
            "enabled": status == "reproduced",
            "reason": "",
        },
        "run_verification": {
            "enabled": status == "fixing"
            and automatic_required,
            "reason": "",
        },
        "record_manual_verification": {
            "enabled": status == "fixing"
            and manual_required,
            "reason": "",
        },
        "mark_verified": {
            "enabled": status == "fixing"
            and _candidate_verification_evidence_satisfied(canonical),
            "reason": "현재 계약의 모든 자동·수동 통과 증거가 필요합니다.",
        },
        "save_closure": {"enabled": status == "verified", "reason": ""},
        "close": {
            "enabled": status == "verified"
            and bool(str(canonical.get("fixed_in_version") or "").strip())
            and bool(str(canonical.get("closure_reason") or "").strip())
            and bool(
                canonical.get("suite_case_id")
                or canonical.get("suite_exclusion_reason")
            ),
            "reason": "수정 버전·종료 사유·회귀 편입 결과가 필요합니다.",
        },
        "reopen": {"enabled": status == "closed", "reason": ""},
    }
    primary_action = {
        "new": "mark_triaged",
        "triaged": "request_expectation",
        "needs_expectation": "mark_ready",
        "ready": (
            "run_baseline"
            if automatic_required
            else "record_manual_reproduction"
        ),
        "reproduced": "start_fixing",
        "fixing": (
            "run_verification"
            if automatic_required
            else "record_manual_verification"
        ),
        "verified": "close",
        "closed": "reopen",
    }.get(status)
    blocked_reason = ""
    if primary_action and not actions[primary_action]["enabled"]:
        blocked_reason = str(actions[primary_action].get("reason") or "")

    # Stable flattened keys are the public operator-UI contract.  The richer
    # action objects above remain available for labels, disabled reasons, and
    # backward-compatible callers.
    actions.update(
        {
            "can_run_baseline": actions["run_baseline"]["enabled"],
            "can_record_manual_reproduction": actions[
                "record_manual_reproduction"
            ]["enabled"],
            "can_run_verification": actions["run_verification"]["enabled"],
            "can_record_manual_verification": actions[
                "record_manual_verification"
            ]["enabled"],
            "can_preview_handoff": actions["create_handoff"]["enabled"],
            "can_start_fixing": actions["start_fixing"]["enabled"],
            "can_mark_not_reproducible": actions[
                "mark_not_reproducible"
            ]["enabled"],
            "blocked_reason": blocked_reason,
        }
    )
    return actions


def build_candidate_evaluation_case(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Adapt an approved candidate contract to the existing evaluator shape."""
    canonical = canonicalize_regression_candidate(candidate)
    if not canonical.get("expected_approved_at"):
        raise CandidateValidationError("candidate expectation is not approved")
    automatic_checks = _candidate_automatic_checks(canonical)
    if not automatic_checks:
        raise CandidateValidationError(
            "automatic evaluation requires automatic hard checks"
        )
    _validate_active_checks(canonical)
    reproduction_input = (canonical.get("observed") or {}).get(
        "reproduction_input"
    ) or {}
    expected = canonical.get("expected") or {}
    normalized_reproduction_input = _normalize_reproduction_input(reproduction_input)
    return {
        "id": canonical["id"],
        "question": reproduction_input.get("question"),
        "expected_route": expected.get("route"),
        "expected_filters": expected.get("filters") or {},
        "expected_sources": expected.get("sources") or [],
        "expected_state": expected.get("state") or {},
        "expected_answer_requirements": expected.get(
            "answer_requirements"
        )
        or [],
        "evaluation_profile": _candidate_validation_plan(canonical)[
            "quality_profile"
        ],
        "reproduction_input": normalized_reproduction_input,
        "active_checks": automatic_checks,
        "candidate_id": canonical["id"],
        "contract_revision": canonical["contract_revision"],
        "candidate_hash": canonical["candidate_hash"],
    }


def compute_evaluation_run_hash(run: Mapping[str, Any]) -> str:
    payload = dict(run)
    for key in ("run_hash", "json_path", "integrity_status", "warnings"):
        payload.pop(key, None)
    try:
        return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    except (TypeError, ValueError) as exc:
        raise CandidateValidationError(
            "evaluation run contains a non-finite or unsupported JSON value"
        ) from exc


def _persist_evaluation_run(path: Path, run: Mapping[str, Any]) -> dict[str, Any]:
    stored = dict(run)
    for key in ("json_path", "integrity_status", "warnings"):
        stored.pop(key, None)
    artifact_io.atomic_write_json(path, stored)
    result = dict(run)
    result["json_path"] = str(path)
    result["integrity_status"] = "valid"
    return result


def _validate_candidate_run_provenance(
    candidate: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    run_kind: str,
) -> tuple[dict[str, Any], str | None]:
    legacy_fields = {
        "backend_mode",
        "snapshot_id",
        "snapshot_available",
        "data_revision",
        "config_fingerprint",
    }
    contract_schema_version = int(
        candidate.get("contract_schema_version") or 1
    )
    required_fields = set(legacy_fields)
    if contract_schema_version >= 2:
        required_fields.add("reproduction_manifest")
    if not isinstance(provenance, Mapping) or set(
        provenance
    ) != required_fields:
        raise CandidateValidationError(
            "provenance must contain the exact required fields"
        )
    normalized = dict(provenance)
    if contract_schema_version < 2:
        return normalized, None
    try:
        actual_manifest = (
            reproduction_manifest_module
            .require_complete_reproduction_manifest(
                provenance.get("reproduction_manifest")
            )
        )
        expected_manifest = (
            reproduction_manifest_module
            .require_complete_reproduction_manifest(
                candidate.get("reproduction_manifest")
            )
        )
    except reproduction_manifest_module.ReproductionManifestError as exc:
        raise CandidateValidationError(str(exc)) from exc
    normalized["reproduction_manifest"] = actual_manifest
    comparable_fields = (
        (
            "app_version",
            "code_revision",
            "code_fingerprint",
            "model_fingerprint",
            "prompt_fingerprint",
            "tool_fingerprint",
            "data_revision",
            "index_revision",
            "config_fingerprint",
            "feature_flags_fingerprint",
        )
        if run_kind == "baseline"
        else (
            "model_fingerprint",
            "data_revision",
            "index_revision",
            "config_fingerprint",
            "feature_flags_fingerprint",
        )
    )
    mismatched_fields = [
        field
        for field in comparable_fields
        if actual_manifest.get(field)
        != expected_manifest.get(field)
    ]
    mismatch = (
        "reproduction_manifest_mismatch:"
        + ",".join(mismatched_fields)
        if mismatched_fields
        else None
    )
    return normalized, mismatch


def run_candidate_evaluation(
    candidate: Mapping[str, Any],
    invoke_fn: Callable[..., dict[str, Any]],
    *,
    output_dir: str | Path,
    run_kind: str,
    provenance: Mapping[str, Any],
    latency_threshold_seconds: float = 30.0,
    evaluation_profile: str | None = None,
) -> dict[str, Any]:
    """Run one approved candidate as a baseline or verification attempt."""
    if run_kind not in {"baseline", "verification"}:
        raise CandidateValidationError("run_kind is invalid")
    canonical = canonicalize_regression_candidate(candidate)
    required_status = "ready" if run_kind == "baseline" else "fixing"
    if canonical.get("triage_status") != required_status:
        raise CandidateValidationError(
            f"{run_kind} is not available in {canonical.get('triage_status')}"
        )
    _require_candidate_reproduction_readiness(canonical)
    case = build_candidate_evaluation_case(canonical)
    normalized_provenance, provenance_blocker = (
        _validate_candidate_run_provenance(
            canonical,
            provenance,
            run_kind=run_kind,
        )
    )

    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    created_at = _utc_iso_now()
    plan = _candidate_validation_plan(canonical)
    case_profile = _resolve_evaluation_profile(
        str(case.get("evaluation_profile"))
        if case.get("evaluation_profile")
        else None
    )
    requested_profile = (
        _resolve_evaluation_profile(evaluation_profile)
        if evaluation_profile is not None
        else case_profile
    )
    if (
        int(canonical.get("contract_schema_version") or 1) >= 2
        and requested_profile != plan["quality_profile"]
    ):
        raise CandidateValidationError(
            "evaluation profile does not match the approved contract"
        )
    evaluation_profile = case_profile
    blocked_reason = (
        "snapshot_unavailable"
        if normalized_provenance.get("snapshot_available") is False
        else provenance_blocker
    )
    contract_schema_version = int(
        canonical.get("contract_schema_version") or 1
    )
    evaluation_checks = _candidate_automatic_checks(canonical)
    run: dict[str, Any] = {
        "schema_version": 2,
        "run_id": run_id,
        "run_kind": run_kind,
        "run_status": "blocked" if blocked_reason else "running",
        "created_at": created_at,
        "evaluation_profile": evaluation_profile,
        "execution_mode": (
            "feedback_candidate_baseline"
            if run_kind == "baseline"
            else "feedback_candidate_verification"
        ),
        "candidate_id": canonical["id"],
        "contract_revision": canonical["contract_revision"],
        "candidate_hash": canonical["candidate_hash"],
        "contract_schema_version": contract_schema_version,
        "expected_approved_at": canonical["expected_approved_at"],
        "app_version": get_app_version(),
        "provenance": normalized_provenance,
        "active_checks": list(canonical["active_checks"]),
        "summary": {},
        "results": [],
    }
    if contract_schema_version >= 2:
        run.update(
            {
                "quality_profile": plan["quality_profile"],
                "validation_plan": {
                    key: plan[key]
                    for key in (
                        "schema_version",
                        "quality_profile",
                        "hard_checks",
                        "soft_objectives",
                        "performance_budget",
                    )
                },
                "evaluation_checks": evaluation_checks,
                "reproduction_manifest_hash": (
                    normalized_provenance.get(
                        "reproduction_manifest"
                    )
                    or {}
                ).get("manifest_hash"),
            }
        )
    if blocked_reason:
        run["blocked_reason"] = blocked_reason
    else:
        try:
            reproduction_payload = _normalize_reproduction_input(
                (canonical.get("observed") or {}).get("reproduction_input")
            )
            if not reproduction_payload.get("question"):
                reproduction_payload["question"] = case.get("question") or ""
            warmup_runs = (
                int(plan["performance_budget"]["warmup_runs"])
                if contract_schema_version >= 2
                else 0
            )
            measured_runs = (
                int(plan["performance_budget"]["min_runs"])
                if contract_schema_version >= 2
                else 1
            )
            for run_index in range(warmup_runs):
                invoke_fn(
                    _normalize_reproduction_input(
                        reproduction_payload
                    ),
                    config={
                        "configurable": {
                            "thread_id": (
                                f"feedback_{run_kind}_{run_id}_"
                                f"{canonical['id']}_warmup_{run_index + 1}"
                            )
                        }
                    },
                )
            results: list[dict[str, Any]] = []
            for run_index in range(measured_runs):
                started = time.perf_counter()
                final_state = invoke_fn(
                    _normalize_reproduction_input(
                        reproduction_payload
                    ),
                    config={
                        "configurable": {
                            "thread_id": (
                                f"feedback_{run_kind}_{run_id}_"
                                f"{canonical['id']}_{run_index + 1}"
                            )
                        }
                    },
                )
                latency = time.perf_counter() - started
                result = evaluate_dataset_case_result(
                    case,
                    final_state,
                    latency_seconds=latency,
                    latency_threshold_seconds=latency_threshold_seconds,
                    evaluation_profile=evaluation_profile,
                )
                if contract_schema_version >= 2:
                    result["sample_index"] = run_index + 1
                results.append(result)
            run["run_status"] = "completed"
            run["results"] = results
            run["summary"] = (
                _summarize_candidate_run_results(
                    results,
                    canonical,
                )
                if contract_schema_version >= 2
                else _summarize_eval_results(results)
            )
            if contract_schema_version >= 2:
                run["summary"]["warmup_runs"] = warmup_runs
        except Exception as exc:  # graph errors become auditable safe attempts
            run["run_status"] = "error"
            run["error_type"] = type(exc).__name__
            run["error_stage"] = "graph_invoke"
    run["run_hash"] = compute_evaluation_run_hash(run)
    output_path = Path(output_dir)
    json_path = output_path / f"evaluation_run_{run_id}.json"
    return _persist_evaluation_run(json_path, run)


def load_evaluation_run(path: str | Path) -> dict[str, Any]:
    """Load a schema-v2 evaluation run and verify its persisted hash."""
    run_path = Path(path)
    try:
        payload = strict_json_loads(run_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CandidateLoadError(f"cannot load evaluation run: {run_path}") from exc
    if not isinstance(payload, Mapping):
        raise CandidateLoadError("evaluation run JSON root must be an object")
    if payload.get("schema_version") != 2:
        raise CandidateLoadError("unsupported_monitoring_schema")
    persisted_hash = payload.get("run_hash")
    if not persisted_hash:
        raise CandidateLoadError("run_hash_missing")
    expected_hash = compute_evaluation_run_hash(payload)
    if persisted_hash != expected_hash:
        raise CandidateLoadError("run_hash_mismatch")
    result = dict(payload)
    result["json_path"] = str(run_path)
    result["integrity_status"] = "valid"
    return result


def list_candidate_evaluation_run_artifacts(
    run_dir: str | Path,
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Discover candidate runs from disk without process-local state."""
    root = Path(run_dir)
    if not root.exists():
        return {"items": [], "warnings": []}
    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for path in root.glob("evaluation_run_*.json"):
        try:
            run = load_evaluation_run(path)
        except CandidateLoadError as exc:
            warnings.append(
                {
                    "code": (
                        "run_hash_mismatch"
                        if "run_hash_mismatch" in str(exc)
                        else "malformed_run"
                    ),
                    "path": str(path),
                    "blocking": True,
                }
            )
            continue
        if candidate_id is not None and run.get("candidate_id") != candidate_id:
            continue
        items.append(run)
    return {
        "items": sorted(
            items,
            key=lambda run: str(run.get("created_at") or ""),
            reverse=True,
        ),
        "warnings": warnings,
    }


def discover_candidate_orphan_runs(
    candidate: Mapping[str, Any],
    *,
    run_dir: str | Path,
) -> dict[str, Any]:
    """Classify unlinked disk runs for safe operator recovery."""
    canonical = canonicalize_regression_candidate(candidate)
    discovered = list_candidate_evaluation_run_artifacts(
        run_dir,
        candidate_id=str(canonical["id"]),
    )
    evidence = canonical.get("evidence") or {}
    linked_hashes = {
        item.get("run_hash")
        for key in ("baseline_runs", "verification_runs")
        for item in evidence.get(key) or []
    }
    result = {
        "attachable": [],
        "stale": [],
        "failed_attempts": [],
        "blocked_attempts": [],
        "warnings": discovered["warnings"],
    }
    for run in discovered["items"]:
        if run.get("run_hash") in linked_hashes:
            continue
        if run.get("run_status") == "error":
            result["failed_attempts"].append(run)
        elif run.get("run_status") == "blocked":
            result["blocked_attempts"].append(run)
        elif (
            run.get("run_status") == "completed"
            and run.get("integrity_status") == "valid"
            and is_current_candidate_contract(canonical, run)
        ):
            result["attachable"].append(run)
        else:
            result["stale"].append(run)
    return result


def list_regression_candidates(candidate_dir: str | Path) -> list[dict[str, Any]]:
    """Return valid schema-v2 candidates."""
    return list_regression_candidate_artifacts(candidate_dir)["items"]


def build_regression_candidate_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Regression candidate table에 표시할 안전한 요약 row를 만듭니다."""
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        draft = candidate.get("eval_case_draft") or {}
        expected = candidate.get("expected") or {}
        reproduction_input = (candidate.get("observed") or {}).get(
            "reproduction_input"
        ) or {}
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
                "draft_question": _safe_preview(
                    reproduction_input.get("question") or draft.get("question"),
                    100,
                ),
                "expected_route": expected.get("route")
                if expected
                else draft.get("expected_route"),
                "expected_filters": expected.get("filters")
                if expected
                else draft.get("expected_filters") or {},
                "expected_source_count": len(
                    expected.get("sources")
                    if expected
                    else draft.get("expected_sources") or []
                ),
                "record_revision": candidate.get("record_revision", 0),
                "contract_revision": candidate.get("contract_revision", 0),
                "expectation_approved": bool(
                    candidate.get("expected_approved_at")
                ),
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
    cases: list[dict[str, Any]] = []
    for candidate in candidates:
        if selected and candidate.get("id") not in selected:
            continue
        if candidate.get("schema_version") == 2:
            if candidate.get("expected_approved_at"):
                try:
                    cases.append(build_candidate_evaluation_case(candidate))
                except CandidateValidationError:
                    continue
            elif candidate.get("eval_case_draft"):
                # Compatibility-only diagnostic execution. It cannot be attached
                # as lifecycle evidence through record_candidate_run().
                cases.append(candidate["eval_case_draft"])
        elif candidate.get("eval_case_draft"):
            cases.append(candidate["eval_case_draft"])
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
    """Promote one report idempotently within this process."""
    with _CANDIDATE_WRITE_LOCK:
        return _promote_issue_report_to_eval_candidate_locked(
            report,
            output_dir=output_dir,
        )


def _promote_issue_report_to_eval_candidate_locked(
    report: dict[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    """issue report를 regression/evaluation candidate artifact로 저장합니다."""
    from src.core.issue_report_store import canonicalize_report

    canonical_report = canonicalize_report(report)
    candidate_id = f"candidate_{canonical_report['id']}"
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    json_path = output_path / f"{safe_artifact_token(candidate_id)}.json"
    if json_path.exists():
        existing = load_regression_candidate(json_path)
        expected_ref = {"kind": "report", "id": canonical_report["id"]}
        if expected_ref not in existing.get("source_refs", []):
            raise CandidateConflictError(
                "candidate path already belongs to a different source report"
            )
        return existing

    report_observed = canonical_report.get("observed") or {}
    report_reproduction_input = _normalize_reproduction_input(
        report_observed.get("reproduction_input")
    )
    if canonical_report.get("report_target_type") == "ui_or_system":
        report_reproduction_input.setdefault(
            "scenario",
            str(canonical_report.get("comment") or "").strip(),
        )
        report_reproduction_input["report_target_type"] = (
            "ui_or_system"
        )
    if not report_reproduction_input.get("chat_history"):
        report_reproduction_input["chat_history"] = _normalize_chat_history(
            report_observed.get("legacy_conversation")
        )
    eval_case_draft = build_eval_case_draft_from_issue_report(canonical_report)
    if not report_reproduction_input.get("prior_search_scope"):
        extracted_scope = _extract_reproduction_scope_from_trace(
            report_observed.get("trace")
        )
        if extracted_scope:
            report_reproduction_input["prior_search_scope"] = extracted_scope
    actual: dict[str, Any] = {}
    reproduction_input: dict[str, Any] = {}
    if eval_case_draft:
        reproduction_input = dict(report_reproduction_input)
        reproduction_input.setdefault("question", eval_case_draft.get("question") or "")
        actual = {
            "route": eval_case_draft.get("expected_route"),
            "filters": dict(eval_case_draft.get("expected_filters") or {}),
            "sources": [
                dict(source)
                for source in eval_case_draft.get("expected_sources") or []
                if isinstance(source, Mapping)
            ],
            "state": dict(eval_case_draft.get("expected_state") or {}),
        }
    else:
        reproduction_input = report_reproduction_input
    created_at = _utc_iso_now()
    impact_area = infer_issue_impact_area(canonical_report)
    new_contract = int(
        canonical_report.get("report_contract_version") or 1
    ) >= 2
    quality_profile = (
        "speed_first"
        if impact_area == "latency"
        else "balanced"
        if impact_area == "ui"
        else "accuracy_first"
    )
    contract_fields: dict[str, Any] = {}
    if new_contract:
        contract_fields = {
            "contract_schema_version": 2,
            "quality_profile": quality_profile,
            "validation_plan": {
                "schema_version": 1,
                "quality_profile": quality_profile,
                "hard_checks": [],
                "soft_objectives": list(
                    QUALITY_PROFILE_RULES[quality_profile][
                        "default_soft_objectives"
                    ]
                ),
                "performance_budget": dict(
                    QUALITY_PROFILE_RULES[quality_profile][
                        "default_performance_budget"
                    ]
                ),
            },
            "reproduction_manifest": (
                reproduction_manifest_module
                .build_runtime_reproduction_manifest(
                    data_revision=None,
                    index_revision=None,
                )
            ),
        }
    candidate = canonicalize_regression_candidate({
        "schema_version": 2,
        **contract_fields,
        "id": candidate_id,
        "status": "candidate",
        "source_refs": [{"kind": "report", "id": canonical_report["id"]}],
        "source_snapshots": [],
        "created_at": created_at,
        "updated_at": created_at,
        "record_revision": 0,
        "contract_revision": 0,
        "triage_status": "new",
        "operator_decision": "unreviewed",
        "severity": "untriaged",
        "impact_area": impact_area,
        "impact_summary": "",
        "observed": {
            "reproduction_input": reproduction_input,
            "actual": actual,
            "legacy_eval_draft": eval_case_draft or {},
        },
        "expected": {},
        "expected_approved_at": None,
        "expected_approved_by": None,
        "active_checks": [],
        "verification_type": "graph_contract",
        "evidence": {
            "baseline_runs": [],
            "verification_runs": [],
            "manual_reproductions": [],
            "manual_verifications": [],
        },
        "handoffs": [],
        "fixed_in_version": None,
        "closure_reason": None,
        "duplicate_of": None,
        "suite_case_id": None,
        "suite_exclusion_reason": None,
        "history": [],
        "source": "issue_report",
        "source_report_id": canonical_report["id"],
        "thread_id": canonical_report.get("thread_id"),
        "category": canonical_report.get("category"),
        "source_file_path": canonical_report.get("file_path"),
        "source_json_path": canonical_report.get("json_path"),
        "preview": _issue_report_preview(
            canonical_report.get("content")
            or canonical_report.get("comment")
            or "",
            max_chars=500,
        ),
        "recommended_next_step": "review_eval_case_draft" if eval_case_draft else "manual_eval_case_required",
        # Legacy UI can still display the draft, but v2 execution ignores it.
        "eval_case_draft": eval_case_draft,
    })
    if eval_case_draft:
        candidate["eval_case_draft"] = eval_case_draft
    return _persist_candidate(json_path, candidate)


def is_native_v2_status(status: Mapping[str, Any]) -> bool:
    """Return whether status was sourced from the native retrieval catalog."""

    retrieval = status.get("retrieval")
    return (
        isinstance(retrieval, Mapping)
        and retrieval.get("mode") == "native"
    )


def summarize_v2_data_integrity(
    status: Mapping[str, Any],
) -> dict[str, Any]:
    """Return V2-only problem signals for the monitoring diagnostics UI."""

    retrieval = status.get("retrieval")
    if not is_native_v2_status(status):
        checks = {
            "v2_runtime": {
                "status": "fail",
                "detail": "V2 retrieval status is unavailable",
            }
        }
    else:
        db = status.get("db") or {}
        vector = status.get("vector_db") or {}
        total = int(db.get("total_reports") or 0)
        embedded = int(db.get("embedded_reports") or 0)
        pending = int(db.get("pending_reports") or 0)
        downloaded = int(status.get("downloaded_pdfs") or 0)
        membership = int(retrieval.get("membership_count") or 0)
        ntotal = int(vector.get("ntotal") or 0)
        snapshot_ready = retrieval.get("snapshot_state") == "ready"
        build_ready = retrieval.get("build_state") in {
            "committed_pending_checkpoint",
            "fully_complete",
        }
        cleanup_count = int(
            retrieval.get("pending_cleanup_file_count") or 0
        )
        checks = {
            "native_snapshot": {
                "status": "pass" if snapshot_ready and build_ready else "fail",
                "detail": (
                    f"빌드={retrieval.get('build_state')}, "
                    f"스냅샷={retrieval.get('snapshot_state')}"
                ),
            },
            "native_membership": {
                "status": "pass" if membership == ntotal and ntotal > 0 else "fail",
                "detail": f"카탈로그 {membership}건 / 벡터 {ntotal}건",
            },
            "manifest_backlog": {
                "status": "pass" if pending == 0 else "warning",
                "detail": f"현재 검색 자료에 미반영 {pending}건",
            },
            "pdf_vs_manifest": {
                "status": "pass" if downloaded >= embedded else "warning",
                "detail": f"원문 PDF {downloaded}건 / 활성 보고서 {embedded}건",
            },
            "search_coverage": {
                "status": "pass" if total == 0 or embedded / total >= 0.95 else "warning",
                "detail": f"활성 검색 자료 {embedded}건 / 전체 {total}건",
            },
            "runtime_health": {
                "status": "warning" if retrieval.get("degraded") else "pass",
                "detail": (
                    f"세대={retrieval.get('publication_generation')}, "
                    f"쓰기 epoch={retrieval.get('write_epoch')}, "
                    f"쓰기 가능={retrieval.get('write_enabled')}"
                ),
            },
            "cleanup_backlog": {
                "status": "warning" if cleanup_count else "pass",
                "detail": f"정리 대기 파일 {cleanup_count}개",
            },
        }
    return {
        "checks": checks,
        "pass_count": sum(
            1 for check in checks.values() if check["status"] == "pass"
        ),
        "warning_count": sum(
            1 for check in checks.values() if check["status"] == "warning"
        ),
        "fail_count": sum(
            1 for check in checks.values() if check["status"] == "fail"
        ),
    }


def build_monitoring_page_labels() -> list[str]:
    """Monitoring Mode가 켜졌을 때 보여줄 top-level page를 반환합니다."""
    return ["Chat", "Monitoring"]


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
            "timing": {
                "total_seconds": round(latency_seconds, 3),
            },
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
            "state_snapshot": {
                "available_keys": sorted(str(key) for key in final_state),
                "route": route,
                "search_filters": final_state.get("search_filters") or {},
                "scope_source": final_state.get("scope_source"),
                "no_vector_results": bool(final_state.get("no_vector_results")),
                "memory_retry_attempted": bool(
                    final_state.get("memory_retry_attempted")
                ),
                "has_generation": final_state.get("generation") is not None,
                "has_rdb_result": final_state.get("rdb_result") is not None,
                "selected_source_count": len(rerank_info),
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


def _normalize_chat_history(
    value: Any,
    *,
    limit: int = 12,
) -> list[list[str]]:
    normalized: list[list[str]] = []
    if not isinstance(value, list):
        return normalized
    for message in value[-limit:]:
        if isinstance(message, Mapping):
            role = message.get("role")
            content = message.get("content")
        elif (
            isinstance(message, (list, tuple))
            and len(message) == 2
        ):
            role, content = message
        else:
            continue
        if not isinstance(role, str) or content is None:
            continue
        normalized.append([role, str(content)])
    return normalized


def _normalize_prior_search_scope(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    compact = _compact_scope_for_state_trace(dict(value))
    if not compact:
        return None
    sections = compact.pop("answer_scope_sections", None)
    if isinstance(sections, list):
        compact["answer_scope_index"] = {"sections": sections}
    return compact


def _normalize_reproduction_input(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    normalized: dict[str, Any] = {}
    question = source.get("question")
    if isinstance(question, str):
        question = question.strip()
        if question:
            normalized["question"] = question
    scenario = source.get("scenario")
    if isinstance(scenario, str):
        scenario = scenario.strip()
        if scenario:
            normalized["scenario"] = scenario
    report_target_type = source.get("report_target_type")
    if report_target_type in {"response", "ui_or_system"}:
        normalized["report_target_type"] = report_target_type
    chat_history = source.get("chat_history")
    normalized_chat_history = _normalize_chat_history(chat_history)
    if normalized_chat_history:
        normalized["chat_history"] = normalized_chat_history
    prior_search_scope = source.get("prior_search_scope")
    normalized_scope = _normalize_prior_search_scope(
        prior_search_scope
    )
    if normalized_scope:
        normalized["prior_search_scope"] = normalized_scope
    if source.get("requires_prior_scope") is True:
        normalized["requires_prior_scope"] = True
    return normalized


def _extract_reproduction_scope_from_trace(trace: Any) -> dict[str, Any] | None:
    if not isinstance(trace, Mapping):
        return None
    state_trace = trace.get("state_trace")
    if isinstance(state_trace, Mapping):
        input_state = state_trace.get("input")
        if isinstance(input_state, Mapping):
            prior_search_scope = input_state.get("prior_search_scope")
            if isinstance(prior_search_scope, Mapping):
                return _normalize_prior_search_scope(
                    prior_search_scope
                )
    state_transitions = trace.get("state_transitions")
    if isinstance(state_transitions, Mapping):
        input_state = state_transitions.get("input")
        if isinstance(input_state, Mapping):
            prior_search_scope = input_state.get(
                "prior_search_scope"
            )
            if isinstance(prior_search_scope, Mapping):
                return _normalize_prior_search_scope(
                    prior_search_scope
                )
    return None


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
        "query_ns": existing_metrics.get("query_ns"),
        "row_count": row_count,
        "column_count": column_count,
        "guardrail_blocked": existing_metrics.get("guardrail_blocked"),
        "result_preview": str(raw_result)[:500] if raw_result is not None else None,
    }
