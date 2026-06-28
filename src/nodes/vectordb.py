import os
from collections import Counter
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
from src.utils.citations import remove_unavailable_citations
from src.utils.ranker import get_ranker

logger = get_logger(__name__)


def build_embeddings_fn():
    return build_embeddings_model()


def _metadata_aware_fetch_k(faiss_store: FAISS, search_filters: dict | None) -> int:
    """metadata filtering 전에 충분한 후보를 가져옵니다."""
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
    """semantic ranking 이후 최신 리포트 가중치를 적용합니다."""
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
            # 외부 rerank score가 없으면 현재 semantic 순서를 유지합니다.
            relevance_score = 1.0 - (index / total)
        final_score = float(relevance_score) + (weight * recency_score)
        item["recency_score"] = recency_score
        item["final_score"] = final_score
        weighted.append(item)

    return sorted(weighted, key=lambda item: item["final_score"], reverse=True)


def _file_name_for_passage(passage: dict) -> str | None:
    file_name = (passage.get("meta") or {}).get("file_name")
    if not file_name or file_name == "-":
        return None
    return str(file_name)


def ensure_document_coverage(
    selected_passages: list[dict],
    docs_with_scores: list[tuple],
    *,
    max_passages: int = SEARCH_TOP_K,
    required_file_names: list[str] | tuple[str, ...] | str | None = None,
) -> list[dict]:
    """필터링된 문서마다 최소 1개 passage를 유지합니다."""
    if not selected_passages or max_passages <= 0:
        return selected_passages

    all_passages = _build_passages(docs_with_scores)
    best_by_file: dict[str, dict] = {}
    for passage in all_passages:
        file_name = _file_name_for_passage(passage)
        if file_name and file_name not in best_by_file:
            best_by_file[file_name] = passage

    if not best_by_file:
        return selected_passages[:max_passages]

    if isinstance(required_file_names, str):
        required_file_names = [required_file_names]

    required_files = [
        str(file_name)
        for file_name in (required_file_names or [])
        if file_name and file_name != "-" and str(file_name) in best_by_file
    ]
    if not required_files:
        required_files = list(best_by_file)

    if len(required_files) > max_passages:
        return selected_passages[:max_passages]

    covered = {
        file_name
        for passage in selected_passages
        if (file_name := _file_name_for_passage(passage))
    }
    missing = [file_name for file_name in required_files if file_name not in covered]
    if not missing:
        return selected_passages[:max_passages]

    result = list(selected_passages[:max_passages])
    file_counts = Counter(
        file_name
        for passage in result
        if (file_name := _file_name_for_passage(passage))
    )

    for file_name in missing:
        candidate = best_by_file[file_name]
        if len(result) < max_passages:
            result.append(candidate)
            file_counts[file_name] += 1
            continue

        replace_index = None
        for index in range(len(result) - 1, -1, -1):
            existing_file_name = _file_name_for_passage(result[index])
            if existing_file_name and file_counts[existing_file_name] > 1:
                replace_index = index
                file_counts[existing_file_name] -= 1
                break
        if replace_index is None:
            continue
        result[replace_index] = candidate
        file_counts[file_name] += 1

    return result[:max_passages]


MULTI_DOCUMENT_COVERAGE_KEYWORDS = (
    "리포트들",
    "보고서들",
    "목록",
    "리스트",
    "비교",
    "각각",
    "전부",
    "전체",
    "여러",
    "발간된",
    "나온 리포트",
    "자료들",
)

SINGLE_TARGET_DEEP_DIVE_KEYWORDS = (
    "전망",
    "리스크",
    "투자포인트",
    "투자 포인트",
    "핵심",
    "자세히",
    "분석",
    "왜",
    "근거",
)

DATE_BOUNDED_TARGET_REPORT_SET_KEYWORDS = (
    "리포트",
    "보고서",
    "발간",
    "해당 기간",
    "기간 내",
    "정리",
    "요약",
    "내용",
    "알려줘",
)

DEICTIC_PERIOD_MARKERS = (
    "해당 기간",
    "위 기간",
    "그 기간",
    "기간 내",
)


def _normalized_contains(text: str, keywords: tuple[str, ...]) -> bool:
    normalized_text = str(text or "").casefold().replace(" ", "")
    return any(keyword.casefold().replace(" ", "") in normalized_text for keyword in keywords)


def _filters_have_date_range(filters: dict | None) -> bool:
    filters = filters or {}
    return bool(filters.get("report_date_start") and filters.get("report_date_end"))


def _file_name_matches_filters(file_name: str, filters: dict | None) -> bool:
    filters = filters or {}
    normalized_file = str(file_name or "").casefold().replace(" ", "")
    if not normalized_file:
        return False

    report_type = filters.get("report_type")
    if report_type and not normalized_file.startswith(f"{str(report_type).casefold()}_"):
        return False

    target_name = filters.get("target_name")
    if target_name and str(target_name).casefold().replace(" ", "") not in normalized_file:
        return False

    report_date_start = filters.get("report_date_start")
    report_date_end = filters.get("report_date_end")
    if report_date_start or report_date_end:
        date_match = None
        parts = str(file_name or "").split("_")
        if len(parts) >= 2:
            date_match = parts[1]
        if not date_match:
            return False
        if report_date_start and date_match < str(report_date_start):
            return False
        if report_date_end and date_match > str(report_date_end):
            return False

    return True


