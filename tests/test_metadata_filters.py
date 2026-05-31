from langchain_core.documents import Document
import pytest

from src.core.metadata_filters import (
    filter_docs_with_scores,
    infer_search_filters,
    metadata_matches,
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
