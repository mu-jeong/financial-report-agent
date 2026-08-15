import sqlite3

import pytest

from src.nodes import rdb


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
        connection.executemany(
            "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "company", "2026-06-01", "A", "A old", "X", "a-old.pdf", 1),
                (2, "company", "2026-06-03", "A", "A new", "X", "a-new.pdf", 1),
                (3, "company", "2026-06-02", "B", "B old", "Y", "b-old.pdf", 1),
                (4, "company", "2026-06-04", "B", "B new", "Y", "b-new.pdf", 1),
                (5, "company", "2026-06-05", "C", "C only", "Z", "c.pdf", 1),
            ],
        )


def _fixture_connection(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def test_parameterized_target_scope_removes_hostile_llm_literal():
    hostile = "A'); DROP TABLE reports; --"
    llm_literal = hostile.replace("'", "''")

    query, params = rdb.build_target_scoped_query(
        f"SELECT target_name, title FROM reports WHERE target_name = '{llm_literal}'",
        [hostile, "B"],
    )

    assert hostile not in query
    assert "target_name IN (?, ?)" in query
    assert params == (hostile, "B")
    rdb.validate_target_scoped_query(query, params, [hostile, "B"])


def test_execute_sql_binds_target_names_instead_of_interpolating(tmp_path, monkeypatch):
    db_path = tmp_path / "fixture.sqlite3"
    _seed_reports(db_path)
    monkeypatch.setattr(rdb, "get_connection", lambda: _fixture_connection(db_path))

    query, params = rdb.build_target_scoped_query(
        "SELECT target_name, title FROM reports ORDER BY target_name, report_date",
        ["A", "B"],
    )
    result = rdb.execute_sql(query, params=params)

    assert result["rows"] == [
        ("A", "A old"),
        ("A", "A new"),
        ("B", "B old"),
        ("B", "B new"),
    ]


def test_latest_per_company_shape_is_preserved_and_scoped(tmp_path, monkeypatch):
    db_path = tmp_path / "fixture.sqlite3"
    _seed_reports(db_path)
    monkeypatch.setattr(rdb, "get_connection", lambda: _fixture_connection(db_path))
    generated = """
        WITH ranked AS (
            SELECT target_name, title, report_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY target_name
                       ORDER BY report_date DESC, id DESC
                   ) AS rn
            FROM reports
        )
        SELECT target_name, title
        FROM ranked
        WHERE rn = 1
        ORDER BY target_name
    """

    query, params = rdb.build_target_scoped_query(generated, ["B", "A"])
    result = rdb.execute_sql(query, params=params)

    assert result["rows"] == [("A", "A new"), ("B", "B new")]
    assert params == ("B", "A")


@pytest.mark.parametrize(
    ("query", "params", "targets"),
    [
        ("SELECT * FROM reports WHERE target_name IN (?)", (), ["A"]),
        ("SELECT * FROM reports WHERE target_name IN (?, ?)", ("A",), ["A", "B"]),
        ("SELECT * FROM reports WHERE target_name IN (?, ?)", ("B", "A"), ["A", "B"]),
        ("SELECT * FROM reports WHERE target_name = ?", ("A",), ["A"]),
        (
            "SELECT * FROM reports WHERE target_name = 'A' AND target_name IN (?)",
            ("A",),
            ["A"],
        ),
        (
            "SELECT * FROM reports WHERE target_name IN (?) OR broker = 'X'",
            ("A",),
            ["A"],
        ),
    ],
)
def test_validator_blocks_placeholder_or_target_mismatch(query, params, targets):
    with pytest.raises(ValueError):
        rdb.validate_target_scoped_query(query, params, targets)


def test_rdb_sql_gen_node_preserves_ordered_targets_as_bound_params(monkeypatch):
    class FakeChain:
        def __or__(self, _other):
            return self

        def invoke(self, _inputs):
            return "SELECT COUNT(*) AS count FROM reports WHERE target_name IN ('B', 'A')"

    monkeypatch.setattr(rdb, "build_chat_model", lambda **_kwargs: FakeChain())
    monkeypatch.setattr(rdb.PromptTemplate, "from_template", lambda _template: FakeChain())
    monkeypatch.setattr(rdb, "StrOutputParser", lambda: FakeChain())

    result = rdb.rdb_sql_gen_node(
        {
            "question": "A와 B 리포트 수",
            "search_filters": {"target_names": ["A", "B"]},
        }
    )

    assert "'A'" not in result["sql_query"]
    assert "'B'" not in result["sql_query"]
    assert "target_name IN (?, ?)" in result["sql_query"]
    assert result["sql_params"] == ("A", "B")
    assert "GROUP BY target_name" in result["sql_query"]
    assert result["rdb_query_shape"] == {
        "type": "count_by_target",
        "per_target_limit": None,
    }


def test_multi_company_count_includes_zero_and_requested_order(tmp_path, monkeypatch):
    db_path = tmp_path / "fixture.sqlite3"
    _seed_reports(db_path)
    monkeypatch.setattr(rdb, "get_connection", lambda: _fixture_connection(db_path))

    query, params, shape = rdb.build_multi_company_query(
        "B와 D와 A 리포트 개수",
        {"target_names": ["B", "D", "A"], "report_type": "company"},
    )
    raw = rdb.execute_sql(query, params=params)
    result, missing = rdb.normalize_multi_company_result(
        raw,
        ["B", "D", "A"],
        shape,
    )

    assert result == {
        "columns": ["target_name", "report_count"],
        "rows": [("B", 2), ("D", 0), ("A", 2)],
    }
    assert missing == ["D"]


def test_multi_company_latest_uses_per_target_window_not_global_limit(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "fixture.sqlite3"
    _seed_reports(db_path)
    monkeypatch.setattr(rdb, "get_connection", lambda: _fixture_connection(db_path))

    query, params, shape = rdb.build_multi_company_query(
        "A와 B의 최신 리포트 1개씩",
        {"target_names": ["A", "B"], "report_type": "company"},
    )
    result = rdb.execute_sql(query, params=params)
    normalized, missing = rdb.normalize_multi_company_result(
        result,
        ["A", "B"],
        shape,
    )

    assert "ROW_NUMBER() OVER (PARTITION BY target_name" in query
    assert "target_row_number <= 1" in query
    assert [row[2] for row in normalized["rows"]] == ["A", "B"]
    assert [row[4] for row in normalized["rows"]] == ["A new", "B new"]
    assert missing == []


def test_multi_company_query_enforces_all_mandatory_scope_filters(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "fixture.sqlite3"
    _seed_reports(db_path)
    monkeypatch.setattr(rdb, "get_connection", lambda: _fixture_connection(db_path))

    filters = {
        "target_names": ["A", "B"],
        "report_type": "company",
        "broker": "X",
        "report_date_start": "2026-06-02",
        "report_date_end": "2026-06-03",
    }
    query, params, shape = rdb.build_multi_company_query("리포트 목록", filters)
    result = rdb.execute_sql(query, params=params)
    normalized, missing = rdb.normalize_multi_company_result(
        result,
        filters["target_names"],
        shape,
    )

    assert [row[2] for row in normalized["rows"]] == ["A"]
    assert [row[4] for row in normalized["rows"]] == ["A new"]
    assert missing == ["B"]


def test_multi_company_list_has_balanced_total_row_cap(tmp_path, monkeypatch):
    db_path = tmp_path / "fixture.sqlite3"
    _seed_reports(db_path)
    monkeypatch.setattr(rdb, "get_connection", lambda: _fixture_connection(db_path))
    monkeypatch.setattr(rdb, "MULTI_TARGET_TOTAL_ROW_LIMIT", 3)

    query, params, shape = rdb.build_multi_company_query(
        "A와 B 리포트 목록",
        {"target_names": ["A", "B"], "report_type": "company"},
    )
    raw = rdb.execute_sql(query, params=params)
    result, missing = rdb.normalize_multi_company_result(
        raw,
        ["A", "B"],
        shape,
    )

    assert "ORDER BY target_row_number ASC" in query
    assert "LIMIT 3" in query
    assert len(result["rows"]) == 3
    assert {row[2] for row in result["rows"]} == {"A", "B"}
    assert missing == []


def test_target_normalization_dedupes_whitespace_variants():
    query, params, _shape = rdb.build_multi_company_query(
        "리포트 목록",
        {"target_names": ["A", " A ", "B"]},
    )

    assert params == ("A", "B")
    assert "target_name IN (?, ?)" in query


@pytest.mark.parametrize(
    ("question", "expected_type"),
    [
        ("A와 B 리포트 수", "count_by_target"),
        ("A와 B의 최근 리포트 목록", "list_per_target"),
        ("A와 B의 최근 리포트 추세", "list_per_target"),
    ],
)
def test_multi_company_intent_shape_precedence(question, expected_type):
    _query, _params, shape = rdb.build_multi_company_query(
        question,
        {"target_names": ["A", "B"]},
    )

    assert shape["type"] == expected_type


@pytest.mark.parametrize(
    "query",
    [
        "SELECT target_name, title FROM reports ORDER BY report_date DESC LIMIT 1",
        "SELECT target_name, COUNT(*) FROM reports",
    ],
)
def test_target_scope_builder_rejects_globally_biased_shapes(query):
    with pytest.raises(ValueError):
        rdb.build_target_scoped_query(query, ["A", "B"])


def test_rdb_execute_node_passes_sql_params_once(monkeypatch):
    calls = []

    def fake_execute(query, params=()):
        calls.append((query, params))
        return "Error: stop after execution boundary"

    monkeypatch.setattr(rdb, "execute_sql", fake_execute)

    filters = {"target_names": ["A", "B"]}
    query, params, shape = rdb.build_multi_company_query(
        "A와 B 리포트 수",
        filters,
    )

    rdb.rdb_execute_node(
        {
            "question": "A와 B 리포트 수",
            "search_filters": filters,
            "sql_query": query,
            "sql_params": params,
            "rdb_query_shape": shape,
        }
    )

    assert calls == [(query, params)]


def test_rdb_execute_node_blocks_params_without_matching_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(rdb, "execute_sql", lambda *_args, **_kwargs: calls.append(True))

    result = rdb.rdb_execute_node(
        {
            "question": "A와 B 리포트 수",
            "sql_query": "SELECT target_name, COUNT(*) FROM reports "
            "WHERE target_name IN (?, ?) GROUP BY target_name",
            "sql_params": ("A", "B"),
        }
    )

    assert calls == []
    assert "validation failed" in result["rdb_result"]
