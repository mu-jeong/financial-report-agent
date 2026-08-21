"""Background data update jobs for the Streamlit GUI.

The GUI starts this module in a separate Python process so report download and
embedding can continue without blocking normal search interactions.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from typing import Any, Callable

from src.configs import config
from src.configs.settings import BASE_DIR

JOB_DIR = BASE_DIR / "logs" / "data_update_jobs"
STATUS_PATH = JOB_DIR / "status.json"
LOG_PATH = JOB_DIR / "latest.log"

_SAFE_EMBEDDING_FAILURE_HINTS = (
    (
        "committed floor checkpoint hash does not match",
        "retrieval checkpoint metadata is out of sync",
    ),
    ("engine is currently overloaded", "embedding provider is overloaded"),
    (
        "delta source file is no longer available",
        "source PDF became unavailable during embedding",
    ),
    (
        "delta source bytes changed before activation",
        "source PDF changed during embedding",
    ),
    ("request failed: 429", "provider rate limit or capacity reached"),
    ("http 429", "provider rate limit or capacity reached"),
    ("provider unavailable", "provider unavailable"),
    ("insufficient credit", "insufficient provider credits"),
    ("rate limit", "provider rate limit reached"),
    ("status code 429", "provider rate limit reached"),
    ("timed out", "provider request timed out"),
    ("timeout", "provider request timed out"),
    ("connection", "provider connection failed"),
    ("status code 401", "provider authentication failed"),
    ("status code 403", "provider authentication failed"),
    ("authentication", "provider authentication failed"),
)


def guard_before_retrieval_write(*args, **kwargs):
    """Load native write validation only when a write is actually requested."""
    from src.retrieval.runtime_guard import (
        guard_before_retrieval_write as _guard_before_retrieval_write,
    )

    return _guard_before_retrieval_write(*args, **kwargs)


class ParentProcessExited(RuntimeError):
    """Raised when the GUI process that started an update job has exited."""


def _today() -> date:
    return date.today()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def normalize_date_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Return sorted unique ISO date strings."""
    if not values:
        return []
    parsed_dates = sorted({parse_date(value).isoformat() for value in values})
    return parsed_dates


def group_consecutive_dates(values: list[str] | tuple[str, ...]) -> list[tuple[str, str]]:
    """Group ISO dates into inclusive consecutive ranges."""
    dates = [parse_date(value) for value in normalize_date_list(values)]
    if not dates:
        return []

    ranges: list[tuple[str, str]] = []
    start = previous = dates[0]
    for current in dates[1:]:
        if current.toordinal() == previous.toordinal() + 1:
            previous = current
            continue
        ranges.append((start.isoformat(), previous.isoformat()))
        start = previous = current
    ranges.append((start.isoformat(), previous.isoformat()))
    return ranges


def normalize_update_categories(categories: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize report categories for GUI-triggered update jobs."""
    # Keep crawler code outside process initialization.  The runtime guard is
    # evaluated before the crawler process is ever launched.
    from src.core.report_crawler import normalize_report_categories

    return normalize_report_categories(categories)


def _coerce_date(value: str | date) -> date:
    return parse_date(value) if isinstance(value, str) else value


def iter_weekdays(start_date: str | date, end_date: str | date, today: date | None = None) -> list[date]:
    """Return weekdays in an inclusive range, capped at today by default."""
    start = _coerce_date(start_date)
    end = min(_coerce_date(end_date), today or _today())
    if start > end:
        start, end = end, start

    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return days


def missing_update_dates_by_category(
    start_date: str | date,
    end_date: str | date,
    date_type_counts: dict[str, dict[str, int]],
    categories: str | list[str] | tuple[str, ...],
    *,
    today: date | None = None,
) -> list[str]:
    """Return weekdays where at least one selected report category is missing.

    Existing status data is derived from embedded rows, so this intentionally
    treats a missing date/category pair as work to try. The crawler may still
    find zero reports for that category on that day.
    """
    selected_categories = normalize_update_categories(categories)
    missing_dates = []
    for day in iter_weekdays(start_date, end_date, today=today):
        counts_for_day = date_type_counts.get(day.isoformat(), {})
        if any(int(counts_for_day.get(category, 0) or 0) == 0 for category in selected_categories):
            missing_dates.append(day.isoformat())
    return normalize_date_list(missing_dates)


def build_update_range(*, last_date: str | None, today: date | None = None) -> tuple[str, str] | None:
    """Return the missing inclusive update range after the latest data date."""
    if not last_date:
        return None
    start = parse_date(last_date) if isinstance(last_date, str) else last_date
    run_today = today or _today()
    missing_start = start.toordinal() + 1
    if missing_start > run_today.toordinal():
        return None
    return date.fromordinal(missing_start).isoformat(), run_today.isoformat()


def build_crawler_env(
    start_date: str,
    end_date: str,
    base_env: dict[str, str] | None = None,
    categories: str | list[str] | tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Build env overrides for crawling an inclusive date range."""
    start = parse_date(start_date)
    end = parse_date(end_date)
    if start > end:
        raise ValueError("start_date must be earlier than or equal to end_date")
    lookback_days = (end - start).days

    env = dict(base_env or os.environ)
    env.update(
        {
            "CRAWLER_MODE": "SPECIFIC_DATE",
            "CRAWLER_TARGET_DATE": end.isoformat(),
            "CRAWLER_LOOKBACK_DAYS": str(lookback_days),
            "CRAWLER_MAX_LOOKBACK_DAYS": str(lookback_days),
            "CRAWLER_TARGET_COUNT": "0",
        }
    )
    if categories is not None:
        env["CRAWLER_CATEGORIES"] = ",".join(normalize_update_categories(categories))
    return env


def _write_status(status: dict[str, Any]) -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        **status,
    }
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_status() -> dict[str, Any] | None:
    if not STATUS_PATH.exists():
        return None
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None


