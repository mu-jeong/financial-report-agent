import os
import json
import sqlite3
import functools
import sqlglot
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.messages import AIMessage

from src.configs.config import GEMINI_API_KEY, GENERATION_MODEL, DB_PATH, get_logger
from src.configs.prompts import RDB_SQL_GEN_PROMPT, RDB_ANSWER_PROMPT
from src.graphs.state import State
from src.nodes.stock_price import stock_price_tools

logger = get_logger(__name__)

def rdb_sql_gen_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL, 
        google_api_key=GEMINI_API_KEY, 
        temperature=0.0
    )
    
    prompt = PromptTemplate.from_template(RDB_SQL_GEN_PROMPT)
    chain = prompt | llm | StrOutputParser()
    sql_query = chain.invoke({"question": query}).strip()
    
    sql_query = sql_query.replace("```sql", "").replace("```", "").strip()
    return {"sql_query": sql_query}

def sql_guardrail(func):
    @functools.wraps(func)
    def wrapper(query: str, *args, **kwargs):
        try:
            parsed = sqlglot.parse_one(query, dialect='sqlite')
            allowed_tables = {"reports"}
            
            # 1. 대상 테이블 검사 (reports 테이블만 허용)
            for table in parsed.find_all(sqlglot.exp.Table):
                if table.name.lower() not in allowed_tables:
                    logger.warning(f"[Guardrail] 🚨 허가되지 않은 테이블 접근 시도: {table.name}")
                    return f"Error: 가드레일 정책에 의해 허가되지 않은 테이블({table.name}) 접근이 차단되었습니다."
            
            # 2. SELECT 구문 유무 구조 검사 (CUD 스크립트 실행 방지)
            if not isinstance(parsed, sqlglot.exp.Select):
                logger.warning(f"[Guardrail] 🚨 SELECT 외의 쿼리 실행 시도 차단")
                return "Error: 가드레일 정책에 의해 데이터 조작(CUD) 명령은 차단되었습니다."
                
        except Exception as e:
            logger.warning(f"[Guardrail] 🚨 쿼리 형태 파싱 실패 (잠재적 공격 차단): {e}")
            return f"Error: 쿼리 구문 분석 실패로 인해 보안상 실행이 차단되었습니다. ({str(e)})"
            
        return func(query, *args, **kwargs)
    return wrapper

@sql_guardrail
def execute_sql(query: str):
    db_uri = f"file:{os.path.abspath(DB_PATH)}?mode=ro"
    
    conn = sqlite3.connect(db_uri, uri=True)
    cursor = conn.cursor()
    try:
        cursor.execute(query)
        res = cursor.fetchall()
        column_names = [description[0] for description in cursor.description] if cursor.description else []
        return {"columns": column_names, "rows": res}
    except Exception as e:
        return f"Error: {e}"
    finally:
        conn.close()

def rdb_execute_node(state: State) -> dict:
    sql_query = state["sql_query"]
    db_result = execute_sql(sql_query)
    
    if "Error:" in str(db_result) and ("가드레일 정책" in str(db_result) or "readonly database" in str(db_result)):
        err_msg = "죄송합니다. 데이터베이스를 수정하거나 삭제하는 명령은 보안상 실행할 수 없도록 가드레일에 의해 차단되었습니다."
        return {
            "rdb_result": str(db_result),
            "generation": err_msg,
            "chat_history": [("사용자", state["question"]), ("AI", err_msg)]
        }
    
    query = state.get("rewritten_query", state["question"])

    # stock_price tool을 bind하여 LLM이 필요 시 주가 조회를 호출할 수 있도록 함
    llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL, 
        google_api_key=GEMINI_API_KEY, 
        temperature=0.0
    ).bind_tools(stock_price_tools)

    answer_prompt = PromptTemplate.from_template(RDB_ANSWER_PROMPT)
    chain = answer_prompt | llm | StrOutputParser()

    # tool_calls 여부 확인을 위해 먼저 AIMessage를 직접 받음
    raw_llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL,
        google_api_key=GEMINI_API_KEY,
        temperature=0.0
    ).bind_tools(stock_price_tools)

    formatted_prompt = answer_prompt.format(question=query, db_result=str(db_result))
    ai_msg: AIMessage = raw_llm.invoke(formatted_prompt)

    # LLM이 tool 호출을 요청했는지 확인
    if ai_msg.tool_calls:
        logger.info(f"[RdbExecuteNode] LLM이 주가 조회 tool 호출 요청: {ai_msg.tool_calls}")
        return {
            "rdb_result": str(db_result),
            "messages": [ai_msg],  # ToolNode가 읽을 수 있도록 messages에 저장
        }

    # ai_msg.content가 리스트(Gemini 멀티파트 등)인 경우 대비하여 문자열로 변환
    answer = ai_msg.content
    if isinstance(answer, list):
        answer = "".join([part.get("text", "") if isinstance(part, dict) else str(part) for part in answer])
    return {
        "rdb_result": str(db_result), 
        "generation": answer,
        "chat_history": [("사용자", state["question"]), ("AI", answer)]
    }
