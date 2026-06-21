from src.configs.config import get_logger
from src.core.db_manager import get_connection
from src.graphs.state import State

logger = get_logger(__name__)

TOP_COMPANY_SELECTION = "top_company_target_by_report_count"


def _top_company_target_from_filters(filters: dict) -> tuple[str, int] | None:
    """Pick the most frequent company target using deterministic DB counts."""
    where = [
        "report_type = 'company'",
        "target_name IS NOT NULL",
        "target_name != ''",
        "target_name != 'null'",
        "target_name != '기타'",
    ]
    params: list[str] = []
    if filters.get("report_date_start"):
        where.append("report_date >= ?")
        params.append(str(filters["report_date_start"]))
    if filters.get("report_date_end"):
        where.append("report_date <= ?")
        params.append(str(filters["report_date_end"]))
    if filters.get("broker"):
        where.append("broker = ?")
        params.append(str(filters["broker"]))

    query = f"""
        SELECT target_name, COUNT(*) AS report_count
        FROM reports
        WHERE {" AND ".join(where)}
        GROUP BY target_name
        ORDER BY report_count DESC, target_name ASC
        LIMIT 1
    """
    try:
        with get_connection() as conn:
            row = conn.execute(query, params).fetchone()
    except Exception as exc:  # pragma: no cover - defensive DB fallback
        logger.warning("[ScopeSelection] top company target lookup failed: %s", exc)
        return None
    if not row:
        return None
    return str(row["target_name"]), int(row["report_count"])


def _rewrite_for_selected_top_target(question: str, target_name: str) -> str:
    """Build a focused retrieval query after deterministic top-target selection."""
    base_question = str(question or "").strip()
    if not base_question:
        return f"{target_name} 리포트 내용 요약"
    return f"{base_question} 선정 대상: {target_name}. {target_name} 리포트 내용만 요약"


def scope_selection_node(state: State) -> dict:
    """Resolve optional scope-selection requests that require RDB aggregation."""
    request = state.get("scope_selection_request") or {}
    if request.get("type") != TOP_COMPANY_SELECTION:
        return {"scope_selection_request": None}

    filters = dict(request.get("filters") or state.get("search_filters") or {})
    top_target = _top_company_target_from_filters(filters)
    if not top_target:
        return {"scope_selection_request": None}

    target_name, report_count = top_target
    search_filters = {
        key: value
        for key, value in filters.items()
        if key not in {"target_name", "file_names", "report_type"}
    }
    search_filters["target_name"] = target_name
    search_filters["report_type"] = "company"

    return {
        "scope_selection_request": None,
        "search_filters": search_filters,
        "scope_source": "top_target_from_rdb",
        "selection_context": {
            "strategy": TOP_COMPANY_SELECTION,
            "target_name": target_name,
            "report_count": report_count,
        },
        "rewritten_query": _rewrite_for_selected_top_target(
            state["question"],
            target_name,
        ),
    }
