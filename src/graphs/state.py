import operator
from typing import Annotated, TypedDict, Optional
from langchain_core.messages import BaseMessage

class State(TypedDict):
    question: str
    chat_history: Annotated[list, operator.add]     # 이전 대화 기록 [(역할, 내용), ... ] 형태
    messages: Annotated[list[BaseMessage], operator.add]  # ToolNode I/O용 메시지 목록
    rewritten_query: str  # 재작성된 검색용 쿼리 (항상 실행되는 노드)
    route: str            # 'rdb' or 'vectordb' (항상 실행되는 노드)
    search_filters: Optional[dict]  # VectorDB 검색 시 적용할 메타데이터 필터 {'target_name': '...', 'broker': '...'}
    
    # --- 아래 필드들은 라우팅 경로(분기)에 따라 값이 없을 수도 있으므로 Optional 처리 ---
    sql_query: Optional[str]        # RDB에서 사용된 SQL (RDB 경로)
    rdb_result: Optional[str]       # RDB 조회 결과 (RDB 경로)
    faiss_context: Optional[str]    # VectorDB에서 검색된 컨텍스트 (VectorDB 경로)
    stock_price_result: Optional[str]   # 주가 데이터 조회 결과 (Tool 호출 시)
    rerank_info: Optional[list]     # 재정렬된 문서/검색된 문서 정보 로깅용 (VectorDB 경로)
    generation: Optional[str]       # 최종 답변 (예외 발생 시 등)
