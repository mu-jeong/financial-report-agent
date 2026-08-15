import functools
import re
import time
from collections import Counter

import sqlglot
from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate

from src.configs.config import get_logger
from src.core.db_manager import get_connection
from src.llms.factory import build_chat_model
from src.llms.generation_observability import (
    invoke_chat_with_observability,
    merge_generation_metrics,
)
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
MULTI_TARGET_ROW_LIMIT = 20
MULTI_TARGET_TOTAL_ROW_LIMIT = 100

COUNT_INTENT_KEYWORDS = ("개수", "건수", "몇 개", "몇개", "몇 건", "몇건", "count", "통계")
LATEST_INTENT_KEYWORDS = ("가장 최근", "최신", "최근", "마지막")
LIST_OR_TREND_INTENT_KEYWORDS = ("목록", "리스트", "추세", "흐름", "변화")


def _ordered_target_names(target_names) -> tuple[str, ...]:
    """Return non-empty target names in caller-provided order without duplicates."""
    ordered: list[str] = []
    seen: set[str] = set()
    for value in target_names or ():
        if not isinstance(value, str) or not value.strip():
            raise ValueError("target_names must contain non-empty strings")
        normalized = value.strip()
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    if not ordered:
        raise ValueError("at least one target_name is required")
    return tuple(ordered)


def _parse_single_select(query: str):
    statements = [
        statement
        for statement in sqlglot.parse(query, dialect="sqlite")
        if statement is not None
    ]
    if len(statements) != 1 or not isinstance(statements[0], sqlglot.exp.Select):
        raise ValueError("target-scoped SQL must be exactly one SELECT statement")
    return statements[0]


def _references_target_name(expression) -> bool:
    return any(
        column.name.lower() == "target_name"
        for column in expression.find_all(sqlglot.exp.Column)
    )


def _remove_target_predicates(expression):
    """Remove simple target predicates while retaining the rest of an AND tree."""
    if isinstance(expression, sqlglot.exp.Paren):
        stripped = _remove_target_predicates(expression.this)
        return sqlglot.exp.Paren(this=stripped) if stripped is not None else None
    if isinstance(expression, sqlglot.exp.And):
        left = _remove_target_predicates(expression.this)
        right = _remove_target_predicates(expression.expression)
        if left is None:
            return right
        if right is None:
            return left
        return sqlglot.exp.and_(left, right)
    if _references_target_name(expression):
        if isinstance(expression, sqlglot.exp.Or):
            raise ValueError("target_name inside OR cannot be safely parameterized")
        # A whole comparison is safe to replace. Nested target references (NOT,
        # functions, arithmetic, subqueries) are intentionally rejected.
        allowed = (
            sqlglot.exp.EQ,
            sqlglot.exp.NEQ,
            sqlglot.exp.In,
            sqlglot.exp.Like,
            sqlglot.exp.ILike,
            sqlglot.exp.Is,
        )
        if not isinstance(expression, allowed):
            raise ValueError("unsupported target_name predicate")
        return None
    return expression.copy()


def validate_target_scoped_query(
    query: str,
    params,
    target_names,
) -> None:
    """Validate the invariant tying one target_name IN clause to bound params."""
    targets = _ordered_target_names(target_names)
    bound_params = tuple(params or ())
    if bound_params != targets:
        raise ValueError("sql_params must exactly match ordered target_names")

    parsed = _parse_single_select(query)
    report_tables = [
        table
        for table in parsed.find_all(sqlglot.exp.Table)
        if table.name.lower() == "reports"
    ]
    if len(report_tables) != 1:
        raise ValueError("target-scoped SQL must read reports exactly once")

    target_in_clauses = [
        clause
        for clause in parsed.find_all(sqlglot.exp.In)
        if isinstance(clause.this, sqlglot.exp.Column)
        and clause.this.name.lower() == "target_name"
    ]
    if len(target_in_clauses) != 1:
        raise ValueError("SQL must contain exactly one target_name IN clause")

    target_predicates = [
        predicate
        for predicate_type in (
            sqlglot.exp.EQ,
            sqlglot.exp.NEQ,
            sqlglot.exp.In,
            sqlglot.exp.Like,
            sqlglot.exp.ILike,
            sqlglot.exp.Is,
        )
        for predicate in parsed.find_all(predicate_type)
        if _references_target_name(predicate)
    ]
    if len(target_predicates) != 1 or target_predicates[0] is not target_in_clauses[0]:
        raise ValueError("target_name may only be constrained by the bound IN clause")
    ancestor = target_in_clauses[0].parent
    while ancestor is not None and not isinstance(ancestor, sqlglot.exp.Where):
        if isinstance(ancestor, (sqlglot.exp.Or, sqlglot.exp.Not)):
            raise ValueError("target_name IN cannot be nested under OR or NOT")
        ancestor = ancestor.parent
    if ancestor is None:
        raise ValueError("target_name IN must be in a WHERE clause")

    target_placeholders = list(
        target_in_clauses[0].find_all(sqlglot.exp.Placeholder)
    )
    all_placeholders = list(parsed.find_all(sqlglot.exp.Placeholder))
    if len(target_placeholders) != len(targets):
        raise ValueError("target_name placeholder count does not match targets")
    if len(all_placeholders) != len(target_placeholders):
        raise ValueError("unexpected SQL placeholders without bound parameters")

    has_target_partition = any(
        any(
            column.name.lower() == "target_name"
            for column in window.args.get("partition_by") or []
            for column in column.find_all(sqlglot.exp.Column)
        )
        for window in parsed.find_all(sqlglot.exp.Window)
    )
    if parsed.args.get("limit") is not None and not has_target_partition:
        raise ValueError("global LIMIT is not allowed for multi-company queries")

    if any(parsed.find_all(sqlglot.exp.Count)):
        groups = list(parsed.find_all(sqlglot.exp.Group))
        if not groups or not any(
            column.name.lower() == "target_name"
            for group in groups
            for column in group.find_all(sqlglot.exp.Column)
        ):
            raise ValueError("multi-company count must GROUP BY target_name")