def required_file_names_from_prior_scope(
    question: str,
    prior_search_scope: dict | None,
    search_filters: dict | None,
) -> list[str]:
    """현재 질문이 prior scope의 특정 파일 집합을 좁혀 봐야 하는 경우 필요한 파일명을 반환합니다."""
    if not prior_search_scope or not search_filters:
        return []
    if not search_filters.get("target_name"):
        return []
    uses_prior_period = _normalized_contains(question, DEICTIC_PERIOD_MARKERS)
    target_with_prior_dates = _filters_have_date_range(search_filters)
    if not uses_prior_period and not target_with_prior_dates:
        return []

    file_names = prior_search_scope.get("file_names") or []
    required: list[str] = []
    seen: set[str] = set()
    for file_name in file_names:
        file_name_text = str(file_name or "")
        if file_name_text in seen:
            continue
        if _file_name_matches_filters(file_name_text, search_filters):
            seen.add(file_name_text)
            required.append(file_name_text)
    return required


def _doc_identity(doc) -> tuple:
    metadata = getattr(doc, "metadata", {}) or {}
    return (
        metadata.get("parent_id"),
        metadata.get("file_name"),
        metadata.get("child_index"),
        id(doc),
    )


def _merge_docs_with_scores(
    primary: list[tuple],
    supplemental: list[tuple],
) -> list[tuple]:
    merged = list(primary)
    seen = {_doc_identity(doc) for doc, _score in merged}
    for doc, score in supplemental:
        identity = _doc_identity(doc)
        if identity in seen:
            continue
        seen.add(identity)
        merged.append((doc, score))
    return merged


def should_apply_document_coverage(
    query: str,
    search_filters: dict | None,
    required_file_names: list[str] | tuple[str, ...] | str | None = None,
    scope_decision: dict | None = None,
) -> tuple[bool, str]:
    """document coverage 적용 여부를 결정합니다."""
    if required_file_names:
        return True, "required_file_names"

    if (scope_decision or {}).get("reason") == "matched_prior_section_alias":
        return True, "section_followup_scope"

    filters = search_filters or {}
    if filters.get("file_names"):
        return True, "file_names_filter"

    if (
        filters.get("target_name")
        and _filters_have_date_range(filters)
        and _normalized_contains(query, DATE_BOUNDED_TARGET_REPORT_SET_KEYWORDS)
    ):
        return True, "date_bounded_target_report_set"

    if _normalized_contains(query, MULTI_DOCUMENT_COVERAGE_KEYWORDS):
        return True, "multi_document_intent"

    if filters.get("target_name") and _normalized_contains(query, SINGLE_TARGET_DEEP_DIVE_KEYWORDS):
        return False, "single_target_deep_dive"

    if filters.get("target_name"):
        return False, "single_target_default"

    return False, "no_coverage_intent"


def select_top_passages(
    query: str,
    docs_with_scores: list[tuple],
    *,
    search_filters: dict | None = None,
    required_file_names: list[str] | tuple[str, ...] | str | None = None,
    scope_decision: dict | None = None,
) -> tuple[list[dict], dict]:
    """최종 context passage를 선택합니다."""
    passages = _build_passages(docs_with_scores)
    candidate_count = min(
        len(passages),
        max(SEARCH_TOP_K, SEARCH_TOP_K * max(RERANK_CANDIDATE_MULTIPLIER, 1)),
    )
    if USE_RERANKER:
        ranked = get_ranker().rerank(query, passages, candidate_count)
    else:
        ranked = passages[:candidate_count]
    ranked = apply_recency_weight(ranked, RECENCY_WEIGHT)[:SEARCH_TOP_K]
    apply_coverage, coverage_reason = should_apply_document_coverage(
        query,
        search_filters,
        required_file_names,
        scope_decision,
    )
    if apply_coverage:
        selected = ensure_document_coverage(
            ranked,
            docs_with_scores,
            max_passages=SEARCH_TOP_K,
            required_file_names=required_file_names,
        )
    else:
        selected = ranked[:SEARCH_TOP_K]
    return selected, {
        "document_coverage_applied": apply_coverage,
        "document_coverage_reason": coverage_reason,
    }


