from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Overwrite

from src.core.followup_scope import build_answer_scope_index
from src.graphs.state import State
from src.nodes.industry_lookup import industry_lookup_node
from src.nodes.query_rewrite import query_rewrite_node
from src.nodes.rdb import rdb_execute_node, rdb_sql_gen_node
from src.nodes.router import router_node
from src.nodes.scope_selection import scope_selection_node
from src.nodes.search_scope import (
    rdb_scope_preflight_node,
    search_scope_merge_node,
    search_scope_prepare_node,
    vectordb_scope_preflight_node,
)
from src.nodes.stock_price import stock_price_tool_node
from src.nodes.vectordb import vectordb_node
from src.nodes.vectordb_comparison import company_comparison_graph


def build_active_scope_from_state(state: State) -> dict | None:
    """Build the durable search scope that should carry to the next thread turn."""
    if state.get("no_vector_results"):
        return None
    search_filters = dict(state.get("search_filters") or {})
    temporal_context = state.get("temporal_context")
    sources = state.get("rerank_info") or state.get("rdb_sources") or []
    file_names: list[str] = []
    seen_file_names = set()
    for source in sources:
        file_name = (source or {}).get("file_name")
        if file_name and file_name != "-" and file_name not in seen_file_names:
            seen_file_names.add(file_name)
            file_names.append(file_name)

    if not search_filters and not temporal_context and not file_names:
        return None

    active_scope = {
        "route": state.get("route"),
        "search_filters": search_filters,
        "temporal_context": temporal_context,
        "scope_source": state.get("scope_source"),
    }
    if file_names:
        active_scope["file_names"] = file_names
    active_scope["answer_scope_index"] = build_answer_scope_index(active_scope, sources)
    return active_scope


def turn_prepare_node(_state: State) -> dict:
    """Start a fresh turn without leaking volatile checkpointed output."""
    return {
        "messages": Overwrite([]),
        "generation": None,
        "rerank_info": None,
        "citation_contract": None,
        "monitoring_metrics": None,
        "no_vector_results": False,
        "memory_retry_attempted": False,
        "retrieval_plan": None,
        "scope_selection_request": None,
        "scope_decision": None,
        "scope_prepare": None,
        "industry_lookup_context": None,
        "selection_context": None,
        "routing_context": None,
        "sql_query": None,
        "sql_params": None,
        "rdb_query_shape": None,
        "rdb_result": None,
        "rdb_sources": None,
        "rdb_missing_targets": None,
        "vector_run_id": str(uuid4()),
        "vector_attempt_id": 0,
        "vector_outcome": None,
        "vector_retryable": None,
    }


def should_retry_vectordb_without_memory(state: State) -> bool:
    """Retry VectorDB search once when history-influenced retrieval found nothing."""
    retryable = state.get("vector_retryable")
    no_result = bool(retryable) if retryable is not None else bool(
        state.get("no_vector_results")
    )
    return no_result and not bool(state.get("memory_retry_attempted"))


def clear_short_term_memory_retry_node(state: State) -> dict:
    """Prepare a one-time VectorDB retry that ignores only rewritten memory.

    The retry should remove conversation-expanded query text, but it must keep
    deterministic metadata constraints such as report_date_start/end,
    target_name, broker, report_type, and top-target selection context. Dropping
    those filters can turn a scoped query like "이번 주" into an all-period
    VectorDB search and mix unrelated reports into the answer.
    """
    result = {
        "rewritten_query": state["question"],
        "generation": None,
        "rerank_info": None,
        "citation_contract": None,
        "messages": Overwrite([]),
        "monitoring_metrics": None,
        "no_vector_results": False,
        "memory_retry_attempted": True,
        "vector_attempt_id": int(state.get("vector_attempt_id") or 0) + 1,
        "vector_outcome": None,
        "vector_retryable": None,
    }
    if retrieval_plan := state.get("retrieval_plan"):
        retry_plan = dict(retrieval_plan)
        retry_plan.pop("expected_revision", None)
        retry_plan.pop("attempt_id", None)
        result["retrieval_plan"] = retry_plan
    return result


def vector_dispatcher_node(_state: State) -> dict:
    """Stable retry anchor for choosing one VectorDB execution strategy."""
    return {}


