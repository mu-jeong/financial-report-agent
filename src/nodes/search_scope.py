from datetime import datetime

from src.configs.config import SEARCH_TOP_K
from src.core.company_industry import resolve_report_file_scope_for_companies
from src.core.followup_scope import resolve_section_followup_scope
from src.core.metadata_filters import get_metadata_candidates, infer_search_filters, resolve_temporal_context
from src.graphs.state import State

VECTORDB_INTENT_KEYWORDS = (
    "살펴볼만",
    "주요 내용",
    "주요 분석",
    "분석 내용",
    "투자 포인트",
    "핵심 포인트",
    "본문",
    "전망",
    "리스크",
    "요약",
    "자세히",
    "상세히",
    "상세하게",
    "summary",
    "content",
)
VECTORDB_CONTENT_INSPECTION_KEYWORDS = (
    "관련 내용",
    "관련된 내용",
    "내용이 확인",
    "내용을 확인",
    "내용 재확인",
    "본문 재확인",
)
TOP_TARGET_KEYWORDS = ("가장 많이", "최다", "많이 발간", "많이 언급", "top", "most frequent", "most published")
TARGET_SCOPE_KEYWORDS = ("회사", "종목", "기업", "target")
FULL_PERIOD_KEYWORDS = ("전체 기간", "전체기간", "전 기간", "전기간", "전체 데이터", "full period", "all period", "all time")
INDUSTRY_COMPANY_KEYWORDS = (
    "섹터",
    "분야",
    "업종",
    "산업",
    "관련주",
    "관련 기업",
    "관련 회사",
)
TEMPORAL_REPORT_SET_KEYWORDS = (
    "시기별",
    "월별",
    "분기별",
    "기간별",
    "흐름",
    "추이",
    "변화",
)
LONG_PERIOD_SUMMARY_KEYWORDS = ("정리", "요약")
LONG_PERIOD_MIN_DAYS = 60
LATEST_REPORT_KEYWORDS = ("가장 최근", "최신", "최근", "마지막", "latest")
REPORT_REFERENCE_KEYWORDS = ("리포트", "보고서", "문서", "자료", "report")
LATEST_REPORT_CHUNKS_PER_TARGET = 4


def _normalize_text(value: str) -> str:
    return str(value or "").casefold().replace(" ", "")


def _is_top_target_request(text: str) -> bool:
    normalized = _normalize_text(text)
    return (
        any(_normalize_text(keyword) in normalized for keyword in TOP_TARGET_KEYWORDS)
        and any(_normalize_text(keyword) in normalized for keyword in TARGET_SCOPE_KEYWORDS)
    )


