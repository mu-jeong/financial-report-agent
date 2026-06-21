from datetime import date
import os

import pytest

from src.core import data_update_jobs
from src.core.data_update_jobs import (
    build_crawler_env,
    build_update_range,
    embedding_file_progress_from_line,
    group_consecutive_dates,
    is_update_job_active,
    normalize_date_list,
    process_is_alive,
)


def test_build_update_range_starts_day_after_latest_date():
    assert build_update_range(last_date="2026-05-29", today=date(2026, 6, 3)) == (
        "2026-05-30",
        "2026-06-03",
    )


def test_build_update_range_returns_none_when_already_current():
    assert build_update_range(last_date="2026-06-03", today=date(2026, 6, 3)) is None


def test_build_crawler_env_uses_specific_date_with_inclusive_lookback():
    env = build_crawler_env(
        "2026-05-30",
        "2026-06-03",
        base_env={"OPENROUTER_API_KEY": "test"},
    )

    assert env["OPENROUTER_API_KEY"] == "test"
    assert env["CRAWLER_MODE"] == "SPECIFIC_DATE"
    assert env["CRAWLER_TARGET_DATE"] == "2026-06-03"
    assert env["CRAWLER_LOOKBACK_DAYS"] == "4"
    assert env["CRAWLER_MAX_LOOKBACK_DAYS"] == "4"
    assert env["CRAWLER_TARGET_COUNT"] == "0"


def test_build_crawler_env_rejects_reversed_range():
    with pytest.raises(ValueError):
        build_crawler_env("2026-06-03", "2026-05-30", base_env={})


def test_normalize_date_list_sorts_and_deduplicates_dates():
    assert normalize_date_list(["2026-06-03", "2026-06-01", "2026-06-03"]) == [
        "2026-06-01",
        "2026-06-03",
    ]


def test_group_consecutive_dates_keeps_non_contiguous_selected_dates_separate():
    assert group_consecutive_dates(["2026-06-03", "2026-06-01", "2026-06-02", "2026-06-05"]) == [
        ("2026-06-01", "2026-06-03"),
        ("2026-06-05", "2026-06-05"),
    ]


def test_embedding_file_progress_from_line_parses_embed_pipeline_header():
    assert embedding_file_progress_from_line("[3/10] 삼성전자 - 반도체 업황 업데이트") == (
        3,
        10,
        "삼성전자 - 반도체 업황 업데이트",
    )


def test_embedding_file_progress_from_line_ignores_non_file_progress_lines():
    assert embedding_file_progress_from_line("  [3/3] Embedding 42 chunks...") is None


def test_process_is_alive_handles_current_and_missing_pids():
    assert process_is_alive(os.getpid())
    assert not process_is_alive(None)
    assert not process_is_alive(-1)


def test_is_update_job_active_ignores_stale_running_pid(monkeypatch):
    monkeypatch.setattr(data_update_jobs, "process_is_alive", lambda pid: False)

    assert not is_update_job_active({"state": "running", "pid": 999999})


def test_is_update_job_active_keeps_running_status_without_pid_active():
    assert is_update_job_active({"state": "running"})


def test_start_update_job_passes_parent_pid_and_records_status(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 4321

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(data_update_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(data_update_jobs, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(data_update_jobs, "LOG_PATH", tmp_path / "latest.log")
    monkeypatch.setattr(data_update_jobs.os, "getpid", lambda: 1234)
    monkeypatch.setattr(data_update_jobs.subprocess, "Popen", fake_popen)

    status = data_update_jobs.start_update_job(
        label="테스트",
        selected_dates=["2026-06-03", "2026-06-05"],
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[-2:] == ["--parent-pid", "1234"]
    assert status["pid"] == 4321
    assert status["parent_pid"] == 1234
    assert data_update_jobs.read_status()["parent_pid"] == 1234
