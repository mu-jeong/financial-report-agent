import os

from langchain_community.vectorstores import FAISS
from langchain_core.prompts import PromptTemplate
from langchain_core.messages import AIMessage, HumanMessage

from src.configs.config import FAISS_DIR, SEARCH_TOP_K, USE_RERANKER, get_logger
from src.configs.prompts import VECTORDB_PROMPT
from src.graphs.state import State
from src.utils.ranker import get_ranker
from src.core.db_manager import fetch_parent_content
from src.nodes.stock_price import stock_price_tools
from src.llms.embeddings import build_embeddings_model
from src.llms.factory import build_chat_model

logger = get_logger(__name__)


def build_embeddings_fn():
    return build_embeddings_model()


def vectordb_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    if not os.path.exists(FAISS_DIR):
        msg = "죄송합니다. faiss_db/ 폴더가 없습니다. 먼저 embed_pipeline.py를 실행하여 리포트를 임베딩해주세요."
        logger.warning(msg)
        return {"generation": msg, "chat_history": [("사용자", state["question"]), ("AI", msg)]}

    embeddings_fn = build_embeddings_fn()
    faiss_store = FAISS.load_local(
        FAISS_DIR,
        embeddings_fn,
        allow_dangerous_deserialization=True,
    )

    docs_with_scores = faiss_store.similarity_search_with_score(query, k=SEARCH_TOP_K)

    if not docs_with_scores:
        msg = "죄송합니다. 수집된 리포트에서는 관련 문서를 찾을 수 없습니다."
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
        rerank_results = ranker.rerank(rerank_request)
        top_passages = rerank_results[:3]
    else:
        top_passages = passages[:3]

    context_text = ""
    for rank, result in enumerate(top_passages, 1):
        meta = result["meta"]
        source_info = f"[{rank}] {meta.get('target_name', '알수없음')} ({meta.get('report_date', '날짜없음')}) - {meta.get('title', '제목없음')}"
        context_text += f"\n--- 문서 {rank} ---\n[출처: {source_info}]\n{result['text']}\n"

    llm = build_chat_model(temperature=0.2).bind_tools(stock_price_tools)

    prompt = PromptTemplate.from_template(VECTORDB_PROMPT)
    formatted_prompt = prompt.format(context=context_text, question=query)
    ai_msg: AIMessage = llm.invoke(formatted_prompt)

    rerank_info = []
    for rank, result in enumerate(top_passages, 1):
        meta = result["meta"]
        score = float(result.get("score", 0.0))
        rerank_info.append(
            {
                "rank": rank,
                "target_name": meta.get("target_name", "-"),
                "report_date": meta.get("report_date", "-"),
                "broker": meta.get("broker", "-"),
                "file_name": meta.get("file_name", "-"),
                "score": score,
            }
        )

    if ai_msg.tool_calls:
        logger.info(f"[VectordbNode] LLM이 주가 조회 tool 호출 요청: {ai_msg.tool_calls}")
        tool_context_message = HumanMessage(
            content=(
                f"사용자 질문: {state['question']}\n\n"
                f"재작성된 질문: {query}\n\n"
                f"검색 컨텍스트:\n{context_text}\n\n"
                "도구 호출 결과를 반영해 최종 답변을 완성하세요."
            )
        )
        return {
            "faiss_context": context_text,
            "rerank_info": rerank_info,
            "messages": [tool_context_message, ai_msg],
        }

    return {
        "faiss_context": context_text,
        "rerank_info": rerank_info,
    }
