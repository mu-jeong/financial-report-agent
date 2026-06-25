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

TOP_TARGET_KEYWORDS = ("가장 많이", "최다", "많이 발간", "많이 언급", "top", "most frequent", "most published")
TARGET_SCOPE_KEYWORDS = ("회사", "종목", "기업", "target")
FULL_PERIOD_KEYWORDS = ("전체 기간", "전체기간", "전 기간", "전기간", "전체 데이터", "full period", "all period", "all time")
INDUSTRY_COMPANY_KEYWORDS = ("섹터", "분야", "업종", "관련주", "관련 기업", "관련 회사")


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


def search_scope_prepare_node(state: State) -> dict:
    """Prepare question-only scope work that can run before query rewriting."""
    question = state["question"]
    current_question_filters = _drop_incompatible_target_filter(infer_search_filters(question))
    full_period_request = _is_full_period_request(question)
    temporal_context = None if full_period_request else resolve_temporal_context(question)
    prior_filters = dict((state.get("prior_search_scope") or {}).get("search_filters") or {})
    base_filters = dict(prior_filters)
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
    if industry_term := _industry_lookup_term(question):
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
    target_name = cleaned.get("target_name")
    report_type = cleaned.get("report_type")
    if not target_name or not report_type:
        return cleaned
    target_report_types = get_metadata_candidates().get("target_report_types")
    if not isinstance(target_report_types, dict):
        return cleaned
    known_report_types = tuple(target_report_types.get(target_name, ()))
    if known_report_types and report_type not in known_report_types:
        cleaned.pop("target_name", None)
    return cleaned


def search_scope_node(state: State) -> dict:
    """Resolve deterministic retrieval scope before route selection.

    This node owns metadata filters, prior-scope reuse, full-period expansion,
    and RDB-based top-company selection. The router consumes the resulting
    scope plus routing_context and only chooses the execution path.
    """
    query = state.get("rewritten_query", state["question"])
    combined_intent_text = f"{state['question']} {query}"
    has_vector_intent = any(keyword in combined_intent_text for keyword in VECTORDB_INTENT_KEYWORDS)
    full_period_request = _is_full_period_request(combined_intent_text)
    current_question_filters = _drop_incompatible_target_filter(infer_search_filters(state["question"]))
    current_temporal_context = None if full_period_request else resolve_temporal_context(state["question"])
    search_filters = _drop_incompatible_target_filter(infer_search_filters(query))
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

    scope_source = None
    route_hint = None
    scope_decision = None
    prior_search_scope = state.get("prior_search_scope") or state.get("active_scope") or {}
    followup_scope_intent = bool(state.get("followup_scope_intent"))
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
            else:
                prior_filters.update(current_non_temporal_filters)
                if current_temporal_context and not full_period_request:
                    prior_filters["report_date_start"] = current_temporal_context["report_date_start"]
                    prior_filters["report_date_end"] = current_temporal_context["report_date_end"]
                    prior_filters.pop("file_names", None)
                if "report_type" in current_non_temporal_filters:
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
            if "report_type" in current_non_temporal_filters and not has_vector_intent:
                route_hint = "rdb"

    scope_selection_request = None
    if _is_top_target_request(combined_intent_text) and not (
        scope_decision and scope_decision.get("reason") == "matched_prior_section_alias"
    ):
        scope_selection_request = {
            "type": "top_company_target_by_report_count",
            "filters": {
                key: value
                for key, value in search_filters.items()
                if key not in {"target_name", "file_names", "report_type"}
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
    if scope_selection_request is not None:
        result["scope_selection_request"] = scope_selection_request
    if scope_decision is not None:
        result["scope_decision"] = scope_decision
    return result


def search_scope_merge_node(state: State) -> dict:
    """Merge rewritten-query scope with optional industry/company lookup output."""
    result = search_scope_node(state)
    industry_context = state.get("industry_lookup_context") or {}
    file_names = industry_context.get("file_names") or []
    if not file_names:
        return result

    search_filters = dict(result.get("search_filters") or {})
    search_filters.pop("target_name", None)
    search_filters["report_type"] = "company"
    search_filters["file_names"] = file_names
    scope_decision = {
        "matched": True,
        "reason": "industry_company_universe_intersection",
        "industry_term": industry_context.get("term"),
        "matched_company_count": industry_context.get("matched_company_count", 0),
        "matched_report_targets": industry_context.get("matched_report_targets", []),
        "report_file_count": industry_context.get("report_file_count", len(file_names)),
        "source_url": industry_context.get("source_url"),
        "search_filters": search_filters,
    }
    result["search_filters"] = search_filters
    result["scope_decision"] = scope_decision
    result["scope_source"] = "industry_company_lookup"
    result.setdefault("routing_context", {})["route_hint"] = None
    result["routing_context"]["has_vector_intent"] = True
    return result
