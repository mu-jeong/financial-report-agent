from datetime import date
import json
import sqlite3

from langchain_core.documents import Document
import pytest

from src.core import metadata_filters as metadata_filters_module
from src.core.metadata_filters import (
    _active_metadata_rows,
    filter_docs_with_scores,
    infer_search_filters,
    metadata_matches,
    resolve_temporal_context,
)
from src.nodes import query_rewrite, router, search_scope, scope_selection
from src.utils.citations import (
    document_rank_aliases,
    group_sources_by_document,
    normalize_citation_ranks,
)


_ORIGINAL_GET_METADATA_CANDIDATES = metadata_filters_module.get_metadata_candidates


def _isolated_metadata_candidates():
    return {
        "target_name": (),
        "broker": (),
        "report_month": (),
        "target_report_types": {},
    }


@pytest.fixture(autouse=True)
def _isolate_repository_metadata(monkeypatch):
    monkeypatch.setattr(
        metadata_filters_module,
        "get_metadata_candidates",
        _isolated_metadata_candidates,
    )
    monkeypatch.setattr(
        search_scope,
        "get_metadata_candidates",
        _isolated_metadata_candidates,
    )


def test_native_metadata_candidates_follow_active_manifest_and_delta_revision(
    monkeypatch,
):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE retrieval_runtime (
                runtime_id INTEGER PRIMARY KEY,
                active_snapshot_id TEXT NOT NULL,
                active_build_id TEXT NOT NULL,
                publication_generation INTEGER NOT NULL
            );
            CREATE TABLE retrieval_builds (
                build_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                source_manifest_json TEXT NOT NULL
            );
            CREATE TABLE reports (
                report_uid TEXT PRIMARY KEY,
                canonical_relative_path TEXT NOT NULL,
                target_name TEXT,
                broker TEXT,
                report_date TEXT,
                report_type TEXT
            );
            CREATE TABLE retrieval_delta_segments (
                segment_id TEXT PRIMARY KEY,
                base_snapshot_id TEXT NOT NULL,
                base_publication_generation INTEGER NOT NULL,
                sequence INTEGER NOT NULL,
                state TEXT NOT NULL
            );
            CREATE TABLE retrieval_delta_reports (
                segment_id TEXT NOT NULL,
                canonical_relative_path TEXT NOT NULL,
                action TEXT NOT NULL,
                report_uid TEXT
            );
            CREATE TEMP VIEW reports AS
            SELECT target_name, broker, report_date, report_type
            FROM main.reports;
            """
        )
        manifest = {
            "reports": [
                {"report_uid": "included", "status": "included"},
                {"report_uid": "excluded", "status": "excluded"},
            ]
        }
        connection.execute(
            "INSERT INTO retrieval_runtime VALUES (1, 'snapshot', 'build', 3)"
        )
        connection.execute(
            "INSERT INTO retrieval_builds VALUES ('build', 'fully_complete', ?)",
            (json.dumps(manifest),),
        )
        connection.executemany(
            "INSERT INTO main.reports VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "included",
                    "reports/included.pdf",
                    "Alpha",
                    "Broker",
                    "2026-07-20",
                    "company",
                ),
                (
                    "excluded",
                    "reports/excluded.pdf",
                    "Beta",
                    "Broker",
                    "2026-07-21",
                    "company",
                ),
            ],
        )

        monkeypatch.setattr(
            metadata_filters_module,
            "get_connection",
            lambda **_kwargs: connection,
        )
        monkeypatch.setattr(
            metadata_filters_module,
            "get_metadata_candidates",
            _ORIGINAL_GET_METADATA_CANDIDATES,
        )
        metadata_filters_module._metadata_candidates_for_revision.cache_clear()

        rows = _active_metadata_rows(connection)
        initial = metadata_filters_module.get_metadata_candidates()

        assert [row["target_name"] for row in rows] == ["Alpha"]
        assert rows[0]["report_month"] == "2026-07"
        assert initial["target_name"] == ("Alpha",)

        connection.execute(
            "INSERT INTO retrieval_delta_segments VALUES (?, ?, ?, ?, ?)",
            ("segment", "snapshot", 3, 1, "ready"),
        )
        connection.execute(
            "INSERT INTO retrieval_delta_reports VALUES (?, ?, ?, ?)",
            ("segment", "reports/excluded.pdf", "upsert", "excluded"),
        )
        refreshed = metadata_filters_module.get_metadata_candidates()

        assert set(refreshed["target_name"]) == {"Alpha", "Beta"}
    finally:
        metadata_filters_module._metadata_candidates_for_revision.cache_clear()
        connection.close()


def test_infer_search_filters_from_known_metadata_values():
    filters = infer_search_filters(
        "미래에셋증권 삼성전자 리포트에서 HBM 전망 알려줘",
        {
            "target_name": ["삼성전자", "삼성"],
            "broker": ["미래에셋증권", "하나증권"],
        },
    )

    assert filters == {
        "target_name": "삼성전자",
        "broker": "미래에셋증권",
    }


def test_infer_search_filters_does_not_match_target_inside_broker_name():
    filters = infer_search_filters(
        "한화투자증권에서 발표한 내용에 대해 좀 더 상세하게 알려줘",
        {
            "target_name": ["증권", "SK하이닉스"],
            "broker": ["한화투자증권"],
            "target_report_types": {"증권": ("industry",), "SK하이닉스": ("company",)},
        },
    )

    assert filters == {"broker": "한화투자증권"}


def test_infer_search_filters_preserves_explicit_target_order_and_deduplicates():
    filters = infer_search_filters(
        "삼성전자와 SK하이닉스, 삼성전자를 비교해줘",
        {
            "target_name": ["SK하이닉스", "삼성전자", "삼성"],
            "broker": [],
            "target_report_types": {
                "SK하이닉스": ("company",),
                "삼성전자": ("company",),
                "삼성": ("company",),
            },
        },
    )

    assert filters == {
        "target_names": ["삼성전자", "SK하이닉스"],
        "report_type": "company",
    }


def test_infer_search_filters_resolves_only_unique_safe_suffix_aliases():
    unique = infer_search_filters(
        "하이닉스와 삼성 전자를 비교해줘",
        {
            "target_name": ["SK하이닉스", "삼성전자"],
            "broker": [],
            "target_report_types": {
                "SK하이닉스": ("company",),
                "삼성전자": ("company",),
            },
        },
    )
    ambiguous = infer_search_filters(
        "하이닉스를 요약해줘",
        {
            "target_name": ["SK하이닉스", "AB하이닉스"],
            "broker": [],
            "target_report_types": {
                "SK하이닉스": ("company",),
                "AB하이닉스": ("company",),
            },
        },
    )

    assert unique["target_names"] == ["SK하이닉스", "삼성전자"]
    assert "target_name" not in ambiguous
    assert "target_names" not in ambiguous


def test_infer_search_filters_does_not_derive_industry_keyword_from_company_name():
    candidates = {
        "target_name": ["KC산업"],
        "broker": [],
        "target_report_types": {"KC산업": ("company",)},
    }

    generic = infer_search_filters(
        "화장품 산업에 관련된 회사들 리포트 정리해서 알려줘",
        candidates,
    )
    explicit = infer_search_filters("KC산업 회사 리포트 정리해서 알려줘", candidates)

    assert "target_name" not in generic
    assert "target_names" not in generic
    assert explicit["target_name"] == "KC산업"


def test_metadata_matches_accepts_any_target_in_ordered_target_scope():
    filters = {"target_names": ["삼성전자", "SK하이닉스"], "report_type": "company"}

    assert metadata_matches(
        {"target_name": "SK하이닉스", "report_type": "company"},
        filters,
    )
    assert not metadata_matches(
        {"target_name": "LG전자", "report_type": "company"},
        filters,
    )


def test_infer_search_filters_detects_report_type_keywords():
    filters = infer_search_filters(
        "경제 리포트에서 금리 전망 알려줘",
        {
            "target_name": ["삼성전자"],
            "broker": ["미래에셋증권"],
        },
    )

    assert filters == {"report_type": "economy"}


def test_infer_search_filters_preserves_multiple_report_types():
    filters = infer_search_filters(
        "지난 주에 발간된 경제, 산업, 기업 리포트를 각각 알려줘",
        {
            "target_name": [],
            "broker": [],
            "report_month": [],
        },
        current_date=date(2026, 8, 9),
    )

    assert filters == {
        "report_date_start": "2026-07-27",
        "report_date_end": "2026-08-02",
        "report_types": ["company", "industry", "economy"],
    }


@pytest.mark.parametrize(
    ("query", "expected_type"),
    [
        ("삼성전자 기업 리포트에서 금리 영향", "company"),
        ("기업 리포트의 환율 리스크", "company"),
        ("자동차 산업 리포트에서 금리 전망", "industry"),
    ],
)
def test_explicit_report_type_wins_over_economic_topic_keywords(query, expected_type):
    filters = infer_search_filters(
        query,
        {
            "target_name": [],
            "broker": [],
            "report_month": [],
        },
    )

    assert filters == {"report_type": expected_type}


def test_metadata_matches_accepts_any_requested_report_type():
    filters = {"report_types": ["company", "industry"]}

    assert metadata_matches({"report_type": "company"}, filters)
    assert metadata_matches({"report_type": "industry"}, filters)
    assert not metadata_matches({"report_type": "economy"}, filters)


def test_metadata_matches_rejects_missing_report_type_for_plural_filter():
    filters = {"report_types": ["company"]}

    assert not metadata_matches({}, filters)
    assert not metadata_matches(
        {"file_name": "economy_2026-08-01_example.pdf"},
        filters,
    )


def test_current_report_types_replace_incompatible_prior_scalar_filter():
    merged = search_scope._merge_prior_filters_with_current(
        {"report_type": "company", "broker": "하나증권"},
        {"report_types": ["industry", "economy"]},
    )

    assert merged == {
        "report_types": ["industry", "economy"],
        "broker": "하나증권",
    }


def test_same_target_and_report_type_keep_prior_file_scope():
    merged = search_scope._merge_prior_filters_with_current(
        {
            "target_name": "화장품",
            "report_type": "industry",
            "file_names": ["first.pdf", "second.pdf"],
        },
        {"target_name": "화장품", "report_type": "industry"},
    )

    assert merged["file_names"] == ["first.pdf", "second.pdf"]


def test_new_target_keeps_prior_broker_but_drops_prior_file_scope():
    merged = search_scope._merge_prior_filters_with_current(
        {
            "broker": "하나증권",
            "file_names": ["hana-a.pdf"],
        },
        {"target_name": "삼성전자"},
    )

    assert merged == {
        "broker": "하나증권",
        "target_name": "삼성전자",
    }


def test_infer_search_filters_does_not_match_report_type_across_word_boundary():
    filters = infer_search_filters(
        "방금 리포트 리스크 알려줘",
        {
            "target_name": [],
            "broker": [],
            "report_month": [],
        },
    )

    assert "report_type" not in filters


@pytest.mark.parametrize(
    ("query", "expected_start", "expected_end"),
    [
        ("월만 있는 질의는 2월 리포트로 제한해줘", "2026-02-01", "2026-02-28"),
        ("월만 있는 질의는 5월 리포트로 제한해줘", "2026-05-01", "2026-05-31"),
        ("2025년 5월 리포트만 검색해줘", "2025-05-01", "2025-05-31"),
        ("2026-05-29 리포트 알려줘", "2026-05-29", "2026-05-29"),
        ("2026년 데이터에서 찾아줘", "2026-01-01", "2026-12-31"),
        ("2026년 2분기 데이터에서 찾아줘", "2026-04-01", "2026-06-30"),
        ("2026 Q2 데이터에서 찾아줘", "2026-04-01", "2026-06-30"),
        ("2026년 6월 1일~5일 발간 리포트", "2026-06-01", "2026-06-05"),
        ("2026-06-01~2026-06-05 발간 리포트", "2026-06-01", "2026-06-05"),
        ("6/15(월)", "2026-06-15", "2026-06-15"),
    ],
)
def test_infer_search_filters_normalizes_temporal_expressions(query, expected_start, expected_end):
    filters = infer_search_filters(
        query,
        {
            "target_name": [],
            "broker": [],
            "report_month": ["2026-06", "2026-05", "2025-05", "2026-02"],
        },
    )

    assert filters == {
        "report_date_start": expected_start,
        "report_date_end": expected_end,
    }


@pytest.mark.parametrize(
    "query",
    [
        "7월 20~24일 발간 리포트",
        "7월 20일 ~ 24일 발간 리포트",
        "7/20-24 발간 리포트",
        "7.20일부터 24일 발간 리포트",
    ],
)
def test_infer_search_filters_normalizes_yearless_same_month_date_ranges(query):
    filters = infer_search_filters(
        query,
        {
            "target_name": [],
            "broker": [],
            "report_month": ["2026-06", "2025-07", "2024-07"],
        },
        current_date=date(2026, 8, 2),
    )

    assert filters == {
        "report_date_start": "2025-07-20",
        "report_date_end": "2025-07-24",
    }


def test_yearless_same_month_date_range_falls_back_to_current_year():
    context = resolve_temporal_context(
        "7월 20~24일",
        known_months=["2026-06"],
        current_date=date(2026, 8, 2),
    )

    assert context is not None
    assert context["report_date_start"] == "2026-07-20"
    assert context["report_date_end"] == "2026-07-24"


@pytest.mark.parametrize("query", ["7월 24~20일", "2월 29~30일"])
def test_yearless_same_month_date_range_rejects_invalid_bounds(query):
    assert (
        resolve_temporal_context(
            query,
            known_months=["2025-07", "2025-02"],
            current_date=date(2026, 8, 2),
        )
        is None
    )


def test_infer_search_filters_combines_date_range_with_other_metadata():
    filters = infer_search_filters(
        "5월 데이터에서 NAVER 리포트 찾아줘",
        {
            "target_name": ["NAVER"],
            "broker": [],
            "report_month": ["2026-05", "2025-05", "2026-02"],
        },
    )

    assert filters == {
        "report_date_start": "2026-05-01",
        "report_date_end": "2026-05-31",
        "target_name": "NAVER",
    }


def test_infer_search_filters_detects_current_week():
    filters = infer_search_filters(
        "이번주 발간된 개별종목 리포트 알려줘",
        {
            "target_name": [],
            "broker": [],
            "report_month": ["2026-06", "2026-05"],
        },
        current_date=date(2026, 6, 6),
    )

    assert filters == {
        "report_date_start": "2026-06-01",
        "report_date_end": "2026-06-06",
        "report_type": "company",
    }


def test_resolve_temporal_context_for_relative_dates():
    today = date(2026, 6, 6)

    assert resolve_temporal_context("오늘 발간된 리포트", current_date=today) == {
        "expression": "오늘",
        "report_date_start": "2026-06-06",
        "report_date_end": "2026-06-06",
        "current_date": "2026-06-06",
        "description": "오늘=2026-06-06 (오늘 2026-06-06 기준)",
    }
    assert resolve_temporal_context("내일 발간 예정 리포트", current_date=today)[
        "description"
    ] == "내일=2026-06-07 (오늘 2026-06-06 기준)"
    assert resolve_temporal_context("다음주 리포트", current_date=today)[
        "description"
    ] == "다음주=2026-06-08~2026-06-14 (오늘 2026-06-06 기준)"


@pytest.mark.parametrize(
    ("query", "expected_expression", "expected_start", "expected_end"),
    [
        ("올해 발간된 리포트", "올해", "2026-01-01", "2026-12-31"),
        ("지난해 발간된 리포트", "지난해", "2025-01-01", "2025-12-31"),
        ("내년 발간 예정 리포트", "내년", "2027-01-01", "2027-12-31"),
    ],
)
def test_resolve_temporal_context_for_relative_years(
    query,
    expected_expression,
    expected_start,
    expected_end,
):
    context = resolve_temporal_context(query, current_date=date(2026, 8, 30))

    assert context == {
        "expression": expected_expression,
        "report_date_start": expected_start,
        "report_date_end": expected_end,
        "current_date": "2026-08-30",
        "description": (
            f"{expected_expression}={expected_start}~{expected_end} "
            "(오늘 2026-08-30 기준)"
        ),
    }


def test_metadata_matches_all_explicit_filters():
    metadata = {
        "target_name": "삼성전자",
        "broker": "미래에셋증권",
        "report_type": "company",
    }

    assert metadata_matches(metadata, {"target_name": "삼성전자", "broker": "미래에셋증권"})
    assert not metadata_matches(metadata, {"target_name": "SK하이닉스"})


def test_metadata_matches_date_range_filters():
    in_range_metadata = {"report_date": "2026-05-29", "target_name": "NAVER"}
    out_of_range_metadata = {"report_date": "2026-02-28", "target_name": "NAVER"}
    filters = {
        "report_date_start": "2026-05-01",
        "report_date_end": "2026-05-31",
        "target_name": "NAVER",
    }

    assert metadata_matches(in_range_metadata, filters)
    assert not metadata_matches(out_of_range_metadata, filters)


def test_filter_docs_with_scores_keeps_matching_documents_only():
    docs = [
        (Document(page_content="a", metadata={"target_name": "삼성전자", "broker": "미래에셋증권"}), 0.1),
        (Document(page_content="b", metadata={"target_name": "SK하이닉스", "broker": "하나증권"}), 0.2),
    ]

    filtered = filter_docs_with_scores(docs, {"target_name": "삼성전자"})

    assert len(filtered) == 1
    assert filtered[0][0].page_content == "a"


def test_filter_docs_with_scores_supports_file_name_scope():
    docs = [
        (
            Document(
                page_content="weekly",
                metadata={"file_name": "weekly-a.pdf", "report_date": "2026-06-10"},
            ),
            0.1,
        ),
        (
            Document(
                page_content="older",
                metadata={"file_name": "older.pdf", "report_date": "2026-05-10"},
            ),
            0.2,
        ),
    ]

    filtered = filter_docs_with_scores(
        docs,
        {
            "report_date_start": "2026-06-08",
            "report_date_end": "2026-06-14",
            "file_names": ["weekly-a.pdf"],
        },
    )

    assert len(filtered) == 1
    assert filtered[0][0].page_content == "weekly"


def test_document_source_aliases_are_sequential_after_deduplication():
    rerank_info = [
        {"rank": 1, "file_name": "naver-a.pdf", "title": "NAVER A"},
        {"rank": 2, "file_name": "naver-b.pdf", "title": "NAVER B"},
        {"rank": 4, "file_name": "naver-c.pdf", "title": "NAVER C"},
        {"rank": 5, "file_name": "naver-d.pdf", "title": "NAVER D"},
        {"rank": 6, "file_name": "naver-e.pdf", "title": "NAVER E"},
        {"rank": 13, "file_name": "naver-f.pdf", "title": "NAVER F"},
        {"rank": 14, "file_name": "naver-f.pdf", "title": "NAVER F duplicate chunk"},
    ]

    grouped_sources = group_sources_by_document(rerank_info)
    aliases = document_rank_aliases(rerank_info)

    assert len(grouped_sources) == 6
    assert aliases[1] == 1
    assert aliases[2] == 2
    assert aliases[4] == 3
    assert aliases[5] == 4
    assert aliases[6] == 5
    assert aliases[13] == 6
    assert aliases[14] == 6
    assert normalize_citation_ranks("근거 [1][2][4][5][6][13][14]", aliases) == (
        "근거 [1][2][3][4][5][6]"
    )


def test_document_source_aliases_prefer_report_uid_over_same_file_name():
    rerank_info = [
        {"rank": 1, "report_uid": "report-1", "file_name": "same.pdf"},
        {"rank": 2, "report_uid": "report-2", "file_name": "same.pdf"},
    ]

    assert len(group_sources_by_document(rerank_info)) == 2
    assert document_rank_aliases(rerank_info) == {1: 1, 2: 2}


def test_query_rewrite_marks_date_only_followup_for_router():
    question = "6/15(월)"

    result = query_rewrite.query_rewrite_node(
        {
            "question": question,
            "chat_history": [],
        }
    )

    assert result == {
        "rewritten_query": question,
        "uses_chat_history": False,
        "followup_scope_intent": True,
    }
    assert not query_rewrite.has_explicit_search_topic(question)


@pytest.mark.parametrize(
    ("question", "expected_report_type"),
    [
        ("\uae30\uc5c5\ubd84\uc11d", "company"),
        ("\uacbd\uc81c\ubd84\uc11d", "economy"),
        ("\uc0b0\uc5c5\ubd84\uc11d", "industry"),
    ],
)
def test_query_rewrite_marks_report_type_only_followup_for_router(
    question,
    expected_report_type,
    monkeypatch,
):
    monkeypatch.setattr(
        metadata_filters_module,
        "get_metadata_candidates",
        lambda: pytest.fail("report-type parsing must not read repository metadata"),
    )
    result = query_rewrite.query_rewrite_node(
        {
            "question": question,
            "chat_history": [],
        }
    )

    assert result == {
        "rewritten_query": question,
        "uses_chat_history": False,
        "followup_scope_intent": True,
    }
    candidates = _isolated_metadata_candidates()
    assert infer_search_filters(question, candidates)["report_type"] == expected_report_type


def test_query_rewrite_leaves_explicit_target_questions_independent_of_keyword_followup_rules():
    question = "sk하이닉스에 대한 내용을 더 자세히 알려줘"

    result = query_rewrite.query_rewrite_node(
        {
            "question": question,
            "chat_history": [],
        }
    )

    assert result == {
        "rewritten_query": question,
        "uses_chat_history": False,
        "followup_scope_intent": False,
    }


def test_search_scope_reuses_prior_period_for_target_detail_drilldown(monkeypatch):
    def fake_infer_search_filters(query):
        if "sk하이닉스" in query.casefold():
            return {"target_name": "SK하이닉스", "report_type": "company"}
        return {}

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    for question in [
        "sk하이닉스에 대한 내용 좀 더 자세히 알려줘",
        "sk하이닉스에 대한 내용을 더 자세히 알려줘",
    ]:
        for followup_scope_intent in [True, None]:
            result = search_scope.search_scope_node(
                {
                    "question": question,
                    "rewritten_query": question,
                    "chat_history": [],
                    "followup_scope_intent": followup_scope_intent,
                    "prior_search_scope": {
                        "route": "vectordb",
                        "search_filters": {
                            "report_date_start": "2026-06-22",
                            "report_date_end": "2026-06-27",
                        },
                        "temporal_context": {
                            "expression": "이번 주",
                            "report_date_start": "2026-06-22",
                            "report_date_end": "2026-06-27",
                        },
                        "file_names": ["weekly-a.pdf", "weekly-b.pdf"],
                    },
                }
            )

            assert result["scope_source"] == "prior_search_scope"
            assert result["temporal_context"] == {
                "expression": "이번 주",
                "report_date_start": "2026-06-22",
                "report_date_end": "2026-06-27",
            }
            assert result["search_filters"] == {
                "report_date_start": "2026-06-22",
                "report_date_end": "2026-06-27",
                "target_name": "SK하이닉스",
                "report_type": "company",
            }


def test_search_scope_broker_followup_preserves_prior_target_and_files(monkeypatch):
    def fake_infer_search_filters(query):
        if "한화투자증권" in query:
            return {"broker": "한화투자증권"}
        return {}

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_node(
        {
            "question": "한화투자증권에서 발표한 내용에 대해 좀 더 상세하게 알려줘",
            "rewritten_query": "한화투자증권에서 발표한 내용에 대해 좀 더 상세하게 알려줘",
            "chat_history": [],
            "followup_scope_intent": False,
            "prior_search_scope": {
                "route": "rdb",
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-27",
                    "target_name": "SK하이닉스",
                    "report_type": "company",
                },
                "temporal_context": {
                    "expression": "이번주",
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-27",
                },
                "file_names": [
                    "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 63.7 조원 전망.pdf",
                    "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배는 테크 주식의 기본 배수다.pdf",
                ],
            },
        }
    )

    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-27",
        "target_name": "SK하이닉스",
        "report_type": "company",
        "file_names": [
            "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 63.7 조원 전망.pdf",
            "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배는 테크 주식의 기본 배수다.pdf",
        ],
        "broker": "한화투자증권",
    }


def test_search_scope_new_target_drops_stale_prior_broker_and_file_scope(monkeypatch):
    def fake_infer_search_filters(query):
        if "DL이앤씨" in query:
            return {"target_name": "DL이앤씨", "report_type": "company"}
        return {}

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_node(
        {
            "question": "DL이앤씨에 대해서도 좀 더 자세히 알려줘",
            "rewritten_query": "DL이앤씨에 대해서도 좀 더 자세히 알려줘",
            "chat_history": [],
            "followup_scope_intent": False,
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-28",
                    "target_name": "SK하이닉스",
                    "report_type": "company",
                    "broker": "하나증권",
                },
                "temporal_context": {
                    "expression": "이번주",
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-28",
                },
                "file_names": [
                    "company_2026-06-26_SK하이닉스_하나증권_실적과 멀티플 둘 다 열려 있다.pdf"
                ],
            },
        }
    )

    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-28",
        "target_name": "DL이앤씨",
        "report_type": "company",
    }


def test_search_scope_sticky_thread_scope_merges_current_target_without_detail_keywords(monkeypatch):
    def fake_infer_search_filters(query):
        if "sk하이닉스" in query.casefold():
            return {"target_name": "SK하이닉스", "report_type": "company"}
        return {}

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_node(
        {
            "question": "SK하이닉스 알려줘",
            "rewritten_query": "SK하이닉스 알려줘",
            "chat_history": [],
            "followup_scope_intent": None,
            "prior_search_scope": {
                "route": "rdb",
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-27",
                },
                "temporal_context": {
                    "expression": "이번주",
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-27",
                },
                "file_names": ["weekly-a.pdf", "weekly-b.pdf"],
            },
        }
    )

    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-27",
        "target_name": "SK하이닉스",
        "report_type": "company",
    }
    assert result["temporal_context"]["report_date_end"] == "2026-06-27"


def test_search_scope_full_period_clears_prior_dates_but_keeps_current_target(monkeypatch):
    def fake_infer_search_filters(query):
        if "삼성전자" in query.casefold():
            return {"target_name": "삼성전자", "report_type": "company"}
        return {}

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_node(
        {
            "question": "삼성전자 전체 기간 리포트 알려줘",
            "rewritten_query": "삼성전자 전체 기간 리포트 알려줘",
            "chat_history": [],
            "followup_scope_intent": None,
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-27",
                    "target_name": "SK하이닉스",
                    "report_type": "company",
                },
                "temporal_context": {
                    "expression": "이번주",
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-27",
                },
                "file_names": ["skhynix.pdf"],
            },
        }
    )

    assert result["scope_source"] == "prior_search_scope"
    assert result["temporal_context"] is None
    assert result["search_filters"] == {
        "target_name": "삼성전자",
        "report_type": "company",
    }


def test_search_scope_repairs_inverted_date_range_from_prior_scope(monkeypatch):
    def fake_infer_search_filters(query):
        if "sk하이닉스" in query.casefold():
            return {
                "report_date_start": "2026-06-22",
                "report_date_end": "2026-06-06",
                "target_name": "SK하이닉스",
                "report_type": "company",
            }
        return {}

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: {
        "expression": "명시 날짜 범위",
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-06",
    })

    result = search_scope.search_scope_node(
        {
            "question": "sk하이닉스에 대한 내용 더 자세히 알려줘",
            "rewritten_query": "sk하이닉스에 대한 내용 더 자세히 알려줘",
            "chat_history": [],
            "followup_scope_intent": None,
            "prior_search_scope": {
                "route": "rdb",
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-27",
                },
                "temporal_context": {
                    "expression": "이번주",
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-27",
                },
            },
        }
    )

    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-27",
        "target_name": "SK하이닉스",
        "report_type": "company",
    }
    assert result["temporal_context"]["report_date_end"] == "2026-06-27"


def test_search_scope_reuses_prior_search_scope_for_summary_followup():
    prior_scope = {
        "search_filters": {
            "report_date_start": "2026-06-08",
            "report_date_end": "2026-06-14",
        },
        "temporal_context": {
            "expression": "이번 주",
            "report_date_start": "2026-06-08",
            "report_date_end": "2026-06-14",
        },
        "file_names": ["weekly-a.pdf", "weekly-b.pdf"],
    }

    result = search_scope.search_scope_node(
        {
            "question": "주요 내용을 정리해줘",
            "rewritten_query": "주요 내용을 정리해줘",
            "chat_history": [],
            "prior_search_scope": prior_scope,
            "followup_scope_intent": True,
        }
    )

    assert result["routing_context"]["has_vector_intent"] is True
    assert result["scope_source"] == "prior_search_scope"
    assert result["temporal_context"] == prior_scope["temporal_context"]
    assert result["search_filters"] == {
        "report_date_start": "2026-06-08",
        "report_date_end": "2026-06-14",
        "file_names": ["weekly-a.pdf", "weekly-b.pdf"],
    }


@pytest.mark.parametrize(
    ("question", "expected_report_type"),
    [
        ("\uae30\uc5c5\ubd84\uc11d", "company"),
        ("\uacbd\uc81c\ubd84\uc11d", "economy"),
        ("\uc0b0\uc5c5\ubd84\uc11d", "industry"),
    ],
)
def test_search_scope_reuses_prior_dates_for_report_type_only_followup(
    question,
    expected_report_type,
):
    prior_scope = {
        "search_filters": {
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
        },
        "temporal_context": {
            "expression": "current week",
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
        },
        "file_names": ["weekly-a.pdf", "weekly-b.pdf"],
    }

    result = search_scope.search_scope_node(
        {
            "question": question,
            "rewritten_query": question,
            "chat_history": [],
            "prior_search_scope": prior_scope,
            "followup_scope_intent": True,
        }
    )

    assert result["routing_context"]["route_hint"] == "rdb"
    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "report_type": expected_report_type,
    }


def test_search_scope_reuses_prior_non_temporal_filters_for_date_only_followup():
    result = search_scope.search_scope_node(
        {
            "question": "6/15(월)",
            "rewritten_query": "6/15(월)",
            "chat_history": [],
            "followup_scope_intent": True,
            "prior_search_scope": {
                "route": "rdb",
                "search_filters": {
                    "report_date_start": "2026-06-15",
                    "report_date_end": "2026-06-21",
                    "report_type": "industry",
                },
                "file_names": ["weekly-industry-a.pdf", "weekly-industry-b.pdf"],
            },
        }
    )

    assert result["routing_context"]["route_hint"] == "rdb"
    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-15",
        "report_type": "industry",
    }
    assert result["temporal_context"]["report_date_start"] == "2026-06-15"
    assert result["temporal_context"]["report_date_end"] == "2026-06-15"


def test_search_scope_reuses_prior_period_and_adds_current_target_for_deictic_period(monkeypatch):
    def fake_infer_search_filters(query):
        if "2026-06-22" in query:
            return {
                "report_date_start": "2026-06-22",
                "report_date_end": "2026-06-25",
                "target_name": "SK하이닉스",
                "report_type": "company",
            }
        if "sk하이닉스" in query.casefold():
            return {"target_name": "SK하이닉스", "report_type": "company"}
        return {}

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_node(
        {
            "question": "해당 기간 내에 발간된 sk하이닉스에 대한 리포트 정리해서 내용을 알려줘",
            "rewritten_query": "SK하이닉스 2026-06-22~2026-06-25 발간 리포트 요약 및 내용",
            "chat_history": [],
            "followup_scope_intent": True,
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-25",
                },
                "temporal_context": {
                    "expression": "이번주",
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-25",
                },
                "file_names": [
                    "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
                    "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
                    "company_2026-06-25_삼성전자_미래에셋증권_생산능력.pdf",
                ],
            },
        }
    )

    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-25",
        "target_name": "SK하이닉스",
        "report_type": "company",
    }


def test_search_scope_uses_active_scope_when_prior_scope_input_is_absent():
    result = search_scope.search_scope_node(
        {
            "question": "리스크는 뭐야?",
            "rewritten_query": "리스크는 뭐야?",
            "chat_history": [],
            "followup_scope_intent": True,
            "active_scope": {
                "route": "vectordb",
                "search_filters": {"target_name": "삼성전자", "report_type": "company"},
                "file_names": ["samsung.pdf"],
            },
        }
    )

    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "target_name": "삼성전자",
        "report_type": "company",
        "file_names": ["samsung.pdf"],
    }


def test_search_scope_keeps_prior_dates_and_adds_company_filter_for_section_deep_dive():
    result = search_scope.search_scope_node(
        {
            "question": "개별종목 리포트에 대해 좀 더 자세히 작성해줘",
            "rewritten_query": "개별종목 리포트에 대해 좀 더 자세히 작성해줘",
            "chat_history": [],
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-15",
                    "report_date_end": "2026-06-21",
                },
                "temporal_context": {
                    "expression": "지난주",
                    "report_date_start": "2026-06-15",
                    "report_date_end": "2026-06-21",
                },
                "file_names": ["economy.pdf", "company-a.pdf", "industry.pdf"],
            },
            "followup_scope_intent": True,
        }
    )

    assert result["routing_context"]["route_hint"] is None
    assert result["routing_context"]["has_vector_intent"] is True
    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "report_type": "company",
    }
    assert result["scope_decision"] == {
        "matched": True,
        "reason": "matched_prior_section_alias",
        "matched_section_id": "company",
        "matched_section_label": "개별 종목 분석 리포트",
        "matched_alias": "개별종목",
        "inherited_filters": {
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
        },
        "added_filters": {"report_type": "company"},
        "dropped_filters": ["file_names"],
        "search_filters": {
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
            "report_type": "company",
        },
    }


def test_search_scope_handles_ordinal_report_followup_with_file_indexing():
    result = search_scope.search_scope_node(
        {
            "question": "첫번째 리포트 좀 더 자세히 알려줘",
            "rewritten_query": "첫번째 리포트 좀 더 자세히 알려줘",
            "chat_history": [],
            "followup_scope_intent": True,
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-15",
                    "report_date_end": "2026-06-21",
                },
                "file_names": ["company-a.pdf", "industry-a.pdf", "economy-a.pdf"],
            },
        }
    )

    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "file_names": ["company-a.pdf"],
    }


def test_report_content_recheck_uses_first_prior_document_and_vectordb(monkeypatch):
    question = "첫번째 리포트에서 화장품과 관련된 내용이 확인되지 않아. 다시 확인해봐"

    monkeypatch.setattr(
        search_scope,
        "infer_search_filters",
        lambda _query: {"target_name": "화장품", "report_type": "industry"},
    )
    result = search_scope.search_scope_node(
        {
            "question": question,
            "rewritten_query": (
                "하나증권 Weekly Retail-Cos. Letter 기대가 확신으로 바뀌는 순간 "
                "화장품 관련 내용 재확인"
            ),
            "chat_history": [],
            "followup_scope_intent": True,
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "target_name": "화장품",
                    "report_type": "industry",
                },
                "file_names": ["weekly-retail-cos.pdf", "second-report.pdf"],
            },
        }
    )

    assert result["routing_context"] == {
        "has_vector_intent": True,
        "route_hint": None,
        "full_period_request": False,
    }
    assert result["search_filters"] == {
        "file_names": ["weekly-retail-cos.pdf"],
    }
    assert result["scope_decision"]["reason"] == "matched_ordinal_report_reference"
    assert router.router_node({"question": question, **result}) == {"route": "vectordb"}


def test_out_of_range_ordinal_does_not_expand_to_all_prior_documents():
    result = search_scope.search_scope_node(
        {
            "question": "세 번째 리포트를 확인해줘",
            "rewritten_query": "세 번째 리포트를 확인해줘",
            "followup_scope_intent": True,
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {},
                "file_names": ["a.pdf", "b.pdf"],
            },
        }
    )

    assert result["search_filters"] == {"file_names": []}
    assert result["scope_decision"] == {
        "matched": False,
        "reason": "document_ordinal_out_of_range",
        "requested_document_rank": 3,
        "document_count": 2,
    }
    assert result["routing_context"]["route_hint"] == "vectordb"
    assert not metadata_matches(
        {"file_name": "a.pdf"},
        result["search_filters"],
    )


def test_search_scope_drops_incompatible_sector_target_for_company_section_followup(monkeypatch):
    def fake_infer_search_filters(query):
        return {"target_name": "반도체", "report_type": "company"}

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(
        search_scope,
        "get_metadata_candidates",
        lambda: {"target_report_types": {"반도체": ("industry",)}},
    )

    result = search_scope.search_scope_node(
        {
            "question": "반도체 섹터에 속한 기업에 대한 내용을 좀 더 자세히 작성해줘",
            "rewritten_query": "반도체 섹터 기업들에 대한 이번 주 발간 리포트 상세 내용",
            "chat_history": [],
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-25",
                },
                "temporal_context": {
                    "expression": "이번주",
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-25",
                },
            },
            "followup_scope_intent": True,
        }
    )

    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-25",
        "report_type": "company",
    }


def test_search_scope_prepare_requests_industry_lookup_for_sector_company_question():
    result = search_scope.search_scope_prepare_node(
        {
            "question": "반도체 섹터에 속한 기업에 대한 내용을 좀 더 자세히 작성해줘",
            "chat_history": [],
            "prior_search_scope": {
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-25",
                },
            },
        }
    )

    assert result["scope_prepare"]["industry_lookup_request"] == {
        "term": "반도체",
        "reason": "sector_company_request",
        "target": "company_universe",
    }


@pytest.mark.parametrize(
    "question",
    [
        "화장품 산업에 관련된 회사들 리포트 정리해서 알려줘",
        "화장품 업종에 관련된 회사들 리포트 정리해서 알려줘",
    ],
)
def test_search_scope_prepare_routes_industry_synonyms_to_company_universe(
    question,
    monkeypatch,
):
    candidates = {
        "target_name": ("KC산업",),
        "broker": (),
        "report_month": (),
        "target_report_types": {"KC산업": ("company",)},
    }
    monkeypatch.setattr(metadata_filters_module, "get_metadata_candidates", lambda: candidates)
    monkeypatch.setattr(search_scope, "get_metadata_candidates", lambda: candidates)

    result = search_scope.search_scope_prepare_node({"question": question})
    prepare = result["scope_prepare"]

    assert "target_name" not in prepare["question_filters"]
    assert "target_names" not in prepare["question_filters"]
    assert prepare["industry_lookup_request"] == {
        "term": "화장품",
        "reason": "sector_company_request",
        "target": "company_universe",
    }


def test_search_scope_prepare_keeps_exact_industry_suffixed_company_as_company_lookup(
    monkeypatch,
):
    candidates = {
        "target_name": ("KC산업",),
        "broker": (),
        "report_month": (),
        "target_report_types": {"KC산업": ("company",)},
    }
    monkeypatch.setattr(metadata_filters_module, "get_metadata_candidates", lambda: candidates)
    monkeypatch.setattr(search_scope, "get_metadata_candidates", lambda: candidates)

    result = search_scope.search_scope_prepare_node(
        {"question": "KC산업 회사 리포트 정리해서 알려줘"}
    )
    prepare = result["scope_prepare"]

    assert prepare["question_filters"]["target_name"] == "KC산업"
    assert "industry_lookup_request" not in prepare


def test_search_scope_prepare_keeps_separate_company_and_industry_lookup(monkeypatch):
    candidates = {
        "target_name": ("삼성전자",),
        "broker": (),
        "report_month": (),
        "target_report_types": {"삼성전자": ("company",)},
    }
    monkeypatch.setattr(metadata_filters_module, "get_metadata_candidates", lambda: candidates)
    monkeypatch.setattr(search_scope, "get_metadata_candidates", lambda: candidates)

    result = search_scope.search_scope_prepare_node(
        {"question": "삼성전자와 화장품 산업 관련 회사들을 비교해줘"}
    )
    prepare = result["scope_prepare"]

    assert prepare["question_filters"]["target_name"] == "삼성전자"
    assert prepare["industry_lookup_request"] == {
        "term": "화장품",
        "reason": "sector_company_request",
        "target": "company_universe",
    }


def test_search_scope_merge_keeps_industry_lookup_as_company_universe():
    result = search_scope.search_scope_merge_node(
        {
            "question": "반도체 섹터에 속한 기업에 대한 내용을 좀 더 자세히 작성해줘",
            "rewritten_query": "반도체 섹터 기업 상세 내용",
            "chat_history": [],
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-25",
                },
                "temporal_context": {
                    "expression": "이번주",
                    "report_date_start": "2026-06-22",
                    "report_date_end": "2026-06-25",
                },
            },
            "followup_scope_intent": True,
            "industry_lookup_context": {
                "term": "반도체",
                "matched_company_count": 177,
                "matched_companies_preview": ["SK하이닉스", "삼성전자"],
                "company_names": ["SK하이닉스", "삼성전자"],
                "source_url": "https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage",
            },
        }
    )

    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-25",
        "report_type": "company",
    }
    assert result["scope_decision"]["reason"] == "industry_company_universe"
    assert result["scope_decision"]["industry_term"] == "반도체"
    assert result["routing_context"]["has_vector_intent"] is True


def test_rdb_scope_preflight_converts_industry_universe_to_sql_constraint():
    result = search_scope.rdb_scope_preflight_node(
        {
            "search_filters": {
                "report_date_start": "2026-06-22",
                "report_date_end": "2026-06-25",
                "report_type": "company",
            },
            "industry_lookup_context": {
                "term": "반도체",
                "company_names": ["SK하이닉스", "삼성전자"],
            },
        }
    )

    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-25",
        "report_type": "company",
        "target_names": ["SK하이닉스", "삼성전자"],
    }
    assert result["scope_decision"]["reason"] == "industry_company_universe_sql_constraint"


def test_vectordb_scope_preflight_converts_industry_universe_to_file_scope(monkeypatch):
    def fake_resolve(company_names, *, base_filters):
        assert company_names == ["SK하이닉스", "삼성전자"]
        assert base_filters == {
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-25",
            "report_type": "company",
        }
        return {
            "matched_report_targets": ["SK하이닉스", "삼성전자"],
            "report_file_count": 2,
            "file_names": ["hynix.pdf", "samsung.pdf"],
        }

    monkeypatch.setattr(search_scope, "resolve_report_file_scope_for_companies", fake_resolve)

    result = search_scope.vectordb_scope_preflight_node(
        {
            "search_filters": {
                "report_date_start": "2026-06-22",
                "report_date_end": "2026-06-25",
                "report_type": "company",
            },
            "industry_lookup_context": {
                "term": "반도체",
                "company_names": ["SK하이닉스", "삼성전자"],
                "source_url": "https://kind.krx.co.kr/corpgeneral/corpList.do?method=loadInitPage",
            },
        }
    )

    assert result["search_filters"] == {
        "report_date_start": "2026-06-22",
        "report_date_end": "2026-06-25",
        "report_type": "company",
        "file_names": ["hynix.pdf", "samsung.pdf"],
    }
    assert result["scope_decision"]["reason"] == "industry_company_universe_file_scope"


def test_search_scope_does_not_select_single_top_company_for_company_section_followup():
    result = search_scope.search_scope_node(
        {
            "question": "주요 기업에 대해 발간된 리포트를 상세하게 정리해줘",
            "rewritten_query": "top target company report summary",
            "chat_history": [],
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-15",
                    "report_date_end": "2026-06-21",
                },
                "temporal_context": {
                    "expression": "지난주",
                    "report_date_start": "2026-06-15",
                    "report_date_end": "2026-06-21",
                },
                "file_names": ["company-a.pdf", "company-b.pdf", "industry.pdf"],
            },
            "followup_scope_intent": True,
        }
    )

    assert "scope_selection_request" not in result
    assert result["scope_decision"]["matched_section_id"] == "company"
    assert result["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "report_type": "company",
    }


def test_search_scope_overrides_date_but_keeps_prior_non_temporal_scope(monkeypatch):
    def fake_infer_search_filters(query):
        return {
            "report_date_start": "2026-05-01",
            "report_date_end": "2026-05-31",
        }

    def fake_resolve_temporal_context(query):
        return {
            "expression": "5월",
            "report_date_start": "2026-05-01",
            "report_date_end": "2026-05-31",
        }

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", fake_resolve_temporal_context)

    result = search_scope.search_scope_node(
        {
            "question": "5월 주요 내용을 정리해줘",
            "rewritten_query": "5월 주요 내용을 정리해줘",
            "chat_history": [],
            "followup_scope_intent": True,
            "prior_search_scope": {
                "search_filters": {
                    "report_date_start": "2026-06-08",
                    "report_date_end": "2026-06-14",
                    "target_name": "삼성전자",
                    "report_type": "company",
                },
                "file_names": ["weekly-a.pdf"],
            },
        }
    )

    assert result["routing_context"]["has_vector_intent"] is True
    assert result["scope_source"] == "prior_search_scope"
    assert result["search_filters"] == {
        "report_date_start": "2026-05-01",
        "report_date_end": "2026-05-31",
        "target_name": "삼성전자",
        "report_type": "company",
    }
    assert "file_names" not in result["search_filters"]


def test_search_scope_requests_top_company_selection_from_prior_week_scope(monkeypatch):
    monkeypatch.setattr(scope_selection, "_top_company_target_from_filters", lambda filters: ("top-company", 3))

    state = {
        "question": "top target report summary",
        "rewritten_query": "top target report summary",
        "chat_history": [],
        "followup_scope_intent": True,
        "prior_search_scope": {
            "search_filters": {
                "report_date_start": "2026-06-15",
                "report_date_end": "2026-06-21",
            },
            "file_names": ["weekly-a.pdf", "weekly-b.pdf"],
        },
    }

    scope_result = search_scope.search_scope_node(state)

    assert scope_result["routing_context"]["has_vector_intent"] is True
    assert scope_result["scope_selection_request"] == {
        "type": "top_company_target_by_report_count",
        "filters": {
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
        },
    }

    result = scope_selection.scope_selection_node({**state, **scope_result})

    assert result["scope_source"] == "top_target_from_rdb"
    assert result["selection_context"] == {
        "strategy": "top_company_target_by_report_count",
        "target_name": "top-company",
        "report_count": 3,
    }
    assert result["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "target_name": "top-company",
        "report_type": "company",
    }
    assert result["rewritten_query"].startswith("top target report summary ")
    assert "top-company" in result["rewritten_query"]


def test_scope_selection_sanitizes_polluted_rewrite_after_top_company_selection(monkeypatch):
    monkeypatch.setattr(scope_selection, "_top_company_target_from_filters", lambda filters: ("DL E&C", 2))

    state = {
        "question": "summarize reports for the most published target company",
        "rewritten_query": "top target company report summary (Samsung, SK Square, Hyundai, DL E&C)",
        "chat_history": [],
        "followup_scope_intent": True,
        "prior_search_scope": {
            "search_filters": {
                "report_date_start": "2026-06-15",
                "report_date_end": "2026-06-19",
            },
        },
    }
    scope_result = search_scope.search_scope_node(state)

    result = scope_selection.scope_selection_node({**state, **scope_result})

    assert result["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-19",
        "target_name": "DL E&C",
        "report_type": "company",
    }
    assert result["rewritten_query"].startswith(
        "summarize reports for the most published target company "
    )
    assert "DL E&C" in result["rewritten_query"]
    assert "Samsung" not in result["rewritten_query"]
    assert "SK Square" not in result["rewritten_query"]

def test_search_scope_full_period_followup_keeps_prior_target_and_drops_dates():
    result = search_scope.search_scope_node(
        {
            "question": "full period",
            "rewritten_query": "full period",
            "chat_history": [],
            "followup_scope_intent": True,
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-06-15",
                    "report_date_end": "2026-06-21",
                    "target_name": "top-company",
                    "report_type": "company",
                },
                "file_names": ["weekly-a.pdf"],
            },
        }
    )

    assert result["routing_context"]["route_hint"] == "vectordb"
    assert result["scope_source"] == "prior_search_scope"
    assert result["temporal_context"] is None
    assert result["search_filters"] == {
        "target_name": "top-company",
        "report_type": "company",
    }


def test_search_scope_marks_long_period_target_summary_for_temporal_retrieval_plan(monkeypatch):
    def fake_infer_search_filters(query):
        return {
            "target_name": "삼성전자",
            "report_type": "company",
            "report_date_start": "2026-01-01",
            "report_date_end": "2026-12-31",
        }

    def fake_resolve_temporal_context(query):
        return {
            "expression": "올해",
            "report_date_start": "2026-01-01",
            "report_date_end": "2026-12-31",
            "current_date": "2026-06-28",
            "description": "올해=2026-01-01~2026-12-31",
        }

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", fake_resolve_temporal_context)

    result = search_scope.search_scope_node(
        {
            "question": "올해 발간된 삼성전자 리포트에 대해 시기별로 요약해서 정리해줘",
            "rewritten_query": "올해 발간된 삼성전자 리포트에 대해 시기별로 요약해서 정리해줘",
            "followup_scope_intent": False,
            "prior_search_scope": None,
        }
    )

    assert result["routing_context"]["has_vector_intent"] is True
    assert result["retrieval_plan"] == {
        "type": "temporal_report_set_summary",
        "preflight": "rdb_file_universe",
        "bucket_by": "month",
    }


def test_search_scope_does_not_mark_short_period_target_summary_for_temporal_plan(monkeypatch):
    def fake_infer_search_filters(query):
        return {
            "target_name": "삼성전자",
            "report_type": "company",
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-28",
        }

    def fake_resolve_temporal_context(query):
        return {
            "expression": "이번주",
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-28",
            "current_date": "2026-06-28",
            "description": "이번주=2026-06-22~2026-06-28",
        }

    monkeypatch.setattr(search_scope, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", fake_resolve_temporal_context)

    result = search_scope.search_scope_node(
        {
            "question": "이번주 삼성전자 리포트 요약해줘",
            "rewritten_query": "이번주 삼성전자 리포트 요약해줘",
            "followup_scope_intent": False,
            "prior_search_scope": None,
        }
    )

    assert "retrieval_plan" not in result


def test_search_scope_builds_ordered_company_comparison_and_clears_stale_scope(monkeypatch):
    candidates = {
        "target_name": ("SK하이닉스", "삼성전자"),
        "broker": (),
        "report_month": (),
        "target_report_types": {
            "SK하이닉스": ("company",),
            "삼성전자": ("company",),
        },
    }
    monkeypatch.setattr(metadata_filters_module, "get_metadata_candidates", lambda: candidates)
    monkeypatch.setattr(search_scope, "get_metadata_candidates", lambda: candidates)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_node(
        {
            "question": "삼성전자와 SK하이닉스 리포트 내용을 비교 요약해줘",
            "rewritten_query": "삼성전자와 SK하이닉스 리포트 내용을 비교 요약해줘",
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "report_date_start": "2026-08-01",
                    "report_date_end": "2026-08-12",
                    "target_name": "이전회사",
                    "report_type": "company",
                    "broker": "공통증권",
                },
                "file_names": ["stale.pdf"],
            },
        }
    )

    assert result["search_filters"] == {
        "report_date_start": "2026-08-01",
        "report_date_end": "2026-08-12",
        "report_type": "company",
        "broker": "공통증권",
        "target_names": ["삼성전자", "SK하이닉스"],
    }
    assert result["retrieval_plan"] == {
        "type": "company_comparison",
        "targets": ["삼성전자", "SK하이닉스"],
        "target_names": ["삼성전자", "SK하이닉스"],
        "shared_filters": {
            "report_date_start": "2026-08-01",
            "report_date_end": "2026-08-12",
            "report_type": "company",
            "broker": "공통증권",
        },
        "union_candidate_limit": 60,
        "final_budget": search_scope.SEARCH_TOP_K,
    }


def test_company_comparison_latest_intent_builds_deterministic_preflight_plan():
    plan = search_scope.build_retrieval_plan(
        "latest reports for A and B",
        {"report_type": "company", "target_names": ["A", "B"]},
        has_vector_intent=True,
    )

    assert plan["selection_mode"] == "latest_per_target"
    assert plan["preflight"] == "active_report_latest_per_target"
    assert plan["candidate_budget_per_target"] == 4
    assert plan["union_candidate_limit"] == 8
    assert plan["final_budget"] == 8

    document_plan = search_scope.build_retrieval_plan(
        "A와 B의 최신 문서를 각각 요약해줘",
        {"report_type": "company", "target_names": ["A", "B"]},
        has_vector_intent=True,
    )
    assert document_plan["selection_mode"] == "latest_per_target"


def test_search_scope_reuses_prior_ordered_targets_for_followup(monkeypatch):
    monkeypatch.setattr(search_scope, "infer_search_filters", lambda query: {})
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_node(
        {
            "question": "둘의 리스크를 자세히 요약해줘",
            "rewritten_query": "둘의 리스크를 자세히 요약해줘",
            "prior_search_scope": {
                "route": "vectordb",
                "search_filters": {
                    "target_names": ["삼성전자", "SK하이닉스"],
                    "report_type": "company",
                },
            },
        }
    )

    assert result["search_filters"]["target_names"] == ["삼성전자", "SK하이닉스"]
    assert result["retrieval_plan"]["type"] == "company_comparison"


def test_search_scope_marks_six_explicit_targets_without_truncation(monkeypatch):
    targets = ["회사A", "회사B", "회사C", "회사D", "회사E", "회사F"]
    candidates = {
        "target_name": tuple(targets),
        "broker": (),
        "report_month": (),
        "target_report_types": {target: ("company",) for target in targets},
    }
    monkeypatch.setattr(metadata_filters_module, "get_metadata_candidates", lambda: candidates)
    monkeypatch.setattr(search_scope, "get_metadata_candidates", lambda: candidates)
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_node(
        {
            "question": "회사A 회사B 회사C 회사D 회사E 회사F 내용을 비교 요약해줘",
            "rewritten_query": "회사A 회사B 회사C 회사D 회사E 회사F 내용을 비교 요약해줘",
        }
    )

    assert result["search_filters"]["target_names"] == targets
    assert result["retrieval_plan"] == {
        "type": "too_many_targets",
        "targets": targets,
        "target_names": targets,
        "max_targets": 5,
    }


def test_industry_company_universe_does_not_become_explicit_comparison(monkeypatch):
    monkeypatch.setattr(search_scope, "infer_search_filters", lambda query: {})
    monkeypatch.setattr(search_scope, "resolve_temporal_context", lambda query: None)

    result = search_scope.search_scope_merge_node(
        {
            "question": "반도체 업종 관련 기업 리포트 내용을 요약해줘",
            "rewritten_query": "반도체 업종 관련 기업 리포트 내용을 요약해줘",
            "industry_lookup_context": {
                "term": "반도체",
                "company_names": ["삼성전자", "SK하이닉스"],
            },
        }
    )

    assert result["scope_decision"]["reason"] == "industry_company_universe"
    assert "target_names" not in result["search_filters"]
    assert "retrieval_plan" not in result
