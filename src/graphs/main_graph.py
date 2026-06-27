from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.graphs.state import State
from src.nodes.industry_lookup import industry_lookup_node
from src.nodes.query_rewrite import query_rewrite_node
from src.nodes.rdb import rdb_execute_node, rdb_sql_gen_node
from src.nodes.router import router_node
from src.nodes.scope_selection import scope_selection_node
from src.nodes.search_scope import search_scope_merge_node, search_scope_prepare_node
from src.nodes.stock_price import stock_price_tool_node
from src.nodes.vectordb import vectordb_node
from src.core.followup_scope import build_answer_scope_index


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


def should_retry_vectordb_without_memory(state: State) -> bool:
    """Retry VectorDB search once when history-influenced retrieval found nothing."""
    return bool(state.get("no_vector_results")) and not bool(state.get("memory_retry_attempted"))


def clear_short_term_memory_retry_node(state: State) -> dict:
    """Prepare a one-time VectorDB retry that ignores only rewritten memory.

    The retry should remove conversation-expanded query text, but it must keep
    deterministic metadata constraints such as report_date_start/end,
    target_name, broker, report_type, and top-target selection context. Dropping
    those filters can turn a scoped query like "이번 주" into an all-period
    VectorDB search and mix unrelated reports into the answer.
    """
    return {
        "rewritten_query": state["question"],
        "generation": None,
        "rerank_info": None,
        "no_vector_results": False,
        "memory_retry_attempted": True,
    }


def final_response_node(state: State) -> dict:
    """Generate a final answer only when tool output needs to be folded back in."""
    from src.llms.factory import build_chat_model
    from src.utils.citations import remove_unavailable_citations

    answer = state.get("generation")
    if answer:
        source_count = len(state.get("rerank_info") or [])
        answer = remove_unavailable_citations(str(answer), source_count=source_count)
        result = {
            "generation": answer,
            "chat_history": [("사용자", state["question"]), ("AI", answer)],
        }
        if active_scope := build_active_scope_from_state(state):
            result["active_scope"] = active_scope
        return result

    messages = state.get("messages", [])
    message_delta = []
    if not messages:
        answer = "최종 응답을 생성하지 못했습니다."
    else:
        llm = build_chat_model(temperature=0.2)
        response = llm.invoke(messages)
        answer = response.content
        if isinstance(answer, list):
            answer = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in answer
            )
        source_count = len(state.get("rerank_info") or [])
        answer = remove_unavailable_citations(str(answer), source_count=source_count)
        if not isinstance(messages[-1], AIMessage):
            message_delta = [response]

    result = {
        "generation": answer,
        "chat_history": [("사용자", state["question"]), ("AI", answer)],
    }
    if message_delta:
        result["messages"] = message_delta
    if active_scope := build_active_scope_from_state(state):
        result["active_scope"] = active_scope
    return result


def build_graph():
    workflow = StateGraph(State)

    workflow.add_node("query_rewrite", query_rewrite_node)
    workflow.add_node("search_scope_prepare", search_scope_prepare_node)
    workflow.add_node("industry_lookup", industry_lookup_node)
    workflow.add_node("search_scope_merge", search_scope_merge_node)
    workflow.add_node("scope_selection", scope_selection_node)
    workflow.add_node("router", router_node)
    workflow.add_node("rdb_sql_gen_node", rdb_sql_gen_node)
    workflow.add_node("rdb_execute_node", rdb_execute_node)
    workflow.add_node("vectordb_node", vectordb_node)
    workflow.add_node("clear_short_term_memory_retry", clear_short_term_memory_retry_node)
    workflow.add_node("stock_price_tools", stock_price_tool_node)
    workflow.add_node("final_response_node", final_response_node)

    workflow.add_edge(START, "query_rewrite")
    workflow.add_edge(START, "search_scope_prepare")
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
            return "rdb_sql_gen_node"
        return "vectordb_node"

    workflow.add_conditional_edges(
        "router",
        decide_next,
        {
            "rdb_sql_gen_node": "rdb_sql_gen_node",
            "vectordb_node": "vectordb_node",
        },
    )

    workflow.add_edge("rdb_sql_gen_node", "rdb_execute_node")

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

    workflow.add_conditional_edges(
        "vectordb_node",
        after_vectordb,
        {
            "clear_short_term_memory_retry": "clear_short_term_memory_retry",
            "stock_price_tools": "stock_price_tools",
            "final_response_node": "final_response_node",
        },
    )

    workflow.add_edge("clear_short_term_memory_retry", "vectordb_node")
    workflow.add_edge("stock_price_tools", "final_response_node")
    workflow.add_edge("final_response_node", END)

    memory = MemorySaver()
    return workflow.compile(checkpointer=memory)


graph_app = build_graph()
