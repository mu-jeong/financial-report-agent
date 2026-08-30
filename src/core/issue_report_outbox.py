"""Durable, non-blocking delivery of user-approved issue reports.

Only the bounded remote envelope is retained in SQLite while delivery is
pending. Successful, rejected, and expired events are removed immediately.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests

from src.core import artifact_io
from src.core.feedback_handoff import redact_handoff_text

LOGGER = logging.getLogger(__name__)

OUTBOX_SCHEMA_VERSION = 1
INGEST_CONTRACT_VERSION = 1
MAX_EVENT_BYTES = 128 * 1024
MAX_OUTBOX_BYTES = 50 * 1024 * 1024
MAX_OUTBOX_EVENTS = 1_000
MAX_EVENT_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_DELIVERY_RETRIES = 3
MAX_DELIVERY_ATTEMPTS = 1 + MAX_DELIVERY_RETRIES
DELIVERY_LEASE_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10.0

_ALLOWED_CATEGORIES = {
    "일반 답변 품질",
    "검색 정확도 이슈",
    "오답/오류",
    "속도",
    "버그/기능",
    "기타",
}
_ALLOWED_KINDS = {"user_feedback", "system_error"}
_ALLOWED_TARGET_TYPES = {"response", "ui_or_system"}
_ALLOWED_SOURCES = {"local_chat", "chat_monitoring_trace", "system"}
_RESULT_COUNT_KINDS = {"document", "row", "source"}
_REMOTE_TURN_TRACE_LIMIT = 8
_REMOTE_TURN_TRACE_BYTES = 48 * 1024
_CASE_DIAGNOSTIC_PRIOR_TURN_LIMIT = 8
_CASE_DIAGNOSTIC_RETRIEVAL_LIMIT = 20
_CASE_DIAGNOSTIC_EVIDENCE_LIMIT = 20
_REMOTE_FILTER_BYTES = 2 * 1024
_REMOTE_FILTER_KEYS = {
    "broker",
    "brokers",
    "file_names",
    "report_date",
    "report_date_end",
    "report_date_start",
    "report_month",
    "report_type",
    "report_types",
    "target_name",
    "target_names",
}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIAGNOSTIC_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$")
_UNSAFE_DIAGNOSTIC_FILE_RE = re.compile(
    r"(?i)(?:^|[\\/])[^\\/]+\.(?:db|sqlite|sqlite3|faiss|zip|tar|tgz|gz|7z|rar)$"
)
_BASE64_BINARY_RE = re.compile(r"^(?:[A-Za-z0-9+/]{4}){32,}={0,2}$")
_OUTBOUND_REDACTIONS = (
    (
        "credential",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])"
            r"(?:[A-Za-z][A-Za-z0-9_-]*[_-])?"
            r"(?:api[_-]?keys?|access[_-]?keys?|access[_-]?tokens?|"
            r"auth[_-]?tokens?|tokens?|credentials?|client[_-]?secrets?|"
            r"private[_-]?keys?|signing[_-]?keys?|encryption[_-]?keys?|"
            r"passwords?|passwd|secrets?|cookies?|sessions?|"
            r"webhook[_-]?urls?|database[_-]?urls?|"
            r"connection[_-]?strings?|dsn)"
            r"\s*[:=]\s*[^\s&#]+"
        ),
    ),
    (
        "credential",
        re.compile(r"(?i)\bsb_secret_[A-Za-z0-9._-]{16,}\b"),
    ),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:gh[pousr]_[A-Za-z0-9]{20,255}"
            r"|github_pat_[A-Za-z0-9_]{20,255})\b"
        ),
    ),
    (
        "credential",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "credential",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\."
            r"[A-Za-z0-9_-]{5,}\b"
        ),
    ),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:xox[baprs]-[A-Za-z0-9-]{10,}"
            r"|AIza[0-9A-Za-z_-]{20,}"
            r"|(?:sk|rk)_live_[A-Za-z0-9]{16,})\b"
        ),
    ),
    (
        "credential",
        re.compile(
            r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
            r"-----END [A-Z ]*PRIVATE KEY-----"
        ),
    ),
    (
        "resident_id",
        re.compile(r"(?<!\d)\d{6}[- ]?[1-4]\d{6}(?!\d)"),
    ),
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outbox_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL UNIQUE,
    payload_json TEXT,
    payload_bytes INTEGER NOT NULL CHECK (payload_bytes >= 0),
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'sending', 'retry', 'delivered',
            'rejected', 'dead_letter', 'expired'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at REAL NOT NULL,
    lease_owner TEXT,
    lease_expires_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_error_code TEXT,
    receipt_id TEXT,
    delivered_at REAL
);

CREATE INDEX IF NOT EXISTS outbox_events_delivery_idx
    ON outbox_events (status, available_at, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS outbox_events_lease_idx
    ON outbox_events (status, lease_expires_at);
"""

_worker_lock = threading.Lock()
_worker_wake = threading.Event()
_worker_thread: threading.Thread | None = None


