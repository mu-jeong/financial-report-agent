from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

from src.configs.config import GEMINI_API_KEY, GENERATION_MODEL, get_logger
from src.configs.prompts import QUERY_REWRITE_PROMPT
from src.graphs.state import State

logger = get_logger(__name__)

def query_rewrite_node(state: State) -> dict:
    history = state.get("chat_history", [])
    question = state["question"]
    
    if not history:
        # 최초 질문이거나 맥락이 없을 땐 그대로 사용
        return {"rewritten_query": question}
        
    # 최근 최대 3턴의 대화만 문자열로 변환 (너무 오래된 맥락 제거)
    history_text = ""
    for role, msg in history[-3:]:
        history_text += f"[{role}]\n{msg}\n\n"
        
    llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL, 
        google_api_key=GEMINI_API_KEY, 
        temperature=0.0
    )
    
    prompt = PromptTemplate.from_template(QUERY_REWRITE_PROMPT)
    chain = prompt | llm | StrOutputParser()
    
    rewritten = chain.invoke({"chat_history": history_text.strip(), "question": question}).strip()
    
    if rewritten != question and rewritten:
        logger.info(f"[AI] 📝 사용자 의도 재분석 완료 (검색어 변경): '{rewritten}'")
    else:
        rewritten = question
        
    return {"rewritten_query": rewritten}
