from src.core.followup_scope import (
    build_answer_scope_index,
    parse_ordinal_reference,
    resolve_section_followup_scope,
)


def test_build_answer_scope_index_groups_sources_by_report_type_with_inherited_dates():
    index = build_answer_scope_index(
        {
            "search_filters": {
                "report_date_start": "2026-06-15",
                "report_date_end": "2026-06-21",
            }
        },
        [
            {"report_type": "company", "file_name": "company-a.pdf"},
            {"report_type": "industry", "file_name": "industry-a.pdf"},
            {"report_type": "company", "file_name": "company-b.pdf"},
        ],
    )

    sections = {section["id"]: section for section in index["sections"]}
    assert sections["company"]["filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "report_type": "company",
    }
    assert sections["company"]["file_names"] == ["company-a.pdf", "company-b.pdf"]
    assert sections["industry"]["filters"]["report_type"] == "industry"


def test_build_answer_scope_index_respects_explicit_multiple_report_types_without_sources():
    index = build_answer_scope_index(
        {
            "search_filters": {
                "report_date_start": "2026-07-27",
                "report_date_end": "2026-08-02",
                "report_types": ["industry", "economy"],
            }
        },
        [],
    )

    assert [section["id"] for section in index["sections"]] == ["industry", "economy"]
    assert all("report_types" not in section["filters"] for section in index["sections"])


def test_resolve_section_followup_scope_matches_prior_section_alias_and_explains_decision():
    prior_scope = {
        "search_filters": {
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
        },
        "file_names": ["economy.pdf", "company-a.pdf", "industry.pdf"],
        "answer_scope_index": {
            "sections": [
                {
                    "id": "company",
                    "label": "개별 종목 분석 리포트",
                    "aliases": ["개별종목", "개별 종목", "종목", "기업", "company"],
                    "filters": {
                        "report_date_start": "2026-06-15",
                        "report_date_end": "2026-06-21",
                        "report_type": "company",
                    },
                    "file_names": ["company-a.pdf"],
                }
            ]
        },
    }

    decision = resolve_section_followup_scope(
        "개별종목 리포트에 대해 좀 더 자세히 작성해줘",
        current_filters={"report_type": "company"},
        prior_search_scope=prior_scope,
    )

    assert decision["matched"] is True
    assert decision["reason"] == "matched_prior_section_alias"
    assert decision["matched_section_id"] == "company"
    assert decision["matched_alias"] == "개별종목"
    assert decision["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "report_type": "company",
    }
    assert decision["dropped_filters"] == ["file_names"]


def test_resolve_section_followup_scope_falls_back_to_default_report_type_sections():
    prior_scope = {
        "search_filters": {
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
        },
        "file_names": ["mixed-a.pdf", "mixed-b.pdf"],
    }

    decision = resolve_section_followup_scope(
        "거시경제 부분을 상세히 알려줘",
        current_filters={"report_type": "economy"},
        prior_search_scope=prior_scope,
    )

    assert decision["matched"] is True
    assert decision["matched_section_id"] == "economy"
    assert decision["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "report_type": "economy",
    }


def test_resolve_section_followup_scope_keeps_plural_report_types_exclusive():
    prior_scope = {
        "search_filters": {
            "report_date_start": "2026-07-27",
            "report_date_end": "2026-08-02",
            "report_types": ["company", "industry", "economy"],
        },
        "answer_scope_index": {
            "sections": [
                {
                    "id": "company",
                    "label": "개별 종목 분석 리포트",
                    "aliases": ["기업", "company"],
                    "filters": {"report_type": "company"},
                },
                {
                    "id": "industry",
                    "label": "섹터/산업 리포트",
                    "aliases": ["산업", "industry"],
                    "filters": {"report_type": "industry"},
                },
            ]
        },
    }

    decision = resolve_section_followup_scope(
        "기업 및 산업 리포트를 자세히 알려줘",
        current_filters={"report_types": ["company", "industry"]},
        prior_search_scope=prior_scope,
    )

    assert decision["matched"] is True
    assert decision["search_filters"] == {
        "report_date_start": "2026-07-27",
        "report_date_end": "2026-08-02",
        "report_types": ["company", "industry"],
    }


