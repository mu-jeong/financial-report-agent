import operator
from typing import Annotated, TypedDict, Optional
from langchain_core.messages import BaseMessage

class State(TypedDict):
    question: str
    chat_history: Annotated[list, operator.add]     # 이전 대화 기록 [(역할, 내용), ... ] 형태
    messages: Annotated[list[BaseMessage], operator.add]  # ToolNode I/O용 메시지 목록
    rewritten_query: str  # 재작성된 검색용 쿼리
    route: str            # 'rdb' or 'vectordb'
    sql_query: str        # RDB에서 사용된 SQL
    rdb_result: str       # RDB 조회 결과
    faiss_context: str    # VectorDB에서 검색된 컨텍스트
    stock_price_result: str   # 주가 데이터 조회 결과
    rerank_info: list     # 재정렬된 문서/검색된 문서 정보 로깅용
    generation: str       # 최종 답변
