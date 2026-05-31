import os

from langchain_community.vectorstores import FAISS
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import PromptTemplate
from src.configs.config import (
    FAISS_DIR,
    SEARCH_TOP_K,
    USE_RERANKER,
    get_logger,
)
from src.configs.prompts import VECTORDB_PROMPT
from src.core.db_manager import fetch_parent_content
from src.core.metadata_filters import filter_docs_with_scores, infer_search_filters
from src.graphs.state import State
from src.nodes.stock_price import stock_price_tools
from src.llms.embeddings import build_embeddings_model
from src.llms.factory import build_chat_model
from src.utils.ranker import get_ranker

logger = get_logger(__name__)


def build_embeddings_fn():
    return build_embeddings_model()


def vectordb_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    search_filters = state.get("search_filters") or infer_search_filters(query)
    if not os.path.exists(FAISS_DIR):
        msg = "faiss_db/ 폴더가 없어 검색을 진행할 수 없습니다. 먼저 임베딩 파이프라인을 실행해 주세요."
        logger.warning(msg)
        return {"generation": msg, "chat_history": [("사용자", state["question"]), ("AI", msg)]}

    embeddings_fn = build_embeddings_fn()
    faiss_store = FAISS.load_local(
        FAISS_DIR,
        embeddings_fn,
        allow_dangerous_deserialization=True,
    )

    fetch_k = max(SEARCH_TOP_K * 8, SEARCH_TOP_K)
    docs_with_scores = faiss_store.similarity_search_with_score(query, k=fetch_k)
    docs_with_scores = filter_docs_with_scores(docs_with_scores, search_filters)
    if not docs_with_scores:
        if search_filters:
            filter_text = ", ".join(f"{key}={value}" for key, value in search_filters.items())
            msg = (
                "지정된 조건에 맞는 임베딩 완료 리포트를 찾지 못했습니다. "
                f"(적용 조건: {filter_text})"
            )
        else:
            msg = "관련 리포트를 찾지 못했습니다."
        logger.info(msg)
        return {"generation": msg, "chat_history": [("사용자", state["question"]), ("AI", msg)]}

    passages = []
    seen_parent_ids = set()
    for rank, (doc, score) in enumerate(docs_with_scores):
        meta = doc.metadata
        page_content = doc.page_content
        parent_id = meta.get("parent_id")

        if parent_id:
            if parent_id in seen_parent_ids:
                continue
            seen_parent_ids.add(parent_id)
            parent_content = fetch_parent_content(parent_id)
            if parent_content:
                page_content = parent_content

        passages.append(
            {
                "id": rank,
                "text": page_content,
                "score": score,
                "meta": meta,
            }
        )

    if USE_RERANKER:
        ranker, req_cls = get_ranker()
        rerank_request = req_cls(query=query, passages=passages)
        top_passages = ranker.rerank(rerank_request)[:3]
    else:
        top_passages = passages[:3]

    context_parts = []
    rerank_info = []
    for rank, result in enumerate(top_passages, 1):
        meta = result["meta"]
        source_info = (
            f"[{rank}] {meta.get('target_name', '알수없음')} "
            f"({meta.get('report_date', '날짜없음')}) - {meta.get('title', '제목없음')}"
        )
        context_parts.append(
            f"\n--- 문서 {rank} ---\n[출처: {source_info}]\n{result['text']}\n"
        )
        rerank_info.append(
            {
                "rank": rank,
                "target_name": meta.get("target_name", "-"),
                "report_date": meta.get("report_date", "-"),
                "broker": meta.get("broker", "-"),
                "file_name": meta.get("file_name", "-"),
                "score": float(result.get("score", 0.0)),
            }
        )

    context_text = "".join(context_parts)
    prompt = PromptTemplate.from_template(VECTORDB_PROMPT)
    llm = build_chat_model(temperature=0.2).bind_tools(stock_price_tools)

    tool_context_message = HumanMessage(
        content=(
            "당신은 증권사 리포트 분석 AI입니다.\n"
            "아래 리포트 문맥만으로 충분하면 바로 답변하세요.\n"
            "최신 주가가 꼭 필요할 때만 `get_stock_price` 도구를 호출하세요.\n\n"
            f"사용자 원질문: {state['question']}\n"
            f"재작성 질의: {query}\n\n"
            f"{prompt.format(context=context_text, question=query)}"
        )
    )
    ai_msg: AIMessage = llm.invoke([tool_context_message])

    if ai_msg.tool_calls:
        return {
            "faiss_context": context_text,
            "rerank_info": rerank_info,
            "search_filters": search_filters,
            "messages": [tool_context_message, ai_msg],
        }

    answer = ai_msg.content
    if isinstance(answer, list):
        answer = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in answer
        )

    return {
        "faiss_context": context_text,
        "rerank_info": rerank_info,
        "generation": answer,
        "search_filters": search_filters,
        "messages": [tool_context_message, ai_msg],
    }
