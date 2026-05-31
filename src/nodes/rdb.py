import functools
import os
import sqlite3

import sqlglot
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from src.configs.config import DB_PATH, get_logger
from src.llms.factory import build_chat_model
from src.configs.prompts import RDB_ANSWER_PROMPT, RDB_SQL_GEN_PROMPT
from src.graphs.state import State
from src.nodes.stock_price import stock_price_tools

logger = get_logger(__name__)


def rdb_sql_gen_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    llm = build_chat_model(temperature=0.0)

    prompt = PromptTemplate.from_template(RDB_SQL_GEN_PROMPT)
    chain = prompt | llm | StrOutputParser()
    sql_query = chain.invoke({"question": query}).strip()
    sql_query = sql_query.replace("```sql", "").replace("```", "").strip()
    return {"sql_query": sql_query}


def sql_guardrail(func):
    @functools.wraps(func)
    def wrapper(query: str, *args, **kwargs):
        try:
            parsed = sqlglot.parse_one(query, dialect="sqlite")
            allowed_tables = {"reports"}

            for table in parsed.find_all(sqlglot.exp.Table):
                if table.name.lower() not in allowed_tables:
                    logger.warning(f"[Guardrail] unauthorized table access attempt: {table.name}")
                    return f"Error: table access blocked by guardrail ({table.name})"

            if not isinstance(parsed, sqlglot.exp.Select):
                logger.warning("[Guardrail] non-SELECT query blocked")
                return "Error: only SELECT queries are allowed"

        except Exception as exc:
            logger.warning(f"[Guardrail] query parse failed and was blocked: {exc}")
            return f"Error: query parse failed and was blocked ({exc})"

        return func(query, *args, **kwargs)

    return wrapper


@sql_guardrail
def execute_sql(query: str):
    db_uri = f"file:{os.path.abspath(DB_PATH)}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        columns = [description[0] for description in cursor.description] if cursor.description else []
        return {"columns": columns, "rows": rows}
    except Exception as exc:
        return f"Error: {exc}"
    finally:
        conn.close()


def rdb_execute_node(state: State) -> dict:
    sql_query = state["sql_query"]
    db_result = execute_sql(sql_query)

    if "Error:" in str(db_result):
        err_msg = (
            "데이터베이스 조회 중 문제가 발생했습니다. "
            "읽기 전용 제약이나 SQL 가드레일에 의해 차단되었을 수 있습니다."
        )
        return {
            "rdb_result": str(db_result),
            "generation": err_msg,
            "chat_history": [("사용자", state["question"]), ("AI", err_msg)],
        }

    query = state.get("rewritten_query", state["question"])
    answer_prompt = PromptTemplate.from_template(RDB_ANSWER_PROMPT)
    llm = build_chat_model(temperature=0.2).bind_tools(stock_price_tools)

    tool_context_message = HumanMessage(
        content=(
            "당신은 금융 데이터 분석 AI입니다.\n"
            "아래 RDB 조회 결과만으로 충분하면 바로 답변하세요.\n"
            "최신 주가가 꼭 필요할 때만 `get_stock_price` 도구를 호출하세요.\n\n"
            f"사용자 원질문: {state['question']}\n"
            f"재작성 질의: {query}\n\n"
            f"{answer_prompt.format(question=query, db_result=str(db_result))}"
        )
    )
    ai_msg: AIMessage = llm.invoke([tool_context_message])

    if ai_msg.tool_calls:
        return {
            "rdb_result": str(db_result),
            "messages": [tool_context_message, ai_msg],
        }

    answer = ai_msg.content
    if isinstance(answer, list):
        answer = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in answer
        )

    return {
        "rdb_result": str(db_result),
        "generation": answer,
        "messages": [tool_context_message, ai_msg],
    }
