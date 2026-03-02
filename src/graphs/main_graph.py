from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import AIMessage

from src.graphs.state import State
from src.nodes.query_rewrite import query_rewrite_node
from src.nodes.router import router_node
from src.nodes.rdb import rdb_sql_gen_node, rdb_execute_node
from src.nodes.vectordb import vectordb_node
from src.nodes.stock_price import stock_price_tool_node

def build_graph():
    workflow = StateGraph(State)

    # ── 노드 등록 ──────────────────────────────────────────────
    workflow.add_node("query_rewrite", query_rewrite_node)
    workflow.add_node("router", router_node)
    workflow.add_node("rdb_sql_gen_node", rdb_sql_gen_node)
    workflow.add_node("rdb_execute_node", rdb_execute_node)
    workflow.add_node("vectordb_node", vectordb_node)
    workflow.add_node("stock_price_tools", stock_price_tool_node)

    # ── 기본 엣지 ──────────────────────────────────────────────
    workflow.add_edge(START, "query_rewrite")
    workflow.add_edge("query_rewrite", "router")

    # router → 각 노드 분기
    def decide_next(state: State) -> str:
        target = state["route"]
        if target == "rdb":
            return "rdb_sql_gen_node"
        return "vectordb_node"

    workflow.add_conditional_edges("router", decide_next, {
        "rdb_sql_gen_node": "rdb_sql_gen_node",
        "vectordb_node": "vectordb_node",
    })

    workflow.add_edge("rdb_sql_gen_node", "rdb_execute_node")

    # ── rdb_execute_node → tool or END ─────────────────────────
    def after_rdb_execute(state: State) -> str:
        """LLM이 tool_calls를 요청했으면 stock_price_tools로, 아니면 END."""
        messages = state.get("messages", [])
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            return "stock_price_tools"
        return END

    workflow.add_conditional_edges("rdb_execute_node", after_rdb_execute, {
        "stock_price_tools": "stock_price_tools",
        END: END,
    })

    # ── vectordb_node → tool or END ────────────────────────────
    def after_vectordb(state: State) -> str:
        """LLM이 tool_calls를 요청했으면 stock_price_tools로, 아니면 END."""
        messages = state.get("messages", [])
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            return "stock_price_tools"
        return END

    workflow.add_conditional_edges("vectordb_node", after_vectordb, {
        "stock_price_tools": "stock_price_tools",
        END: END,
    })

    # ToolNode 실행 후 종료
    workflow.add_edge("stock_price_tools", END)

    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)
    return app

# Singleton 인스턴스처럼 사용할 수 있도록 모듈 로드 시 생성
graph_app = build_graph()