def _is_full_period_request(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(_normalize_text(keyword) in normalized for keyword in FULL_PERIOD_KEYWORDS)


def _is_latest_report_request(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(
        _normalize_text(keyword) in normalized for keyword in LATEST_REPORT_KEYWORDS
    ) and any(
        _normalize_text(keyword) in normalized
        for keyword in REPORT_REFERENCE_KEYWORDS
    )


def _has_valid_date_range(filters: dict | None) -> bool:
    filters = filters or {}
    start = filters.get("report_date_start")
    end = filters.get("report_date_end")
    return bool(start and end and str(start) <= str(end))


def _date_span_days(filters: dict | None) -> int:
    filters = filters or {}
    try:
        start = datetime.strptime(str(filters.get("report_date_start")), "%Y-%m-%d").date()
        end = datetime.strptime(str(filters.get("report_date_end")), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0
    return max((end - start).days, 0)


def build_retrieval_plan(question: str, filters: dict | None, *, has_vector_intent: bool) -> dict | None:
    """Choose a VectorDB-internal retrieval strategy for the resolved scope."""
    if not has_vector_intent:
        return None
    filters = filters or {}
    targets = list(dict.fromkeys(filters.get("target_names") or []))
    if len(targets) >= 6:
        return {
            "type": "too_many_targets",
            "targets": targets,
            "target_names": targets,
            "max_targets": 5,
        }
    if 2 <= len(targets) <= 5:
        shared_filters = {
            key: value
            for key, value in filters.items()
            if key not in {"target_name", "target_names", "file_names"}
        }
        plan = {
            "type": "company_comparison",
            "targets": targets,
            "target_names": targets,
            "shared_filters": shared_filters,
            "union_candidate_limit": 60,
            "final_budget": SEARCH_TOP_K,
        }
        if _is_latest_report_request(question):
            latest_budget = min(
                SEARCH_TOP_K,
                len(targets) * LATEST_REPORT_CHUNKS_PER_TARGET,
            )
            plan.update(
                {
                    "selection_mode": "latest_per_target",
                    "preflight": "active_report_latest_per_target",
                    "candidate_budget_per_target": LATEST_REPORT_CHUNKS_PER_TARGET,
                    "union_candidate_limit": latest_budget,
                    "final_budget": latest_budget,
                }
            )
        return plan
    if not _has_valid_date_range(filters):
        return None
    if not filters.get("target_name"):
        return None
    normalized = _normalize_text(question)
    explicit_temporal_breakdown = any(
        _normalize_text(keyword) in normalized for keyword in TEMPORAL_REPORT_SET_KEYWORDS
    )
    long_period_summary = (
        _date_span_days(filters) >= LONG_PERIOD_MIN_DAYS
        and any(_normalize_text(keyword) in normalized for keyword in LONG_PERIOD_SUMMARY_KEYWORDS)
    )
    if not explicit_temporal_breakdown and not long_period_summary:
        return None
    return {
        "type": "temporal_report_set_summary",
        "preflight": "rdb_file_universe",
        "bucket_by": "month",
    }


def _industry_lookup_term(text: str) -> str | None:
    value = str(text or "").strip()
    if not value:
        return None
    if not any(keyword in value for keyword in INDUSTRY_COMPANY_KEYWORDS):
        return None
    if not any(keyword in value for keyword in ("기업", "회사", "종목", "관련주")):
        return None
    for keyword in INDUSTRY_COMPANY_KEYWORDS:
        if keyword not in value:
            continue
        before = value.split(keyword, 1)[0].strip()
        if not before:
            continue
        term = before.split()[-1].strip("'\"`.,()[]{}")
        return term or None
    return None


def _industry_term_belongs_to_company_target(
    text: str,
    industry_term: str,
    filters: dict,
) -> bool:
    targets = list(filters.get("target_names") or [])
    if filters.get("target_name"):
        targets.append(filters["target_name"])
    if not targets:
        return False

    target_report_types = get_metadata_candidates().get("target_report_types")
    if not isinstance(target_report_types, dict):
        return False
    normalized_text = _normalize_text(text)
    normalized_term = _normalize_text(industry_term)
    company_name_candidates = {
        f"{normalized_term}{_normalize_text(keyword)}"
        for keyword in INDUSTRY_COMPANY_KEYWORDS
    }
    return any(
        "company" in target_report_types.get(target, ())
        and _normalize_text(target) in normalized_text
        and _normalize_text(target) in company_name_candidates
        for target in targets
    )


def _date_range_is_inverted(filters: dict | None) -> bool:
    filters = filters or {}
    start = filters.get("report_date_start")
    end = filters.get("report_date_end")
    return bool(start and end and str(start) > str(end))


def _repair_inverted_date_range_from_prior(filters: dict, prior_search_scope: dict | None) -> tuple[dict, dict | None]:
    if not _date_range_is_inverted(filters):
        return filters, None
    prior_context = (prior_search_scope or {}).get("temporal_context") if isinstance(prior_search_scope, dict) else None
    prior_filters = (prior_search_scope or {}).get("search_filters") if isinstance(prior_search_scope, dict) else None
    if _has_valid_date_range(prior_filters):
        repaired = dict(filters)
        repaired["report_date_start"] = prior_filters["report_date_start"]
        repaired["report_date_end"] = prior_filters["report_date_end"]
        return repaired, prior_context
    repaired = dict(filters)
    repaired.pop("report_date_start", None)
    repaired.pop("report_date_end", None)
    return repaired, None


def search_scope_prepare_node(state: State) -> dict:
    """Prepare question-only scope work that can run before query rewriting."""
    question = state["question"]
    current_question_filters = _drop_incompatible_target_filter(infer_search_filters(question))
    full_period_request = _is_full_period_request(question)
    temporal_context = None if full_period_request else resolve_temporal_context(question)
    prior_filters = dict((state.get("prior_search_scope") or {}).get("search_filters") or {})
    base_filters = dict(prior_filters)
    if "report_types" in current_question_filters:
        base_filters.pop("report_type", None)
    elif "report_type" in current_question_filters:
        base_filters.pop("report_types", None)
    base_filters.update(
        {
            key: value
            for key, value in current_question_filters.items()
            if key not in {"target_name", "file_names"}
        }
    )
    if temporal_context:
        base_filters["report_date_start"] = temporal_context["report_date_start"]
        base_filters["report_date_end"] = temporal_context["report_date_end"]

    prepare = {
        "question_filters": current_question_filters,
        "temporal_context": temporal_context,
        "base_filters": base_filters,
    }
    industry_term = _industry_lookup_term(question)
    if industry_term and not _industry_term_belongs_to_company_target(
        question,
        industry_term,
        current_question_filters,
    ):
        prepare["industry_lookup_request"] = {
            "term": industry_term,
            "reason": "sector_company_request",
            "target": "company_universe",
        }
    return {"scope_prepare": prepare}


def _valid_route(route: object) -> str | None:
    return route if route in {"rdb", "vectordb"} else None


def _drop_incompatible_target_filter(filters: dict) -> dict:
    """Drop target_name when it is known only under a different report_type.

    Some sector names are also stored as ``target_name`` for industry reports.
    A follow-up such as "반도체 섹터에 속한 기업" should search company reports
    within the prior date scope, not require ``target_name=반도체`` on company
    documents where no such exact company target exists.
    """
    cleaned = dict(filters or {})
    target_names = list(cleaned.get("target_names") or [])
    target_name = cleaned.get("target_name")
    report_type = cleaned.get("report_type")
    report_types = cleaned.get("report_types")
    requested_report_types = (
        {str(report_type)}
        if report_type
        else {str(value) for value in (report_types or [])}
    )
    if not (target_name or target_names) or not requested_report_types:
        return cleaned
    target_report_types = get_metadata_candidates().get("target_report_types")
    if not isinstance(target_report_types, dict):
        return cleaned
    if target_names:
        compatible_targets = [
            value
            for value in target_names
            if not (
                (known_types := tuple(target_report_types.get(value, ())))
                and requested_report_types.isdisjoint(known_types)
            )
        ]
        cleaned.pop("target_names", None)
        cleaned.pop("target_name", None)
        if len(compatible_targets) == 1:
            cleaned["target_name"] = compatible_targets[0]
        elif compatible_targets:
            cleaned["target_names"] = compatible_targets
        return cleaned
    known_report_types = tuple(target_report_types.get(target_name, ()))
    if known_report_types and requested_report_types.isdisjoint(known_report_types):
        cleaned.pop("target_name", None)
    return cleaned


def _merge_prior_filters_with_current(prior_filters: dict, current_filters: dict) -> dict:
    """Merge current explicit filters into thread scope without stale refinements.

    A prior drill-down can contain narrow refinements such as broker/file scope.
    When the next question explicitly switches to a different target, keep the
    durable period/report-type context but do not carry over the prior broker.
    """
    merged = dict(prior_filters or {})
    current = dict(current_filters or {})
    prior_targets = set(merged.get("target_names") or [])
    if merged.get("target_name"):
        prior_targets.add(merged["target_name"])
    current_targets = set(current.get("target_names") or [])
    if current.get("target_name"):
        current_targets.add(current["target_name"])
    file_scope_changed = bool(current_targets) and current_targets != prior_targets
    target_switched = bool(prior_targets) and file_scope_changed

    current_target = current.get("target_name")
    if current.get("target_names"):
        merged.pop("target_name", None)
        merged.pop("target_names", None)
    elif current_target and target_switched:
        if "broker" not in current:
            merged.pop("broker", None)
    if current_target:
        merged.pop("target_names", None)

    prior_report_types = set(merged.get("report_types") or [])
    if merged.get("report_type"):
        prior_report_types.add(merged["report_type"])
    current_report_types = set(current.get("report_types") or [])
    if current.get("report_type"):
        current_report_types.add(current["report_type"])
    if current_report_types and current_report_types != prior_report_types:
        file_scope_changed = True
    if "report_types" in current:
        merged.pop("report_type", None)
    elif "report_type" in current:
        merged.pop("report_types", None)
    merged.update(current)
    if file_scope_changed:
        merged.pop("file_names", None)
    return merged


def search_scope_node(state: State) -> dict:
    """Resolve deterministic retrieval scope before route selection.

    This node owns metadata filters, prior-scope reuse, full-period expansion,
    and RDB-based top-company selection. The router consumes the resulting
    scope plus routing_context and only chooses the execution path.
    """
    query = state.get("rewritten_query", state["question"])
    combined_intent_text = f"{state['question']} {query}"
    has_vector_intent = any(
        keyword in combined_intent_text
        for keyword in (*VECTORDB_INTENT_KEYWORDS, *VECTORDB_CONTENT_INSPECTION_KEYWORDS)
    )
    full_period_request = _is_full_period_request(combined_intent_text)
    current_question_filters = _drop_incompatible_target_filter(infer_search_filters(state["question"]))
    current_temporal_context = None if full_period_request else resolve_temporal_context(state["question"])
    if _date_range_is_inverted(current_temporal_context):
        current_temporal_context = None
    search_filters = _drop_incompatible_target_filter(infer_search_filters(query))
    prior_search_scope = state.get("prior_search_scope") or state.get("active_scope") or {}
    if full_period_request:
        search_filters.pop("report_date_start", None)
        search_filters.pop("report_date_end", None)
    temporal_context = current_temporal_context or (
        None if full_period_request else resolve_temporal_context(query)
    )
    if temporal_context:
        search_filters.update(
            {
                "report_date_start": temporal_context["report_date_start"],
                "report_date_end": temporal_context["report_date_end"],
            }
        )
    search_filters, repaired_temporal_context = _repair_inverted_date_range_from_prior(
        search_filters,
        prior_search_scope,
    )
    if repaired_temporal_context is not None:
        temporal_context = repaired_temporal_context

    scope_source = None
    route_hint = None
    scope_decision = None
    followup_scope_intent = bool(state.get("followup_scope_intent")) or bool(
        isinstance(prior_search_scope, dict) and prior_search_scope.get("search_filters")
    )
    current_non_temporal_filters = {
        key: value
        for key, value in current_question_filters.items()
        if key not in {"report_date_start", "report_date_end"}
    }
    if followup_scope_intent and isinstance(prior_search_scope, dict):
        prior_filters = dict(prior_search_scope.get("search_filters") or {})
        file_names = [
            file_name
            for file_name in prior_search_scope.get("file_names", [])
            if file_name and file_name != "-"
        ]
        if file_names:
            prior_filters["file_names"] = file_names
        if prior_filters:
            section_decision = resolve_section_followup_scope(
                state["question"],
                current_filters=current_non_temporal_filters,
                prior_search_scope=prior_search_scope,
            )
            if section_decision.get("matched"):
                search_filters = section_decision["search_filters"]
                scope_decision = section_decision
            elif section_decision.get("reason") == "document_ordinal_out_of_range":
                search_filters = {"file_names": []}
                scope_decision = section_decision
            else:
                prior_filters = _merge_prior_filters_with_current(prior_filters, current_non_temporal_filters)
                if current_temporal_context and not full_period_request:
                    prior_filters["report_date_start"] = current_temporal_context["report_date_start"]
                    prior_filters["report_date_end"] = current_temporal_context["report_date_end"]
                    prior_filters.pop("file_names", None)
                if full_period_request:
                    prior_filters.pop("report_date_start", None)
                    prior_filters.pop("report_date_end", None)
                    prior_filters.pop("file_names", None)
                search_filters = prior_filters
            temporal_context = (
                None
                if full_period_request
                else current_temporal_context or prior_search_scope.get("temporal_context")
            )
            scope_source = "prior_search_scope"

            prior_route = _valid_route(prior_search_scope.get("route"))
            if full_period_request or (current_temporal_context and not has_vector_intent):
                route_hint = prior_route or "rdb"
            if (
                {"report_type", "report_types"} & current_non_temporal_filters.keys()
                and not has_vector_intent
            ):
                route_hint = "rdb"
            if (
                scope_decision
                and scope_decision.get("reason") == "document_ordinal_out_of_range"
            ):
                route_hint = "vectordb"

    scope_selection_request = None
    if _is_top_target_request(combined_intent_text) and not (
        scope_decision and scope_decision.get("reason") == "matched_prior_section_alias"
    ):
        scope_selection_request = {
            "type": "top_company_target_by_report_count",
            "filters": {
                key: value
                for key, value in search_filters.items()
                if key not in {"target_name", "file_names", "report_type", "report_types"}
            },
        }

    result = {
        "search_filters": search_filters,
        "temporal_context": temporal_context,
        "scope_source": scope_source,
        "routing_context": {
            "has_vector_intent": has_vector_intent,
            "route_hint": route_hint,
            "full_period_request": full_period_request,
        },
    }
    retrieval_plan = build_retrieval_plan(
        combined_intent_text,
        search_filters,
        has_vector_intent=has_vector_intent,
    )
    if retrieval_plan is not None:
        result["retrieval_plan"] = retrieval_plan
    if scope_selection_request is not None:
        result["scope_selection_request"] = scope_selection_request
    if scope_decision is not None:
        result["scope_decision"] = scope_decision
    return result


def search_scope_merge_node(state: State) -> dict:
    """Merge rewritten-query scope with optional industry/company lookup output."""
    result = search_scope_node(state)
    industry_context = state.get("industry_lookup_context") or {}
    company_names = industry_context.get("company_names") or []
    if not company_names:
        return result

    search_filters = dict(result.get("search_filters") or {})
    search_filters.pop("target_name", None)
    search_filters.pop("target_names", None)
    search_filters.pop("report_types", None)
    search_filters["report_type"] = "company"
    scope_decision = {
        "matched": True,
        "reason": "industry_company_universe",
        "industry_term": industry_context.get("term"),
        "matched_company_count": industry_context.get("matched_company_count", 0),
        "matched_companies_preview": industry_context.get("matched_companies_preview", []),
        "company_count": len(company_names),
        "source_url": industry_context.get("source_url"),
        "search_filters": search_filters,
    }
    result["search_filters"] = search_filters
    result["scope_decision"] = scope_decision
    result["scope_source"] = "industry_company_lookup"
    result.setdefault("routing_context", {})["route_hint"] = None
    result.pop("retrieval_plan", None)
    return result


def rdb_scope_preflight_node(state: State) -> dict:
    """Convert an industry/company universe into RDB SQL constraints."""
    industry_context = state.get("industry_lookup_context") or {}
    company_names = industry_context.get("company_names") or []
    if not company_names:
        return {}

    search_filters = dict(state.get("search_filters") or {})
    search_filters.pop("target_name", None)
    search_filters.pop("file_names", None)
    search_filters.pop("report_types", None)
    search_filters["report_type"] = "company"
    search_filters["target_names"] = company_names
    scope_decision = dict(state.get("scope_decision") or {})
    scope_decision.update(
        {
            "matched": True,
            "reason": "industry_company_universe_sql_constraint",
            "industry_term": industry_context.get("term"),
            "company_count": len(company_names),
            "search_filters": search_filters,
        }
    )
    return {
        "search_filters": search_filters,
        "scope_decision": scope_decision,
        "scope_source": "industry_company_lookup",
    }


def vectordb_scope_preflight_node(state: State) -> dict:
    """Convert an industry/company universe into embedded VectorDB file scope."""
    industry_context = state.get("industry_lookup_context") or {}
    company_names = industry_context.get("company_names") or []
    if not company_names:
        return {}

    search_filters = dict(state.get("search_filters") or {})
    search_filters.pop("target_name", None)
    search_filters.pop("report_types", None)
    search_filters["report_type"] = "company"
    report_scope = resolve_report_file_scope_for_companies(
        company_names,
        base_filters=search_filters,
    )
    if report_scope.get("file_names"):
        search_filters["file_names"] = report_scope["file_names"]
    scope_decision = dict(state.get("scope_decision") or {})
    scope_decision.update(
        {
            "matched": True,
            "reason": "industry_company_universe_file_scope",
            "industry_term": industry_context.get("term"),
            "company_count": len(company_names),
            "matched_report_targets": report_scope.get("matched_report_targets", []),
            "report_file_count": report_scope.get("report_file_count", 0),
            "source_url": industry_context.get("source_url"),
            "search_filters": search_filters,
        }
    )
    return {
        "search_filters": search_filters,
        "scope_decision": scope_decision,
        "scope_source": "industry_company_lookup",
    }
