from datetime import date

from langchain_core.documents import Document
import pytest

from src.core.metadata_filters import (
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


def test_infer_search_filters_detects_report_type_keywords():
    filters = infer_search_filters(
        "경제 리포트에서 금리 전망 알려줘",
        {
            "target_name": ["삼성전자"],
            "broker": ["미래에셋증권"],
        },
    )

    assert filters == {"report_type": "economy"}


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


def test_query_rewrite_marks_prior_scope_followup_for_router():
    result = query_rewrite.query_rewrite_node(
        {
            "question": "주요 내용을 정리해줘",
            "chat_history": [],
        }
    )

    assert result == {
        "rewritten_query": "주요 내용을 정리해줘",
        "uses_chat_history": False,
        "followup_scope_intent": True,
    }


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
def test_query_rewrite_marks_report_type_only_followup_for_router(question, expected_report_type):
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
    assert infer_search_filters(question)["report_type"] == expected_report_type


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


def test_search_scope_keeps_explicit_current_filters_over_prior_scope(monkeypatch):
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
                },
                "file_names": ["weekly-a.pdf"],
            },
        }
    )

    assert result["routing_context"]["has_vector_intent"] is True
    assert result["scope_source"] is None
    assert result["search_filters"] == {
        "report_date_start": "2026-05-01",
        "report_date_end": "2026-05-31",
    }


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
