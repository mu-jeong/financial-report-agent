"""Multi-company, retrieval-only fan-out for VectorDB comparisons.

The branch worker in this module deliberately does not write user-visible graph
state.  It only returns compact, primitive candidates which are reduced by a
comparison/attempt/target key and synthesized once at the fan-in boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.prompts import PromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.types import Overwrite, Send

from src.configs.config import (
    SEARCH_TOP_K,
    USE_RERANKER,
    VECTOR_RETRIEVAL_CONCURRENCY,
    get_logger,
)
from src.configs.prompts import VECTORDB_PROMPT
from src.core.metadata_filters import filter_docs_with_scores
from src.llms.factory import build_chat_model
from src.llms.generation_observability import (
    invoke_chat_with_observability,
    merge_generation_metrics,
)
from src.nodes import vectordb
from src.nodes.stock_price import stock_price_tools
from src.retrieval.bootstrap import RetrievalBootstrapError
from src.utils.citations import extract_citation_ranks, remove_unavailable_citations
from src.utils.ranker import get_ranker


MAX_TARGETS = 5
DEFAULT_UNION_CANDIDATE_LIMIT = 60
MAX_CANDIDATE_TEXT_BYTES = 4096
DEFAULT_RETRIEVAL_CONCURRENCY = VECTOR_RETRIEVAL_CONCURRENCY

logger = get_logger(__name__)

SnapshotRevision = dict[str, str | int | None]
Candidate = dict[str, object]


class ComparisonPlan(TypedDict):
    comparison_id: str
    attempt_id: str
    original_query: str
    retrieval_query: str
    targets: list[str]
    shared_filters: dict[str, object]
    expected_revision: SnapshotRevision
    candidate_budget_per_target: int
    union_candidate_limit: int
    final_budget: int
    execution_mode: Literal["send", "sequential_reference"]
    selection_mode: Literal["semantic", "latest_per_target"]
    selected_reports_by_target: dict[str, dict[str, object]]
    retrieval_concurrency_limit: int
    process_retrieval_concurrency_limit: int
    preflight_ns: int


class CompanyBranchInput(TypedDict):
    comparison_id: str
    attempt_id: str
    target_index: int
    target_name: str
    expected_revision: SnapshotRevision
    branch_query: str
    branch_query_sha256: str
    filters: dict[str, object]
    candidate_budget: int
    selected_report: dict[str, object] | None
    retrieval_concurrency_limit: int


class CompanyBranchResult(TypedDict):
    comparison_id: str
    attempt_id: str
    target_index: int
    target_name: str
    status: Literal[
        "success", "success_degraded", "no_result", "failed", "revision_mismatch"
    ]
    actual_revision: SnapshotRevision
    candidates: list[Candidate]
    metrics: dict[str, object]
    error: str | None


BranchKey = tuple[str, str, str]
BranchResults = dict[BranchKey, CompanyBranchResult]


class ComparisonInput(TypedDict, total=False):
    question: str
    rewritten_query: str
    search_filters: dict[str, object] | None
    retrieval_plan: dict[str, object] | None
    expected_revision: SnapshotRevision
    vector_run_id: str
    vector_attempt_id: int


class ComparisonOutput(TypedDict, total=False):
    generation: str | None
    messages: list[BaseMessage]
    rerank_info: list[dict[str, object]]
    no_vector_results: bool
    monitoring_metrics: dict[str, object]
    search_filters: dict[str, object]
    vector_outcome: str
    vector_retryable: bool


class ComparisonState(ComparisonInput, ComparisonOutput, total=False):
    comparison_plan: ComparisonPlan
    comparison_preflight_error: dict[str, str] | None
    branch_input: CompanyBranchInput
    branch_results: Annotated[BranchResults, lambda left, right: keyed_upsert_results(left, right)]


_retrieval_semaphore = threading.BoundedSemaphore(DEFAULT_RETRIEVAL_CONCURRENCY)
_retrieval_counter_lock = threading.Lock()
_retrieval_concurrency_limit = DEFAULT_RETRIEVAL_CONCURRENCY
_retrieval_active = 0


def configure_retrieval_concurrency(limit: int) -> None:
    """Replace the process-wide branch limiter (intended for startup config)."""
    if limit < 1:
        raise ValueError("retrieval concurrency must be at least 1")
    global _retrieval_semaphore, _retrieval_concurrency_limit, _retrieval_active
    _retrieval_semaphore = threading.BoundedSemaphore(limit)
    _retrieval_concurrency_limit = limit
    _retrieval_active = 0


def _revision(value: object) -> SnapshotRevision:
    source = value if isinstance(value, dict) else {}
    return {
        "snapshot_id": source.get("snapshot_id"),
        "publication_generation": source.get("publication_generation"),
        "delta_generation": source.get("delta_generation"),
        "profile_id": source.get("profile_id"),
    }


def _ordered_targets(state: dict) -> list[str]:
    plan = state.get("retrieval_plan") or {}
    filters = state.get("search_filters") or {}
    raw = plan.get("target_names") or filters.get("target_names") or []
    if not raw and filters.get("target_name"):
        raw = [filters["target_name"]]
    if isinstance(raw, str):
        raw = [raw]
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def build_comparison_plan(
    state: dict,
    *,
    execution_mode: Literal["send", "sequential_reference"] | None = None,
) -> ComparisonPlan:
    preflight_started = time.perf_counter_ns()
    targets = _ordered_targets(state)
    if not 2 <= len(targets) <= MAX_TARGETS:
        raise ValueError("company comparison requires between 2 and 5 targets")
    retrieval_plan = state.get("retrieval_plan") or {}
    shared_filters = dict(state.get("search_filters") or {})
    shared_filters.pop("target_name", None)
    shared_filters.pop("target_names", None)
    shared_filters.pop("file_names", None)
    union_limit = min(
        max(int(retrieval_plan.get("union_candidate_limit") or DEFAULT_UNION_CANDIDATE_LIMIT), 1),
        DEFAULT_UNION_CANDIDATE_LIMIT,
    )
    budget = min(
        max(int(retrieval_plan.get("candidate_budget_per_target") or math.ceil(union_limit / len(targets))), 1),
        math.ceil(union_limit / len(targets)),
    )
    expected = state.get("expected_revision") or retrieval_plan.get("expected_revision")
    if not expected:
        # Preparing the plan pins one read view before any dynamic branch starts.
        # Failure is intentionally propagated: dispatching with an unknown
        # expected revision could synthesize from mixed publications.
        expected = vectordb.get_active_retrieval_revision()
    resolved_mode = execution_mode or retrieval_plan.get("execution_mode") or "send"
    if resolved_mode not in {"send", "sequential_reference"}:
        raise ValueError(f"unsupported comparison execution mode: {resolved_mode}")
    selection_mode = str(retrieval_plan.get("selection_mode") or "semantic")
    if selection_mode not in {"semantic", "latest_per_target"}:
        raise ValueError(f"unsupported comparison selection mode: {selection_mode}")
    selected_reports_by_target: dict[str, dict[str, object]] = {}
    if selection_mode == "latest_per_target":
        selection_override = state.get("_selected_reports_by_target")
        if isinstance(selection_override, dict):
            selected_reports_by_target = {
                str(target): dict(report)
                for target, report in selection_override.items()
                if isinstance(report, dict)
            }
        else:
            selected_reports_by_target = vectordb.fetch_latest_reports_by_target(
                targets,
                shared_filters,
            )
    effective_concurrency = (
        1
        if resolved_mode == "sequential_reference"
        else min(_retrieval_concurrency_limit, len(targets))
    )
    comparison_id = (
        retrieval_plan.get("comparison_id")
        or state.get("vector_run_id")
        or uuid.uuid4()
    )
    attempt_id = retrieval_plan.get("attempt_id")
    if attempt_id is None:
        attempt_id = state.get("vector_attempt_id")
    if attempt_id is None:
        attempt_id = 0
    return {
        "comparison_id": str(comparison_id),
        "attempt_id": str(attempt_id),
        "original_query": str(state.get("question") or ""),
        "retrieval_query": str(
            state.get("rewritten_query") or state.get("question") or ""
        ),
        "targets": targets,
        "shared_filters": shared_filters,
        "expected_revision": _revision(expected),
        "candidate_budget_per_target": budget,
        "union_candidate_limit": union_limit,
        "final_budget": min(
            max(int(retrieval_plan.get("final_budget") or SEARCH_TOP_K), 1),
            SEARCH_TOP_K,
        ),
        "execution_mode": resolved_mode,
        "selection_mode": selection_mode,
        "selected_reports_by_target": selected_reports_by_target,
        "retrieval_concurrency_limit": effective_concurrency,
        "process_retrieval_concurrency_limit": _retrieval_concurrency_limit,
        "preflight_ns": max(0, time.perf_counter_ns() - preflight_started),
    }


def keyed_upsert_results(left: BranchResults | None, right: BranchResults | None) -> BranchResults:
    """Associative, idempotent branch-result reducer for retry/pending writes."""
    merged = dict(left or {})
    for key, result in (right or {}).items():
        merged[key] = result
    return merged


def _compact_target_aliases(target: str) -> set[str]:
    canonical = str(target or "").strip()
    compact = "".join(canonical.casefold().split())
    aliases = {compact} if compact else set()
    for index in range(1, len(compact)):
        if (
            compact[index - 1].isascii()
            and compact[index - 1].isalnum()
            and not compact[index].isascii()
        ):
            aliases.add(compact[index:])
    return aliases


def _recognized_target_spans(
    query: str, targets: list[str]
) -> list[tuple[int, int, str]]:
    compact_chars: list[str] = []
    original_indexes: list[int] = []
    for index, char in enumerate(str(query or "")):
        if char.isspace():
            continue
        compact_chars.append(char.casefold())
        original_indexes.append(index)
    compact_query = "".join(compact_chars)

    owners: dict[str, set[str]] = {}
    for target in targets:
        for alias in _compact_target_aliases(target):
            owners.setdefault(alias, set()).add(target)

    mentions: list[tuple[int, int, int, str]] = []
    for alias, alias_owners in owners.items():
        if not alias or len(alias_owners) != 1:
            continue
        owner = next(iter(alias_owners))
        compact_start = compact_query.find(alias)
        while compact_start >= 0:
            compact_end = compact_start + len(alias)
            mentions.append(
                (
                    original_indexes[compact_start],
                    original_indexes[compact_end - 1] + 1,
                    len(alias),
                    owner,
                )
            )
            compact_start = compact_query.find(alias, compact_start + 1)

    selected: list[tuple[int, int, str]] = []
    occupied_until = -1
    for start, end, length, owner in sorted(
        mentions,
        key=lambda item: (item[0], -item[2], item[3]),
    ):
        if start < occupied_until:
            continue
        selected.append((start, end, owner))
        occupied_until = end
    return selected


def build_target_branch_query(
    retrieval_query: str,
    active_target: str,
    targets: list[str],
) -> tuple[str, str]:
    """Build one clean target query while preserving the shared user intent."""
    query = str(retrieval_query or "")
    spans = _recognized_target_spans(query, targets)
    for start, end, _owner in reversed(spans):
        query = query[:start] + query[end:]
    query = re.sub(r"[,，;/|]+", " ", query)
    query = re.sub(
        r"(?<!\S)(?:와|과|및|의|각각|and)(?!\S)",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\s+", " ", query).strip()
    query = f"{active_target} {query}".strip()
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    return query, digest


def _branch_inputs(plan: ComparisonPlan) -> list[CompanyBranchInput]:
    inputs: list[CompanyBranchInput] = []
    for target_index, target_name in enumerate(plan["targets"]):
        filters = dict(plan["shared_filters"])
        filters["target_name"] = target_name
        selected_report = plan["selected_reports_by_target"].get(target_name)
        if plan["selection_mode"] == "latest_per_target":
            file_name = (selected_report or {}).get("file_name")
            filters["file_names"] = [str(file_name)] if file_name else []
        branch_query, branch_query_sha256 = build_target_branch_query(
            plan["retrieval_query"],
            target_name,
            plan["targets"],
        )
        inputs.append(
            {
                "comparison_id": plan["comparison_id"],
                "attempt_id": plan["attempt_id"],
                "target_index": target_index,
                "target_name": target_name,
                "expected_revision": plan["expected_revision"],
                "branch_query": branch_query,
                "branch_query_sha256": branch_query_sha256,
                "filters": filters,
                "candidate_budget": plan["candidate_budget_per_target"],
                "selected_report": selected_report,
                "retrieval_concurrency_limit": plan["retrieval_concurrency_limit"],
            }
        )
    return inputs


def _truncate_utf8(text: object, limit: int = MAX_CANDIDATE_TEXT_BYTES) -> str:
    encoded = str(text or "").encode("utf-8")
    if len(encoded) <= limit:
        return encoded.decode("utf-8")
    return encoded[:limit].decode("utf-8", errors="ignore")


def _primitive_payload(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _primitive_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_primitive_payload(item) for item in value]
    return str(value)


def _stable_identity(meta: dict[str, object], fallback_index: int) -> str:
    for key in ("physical_id", "chunk_uid", "parent_uid", "report_uid"):
        if meta.get(key):
            return f"{key}:{meta[key]}"
    return "fallback:" + "|".join(
        str(meta.get(key) or "")
        for key in ("file_name", "span_start", "span_end", "child_index")
    ) + f"|{fallback_index}"


def _dedupe_identity(doc: object, fallback_index: int) -> str:
    meta = dict(getattr(doc, "metadata", {}) or {})
    for key in ("parent_uid", "physical_id", "chunk_uid", "report_uid"):
        if meta.get(key) is not None:
            return f"{key}:{meta[key]}"
    source_parts = tuple(
        str(meta.get(key) or "")
        for key in ("file_name", "span_start", "span_end", "child_index")
    )
    if any(source_parts):
        return "source:" + "|".join(source_parts)
    text_hash = hashlib.sha256(
        str(getattr(doc, "page_content", "") or "").encode("utf-8")
    ).hexdigest()
    return f"text:{text_hash}"


_META_KEYS = (
    "target_name", "report_date", "title", "broker", "report_type", "file_name",
    "report_uid", "chunk_uid", "parent_uid", "profile_id", "child_index", "span_start",
    "span_end", "physical_id", "snapshot_id", "publication_generation",
)


def _candidate(doc: object, score: object, index: int, target_name: str) -> Candidate:
    meta = dict(getattr(doc, "metadata", {}) or {})
    compact_meta = {
        key: _primitive_payload(meta.get(key))
        for key in _META_KEYS
        if meta.get(key) is not None
    }
    compact_meta["target_name"] = target_name
    return {
        "stable_id": _stable_identity(meta, index),
        "retrieval_rank": index,
        "target_name": target_name,
        "text": _truncate_utf8(getattr(doc, "page_content", "")),
        "score": float(score),
        "meta": compact_meta,
    }


def _revision_conflicts(expected: SnapshotRevision, actual: SnapshotRevision) -> bool:
    # Every field pinned at prepare time must be reproduced by the branch.  A
    # missing actual value cannot prove consistency and therefore fails closed.
    return any(
        expected.get(key) is not None
        and expected[key] != actual[key]
        for key in expected
    )


def retrieve_company(branch: CompanyBranchInput | dict) -> dict[str, BranchResults]:
    """Run one exact-target retrieval and convert expected failures to data."""
    branch = branch.get("branch_input", branch)
    key: BranchKey = (
        str(branch["comparison_id"]),
        str(branch["attempt_id"]),
        str(branch["target_name"]),
    )
    base: CompanyBranchResult = {
        "comparison_id": key[0],
        "attempt_id": key[1],
        "target_index": int(branch["target_index"]),
        "target_name": key[2],
        "status": "failed",
        "actual_revision": _revision({}),
        "candidates": [],
        "metrics": {},
        "error": None,
    }
    try:
        queue_started = time.perf_counter_ns()
        with _retrieval_semaphore:
            queue_wait_ns = max(0, time.perf_counter_ns() - queue_started)
            global _retrieval_active
            with _retrieval_counter_lock:
                _retrieval_active += 1
                active_retrievals = _retrieval_active
            retrieval_started_ns = time.perf_counter_ns()
            try:
                docs_with_scores, metrics = vectordb._retrieve_docs_with_scores(
                    str(branch["branch_query"]), dict(branch["filters"])
                )
            finally:
                retrieval_finished_ns = time.perf_counter_ns()
                retrieval_ns = max(0, retrieval_finished_ns - retrieval_started_ns)
                with _retrieval_counter_lock:
                    _retrieval_active -= 1
        # Defense in depth for backends that apply scope only approximately.
        docs_with_scores = filter_docs_with_scores(docs_with_scores, dict(branch["filters"]))
        docs_with_scores = [
            (doc, score)
            for doc, score in docs_with_scores
            if str((getattr(doc, "metadata", {}) or {}).get("target_name") or "") == key[2]
        ]
        actual = _revision(metrics.get("revision") or metrics)
        base["actual_revision"] = actual
        base["metrics"] = _primitive_payload(metrics)
        if _revision_conflicts(_revision(branch.get("expected_revision")), actual):
            base["status"] = "revision_mismatch"
            base["error"] = "retrieval snapshot changed during comparison"
        else:
            budget = max(int(branch.get("candidate_budget") or 1), 1)
            deduped_docs: list[tuple[object, object]] = []
            seen_candidates: set[str] = set()
            for index, item in enumerate(docs_with_scores):
                identity = _dedupe_identity(item[0], index)
                if identity in seen_candidates:
                    continue
                seen_candidates.add(identity)
                deduped_docs.append(item)
            base["candidates"] = [
                _candidate(doc, score, index, key[2])
                for index, (doc, score) in enumerate(deduped_docs[:budget])
            ]
            base["status"] = "success" if base["candidates"] else "no_result"
    except (RetrievalBootstrapError, vectordb.RetrievalDispatchError, OSError, ValueError) as exc:
        base["status"] = "failed"
        base["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    base["metrics"] = {
        **dict(base["metrics"]),
        "branch_query": str(branch["branch_query"]),
        "branch_query_sha256": str(branch["branch_query_sha256"]),
        "candidate_count": len(base["candidates"]),
        "selected_report": _primitive_payload(branch.get("selected_report")),
        "retrieval_concurrency_limit": int(
            branch.get("retrieval_concurrency_limit") or 1
        ),
        "process_retrieval_concurrency_limit": _retrieval_concurrency_limit,
        "queue_wait_ns": locals().get("queue_wait_ns", 0),
        "retrieval_ns": locals().get("retrieval_ns", 0),
        "retrieval_started_ns": locals().get("retrieval_started_ns"),
        "retrieval_finished_ns": locals().get("retrieval_finished_ns"),
        "active_retrievals_at_start": locals().get("active_retrievals", 0),
    }
    return {"branch_results": {key: base}}


def comparison_prepare(state: ComparisonState) -> dict:
    try:
        plan = build_comparison_plan(state, execution_mode="send")
        return {
            "comparison_plan": plan,
            "comparison_preflight_error": None,
            "branch_results": Overwrite({}),
        }
    except (RetrievalBootstrapError, vectordb.RetrievalDispatchError) as exc:
        # No branch may run without a known expected revision. Represent the
        # failure as one compact failed result per requested target so the
        # parent graph can perform its normal one-shot retry and then answer
        # gracefully if the repository remains unavailable.
        fallback_state = dict(state)
        fallback_state["expected_revision"] = _revision({})
        fallback_state["_selected_reports_by_target"] = {}
        plan = build_comparison_plan(fallback_state, execution_mode="send")
        error = {
            "code": type(exc).__name__,
            "message": str(exc)[:240],
        }
        failed_results: BranchResults = {}
        for branch in _branch_inputs(plan):
            key: BranchKey = (
                plan["comparison_id"],
                plan["attempt_id"],
                branch["target_name"],
            )
            failed_results[key] = {
                "comparison_id": plan["comparison_id"],
                "attempt_id": plan["attempt_id"],
                "target_index": branch["target_index"],
                "target_name": branch["target_name"],
                "status": "failed",
                "actual_revision": _revision({}),
                "candidates": [],
                "metrics": {"preflight_error_code": error["code"]},
                "error": f"{error['code']}: {error['message']}",
            }
        return {
            "comparison_plan": plan,
            "comparison_preflight_error": error,
            "branch_results": Overwrite(failed_results),
        }


def dispatch_company_retrieval(state: ComparisonState) -> list[Send] | str:
    if state.get("comparison_preflight_error"):
        return "comparison_fan_in"
    return [Send("retrieve_company", item) for item in _branch_inputs(state["comparison_plan"])]


def _current_results(plan: ComparisonPlan, results: BranchResults) -> list[CompanyBranchResult]:
    current = [
        result for (comparison_id, attempt_id, _target), result in results.items()
        if comparison_id == plan["comparison_id"] and attempt_id == plan["attempt_id"]
    ]
    return sorted(current, key=lambda item: (item["target_index"], item["target_name"]))


def _retrieval_timing_summary(
    results: list[CompanyBranchResult],
) -> tuple[int | None, int | None]:
    intervals: list[tuple[int, int]] = []
    for result in results:
        metrics = result.get("metrics") or {}
        start = metrics.get("retrieval_started_ns")
        end = metrics.get("retrieval_finished_ns")
        if isinstance(start, int) and isinstance(end, int) and end >= start:
            intervals.append((start, end))
    if not intervals:
        return None, None
    wall_ns = max(end for _, end in intervals) - min(start for start, _ in intervals)
    observed_peak = max(
        sum(start <= point < end for start, end in intervals)
        for point, _ in intervals
    )
    return max(wall_ns, 0), observed_peak


def _output_filters(plan: ComparisonPlan) -> dict[str, object]:
    filters = dict(plan["shared_filters"])
    filters["target_names"] = list(plan["targets"])
    if plan["selection_mode"] == "latest_per_target":
        filters["file_names"] = [
            str(report["file_name"])
            for target in plan["targets"]
            if (report := plan["selected_reports_by_target"].get(target))
            and report.get("file_name")
        ]
    return filters


def _balanced_union(
    plan: ComparisonPlan, results: list[CompanyBranchResult]
) -> list[Candidate]:
    queues = {
        result["target_index"]: sorted(
            result["candidates"],
            key=lambda item: (
                int(item.get("retrieval_rank") or 0),
                str(item["stable_id"]),
            ),
        )
        for result in results if result["status"] in ("success", "success_degraded")
    }
    union: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    while queues and len(union) < plan["union_candidate_limit"]:
        progressed = False
        for target_index in sorted(queues):
            queue = queues[target_index]
            while queue and (
                str(queue[0]["target_name"]), str(queue[0]["stable_id"])
            ) in seen:
                queue.pop(0)
            if queue:
                item = queue.pop(0)
                seen.add((str(item["target_name"]), str(item["stable_id"])))
                union.append(item)
                progressed = True
                if len(union) >= plan["union_candidate_limit"]:
                    break
        if not progressed:
            break
    return union


def _global_rerank(
    query: str, candidates: list[Candidate]
) -> tuple[list[Candidate], bool, str | None]:
    if not candidates:
        return [], False, None
    passages = [
        {"id": index, "text": item["text"], "score": item["score"], "meta": item["meta"]}
        for index, item in enumerate(candidates)
    ]
    if not USE_RERANKER:
        return candidates, False, None
    try:
        ranked = get_ranker().rerank(query, passages, len(passages))
        by_id = {index: item for index, item in enumerate(candidates)}
        output = []
        for passage in ranked:
            item = dict(by_id[int(passage["id"])])
            if "rerank_score" in passage:
                item["rerank_score"] = passage["rerank_score"]
            output.append(item)
        return output, True, None
    except Exception as exc:
        logger.warning("Comparison rerank failed; using retrieval order: %s", exc)
        return candidates, True, f"{type(exc).__name__}: {str(exc)[:240]}"


def _balanced_final(
    candidates: list[Candidate], targets: list[str], final_budget: int
) -> list[Candidate]:
    positions = {target: index for index, target in enumerate(targets)}
    buckets = {target: [] for target in targets}
    for candidate in candidates:
        target = str(candidate["target_name"])
        if target in buckets:
            buckets[target].append(candidate)
    selected: list[Candidate] = []
    while len(selected) < final_budget:
        progressed = False
        for target in sorted(targets, key=positions.get):
            if buckets[target]:
                selected.append(buckets[target].pop(0))
                progressed = True
                if len(selected) >= final_budget:
                    break
        if not progressed:
            break
    return selected


def _synthesize_answer(
    question: str, query: str, candidates: list[Candidate], missing: list[str]
) -> tuple[str | None, list[BaseMessage], dict[str, object]]:
    context = "".join(
        f"\n--- 문서 {rank} ---\n[출처: [{rank}] 기업: {item['target_name']} | "
        f"발간일: {(item['meta'] or {}).get('report_date', '-')} | "
        f"증권사: {(item['meta'] or {}).get('broker', '-')} | "
        f"제목: {(item['meta'] or {}).get('title', '-')} ]\n{item['text']}\n"
        for rank, item in enumerate(candidates, 1)
    )
    notice = f"\n비교 근거가 없거나 검색에 실패한 기업: {', '.join(missing)}" if missing else ""
    prompt = PromptTemplate.from_template(VECTORDB_PROMPT)
    message = HumanMessage(
        content=(
            "당신은 증권사 리포트 분석 AI입니다.\n"
            "아래 리포트 문맥만으로 충분하면 바로 답변하세요.\n"
            "섹션의 발간일은 반드시 출처 헤더의 `발간일`을 그대로 사용하세요. "
            "본문의 투자의견·목표주가 제시일자를 발간일로 해석하지 마세요.\n"
            "최신 주가가 꼭 필요할 때만 `get_stock_price` 도구를 호출하세요.\n\n"
            f"사용자 원질문: {question}\n"
            f"재작성 질의: {query}{notice}\n"
            + prompt.format(context=context, question=query)
        )
    )
    llm = build_chat_model(temperature=0.2).bind_tools(stock_price_tools)
    ai_message, generation_call = invoke_chat_with_observability(llm, [message])
    generation_metrics = merge_generation_metrics(
        None,
        generation_call,
        phase="company_comparison_answer",
    )
    if ai_message.tool_calls:
        return None, [message, ai_message], generation_metrics
    answer = ai_message.content
    if isinstance(answer, list):
        answer = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in answer)
    answer = remove_unavailable_citations(str(answer), source_count=len(candidates))
    if notice:
        answer = f"{answer.rstrip()}\n\n{notice.strip()}"
    return answer, [message, ai_message], generation_metrics


def _missing_target_details(
    targets: list[str], statuses: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    labels = {
        "no_result": "검색 결과 없음",
        "failed": "검색 실패",
        "revision_mismatch": "저장소 revision 불일치",
    }
    missing_statuses = {
        target: statuses.get(target, "failed")
        for target in targets
        if statuses.get(target) not in ("success", "success_degraded")
    }
    details = [
        f"{target} ({labels.get(status, status)})"
        for target, status in missing_statuses.items()
    ]
    return details, missing_statuses


def _latest_selection_metrics(
    plan: ComparisonPlan,
    candidates: list[Candidate],
    answer: str | None,
) -> dict[str, object]:
    if plan["selection_mode"] != "latest_per_target":
        return {"mode": plan["selection_mode"], "status": "not_applicable"}

    targets = list(plan["targets"])
    expected = plan["selected_reports_by_target"]
    resolved_targets = [target for target in targets if target in expected]
    context_targets = {
        str(candidate["target_name"])
        for candidate in candidates
        if (report := expected.get(str(candidate["target_name"])))
        and str((candidate.get("meta") or {}).get("file_name") or "")
        == str(report.get("file_name") or "")
    }
    citation_ranks = (
        extract_citation_ranks(answer, source_count=len(candidates)) if answer else set()
    )
    cited_targets = {
        str(candidates[rank - 1]["target_name"])
        for rank in citation_ranks
        if 1 <= rank <= len(candidates)
    }
    missing_preflight = [target for target in targets if target not in expected]
    missing_context = [target for target in resolved_targets if target not in context_targets]
    missing_citations = (
        [target for target in resolved_targets if target not in cited_targets]
        if answer is not None
        else []
    )
    return {
        "mode": "latest_per_target",
        "status": (
            "complete"
            if not missing_preflight and not missing_context
            else "partial"
        ),
        "requested_target_count": len(targets),
        "resolved_target_count": len(resolved_targets),
        "context_target_count": len(context_targets),
        "cited_target_count": len(cited_targets) if answer is not None else None,
        "citation_status": (
            "complete"
            if answer is not None and not missing_citations and not missing_preflight
            else "partial"
            if answer is not None
            else "not_measured"
        ),
        "missing_preflight_targets": missing_preflight,
        "missing_context_targets": missing_context,
        "missing_citation_targets": missing_citations,
        "reports_by_target": _primitive_payload(expected),
    }


def comparison_fan_in(state: ComparisonState) -> dict:
    plan = state["comparison_plan"]
    results = _current_results(plan, state.get("branch_results") or {})
    result_targets = [result["target_name"] for result in results]
    mismatch = result_targets != plan["targets"] or any(
        result["status"] == "revision_mismatch" for result in results
    )
    revisions = {
        tuple(result["actual_revision"].get(key) for key in ("snapshot_id", "publication_generation", "delta_generation", "profile_id"))
        for result in results if result["status"] in ("success", "success_degraded", "no_result")
    }
    known_revisions = {revision for revision in revisions if any(value is not None for value in revision)}
    mismatch = mismatch or len(known_revisions) > 1
    statuses = {result["target_name"]: result["status"] for result in results}
    branch_metrics = {
        result["target_name"]: result["metrics"] for result in results
    }
    retrieval_wall_ns, observed_peak = _retrieval_timing_summary(results)
    serialized_state_bytes = len(
        json.dumps(
            {"comparison_plan": plan, "branch_results": results},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    )
    metrics = {
        "comparison": {
            "comparison_id": plan["comparison_id"],
            "attempt_id": plan["attempt_id"],
            "execution_mode": plan["execution_mode"],
            "target_statuses": statuses,
            "branch_count": len(results),
            "rerank_calls": 0,
            "synthesis_calls": 0,
            "requested_target_count": len(plan["targets"]),
            "available_target_count": sum(
                status in ("success", "success_degraded")
                for status in statuses.values()
            ),
            "expected_revision": plan["expected_revision"],
            "actual_revisions": {
                result["target_name"]: result["actual_revision"]
                for result in results
            },
            "branch_metrics": branch_metrics,
            "selection_mode": plan["selection_mode"],
            "latest_selection": _latest_selection_metrics(plan, [], None),
            "preflight_ns": plan["preflight_ns"],
            "retrieval_concurrency_limit": plan["retrieval_concurrency_limit"],
            "process_retrieval_concurrency_limit": plan[
                "process_retrieval_concurrency_limit"
            ],
            "observed_peak_retrieval_concurrency": observed_peak,
            "retrieval_wall_ns": retrieval_wall_ns,
            "checkpoint_candidate_count": sum(
                len(result["candidates"]) for result in results
            ),
            "checkpoint_serialized_bytes": serialized_state_bytes,
        }
    }
    if preflight_error := state.get("comparison_preflight_error"):
        metrics["comparison"]["preflight_error_code"] = preflight_error["code"]
    if mismatch:
        metrics["comparison"]["status"] = "revision_mismatch"
        return {
            "generation": "검색 중 저장소 revision이 변경되어 비교 답변을 생성하지 않았습니다.",
            "messages": [],
            "rerank_info": [],
            "no_vector_results": True,
            "search_filters": _output_filters(plan),
            "monitoring_metrics": metrics,
            "vector_outcome": "revision_mismatch",
            "vector_retryable": True,
        }

    union = _balanced_union(plan, results)
    ranked, rerank_attempted, rerank_error = _global_rerank(
        plan["retrieval_query"], union
    )
    metrics["comparison"]["rerank_calls"] = int(rerank_attempted)
    if rerank_error:
        metrics["comparison"]["rerank_degraded"] = True
        metrics["comparison"]["rerank_error"] = rerank_error
    selected = _balanced_final(ranked, plan["targets"], plan["final_budget"])
    metrics["comparison"]["latest_selection"] = _latest_selection_metrics(
        plan,
        selected,
        None,
    )
    missing = [
        target for target in plan["targets"]
        if statuses.get(target) not in ("success", "success_degraded")
    ]
    missing_details, missing_statuses = _missing_target_details(
        plan["targets"], statuses
    )
    if not selected:
        all_failed = bool(results) and all(
            result["status"] == "failed" for result in results
        )
        outcome = "all_failed" if all_failed else "insufficient"
        metrics["comparison"]["status"] = outcome
        return {
            "generation": "요청한 기업의 비교 근거를 찾지 못했습니다.",
            "messages": [],
            "rerank_info": [],
            "no_vector_results": True,
            "search_filters": _output_filters(plan),
            "monitoring_metrics": metrics,
            "vector_outcome": outcome,
            "vector_retryable": all_failed,
        }
    rerank_info = []
    for rank, candidate in enumerate(selected, 1):
        meta = dict(candidate["meta"])
        rerank_info.append(
            {
                "rank": rank,
                "target_name": meta.get("target_name", candidate["target_name"]),
                "report_date": meta.get("report_date", "-"),
                "title": meta.get("title", "-"),
                "broker": meta.get("broker", "-"),
                "report_type": meta.get("report_type", "-"),
                "file_name": meta.get("file_name", "-"),
                **meta,
                "score": candidate["score"],
                "rerank_score": candidate.get("rerank_score"),
            }
        )
    successful_target_count = sum(
        status in ("success", "success_degraded") for status in statuses.values()
    )
    if successful_target_count < 2:
        selected_by_target = {
            target: [
                rank
                for rank, candidate in enumerate(selected, 1)
                if candidate["target_name"] == target
            ]
            for target in plan["targets"]
        }
        metrics["comparison"].update(
            {
                "status": "insufficient",
                "missing_targets": missing,
                "missing_target_statuses": missing_statuses,
                "union_candidate_count": len(union),
                "selected_source_count": len(selected),
                "citation_ranks_by_target": selected_by_target,
            }
        )
        return {
            "generation": (
                "비교에 필요한 기업별 근거가 충분하지 않습니다. "
                f"누락 사유: {', '.join(missing_details)}"
            ),
            "messages": [],
            "rerank_info": rerank_info,
            "no_vector_results": False,
            "search_filters": _output_filters(plan),
            "monitoring_metrics": metrics,
            "vector_outcome": "insufficient",
            "vector_retryable": False,
        }

    synthesis_started = time.perf_counter_ns()
    try:
        synthesis_result = _synthesize_answer(
            plan["original_query"], plan["retrieval_query"], selected, missing_details
        )
        if len(synthesis_result) == 3:
            answer, messages, generation_metrics = synthesis_result
        else:
            answer, messages = synthesis_result
            generation_metrics = None
    finally:
        synthesis_ns = max(0, time.perf_counter_ns() - synthesis_started)
    outcome = "partial" if missing else "complete"
    selected_by_target = {
        target: [
            rank
            for rank, candidate in enumerate(selected, 1)
            if candidate["target_name"] == target
        ]
        for target in plan["targets"]
    }
    metrics["comparison"].update(
        {
            "status": outcome,
            "missing_targets": missing,
            "missing_target_statuses": missing_statuses,
            "union_candidate_count": len(union),
            "selected_source_count": len(selected),
            "citation_ranks_by_target": selected_by_target,
            "synthesis_calls": 1,
            "synthesis_ns": synthesis_ns,
            "latest_selection": _latest_selection_metrics(plan, selected, answer),
        }
    )
    if generation_metrics:
        metrics["generation"] = generation_metrics
    if rerank_error:
        metrics["comparison"]["comparison_degraded"] = True
        metrics["comparison"]["degraded_reason"] = "reranker_failure"
    output = {
        "messages": messages,
        "rerank_info": rerank_info,
        "no_vector_results": False,
        "search_filters": _output_filters(plan),
        "monitoring_metrics": metrics,
        "vector_outcome": outcome,
        "vector_retryable": False,
    }
    if answer is not None:
        output["generation"] = answer
    return output


def run_sequential_reference(state: dict) -> dict:
    plan = build_comparison_plan(state, execution_mode="sequential_reference")
    results: BranchResults = {}
    for branch in _branch_inputs(plan):
        results = keyed_upsert_results(results, retrieve_company(branch)["branch_results"])
    return comparison_fan_in({**state, "comparison_plan": plan, "branch_results": results})


def build_company_comparison_subgraph(*, checkpointer=None):
    workflow = StateGraph(
        ComparisonState,
        input_schema=ComparisonInput,
        output_schema=ComparisonOutput,
    )
    workflow.add_node("comparison_prepare", comparison_prepare)
    workflow.add_node("retrieve_company", retrieve_company)
    workflow.add_node("comparison_fan_in", comparison_fan_in)
    workflow.add_edge(START, "comparison_prepare")
    workflow.add_conditional_edges(
        "comparison_prepare",
        dispatch_company_retrieval,
        ["retrieve_company", "comparison_fan_in"],
    )
    workflow.add_edge("retrieve_company", "comparison_fan_in")
    workflow.add_edge("comparison_fan_in", END)
    return workflow.compile(checkpointer=checkpointer, name="company_comparison")


company_comparison_graph = build_company_comparison_subgraph()


__all__ = [
    "ComparisonPlan", "ComparisonInput", "ComparisonOutput",
    "CompanyBranchInput", "CompanyBranchResult",
    "build_comparison_plan", "build_target_branch_query",
    "keyed_upsert_results", "retrieve_company",
    "comparison_prepare", "dispatch_company_retrieval", "comparison_fan_in",
    "run_sequential_reference", "build_company_comparison_subgraph",
    "company_comparison_graph", "configure_retrieval_concurrency",
]
