import operator
from typing import Annotated, TypedDict, Optional
from langchain_core.messages import BaseMessage

class State(TypedDict):
    question: str
    messages: Annotated[list[BaseMessage], operator.add]  # ToolNode I/O용 메시지 목록
    rewritten_query: str  # 재작성된 검색용 쿼리 (항상 실행되는 노드)
    uses_chat_history: Optional[bool]  # query_rewrite가 이전 대화 맥락을 검색어에 반영했는지 여부
    followup_scope_intent: Optional[bool]  # 현재 질문이 직전 검색 범위를 가리키는 후속 질문인지 여부
    route: str            # 'rdb' or 'vectordb' (항상 실행되는 노드)
    search_filters: Optional[dict]  # VectorDB 검색 시 적용할 메타데이터 필터 {'target_name': '...', 'broker': '...'}
    temporal_context: Optional[dict]  # 상대/명시 날짜 표현을 구체 날짜 범위로 해석한 정보
    prior_search_scope: Optional[dict]  # 직전 답변의 검색 범위. 후속 질문에서 명시 조건이 없을 때 재사용
    active_scope: Optional[dict]  # LangGraph thread에 유지되는 현재 대화의 활성 검색 범위
    scope_source: Optional[str]  # search_filters가 이전 검색 범위에서 온 경우의 출처 표시
    selection_context: Optional[dict]  # DB aggregation에서 선택된 대상/범위 컨텍스트
    routing_context: Optional[dict]  # search_scope_node가 router에 전달하는 결정 힌트
    retrieval_plan: Optional[dict]  # VectorDB 경로 내부의 검색 전략(preflight/bucket 등)
    scope_selection_request: Optional[dict]  # 조건부 scope_selection_node 실행 요청
    scope_decision: Optional[dict]  # 후속 질문을 위한 이전 답변 섹션/범위 매칭 결정
    scope_prepare: Optional[dict]  # rewritten_query 생성 전 병렬 범위 준비 결과
    industry_lookup_context: Optional[dict]  # 기업 리포트 범위 설정을 위한 섹터/기업군 조회 결과
    
    # --- 아래 필드들은 라우팅 경로(분기)에 따라 값이 없을 수도 있으므로 Optional 처리 ---
    sql_query: Optional[str]        # RDB에서 사용된 SQL (RDB 경로)
    rdb_result: Optional[object]       # RDB 조회 결과 (RDB 경로)
    rdb_sources: Optional[list]     # RDB 결과에서 추출한 참고 문서 목록
    rerank_info: Optional[list]     # 재정렬된 문서/검색된 문서 정보 로깅용 (VectorDB 경로)
    monitoring_metrics: Optional[dict]  # Monitoring Mode에서 쓰는 단계별 compact 지표
    generation: Optional[str]       # 최종 답변 (예외 발생 시 등)
    no_vector_results: Optional[bool]
    memory_retry_attempted: Optional[bool]
