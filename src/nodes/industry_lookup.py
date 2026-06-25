from src.core.company_industry import resolve_industry_report_file_scope
from src.graphs.state import State


def industry_lookup_node(state: State) -> dict:
    """Resolve sector/industry terms to a company-report file universe."""
    prepare = state.get("scope_prepare") or {}
    request = prepare.get("industry_lookup_request") or {}
    term = request.get("term")
    if not term:
        return {"industry_lookup_context": None}

    return {
        "industry_lookup_context": resolve_industry_report_file_scope(
            str(term),
            base_filters=prepare.get("base_filters") or {},
        )
    }
