from src.core.followup_scope import resolve_section_followup_scope
from src.core.metadata_filters import infer_search_filters, resolve_temporal_context
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


def _valid_route(route: object) -> str | None:
    return route if route in {"rdb", "vectordb"} else None


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
    current_question_filters = infer_search_filters(state["question"])
    current_temporal_context = None if full_period_request else resolve_temporal_context(state["question"])
    search_filters = infer_search_filters(query)
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
    prior_search_scope = state.get("prior_search_scope") or {}
    followup_scope_intent = bool(state.get("followup_scope_intent"))
    current_non_temporal_filters = {
        key: value
        for key, value in current_question_filters.items()
        if key not in {"report_date_start", "report_date_end"}
    }
    if (
        followup_scope_intent
        and (not current_temporal_context or full_period_request or not has_vector_intent)
        and isinstance(prior_search_scope, dict)
    ):
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
