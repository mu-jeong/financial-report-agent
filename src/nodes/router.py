from pydantic import BaseModel, Field, field_validator
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate

from src.configs.config import GEMINI_API_KEY, GENERATION_MODEL, get_logger
from src.configs.prompts import ROUTER_PROMPT
from src.graphs.state import State

logger = get_logger(__name__)

class RouteDecision(BaseModel):
    """Router 판단 결과 구조체"""
    route: str = Field(
        description="판단 결과 플래그: 메타데이터 기반이면 'rdb', 본문 문서 내용 분석이 필요하다면 'vectordb', 주식 가격(주가) 조회가 필요하다면 'stock_price'를 반환합니다."
    )

    @field_validator("route", mode="after")
    @classmethod
    def validate_route(cls, v: str) -> str:
        """라우팅 값이 유효한지 검증하는 Pydantic Validator"""
        cleaned = v.strip().lower()
        if cleaned not in ["rdb", "vectordb", "stock_price"]:
            logger.warning(f"[Validator] 예기치 않은 라우팅 값 감지: '{v}'. 'vectordb'로 폴백합니다.")
            return "vectordb"
        return cleaned

def router_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL, 
        google_api_key=GEMINI_API_KEY, 
        temperature=0.0
    ).with_structured_output(RouteDecision)
    
    prompt = PromptTemplate.from_template(ROUTER_PROMPT)
    chain = prompt | llm
    
    try:
        decision = chain.invoke({"question": query})
        route = decision.route # validator에서 이미 정제됨
    except Exception as e:
        logger.warning(f"[Router] 구조화된 응답 파싱에 실패하여 기본값으로 대체합니다. ({e})")
        route = "vectordb"
        
    return {"route": route}