def build_target_scoped_query(query: str, target_names) -> tuple[str, tuple[str, ...]]:
    """Replace LLM-authored target filters with a deterministic bound IN scope."""
    targets = _ordered_target_names(target_names)
    parsed = _parse_single_select(query)
    if any(parsed.find_all(sqlglot.exp.Placeholder)):
        raise ValueError("LLM-generated placeholders are not accepted")

    report_tables = [
        table
        for table in parsed.find_all(sqlglot.exp.Table)
        if table.name.lower() == "reports"
    ]
    if len(report_tables) != 1:
        raise ValueError("target-scoped SQL must read reports exactly once")
    report_select = report_tables[0].find_ancestor(sqlglot.exp.Select)
    if report_select is None:
        raise ValueError("reports table must belong to a SELECT")

    # Remove every LLM-authored target predicate, including one on an outer CTE
    # SELECT, so target values can only enter through SQLite bindings.
    for select in parsed.find_all(sqlglot.exp.Select):
        where = select.args.get("where")
        if where is None:
            continue
        stripped = _remove_target_predicates(where.this)
        select.set(
            "where",
            sqlglot.exp.Where(this=stripped) if stripped is not None else None,
        )

    table_alias = report_tables[0].alias
    target_column = sqlglot.exp.column("target_name", table=table_alias or None)
    target_scope = sqlglot.exp.In(
        this=target_column,
        expressions=[sqlglot.exp.Placeholder() for _ in targets],
    )
    report_select.where(target_scope, append=True, copy=False)
    scoped_query = parsed.sql(dialect="sqlite")
    validate_target_scoped_query(scoped_query, targets, targets)
    return scoped_query, targets


def _sql_literal(value: object) -> str:
    return sqlglot.exp.Literal.string(str(value)).sql(dialect="sqlite")


def _mandatory_scope_sql(filters: dict) -> list[str]:
    predicates: list[str] = []
    if filters.get("report_type"):
        predicates.append(f"report_type = {_sql_literal(filters['report_type'])}")
    elif filters.get("report_types"):
        values = tuple(dict.fromkeys(str(value) for value in filters["report_types"] or ()))
        if values:
            predicates.append(
                "report_type IN (" + ", ".join(_sql_literal(value) for value in values) + ")"
            )
    if filters.get("broker"):
        predicates.append(f"broker = {_sql_literal(filters['broker'])}")
    if filters.get("report_date_start"):
        predicates.append(
            f"report_date >= {_sql_literal(filters['report_date_start'])}"
        )
    if filters.get("report_date_end"):
        predicates.append(
            f"report_date <= {_sql_literal(filters['report_date_end'])}"
        )
    return predicates


