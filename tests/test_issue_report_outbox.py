import json
import sqlite3
from pathlib import Path

import pytest
import requests

from src.core import issue_report_outbox, issue_report_store


NOW = 1_786_233_600.0  # 2026-08-09T00:00:00Z
RECEIPT_ID = "018f47a0-3333-7333-8333-333333333333"


class FakeResponse:
    def __init__(self, status_code, payload, *, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = json.dumps(payload).encode("utf-8")

    def json(self):
        return self._payload


def _report(report_id="report-1"):
    return {
        "schema_version": 2,
        "report_contract_version": 2,
        "id": report_id,
        "kind": "user_feedback",
        "report_target_type": "response",
        "source": "chat_monitoring_trace",
        "created_at": "2026-08-08T23:59:00+00:00",
        "app_version": "1.2.3",
        "thread_id": "thread-1",
        "message_id": 42,
        "job_id": None,
        "category": "검색 정확도 이슈",
        "comment": "wrong answer for user@example.com; bearer abcdefghijk",
        "context": {
            "selected_user_question": "C:\\Users\\alice\\question.txt 내용을 봐줘",
            "selected_message": {
                "content_preview": "API_KEY=super-secret-value",
                "metadata": {"route": "vectordb", "status": "succeeded"},
            },
            "conversation_messages": [{"content": "must stay local"}],
            "trace_detail": {
                "routing": {"route": "vectordb"},
                "timing": {"total_seconds": 1.25},
                "answer": {
                    "source_count": 3,
                    "citation_ranks_used": [1, 2],
                },
            },
        },
        "diagnostics": {
            "error_code": "retrieval_mismatch",
            "error_type": "LookupError",
            "error_signature": "a" * 64,
            "debug_hints": ["email admin@example.com", "safe_hint"],
        },
    }


def _config(tmp_path):
    return issue_report_outbox.DeliveryConfig(
        enabled=True,
        ingest_url=(
            "https://example.supabase.co/functions/v1/issue-report-ingest"
        ),
        publishable_key="sb_publishable_test-only",
        outbox_dir=tmp_path / "outbox",
    )


def _payload(database_path: Path):
    with sqlite3.connect(database_path) as connection:
        raw = connection.execute(
            "SELECT payload_json FROM outbox_events"
        ).fetchone()[0]
    return json.loads(raw)


def test_remote_report_is_exact_bounded_and_redacted():
    remote = issue_report_outbox.build_remote_report(
        _report(),
        consent={
            "consent_version": 1,
            "include_comment": True,
            "include_selected_question": True,
            "include_selected_answer": True,
        },
    )

    assert set(remote) == {
        "schema_version",
        "report_contract_version",
        "kind",
        "report_target_type",
        "source",
        "app_version",
        "category",
        "comment",
        "consent",
        "observed",
        "diagnostics",
        "privacy",
    }
    assert remote["consent"] == {
        "consent_version": 1,
        "include_comment": True,
        "include_selected_question": True,
        "include_selected_answer": True,
        "include_previous_turns": False,
    }
    serialized = json.dumps(remote, ensure_ascii=False)
    assert "user@example.com" not in serialized
    assert "admin@example.com" not in serialized
    assert "super-secret-value" not in serialized
    assert "C:\\Users\\alice" not in serialized
    assert "must stay local" not in serialized
    assert remote["observed"]["latency_ms"] == 1250
    assert remote["observed"]["result_count"] == 3
    assert remote["observed"]["citation_count"] == 2
    assert remote["diagnostics"]["stack_hash"] == "a" * 64
    assert remote["app_version"] == "1.2.3"
    assert {
        "id",
        "created_at",
        "thread_id",
        "message_id",
        "job_id",
    }.issubset(remote["privacy"]["removed_fields"])
    assert "context.conversation_messages" in remote["privacy"][
        "removed_fields"
    ]


def test_remote_content_consent_is_explicit_and_fail_closed():
    without_consent = issue_report_outbox.build_remote_report(_report())
    invalid_consent = issue_report_outbox.build_remote_report(
        _report(),
        consent={
            "include_comment": True,
            "include_selected_question": True,
            "include_selected_answer": True,
        },
    )
    boolean_version = issue_report_outbox.build_remote_report(
        _report(),
        consent={"consent_version": True, "include_comment": True},
    )
    float_version = issue_report_outbox.build_remote_report(
        _report(),
        consent={"consent_version": 1.0, "include_comment": True},
    )

    for remote in (
        without_consent,
        invalid_consent,
        boolean_version,
        float_version,
    ):
        assert remote["comment"] == ""
        assert remote["observed"]["selected_question"] is None
        assert remote["observed"]["selected_answer"] is None
        assert remote["consent"]["include_comment"] is False


@pytest.mark.parametrize(
    "unsafe",
    [
        "sb_secret_FAKE1234567890abcdefghijklmnop",
        "ISSUE_IP_HMAC_SECRET=abcdefghijklmnopqrstuvwxyz123456",
        "SUPABASE_SECRET_KEYS=sb_secret_FAKEabcdefghijklmnop",
        "ISSUE_NOTIFICATION_WEBHOOK_URL=https://hooks.example/secret-capability",
        "GITHUB_TOKEN=abcdefghijklmnopqrstuvwxyz123456",
        "HF_TOKEN: abcdefghijklmnopqrstuvwxyz123456",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature12345",
        "900101-1234567",
        "".join(("xox", "b-", "1234567890-abcdefghijklmnop")),
        "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
    ],
)
def test_outbound_redaction_removes_high_risk_secret_patterns(unsafe):
    report = _report()
    report["comment"] = f"before {unsafe} after"

    remote = issue_report_outbox.build_remote_report(
        report,
        consent={"consent_version": 1, "include_comment": True},
    )

    serialized = json.dumps(remote, ensure_ascii=False)
    assert unsafe not in serialized
    assert "[REDACTED:" in serialized


def test_residual_secret_detection_blocks_remote_queue(monkeypatch):
    unsafe = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    report = _report()
    report["comment"] = unsafe
    monkeypatch.setattr(
        issue_report_outbox,
        "_redact_outbound_text",
        lambda value: value,
    )

    with pytest.raises(
        issue_report_outbox.IssueReportOutboxError,
        match="unredacted_sensitive_data",
    ):
        issue_report_outbox.build_remote_report(
            report,
            consent={"consent_version": 1, "include_comment": True},
        )


def test_enqueue_is_durable_and_idempotent_per_local_report(tmp_path):
    config = _config(tmp_path)

    first = issue_report_outbox.enqueue_report(
        _report(),
        database_path=config.database_path,
        now=NOW,
    )
    second = issue_report_outbox.enqueue_report(
        _report(),
        database_path=config.database_path,
        now=NOW + 1,
    )

    assert first == second
    payload = _payload(config.database_path)
    assert payload["event_id"] == first["event_id"]
    assert payload["queued_at"] == "2026-08-09T00:00:00Z"
    assert payload["report"]["consent"]["include_previous_turns"] is False
    assert payload["report"]["app_version"] == "1.2.3"
    assert {
        "id",
        "created_at",
        "thread_id",
        "message_id",
        "job_id",
    }.isdisjoint(payload["report"])
    assert str(payload["installation_id"])
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM outbox_events"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_supabase_storage_migration_scrubs_local_correlation_fields():
    migration = (
        Path("supabase/migrations/202608090003_minimize_issue_report_payload.sql")
        .read_text(encoding="utf-8")
    )

    assert "before insert or update of report" in migration
    assert "issue_reports_excludes_local_correlation_fields" in migration
    for field in ("id", "created_at", "thread_id", "message_id", "job_id"):
        assert f"'{field}'" in migration


def test_in_memory_report_queues_without_creating_local_artifacts(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    report = issue_report_store.build_issue_report(
        "thread-1",
        "버그/기능",
        "버튼이 작동하지 않습니다.",
        {
            "submitted_from": "streamlit_chat",
            "app_version": "1.2.3",
            "remote_consent": {
                "consent_version": 1,
                "include_comment": True,
                "include_selected_question": False,
                "include_selected_answer": False,
                "include_previous_turns": False,
            },
        },
        report_target_type="ui_or_system",
    )

    result = issue_report_outbox.queue_report(
        report,
        config=config,
        start_worker=False,
        now=NOW,
    )

    assert result["status"] == "queued"
    assert not debug_dir.exists()
    assert "file_path" not in report
    assert "json_path" not in report
    assert _payload(config.database_path)["report"]["comment"] == (
        "버튼이 작동하지 않습니다."
    )


def test_queue_saved_report_stays_local_when_remote_is_not_configured(tmp_path):
    config = issue_report_outbox.DeliveryConfig(
        enabled=True,
        ingest_url=None,
        publishable_key=None,
        outbox_dir=tmp_path / "outbox",
    )

    result = issue_report_outbox.queue_saved_report(
        tmp_path / "missing.json",
        config=config,
    )

    assert result == {
        "status": "local_only",
        "reason": "remote_not_configured",
    }
    assert not config.database_path.exists()


def test_in_memory_report_is_unavailable_without_remote_configuration(tmp_path):
    config = issue_report_outbox.DeliveryConfig(
        enabled=True,
        ingest_url=None,
        publishable_key=None,
        outbox_dir=tmp_path / "outbox",
    )

    result = issue_report_outbox.queue_report(_report(), config=config)

    assert result == {
        "status": "unavailable",
        "reason": "remote_not_configured",
    }
    assert not config.database_path.exists()


def test_saved_local_report_is_queued_without_changing_local_artifacts(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    stored = issue_report_store.create_issue_report(
        "thread-1",
        "버그/기능",
        "버튼이 작동하지 않습니다.",
        {
            "submitted_from": "streamlit_chat",
            "app_version": "1.2.3",
            "remote_consent": {
                "consent_version": 1,
                "include_comment": True,
                "include_selected_question": False,
                "include_selected_answer": False,
                "include_previous_turns": False,
            },
        },
        report_target_type="ui_or_system",
    )

    result = issue_report_outbox.queue_saved_report(
        stored["json_path"],
        config=config,
        start_worker=False,
        now=NOW,
    )

    assert result["status"] == "queued"
    assert Path(stored["file_path"]).exists()
    assert Path(stored["json_path"]).exists()
    assert _payload(config.database_path)["report"]["comment"] == (
        "버튼이 작동하지 않습니다."
    )


def test_corrupt_saved_report_cannot_break_local_success(tmp_path):
    config = _config(tmp_path)
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{", encoding="utf-8")

    result = issue_report_outbox.queue_saved_report(
        corrupt,
        config=config,
        start_worker=False,
    )

    assert result["status"] == "queue_failed"
    assert corrupt.read_text(encoding="utf-8") == "{"


def test_worker_start_failure_never_escapes_queue_boundary(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    report = issue_report_store.build_issue_report(
        "thread-1",
        "기타",
        "재시도 대기열에만 남아야 합니다.",
        {"app_version": "1.2.3"},
    )

    def fail_to_start(_config):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(
        issue_report_outbox,
        "start_delivery_worker",
        fail_to_start,
    )

    result = issue_report_outbox.queue_report(
        report,
        config=config,
        start_worker=True,
        now=NOW,
    )

    assert result["status"] == "queued"
    assert result["reason"] == "worker_not_started"
    assert not debug_dir.exists()
    assert issue_report_outbox.outbox_status(
        config.database_path,
        report_id=str(report["id"]),
    )["status"] == "queued"


def test_outbox_failure_does_not_create_a_local_report_file(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    monkeypatch.setattr(issue_report_outbox, "MAX_OUTBOX_EVENTS", 0)
    report = issue_report_store.build_issue_report(
        "thread-1",
        "기타",
        "로컬 파일로 남지 않아야 합니다.",
        {"app_version": "1.2.3"},
    )

    result = issue_report_outbox.queue_report(
        report,
        config=config,
        start_worker=False,
        now=NOW,
    )

    assert result == {"status": "queue_failed", "reason": "outbox_full"}
    assert not debug_dir.exists()


def test_successful_delivery_uses_only_apikey_and_acks_payload(tmp_path):
    config = _config(tmp_path)
    queued = issue_report_outbox.enqueue_report(
        _report(), database_path=config.database_path, now=NOW
    )
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(
            200,
            {
                "ok": True,
                "disposition": "accepted",
                "receipt_id": RECEIPT_ID,
                "received_at": "2026-08-09T00:00:01Z",
            },
        )

    result = issue_report_outbox.process_outbox_once(
        config, now=NOW, post=post
    )

    assert result == {
        "event_id": queued["event_id"],
        "status": "delivered",
        "code": "accepted",
        "receipt_id": RECEIPT_ID,
    }
    assert captured["headers"]["apikey"] == "sb_publishable_test-only"
    assert "Authorization" not in captured["headers"]
    assert captured["timeout"] == issue_report_outbox.REQUEST_TIMEOUT_SECONDS
    sent = json.loads(captured["data"])
    assert sent["event_id"] == queued["event_id"]
    assert issue_report_outbox.outbox_status(
        config.database_path, report_id="report-1"
    ) == {}
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM outbox_events"
        ).fetchone()[0] == 0


def test_terminal_delivery_removes_payload_from_sqlite_and_wal(tmp_path):
    config = _config(tmp_path)
    marker = "retry_only_payload_marker_4f6350b4"
    report = _report("report-delete-marker")
    report["comment"] = marker
    issue_report_outbox.enqueue_report(
        report,
        consent={"consent_version": 1, "include_comment": True},
        database_path=config.database_path,
        now=NOW,
    )
    assert marker in _payload(config.database_path)["report"]["comment"]

    result = issue_report_outbox.process_outbox_once(
        config,
        now=NOW,
        post=lambda *_args, **_kwargs: FakeResponse(
            200,
            {
                "ok": True,
                "disposition": "accepted",
                "receipt_id": RECEIPT_ID,
                "received_at": "2026-08-09T00:00:01Z",
            },
        ),
    )

    assert result["status"] == "delivered"
    assert issue_report_outbox.outbox_status(config.database_path) == {}
    marker_bytes = marker.encode("utf-8")
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{config.database_path}{suffix}")
        if path.exists():
            assert marker_bytes not in path.read_bytes()


def test_429_preserves_event_and_honors_retry_after(tmp_path):
    config = _config(tmp_path)
    queued = issue_report_outbox.enqueue_report(
        _report(), database_path=config.database_path, now=NOW
    )

    result = issue_report_outbox.process_outbox_once(
        config,
        now=NOW,
        post=lambda *_args, **_kwargs: FakeResponse(
            429,
            {"ok": False, "code": "rate_limited"},
            headers={"Retry-After": "120"},
        ),
    )

    assert result["status"] == "retry"
    assert result["event_id"] == queued["event_id"]
    with sqlite3.connect(config.database_path) as connection:
        row = connection.execute(
            """
            SELECT event_id, status, attempt_count, available_at, payload_json
            FROM outbox_events
            """
        ).fetchone()
    assert row[:4] == (queued["event_id"], "retry", 1, NOW + 120)
    assert json.loads(row[4])["event_id"] == queued["event_id"]


def test_permanent_contract_error_is_rejected_without_retry(tmp_path):
    config = _config(tmp_path)
    issue_report_outbox.enqueue_report(
        _report(), database_path=config.database_path, now=NOW
    )

    result = issue_report_outbox.process_outbox_once(
        config,
        now=NOW,
        post=lambda *_args, **_kwargs: FakeResponse(
            422, {"ok": False, "code": "invalid_report"}
        ),
    )

    assert result["status"] == "rejected"
    assert result["code"] == "invalid_report"
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM outbox_events"
        ).fetchone()[0] == 0


def test_network_failure_retries_without_raising_into_caller(tmp_path):
    config = _config(tmp_path)
    issue_report_outbox.enqueue_report(
        _report(), database_path=config.database_path, now=NOW
    )

    def unavailable(*_args, **_kwargs):
        raise requests.ConnectionError("offline")

    result = issue_report_outbox.process_outbox_once(
        config,
        now=NOW,
        post=unavailable,
        jitter=lambda _start, _end: 0.0,
    )

    assert result["status"] == "retry"
    assert result["code"] == "network_error"
    with sqlite3.connect(config.database_path) as connection:
        row = connection.execute(
            "SELECT attempt_count, available_at FROM outbox_events"
        ).fetchone()
    assert row == (1, NOW + 30)


def test_retriable_server_failures_stop_after_three_retries(tmp_path):
    config = _config(tmp_path)
    marker = "three_retry_failure_marker_35bd0eb8"
    report = _report()
    report["comment"] = marker
    issue_report_outbox.enqueue_report(
        report,
        consent={"consent_version": 1, "include_comment": True},
        database_path=config.database_path,
        now=NOW,
    )
    current_time = NOW

    for attempt in range(issue_report_outbox.MAX_DELIVERY_ATTEMPTS):
        result = issue_report_outbox.process_outbox_once(
            config,
            now=current_time,
            post=lambda *_args, **_kwargs: FakeResponse(
                503, {"ok": False, "code": "storage_unavailable"}
            ),
            jitter=lambda _start, _end: 0.0,
        )
        if attempt < issue_report_outbox.MAX_DELIVERY_ATTEMPTS - 1:
            assert result["status"] == "retry"
            with sqlite3.connect(config.database_path) as connection:
                row = connection.execute(
                    "SELECT attempt_count, available_at FROM outbox_events"
                ).fetchone()
            assert row[0] == attempt + 1
            current_time = row[1]
        else:
            assert result["status"] == "dead_letter"

    assert issue_report_outbox.MAX_DELIVERY_RETRIES == 3
    assert issue_report_outbox.outbox_status(
        config.database_path,
        report_id="report-1",
    ) == {}
    marker_bytes = marker.encode("utf-8")
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{config.database_path}{suffix}")
        if path.exists():
            assert marker_bytes not in path.read_bytes()


def test_expired_worker_lease_is_recovered(tmp_path):
    config = _config(tmp_path)
    issue_report_outbox.enqueue_report(
        _report(), database_path=config.database_path, now=NOW
    )
    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            """
            UPDATE outbox_events
            SET status = 'sending', lease_owner = 'dead-worker',
                lease_expires_at = ?
            """,
            (NOW - 1,),
        )

    result = issue_report_outbox.process_outbox_once(
        config,
        now=NOW,
        post=lambda *_args, **_kwargs: FakeResponse(
            200,
            {
                "ok": True,
                "disposition": "duplicate",
                "receipt_id": RECEIPT_ID,
                "received_at": "2026-08-09T00:00:01Z",
            },
        ),
    )

    assert result["status"] == "delivered"
    assert result["code"] == "duplicate"


def test_queue_limit_never_breaks_the_local_report(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(issue_report_outbox, "MAX_OUTBOX_EVENTS", 1)
    issue_report_outbox.enqueue_report(
        _report("report-1"), database_path=config.database_path, now=NOW
    )

    with pytest.raises(
        issue_report_outbox.IssueReportOutboxError, match="outbox_full"
    ):
        issue_report_outbox.enqueue_report(
            _report("report-2"),
            database_path=config.database_path,
            now=NOW,
        )


def test_delivery_config_requires_https_except_for_local_development(tmp_path):
    insecure = issue_report_outbox.DeliveryConfig(
        enabled=True,
        ingest_url="http://example.supabase.co/functions/v1/issue-report-ingest",
        publishable_key="sb_publishable_test-only",
        outbox_dir=tmp_path,
    )
    local = issue_report_outbox.DeliveryConfig(
        enabled=True,
        ingest_url="http://127.0.0.1:54321/functions/v1/issue-report-ingest",
        publishable_key="sb_publishable_test-only",
        outbox_dir=tmp_path,
    )

    assert insecure.configured is False
    assert local.configured is True
