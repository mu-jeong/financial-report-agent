import os
from datetime import datetime

from langchain_community.vectorstores import FAISS
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import PromptTemplate
from src.configs.config import (
    FAISS_DIR,
    RECENCY_WEIGHT,
    RERANK_CANDIDATE_MULTIPLIER,
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


def _metadata_aware_fetch_k(faiss_store: FAISS, search_filters: dict | None) -> int:
    """Fetch enough candidates before deterministic metadata filtering.

    FAISS does vector ranking first, while report dates live in metadata. If the
    user asks for "5월" and we only inspect the top few vector hits, all May
    documents can be dropped before the metadata filter gets a chance to match.
    """
    default_fetch_k = max(SEARCH_TOP_K * 8, SEARCH_TOP_K)
    if not search_filters:
        return default_fetch_k
    total_docs = len(getattr(faiss_store, "index_to_docstore_id", {}) or {})
    return max(default_fetch_k, total_docs)


def _build_passages(docs_with_scores: list[tuple]) -> list[dict]:
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
    return passages


def _parse_report_date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().toordinal()
    except ValueError:
        return None


def apply_recency_weight(passages: list[dict], weight: float = RECENCY_WEIGHT) -> list[dict]:
    """Boost newer reports after semantic ranking.

    The semantic rank remains the main signal. ``RECENCY_WEIGHT`` adds a small
    normalized bonus based on report_date so that similarly relevant passages
    prefer newer reports.
    """
    if not passages or weight <= 0:
        return passages

    ordinals = [
        parsed
        for passage in passages
        if (parsed := _parse_report_date(passage.get("meta", {}).get("report_date"))) is not None
    ]
    if not ordinals:
        return passages

    oldest, newest = min(ordinals), max(ordinals)
    span = max(newest - oldest, 1)
    weighted: list[dict] = []
    total = max(len(passages) - 1, 1)
    for index, passage in enumerate(passages):
        item = dict(passage)
        ordinal = _parse_report_date(item.get("meta", {}).get("report_date"))
        recency_score = ((ordinal - oldest) / span) if ordinal is not None else 0.0
        relevance_score = item.get("rerank_score")
        if relevance_score is None:
            # Preserve the current semantic order when no external rerank score exists.
            relevance_score = 1.0 - (index / total)
        final_score = float(relevance_score) + (weight * recency_score)
        item["recency_score"] = recency_score
        item["final_score"] = final_score
        weighted.append(item)

    return sorted(weighted, key=lambda item: item["final_score"], reverse=True)


def select_top_passages(query: str, docs_with_scores: list[tuple]) -> list[dict]:
    """Build passages and select final context entries using SEARCH_TOP_K."""
    passages = _build_passages(docs_with_scores)
    candidate_count = min(
        len(passages),
        max(SEARCH_TOP_K, SEARCH_TOP_K * max(RERANK_CANDIDATE_MULTIPLIER, 1)),
    )
    if USE_RERANKER:
        ranked = get_ranker().rerank(query, passages, candidate_count)
    else:
        ranked = passages[:candidate_count]
    return apply_recency_weight(ranked, RECENCY_WEIGHT)[:SEARCH_TOP_K]


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

    fetch_k = _metadata_aware_fetch_k(faiss_store, search_filters)
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

    top_passages = select_top_passages(query, docs_with_scores)

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
                "rerank_score": result.get("rerank_score"),
                "recency_score": result.get("recency_score"),
                "final_score": result.get("final_score"),
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
