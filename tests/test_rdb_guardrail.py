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
