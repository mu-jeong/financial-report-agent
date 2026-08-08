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


def _target_is_only_part_of_broker(query: str, target: str | None, broker: str | None) -> bool:
    """증권사명 내부의 일반 target 조각을 별도 검색 대상으로 보지 않습니다."""
    if not target or not broker:
        return False
    normalized_target = _normalize_text(target)
    normalized_broker = _normalize_text(broker)
    if not normalized_target or normalized_target not in normalized_broker:
        return False
    query_without_broker = _normalize_text(query).replace(normalized_broker, "")
    return normalized_target not in query_without_broker


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

    yearless_same_month_date_range_match = re.search(
        "(?<!\\d)(?P<month>1[0-2]|0?[1-9])\\s*(?:-|/|\\.|\\uC6D4)\\s*"
        "(?P<start_day>3[01]|[12]\\d|0?[1-9])\\s*\\uC77C?\\s*"
        "(?:~|-|\\uBD80\\uD130)\\s*"
        "(?P<end_day>3[01]|[12]\\d|0?[1-9])\\s*\\uC77C?",
        query,
    )
    if yearless_same_month_date_range_match:
        month = int(yearless_same_month_date_range_match.group("month"))
        report_month = _latest_known_month_matching(month, known_months)
        if report_month is None:
            report_month = f"{today.year:04d}-{month:02d}"
        year = int(report_month[:4])
        try:
            start = date(
                year,
                month,
                int(yearless_same_month_date_range_match.group("start_day")),
            )
            end = date(
                year,
                month,
                int(yearless_same_month_date_range_match.group("end_day")),
            )
        except ValueError:
            return None
        if start > end:
            return None
        return _temporal_context("명시 날짜 범위", start.isoformat(), end.isoformat(), today)

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


def get_metadata_candidates() -> dict[str, tuple[str, ...]]:
    """Return distinct metadata values currently known by SQLite.

    Values are read from the local DB only; no network or LLM call is involved.
    The cache key follows the active full/delta publication revision so a
    long-lived graph sees newly published report metadata.
    """
    try:
        with get_connection() as connection:
            revision = _metadata_revision_token(connection)
    except sqlite3.Error as exc:
        logger.warning(f"[MetadataFilter] metadata revision scan failed: {exc}")
        return {
            "target_name": (),
            "broker": (),
            "report_month": (),
            "target_report_types": {},
        }
    return _metadata_candidates_for_revision(revision)


@lru_cache(maxsize=8)
def _metadata_candidates_for_revision(
    _revision: tuple[Any, ...],
) -> dict[str, tuple[str, ...]]:
    """Load candidates once for one immutable runtime/delta revision."""

    try:
        with get_connection() as conn:
            rows = _active_metadata_rows(conn)
    except sqlite3.Error as exc:
        logger.warning(f"[MetadataFilter] metadata candidate scan failed: {exc}")
        return {"target_name": (), "broker": (), "report_month": (), "target_report_types": {}}

    targets = tuple(
        sorted(
            {
                str(row["target_name"])
                for row in rows
                if row["target_name"] not in {None, "", "null"}
            },
            key=lambda value: (-len(value), value),
        )
    )
    brokers = tuple(
        sorted(
            {
                str(row["broker"])
                for row in rows
                if row["broker"] not in {None, ""}
            },
            key=lambda value: (-len(value), value),
        )
    )
    report_months = tuple(
        sorted(
            {
                str(row["report_month"])
                for row in rows
                if row["report_month"] not in {None, ""}
            },
            reverse=True,
        )
    )
    report_types_by_target: dict[str, set[str]] = {}
    for row in rows:
        target = row["target_name"]
        report_type = row["report_type"]
        if target in {None, "", "null"} or report_type in {None, ""}:
            continue
        report_types_by_target.setdefault(str(target), set()).add(str(report_type))
    target_report_types = {
        target: tuple(sorted(report_types))
        for target, report_types in sorted(report_types_by_target.items())
    }

    return {
        "target_name": targets,
        "broker": brokers,
        "report_month": report_months,
        "target_report_types": target_report_types,
    }


