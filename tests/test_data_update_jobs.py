from datetime import date
import os

import pytest

from src.core import data_update_jobs
from src.core.data_update_jobs import (
    build_crawler_env,
    build_embedding_command,
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


def test_embedding_failure_message_points_profile_mismatches_to_rebuild_v2():
    output = (
        "NativeBuildError: incremental extractor differs from the active "
        "embedding profile: active=opendataloader|fallback=pymupdf, "
        "requested=pymupdf|fallback=opendataloader"
    )

    message = data_update_jobs.embedding_failure_message(1, output)

    assert "활성 V2 추출 프로필" in message
    assert "REBUILD_V2.bat" in message
    assert "exit code 1" not in message


def test_embedding_failure_message_keeps_a_concise_error_detail():
    output = "setup\n2026-07-25 [ERROR] embed_pipeline.py: provider unavailable\n"

    message = data_update_jobs.embedding_failure_message(7, output)

    assert "exit code 7" in message
    assert "provider unavailable" in message


def test_embedding_failure_message_redacts_credentials_from_subprocess_output():
    output = (
        "2026-07-25 [ERROR] request failed: "
        "{'api_key': 'top-secret'} OPENROUTER_API_KEY=another-secret "
        "Authorization: Bearer sk-or-v1-secret-token"
    )

    message = data_update_jobs.embedding_failure_message(1, output)

    assert message == "embedding failed with exit code 1"
    assert "top-secret" not in message
    assert "another-secret" not in message
    assert "secret-token" not in message


def test_embedding_extraction_failure_count_reads_safe_v1_and_v2_summaries():
    assert data_update_jobs.embedding_extraction_failure_count(
        "Quick Start is continuing after 3 PDF parsing failure(s)."
    ) == 3
    assert data_update_jobs.embedding_extraction_failure_count(
        "Excluding PDF after primary and fallback extraction failed: a.pdf\n"
        "Excluding PDF after primary and fallback extraction failed: b.pdf\n"
    ) == 2


def test_embedding_job_surfaces_partial_extraction_completion(monkeypatch):
    statuses: list[dict[str, object]] = []
    monkeypatch.setattr(
        data_update_jobs,
        "guard_before_retrieval_write",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        data_update_jobs,
        "_run_subprocess_stream",
        lambda *_args, **_kwargs: (
            0,
            "Quick Start is continuing after 2 PDF parsing failure(s).",
        ),
    )
    monkeypatch.setattr(
        data_update_jobs,
        "_write_status",
        lambda status: statuses.append(status),
    )

    assert data_update_jobs.run_embedding_job(label="재처리") == 0
    assert statuses[-1]["state"] == "succeeded"
    assert statuses[-1]["embedding_failure_count"] == 2
    assert "관리 목록에 남았습니다" in str(statuses[-1]["message"])


def test_build_embedding_command_uses_all_or_limit():
    assert build_embedding_command(limit=None)[-1] == "--all"
    assert build_embedding_command(limit=3)[-2:] == ["--limit", "3"]
    assert build_embedding_command(
        limit=None,
        continue_on_extraction_error=True,
        retry_extraction_failures=True,
    )[-3:] == [
        "--all",
        "--continue-on-extraction-error",
        "--retry-extraction-failures",
    ]


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


def test_start_embedding_job_records_limit_and_parent_pid(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 9876

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(data_update_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(data_update_jobs, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(data_update_jobs, "LOG_PATH", tmp_path / "latest.log")
    monkeypatch.setattr(data_update_jobs.os, "getpid", lambda: 1234)
    monkeypatch.setattr(data_update_jobs.subprocess, "Popen", fake_popen)

    status = data_update_jobs.start_embedding_job(label="미임베딩 문서 3건", limit=3)

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:3] == [data_update_jobs.sys.executable, "-m", "src.core.data_update_jobs"]
    assert command[-4:] == ["--limit", "3", "--parent-pid", "1234"]
    assert status["phase"] == "embed"
    assert status["embedding_limit"] == 3
    assert status["pid"] == 9876
    assert data_update_jobs.read_status()["parent_pid"] == 1234


def test_start_embedding_job_forwards_explicit_native_failure_retry(
    monkeypatch,
    tmp_path,
):
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 9876

    def fake_popen(command, **_kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(data_update_jobs, "JOB_DIR", tmp_path)
    monkeypatch.setattr(data_update_jobs, "STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(data_update_jobs, "LOG_PATH", tmp_path / "latest.log")
    monkeypatch.setattr(data_update_jobs.os, "getpid", lambda: 1234)
    monkeypatch.setattr(data_update_jobs.subprocess, "Popen", fake_popen)

    status = data_update_jobs.start_embedding_job(
        label="파싱 실패 문서 재시도",
        retry_extraction_failures=True,
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert "--retry-extraction-failures" in command
    assert status["retry_extraction_failures"] is True
