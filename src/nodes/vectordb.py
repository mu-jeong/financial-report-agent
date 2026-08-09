import sqlite3
from collections import Counter
from datetime import datetime

import numpy as np
from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import PromptTemplate
from src.configs.config import (
    DATA_ROOT,
    RECENCY_WEIGHT,
    SEARCH_CANDIDATE_MULTIPLIER,
    SEARCH_TOP_K,
    USE_RERANKER,
    get_logger,
)
from src.configs.prompts import VECTORDB_PROMPT
from src.core.db_manager import get_connection
from src.core.metadata_filters import filter_docs_with_scores, infer_search_filters
from src.graphs.state import State
from src.nodes.stock_price import stock_price_tools
from src.llms.embeddings import build_embeddings_model
from src.llms.factory import build_chat_model
from src.retrieval.bootstrap import RetrievalBootstrapError
from src.retrieval.dispatch import (
    RetrievalDispatchStateError,
    resolve_retrieval_dispatch,
)
from src.retrieval.repository import (
    RepositoryError,
    RetrievedChunk,
)
from src.utils.citations import remove_unavailable_citations
from src.utils.ranker import get_ranker

logger = get_logger(__name__)


class RetrievalDispatchError(RuntimeError):
    """Raised when the selected retrieval backend cannot safely serve a request."""


def build_embeddings_fn():
    return build_embeddings_model()


def _requested_candidate_count() -> int:
    return SEARCH_TOP_K * max(SEARCH_CANDIDATE_MULTIPLIER, 1)


def _build_passages(docs_with_scores: list[tuple]) -> list[dict]:
    passages = []
    seen_parent_ids = set()
    for rank, (doc, score) in enumerate(docs_with_scores):
        meta = doc.metadata
        page_content = doc.page_content
        parent_id = meta.get("parent_uid")

        if parent_id:
            if parent_id in seen_parent_ids:
                continue
            seen_parent_ids.add(parent_id)

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


def _report_universe_query(filters: dict | None) -> tuple[str, list]:
    filters = filters or {}
    clauses = ["is_embedded = 1"]
    params: list = []
    report_types = filters.get("report_types")
    if report_types is not None:
        normalized_types = [str(report_type) for report_type in report_types]
        if normalized_types:
            placeholders = ", ".join("?" for _ in normalized_types)
            clauses.append(f"report_type IN ({placeholders})")
            params.extend(normalized_types)
        else:
            clauses.append("1 = 0")
    elif filters.get("report_type"):
        clauses.append("report_type = ?")
        params.append(filters["report_type"])
    for column in ("target_name", "broker"):
        if filters.get(column):
            clauses.append(f"{column} = ?")
            params.append(filters[column])
    if filters.get("report_date_start"):
        clauses.append("report_date >= ?")
        params.append(filters["report_date_start"])
    if filters.get("report_date_end"):
        clauses.append("report_date <= ?")
        params.append(filters["report_date_end"])
    where_sql = " AND ".join(clauses)
    return (
        "SELECT report_date, report_type, target_name, broker, title, file_name, is_embedded "
        f"FROM reports WHERE {where_sql} ORDER BY report_date ASC, broker ASC, title ASC, file_name ASC",
        params,
    )


def fetch_report_universe_for_filters(filters: dict | None) -> list[dict]:
    """Return embedded report metadata rows used to plan broad temporal summaries."""
    query, params = _report_universe_query(filters)
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def _month_bucket(report_date: object) -> str:
    value = str(report_date or "")
    return value[:7] if len(value) >= 7 else "unknown"


def build_temporal_preflight_plan(rows: list[dict], *, max_files: int = SEARCH_TOP_K) -> dict:
    """Select file coverage for date-bounded target summaries after RDB preflight."""
    ordered_rows = sorted(
        [row for row in rows if row.get("file_name")],
        key=lambda row: (
            str(row.get("report_date") or ""),
            str(row.get("broker") or ""),
            str(row.get("title") or ""),
            str(row.get("file_name") or ""),
        ),
    )
    all_files = list(dict.fromkeys(str(row["file_name"]) for row in ordered_rows))
    buckets: dict[str, list[dict]] = {}
    for row in ordered_rows:
        buckets.setdefault(_month_bucket(row.get("report_date")), []).append(row)

    if len(all_files) <= max_files:
        selected_files = all_files
        selection_reason = "all_files_within_search_top_k"
    else:
        selected_files = []
        seen: set[str] = set()
        for bucket in sorted(buckets):
            for row in buckets[bucket]:
                file_name = str(row["file_name"])
                if file_name in seen:
                    continue
                selected_files.append(file_name)
                seen.add(file_name)
                break
            if len(selected_files) >= max_files:
                break
        selection_reason = "month_bucket_representatives"

    return {
        "file_names": selected_files,
        "rows": ordered_rows,
        "metrics": {
            "preflight_file_count": len(all_files),
            "preflight_row_count": len(ordered_rows),
            "selected_file_count": len(selected_files),
            "bucket_by": "month",
            "bucket_count": len(buckets),
            "selection_reason": selection_reason,
        },
    }