class IssueReportOutboxError(RuntimeError):
    """Raised when a remote envelope cannot safely enter the outbox."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        super().__init__(code if detail is None else f"{code}: {detail}")


@dataclass(frozen=True)
class DeliveryConfig:
    enabled: bool
    ingest_url: str | None
    publishable_key: str | None
    outbox_dir: Path

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.ingest_url
            and self.publishable_key
            and _valid_ingest_url(self.ingest_url)
            and self.publishable_key.startswith("sb_publishable_")
        )

    @property
    def database_path(self) -> Path:
        return self.outbox_dir / "issue-report-outbox.sqlite3"


@dataclass(frozen=True)
class DeliveryOutcome:
    action: str
    code: str
    retry_after_seconds: float | None = None
    receipt_id: str | None = None


def load_delivery_config() -> DeliveryConfig:
    """Read the current runtime config without persisting credentials."""

    from src.configs import config as config_module

    configured_dir = config_module.ISSUE_REPORT_OUTBOX_DIR
    outbox_dir = (
        Path(configured_dir).expanduser()
        if configured_dir
        else Path(config_module.DATA_ROOT).expanduser() / "issue-report-outbox"
    )
    return DeliveryConfig(
        enabled=bool(config_module.ISSUE_REPORT_REMOTE_ENABLED),
        ingest_url=_optional_text(config_module.ISSUE_REPORT_INGEST_URL),
        publishable_key=_optional_text(
            config_module.ISSUE_REPORT_PUBLISHABLE_KEY
        ),
        outbox_dir=outbox_dir,
    )


def remote_delivery_available(config: DeliveryConfig | None = None) -> bool:
    return (config or load_delivery_config()).configured


def build_remote_report(
    report: Mapping[str, Any],
    *,
    consent: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the exact, bounded report subset accepted by ingest contract v1."""

    context = _mapping(report.get("context"))
    observed = _mapping(report.get("observed"))
    diagnostics = _mapping(report.get("diagnostics"))
    selected = _mapping(context.get("selected_message"))
    selected_metadata = _mapping(selected.get("metadata"))
    trace = _mapping(context.get("trace_detail"))
    trace_timing = _mapping(trace.get("timing"))
    trace_answer = _mapping(trace.get("answer"))
    trace_routing = _mapping(trace.get("routing"))
    remote_consent = dict(_mapping(context.get("remote_consent")))
    if consent is not None:
        remote_consent.update(dict(consent))
    consent_version = remote_consent.get("consent_version")
    consent_is_valid = type(consent_version) is int and consent_version == 1

    def explicitly_allowed(name: str) -> bool:
        return consent_is_valid and remote_consent.get(name) is True

    removed_fields: set[str] = {
        "context",
        "created_at",
        "id",
        "job_id",
        "message_id",
        "thread_id",
    }
    if context.get("conversation_messages") or context.get("recent_messages"):
        removed_fields.add("context.conversation_messages")
    if trace:
        removed_fields.add("context.trace_detail")

    comment = ""
    if explicitly_allowed("include_comment"):
        comment = _redact_and_bound(
            report.get("comment")
            if report.get("comment") is not None
            else report.get("description"),
            maximum=4 * 1024,
            field="comment",
            removed_fields=removed_fields,
        )
    else:
        removed_fields.add("comment")

    question: str | None = None
    if explicitly_allowed("include_selected_question"):
        raw_question = (
            context.get("selected_user_question")
            or observed.get("user_question")
            or _mapping(context.get("reproduction_input")).get("question")
        )
        bounded_question = _redact_and_bound(
            raw_question,
            maximum=16 * 1024,
            field="observed.selected_question",
            removed_fields=removed_fields,
        )
        if bounded_question:
            question = bounded_question
    else:
        removed_fields.add("observed.selected_question")

    answer: str | None = None
    if explicitly_allowed("include_selected_answer"):
        raw_answer = (
            selected.get("content_preview")
            or trace_answer.get("assistant_preview")
            or observed.get("assistant_response_preview")
        )
        bounded_answer = _redact_and_bound(
            raw_answer,
            maximum=16 * 1024,
            field="observed.selected_answer",
            removed_fields=removed_fields,
        )
        if bounded_answer:
            answer = bounded_answer
    else:
        removed_fields.add("observed.selected_answer")

    question, answer = _bound_selected_content(question, answer)
    turn_trace = (
        _compact_remote_turn_trace(
            context.get("turn_trace"),
            removed_fields=removed_fields,
        )
        if explicitly_allowed("include_previous_turns") and question is not None
        else []
    )
    if not turn_trace and context.get("turn_trace"):
        removed_fields.add("context.turn_trace")
    hints = _remote_debug_hints(
        diagnostics.get("debug_hints") or context.get("debug_hints") or [],
        removed_fields=removed_fields,
    )

    route = _nullable_token(
        trace_routing.get("route")
        or observed.get("route")
        or _mapping(observed.get("actual")).get("route")
        or selected_metadata.get("route")
    )
    status = _nullable_token(
        observed.get("status") or selected_metadata.get("status")
    )
    latency_ms = _latency_ms(
        trace_timing.get("total_seconds") or observed.get("latency")
    )
    result_count = _bounded_count(
        trace_answer.get("result_count"), maximum=1_000_000
    )
    if result_count is None:
        result_count = _bounded_count(
            trace_answer.get("source_count"), maximum=1_000_000
        )
    raw_result_count_kind = trace_answer.get("result_count_kind")
    result_count_kind = (
        str(raw_result_count_kind)
        if raw_result_count_kind in _RESULT_COUNT_KINDS
        else "document" if route == "vectordb" else "source"
    )
    citation_ranks = trace_answer.get("citation_ranks_used")
    citation_count = (
        _bounded_count(len(citation_ranks), maximum=1_000_000)
        if isinstance(citation_ranks, list)
        else None
    )

    source = str(report.get("source") or "")
    source = (
        source
        if source in _ALLOWED_SOURCES
        else {
            "streamlit_chat": "local_chat",
            "email_import": "local_chat",
        }.get(source, "local_chat")
    )
    kind = str(report.get("kind") or "user_feedback")
    if kind not in _ALLOWED_KINDS:
        kind = "user_feedback"
    target_type = str(report.get("report_target_type") or "ui_or_system")
    if target_type not in _ALLOWED_TARGET_TYPES:
        target_type = "ui_or_system"
    category = str(report.get("category") or "기타")
    if category not in _ALLOWED_CATEGORIES:
        category = "기타"

    app_version = str(report.get("app_version") or "unknown")
    if _VERSION_RE.fullmatch(app_version) is None:
        app_version = "unknown"

    stack_hash = diagnostics.get("stack_hash") or diagnostics.get(
        "error_signature"
    )
    stack_hash = (
        str(stack_hash)
        if isinstance(stack_hash, str) and _SHA256_RE.fullmatch(stack_hash)
        else None
    )

    diagnostic_consent = explicitly_allowed("include_previous_turns")
    case_diagnostics = (
        _build_case_diagnostics(
            context=context,
            trace=trace,
            selected_metadata=selected_metadata,
            include_prior_turns=True,
            removed_fields=removed_fields,
        )
        if diagnostic_consent and question is not None
        else None
    )
    if case_diagnostics is None:
        removed_fields.add("case_diagnostics")
    normalized_release_version = app_version.removeprefix("v")
    reported_release_id = (
        f"release-v{normalized_release_version}"
        if normalized_release_version != "unknown"
        else "release-unknown"
    )

    remote = {
        "schema_version": 3,
        "report_contract_version": 3,
        "kind": kind,
        "report_target_type": target_type,
        "source": source,
        "app_version": app_version,
        "reported_release_id": reported_release_id,
        "category": category,
        "comment": comment,
        "consent": {
            "consent_version": 1,
            "include_comment": bool(comment),
            "include_selected_question": question is not None,
            "include_selected_answer": answer is not None,
            "include_previous_turns": bool(turn_trace) or case_diagnostics is not None,
        },
        "observed": {
            "route": route,
            "status": status,
            "latency_ms": latency_ms,
            "result_count": result_count,
            "result_count_kind": result_count_kind,
            "citation_count": citation_count,
            "selected_question": question,
            "selected_answer": answer,
            "turn_trace": turn_trace,
        },
        "diagnostics": {
            "stable_error_code": _nullable_token(
                diagnostics.get("stable_error_code")
                or diagnostics.get("error_code")
            ),
            "exception_type": _nullable_token(
                diagnostics.get("exception_type")
                or diagnostics.get("error_type")
            ),
            "stack_hash": stack_hash,
            "debug_hints": hints,
        },
        "privacy": {
            "redaction_version": 1,
            "removed_fields": sorted(removed_fields)[:16],
        },
    }
    if case_diagnostics is not None:
        remote["case_diagnostics"] = case_diagnostics
    serialized = json.dumps(
        remote,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if _contains_outbound_sensitive_pattern(serialized):
        raise IssueReportOutboxError("unredacted_sensitive_data")
    return remote


def queue_report(
    report: Mapping[str, Any],
    *,
    consent: Mapping[str, Any] | None = None,
    config: DeliveryConfig | None = None,
    start_worker: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """Queue an in-memory report without creating a local report artifact."""

    try:
        resolved = config or load_delivery_config()
        if not resolved.enabled:
            return {"status": "unavailable", "reason": "remote_disabled"}
        if not resolved.configured:
            return {
                "status": "unavailable",
                "reason": "remote_not_configured",
            }
        result = enqueue_report(
            report,
            consent=consent,
            database_path=resolved.database_path,
            now=now,
        )
    except Exception as exc:  # Delivery cannot escape into the app thread.
        code = getattr(exc, "code", "outbox_write_failed")
        LOGGER.warning("Issue report remote queue failed: %s", code)
        return {"status": "queue_failed", "reason": str(code)}

    if start_worker:
        try:
            worker_started = start_delivery_worker(resolved)
        except Exception as exc:
            LOGGER.warning(
                "Issue report delivery worker could not start: %s",
                type(exc).__name__,
            )
            worker_started = False
        if not worker_started:
            result = {**result, "reason": "worker_not_started"}
    return result


def queue_saved_report(
    json_path: str | Path,
    *,
    consent: Mapping[str, Any] | None = None,
    config: DeliveryConfig | None = None,
    start_worker: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """Compatibility path for queuing an existing legacy report artifact."""

    try:
        resolved = config or load_delivery_config()
        if not resolved.enabled:
            return {"status": "local_only", "reason": "remote_disabled"}
        if not resolved.configured:
            return {"status": "local_only", "reason": "remote_not_configured"}

        from src.core import issue_report_store

        report = issue_report_store.load_report(json_path)
        return queue_report(
            report,
            consent=consent,
            config=resolved,
            start_worker=start_worker,
            now=now,
        )
    except Exception as exc:  # Remote delivery cannot invalidate local success.
        code = getattr(exc, "code", "outbox_write_failed")
        LOGGER.warning("Issue report remote queue failed: %s", code)
        return {"status": "queue_failed", "reason": str(code)}


def enqueue_report(
    report: Mapping[str, Any],
    *,
    consent: Mapping[str, Any] | None = None,
    database_path: str | Path,
    now: float | None = None,
) -> dict[str, Any]:
    """Insert one idempotent event into the durable SQLite outbox."""

    current_time = time.time() if now is None else float(now)
    report_id = str(_bounded_identifier(report.get("id"), required=True))
    remote_report = build_remote_report(report, consent=consent)
    database = Path(database_path)

    with closing(_connect(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        expired_count = _expire_events(connection, current_time)
        existing = connection.execute(
            """
            SELECT event_id, status, receipt_id
            FROM outbox_events
            WHERE report_id = ?
            """,
            (report_id,),
        ).fetchone()
        if existing is not None:
            connection.commit()
            if expired_count:
                _truncate_wal(connection)
            return {
                "status": str(existing["status"]),
                "event_id": str(existing["event_id"]),
                "receipt_id": existing["receipt_id"],
            }

        event_id = str(uuid.uuid4())
        installation_id = _installation_id(connection)
        envelope = {
            "ingest_contract_version": INGEST_CONTRACT_VERSION,
            "event_id": event_id,
            "installation_id": installation_id,
            "queued_at": _rfc3339(current_time),
            "report": remote_report,
        }
        payload_json = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload_bytes = len(payload_json.encode("utf-8"))
        if payload_bytes > MAX_EVENT_BYTES:
            raise IssueReportOutboxError("payload_too_large")

        active = connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(payload_bytes), 0)
            FROM outbox_events
            WHERE payload_json IS NOT NULL
              AND status IN ('queued', 'sending', 'retry')
            """
        ).fetchone()
        if (
            int(active[0]) >= MAX_OUTBOX_EVENTS
            or int(active[1]) + payload_bytes > MAX_OUTBOX_BYTES
        ):
            raise IssueReportOutboxError("outbox_full")

        connection.execute(
            """
            INSERT INTO outbox_events (
                event_id, report_id, payload_json, payload_bytes, priority,
                status, attempt_count, available_at, created_at, updated_at,
                expires_at
            ) VALUES (?, ?, ?, ?, 100, 'queued', 0, ?, ?, ?, ?)
            """,
            (
                event_id,
                report_id,
                payload_json,
                payload_bytes,
                current_time,
                current_time,
                current_time,
                current_time + MAX_EVENT_AGE_SECONDS,
            ),
        )
        connection.commit()
        if expired_count:
            _truncate_wal(connection)
    return {"status": "queued", "event_id": event_id, "receipt_id": None}


def process_outbox_once(
    config: DeliveryConfig,
    *,
    now: float | None = None,
    post: Callable[..., Any] = requests.post,
    jitter: Callable[[float, float], float] = random.uniform,
) -> dict[str, Any] | None:
    """Lease and deliver at most one due event; useful for workers and tests."""

    if not config.configured:
        return None
    current_time = time.time() if now is None else float(now)
    lease_owner = uuid.uuid4().hex
    event = _lease_next_event(
        config.database_path,
        lease_owner=lease_owner,
        now=current_time,
    )
    if event is None:
        return None

    try:
        outcome = _post_event(event, config, post=post, now=current_time)
    except Exception as exc:  # Delivery must never escape into the app thread.
        LOGGER.warning(
            "Issue report delivery failed for event %s: %s",
            event["event_id"],
            type(exc).__name__,
        )
        outcome = DeliveryOutcome("retry", "client_delivery_error")

    attempt_count = int(event["attempt_count"])
    retry_allowed = (
        outcome.action == "retry"
        and attempt_count < MAX_DELIVERY_ATTEMPTS
    )
    if retry_allowed:
        retry_after = outcome.retry_after_seconds
        if retry_after is None:
            retry_after = _backoff_seconds(attempt_count, jitter=jitter)
        available_at = current_time + max(1.0, min(retry_after, 86_400.0))
    else:
        available_at = current_time
    final_status = _record_outcome(
        config.database_path,
        event_id=str(event["event_id"]),
        lease_owner=lease_owner,
        outcome=outcome,
        attempt_count=attempt_count,
        available_at=available_at,
        now=current_time,
    )
    return {
        "event_id": str(event["event_id"]),
        "status": final_status,
        "code": outcome.code,
        "receipt_id": outcome.receipt_id,
    }


def start_delivery_worker(config: DeliveryConfig | None = None) -> bool:
    """Start or wake the process-wide daemon worker when remote delivery is ready."""

    try:
        resolved = config or load_delivery_config()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        LOGGER.warning(
            "Issue report delivery is not configured: %s",
            type(exc).__name__,
        )
        return False
    if not resolved.configured:
        return False
    global _worker_thread
    try:
        with _worker_lock:
            if _worker_thread is None or not _worker_thread.is_alive():
                _worker_thread = threading.Thread(
                    target=_delivery_worker_loop,
                    name="issue-report-outbox",
                    daemon=True,
                )
                _worker_thread.start()
            _worker_wake.set()
    except Exception as exc:
        LOGGER.warning(
            "Issue report delivery worker could not start: %s",
            type(exc).__name__,
        )
        return False
    return True


def outbox_status(
    database_path: str | Path,
    *,
    report_id: str | None = None,
) -> dict[str, Any]:
    """Return bounded delivery state without exposing payload contents."""

    with closing(_connect(Path(database_path))) as connection:
        if report_id is not None:
            row = connection.execute(
                """
                SELECT event_id, report_id, status, attempt_count,
                       last_error_code, receipt_id
                FROM outbox_events
                WHERE report_id = ?
                """,
                (report_id,),
            ).fetchone()
            return dict(row) if row is not None else {}
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM outbox_events
            GROUP BY status
            """
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}


def _delivery_worker_loop() -> None:
    while True:
        try:
            config = load_delivery_config()
            if not config.configured:
                return
            processed = process_outbox_once(config)
            if processed is not None:
                continue
        except Exception as exc:
            LOGGER.warning(
                "Issue report outbox worker paused after %s",
                type(exc).__name__,
            )
        _worker_wake.clear()
        _worker_wake.wait(60.0)


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    existed = database_path.exists()
    connection = sqlite3.connect(database_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA secure_delete = ON")
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, OUTBOX_SCHEMA_VERSION}:
        connection.close()
        raise IssueReportOutboxError("unsupported_outbox_schema")
    connection.executescript(_SCHEMA_SQL)
    if version == 0:
        connection.execute(f"PRAGMA user_version = {OUTBOX_SCHEMA_VERSION}")
    removed = connection.execute(
        """
        DELETE FROM outbox_events
        WHERE payload_json IS NULL
           OR status IN ('delivered', 'rejected', 'dead_letter', 'expired')
        """
    ).rowcount
    connection.commit()
    if removed:
        _truncate_wal(connection)
    if not existed:
        try:
            os.chmod(database_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return connection


def _installation_id(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT value FROM outbox_meta WHERE key = 'installation_id'"
    ).fetchone()
    if row is not None:
        try:
            return str(uuid.UUID(str(row[0])))
        except ValueError as exc:
            raise IssueReportOutboxError("invalid_installation_id") from exc
    installation_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO outbox_meta (key, value) VALUES ('installation_id', ?)",
        (installation_id,),
    )
    return installation_id


def _expire_events(connection: sqlite3.Connection, now: float) -> int:
    return connection.execute(
        """
        DELETE FROM outbox_events
        WHERE payload_json IS NOT NULL
          AND expires_at <= ?
          AND status IN ('queued', 'sending', 'retry')
        """,
        (now,),
    ).rowcount


def _lease_next_event(
    database_path: Path,
    *,
    lease_owner: str,
    now: float,
) -> sqlite3.Row | None:
    with closing(_connect(database_path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        expired_count = _expire_events(connection, now)
        row = connection.execute(
            """
            SELECT *
            FROM outbox_events
            WHERE payload_json IS NOT NULL
              AND (
                    (status IN ('queued', 'retry') AND available_at <= ?)
                 OR (status = 'sending' AND lease_expires_at <= ?)
              )
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """,
            (now, now),
        ).fetchone()
        if row is None:
            connection.commit()
            if expired_count:
                _truncate_wal(connection)
            return None
        connection.execute(
            """
            UPDATE outbox_events
            SET status = 'sending', attempt_count = attempt_count + 1,
                lease_owner = ?, lease_expires_at = ?, updated_at = ?
            WHERE event_id = ?
            """,
            (
                lease_owner,
                now + DELIVERY_LEASE_SECONDS,
                now,
                row["event_id"],
            ),
        )
        leased = connection.execute(
            "SELECT * FROM outbox_events WHERE event_id = ?",
            (row["event_id"],),
        ).fetchone()
        connection.commit()
        if expired_count:
            _truncate_wal(connection)
        return leased


def _post_event(
    event: Mapping[str, Any],
    config: DeliveryConfig,
    *,
    post: Callable[..., Any],
    now: float,
) -> DeliveryOutcome:
    try:
        response = post(
            str(config.ingest_url),
            data=str(event["payload_json"]).encode("utf-8"),
            headers={
                "apikey": str(config.publishable_key),
                "content-type": "application/json",
                "accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        return DeliveryOutcome("retry", "network_error")

    status = int(response.status_code)
    body = _bounded_response_json(response)
    if status == 200:
        if (
            body.get("ok") is True
            and body.get("disposition") in {"accepted", "duplicate"}
            and _is_uuid(body.get("receipt_id"))
        ):
            return DeliveryOutcome(
                "ack",
                str(body["disposition"]),
                receipt_id=str(body["receipt_id"]),
            )
        return DeliveryOutcome("retry", "invalid_success_response")

    code = _nullable_token(body.get("code")) or f"http_{status}"
    if status in {408, 425, 429} or 500 <= status <= 599:
        retry_after = (
            _retry_after_seconds(response.headers.get("Retry-After"), now=now)
            if status == 429
            else None
        )
        return DeliveryOutcome("retry", code, retry_after_seconds=retry_after)
    if 400 <= status <= 499:
        return DeliveryOutcome("reject", code)
    return DeliveryOutcome("retry", code)


def _record_outcome(
    database_path: Path,
    *,
    event_id: str,
    lease_owner: str,
    outcome: DeliveryOutcome,
    attempt_count: int,
    available_at: float,
    now: float,
) -> str:
    if outcome.action == "ack":
        status = "delivered"
    elif outcome.action == "reject":
        status = "rejected"
    elif attempt_count >= MAX_DELIVERY_ATTEMPTS:
        status = "dead_letter"
    else:
        status = "retry"
    terminal = status in {"delivered", "rejected", "dead_letter"}

    with closing(_connect(database_path)) as connection:
        if terminal:
            connection.execute(
                """
                DELETE FROM outbox_events
                WHERE event_id = ? AND status = 'sending' AND lease_owner = ?
                """,
                (event_id, lease_owner),
            )
        else:
            connection.execute(
                """
                UPDATE outbox_events
                SET status = ?, available_at = ?, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?,
                    last_error_code = ?, receipt_id = ?
                WHERE event_id = ? AND status = 'sending' AND lease_owner = ?
                """,
                (
                    status,
                    available_at,
                    now,
                    outcome.code,
                    outcome.receipt_id,
                    event_id,
                    lease_owner,
                ),
            )
        connection.commit()
        if terminal:
            _truncate_wal(connection)
    return status


def _truncate_wal(connection: sqlite3.Connection) -> None:
    """Best-effort removal of deleted payload remnants from the WAL."""

    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error:
        LOGGER.debug("Issue report outbox WAL checkpoint was busy")


def _bounded_response_json(response: Any) -> dict[str, Any]:
    try:
        content = bytes(response.content)
    except (AttributeError, TypeError, ValueError):
        content = b""
    if len(content) > 64 * 1024:
        return {}
    try:
        value = response.json()
    except (ValueError, AttributeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _backoff_seconds(
    attempt_count: int,
    *,
    jitter: Callable[[float, float], float],
) -> float:
    base = min(30.0 * (2 ** max(0, attempt_count - 1)), 21_600.0)
    return base + jitter(0.0, min(30.0, base * 0.2))


def _retry_after_seconds(value: Any, *, now: float) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = parsed.timestamp() - now
    if seconds <= 0:
        return 1.0
    return min(seconds, 86_400.0)


def _remote_debug_hints(
    value: Any,
    *,
    removed_fields: set[str],
) -> list[str]:
    if not isinstance(value, list):
        return []
    hints: list[str] = []
    total = 0
    for item in value[:8]:
        hint = _redact_and_bound(
            item,
            maximum=512,
            field="diagnostics.debug_hints",
            removed_fields=removed_fields,
        )
        encoded = hint.encode("utf-8")
        if total + len(encoded) > 4096:
            break
        hints.append(hint)
        total += len(encoded)
    return hints


def _build_case_diagnostics(
    *,
    context: Mapping[str, Any],
    trace: Mapping[str, Any],
    selected_metadata: Mapping[str, Any],
    include_prior_turns: bool,
    removed_fields: set[str],
) -> dict[str, Any] | None:
    truncated = False
    prior_turns: list[dict[str, str]] = []
    raw_messages = context.get("conversation_messages") or context.get(
        "recent_messages"
    )
    if include_prior_turns and isinstance(raw_messages, list):
        user_messages: list[Mapping[str, Any]] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, Mapping):
                truncated = True
                continue
            role = str(raw_message.get("role") or "").lower()
            if role == "assistant":
                continue
            if role != "user":
                truncated = True
                continue
            user_messages.append(raw_message)

        if len(user_messages) > _CASE_DIAGNOSTIC_PRIOR_TURN_LIMIT:
            truncated = True
        for raw_message in user_messages[-_CASE_DIAGNOSTIC_PRIOR_TURN_LIMIT:]:
            content = _redact_and_bound(
                raw_message.get("content"),
                maximum=4096,
                field="case_diagnostics.prior_turns.content",
                removed_fields=removed_fields,
            )
            if not content:
                truncated = True
                continue
            prior_turns.append({"role": "user", "content": content})

    route_observations: list[dict[str, Any]] = []
    query_rewrite = _mapping(trace.get("query_rewrite"))
    routing = _mapping(trace.get("routing"))
    scope = _mapping(trace.get("scope"))
    rewritten_query = _safe_diagnostic_text(
        query_rewrite.get("rewritten_query") or trace.get("rewritten_query"),
        maximum=2048,
        removed_fields=removed_fields,
        field="case_diagnostics.route_observations.rewritten_query",
    )
    selected_route = _safe_diagnostic_token(
        routing.get("route") or selected_metadata.get("route")
    )
    filters, filters_truncated = _case_diagnostic_filters(
        scope.get("search_filters") or selected_metadata.get("search_filters"),
        removed_fields=removed_fields,
    )
    fallback_reason = _safe_diagnostic_text(
        routing.get("fallback_reason"),
        maximum=512,
        removed_fields=removed_fields,
        field="case_diagnostics.route_observations.fallback_reason",
    )
    truncated = truncated or filters_truncated
    if rewritten_query or selected_route or filters or fallback_reason:
        route_observations.append(
            {
                "rewritten_query": rewritten_query,
                "selected_route": selected_route,
                "filters": filters,
                "fallback_reason": fallback_reason,
            }
        )

    raw_sources = trace.get("sources") or selected_metadata.get(
        "selected_sources"
    )
    retrieval_observations: list[dict[str, Any]] = []
    evidence_refs: list[str] = []
    if isinstance(raw_sources, list):
        if len(raw_sources) > _CASE_DIAGNOSTIC_RETRIEVAL_LIMIT:
            truncated = True
        for position, raw_source in enumerate(raw_sources, 1):
            if len(retrieval_observations) >= _CASE_DIAGNOSTIC_RETRIEVAL_LIMIT:
                break
            if not isinstance(raw_source, Mapping):
                truncated = True
                continue
            source_uid = _safe_diagnostic_token(
                raw_source.get("source_uid") or raw_source.get("report_uid")
            )
            source_sha256 = _safe_sha256(raw_source.get("source_sha256"))
            rank = _positive_integer(raw_source.get("rank")) or position
            role = str(raw_source.get("role") or "OBSERVED_RESULT").upper()
            if role not in {"OBSERVED_RESULT", "CONTEXT_USED", "CITED"}:
                role = "OBSERVED_RESULT"
                truncated = True
            if source_uid is None or source_sha256 is None:
                truncated = True
                continue
            observation: dict[str, Any] = {
                "role": role,
                "source_uid": source_uid,
                "source_sha256": source_sha256,
                "rank": rank,
            }
            chunk_uid = _safe_diagnostic_token(raw_source.get("chunk_uid"))
            chunk_sha256 = _safe_sha256(raw_source.get("chunk_sha256"))
            if chunk_uid is not None or chunk_sha256 is not None:
                if chunk_uid is None or chunk_sha256 is None:
                    truncated = True
                else:
                    observation["chunk_uid"] = chunk_uid
                    observation["chunk_sha256"] = chunk_sha256
            retrieval_observations.append(observation)
            locator = _safe_diagnostic_text(
                raw_source.get("locator"),
                maximum=256,
                removed_fields=removed_fields,
                field="case_diagnostics.evidence_refs",
            )
            evidence_ref = f"{source_uid}#{locator}" if locator else source_uid
            if (
                evidence_ref not in evidence_refs
                and len(evidence_refs) < _CASE_DIAGNOSTIC_EVIDENCE_LIMIT
            ):
                evidence_refs.append(evidence_ref)

    if not prior_turns and not route_observations and not retrieval_observations:
        return None
    return {
        "schema_version": 1,
        "truncated": truncated,
        "prior_turns": prior_turns,
        "route_observations": route_observations,
        "retrieval_observations": retrieval_observations,
        "evidence_refs": evidence_refs,
    }


def _safe_sha256(value: Any) -> str | None:
    text = str(value or "")
    return text if _SHA256_RE.fullmatch(text) is not None else None


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return min(value, 1_000_000)


def _safe_diagnostic_token(value: Any) -> str | None:
    text = str(value or "").strip()
    if (
        not text
        or _DIAGNOSTIC_TOKEN_RE.fullmatch(text) is None
        or artifact_io.contains_sensitive_identifier_pattern(text)
        or _UNSAFE_DIAGNOSTIC_FILE_RE.search(text) is not None
        or _BASE64_BINARY_RE.fullmatch(text) is not None
    ):
        return None
    return text


def _safe_diagnostic_text(
    value: Any,
    *,
    maximum: int,
    removed_fields: set[str],
    field: str,
) -> str | None:
    if value is None:
        return None
    raw = str(value)
    if (
        artifact_io.contains_sensitive_identifier_pattern(raw)
        or _UNSAFE_DIAGNOSTIC_FILE_RE.search(raw) is not None
        or _BASE64_BINARY_RE.fullmatch(raw.strip()) is not None
        or raw.strip().lower().startswith("data:")
    ):
        removed_fields.add(field)
        return None
    text = _redact_and_bound(
        raw,
        maximum=maximum,
        field=field,
        removed_fields=removed_fields,
    )
    return text or None


def _case_diagnostic_filters(
    value: Any,
    *,
    removed_fields: set[str],
) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, Mapping):
        return {}, False
    result: dict[str, Any] = {}
    truncated = len(value) > 16
    for raw_key in sorted(value, key=str)[:16]:
        key = str(raw_key)
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None:
            truncated = True
            continue
        raw_item = value[raw_key]
        if raw_item is None or isinstance(raw_item, (bool, int, float)):
            item: Any = raw_item
        elif isinstance(raw_item, str):
            item = _safe_diagnostic_text(
                raw_item,
                maximum=256,
                removed_fields=removed_fields,
                field="case_diagnostics.route_observations.filters",
            )
            if item is None:
                truncated = True
                continue
        elif isinstance(raw_item, (list, tuple)):
            if len(raw_item) > 8:
                truncated = True
            items: list[Any] = []
            for value_item in list(raw_item)[:8]:
                if value_item is None or isinstance(value_item, (bool, int, float)):
                    items.append(value_item)
                elif isinstance(value_item, str):
                    safe = _safe_diagnostic_text(
                        value_item,
                        maximum=256,
                        removed_fields=removed_fields,
                        field="case_diagnostics.route_observations.filters",
                    )
                    if safe is not None:
                        items.append(safe)
                    else:
                        truncated = True
                else:
                    truncated = True
            item = items
        else:
            truncated = True
            continue
        result[key] = item
    return result, truncated


def _compact_remote_turn_trace(
    value: Any,
    *,
    removed_fields: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    raw_turns = value[-_REMOTE_TURN_TRACE_LIMIT:]
    if len(value) > len(raw_turns):
        removed_fields.add("observed.turn_trace")

    turns: list[dict[str, Any]] = []
    for raw_turn in raw_turns:
        if not isinstance(raw_turn, Mapping):
            removed_fields.add("observed.turn_trace")
            continue
        turn = {
            "turn_index": _bounded_count(
                raw_turn.get("turn_index"), maximum=1_000_000
            ),
            "question": _redact_and_bound(
                raw_turn.get("question"),
                maximum=2048,
                field="observed.turn_trace.question",
                removed_fields=removed_fields,
            ),
            "rewritten_query": _nullable_redacted_text(
                raw_turn.get("rewritten_query"),
                maximum=2048,
                field="observed.turn_trace.rewritten_query",
                removed_fields=removed_fields,
            ),
            "route": _nullable_token(raw_turn.get("route")),
            "status": _nullable_token(raw_turn.get("status")),
            "followup_scope_intent": _nullable_bool(
                raw_turn.get("followup_scope_intent")
            ),
            "scope_source": _nullable_token(raw_turn.get("scope_source")),
            "scope_reason": _nullable_token(raw_turn.get("scope_reason")),
            "matched_document_rank": _bounded_count(
                raw_turn.get("matched_document_rank"), maximum=1_000_000
            ),
            "route_hint": _nullable_token(raw_turn.get("route_hint")),
            "has_vector_intent": _nullable_bool(
                raw_turn.get("has_vector_intent")
            ),
            "search_filters": _compact_remote_filters(
                raw_turn.get("search_filters"),
                field="observed.turn_trace.search_filters",
                removed_fields=removed_fields,
            ),
            "prior_search_filters": _compact_remote_filters(
                raw_turn.get("prior_search_filters"),
                field="observed.turn_trace.prior_search_filters",
                removed_fields=removed_fields,
            ),
            "prior_file_names": _compact_remote_text_list(
                raw_turn.get("prior_file_names"),
                field="observed.turn_trace.prior_file_names",
                removed_fields=removed_fields,
            ),
            "selected_file_names": _compact_remote_text_list(
                raw_turn.get("selected_file_names"),
                field="observed.turn_trace.selected_file_names",
                removed_fields=removed_fields,
            ),
            "result_count": _bounded_count(
                raw_turn.get("result_count"), maximum=1_000_000
            ),
            "result_count_kind": (
                str(raw_turn.get("result_count_kind"))
                if raw_turn.get("result_count_kind") in _RESULT_COUNT_KINDS
                else None
            ),
        }
        if turn["turn_index"] is None or not turn["question"]:
            removed_fields.add("observed.turn_trace")
            continue
        turns.append(turn)
    while turns and _json_bytes(turns) > _REMOTE_TURN_TRACE_BYTES:
        turns.pop(0)
        removed_fields.add("observed.turn_trace")
    return turns


def _compact_remote_filters(
    value: Any,
    *,
    field: str,
    removed_fields: set[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    compact: dict[str, Any] = {}
    for key, raw_value in value.items():
        if key not in _REMOTE_FILTER_KEYS:
            removed_fields.add(field)
            continue
        if isinstance(raw_value, (list, tuple)):
            compact_value: Any = _compact_remote_text_list(
                list(raw_value),
                field=field,
                removed_fields=removed_fields,
            )
        elif raw_value is not None:
            compact_value = _redact_and_bound(
                raw_value,
                maximum=256,
                field=field,
                removed_fields=removed_fields,
            )
        else:
            continue
        candidate = {**compact, key: compact_value}
        while (
            isinstance(compact_value, list)
            and compact_value
            and _json_bytes(candidate) > _REMOTE_FILTER_BYTES
        ):
            compact_value.pop()
            candidate[key] = compact_value
            removed_fields.add(field)
        if _json_bytes(candidate) > _REMOTE_FILTER_BYTES:
            removed_fields.add(field)
            continue
        compact = candidate
    return compact


def _compact_remote_text_list(
    value: Any,
    *,
    field: str,
    removed_fields: set[str],
) -> list[str]:
    if not isinstance(value, list):
        return []
    if len(value) > 8:
        removed_fields.add(field)
    return [
        _redact_and_bound(
            item,
            maximum=256,
            field=field,
            removed_fields=removed_fields,
        )
        for item in value[:8]
        if item is not None
    ]


def _nullable_redacted_text(
    value: Any,
    *,
    maximum: int,
    field: str,
    removed_fields: set[str],
) -> str | None:
    if value is None:
        return None
    text = _redact_and_bound(
        value,
        maximum=maximum,
        field=field,
        removed_fields=removed_fields,
    )
    return text or None


def _nullable_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _bound_selected_content(
    question: str | None,
    answer: str | None,
) -> tuple[str | None, str | None]:
    question_bytes = len((question or "").encode("utf-8"))
    remaining = max(0, 32 * 1024 - question_bytes)
    if answer is not None:
        answer = _truncate_utf8(answer, remaining)
        if not answer:
            answer = None
    return question, answer


def _redact_and_bound(
    value: Any,
    *,
    maximum: int,
    field: str,
    removed_fields: set[str],
) -> str:
    raw = "" if value is None else str(value)
    redacted = _redact_outbound_text(raw)
    bounded = _truncate_utf8(redacted, maximum)
    if bounded != raw:
        removed_fields.add(field)
    return bounded


def _redact_outbound_text(value: str) -> str:
    redacted = redact_handoff_text(value)
    for kind, pattern in _OUTBOUND_REDACTIONS:
        redacted = pattern.sub(f"[REDACTED:{kind}]", redacted)
    return redacted


def _contains_outbound_sensitive_pattern(value: str) -> bool:
    return artifact_io.contains_sensitive_identifier_pattern(value) or any(
        pattern.search(value) is not None
        for _kind, pattern in _OUTBOUND_REDACTIONS
    )


def _truncate_utf8(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore")


def _bounded_identifier(value: Any, *, required: bool = False) -> str | int | None:
    if value is None:
        if required:
            raise IssueReportOutboxError("missing_report_id")
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    text = str(value)
    if not text and required:
        raise IssueReportOutboxError("missing_report_id")
    if len(text.encode("utf-8")) > 128:
        return None if not required else _truncate_utf8(text, 128)
    if artifact_io.contains_sensitive_identifier_pattern(text):
        if required:
            raise IssueReportOutboxError("unsafe_report_id")
        return None
    return text


def _bounded_count(value: Any, *, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= maximum else None


def _latency_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    milliseconds = round(float(value) * 1000)
    return milliseconds if 0 <= milliseconds <= 86_400_000 else None


def _nullable_token(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if _TOKEN_RE.fullmatch(text) else None


def _rfc3339(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _valid_ingest_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme == "https" and bool(parsed.netloc):
        return True
    return bool(
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True
