from pydantic import BaseModel, Field, field_validator
from langchain_core.prompts import PromptTemplate

from src.configs.config import get_logger
from src.configs.prompts import ROUTER_PROMPT
from src.core.metadata_filters import infer_search_filters
from src.graphs.state import State
from src.llms.factory import build_chat_model

logger = get_logger(__name__)


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

    return {"route": route, "search_filters": search_filters}
