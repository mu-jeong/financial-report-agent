from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from graphs.state import State
from nodes.query_rewrite import query_rewrite_node
from nodes.router import router_node
from nodes.rdb import rdb_sql_gen_node, rdb_execute_node
from nodes.vectordb import vectordb_node

def build_graph():
    workflow = StateGraph(State)

    workflow.add_node("query_rewrite", query_rewrite_node)
    workflow.add_node("router", router_node)
    workflow.add_node("rdb_sql_gen_node", rdb_sql_gen_node)
    workflow.add_node("rdb_execute_node", rdb_execute_node)
    workflow.add_node("vectordb_node", vectordb_node)

    workflow.add_edge(START, "query_rewrite")
    workflow.add_edge("query_rewrite", "router")

    def decide_next(state: State):
        target = state["route"]
        if target == "rdb":
            return "rdb_sql_gen_node"
        return "vectordb_node"

    workflow.add_conditional_edges("router", decide_next, {
        "rdb_sql_gen_node": "rdb_sql_gen_node",
        "vectordb_node": "vectordb_node"
    })

    workflow.add_edge("rdb_sql_gen_node", "rdb_execute_node")

    workflow.add_edge("vectordb_node", END)
    workflow.add_edge("rdb_execute_node", END)

    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)
    return app

# Singleton 인스턴스처럼 사용할 수 있도록 모듈 로드 시 생성
graph_app = build_graph()
