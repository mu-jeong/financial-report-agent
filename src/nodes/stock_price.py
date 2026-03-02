from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode
import FinanceDataReader as fdr
import pandas as pd
from typing import Optional
from datetime import datetime, timedelta

from src.configs.config import GEMINI_API_KEY, GENERATION_MODEL, get_logger

logger = get_logger(__name__)

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
    matched = df[df['Name'] == company_name]
    if not matched.empty:
        return str(matched.iloc[0]['Code'])
    matched = df[df['Name'].str.contains(company_name, na=False)]
    if not matched.empty:
        return str(matched.iloc[0]['Code'])
    return None


@tool
def get_stock_price(company_name: str) -> str:
    """한국거래소(KRX) 상장 종목의 최근 주가 데이터를 조회합니다.
    
    Args:
        company_name: 주가를 조회할 대한민국 상장 회사명 (예: '삼성전자', 'SK하이닉스')
        
    Returns:
        최근 5일치 주가 데이터가 담긴 마크다운 테이블 문자열
    """
    ticker = get_ticker(company_name)
    if not ticker:
        return f"'{company_name}' 종목 코드를 한국거래소(KRX) 상장 목록에서 찾을 수 없습니다."
    
    try:
        start_date = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        df = fdr.DataReader(ticker, start_date)
        if df.empty:
            return f"최근 '{company_name}' 주가 데이터가 없습니다."
        
        df_recent = df.tail(5)
        return f"종목명: {company_name} (코드: {ticker})\n\n최근 주가 데이터:\n{df_recent.to_markdown()}"
    except Exception as e:
        logger.error(f"[FinanceDataReader] 주가 조회 중 오류 발생: {e}")
        return f"'{company_name}' 주가 정보를 가져오는 중 오류가 발생했습니다: {e}"


# 외부에서 import 하여 사용할 tool 목록 및 ToolNode
stock_price_tools = [get_stock_price]
stock_price_tool_node = ToolNode(stock_price_tools)
