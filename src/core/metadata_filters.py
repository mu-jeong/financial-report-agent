"""Deterministic metadata filtering helpers for VectorDB retrieval.

The project goal is to answer questions about the user's local report corpus.
When a question names a company, broker, or report type, retrieval should prefer
documents with matching metadata instead of relying only on vector similarity.
These helpers keep that step deterministic and testable.
"""

from __future__ import annotations

import sqlite3
import re
from datetime import date
from functools import lru_cache
from typing import Any, Iterable

from src.configs.config import get_logger
from src.core.db_manager import get_connection

logger = get_logger(__name__)

SearchFilters = dict[str, str]

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


def _month_bounds(report_month: str) -> tuple[str, str]:
    """Return inclusive date bounds for a YYYY-MM month string."""
    year, month = map(int, report_month.split("-"))
    return _bounds_for_month(year, month)


def _bounds_for_month(year: int, month: int) -> tuple[str, str]:
    """Return inclusive date bounds for a year/month pair."""
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    end = next_month.toordinal() - 1
    return f"{year:04d}-{month:02d}-01", date.fromordinal(end).isoformat()


def _bounds_for_year(year: int) -> tuple[str, str]:
    """Return inclusive date bounds for a calendar year."""
    return f"{year:04d}-01-01", f"{year:04d}-12-31"


def _bounds_for_quarter(year: int, quarter: int) -> tuple[str, str]:
    """Return inclusive date bounds for a calendar quarter."""
    start_month = (quarter - 1) * 3 + 1
    _, end = _bounds_for_month(year, start_month + 2)
    return f"{year:04d}-{start_month:02d}-01", end


def _latest_known_month_matching(month: int, known_months: Iterable[str]) -> str | None:
    """Pick the latest YYYY-MM in metadata for a month number."""
    suffix = f"-{month:02d}"
    matching = sorted(
        (value for value in known_months if isinstance(value, str) and value.endswith(suffix)),
        reverse=True,
    )
    return matching[0] if matching else None


def _range_filters(start: str, end: str) -> SearchFilters:
    return {"report_date_start": start, "report_date_end": end}


def _infer_date_filters(query: str, known_months: Iterable[str]) -> SearchFilters:
    """Infer report_date bounds from explicit temporal expressions.

    The returned filters are always normalized to an inclusive
    ``report_date_start`` / ``report_date_end`` range, regardless of whether the
    query used a full date, a year-month, a year, a quarter, or a month without a
    year. If a month has no year, the latest matching year in local metadata is
    used as the anchor.
    """
    exact_match = re.search(
        "(?P<year>20\\d{2})\\s*(?:-|/|\\.|\\uB144)\\s*"
        "(?P<month>1[0-2]|0?[1-9])\\s*(?:-|/|\\.|\\uC6D4)\\s*"
        "(?P<day>3[01]|[12]\\d|0?[1-9])\\s*\\uC77C?",
        query,
    )
    if exact_match:
        report_date = (
            f"{int(exact_match.group('year')):04d}-"
            f"{int(exact_match.group('month')):02d}-"
            f"{int(exact_match.group('day')):02d}"
        )
        return _range_filters(report_date, report_date)

    quarter_match = re.search(
        "(?P<year>20\\d{2})\\s*(?:\\uB144)?\\s*(?:Q|q|"
        "\\uBD84\\uAE30)\\s*(?P<quarter>[1-4])|"
        "(?P<year_alt>20\\d{2})\\s*(?:\\uB144)?\\s*(?P<quarter_alt>[1-4])\\s*\\uBD84\\uAE30",
        query,
    )
    if quarter_match:
        year = int(quarter_match.group("year") or quarter_match.group("year_alt"))
        quarter = int(quarter_match.group("quarter") or quarter_match.group("quarter_alt"))
        start, end = _bounds_for_quarter(year, quarter)
        return _range_filters(start, end)

    year_month_match = re.search(
        "(?P<year>20\\d{2})\\s*(?:-|/|\\.|\\uB144)\\s*"
        "(?P<month>1[0-2]|0?[1-9])\\s*\\uC6D4?",
        query,
    )
    if year_month_match:
        start, end = _bounds_for_month(
            int(year_month_match.group("year")),
            int(year_month_match.group("month")),
        )
        return _range_filters(start, end)

    year_match = re.search("(?<!\\d)(?P<year>20\\d{2})\\s*\\uB144(?!\\s*\\d)", query)
    if year_match:
        start, end = _bounds_for_year(int(year_match.group("year")))
        return _range_filters(start, end)

    month_match = re.search("(?<!\\d)(?P<month>1[0-2]|0?[1-9])\\s*\\uC6D4", query)
    if month_match:
        month = int(month_match.group("month"))
        report_month = _latest_known_month_matching(month, known_months)
        if report_month is None:
            report_month = f"{date.today().year:04d}-{month:02d}"
        start, end = _month_bounds(report_month)
        return _range_filters(start, end)

    return {}


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
            report_months = tuple(
                row["report_month"]
                for row in conn.execute(
                    """
                    SELECT DISTINCT substr(report_date, 1, 7) AS report_month
                    FROM reports
                    WHERE report_date IS NOT NULL AND report_date != ''
                    ORDER BY report_month DESC
                    """
                ).fetchall()
            )
    except sqlite3.Error as exc:
        logger.warning(f"[MetadataFilter] metadata candidate scan failed: {exc}")
        return {"target_name": (), "broker": (), "report_month": ()}

    return {"target_name": targets, "broker": brokers, "report_month": report_months}


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
    filters: SearchFilters = {}

    filters.update(_infer_date_filters(query, values.get("report_month", ())))

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


def metadata_matches(metadata: dict[str, Any], filters: SearchFilters) -> bool:
    """Return whether a document metadata dict satisfies all filters."""
    for key, expected in filters.items():
        if key in {"report_date_start", "report_date_end"}:
            actual = metadata.get("report_date")
            if not actual:
                return False
            actual_text = str(actual)
            if key == "report_date_start" and actual_text < expected:
                return False
            if key == "report_date_end" and actual_text > expected:
                return False
            continue

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
    filters: SearchFilters,
) -> list[tuple[Any, float]]:
    """Filter LangChain document/score pairs by metadata."""
    if not filters:
        return docs_with_scores
    return [
        (doc, score)
        for doc, score in docs_with_scores
        if metadata_matches(getattr(doc, "metadata", {}), filters)
    ]
