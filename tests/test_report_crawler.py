import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from src.core import report_crawler

from src.core.report_crawler import (
    _crawl_start_date,
    classify_report_date,
    download_naver_reports,
    normalize_report_categories,
)
from src.retrieval.build_service import materialize_candidate, publish_candidate
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from tests.retrieval.test_retrieval_build_service import (
    DeterministicEmbeddings,
    _native_seed,
    _prepare,
)


def test_download_holds_cutover_fence_for_guard_and_source_writes(
    tmp_path,
    monkeypatch,
):
    events = []
    data_root = tmp_path / "data"
    data_root.mkdir()

    class FakeUpdateLock:
        def __init__(self, observed_root):
            assert Path(observed_root) == data_root

        def __enter__(self):
            events.append("locked")
            return self

        def __exit__(self, *_args):
            events.append("unlocked")

    def guarded():
        assert events == ["locked"]
        events.append("guarded")

    def download(*_args, **_kwargs):
        assert events == ["locked", "guarded"]
        events.append("downloaded")
        return 3

    monkeypatch.setattr("src.configs.config.DB_PATH", str(data_root / "reports.db"))
    monkeypatch.setattr(report_crawler, "RetrievalUpdateLock", FakeUpdateLock)
    monkeypatch.setattr(report_crawler, "guard_before_report_download", guarded)
    monkeypatch.setattr(report_crawler, "_download_naver_reports_locked", download)

    assert report_crawler.download_naver_reports("2026-07-18") == 3
    assert events == ["locked", "guarded", "downloaded", "unlocked"]


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


def test_download_guard_blocks_before_crawler_dependencies_or_source_writes(
    tmp_path,
    monkeypatch,
):
    events = []
    data_root = tmp_path / "data"
    data_root.mkdir()

    def blocked_guard():
        events.append("guard")
        raise RuntimeError("write runtime blocked")

    monkeypatch.setattr(
        "src.core.report_crawler.guard_before_report_download",
        blocked_guard,
    )
    monkeypatch.setattr(
        "src.configs.config.DB_PATH",
        str(data_root / "reports.db"),
    )
    monkeypatch.setattr(
        "os.makedirs",
        lambda *_args, **_kwargs: events.append("mkdir"),
    )

    with pytest.raises(RuntimeError, match="write runtime blocked"):
        download_naver_reports("2026-07-16")

    assert events == ["guard"]


def test_direct_crawler_command_fails_before_source_mutation_when_degraded(tmp_path):
    native_fixture = tmp_path / "native"
    native_fixture.mkdir()
    data_root, sources = _native_seed(native_fixture)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())
    candidate = materialize_candidate(plan, data_root)
    publish_candidate(candidate, data_root)
    active_snapshot = data_root.joinpath(
        *candidate.snapshot_relative_path.split("/")
    )
    active_snapshot.write_bytes(b"corrupt-active-snapshot")
    recovery = StartupReconciler(data_root).reconcile()
    assert recovery.disposition == RecoveryDisposition.PREDECESSOR_DEGRADED
    save_dir = tmp_path / "must-not-be-created"
    environment = os.environ.copy()
    environment["DB_PATH"] = str(data_root / "reports.db")
    environment["SAVE_DIR"] = str(save_dir)
    root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [sys.executable, "-m", "src.core.report_crawler"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode != 0
    assert "RetrievalWriteBlocked" in completed.stderr
    assert not save_dir.exists()