def _multi_company_query_shape(question: str) -> dict[str, object]:
    normalized = str(question or "").casefold()
    if any(keyword in normalized for keyword in COUNT_INTENT_KEYWORDS) or re.search(
        r"(?:리포트|보고서)\s*수(?:\s|는|가|를|와|과|$)",
        normalized,
    ):
        return {"type": "count_by_target", "per_target_limit": None}
    if any(keyword in normalized for keyword in LIST_OR_TREND_INTENT_KEYWORDS):
        return {
            "type": "list_per_target",
            "per_target_limit": MULTI_TARGET_ROW_LIMIT,
        }
    if any(keyword in normalized for keyword in LATEST_INTENT_KEYWORDS):
        match = re.search(r"(?:최근|최신)\s*(\d+)\s*(?:개|건)", normalized)
        limit = int(match.group(1)) if match else 1
        return {
            "type": "latest_per_target",
            "per_target_limit": min(max(limit, 1), MULTI_TARGET_ROW_LIMIT),
        }
    return {
        "type": "list_per_target",
        "per_target_limit": MULTI_TARGET_ROW_LIMIT,
    }


def build_multi_company_query(
    question: str,
    search_filters: dict,
    *,
    query_shape: dict[str, object] | None = None,
) -> tuple[str, tuple[str, ...], dict[str, object]]:
    """Build one deterministic, scoped set query for every requested company."""
    filters = dict(search_filters or {})
    targets = _ordered_target_names(filters.get("target_names") or ())
    shape = dict(query_shape or _multi_company_query_shape(question))
    placeholders = ", ".join("?" for _ in targets)
    predicates = [f"target_name IN ({placeholders})", *_mandatory_scope_sql(filters)]
    where_sql = " AND ".join(predicates)

    if shape.get("type") == "count_by_target":
        query = (
            "SELECT target_name, COUNT(*) AS report_count "
            f"FROM reports WHERE {where_sql} "
            "GROUP BY target_name ORDER BY target_name ASC"
        )
        normalized_shape = {"type": "count_by_target", "per_target_limit": None}
    else:
        limit = min(
            max(int(shape.get("per_target_limit") or MULTI_TARGET_ROW_LIMIT), 1),
            MULTI_TARGET_ROW_LIMIT,
        )
        shape_type = (
            "latest_per_target"
            if shape.get("type") == "latest_per_target"
            else "list_per_target"
        )
        query = (
            "WITH ranked AS ("
            "SELECT report_type, report_date, target_name, broker, title, "
            "file_name, is_embedded, "
            "ROW_NUMBER() OVER (PARTITION BY target_name "
            "ORDER BY report_date DESC, id DESC, file_name ASC) "
            f"AS target_row_number FROM reports WHERE {where_sql}"
            ") "
            "SELECT report_type, report_date, target_name, broker, title, "
            "file_name, is_embedded FROM ranked "
            f"WHERE target_row_number <= {limit} "
            "ORDER BY target_row_number ASC, target_name ASC, "
            "report_date DESC, file_name ASC "
            f"LIMIT {MULTI_TARGET_TOTAL_ROW_LIMIT}"
        )
        normalized_shape = {"type": shape_type, "per_target_limit": limit}

    validate_target_scoped_query(query, targets, targets)
    return query, targets, normalized_shape


def normalize_multi_company_result(
    db_result,
    target_names,
    query_shape: dict[str, object],
) -> tuple[object, list[str]]:
    """Restore requested target order and explicit zero counts after SQL."""
    if not isinstance(db_result, dict):
        return db_result, list(_ordered_target_names(target_names))
    columns = list(db_result.get("columns") or [])
    rows = [tuple(row) for row in db_result.get("rows") or []]
    if "target_name" not in columns:
        raise ValueError("multi-company SQL result must include target_name")
    target_index = columns.index("target_name")
    requested = _ordered_target_names(target_names)
    position = {target: index for index, target in enumerate(requested)}
    grouped: dict[str, list[tuple]] = {target: [] for target in requested}
    for row in rows:
        if len(row) <= target_index:
            continue
        target = str(row[target_index])
        if target in grouped:
            grouped[target].append(row)
    missing = [target for target in requested if not grouped[target]]
    if query_shape.get("type") == "count_by_target":
        count_index = columns.index("report_count")
        normalized_rows = []
        for target in requested:
            if grouped[target]:
                normalized_rows.append(grouped[target][0])
            else:
                row = [None] * len(columns)
                row[target_index] = target
                row[count_index] = 0
                normalized_rows.append(tuple(row))
    else:
        normalized_rows = sorted(
            rows,
            key=lambda row: (
                position.get(str(row[target_index]), len(position)),
                str(row[1] if len(row) > 1 else ""),
                str(row[5] if len(row) > 5 else ""),
            ),
        )
        # SQL already orders each target newest-first; preserve that order
        # while moving target groups into caller-requested order.
        normalized_rows = [
            row
            for target in requested
            for row in rows
            if str(row[target_index]) == target
        ]
    return {"columns": columns, "rows": normalized_rows}, missing


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
    target_names = state.get("target_names") or search_filters.get("target_names") or ()
    if len(target_names) > 1:
        multi_filters = dict(search_filters)
        multi_filters["target_names"] = list(target_names)
        sql_query, sql_params, query_shape = build_multi_company_query(
            query,
            multi_filters,
        )
        return {
            "sql_query": sql_query,
            "sql_params": sql_params,
            "rdb_query_shape": query_shape,
        }
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
def execute_sql(query: str, params=()):
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(query, tuple(params or ()))
        rows = [tuple(row) for row in cursor.fetchall()]
        columns = [description[0] for description in cursor.description] if cursor.description else []
        return {"columns": columns, "rows": rows}
    except Exception as exc:
        return f"Error: {exc}"
    finally:
        if conn is not None:
            conn.close()


