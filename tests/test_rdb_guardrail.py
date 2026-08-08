import sqlite3

import pytest

from src.core import db_manager
from src.core.company_industry import resolve_report_file_scope_for_companies
from src.nodes import rdb
from src.retrieval.build_service import materialize_candidate, publish_candidate
from tests.retrieval.native_build_fixtures import (
    DeterministicEmbeddings,
    _metadata,
    _native_seed,
    _prepare,
)


def _seed_reports(path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY,
                report_type TEXT NOT NULL,
                report_date TEXT NOT NULL,
                target_name TEXT,
                title TEXT NOT NULL,
                broker TEXT NOT NULL,
                file_name TEXT NOT NULL UNIQUE,
                is_embedded INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "company",
                "2026-06-05",
                "Example",
                "Outlook",
                "Broker",
                "example.pdf",
                1,
            ),
        )


def _fixture_connection(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def test_execute_sql_allows_readonly_select(tmp_path, monkeypatch):
    db_path = tmp_path / "fixture.sqlite3"
    _seed_reports(db_path)
    monkeypatch.setattr(rdb, "get_connection", lambda: _fixture_connection(db_path))

    result = rdb.execute_sql("SELECT target_name, title FROM reports")

    assert result == {
        "columns": ["target_name", "title"],
        "rows": [("Example", "Outlook")],
    }


def test_execute_sql_allows_readonly_cte_aliases(tmp_path, monkeypatch):
    db_path = tmp_path / "fixture.sqlite3"
    _seed_reports(db_path)
    monkeypatch.setattr(rdb, "get_connection", lambda: _fixture_connection(db_path))

    result = rdb.execute_sql(
        """
        WITH week_start AS (SELECT '2026-06-01' AS start_date)
        SELECT target_name
        FROM reports, week_start
        WHERE report_date >= week_start.start_date
        """
    )

    assert result == {"columns": ["target_name"], "rows": [("Example",)]}


def test_native_rdb_readers_follow_active_successor(tmp_path, monkeypatch):
    data_root, sources = _native_seed(tmp_path)
    monkeypatch.setattr(db_manager, "DATA_ROOT", str(data_root))

    def company_metadata(file_name: str):
        value = dict(_metadata(file_name) or {})
        if file_name == "b.pdf":
            value.update(report_type="company", target_name="B")
        return value

    initial = rdb.execute_sql("SELECT id, file_name FROM reports ORDER BY file_name")
    assert [row[1] for row in initial["rows"]] == ["a.pdf", "b.pdf"]
    initial_b_id = next(row[0] for row in initial["rows"] if row[1] == "b.pdf")
    assert resolve_report_file_scope_for_companies(["A", "B"])["file_names"] == [
        "a.pdf"
    ]

    successor_plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        metadata_parser=company_metadata,
    )
    publish_candidate(materialize_candidate(successor_plan, data_root), data_root)
    successor = rdb.execute_sql("SELECT id, file_name FROM reports ORDER BY file_name")
    assert [row[1] for row in successor["rows"]] == ["a.pdf", "b.pdf"]
    successor_b_id = next(row[0] for row in successor["rows"] if row[1] == "b.pdf")
    assert successor_b_id != initial_b_id
    assert resolve_report_file_scope_for_companies(["A", "B"])["file_names"] == [
        "a.pdf",
        "b.pdf",
    ]

    (sources / "b.pdf").write_bytes(b"corrected-b")
    correction_plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        metadata_parser=company_metadata,
    )
    publish_candidate(materialize_candidate(correction_plan, data_root), data_root)
    corrected = rdb.execute_sql("SELECT id, file_name FROM reports ORDER BY file_name")
    corrected_b_id = next(row[0] for row in corrected["rows"] if row[1] == "b.pdf")
    assert corrected_b_id != successor_b_id

    (sources / "a.pdf").unlink()
    deletion_plan = _prepare(
        data_root,
        sources,
        DeterministicEmbeddings(),
        metadata_parser=company_metadata,
        deleted_relative_paths=("downloaded/a.pdf",),
    )
    publish_candidate(materialize_candidate(deletion_plan, data_root), data_root)
    deleted = rdb.execute_sql("SELECT file_name FROM reports ORDER BY file_name")
    assert deleted == {"columns": ["file_name"], "rows": [("b.pdf",)]}
    assert resolve_report_file_scope_for_companies(["A", "B"])["file_names"] == [
        "b.pdf"
    ]


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


def test_rdb_execute_node_records_query_duration_on_blocked_result(monkeypatch):
    ticks = iter([1_000_000_000, 1_125_000_000])
    monkeypatch.setattr(rdb.time, "perf_counter_ns", lambda: next(ticks))
    monkeypatch.setattr(
        rdb,
        "execute_sql",
        lambda _query: "Error: blocked for deterministic test",
    )

    result = rdb.rdb_execute_node(
        {
            "question": "count reports",
            "sql_query": "SELECT COUNT(*) FROM reports",
        }
    )

    assert result["monitoring_metrics"]["rdb"]["query_ns"] == 125_000_000


def test_extract_sources_from_rdb_result_uses_file_name_rows():
    result = {
        "columns": [
            "report_type",
            "report_date",
            "target_name",
            "broker",
            "title",
            "file_name",
        ],
        "rows": [
            ("company", "2026-06-16", "A", "Broker A", "Title A", "a.pdf"),
            ("company", "2026-06-18", "B", "Broker B", "Title B", "b.pdf"),
        ],
    }

    sources = rdb.extract_sources_from_rdb_result(result)

    assert [source["file_name"] for source in sources] == ["a.pdf", "b.pdf"]
    assert [source["rank"] for source in sources] == [1, 2]
    assert sources[0]["broker"] == "Broker A"


def test_extract_sources_from_rdb_result_ignores_rows_without_file_name():
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
