import sqlite3
from types import SimpleNamespace

import pytest

from src.core import db_manager
from src.core.db_manager import parse_filename


def test_parse_filename_accepts_current_five_part_rule():
    parsed = parse_filename("company_2026-02-05_Samsung_Broker_HBM outlook.pdf")

    assert parsed == {
        "report_type": "company",
        "report_date": "2026-02-05",
        "target_name": "Samsung",
        "broker": "Broker",
        "title": "HBM outlook",
    }


def test_parse_filename_preserves_underscores_inside_title():
    parsed = parse_filename("company_2026-02-05_Samsung_Broker_HBM_AI outlook.pdf")

    assert parsed is not None
    assert parsed["title"] == "HBM_AI outlook"


def test_parse_filename_rejects_invalid_shape_or_date():
    assert parse_filename("Samsung.pdf") is None
    assert parse_filename("company_2026-99-99_Samsung_Broker_Title.pdf") is None
    assert parse_filename("company_2026-02-05_Samsung_Broker_Title.txt") is None


def test_native_report_projection_is_read_only(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(catalog) as connection:
        connection.executescript(
            """
            CREATE TABLE source_reports (
                report_id INTEGER,
                report_type TEXT,
                report_date TEXT,
                target_name TEXT,
                title TEXT,
                broker TEXT,
                canonical_relative_path TEXT
            );
            CREATE TABLE active_membership (report_id INTEGER);
            CREATE VIEW active_reports AS
            SELECT DISTINCT report.*
            FROM source_reports AS report
            JOIN active_membership AS membership
              ON membership.report_id = report.report_id;
            """
        )
        connection.execute(
            "INSERT INTO source_reports VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "company",
                "2026-02-05",
                "Example",
                "Outlook",
                "Broker",
                "pdfs/report.pdf",
            ),
        )
        connection.execute("INSERT INTO active_membership VALUES (?)", (1,))

    monkeypatch.setattr(
        db_manager,
        "_resolve_retrieval_dispatch",
        lambda _root: SimpleNamespace(
            mode="native",
            paths=SimpleNamespace(catalog=catalog),
        ),
    )

    with db_manager.get_connection() as connection:
        row = connection.execute("SELECT * FROM reports").fetchone()
        assert row["file_name"] == "report.pdf"
        assert row["is_embedded"] == 1
        projection = connection.execute(
            "SELECT type, sql FROM sqlite_temp_master WHERE name = 'reports'"
        ).fetchone()
        assert projection[0] == "table"
        assert [column[1] for column in connection.execute("PRAGMA table_info(reports)")] == [
            "id",
            "report_type",
            "report_date",
            "target_name",
            "title",
            "broker",
            "file_name",
            "is_embedded",
        ]
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM reports")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM main.source_reports")

    with db_manager.get_connection(materialize_reports=False) as connection:
        projection = connection.execute(
            "SELECT type FROM sqlite_temp_master WHERE name = 'reports'"
        ).fetchone()
        assert projection is None
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM main.source_reports")
