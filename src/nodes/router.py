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
    search_filters = infer_search_filters(query)
    temporal_context = resolve_temporal_context(state["question"]) or resolve_temporal_context(query)
    if temporal_context:
        search_filters.update(
            {
                "report_date_start": temporal_context["report_date_start"],
                "report_date_end": temporal_context["report_date_end"],
            }
        )

    if any(keyword in query for keyword in VECTORDB_INTENT_KEYWORDS):
        return {
            "route": "vectordb",
            "search_filters": search_filters,
            "temporal_context": temporal_context,
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

    return {"route": route, "search_filters": search_filters, "temporal_context": temporal_context}