def rdb_execute_node(state: State) -> dict:
    sql_query = state["sql_query"]
    sql_params = tuple(state.get("sql_params") or ())
    query_started = time.perf_counter_ns()
    target_names = state.get("target_names") or (
        state.get("search_filters") or {}
    ).get("target_names")
    query_shape = state.get("rdb_query_shape") or {}
    missing_targets: list[str] = []
    try:
        if sql_params and not target_names:
            raise ValueError("bound SQL requires canonical target_names in state")
        if target_names and len(target_names) > 1 and not sql_params:
            raise ValueError("multi-company SQL requires bound parameters")
        if sql_params and target_names:
            validate_target_scoped_query(sql_query, sql_params, target_names)
            expected_query, expected_params, expected_shape = build_multi_company_query(
                state.get("rewritten_query", state["question"]),
                {
                    **dict(state.get("search_filters") or {}),
                    "target_names": list(target_names),
                },
                query_shape=query_shape,
            )
            if (
                sql_query != expected_query
                or sql_params != expected_params
                or query_shape != expected_shape
            ):
                raise ValueError(
                    "SQL, params, or query shape does not match the mandatory scope"
                )
        db_result = (
            execute_sql(sql_query, params=sql_params)
            if sql_params
            else execute_sql(sql_query)
        )
        if sql_params and target_names and isinstance(db_result, dict):
            db_result, missing_targets = normalize_multi_company_result(
                db_result,
                target_names,
                query_shape,
            )
    except ValueError as exc:
        db_result = f"Error: target scope validation failed ({exc})"
    query_ns = max(0, time.perf_counter_ns() - query_started)
    rdb_metrics = {
        "sql_query": sql_query,
        "query_ns": query_ns,
        "row_count": None,
        "column_count": None,
        "guardrail_blocked": "Error:" in str(db_result),
    }
    if query_shape:
        rdb_metrics["query_shape"] = query_shape
        rdb_metrics["requested_targets"] = list(target_names or ())
        rdb_metrics["missing_targets"] = missing_targets

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
    multi_target_context = ""
    if target_names:
        requested_text = ", ".join(str(target) for target in target_names)
        missing_text = ", ".join(missing_targets) if missing_targets else "없음"
        multi_target_context = (
            "\n[복수 기업 조회 범위]\n"
            f"요청 기업: {requested_text}\n"
            f"조회 결과가 없는 기업: {missing_text}\n"
            "결과가 없는 기업을 다른 기업의 값으로 추정하지 마세요.\n"
        )

    tool_context_message = HumanMessage(
        content=(
            "당신은 금융 데이터 분석 AI입니다.\n"
            "아래 RDB 조회 결과만으로 충분하면 바로 답변하세요.\n"
            "최신 주가가 꼭 필요할 때만 `get_stock_price` 도구를 호출하세요.\n\n"
            f"사용자 원질문: {state['question']}\n"
            f"재작성 질의: {query}\n\n"
            f"{temporal_context_text}"
            f"{multi_target_context}"
            f"{answer_prompt.format(question=query, db_result=format_rdb_result_for_answer(db_result, rdb_summary))}"
        )
    )
    ai_msg, generation_call = invoke_chat_with_observability(
        llm,
        [tool_context_message],
    )
    generation_metrics = merge_generation_metrics(
        None,
        generation_call,
        phase="rdb_answer",
    )

    if ai_msg.tool_calls:
        return {
            "rdb_result": db_result,
            "messages": [tool_context_message, ai_msg],
            "rdb_sources": rdb_sources,
            "rdb_missing_targets": missing_targets,
            "monitoring_metrics": {
                "rdb": rdb_metrics,
                "generation": generation_metrics,
            },
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
        "rdb_missing_targets": missing_targets,
        "monitoring_metrics": {
            "rdb": rdb_metrics,
            "generation": generation_metrics,
        },
    }
