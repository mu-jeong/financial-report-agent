from pydantic import BaseModel, Field, field_validator
from langchain_core.prompts import PromptTemplate

from src.configs.config import get_logger
from src.configs.prompts import ROUTER_PROMPT
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
    """Choose the execution route after search scope has been resolved.

    Metadata filters, prior-scope reuse, top-target selection, and rewrite
    sanitization belong to ``search_scope_node``. This router intentionally
    consumes only routing hints and falls back to the LLM route classifier when
    no deterministic route is available.
    """
    query = state.get("rewritten_query", state["question"])
    routing_context = state.get("routing_context") or {}

    route_hint = routing_context.get("route_hint")
    if route_hint in {"rdb", "vectordb"}:
        return {"route": route_hint}

    if routing_context.get("has_vector_intent"):
        return {"route": "vectordb"}

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

    return {"route": route}
