from datetime import date

from langchain_core.documents import Document
import pytest

from src.core.metadata_filters import (
    filter_docs_with_scores,
    infer_search_filters,
    metadata_matches,
    resolve_temporal_context,
)
from src.nodes import query_rewrite, router
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
    ],
)
def test_infer_search_filters_normalizes_temporal_expressions(query, expected_start, expected_end):
    filters = infer_search_filters(
        query,
        {
            "target_name": [],
            "broker": [],
            "report_month": ["2026-05", "2025-05", "2026-02"],
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


def test_router_reuses_prior_search_scope_for_summary_followup(monkeypatch):
    def fail_if_llm_router_is_called(*args, **kwargs):
        raise AssertionError("summary follow-up should route deterministically")

    monkeypatch.setattr(router, "build_chat_model", fail_if_llm_router_is_called)

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

    result = router.router_node(
        {
            "question": "주요 내용을 정리해줘",
            "rewritten_query": "주요 내용을 정리해줘",
            "chat_history": [],
            "prior_search_scope": prior_scope,
            "followup_scope_intent": True,
        }
    )

    assert result["route"] == "vectordb"
    assert result["scope_source"] == "prior_search_scope"
    assert result["temporal_context"] == prior_scope["temporal_context"]
    assert result["search_filters"] == {
        "report_date_start": "2026-06-08",
        "report_date_end": "2026-06-14",
        "file_names": ["weekly-a.pdf", "weekly-b.pdf"],
    }


def test_router_keeps_explicit_current_filters_over_prior_scope(monkeypatch):
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

    monkeypatch.setattr(router, "infer_search_filters", fake_infer_search_filters)
    monkeypatch.setattr(router, "resolve_temporal_context", fake_resolve_temporal_context)

    result = router.router_node(
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

    assert result["route"] == "vectordb"
    assert result["scope_source"] is None
    assert result["search_filters"] == {
        "report_date_start": "2026-05-01",
        "report_date_end": "2026-05-31",
    }