def vectordb_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    search_filters = state.get("search_filters") or infer_search_filters(query)
    temporal_context = state.get("temporal_context")
    temporal_context_text = ""
    if temporal_context:
        temporal_context_text = f"\n[상대 날짜 해석]\n{temporal_context['description']}\n"
    selection_context = state.get("selection_context")
    if selection_context:
        temporal_context_text += f"\n[검색 대상 선정 근거]\n{selection_context}\n"
    scope_decision = state.get("scope_decision")
    if not os.path.exists(FAISS_DIR):
        msg = "faiss_db/ 폴더가 없어 검색을 진행할 수 없습니다. 먼저 임베딩 파이프라인을 실행해 주세요."
        logger.warning(msg)
        return {"generation": msg}

    embeddings_fn = build_embeddings_fn()
    faiss_store = FAISS.load_local(
        FAISS_DIR,
        embeddings_fn,
        allow_dangerous_deserialization=True,
    )

    fetch_k = _metadata_aware_fetch_k(faiss_store, search_filters)
    all_docs_with_scores = faiss_store.similarity_search_with_score(query, k=fetch_k)
    candidate_count_before_filter = len(all_docs_with_scores)
    docs_with_scores = filter_docs_with_scores(all_docs_with_scores, search_filters)
    prior_required_file_names = required_file_names_from_prior_scope(
        state["question"],
        state.get("prior_search_scope"),
        search_filters,
    )
    if prior_required_file_names:
        supplemental_filters = dict(search_filters)
        supplemental_filters.pop("target_name", None)
        supplemental_filters["file_names"] = prior_required_file_names
        supplemental_docs_with_scores = filter_docs_with_scores(
            all_docs_with_scores,
            supplemental_filters,
        )
        docs_with_scores = _merge_docs_with_scores(docs_with_scores, supplemental_docs_with_scores)
        matched_required_file_names = {
            str((getattr(doc, "metadata", {}) or {}).get("file_name") or "")
            for doc, _score in docs_with_scores
            if (getattr(doc, "metadata", {}) or {}).get("file_name") in prior_required_file_names
        }
        missing_required_file_names = [
            file_name
            for file_name in prior_required_file_names
            if file_name not in matched_required_file_names
        ]
    candidate_count_after_filter = len(docs_with_scores)
    retrieval_metrics = {
        "fetch_k": fetch_k,
        "candidate_count_before_filter": candidate_count_before_filter,
        "candidate_count_after_filter": candidate_count_after_filter,
        "search_top_k": SEARCH_TOP_K,
        "use_reranker": USE_RERANKER,
    }
    if prior_required_file_names:
        retrieval_metrics["prior_scope_required_file_names"] = prior_required_file_names
        retrieval_metrics["prior_scope_required_file_count"] = len(prior_required_file_names)
        retrieval_metrics["prior_scope_required_file_names_missing_after_filter"] = missing_required_file_names
        retrieval_metrics["prior_scope_required_file_count_after_filter"] = len(matched_required_file_names)
    if not docs_with_scores:
        if search_filters:
            filter_text = ", ".join(f"{key}={value}" for key, value in search_filters.items())
            msg = (
                "지정된 조건에 맞는 임베딩 완료 리포트를 찾지 못했습니다. "
                f"(적용 조건: {filter_text})"
            )
            if temporal_context:
                msg += f"\n해석한 날짜 기준: {temporal_context['description']}"
        else:
            msg = "관련 리포트를 찾지 못했습니다."
        logger.info(msg)
        return {
            "generation": msg,
            "no_vector_results": True,
            "search_filters": search_filters,
            "monitoring_metrics": {"retrieval": retrieval_metrics},
        }

    required_file_names = (
        prior_required_file_names
        or (search_filters.get("file_names") if isinstance(search_filters, dict) else None)
    )
    top_passages, coverage_metrics = select_top_passages(
        query,
        docs_with_scores,
        search_filters=search_filters,
        required_file_names=required_file_names,
        scope_decision=scope_decision,
    )
    retrieval_metrics.update(coverage_metrics)
    retrieval_metrics["selected_source_count"] = len(top_passages)
    retrieval_metrics["selected_file_names"] = sorted(
        {
            file_name
            for passage in top_passages
            if (file_name := _file_name_for_passage(passage))
        }
    )

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
                "title": meta.get("title", "-"),
                "broker": meta.get("broker", "-"),
                "report_type": meta.get("report_type", "-"),
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
            f"{temporal_context_text}"
            f"{prompt.format(context=context_text, question=query)}"
        )
    )
    ai_msg: AIMessage = llm.invoke([tool_context_message])

    if ai_msg.tool_calls:
        return {
            "rerank_info": rerank_info,
            "search_filters": search_filters,
            "no_vector_results": False,
            "messages": [tool_context_message, ai_msg],
            "monitoring_metrics": {"retrieval": retrieval_metrics},
        }

    answer = ai_msg.content
    if isinstance(answer, list):
        answer = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in answer
        )
    answer = remove_unavailable_citations(str(answer), source_count=len(rerank_info))

    return {
        "rerank_info": rerank_info,
        "generation": answer,
        "search_filters": search_filters,
        "no_vector_results": False,
        "messages": [tool_context_message, ai_msg],
        "monitoring_metrics": {"retrieval": retrieval_metrics},
    }
