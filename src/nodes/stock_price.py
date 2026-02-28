from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
import FinanceDataReader as fdr
import pandas as pd
from typing import Optional
from datetime import datetime, timedelta

from src.configs.config import GEMINI_API_KEY, GENERATION_MODEL, get_logger
from src.configs.prompts import STOCK_PRICE_PROMPT
from src.graphs.state import State

logger = get_logger(__name__)

class CompanyTarget(BaseModel):
    company_names: list[str] = Field(description="주가를 조회할 대한민국의 회사 이름 목록 (최대 5개)")

# 전역 캐싱용
_krx_stocks = None

def get_krx_stocks() -> pd.DataFrame:
    global _krx_stocks
    if _krx_stocks is None:
        try:
            _krx_stocks = fdr.StockListing('KRX-DESC')
        except Exception as e:
            logger.error(f"[FinanceDataReader] KRX 종목 목록 조회 실패: {e}")
            _krx_stocks = pd.DataFrame()
    return _krx_stocks

def get_ticker(company_name: str) -> Optional[str]:
    df = get_krx_stocks()
    if df.empty:
        return None
    # 정확히 일치하거나 포함되는 종목 검색
    matched = df[df['Name'] == company_name]
    if not matched.empty:
        return str(matched.iloc[0]['Code'])
    
    # 부분 일치 검색
    matched = df[df['Name'].str.contains(company_name, na=False)]
    if not matched.empty:
        return str(matched.iloc[0]['Code'])
    
    return None

def get_stock_price(company_name: str) -> str:
    ticker = get_ticker(company_name)
    if not ticker:
        return f"'{company_name}' 종목 코드를 한국거래소(KRX) 상장 목록에서 찾을 수 없습니다."
    
    try:
        start_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        df = fdr.DataReader(ticker, start_date)
        if df.empty:
            return f"최근 '{company_name}' 주가 데이터가 없습니다."
        
        # 최근 5일치 데이터
        df_recent = df.tail(5)
        # to_markdown()으로 깔끔한 마크다운 테이블 포맷팅 (tabulate 패키지 사용)
        return f"종목명: {company_name} (코드: {ticker})\n\n최근 주가 데이터:\n{df_recent.to_markdown()}"
    except Exception as e:
        logger.error(f"[FinanceDataReader] 주가 조회 중 오류 발생: {e}")
        return f"'{company_name}' 주가 정보를 가져오는 중 오류가 발생했습니다: {e}"

def stock_price_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    
    # 회사 이름 추출
    extract_llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL, 
        google_api_key=GEMINI_API_KEY, 
        temperature=0.0
    ).with_structured_output(CompanyTarget)
    
    logger.info(f"[StockPriceNode] 질문에서 주가 조회 대상 회사명 추출...")
    try:
        target = extract_llm.invoke(f"다음 사용자의 질문(또는 재작성된 질문)에서 주가를 조회할 회사 이름들의 목록을 추출하세요. '주식', '주가' 등의 단어는 제외하고 기업명만 도출하세요. 최대 5개까지만 추출합니다.\n질문: {query}")
        company_names = target.company_names
        logger.info(f"[StockPriceNode] 추출된 회사명 목록: {company_names}")
    except Exception as e:
        logger.warning(f"[StockPriceNode] 회사 이름 추출 중 오류 발생: {e}")
        return {"generation": "죄송합니다, 질문에서 정보 조회를 위한 회사 이름을 추출하는 데 실패했습니다."}
    
    if not company_names:
        return {"generation": "주가를 조회할 회사 이름을 찾지 못했습니다."}

    logger.info(f"[StockPriceNode] 주가 정보 조회 중...")
    stock_price_data = ""
    for company_name in company_names:
        stock_price_data += get_stock_price(company_name) + "\n\n"
    
    # 응답 생성
    answer_llm = ChatGoogleGenerativeAI(
        model=GENERATION_MODEL, 
        google_api_key=GEMINI_API_KEY, 
        temperature=0.0
    )
    prompt = PromptTemplate.from_template(STOCK_PRICE_PROMPT)
    chain = prompt | answer_llm
    
    logger.info(f"[StockPriceNode] 조회된 주가 데이터를 바탕으로 답변 생성 중...")
    try:
        response = chain.invoke({"stock_price_data": stock_price_data, "question": query})
        answer = response.content
    except Exception as e:
        logger.error(f"[StockPriceNode] 답변 생성 중 오류: {e}")
        answer = f"주가 분석에 실패했습니다. 다음 데이터를 참고하세요:\n\n{stock_price_data}"
        
    return {
        "stock_price_result": stock_price_data,
        "generation": answer,
        "chat_history": [("사용자", state["question"]), ("AI", answer)]
    }
