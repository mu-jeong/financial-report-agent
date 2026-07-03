from src.core.company_industry import lookup_companies_by_industry
from src.graphs.state import State


def industry_lookup_node(state: State) -> dict:
    """Resolve sector/industry terms to a company universe.

    Route-specific nodes later decide how to use this universe: RDB turns it into
    SQL target constraints, while VectorDB turns it into embedded file scope.
    """
    prepare = state.get("scope_prepare") or {}
    request = prepare.get("industry_lookup_request") or {}
    term = request.get("term")
    if not term:
        return {"industry_lookup_context": None}

    lookup = lookup_companies_by_industry(str(term))
    return {
        "industry_lookup_context": {
            "term": lookup["term"],
            "matched_company_count": lookup["matched_company_count"],
            "matched_companies_preview": lookup["company_names"][:20],
            "company_names": lookup["company_names"],
            "base_filters": prepare.get("base_filters") or {},
            "source_path": lookup["source_path"],
            "source_url": lookup["source_url"],
        }
    }
