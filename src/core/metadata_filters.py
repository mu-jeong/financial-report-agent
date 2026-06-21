"""Deterministic metadata filtering helpers for VectorDB retrieval.

The project goal is to answer questions about the user's local report corpus.
When a question names a company, broker, or report type, retrieval should prefer
documents with matching metadata instead of relying only on vector similarity.
These helpers keep that step deterministic and testable.
"""

from __future__ import annotations

import sqlite3
import re
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Iterable

from src.configs.config import get_logger
from src.core.db_manager import get_connection

logger = get_logger(__name__)

SearchFilters = dict[str, str]
TemporalContext = dict[str, str]

REPORT_TYPE_KEYWORDS = {
    "company": ("company", "종목", "기업", "회사", "개별주", "개별 종목"),
    "industry": ("industry", "산업", "업종", "섹터"),
    "economy": ("economy", "경제", "매크로", "금리", "환율"),
}

NOSPACE_REPORT_TYPE_KEYWORDS = {
    report_type: tuple(keyword for keyword in keywords if " " in keyword)
    for report_type, keywords in REPORT_TYPE_KEYWORDS.items()
}

PLAIN_REPORT_TYPE_KEYWORDS = {
    report_type: tuple(keyword for keyword in keywords if " " not in keyword)
    for report_type, keywords in REPORT_TYPE_KEYWORDS.items()
}


def _normalize_text(value: Any) -> str:
    return str(value or "").casefold().replace(" ", "")


def _contains_report_type_keyword(query: str, keyword: str, *, allow_nospace: bool) -> bool:
    query_text = str(query or "").casefold()
    keyword_text = str(keyword or "").casefold()
    if allow_nospace:
        return keyword_text.replace(" ", "") in query_text.replace(" ", "")
    return keyword_text in query_text


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


def _temporal_context(expression: str, start: str, end: str, today: date) -> TemporalContext:
    if start == end:
        range_text = start
    else:
        range_text = f"{start}~{end}"
    return {
        "expression": expression,
        "report_date_start": start,
        "report_date_end": end,
        "current_date": today.isoformat(),
        "description": f"{expression}={range_text} (오늘 {today.isoformat()} 기준)",
    }


def _bounds_for_current_week(today: date) -> tuple[str, str]:
    """Return Monday-through-today bounds for Korean "이번주/금주" queries."""
    start = today - timedelta(days=today.weekday())
    return start.isoformat(), today.isoformat()


def _bounds_for_week_offset(today: date, week_offset: int) -> tuple[str, str]:
    start = today - timedelta(days=today.weekday()) + timedelta(days=7 * week_offset)
    end = start + timedelta(days=6)
    return start.isoformat(), end.isoformat()


def _bounds_for_month_offset(today: date, month_offset: int) -> tuple[str, str]:
    month_index = (today.year * 12 + today.month - 1) + month_offset
    year = month_index // 12
    month = (month_index % 12) + 1
    start, end = _bounds_for_month(year, month)
    if month_offset == 0:
        end = today.isoformat()
    return start, end


def _resolve_relative_temporal_context(query: str, today: date) -> TemporalContext | None:
    normalized_query = _normalize_text(query)

    relative_days = (
        ("그제", -2),
        ("어제", -1),
        ("오늘", 0),
        ("내일", 1),
        ("모레", 2),
    )
    for expression, offset in relative_days:
        if expression in normalized_query:
            target = today + timedelta(days=offset)
            return _temporal_context(expression, target.isoformat(), target.isoformat(), today)

    relative_weeks = (
        ("지난주", -1),
        ("전주", -1),
        ("이번주", 0),
        ("금주", 0),
        ("다음주", 1),
        ("차주", 1),
    )
    for expression, offset in relative_weeks:
        if expression in normalized_query:
            if offset == 0:
                start, end = _bounds_for_current_week(today)
            else:
                start, end = _bounds_for_week_offset(today, offset)
            return _temporal_context(expression, start, end, today)

    relative_months = (
        ("지난달", -1),
        ("전월", -1),
        ("이번달", 0),
        ("이번월", 0),
        ("금월", 0),
        ("다음달", 1),
        ("익월", 1),
    )
    for expression, offset in relative_months:
        if expression in normalized_query:
            start, end = _bounds_for_month_offset(today, offset)
            return _temporal_context(expression, start, end, today)

    return None


