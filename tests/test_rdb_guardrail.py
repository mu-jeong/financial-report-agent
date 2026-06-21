import sqlite3

import pytest

from src.nodes import rdb


def test_execute_sql_allows_readonly_select(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        conn.execute(
            """
            INSERT INTO reports
                (report_type, report_date, target_name, title, broker, file_name, is_embedded)
            VALUES
                ('company', '2026-02-05', '삼성전자', 'HBM 전망', '미래에셋증권', 'sample.pdf', 1)
            """
        )

    monkeypatch.setattr(rdb, "DB_PATH", str(db_path))

    result = rdb.execute_sql("SELECT target_name, title FROM reports")

    assert result == {
        "columns": ["target_name", "title"],
        "rows": [("삼성전자", "HBM 전망")],
    }


def test_execute_sql_allows_readonly_cte_aliases(tmp_path, monkeypatch):
    db_path = tmp_path / "reports.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        conn.execute(
            """
            INSERT INTO reports
                (report_type, report_date, target_name, title, broker, file_name, is_embedded)
            VALUES
                ('company', '2026-06-05', '올릭스', '로레알 지분투자', '교보증권', 'olix.pdf', 1)
            """
        )

    monkeypatch.setattr(rdb, "DB_PATH", str(db_path))

    result = rdb.execute_sql(
        """
        WITH week_start AS (SELECT '2026-06-01' AS start_date)
        SELECT target_name
        FROM reports, week_start
        WHERE report_date >= week_start.start_date
        """
    )

    assert result == {
        "columns": ["target_name"],
        "rows": [("올릭스",)],
    }


@pytest.mark.parametrize(
    "query",
    [
        "DELETE FROM reports",
        "UPDATE reports SET title = 'x'",
        "SELECT name FROM sqlite_master",
        "SELECT * FROM parent_chunks",
        "not a sql query",
    ],
)
def test_execute_sql_blocks_non_select_or_unauthorized_tables(query):
    result = rdb.execute_sql(query)

    assert isinstance(result, str)
    assert result.startswith("Error:")


def test_execute_sql_blocks_unauthorized_tables_inside_cte():
    result = rdb.execute_sql(
        """
        WITH leaked AS (SELECT * FROM parent_chunks)
        SELECT * FROM leaked
        """
    )

    assert isinstance(result, str)
    assert result.startswith("Error:")


def test_extract_sources_from_rdb_result_uses_file_name_rows():
    result = {
        "columns": ["report_type", "report_date", "target_name", "broker", "title", "file_name"],
        "rows": [
            (
                "company",
                "2026-06-16",
                "현대차",
                "유안타증권",
                "속성이 다른 이익에 기반한 밸류에이션 할증",
                "yuanta.pdf",
            ),
            (
                "company",
                "2026-06-18",
                "현대차",
                "한화투자증권",
                "기대감에서 실적으로",
                "hanwha.pdf",
            ),
        ],
    }

    sources = rdb.extract_sources_from_rdb_result(result)

    assert [source["file_name"] for source in sources] == ["yuanta.pdf", "hanwha.pdf"]
    assert [source["rank"] for source in sources] == [1, 2]
    assert sources[0]["broker"] == "유안타증권"


def test_extract_sources_from_rdb_result_ignores_aggregate_rows_without_file_name():
    assert rdb.extract_sources_from_rdb_result(
        {"columns": ["count"], "rows": [(2,)]}
    ) == []


def test_summarize_rdb_result_counts_rows_and_groups():
    result = {
        "columns": ["report_date", "report_type", "target_name", "broker", "file_name"],
        "rows": [
            ("2026-06-15", "company", "A", "broker-a", "a.pdf"),
            ("2026-06-15", "company", "B", "broker-a", "b.pdf"),
            ("2026-06-16", "industry", "sector", "broker-b", "c.pdf"),
        ],
    }

    summary = rdb.summarize_rdb_result(result)

    assert summary["row_count"] == 3
    assert summary["column_count"] == 5
    assert summary["by_report_date"] == {"2026-06-15": 2, "2026-06-16": 1}
    assert summary["by_report_type"] == {"company": 2, "industry": 1}
    assert summary["by_broker"] == {"broker-a": 2, "broker-b": 1}
    assert summary["by_target_name"] == {"A": 1, "B": 1, "sector": 1}


def test_format_rdb_result_for_answer_puts_summary_before_raw_rows():
    result = {"columns": ["report_date"], "rows": [("2026-06-15",)]}
    summary = {"row_count": 1}

    formatted = rdb.format_rdb_result_for_answer(result, summary)

    assert formatted.startswith("[DB_RESULT_SUMMARY]")
    assert "'row_count': 1" in formatted
    assert "[RAW_DB_RESULT]" in formatted