def _metadata_revision_token(connection: sqlite3.Connection) -> tuple[Any, ...]:
    """Return a stable cache key for the currently served report universe."""

    database = next(
        (
            row
            for row in connection.execute("PRAGMA database_list")
            if str(row[1]) == "main"
        ),
        None,
    )
    database_identity = (
        str(database[2]) if database is not None and database[2] else f":memory:{id(connection)}"
    )
    runtime = connection.execute(
        """
        SELECT active_snapshot_id, active_build_id, publication_generation
        FROM retrieval_runtime WHERE runtime_id = 1
        """
    ).fetchone()
    if runtime is None:
        raise sqlite3.DatabaseError("native runtime singleton is missing")
    delta_tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'table' AND name IN (
                'retrieval_delta_segments', 'retrieval_delta_reports'
            )
            """
        )
    }
    delta_revision: tuple[Any, ...] = (0, 0, "")
    if delta_tables == {"retrieval_delta_segments", "retrieval_delta_reports"}:
        delta = connection.execute(
            """
            SELECT COALESCE(MAX(segment.sequence), 0), COUNT(*),
                   COALESCE(MAX(segment.segment_id), '')
            FROM retrieval_delta_segments AS segment
            WHERE segment.base_snapshot_id = ?
              AND segment.base_publication_generation = ?
              AND segment.state = 'ready'
            """,
            (runtime[0], int(runtime[2])),
        ).fetchone()
        delta_revision = tuple(delta)
    return (
        database_identity,
        "native",
        runtime[0],
        runtime[1],
        int(runtime[2]),
        *delta_revision,
    )


def _active_metadata_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Read active report metadata without expanding snapshot chunk membership."""

    delta_tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'table' AND name IN (
                'retrieval_delta_segments', 'retrieval_delta_reports'
            )
            """
        )
    }
    if delta_tables == {"retrieval_delta_segments", "retrieval_delta_reports"}:
        return connection.execute(
            """
            WITH base_metadata AS (
                SELECT report.canonical_relative_path, report.target_name,
                       report.broker, substr(report.report_date, 1, 7) AS report_month,
                       report.report_type
                FROM retrieval_runtime AS runtime
                JOIN retrieval_builds AS build
                  ON build.build_id = runtime.active_build_id
                 AND build.state = 'fully_complete'
                JOIN json_each(build.source_manifest_json, '$.reports') AS decision
                JOIN main.reports AS report
                  ON report.report_uid = json_extract(decision.value, '$.report_uid')
                WHERE runtime.runtime_id = 1
                  AND json_extract(decision.value, '$.status') = 'included'
            ),
            ready_segments AS (
                SELECT segment.segment_id, segment.sequence
                FROM retrieval_delta_segments AS segment
                JOIN retrieval_runtime AS runtime
                  ON runtime.runtime_id = 1
                 AND runtime.active_snapshot_id = segment.base_snapshot_id
                 AND runtime.publication_generation =
                     segment.base_publication_generation
                WHERE segment.state = 'ready'
            ),
            ranked_heads AS (
                SELECT action.canonical_relative_path, action.action,
                       action.report_uid,
                       row_number() OVER (
                           PARTITION BY action.canonical_relative_path
                           ORDER BY segment.sequence DESC, segment.segment_id DESC
                       ) AS position
                FROM retrieval_delta_reports AS action
                JOIN ready_segments AS segment
                  ON segment.segment_id = action.segment_id
                WHERE action.action IN ('upsert', 'delete')
            ),
            heads AS (
                SELECT canonical_relative_path, action, report_uid
                FROM ranked_heads WHERE position = 1
            )
            SELECT base.target_name, base.broker, base.report_month,
                   base.report_type
            FROM base_metadata AS base
            LEFT JOIN heads AS head
              ON head.canonical_relative_path = base.canonical_relative_path
            WHERE head.canonical_relative_path IS NULL
            UNION ALL
            SELECT report.target_name, report.broker,
                   substr(report.report_date, 1, 7) AS report_month,
                   report.report_type
            FROM heads AS head
            JOIN main.reports AS report ON report.report_uid = head.report_uid
            WHERE head.action = 'upsert'
            """
        ).fetchall()

    return connection.execute(
        """
        SELECT report.target_name, report.broker,
               substr(report.report_date, 1, 7) AS report_month,
               report.report_type
        FROM retrieval_runtime AS runtime
        JOIN retrieval_builds AS build
          ON build.build_id = runtime.active_build_id
         AND build.state = 'fully_complete'
        JOIN json_each(build.source_manifest_json, '$.reports') AS decision
        JOIN main.reports AS report
          ON report.report_uid = json_extract(decision.value, '$.report_uid')
        WHERE runtime.runtime_id = 1
          AND json_extract(decision.value, '$.status') = 'included'
        """
    ).fetchall()


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

    if _target_is_only_part_of_broker(query, target, broker):
        filters.pop("target_name", None)
        target = None

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