def process_is_alive(pid: int | str | None) -> bool:
    """Return whether a process id appears to still be alive."""
    if pid is None:
        return False
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False

    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            process_query_limited_information = 0x1000
            handle = kernel32.OpenProcess(process_query_limited_information, False, process_id)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_update_job_active(status: dict[str, Any] | None) -> bool:
    """Return whether a persisted update-job status still represents live work."""
    if not status or status.get("state") != "running":
        return False
    pid = status.get("pid")
    if pid is None:
        return True
    return process_is_alive(pid)


def _raise_if_parent_exited(parent_pid: int | None) -> None:
    if parent_pid is not None and not process_is_alive(parent_pid):
        raise ParentProcessExited("parent Streamlit process exited")


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    try:
        os.killpg(process.pid, 15)
    except OSError:
        process.terminate()


def _start_parent_watchdog(process: subprocess.Popen[Any], parent_pid: int | None) -> None:
    if parent_pid is None:
        return

    def watch() -> None:
        while process.poll() is None:
            if not process_is_alive(parent_pid):
                _terminate_process_tree(process)
                return
            time.sleep(1)

    threading.Thread(target=watch, name="data-update-parent-watchdog", daemon=True).start()


def _popen_creation_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def build_embedding_command(
    *,
    retry_extraction_failures: bool = False,
) -> list[str]:
    """Build the embed_pipeline command for pending reports."""
    command = [sys.executable, "-m", "src.core.embed_pipeline"]
    if retry_extraction_failures:
        command.append("--retry-extraction-failures")
    return command


def start_embedding_job(
    *,
    label: str,
    retry_extraction_failures: bool = False,
) -> dict[str, Any]:
    """Start a detached embedding-only job and return the initial status."""
    guard_before_retrieval_write(
        config.DATA_ROOT,
        allow_degraded_forward_recovery=True,
        allow_empty_preflight=True,
    )
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    parent_pid = os.getpid()
    command = [sys.executable, "-m", "src.core.data_update_jobs", "embed", "--label", label]
    if retry_extraction_failures:
        command.append("--retry-extraction-failures")
    command.extend(["--parent-pid", str(parent_pid)])
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_popen_creation_kwargs(),
    )
    status = {
        "state": "running",
        "phase": "embed",
        "percent": 1,
        "message": f"{label}: 임베딩 작업을 시작했습니다.",
        "pid": process.pid,
        "label": label,
        "retry_extraction_failures": retry_extraction_failures,
        "log_path": str(LOG_PATH),
        "parent_pid": parent_pid,
    }
    _write_status(status)
    return status


