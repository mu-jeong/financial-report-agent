import functools
import time
from collections import Counter

import sqlglot
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from src.configs.config import DB_PATH, get_logger
from src.core.db_manager import get_connection
from src.llms.factory import build_chat_model
from src.configs.prompts import RDB_ANSWER_PROMPT, RDB_SQL_GEN_PROMPT
from src.graphs.state import State
from src.nodes.stock_price import stock_price_tools

logger = get_logger(__name__)


SOURCE_COLUMNS = (
    "report_type",
    "report_date",
    "target_name",
    "broker",
    "title",
    "file_name",
)

SUMMARY_COLUMNS = ("report_date", "report_type", "broker", "target_name")


def extract_sources_from_rdb_result(db_result) -> list[dict]:
    """Build source metadata rows from SELECT results that include file_name."""
    if not isinstance(db_result, dict):
        return []
    columns = list(db_result.get("columns") or [])
    if "file_name" not in columns:
        return []

    sources: list[dict] = []
    seen_files: set[str] = set()
    for row in db_result.get("rows") or []:
        row_map = dict(zip(columns, row))
        file_name = row_map.get("file_name")
        if not file_name or file_name in seen_files:
            continue
        seen_files.add(file_name)
        sources.append(
            {
                "rank": len(sources) + 1,
                **{
                    column: row_map.get(column, "-")
                    for column in SOURCE_COLUMNS
                },
                "score": 0.0,
                "rerank_score": None,
                "recency_score": None,
                "final_score": None,
            }
        )
    return sources


def summarize_rdb_result(db_result) -> dict:
    """Return deterministic counts so the LLM does not recount large row sets."""
    if not isinstance(db_result, dict):
        return {}
    columns = list(db_result.get("columns") or [])
    rows = list(db_result.get("rows") or [])
    summary = {
        "row_count": len(rows),
        "column_count": len(columns),
    }
    for column in SUMMARY_COLUMNS:
        if column not in columns:
            continue
        index = columns.index(column)
        counts = Counter(
            str(row[index])
            for row in rows
            if len(row) > index and row[index] not in (None, "", "null")
        )
        summary[f"by_{column}"] = dict(
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        )
    return summary


def format_rdb_result_for_answer(db_result, summary: dict) -> str:
    """Format RDB output with exact precomputed counts before raw rows."""
    return (
        "[DB_RESULT_SUMMARY]\n"
        f"{summary}\n\n"
        "[RAW_DB_RESULT]\n"
        f"{db_result}"
    )


def rdb_sql_gen_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    temporal_context = state.get("temporal_context")
    if temporal_context:
        query = f"{query}\n\n[상대 날짜 해석]\n{temporal_context['description']}"
    search_filters = state.get("search_filters") or {}
    if search_filters:
        filter_lines = "\n".join(f"- {key}: {value}" for key, value in search_filters.items())
        query = f"{query}\n\n[적용해야 할 메타데이터 필터]\n{filter_lines}"
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
            cte_names = {
                cte.alias.lower()
                for cte in parsed.find_all(sqlglot.exp.CTE)
                if cte.alias
            }

            for table in parsed.find_all(sqlglot.exp.Table):
                table_name = table.name.lower()
                if table_name in cte_names:
                    continue
                if table_name not in allowed_tables:
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
    conn = get_connection(DB_PATH)
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        rows = [tuple(row) for row in cursor.fetchall()]
        columns = [description[0] for description in cursor.description] if cursor.description else []
        return {"columns": columns, "rows": rows}
    except Exception as exc:
        return f"Error: {exc}"
    finally:
        conn.close()


def rdb_execute_node(state: State) -> dict:
    sql_query = state["sql_query"]
    query_started = time.perf_counter_ns()
    db_result = execute_sql(sql_query)
    query_ns = max(0, time.perf_counter_ns() - query_started)
    rdb_metrics = {
        "sql_query": sql_query,
        "query_ns": query_ns,
        "row_count": None,
        "column_count": None,
        "guardrail_blocked": "Error:" in str(db_result),
    }

    if "Error:" in str(db_result):
        err_msg = (
            "데이터베이스 조회 중 문제가 발생했습니다. "
            "읽기 전용 제약이나 SQL 가드레일에 의해 차단되었을 수 있습니다."
        )
        return {
            "rdb_result": str(db_result),
            "generation": err_msg,
            "monitoring_metrics": {"rdb": rdb_metrics},
        }

    if isinstance(db_result, dict):
        rdb_metrics["row_count"] = len(db_result.get("rows") or [])
        rdb_metrics["column_count"] = len(db_result.get("columns") or [])
    rdb_summary = summarize_rdb_result(db_result)
    if rdb_summary:
        rdb_metrics["summary"] = rdb_summary
    rdb_sources = extract_sources_from_rdb_result(db_result)

    query = state.get("rewritten_query", state["question"])
    temporal_context = state.get("temporal_context")
    temporal_context_text = ""
    if temporal_context:
        temporal_context_text = f"\n[상대 날짜 해석]\n{temporal_context['description']}\n"
    answer_prompt = PromptTemplate.from_template(RDB_ANSWER_PROMPT)
    llm = build_chat_model(temperature=0.2).bind_tools(stock_price_tools)

    tool_context_message = HumanMessage(
        content=(
            "당신은 금융 데이터 분석 AI입니다.\n"
            "아래 RDB 조회 결과만으로 충분하면 바로 답변하세요.\n"
            "최신 주가가 꼭 필요할 때만 `get_stock_price` 도구를 호출하세요.\n\n"
            f"사용자 원질문: {state['question']}\n"
            f"재작성 질의: {query}\n\n"
            f"{temporal_context_text}"
            f"{answer_prompt.format(question=query, db_result=format_rdb_result_for_answer(db_result, rdb_summary))}"
        )
    )
    ai_msg: AIMessage = llm.invoke([tool_context_message])

    if ai_msg.tool_calls:
        return {
            "rdb_result": db_result,
            "messages": [tool_context_message, ai_msg],
            "rdb_sources": rdb_sources,
            "monitoring_metrics": {"rdb": rdb_metrics},
        }

    answer = ai_msg.content
    if isinstance(answer, list):
        answer = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in answer
        )

    return {
        "rdb_result": db_result,
        "generation": answer,
        "messages": [tool_context_message, ai_msg],
        "rdb_sources": rdb_sources,
        "monitoring_metrics": {"rdb": rdb_metrics},
    }
