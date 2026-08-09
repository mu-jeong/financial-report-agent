from src.core.followup_scope import (
    build_answer_scope_index,
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
