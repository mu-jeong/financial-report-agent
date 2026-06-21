from datetime import date

import pytest

from src.core.report_crawler import (
    _crawl_start_date,
    classify_report_date,
    normalize_report_categories,
)


def test_normalize_report_categories_defaults_to_company():
    assert normalize_report_categories(None) == ["company"]
    assert normalize_report_categories("") == ["company"]


def test_normalize_report_categories_accepts_comma_separated_selection():
    assert normalize_report_categories("industry,economy") == ["industry", "economy"]


def test_normalize_report_categories_expands_all():
    assert normalize_report_categories("all") == ["company", "industry", "economy"]


def test_normalize_report_categories_deduplicates_preserving_order():
    assert normalize_report_categories(["economy", "company", "economy"]) == ["economy", "company"]


def test_normalize_report_categories_rejects_unknown_values():
    with pytest.raises(ValueError):
        normalize_report_categories("company,invalid")


def test_classify_report_date_skips_newer_than_requested_end():
    assert (
        classify_report_date(
            report_date=date(2026, 5, 30),
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 29),
        )
        == "skip_newer"
    )


def test_classify_report_date_processes_within_window():
    assert (
        classify_report_date(
            report_date=date(2026, 5, 29),
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
        )
        == "process"
    )


def test_classify_report_date_stops_when_older_than_window():
    assert (
        classify_report_date(
            report_date=date(2026, 4, 30),
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 31),
        )
        == "stop_older"
    )


def test_crawl_start_date_uses_target_count_safety_window():
    assert _crawl_start_date(
        end_date=date(2026, 5, 31),
        lookback_days=0,
        target_count=100,
        max_lookback_days=30,
    ) == date(2026, 5, 1)


def test_crawl_start_date_prefers_explicit_lookback_window():
    assert _crawl_start_date(
        end_date=date(2026, 5, 31),
        lookback_days=7,
        target_count=0,
        max_lookback_days=30,
    ) == date(2026, 5, 24)