def resolve_temporal_context(
    query: str,
    known_months: Iterable[str] = (),
    *,
    current_date: date | None = None,
) -> TemporalContext | None:
    """Resolve explicit or relative temporal expressions to concrete date bounds."""
    today = current_date or date.today()

    full_date_range_match = re.search(
        "(?P<start_year>20\\d{2})\\s*(?:-|/|\\.|\\uB144)\\s*"
        "(?P<start_month>1[0-2]|0?[1-9])\\s*(?:-|/|\\.|\\uC6D4)\\s*"
        "(?P<start_day>3[01]|[12]\\d|0?[1-9])\\s*\\uC77C?\\s*"
        "(?:~|-|\\uBD80\\uD130)\\s*"
        "(?P<end_year>20\\d{2})\\s*(?:-|/|\\.|\\uB144)\\s*"
        "(?P<end_month>1[0-2]|0?[1-9])\\s*(?:-|/|\\.|\\uC6D4)\\s*"
        "(?P<end_day>3[01]|[12]\\d|0?[1-9])\\s*\\uC77C?",
        query,
    )
    if full_date_range_match:
        start = (
            f"{int(full_date_range_match.group('start_year')):04d}-"
            f"{int(full_date_range_match.group('start_month')):02d}-"
            f"{int(full_date_range_match.group('start_day')):02d}"
        )
        end = (
            f"{int(full_date_range_match.group('end_year')):04d}-"
            f"{int(full_date_range_match.group('end_month')):02d}-"
            f"{int(full_date_range_match.group('end_day')):02d}"
        )
        return _temporal_context("명시 날짜 범위", start, end, today)

    same_month_date_range_match = re.search(
        "(?P<year>20\\d{2})\\s*(?:-|/|\\.|\\uB144)\\s*"
        "(?P<month>1[0-2]|0?[1-9])\\s*(?:-|/|\\.|\\uC6D4)\\s*"
        "(?P<start_day>3[01]|[12]\\d|0?[1-9])\\s*\\uC77C?\\s*"
        "(?:~|-|\\uBD80\\uD130)\\s*"
        "(?P<end_day>3[01]|[12]\\d|0?[1-9])\\s*\\uC77C?",
        query,
    )
    if same_month_date_range_match:
        year = int(same_month_date_range_match.group("year"))
        month = int(same_month_date_range_match.group("month"))
        start = f"{year:04d}-{month:02d}-{int(same_month_date_range_match.group('start_day')):02d}"
        end = f"{year:04d}-{month:02d}-{int(same_month_date_range_match.group('end_day')):02d}"
        return _temporal_context("명시 날짜 범위", start, end, today)

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
        return _temporal_context("명시 날짜", report_date, report_date, today)

    month_day_match = re.search(
        "(?<!\\d)(?P<month>1[0-2]|0?[1-9])\\s*(?:-|/|\\.|\\uC6D4)\\s*"
        "(?P<day>3[01]|[12]\\d|0?[1-9])\\s*(?:\\uC77C)?",
        query,
    )
    if month_day_match:
        month = int(month_day_match.group("month"))
        report_month = _latest_known_month_matching(month, known_months)
        if report_month is None:
            report_month = f"{today.year:04d}-{month:02d}"
        report_date = f"{int(report_month[:4]):04d}-{month:02d}-{int(month_day_match.group('day')):02d}"
        return _temporal_context("명시 날짜", report_date, report_date, today)

    relative_context = _resolve_relative_temporal_context(query, today)
    if relative_context:
        return relative_context

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
        return _temporal_context(f"{year}년 {quarter}분기", start, end, today)

    year_month_match = re.search(
        "(?P<year>20\\d{2})\\s*(?:-|/|\\.|\\uB144)\\s*"
        "(?P<month>1[0-2]|0?[1-9])\\s*\\uC6D4?",
        query,
    )
    if year_month_match:
        year = int(year_month_match.group("year"))
        month = int(year_month_match.group("month"))
        start, end = _bounds_for_month(year, month)
        return _temporal_context(f"{year}년 {month}월", start, end, today)

    year_match = re.search("(?<!\\d)(?P<year>20\\d{2})\\s*\\uB144(?!\\s*\\d)", query)
    if year_match:
        year = int(year_match.group("year"))
        start, end = _bounds_for_year(year)
        return _temporal_context(f"{year}년", start, end, today)

    month_match = re.search("(?<!\\d)(?P<month>1[0-2]|0?[1-9])\\s*\\uC6D4", query)
    if month_match:
        month = int(month_match.group("month"))
        report_month = _latest_known_month_matching(month, known_months)
        if report_month is None:
            report_month = f"{today.year:04d}-{month:02d}"
        start, end = _month_bounds(report_month)
        return _temporal_context(f"{int(report_month[:4])}년 {month}월", start, end, today)

    return None