def start_update_job(
    *,
    label: str,
    start_date: str | None = None,
    end_date: str | None = None,
    selected_dates: list[str] | tuple[str, ...] | None = None,
    categories: str | list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Start a detached update job and return the initial status."""
    guard_before_retrieval_write(
        config.DATA_ROOT,
        allow_empty_preflight=True,
    )
    selected_dates = normalize_date_list(selected_dates)
    selected_categories = normalize_update_categories(categories)
    if selected_dates:
        start_date = selected_dates[0]
        end_date = selected_dates[-1]
    if not start_date or not end_date:
        raise ValueError("start_date/end_date or selected_dates is required")

    JOB_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")

    command = [
        sys.executable,
        "-m",
        "src.core.data_update_jobs",
        "run",
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--label",
        label,
    ]
    if selected_dates:
        command.extend(["--dates", *selected_dates])
    command.extend(["--categories", ",".join(selected_categories)])
    parent_pid = os.getpid()
    command.extend(["--parent-pid", str(parent_pid)])
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_popen_creation_kwargs(),
    )
    status = {
        "state": "running",
        "phase": "queued",
        "percent": 1,
        "message": "데이터 업데이트 작업을 시작했습니다.",
        "pid": process.pid,
        "label": label,
        "start_date": start_date,
        "end_date": end_date,
        "selected_dates": selected_dates,
        "categories": selected_categories,
        "log_path": str(LOG_PATH),
        "parent_pid": parent_pid,
    }
    _write_status(status)
    return status


def _run_subprocess(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    parent_pid: int | None = None,
) -> tuple[int, str]:
    return _run_subprocess_stream(command, env=env, parent_pid=parent_pid)


def _run_subprocess_stream(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
    parent_pid: int | None = None,
) -> tuple[int, str]:
    _raise_if_parent_exited(parent_pid)
    run_env = dict(os.environ if env is None else env)
    run_env.setdefault("PYTHONIOENCODING", "utf-8")
    run_env.setdefault("PYTHONUTF8", "1")
    output_parts: list[str] = []
    with LOG_PATH.open("a", encoding="utf-8", errors="replace") as log_file:
        log_file.write("\n$ " + " ".join(command) + "\n")
        log_file.flush()
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            env=run_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_popen_creation_kwargs(),
        )
        _start_parent_watchdog(process, parent_pid)
        assert process.stdout is not None
        for line in process.stdout:
            output_parts.append(line)
            log_file.write(line)
            log_file.flush()
            if on_line:
                on_line(line.rstrip("\n"))
        return_code = process.wait()
        log_file.write(f"\n[exit] {return_code}\n")
        _raise_if_parent_exited(parent_pid)
        return return_code, "".join(output_parts)


def run_embedding_job(
    *,
    label: str,
    retry_extraction_failures: bool = False,
    parent_pid: int | None = None,
) -> int:
    """Run an embedding-only job and persist progress status for the GUI."""
    try:
        guard_before_retrieval_write(
            config.DATA_ROOT,
            allow_degraded_forward_recovery=True,
            allow_empty_preflight=True,
        )
        _write_status(
            {
                "state": "running",
                "phase": "embed",
                "percent": 5,
                "message": (
                    f"{label}: 문서를 처리해 검색에 반영하는 중입니다. "
                    "기존 검색은 계속 사용할 수 있습니다."
                ),
                "label": label,
                "search_available_during_update": True,
                "retry_extraction_failures": retry_extraction_failures,
                "log_path": str(LOG_PATH),
                "pid": os.getpid(),
                "parent_pid": parent_pid,
            }
        )

        def on_embed_line(line: str) -> None:
            progress = embedding_file_progress_from_line(line)
            if not progress:
                return
            current, total, file_label = progress
            total = max(total, 1)
            _write_status(
                {
                    "state": "running",
                    "phase": "embed",
                    "percent": min(98, 5 + int((current / total) * 90)),
                    "message": f"{label}: 처리 중 ({current}/{total}) {file_label}",
                    "label": label,
                    "search_available_during_update": True,
                    "log_path": str(LOG_PATH),
                    "pid": os.getpid(),
                    "embedding_current": current,
                    "embedding_total": total,
                    "embedding_file": file_label,
                    "parent_pid": parent_pid,
                }
            )

        code, output = _run_subprocess_stream(
            build_embedding_command(
                retry_extraction_failures=retry_extraction_failures,
            ),
            on_line=on_embed_line,
            parent_pid=parent_pid,
        )
        if code != 0:
            raise RuntimeError(embedding_failure_message(code, output))
        extraction_failure_count = embedding_extraction_failure_count(output)
        completion_message = (
            f"{label}: 처리는 완료했지만 파싱 실패 문서 "
            f"{extraction_failure_count}건이 관리 목록에 남았습니다."
            if extraction_failure_count
            else f"{label}: 임베딩 작업이 완료되었습니다."
        )

        _write_status(
            {
                "state": "succeeded",
                "phase": "done",
                "percent": 100,
                "message": completion_message,
                "label": label,
                "embedding_failure_count": extraction_failure_count,
                "retry_extraction_failures": retry_extraction_failures,
                "log_path": str(LOG_PATH),
                "pid": os.getpid(),
                "parent_pid": parent_pid,
            }
        )
        return 0
    except Exception as exc:
        _write_status(
            {
                "state": "failed",
                "phase": "failed",
                "percent": 100,
                "message": f"{label}: 임베딩 작업 실패 - {exc}",
                "label": label,
                "retry_extraction_failures": retry_extraction_failures,
                "log_path": str(LOG_PATH),
                "pid": os.getpid(),
                "parent_pid": parent_pid,
                "error": str(exc),
            }
        )
        return 1


def embedding_file_progress_from_line(line: str) -> tuple[int, int, str] | None:
    match = re.match(r"^\[(\d+)/(\d+)\]\s*(.+)$", line.rstrip())
    if match:
        current = int(match.group(1))
        total = int(match.group(2))
        label = match.group(3).strip()
        return current, total, label

    native = re.search(
        r"Native V2 delta publication complete: batch=(\d+).*"
        r"processed=(\d+).*deferred=(\d+)",
        line.rstrip(),
    )
    if native:
        batch = int(native.group(1))
        processed = int(native.group(2))
        deferred = int(native.group(3))
        return processed, processed + deferred, f"처리 완료 문서 반영 · {batch}차"
    if "Native V2 final compaction complete:" in line:
        return 1, 1, "검색 데이터 정리"
    return None


def embedding_failure_message(exit_code: int, output: str) -> str:
    """Return an actionable, concise error for a failed embedding subprocess."""

    if "incremental extractor differs from the active embedding profile" in output:
        return (
            "활성 V2 추출 프로필이 현재 설정과 다릅니다. "
            "tools\\recovery\\REBUILD_V2.bat --check로 확인한 뒤 tools\\recovery\\REBUILD_V2.bat을 실행하세요."
        )
    lowered = output.lower()
    detail = next(
        (
            safe_detail
            for marker, safe_detail in _SAFE_EMBEDDING_FAILURE_HINTS
            if marker in lowered
        ),
        "",
    )
    message = f"embedding failed with exit code {exit_code}"
    return f"{message}: {detail}" if detail else message


def embedding_extraction_failure_count(output: str) -> int:
    """Return the safe aggregate count emitted by native extraction handling."""

    native_matches = re.findall(
        r"Native V2 update complete:.*\bfailed=(\d+)",
        output,
    )
    if native_matches:
        return int(native_matches[-1])
    return sum(
        "Excluding PDF after primary and fallback extraction failed:" in line
        for line in output.splitlines()
    )


def run_update_job(
    *,
    start_date: str | None,
    end_date: str | None,
    label: str,
    selected_dates: list[str] | tuple[str, ...] | None = None,
    categories: str | list[str] | tuple[str, ...] | None = None,
    parent_pid: int | None = None,
) -> int:
    """Run crawler then embedding pipeline, updating status as each phase completes."""
    try:
        guard_before_retrieval_write(
            config.DATA_ROOT,
            allow_empty_preflight=True,
        )
        normalized_dates = normalize_date_list(selected_dates)
        selected_categories = normalize_update_categories(categories)
        if normalized_dates:
            start_date = normalized_dates[0]
            end_date = normalized_dates[-1]
            date_ranges = group_consecutive_dates(normalized_dates)
        else:
            if not start_date or not end_date:
                raise ValueError("start_date/end_date or selected_dates is required")
            parse_date(start_date)
            parse_date(end_date)
            date_ranges = [(start_date, end_date)]

        if not date_ranges:
            raise ValueError("No update date range was provided")

        for index, (range_start, range_end) in enumerate(date_ranges, start=1):
            percent = 10 + int(((index - 1) / max(len(date_ranges), 1)) * 50)
            _write_status(
                {
                    "state": "running",
                    "phase": "download",
                    "percent": percent,
                    "message": f"{label}: 리포트 다운로드 중... ({index}/{len(date_ranges)}: {range_start}~{range_end})",
                    "label": label,
                    "start_date": start_date,
                    "end_date": end_date,
                    "selected_dates": normalized_dates,
                    "categories": selected_categories,
                    "log_path": str(LOG_PATH),
                    "pid": os.getpid(),
                    "parent_pid": parent_pid,
                }
            )
            crawler_env = build_crawler_env(range_start, range_end, categories=selected_categories)
            code, output = _run_subprocess(
                [sys.executable, "-m", "src.core.report_crawler"],
                env=crawler_env,
                parent_pid=parent_pid,
            )
            if code != 0:
                raise RuntimeError(f"crawler failed with exit code {code}")

        _write_status(
            {
                "state": "running",
                "phase": "embed",
                "percent": 70,
                "message": (
                    f"{label}: 문서를 처리해 검색에 반영하는 중입니다. "
                    "기존 검색은 계속 사용할 수 있습니다."
                ),
                "label": label,
                "search_available_during_update": True,
                "start_date": start_date,
                "end_date": end_date,
                "selected_dates": normalized_dates,
                "categories": selected_categories,
                "log_path": str(LOG_PATH),
                "pid": os.getpid(),
                "parent_pid": parent_pid,
            }
        )

        def on_embed_line(line: str) -> None:
            progress = embedding_file_progress_from_line(line)
            if not progress:
                return
            current, total, file_label = progress
            total = max(total, 1)
            percent = min(98, 70 + int((current / total) * 28))
            _write_status(
                {
                    "state": "running",
                    "phase": "embed",
                    "percent": percent,
                    "message": f"{label}: 처리 중 ({current}/{total}) {file_label}",
                    "label": label,
                    "search_available_during_update": True,
                    "start_date": start_date,
                    "end_date": end_date,
                    "selected_dates": normalized_dates,
                    "categories": selected_categories,
                    "log_path": str(LOG_PATH),
                    "pid": os.getpid(),
                    "embedding_current": current,
                    "embedding_total": total,
                    "embedding_file": file_label,
                    "parent_pid": parent_pid,
                }
            )

        code, output = _run_subprocess_stream(
            build_embedding_command(),
            on_line=on_embed_line,
            parent_pid=parent_pid,
        )
        if code != 0:
            raise RuntimeError(embedding_failure_message(code, output))
        extraction_failure_count = embedding_extraction_failure_count(output)
        completion_message = (
            f"{label}: 업데이트는 완료했지만 파싱 실패 문서 "
            f"{extraction_failure_count}건이 관리 목록에 남았습니다."
            if extraction_failure_count
            else f"{label}: 데이터 업데이트가 완료되었습니다."
        )

        _write_status(
            {
                "state": "succeeded",
                "phase": "done",
                "percent": 100,
                "message": completion_message,
                "label": label,
                "start_date": start_date,
                "end_date": end_date,
                "selected_dates": normalized_dates,
                "categories": selected_categories,
                "embedding_failure_count": extraction_failure_count,
                "log_path": str(LOG_PATH),
                "pid": os.getpid(),
                "parent_pid": parent_pid,
            }
        )
        return 0
    except Exception as exc:
        _write_status(
            {
                "state": "failed",
                "phase": "failed",
                "percent": 100,
                "message": f"{label}: 데이터 업데이트 실패 - {exc}",
                "label": label,
                "start_date": start_date,
                "end_date": end_date,
                "selected_dates": normalize_date_list(selected_dates),
                "categories": normalize_update_categories(categories),
                "log_path": str(LOG_PATH),
                "pid": os.getpid(),
                "parent_pid": parent_pid,
                "error": str(exc),
            }
        )
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finance LLM data update job runner")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--start-date", required=True)
    run_parser.add_argument("--end-date", required=True)
    run_parser.add_argument("--label", required=True)
    run_parser.add_argument("--dates", nargs="*")
    run_parser.add_argument("--categories", default="")
    run_parser.add_argument("--parent-pid", type=int)
    embed_parser = subparsers.add_parser("embed")
    embed_parser.add_argument("--label", required=True)
    embed_parser.add_argument("--retry-extraction-failures", action="store_true")
    embed_parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)

    if args.command == "run":
        return run_update_job(
            start_date=args.start_date,
            end_date=args.end_date,
            label=args.label,
            selected_dates=args.dates,
            categories=args.categories or None,
            parent_pid=args.parent_pid,
        )
    if args.command == "embed":
        return run_embedding_job(
            label=args.label,
            retry_extraction_failures=args.retry_extraction_failures,
            parent_pid=args.parent_pid,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
