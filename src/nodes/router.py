from pydantic import BaseModel, Field, field_validator
from langchain_core.prompts import PromptTemplate

from src.configs.config import get_logger
from src.configs.prompts import ROUTER_PROMPT
from src.core.metadata_filters import infer_search_filters, resolve_temporal_context
from src.graphs.state import State
from src.llms.factory import build_chat_model

logger = get_logger(__name__)

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
)


class RouteDecision(BaseModel):
    """Structured router decision."""

    route: str = Field(description="Route flag: 'rdb' or 'vectordb'")

    @field_validator("route", mode="after")
    @classmethod
    def validate_route(cls, value: str) -> str:
        """Normalize invalid route values to the safe VectorDB path."""
        cleaned = value.strip().lower()
        if cleaned not in ["rdb", "vectordb"]:
            logger.warning(
                "[Router] Unexpected route value '%s'; falling back to 'vectordb'.",
                value,
            )
            return "vectordb"
        return cleaned


def router_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    current_question_filters = infer_search_filters(state["question"])
    current_temporal_context = resolve_temporal_context(state["question"])
    search_filters = infer_search_filters(query)
    temporal_context = current_temporal_context or resolve_temporal_context(query)
    if temporal_context:
        search_filters.update(
            {
                "report_date_start": temporal_context["report_date_start"],
                "report_date_end": temporal_context["report_date_end"],
            }
        )

    scope_source = None
    prior_search_scope = state.get("prior_search_scope") or {}
    followup_scope_intent = bool(state.get("followup_scope_intent"))
    if (
        followup_scope_intent
        and not current_temporal_context
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
            current_non_temporal_filters = {
                key: value
                for key, value in current_question_filters.items()
                if key not in {"report_date_start", "report_date_end"}
            }
            prior_filters.update(current_non_temporal_filters)
            search_filters = prior_filters
            temporal_context = prior_search_scope.get("temporal_context")
            scope_source = "prior_search_scope"

    intent_text = f"{state['question']} {query}"
    if any(keyword in intent_text for keyword in VECTORDB_INTENT_KEYWORDS):
        return {
            "route": "vectordb",
            "search_filters": search_filters,
            "temporal_context": temporal_context,
            "scope_source": scope_source,
        }

    llm = build_chat_model(temperature=0.0).with_structured_output(RouteDecision)

    prompt = PromptTemplate.from_template(ROUTER_PROMPT)
    chain = prompt | llm

    try:
        decision = chain.invoke({"question": query})
        route = decision.route
    except Exception as exc:
        logger.warning(
            "[Router] Structured route parsing failed; falling back to 'vectordb'. (%s)",
            exc,
        )
        route = "vectordb"

    return {
        "route": route,
        "search_filters": search_filters,
        "temporal_context": temporal_context,
        "scope_source": scope_source,
    }