def select_vector_execution(state: State) -> str:
    """Choose single retrieval or the standard dynamic Send comparison."""
    plan = state.get("retrieval_plan") or {}
    plan_type = plan.get("type")
    targets = plan.get("target_names") or plan.get("targets") or []
    if plan_type == "too_many_targets" or len(targets) > 5:
        return "too_many_targets"
    if plan_type != "company_comparison" or not 2 <= len(targets) <= 5:
        return "vectordb_node"
    return "company_comparison"


def too_many_targets_node(state: State) -> dict:
    """Ask for scope reduction instead of silently truncating target names."""
    plan = state.get("retrieval_plan") or {}
    targets = list(plan.get("target_names") or plan.get("targets") or [])
    max_targets = int(plan.get("max_targets") or 5)
    preview = ", ".join(str(target) for target in targets)
    return {
        "generation": (
            f"한 번에 비교할 수 있는 기업은 최대 {max_targets}개입니다. "
            f"대상을 {max_targets}개 이하로 줄여 다시 질문해 주세요."
            + (f"\n요청 대상: {preview}" if preview else "")
        ),
        "messages": [],
        "rerank_info": [],
        "citation_contract": None,
        "no_vector_results": False,
        "vector_outcome": "too_many_targets",
        "vector_retryable": False,
        "monitoring_metrics": {
            "comparison": {
                "status": "too_many_targets",
                "target_count": len(targets),
                "max_targets": max_targets,
            }
        },
    }


def final_response_node(state: State) -> dict:
    """Generate a final answer only when tool output needs to be folded back in."""
    from src.llms.factory import build_chat_model
    from src.llms.generation_observability import (
        invoke_chat_with_observability,
        merge_generation_metrics,
    )
    from src.utils.citations import (
        CITATION_CONTRACT_LEGACY,
        CITATION_CONTRACT_VALID,
        remove_unavailable_citations,
        remove_unavailable_document_references,
        validate_citation_contract,
    )

    def append_missing_target_notice(value: object) -> str:
        text = str(value)
        missing_targets = state.get("rdb_missing_targets") or []
        if missing_targets and "조회 결과가 없는 기업:" not in text:
            text += "\n\n조회 결과가 없는 기업: " + ", ".join(
                str(target) for target in missing_targets
            )
        return text

    def clean_citations(value: object) -> str:
        text = append_missing_target_notice(value)
        sources = state.get("rerank_info") or []
        validation = validate_citation_contract(
            sources,
            state.get("citation_contract"),
        )
        if validation["status"] == CITATION_CONTRACT_VALID:
            source_count = int(validation["document_count"])
            return remove_unavailable_document_references(
                text,
                source_count=source_count,
            )
        elif validation["status"] == CITATION_CONTRACT_LEGACY:
            source_count = len(sources)
        else:
            # A marked contract is authoritative. If it is malformed, keep the
            # answer intact rather than reinterpreting document citations as
            # legacy passage citations.
            return text
        return remove_unavailable_citations(text, source_count=source_count)

    answer = state.get("generation")
    if answer:
        answer = clean_citations(answer)
        result = {
            "generation": answer,
        }
        if state.get("citation_contract") is not None:
            result["citation_contract"] = state["citation_contract"]
        if active_scope := build_active_scope_from_state(state):
            result["active_scope"] = active_scope
        return result

    messages = state.get("messages", [])
    message_delta = []
    monitoring_metrics = None
    if not messages:
        answer = "최종 응답을 생성하지 못했습니다."
    else:
        llm = build_chat_model(temperature=0.2)
        response, generation_call = invoke_chat_with_observability(llm, messages)
        monitoring_metrics = dict(state.get("monitoring_metrics") or {})
        monitoring_metrics["generation"] = merge_generation_metrics(
            monitoring_metrics.get("generation"),
            generation_call,
            phase="tool_followup_answer",
        )
        answer = response.content
        if isinstance(answer, list):
            answer = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in answer
            )
        answer = clean_citations(answer)
        if not isinstance(messages[-1], AIMessage):
            message_delta = [response]

    result = {
        "generation": answer,
    }
    if state.get("citation_contract") is not None:
        result["citation_contract"] = state["citation_contract"]
    if message_delta:
        result["messages"] = message_delta
    if monitoring_metrics is not None:
        result["monitoring_metrics"] = monitoring_metrics
    if active_scope := build_active_scope_from_state(state):
        result["active_scope"] = active_scope
    return result