def _format_temporal_preflight_context(plan: dict) -> str:
    rows = plan.get("rows") or []
    if not rows:
        return ""
    lines = ["\n[검색 대상 리포트 목록 - RDB preflight]"]
    for row in rows:
        lines.append(
            "- "
            f"{row.get('report_date', '-')} | {row.get('broker', '-')} | "
            f"{row.get('title', '-')} | {row.get('file_name', '-')}"
        )
    lines.append(
        "위 목록은 기간/종목 조건에 맞는 임베딩 완료 리포트 universe입니다. "
        "답변에서는 이 전체 범위를 먼저 밝히고, 제공된 본문 문맥은 시기별 대표/선택 문서 기준임을 명시하세요."
    )
    return "\n".join(lines) + "\n"


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

    report_types = filters.get("report_types")
    if report_types is not None:
        prefixes = tuple(
            f"{str(report_type).casefold()}_" for report_type in report_types
        )
        if not prefixes or not normalized_file.startswith(prefixes):
            return False
    else:
        report_type = filters.get("report_type")
        if report_type and not normalized_file.startswith(
            f"{str(report_type).casefold()}_"
        ):
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
        metadata.get("parent_uid"),
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
    candidate_count = min(len(passages), _requested_candidate_count())
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


def _native_document(chunk: RetrievedChunk) -> Document:
    metadata = dict(chunk.metadata)
    metadata.update(
        {
            "child_index": chunk.child_order,
            "span_start": chunk.span_start,
            "span_end": chunk.span_end,
            "physical_id": chunk.physical_id,
            "snapshot_id": chunk.snapshot_id,
            "publication_generation": chunk.publication_generation,
        }
    )
    return Document(page_content=chunk.parent_slice, metadata=metadata)


def _retrieve_docs_with_scores(
    query: str,
    search_filters: dict | None,
) -> tuple[list[tuple[Document, float]], dict]:
    """Dispatch one query to the runtime selected by the canonical bootstrap."""

    try:
        dispatch = resolve_retrieval_dispatch(DATA_ROOT)
    except RetrievalBootstrapError:
        raise
    except (
        OSError,
        sqlite3.Error,
        RepositoryError,
        RetrievalDispatchStateError,
        ValueError,
    ) as exc:
        raise RetrievalDispatchError("retrieval bootstrap inspection failed") from exc
    requested_k = _requested_candidate_count()
    if dispatch.native is None:
        selection = dispatch.selection
        if (
            dispatch.mode == "native"
            and selection is not None
            and selection.is_empty
        ):
            return [], {
                "runtime_mode": dispatch.mode,
                "requested_k": requested_k,
                "fetch_k": 0,
                "native_candidate_count": 0,
                "native_eligible_count": 0,
                "native_snapshot_total": 0,
                "native_search_strategy": "empty",
                "native_faiss_calls": 0,
                "native_hydration_batches": 0,
                "native_hydration_rows": 0,
                "native_hydration_cache_hits": 0,
                "native_hydration_cache_misses": 0,
                "snapshot_id": selection.active_snapshot_id,
                "publication_generation": selection.publication_generation,
            }
        mode = getattr(dispatch, "mode", None) or getattr(selection, "mode", None)
        raise RetrievalDispatchError(
            f"unsupported retrieval runtime mode: {mode or 'unknown'}; Native V2 is required"
        )
    try:
        embeddings_fn = build_embeddings_fn()
        query_vector = embeddings_fn.embed_query(query)
    except Exception as exc:
        raise RetrievalDispatchError("query embedding failed") from exc

    try:
        response = dispatch.native.reader.search(
            np.asarray(query_vector, dtype=np.float32),
            requested_k,
            scope=search_filters or None,
        )
    except (RepositoryError, OSError, sqlite3.Error, ValueError) as exc:
        raise RetrievalDispatchError("native retrieval request failed") from exc
    docs_with_scores = [
        (_native_document(chunk), float(chunk.score)) for chunk in response.results
    ]
    metrics = {
        "runtime_mode": dispatch.mode,
        "requested_k": requested_k,
        "fetch_k": response.faiss_fetch_k,
        "native_candidate_count": response.candidate_count,
        "native_eligible_count": response.eligible_count,
        "native_snapshot_total": response.snapshot_total,
        "native_search_strategy": response.strategy.value,
        "native_faiss_calls": response.faiss_calls,
        "native_hydration_batches": response.hydration_batches,
        "native_hydration_rows": response.hydration_rows,
        "native_hydration_cache_hits": response.hydration_cache_hits,
        "native_hydration_cache_misses": response.hydration_cache_misses,
        "snapshot_id": response.revision.snapshot_id,
        "publication_generation": response.revision.publication_generation,
    }
    timings = getattr(response, "timings", None)
    if timings is not None:
        metrics.update(
            {
                "native_scope_compile_ns": timings.scope_compile_ns,
                "native_eligibility_ns": timings.eligibility_ns,
                "native_faiss_ns": timings.faiss_ns,
                "native_hydration_ns": timings.hydration_ns,
                "native_lease_ns": timings.lease_ns,
                "native_total_ns": timings.total_ns,
            }
        )
    return docs_with_scores, metrics