def test_parse_ordinal_reference_handles_first_and_last():
    assert parse_ordinal_reference("첫번째 리포트 알려줘") == 0
    assert parse_ordinal_reference("1번째 리포트 보여줘") == 0
    assert parse_ordinal_reference("첫 번째 리포트 보여줘") == 0
    assert parse_ordinal_reference("두 번째 보고서 정리해줘") == 1
    assert parse_ordinal_reference("마지막 리포트 보여줘") == -1


def test_parse_ordinal_reference_requires_a_document_noun():
    assert parse_ordinal_reference("첫 고객사 매출을 알려줘") is None
    assert parse_ordinal_reference("두번째 회사 실적을 비교해줘") is None
    assert parse_ordinal_reference("리포트 중 두번째를 알려줘") is None


def test_parse_ordinal_reference_supports_arbitrary_numeric_rank():
    assert parse_ordinal_reference("12번째 문서를 확인해줘") == 11


def test_resolve_section_followup_scope_matches_ordinal_file_order():
    prior_scope = {
        "search_filters": {
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
        },
        "file_names": ["a.pdf", "b.pdf", "c.pdf"],
        "answer_scope_index": {
            "sections": [
                {
                    "id": "company",
                    "label": "개별 종목 리포트",
                    "aliases": ["개별종목", "company"],
                    "filters": {"report_type": "company"},
                    "file_names": ["a.pdf", "c.pdf"],
                },
                {
                    "id": "industry",
                    "label": "업종/섹터 리포트",
                    "aliases": ["섹터", "industry"],
                    "filters": {"report_type": "industry"},
                    "file_names": ["b.pdf"],
                },
            ]
        },
    }

    decision = resolve_section_followup_scope(
        "첫번째 리포트 자세히 알려줘",
        current_filters={},
        prior_search_scope=prior_scope,
    )

    assert decision["matched"] is True
    assert decision["reason"] == "matched_ordinal_report_reference"
    assert decision["matched_document_rank"] == 1
    assert decision["selected_file_name"] == "a.pdf"
    assert decision["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "file_names": ["a.pdf"],
    }


def test_resolve_section_followup_scope_matches_ordinal_within_report_type():
    prior_scope = {
        "search_filters": {
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
            "report_type": "company",
        },
        "file_names": ["industry-a.pdf", "company-a.pdf", "company-b.pdf", "industry-b.pdf"],
        "answer_scope_index": {
            "sections": [
                {
                    "id": "company",
                    "label": "개별 종목 리포트",
                    "aliases": ["개별종목", "company"],
                    "filters": {"report_type": "company"},
                    "file_names": ["company-a.pdf", "company-b.pdf"],
                },
            ]
        },
    }

    decision = resolve_section_followup_scope(
        "두번째 company 리포트 알려줘",
        current_filters={"report_type": "company"},
        prior_search_scope=prior_scope,
    )

    assert decision["matched"] is True
    assert decision["selected_file_name"] == "company-b.pdf"
    assert decision["search_filters"] == {
        "report_date_start": "2026-06-15",
        "report_date_end": "2026-06-21",
        "report_type": "company",
        "file_names": ["company-b.pdf"],
    }


def test_ordinal_document_rank_wins_over_content_words_after_the_reference():
    prior_scope = {
        "file_names": ["company-first.pdf", "industry-second.pdf"],
        "answer_scope_index": {
            "sections": [
                {
                    "id": "company",
                    "aliases": ["company", "기업"],
                    "filters": {"report_type": "company"},
                    "file_names": ["company-first.pdf"],
                },
                {
                    "id": "industry",
                    "aliases": ["industry", "산업"],
                    "filters": {"report_type": "industry"},
                    "file_names": ["industry-second.pdf"],
                },
            ]
        },
    }

    decision = resolve_section_followup_scope(
        "첫번째 리포트에서 산업 전망을 확인해줘",
        current_filters={"report_type": "industry"},
        prior_search_scope=prior_scope,
    )

    assert decision["selected_file_name"] == "company-first.pdf"
    assert decision["search_filters"] == {
        "file_names": ["company-first.pdf"]
    }


def test_resolve_section_followup_scope_reports_out_of_range_ordinal():
    decision = resolve_section_followup_scope(
        "세 번째 리포트를 확인해줘",
        current_filters={},
        prior_search_scope={"file_names": ["a.pdf", "b.pdf"]},
    )

    assert decision == {
        "matched": False,
        "reason": "document_ordinal_out_of_range",
        "requested_document_rank": 3,
        "document_count": 2,
    }
