import os
import sqlite3
import functools
import sqlglot
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

from src.configs.config import GEMINI_API_KEY, GENERATION_MODEL, DB_PATH, get_logger
from src.configs.prompts import RDB_SQL_GEN_PROMPT, RDB_ANSWER_PROMPT
from src.graphs.state import State

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
            
            # 3. SELECT * 사용 금지 (Schema Leakage 보안 결함 원천 차단)
            if list(parsed.find_all(sqlglot.exp.Star)):
                logger.warning(f"[Guardrail] 🚨 SELECT * 쿼리 감지. AST-level 차단")
                return "Error: 성능 및 보안 정책 상 'SELECT *' 구문은 사용할 수 없으며, 반드시 필요한 컬럼명을 지정해야 합니다."

            # 4. 금지된 민감/시스템 컬럼 파싱 감지
            forbidden_columns = {"id", "file_name", "is_embedded"}
            for column in parsed.find_all(sqlglot.exp.Column):
                if column.name.lower() in forbidden_columns:
                    logger.warning(f"[Guardrail] 🚨 민감한 시스템 컬럼 접근 감지: {column.name}")
                    return f"Error: '{column.name}'은(는) 비공개 시스템 컬럼이므로 조회할 권한이 없습니다."

            # 5. LIMIT 강제 할당 (SQL DoS 및 대량 조회 차단)
            # 쿼리에 LIMIT이 없거나 기존 값이 5를 초과할 경우 무조건 5로 덮어씌움 (안내 문구를 위해 에러로 떨구지 않고 Rewrite만 진행)
            has_limit = parsed.args.get("limit")
            if not has_limit:
                parsed = parsed.limit(5)
                logger.info("[Guardrail] ⚖️ 쿼리에 LIMIT 구문이 없어 LIMIT 5를 강제 주입(Rewrite)했습니다.")
            else:
                # 사용자가 지정한 한도가 5를 초과하는 경우 5로 하향조정
                limit_val_expr = has_limit.expression
                if isinstance(limit_val_expr, sqlglot.exp.Literal) and limit_val_expr.is_int:
                    limit_int = int(limit_val_expr.this)
                    if limit_int > 5:
                        parsed.args["limit"].set("expression", sqlglot.exp.Literal.number(5))
                        logger.info(f"[Guardrail] ⚖️ 사용자 쿼리의 LIMIT 값이 {limit_int}로 5를 초과하여 LIMIT 5로 하향조정(Rewrite)했습니다.")
            
            # AST 트리를 다시 안전한 쿼리 문자열로 복원
            safe_query = parsed.sql(dialect='sqlite')
                
        except Exception as e:
            logger.warning(f"[Guardrail] 🚨 쿼리 형태 파싱 실패 (잠재적 공격 차단): {e}")
            return f"Error: 쿼리 구문 분석 실패로 인해 보안상 실행이 차단되었습니다. ({str(e)})"
            
        return func(safe_query, *args, **kwargs)
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
    llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL, 
        google_api_key=GEMINI_API_KEY, 
        temperature=0.0
    )
    answer_prompt = PromptTemplate.from_template(RDB_ANSWER_PROMPT)
    ans_chain = answer_prompt | llm | StrOutputParser()
    
    answer = ""
    for chunk in ans_chain.stream({"question": query, "db_result": str(db_result)}):
        answer += chunk
        print(chunk, end="", flush=True) 
    print()
    
    return {
        "rdb_result": str(db_result), 
        "generation": answer,
        "chat_history": [("사용자", state["question"]), ("AI", answer)]
    }
