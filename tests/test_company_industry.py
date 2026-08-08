import csv
import sqlite3
from pathlib import Path

from src.core.company_industry import (
    lookup_companies_by_industry,
    resolve_industry_report_file_scope,
)


def _write_company_industry_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["company_name", "industry", "main_products"])
        writer.writeheader()
        writer.writerows(rows)


def _write_catalog_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE reports (
            report_type TEXT NOT NULL,
            report_date TEXT NOT NULL,
            target_name TEXT,
            title TEXT NOT NULL,
            broker TEXT NOT NULL,
            file_name TEXT NOT NULL UNIQUE,
            is_embedded INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.executemany(
        "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("company", "2026-06-25", "삼성전자", "반도체 리포트", "A증권", "samsung.pdf", 1),
            ("company", "2026-06-25", "SK하이닉스", "메모리 리포트", "A증권", "hynix.pdf", 1),
            ("company", "2026-06-25", "현대차", "자동차 리포트", "A증권", "hyundai.pdf", 1),
            ("company", "2026-06-10", "파두", "SSD 리포트", "A증권", "fadu-old.pdf", 1),
        ],
    )
    conn.commit()
    conn.close()


def test_lookup_companies_by_industry_matches_industry_and_products(tmp_path):
    data_path = tmp_path / "listed_company_industries.csv"
    _write_company_industry_csv(
        data_path,
        [
            {"company_name": "삼성전자", "industry": "반도체 제조업", "main_products": "메모리 반도체"},
            {"company_name": "SK하이닉스", "industry": "전자부품 제조업", "main_products": "반도체기억장치"},
            {"company_name": "현대차", "industry": "자동차 제조업", "main_products": "승용차"},
        ],
    )

    result = lookup_companies_by_industry("반도체", data_path=data_path)

    assert result["term"] == "반도체"
    assert result["matched_company_count"] == 2
    assert [row["company_name"] for row in result["matches"]] == ["삼성전자", "SK하이닉스"]
    assert result["source_url"] == "https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage"


def test_resolve_industry_report_file_scope_intersects_lookup_with_report_scope(
    tmp_path, monkeypatch
):
    data_path = tmp_path / "listed_company_industries.csv"
    db_path = tmp_path / "catalog.sqlite3"
    _write_company_industry_csv(
        data_path,
        [
            {"company_name": "삼성전자", "industry": "반도체 제조업", "main_products": "메모리"},
            {"company_name": "SK하이닉스", "industry": "전자부품 제조업", "main_products": "반도체기억장치"},
            {"company_name": "파두", "industry": "반도체 제조업", "main_products": "SSD 컨트롤러"},
            {"company_name": "현대차", "industry": "자동차 제조업", "main_products": "승용차"},
        ],
    )
    _write_catalog_fixture(db_path)

    def open_fixture():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr("src.core.company_industry.get_connection", open_fixture)

    result = resolve_industry_report_file_scope(
        "반도체",
        base_filters={
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-25",
            "report_type": "company",
        },
        data_path=data_path,
    )

    assert result["matched_company_count"] == 3
    assert result["report_file_count"] == 2
    assert result["file_names"] == ["hynix.pdf", "samsung.pdf"]
    assert result["matched_report_targets"] == ["SK하이닉스", "삼성전자"]
