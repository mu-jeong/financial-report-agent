"""Deterministic metadata filtering helpers for VectorDB retrieval.

The project goal is to answer questions about the user's local report corpus.
When a question names a company, broker, or report type, retrieval should prefer
documents with matching metadata instead of relying only on vector similarity.
These helpers keep that step deterministic and testable.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from typing import Any, Iterable

from src.configs.config import DB_PATH, get_logger
from src.core.db_manager import get_connection

logger = get_logger(__name__)

REPORT_TYPE_KEYWORDS = {
    "company": ("company", "종목", "기업", "회사", "개별주", "개별 종목"),
    "industry": ("industry", "산업", "업종", "섹터"),
    "economy": ("economy", "경제", "매크로", "금리", "환율"),
}


def _normalize_text(value: Any) -> str:
    return str(value or "").casefold().replace(" ", "")


def _pick_longest_mentioned(query: str, candidates: Iterable[str]) -> str | None:
    normalized_query = _normalize_text(query)
    mentioned = [
        candidate
        for candidate in candidates
        if candidate and _normalize_text(candidate) and _normalize_text(candidate) in normalized_query
    ]
    if not mentioned:
        return None
    return max(mentioned, key=len)


@lru_cache(maxsize=1)
def get_metadata_candidates() -> dict[str, tuple[str, ...]]:
    """Return distinct metadata values currently known by SQLite.

    Values are read from the local DB only; no network or LLM call is involved.
    The cache avoids repeated metadata scans during a chat session.
    """
    try:
        with get_connection() as conn:
            targets = tuple(
                row["target_name"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT target_name
                    FROM reports
                    WHERE target_name IS NOT NULL AND target_name != '' AND target_name != 'null'
                    ORDER BY LENGTH(target_name) DESC, target_name
                    """
                ).fetchall()
            )
            brokers = tuple(
                row["broker"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT broker
                    FROM reports
                    WHERE broker IS NOT NULL AND broker != ''
                    ORDER BY LENGTH(broker) DESC, broker
                    """
                ).fetchall()
            )
    except sqlite3.Error as exc:
        logger.warning(f"[MetadataFilter] metadata candidate scan failed: {exc}")
        return {"target_name": (), "broker": ()}

    return {"target_name": targets, "broker": brokers}


def infer_search_filters(
    query: str,
    candidates: dict[str, Iterable[str]] | None = None,
) -> dict[str, str]:
    """Infer exact metadata filters from explicit names in a query.

    This intentionally only matches values already present in local metadata.
    It avoids LLM guessing and therefore will not infer aliases that do not
    appear in the corpus.
    """
    values = candidates or get_metadata_candidates()
    filters: dict[str, str] = {}

    target = _pick_longest_mentioned(query, values.get("target_name", ()))
    if target:
        filters["target_name"] = target

    broker = _pick_longest_mentioned(query, values.get("broker", ()))
    if broker:
        filters["broker"] = broker

    normalized_query = _normalize_text(query)
    for report_type, keywords in REPORT_TYPE_KEYWORDS.items():
        if any(_normalize_text(keyword) in normalized_query for keyword in keywords):
            filters["report_type"] = report_type
            break

    return filters


def metadata_matches(metadata: dict[str, Any], filters: dict[str, str]) -> bool:
    """Return whether a document metadata dict satisfies all filters."""
    for key, expected in filters.items():
        if key == "report_type":
            actual = metadata.get("report_type")
            # Older vector metadata may not contain report_type. In that case,
            # do not reject the candidate solely on a missing optional field.
            if actual and _normalize_text(actual) != _normalize_text(expected):
                return False
            continue

        actual = metadata.get(key)
        if not actual or _normalize_text(actual) != _normalize_text(expected):
            return False

    return True


def filter_docs_with_scores(
    docs_with_scores: list[tuple[Any, float]],
    filters: dict[str, str],
) -> list[tuple[Any, float]]:
    """Filter LangChain document/score pairs by metadata."""
    if not filters:
        return docs_with_scores
    return [
        (doc, score)
        for doc, score in docs_with_scores
        if metadata_matches(getattr(doc, "metadata", {}), filters)
    ]
