from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.configs.config import DB_PATH
from src.core.db_manager import get_connection
from src.configs.settings import BASE_DIR, get_config_value

KRX_LISTED_COMPANY_SOURCE_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage"
DEFAULT_COMPANY_INDUSTRY_DATA_PATH = BASE_DIR / "data" / "listed_company_industries.csv"
REQUIRED_COLUMNS = ("company_name", "industry", "main_products")


def default_company_industry_data_path() -> Path:
    configured = get_config_value("COMPANY_INDUSTRY_DATA_PATH")
    if configured:
        return Path(str(configured))
    return DEFAULT_COMPANY_INDUSTRY_DATA_PATH


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


@lru_cache(maxsize=4)
def load_company_industry_rows(data_path: str | Path | None = None) -> tuple[dict[str, str], ...]:
    path = Path(data_path) if data_path else default_company_industry_data_path()
    if not path.exists():
        return tuple()

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = tuple(reader.fieldnames or ())
        missing = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
        if missing:
            raise ValueError(f"Unexpected company industry CSV columns: {fieldnames}")
        rows = [
            {
                "company_name": (row.get("company_name") or "").strip(),
                "industry": (row.get("industry") or "").strip(),
                "main_products": (row.get("main_products") or "").strip(),
            }
            for row in reader
            if (row.get("company_name") or "").strip()
        ]
    return tuple(rows)


def lookup_companies_by_industry(
    term: str,
    *,
    data_path: str | Path | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    normalized_term = _normalize_text(term)
    rows = []
    if normalized_term:
        for row in load_company_industry_rows(str(data_path) if data_path else None):
            industry = _normalize_text(row["industry"])
            products = _normalize_text(row["main_products"])
            if normalized_term in industry or normalized_term in products:
                rows.append(dict(row))

    if limit is not None:
        rows = rows[:limit]
    source_path = Path(data_path) if data_path else default_company_industry_data_path()
    return {
        "term": term,
        "matched_company_count": len(rows),
        "matches": rows,
        "company_names": [row["company_name"] for row in rows],
        "source_path": str(source_path),
        "source_url": KRX_LISTED_COMPANY_SOURCE_URL,
    }


def _report_scope_sql(base_filters: dict[str, Any], company_names: list[str]) -> tuple[str, list[Any]]:
    where = ["report_type = 'company'", "is_embedded = 1"]
    params: list[Any] = []
    if company_names:
        placeholders = ", ".join("?" for _ in company_names)
        where.append(f"target_name IN ({placeholders})")
        params.extend(company_names)
    else:
        where.append("1 = 0")
    if base_filters.get("report_date_start"):
        where.append("report_date >= ?")
        params.append(base_filters["report_date_start"])
    if base_filters.get("report_date_end"):
        where.append("report_date <= ?")
        params.append(base_filters["report_date_end"])
    if base_filters.get("broker"):
        where.append("broker = ?")
        params.append(base_filters["broker"])
    return (
        "SELECT DISTINCT target_name, file_name FROM reports "
        f"WHERE {' AND '.join(where)} ORDER BY target_name, file_name"
    ), params


def resolve_report_file_scope_for_companies(
    company_names: list[str],
    *,
    base_filters: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Return embedded company-report files for an already resolved company universe."""
    base_filters = dict(base_filters or {})
    sql, params = _report_scope_sql(base_filters, list(company_names or []))
    database_path = str(db_path or DB_PATH)
    with get_connection(database_path) as conn:
        rows = conn.execute(sql, params).fetchall()

    file_names = sorted({row["file_name"] for row in rows if row["file_name"]})
    matched_targets = sorted({row["target_name"] for row in rows if row["target_name"]})
    return {
        "matched_report_targets": matched_targets,
        "report_file_count": len(file_names),
        "file_names": file_names,
        "base_filters": base_filters,
    }


def resolve_industry_report_file_scope(
    term: str,
    *,
    base_filters: dict[str, Any] | None = None,
    data_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    base_filters = dict(base_filters or {})
    lookup = lookup_companies_by_industry(term, data_path=data_path)
    report_scope = resolve_report_file_scope_for_companies(
        lookup["company_names"],
        base_filters=base_filters,
        db_path=db_path,
    )
    return {
        "term": term,
        "matched_company_count": lookup["matched_company_count"],
        "matched_companies_preview": lookup["company_names"][:20],
        "matched_report_targets": report_scope["matched_report_targets"],
        "report_file_count": report_scope["report_file_count"],
        "file_names": report_scope["file_names"],
        "base_filters": base_filters,
        "source_path": lookup["source_path"],
        "source_url": lookup["source_url"],
    }