def _infer_date_filters(
    query: str,
    known_months: Iterable[str],
    *,
    current_date: date | None = None,
) -> SearchFilters:
    """Infer report_date bounds from explicit temporal expressions.

    The returned filters are always normalized to an inclusive
    ``report_date_start`` / ``report_date_end`` range, regardless of whether the
    query used a full date, a year-month, a year, a quarter, or a month without a
    year. If a month has no year, the latest matching year in local metadata is
    used as the anchor.
    """
    context = resolve_temporal_context(
        query,
        known_months,
        current_date=current_date,
    )
    if not context:
        return {}
    return _range_filters(context["report_date_start"], context["report_date_end"])


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
            target_report_types: dict[str, tuple[str, ...]] = {}
            for row in conn.execute(
                """
                SELECT target_name, report_type
                FROM reports
                WHERE target_name IS NOT NULL
                  AND target_name != ''
                  AND target_name != 'null'
                  AND report_type IS NOT NULL
                  AND report_type != ''
                GROUP BY target_name, report_type
                ORDER BY target_name, report_type
                """
            ).fetchall():
                target_report_types.setdefault(row["target_name"], tuple())
                target_report_types[row["target_name"]] = (
                    *target_report_types[row["target_name"]],
                    row["report_type"],
                )
    except sqlite3.Error as exc:
        logger.warning(f"[MetadataFilter] metadata candidate scan failed: {exc}")
        return {"target_name": (), "broker": (), "report_month": (), "target_report_types": {}}

    return {
        "target_name": targets,
        "broker": brokers,
        "report_month": report_months,
        "target_report_types": target_report_types,
    }


def infer_search_filters(
    query: str,
    candidates: dict[str, Iterable[str]] | None = None,
    *,
    current_date: date | None = None,
) -> dict[str, str]:
    """Infer exact metadata filters from explicit names in a query.

    This intentionally only matches values already present in local metadata.
    It avoids LLM guessing and therefore will not infer aliases that do not
    appear in the corpus.
    """
    values = candidates or get_metadata_candidates()
    filters: SearchFilters = {}

    filters.update(
        _infer_date_filters(
            query,
            values.get("report_month", ()),
            current_date=current_date,
        )
    )

    target = _pick_longest_mentioned(query, values.get("target_name", ()))
    if target:
        filters["target_name"] = target

    broker = _pick_longest_mentioned(query, values.get("broker", ()))
    if broker:
        filters["broker"] = broker

    for report_type, keywords in PLAIN_REPORT_TYPE_KEYWORDS.items():
        if any(_contains_report_type_keyword(query, keyword, allow_nospace=False) for keyword in keywords):
            filters["report_type"] = report_type
            break
    else:
        for report_type, keywords in NOSPACE_REPORT_TYPE_KEYWORDS.items():
            if any(_contains_report_type_keyword(query, keyword, allow_nospace=True) for keyword in keywords):
                filters["report_type"] = report_type
                break

    if target and "report_type" not in filters:
        target_report_types = values.get("target_report_types")
        if isinstance(target_report_types, dict):
            report_types = tuple(target_report_types.get(target, ()))
            if len(report_types) == 1:
                filters["report_type"] = report_types[0]

    return filters


def metadata_matches(metadata: dict[str, Any], filters: SearchFilters) -> bool:
    """Return whether a document metadata dict satisfies all filters."""
    for key, expected in filters.items():
        if key == "file_names":
            expected_names = {str(name) for name in (expected or [])}
            if metadata.get("file_name") not in expected_names:
                return False
            continue

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
