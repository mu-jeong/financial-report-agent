from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.graphs.state import State
from src.nodes.query_rewrite import query_rewrite_node
from src.nodes.rdb import rdb_execute_node, rdb_sql_gen_node
from src.nodes.router import router_node
from src.nodes.scope_selection import scope_selection_node
from src.nodes.search_scope import search_scope_node
from src.nodes.stock_price import stock_price_tool_node
from src.nodes.vectordb import vectordb_node


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
        "faiss_context": None,
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
        return {
            "generation": answer,
            "chat_history": [("사용자", state["question"]), ("AI", answer)],
        }

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
    return result


def build_graph():
    workflow = StateGraph(State)

    workflow.add_node("query_rewrite", query_rewrite_node)
    workflow.add_node("search_scope", search_scope_node)
    workflow.add_node("scope_selection", scope_selection_node)
    workflow.add_node("router", router_node)
    workflow.add_node("rdb_sql_gen_node", rdb_sql_gen_node)
    workflow.add_node("rdb_execute_node", rdb_execute_node)
    workflow.add_node("vectordb_node", vectordb_node)
    workflow.add_node("clear_short_term_memory_retry", clear_short_term_memory_retry_node)
    workflow.add_node("stock_price_tools", stock_price_tool_node)
    workflow.add_node("final_response_node", final_response_node)

    workflow.add_edge(START, "query_rewrite")
    workflow.add_edge("query_rewrite", "search_scope")

    def after_search_scope(state: State) -> str:
        if state.get("scope_selection_request"):
            return "scope_selection"
        return "router"

    workflow.add_conditional_edges(
        "search_scope",
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