def vectordb_node(state: State) -> dict:
    query = state.get("rewritten_query", state["question"])
    search_filters = state.get("search_filters") or infer_search_filters(query)
    retrieval_plan = state.get("retrieval_plan") or {}
    temporal_preflight_plan = None
    if retrieval_plan.get("type") == "temporal_report_set_summary":
        preflight_rows = fetch_report_universe_for_filters(search_filters)
        temporal_preflight_plan = build_temporal_preflight_plan(
            preflight_rows,
            max_files=SEARCH_TOP_K,
        )
        if temporal_preflight_plan.get("file_names"):
            search_filters = dict(search_filters)
            search_filters["file_names"] = temporal_preflight_plan["file_names"]
    temporal_context = state.get("temporal_context")
    temporal_context_text = ""
    if temporal_context:
        temporal_context_text = f"\n[상대 날짜 해석]\n{temporal_context['description']}\n"
    if temporal_preflight_plan:
        temporal_context_text += _format_temporal_preflight_context(temporal_preflight_plan)
    selection_context = state.get("selection_context")
    if selection_context:
        temporal_context_text += f"\n[검색 대상 선정 근거]\n{selection_context}\n"
    scope_decision = state.get("scope_decision")
    prior_required_file_names = required_file_names_from_prior_scope(
        state["question"],
        state.get("prior_search_scope"),
        search_filters,
    )
    retrieval_scope = search_filters
    if prior_required_file_names:
        retrieval_scope = dict(search_filters)
        retrieval_scope["file_names"] = prior_required_file_names
    try:
        all_docs_with_scores, backend_metrics = _retrieve_docs_with_scores(
            query,
            retrieval_scope,
        )
    except (RetrievalBootstrapError, RetrievalDispatchError) as exc:
        logger.error("Retrieval bootstrap/search failed closed: %s", exc)
        return {
            "generation": "검색 저장소를 안전하게 열 수 없어 결과를 제공할 수 없습니다.",
            "no_vector_results": True,
            "search_filters": search_filters,
            "monitoring_metrics": {
                "retrieval": {
                    "runtime_mode": "unavailable",
                    "error_code": type(exc).__name__,
                    "candidate_count_before_filter": 0,
                    "candidate_count_after_filter": 0,
                    "search_top_k": SEARCH_TOP_K,
                    "use_reranker": USE_RERANKER,
                }
            },
        }

    candidate_count_before_filter = len(all_docs_with_scores)
    docs_with_scores = filter_docs_with_scores(all_docs_with_scores, search_filters)
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
        **backend_metrics,
        "candidate_count_before_filter": candidate_count_before_filter,
        "candidate_count_after_filter": candidate_count_after_filter,
        "search_top_k": SEARCH_TOP_K,
        "use_reranker": USE_RERANKER,
    }
    if retrieval_plan:
        retrieval_metrics["retrieval_plan"] = retrieval_plan
    if temporal_preflight_plan:
        retrieval_metrics["temporal_preflight"] = temporal_preflight_plan["metrics"]
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
                "report_uid": meta.get("report_uid"),
                "chunk_uid": meta.get("chunk_uid"),
                "parent_uid": meta.get("parent_uid"),
                "profile_id": meta.get("profile_id"),
                "child_index": meta.get("child_index"),
                "span_start": meta.get("span_start"),
                "span_end": meta.get("span_end"),
                "physical_id": meta.get("physical_id"),
                "snapshot_id": meta.get("snapshot_id"),
                "publication_generation": meta.get("publication_generation"),
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