def build_graph():
    workflow = StateGraph(State)

    workflow.add_node("turn_prepare", turn_prepare_node)
    workflow.add_node("query_rewrite", query_rewrite_node)
    workflow.add_node("search_scope_prepare", search_scope_prepare_node)
    workflow.add_node("industry_lookup", industry_lookup_node)
    workflow.add_node("search_scope_merge", search_scope_merge_node)
    workflow.add_node("scope_selection", scope_selection_node)
    workflow.add_node("router", router_node)
    workflow.add_node("rdb_scope_preflight", rdb_scope_preflight_node)
    workflow.add_node("vectordb_scope_preflight", vectordb_scope_preflight_node)
    workflow.add_node("rdb_sql_gen_node", rdb_sql_gen_node)
    workflow.add_node("rdb_execute_node", rdb_execute_node)
    workflow.add_node("vector_dispatcher", vector_dispatcher_node)
    workflow.add_node("vectordb_node", vectordb_node)
    workflow.add_node("company_comparison", company_comparison_graph)
    workflow.add_node("too_many_targets", too_many_targets_node)
    workflow.add_node("clear_short_term_memory_retry", clear_short_term_memory_retry_node)
    workflow.add_node("stock_price_tools", stock_price_tool_node)
    workflow.add_node("final_response_node", final_response_node)

    workflow.add_edge(START, "turn_prepare")
    workflow.add_edge("turn_prepare", "query_rewrite")
    workflow.add_edge("turn_prepare", "search_scope_prepare")
    workflow.add_edge("search_scope_prepare", "industry_lookup")
    workflow.add_edge(["query_rewrite", "industry_lookup"], "search_scope_merge")

    def after_search_scope(state: State) -> str:
        if state.get("scope_selection_request"):
            return "scope_selection"
        return "router"

    workflow.add_conditional_edges(
        "search_scope_merge",
        after_search_scope,
        {
            "scope_selection": "scope_selection",
            "router": "router",
        },
    )
    workflow.add_edge("scope_selection", "router")

    def decide_next(state: State) -> str:
        if state["route"] == "rdb":
            return "rdb_scope_preflight"
        return "vectordb_scope_preflight"

    workflow.add_conditional_edges(
        "router",
        decide_next,
        {
            "rdb_scope_preflight": "rdb_scope_preflight",
            "vectordb_scope_preflight": "vectordb_scope_preflight",
        },
    )

    workflow.add_edge("rdb_scope_preflight", "rdb_sql_gen_node")
    workflow.add_edge("vectordb_scope_preflight", "vector_dispatcher")
    workflow.add_edge("rdb_sql_gen_node", "rdb_execute_node")

    workflow.add_conditional_edges(
        "vector_dispatcher",
        select_vector_execution,
        {
            "vectordb_node": "vectordb_node",
            "company_comparison": "company_comparison",
            "too_many_targets": "too_many_targets",
        },
    )

    def after_generation(state: State) -> str:
        messages = state.get("messages", [])
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            return "stock_price_tools"
        return "final_response_node"

    workflow.add_conditional_edges(
        "rdb_execute_node",
        after_generation,
        {
            "stock_price_tools": "stock_price_tools",
            "final_response_node": "final_response_node",
        },
    )

    def after_vectordb(state: State) -> str:
        if should_retry_vectordb_without_memory(state):
            return "clear_short_term_memory_retry"
        return after_generation(state)

    vector_result_routes = {
        "clear_short_term_memory_retry": "clear_short_term_memory_retry",
        "stock_price_tools": "stock_price_tools",
        "final_response_node": "final_response_node",
    }
    for node_name in (
        "vectordb_node",
        "company_comparison",
    ):
        workflow.add_conditional_edges(
            node_name,
            after_vectordb,
            vector_result_routes,
        )
    workflow.add_edge("too_many_targets", "final_response_node")

    workflow.add_edge("clear_short_term_memory_retry", "vector_dispatcher")
    workflow.add_edge("stock_price_tools", "final_response_node")
    workflow.add_edge("final_response_node", END)

    memory = MemorySaver()
    return workflow.compile(checkpointer=memory, name="finance_chat")


graph_app = build_graph()
