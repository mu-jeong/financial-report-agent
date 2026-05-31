from langchain_core.messages import AIMessage
from langchain_core.prompts import PromptTemplate
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.configs.prompts import RDB_ANSWER_PROMPT, VECTORDB_PROMPT
from src.graphs.state import State
from src.nodes.query_rewrite import query_rewrite_node
from src.nodes.rdb import rdb_execute_node, rdb_sql_gen_node
from src.nodes.router import router_node
from src.nodes.stock_price import stock_price_tool_node
from src.nodes.vectordb import vectordb_node


def final_response_node(state: State) -> dict:
    """검색 결과와 tool 결과를 바탕으로 최종 자연어 응답을 생성하는 노드."""
    from src.llms.factory import build_chat_model

    llm = build_chat_model(temperature=0.2)

    if state.get("generation"):
        answer = state["generation"]
        return {
            "generation": answer,
            "chat_history": [("사용자", state["question"]), ("AI", answer)],
        }

    messages = state.get("messages", [])
    if messages:
        response = llm.invoke(messages)
    else:
        query = state.get("rewritten_query", state["question"])
        route = state.get("route")

        if route == "rdb":
            db_result = state.get("rdb_result")
            if not db_result:
                return {"generation": "RDB 조회 결과가 없어 최종 답변을 생성하지 못했습니다."}

            prompt = PromptTemplate.from_template(RDB_ANSWER_PROMPT)
            response = llm.invoke(prompt.format(question=query, db_result=db_result))
        else:
            context_text = state.get("faiss_context")
            if not context_text:
                return {"generation": "검색 컨텍스트가 없어 최종 답변을 생성하지 못했습니다."}

            prompt = PromptTemplate.from_template(VECTORDB_PROMPT)
            response = llm.invoke(prompt.format(context=context_text, question=query))

    answer = response.content
    if isinstance(answer, list):
        answer = "".join([part.get("text", "") if isinstance(part, dict) else str(part) for part in answer])

    return {
        "generation": answer,
        "chat_history": [("사용자", state["question"]), ("AI", answer)],
    }


def build_graph():
    workflow = StateGraph(State)

    workflow.add_node("query_rewrite", query_rewrite_node)
    workflow.add_node("router", router_node)
    workflow.add_node("rdb_sql_gen_node", rdb_sql_gen_node)
    workflow.add_node("rdb_execute_node", rdb_execute_node)
    workflow.add_node("vectordb_node", vectordb_node)
    workflow.add_node("stock_price_tools", stock_price_tool_node)
    workflow.add_node("final_response_node", final_response_node)

    workflow.add_edge(START, "query_rewrite")
    workflow.add_edge("query_rewrite", "router")

    def decide_next(state: State) -> str:
        target = state["route"]
        if target == "rdb":
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

    def after_rdb_execute(state: State) -> str:
        """LLM이 tool_calls를 요청했으면 stock_price_tools로, 아니면 final_response_node."""
        messages = state.get("messages", [])
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            return "stock_price_tools"
        return "final_response_node"

    workflow.add_conditional_edges(
        "rdb_execute_node",
        after_rdb_execute,
        {
            "stock_price_tools": "stock_price_tools",
            "final_response_node": "final_response_node",
        },
    )

    def after_vectordb(state: State) -> str:
        """LLM이 tool_calls를 요청했으면 stock_price_tools로, 아니면 final_response_node."""
        messages = state.get("messages", [])
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].tool_calls:
            return "stock_price_tools"
        return "final_response_node"

    workflow.add_conditional_edges(
        "vectordb_node",
        after_vectordb,
        {
            "stock_price_tools": "stock_price_tools",
            "final_response_node": "final_response_node",
        },
    )

    workflow.add_edge("stock_price_tools", "final_response_node")
    workflow.add_edge("final_response_node", END)

    memory = MemorySaver()
    app = workflow.compile(checkpointer=memory)
    return app


graph_app = build_graph()
