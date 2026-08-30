from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.core.monitoring_admin_client import (
    MonitoringAdminClient,
    OperatorApiConfig,
    OperatorApiError,
    OperatorConflictError,
    OperatorForbiddenError,
    OperatorSession,
    OperatorUnauthorizedError,
    production_monitoring_enabled,
    sign_in_with_password,
)


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class NonJsonResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.closed = False

    def json(self):
        raise ValueError("gateway did not return JSON")

    def close(self):
        self.closed = True


@pytest.fixture
def config(tmp_path: Path) -> OperatorApiConfig:
    return OperatorApiConfig(
        project_url="https://example.supabase.co",
        publishable_key="sb_publishable_test-only",
        function_url=(
            "https://example.supabase.co/functions/v1/issue-report-operator"
        ),
        artifact_root=tmp_path / "monitoring",
    )


def test_password_login_keeps_only_access_token_and_never_refresh_token(config):
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(
            200,
            {
                "access_token": "user-jwt",
                "refresh_token": "must-not-be-retained",
                "expires_in": 3600,
                "user": {
                    "id": "018f47a0-1111-7111-8111-111111111111",
                    "email": "admin@example.com",
                },
            },
        )

    session = sign_in_with_password(
        config,
        email="admin@example.com",
        password="correct horse battery staple",
        post=post,
        now=1000.0,
    )

    assert session == OperatorSession(
        access_token="user-jwt",
        user_id="018f47a0-1111-7111-8111-111111111111",
        email="admin@example.com",
        expires_at=4600.0,
    )
    assert "refresh" not in repr(session).lower()
    assert captured["url"].endswith("/auth/v1/token?grant_type=password")
    assert captured["headers"] == {
        "apikey": "sb_publishable_test-only",
        "content-type": "application/json",
    }
    assert captured["json"] == {
        "email": "admin@example.com",
        "password": "correct horse battery staple",
    }


def test_password_login_rejects_invalid_credentials_without_leaking_detail(config):
    with pytest.raises(OperatorUnauthorizedError, match="login failed"):
        sign_in_with_password(
            config,
            email="admin@example.com",
            password="wrong",
            post=lambda *_args, **_kwargs: FakeResponse(
                400, {"error_description": "user not found"}
            ),
        )


def test_operator_api_sends_publishable_key_and_user_jwt(config):
    captured = {}

    def request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse(200, {"ok": True, "issues": []})

    client = MonitoringAdminClient(
        config,
        OperatorSession(
            access_token="user-jwt",
            user_id="admin-id",
            email="admin@example.com",
            expires_at=time.time() + 60,
        ),
        request=request,
    )

    assert client.list_issues(state="OPEN") == []
    assert captured["method"] == "GET"
    assert captured["params"] == {"state": "OPEN", "limit": 50}
    assert captured["headers"] == {
        "apikey": "sb_publishable_test-only",
        "Authorization": "Bearer user-jwt",
        "Accept": "application/json",
    }


def test_operator_get_retries_one_transient_gateway_failure(config):
    gateway_failure = NonJsonResponse(502)
    responses = [
        gateway_failure,
        FakeResponse(200, {"ok": True, "issues": []}),
    ]
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses.pop(0)

    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=request,
    )

    assert client.list_issues(limit=1) == []
    assert len(calls) == 2
    assert gateway_failure.closed is True


def test_repeated_non_json_gateway_failure_uses_available_error_code(config):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return NonJsonResponse(502)

    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=request,
    )

    with pytest.raises(OperatorApiError) as caught:
        client.list_issues(limit=1)

    assert caught.value.code == "operator_api_unavailable"
    assert caught.value.status_code == 502
    assert len(calls) == 2


def test_operator_post_does_not_retry_transient_gateway_failure(config):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return NonJsonResponse(502)

    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=request,
    )

    with pytest.raises(OperatorApiError) as caught:
        client.view_raw("018f47a0-1111-7111-8111-111111111111")

    assert caught.value.code == "operator_api_unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "payload", "error_type"),
    [
        (401, {"code": "unauthorized"}, OperatorUnauthorizedError),
        (403, {"code": "forbidden"}, OperatorForbiddenError),
        (409, {"code": "revision_conflict"}, OperatorConflictError),
        (503, {"code": "storage_unavailable"}, OperatorApiError),
    ],
)
def test_operator_api_maps_stable_errors(config, status, payload, error_type):
    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=lambda *_args, **_kwargs: FakeResponse(status, payload),
    )

    with pytest.raises(error_type):
        client.get_issue("018f47a0-1111-7111-8111-111111111111")


def test_raw_view_is_an_explicit_post_and_transition_uses_cas(config):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/raw"):
            return FakeResponse(200, {"ok": True, "raw_report": {"comment": "동의됨"}})
        return FakeResponse(
            200,
            {"ok": True, "issue": {"state": "RESOLVED", "record_revision": 2}},
        )

    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=request,
    )

    assert client.view_raw("018f47a0-1111-7111-8111-111111111111")["comment"] == "동의됨"
    updated = client.transition_issue(
        "018f47a0-1111-7111-8111-111111111111",
        target_state="RESOLVED",
        expected_record_revision=1,
        reason="개선 확인",
    )

    assert updated["record_revision"] == 2
    assert calls[0][0] == "POST" and calls[0][1].endswith("/raw")
    assert "json" not in calls[0][2]
    assert calls[1][1].endswith("/resolve")
    assert calls[1][2]["json"] == {
        "expected_record_revision": 1,
        "reason": "개선 확인",
    }


@pytest.mark.parametrize(
    ("target_state", "action"),
    [
        ("OPEN", "reopen"),
        ("IN_PROGRESS", "start"),
        ("RESOLVED", "resolve"),
        ("NOT_ISSUE", "dismiss"),
    ],
)
def test_operator_issue_lifecycle_routes_are_explicit(
    config, target_state, action
):
    captured = {}

    def request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse(
            200,
            {
                "ok": True,
                "issue": {"state": target_state, "record_revision": 2},
            },
        )

    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=request,
    )

    updated = client.transition_issue(
        "018f47a0-1111-7111-8111-111111111111",
        target_state=target_state,
        expected_record_revision=1,
        reason="상태 변경 근거",
    )

    assert updated["state"] == target_state
    assert captured["method"] == "POST"
    assert captured["url"].endswith(f"/{action}")
    assert captured["json"] == {
        "expected_record_revision": 1,
        "reason": "상태 변경 근거",
    }


def test_operator_rejects_legacy_closed_as_a_transition_target(config):
    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=lambda *_args, **_kwargs: pytest.fail(
            "legacy CLOSED target reached the operator API"
        ),
    )

    with pytest.raises(ValueError, match="writable Issue lifecycle state"):
        client.transition_issue(
            "018f47a0-1111-7111-8111-111111111111",
            target_state="CLOSED",
            expected_record_revision=1,
            reason="분류 없는 종료",
        )


def test_operator_issue_list_accepts_each_lifecycle_state(config):
    states = ("OPEN", "IN_PROGRESS", "RESOLVED", "NOT_ISSUE", "CLOSED")
    captured = []

    def request(_method, _url, **kwargs):
        captured.append(kwargs["params"])
        return FakeResponse(200, {"ok": True, "issues": []})

    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=request,
    )

    for state in states:
        assert client.list_issues(state=state) == []

    assert [params["state"] for params in captured] == list(states)


def test_production_gate_requires_environment_flag_mode_and_complete_config(config):
    assert production_monitoring_enabled(
        deployment_environment="production",
        monitoring_mode=True,
        config=config,
    )
    assert not production_monitoring_enabled(
        deployment_environment="development",
        monitoring_mode=True,
        config=config,
    )
    assert not production_monitoring_enabled(
        deployment_environment="production",
        monitoring_mode=False,
        config=config,
    )
    incomplete = OperatorApiConfig(
        project_url=None,
        publishable_key=None,
        function_url=None,
        artifact_root=config.artifact_root,
    )
    assert not production_monitoring_enabled(
        deployment_environment="production",
        monitoring_mode=True,
        config=incomplete,
    )


def test_operator_config_accepts_http_only_for_loopback(tmp_path):
    local = OperatorApiConfig(
        project_url="http://127.0.0.1:54321",
        publishable_key="sb_publishable_local",
        function_url="http://127.0.0.1:54321/functions/v1/issue-report-operator",
        artifact_root=tmp_path,
    )
    assert local.configured

    remote = OperatorApiConfig(
        project_url="http://example.com",
        publishable_key="sb_publishable_remote",
        function_url="http://example.com/functions/v1/issue-report-operator",
        artifact_root=tmp_path,
    )
    assert not remote.configured


@pytest.mark.parametrize(
    "function_url",
    (
        "https://attacker.example/functions/v1/issue-report-operator",
        "https://example.supabase.co/functions/v1/another-function",
        "https://example.supabase.co:444/functions/v1/issue-report-operator",
    ),
)
def test_operator_config_rejects_jwt_destination_outside_project_function(
    tmp_path: Path,
    function_url: str,
) -> None:
    unsafe = OperatorApiConfig(
        project_url="https://example.supabase.co",
        publishable_key="sb_publishable_remote",
        function_url=function_url,
        artifact_root=tmp_path,
    )
    requested = False

    def request(*_args, **_kwargs):
        nonlocal requested
        requested = True
        pytest.fail("JWT was sent to an untrusted function URL")

    assert not unsafe.configured
    with pytest.raises(OperatorApiError, match="operator_api_not_configured"):
        MonitoringAdminClient(
            unsafe,
            OperatorSession(
                "must-not-be-sent",
                "admin",
                "admin@example.com",
                time.time() + 60,
            ),
            request=request,
        )
    assert requested is False


def _control_record(**overrides):
    record = {
        "record_kind": "RUN",
        "record_id": "run_0123456789abcdef0123456789abcdef",
        "lifecycle_status": "SUCCEEDED",
        "content_digest": "a" * 64,
        "availability": None,
        "references": {
            "fixed_snapshot_revision_id": "snapshot_0123456789abcdef0123456789abcdef",
            "case_contract_id": "b" * 64,
            "release_manifest_id": "release_0123456789abcdef0123456789abcdef",
        },
        "attributes": {
            "side": "BASELINE",
            "validity": "VALID",
            "evidence_qualifier": "EXACT",
        },
    }
    record.update(overrides)
    return record


def test_control_reconcile_uses_bounded_canonical_record_and_cas(config):
    captured = {}

    def request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "disposition": "updated",
                    "record_revision": 3,
                    "content_digest": "a" * 64,
                },
            },
        )

    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=request,
    )
    result = client.reconcile_control_record(
        "018f47a0-1111-7111-8111-111111111111",
        expected_record_revision=2,
        record=_control_record(),
    )

    assert result["record_revision"] == 3
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/issues/018f47a0-1111-7111-8111-111111111111/control")
    assert captured["json"] == {
        "expected_record_revision": 2,
        "record": _control_record(),
    }


@pytest.mark.parametrize(
    "record",
    [
        _control_record(local_path="C:/secret/artifact.json"),
        _control_record(content_digest="not-a-digest"),
        _control_record(references={"artifact_path": "secret"}),
        _control_record(references={"case_contract_id": ["b" * 64]}),
        _control_record(
            references={
                "case_contract_id": "b" * 64,
                "release_manifest_id": "release_0123456789abcdef0123456789abcdef",
            }
        ),
        _control_record(attributes={"answer": "SECRET"}),
        _control_record(lifecycle_status="RUNNING"),
        _control_record(record_kind="COMPARISON", lifecycle_status="SUCCEEDED"),
        {
            "record_kind": "COMPARISON",
            "record_id": "comparison_0123456789abcdef0123456789abcdef",
            "lifecycle_status": "CREATED",
            "content_digest": "d" * 64,
            "availability": None,
            "references": {},
            "attributes": {"verdict": "INCONCLUSIVE"},
        },
        {
            "record_kind": "COMPARISON",
            "record_id": "comparison_0123456789abcdef0123456789abcdef",
            "lifecycle_status": "CREATED",
            "content_digest": "d" * 64,
            "availability": None,
            "references": {"case_contract_id": "b" * 64},
            "attributes": {},
        },
        {
            "record_kind": "CASE",
            "record_id": "case_0123456789abcdef0123456789abcdef",
            "lifecycle_status": "READY",
            "content_digest": "b" * 64,
            "availability": None,
            "references": {
                "fixture_revision_id": "fixture_0123456789abcdef0123456789abcdef",
                "fixed_snapshot_revision_id": "snapshot_0123456789abcdef0123456789abcdef",
            },
            "attributes": {},
        },
        {
            "record_kind": "FIXTURE",
            "record_id": "fixture_0123456789abcdef0123456789abcdef",
            "lifecycle_status": "READY",
            "content_digest": "c" * 64,
            "availability": "AVAILABLE",
            "references": {},
            "attributes": {},
        },
        {
            "record_kind": "COMPARISON",
            "record_id": "comparison_0123456789abcdef0123456789abcdef",
            "lifecycle_status": "CREATED",
            "content_digest": "d" * 64,
            "availability": None,
            "references": {"case_contract_id": "b" * 64},
            "attributes": {"verdict": "MIXED"},
        },
    ],
)
def test_control_reconcile_rejects_noncanonical_or_sensitive_fields(config, record):
    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=lambda *_args, **_kwargs: pytest.fail("invalid record reached network"),
    )
    with pytest.raises(ValueError):
        client.reconcile_control_record(
            "018f47a0-1111-7111-8111-111111111111",
            expected_record_revision=0,
            record=record,
        )


def test_control_projection_check_reports_missing_unexpected_and_mismatch(config):
    local_run = _control_record()
    local_fixture = {
        "record_kind": "FIXTURE",
        "record_id": "fixture_0123456789abcdef0123456789abcdef",
        "lifecycle_status": "READY",
        "content_digest": "c" * 64,
        "availability": None,
        "references": {},
        "attributes": {},
    }
    remote_run = {**local_run, "lifecycle_status": "RUNNING", "record_revision": 2}
    remote_release = {
        "record_kind": "RELEASE",
        "record_id": "release_0123456789abcdef0123456789abcdef",
        "lifecycle_status": "REGISTERED",
        "content_digest": "d" * 64,
        "availability": "AVAILABLE",
        "references": {},
        "attributes": {},
        "record_revision": 1,
    }
    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=lambda *_args, **_kwargs: FakeResponse(
            200, {"ok": True, "records": [remote_run, remote_release]}
        ),
    )

    diff = client.check_control_projection(
        "018f47a0-1111-7111-8111-111111111111",
        [local_run, local_fixture],
    )

    assert diff["missing"] == [local_fixture]
    assert diff["unexpected"] == [remote_release]
    assert diff["mismatched"] == [{"local": local_run, "remote": remote_run}]


def test_control_projection_reconcile_bridges_queued_run_before_success(
    config, monkeypatch
):
    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=lambda *_args, **_kwargs: pytest.fail("network should be mocked"),
    )
    local = _control_record()
    remote = {
        **local,
        "lifecycle_status": "QUEUED",
        "record_revision": 2,
    }
    checks = iter(
        [
            {
                "missing": [],
                "unexpected": [],
                "mismatched": [{"local": local, "remote": remote}],
            },
            {"missing": [], "unexpected": [], "mismatched": []},
        ]
    )
    reconciled = []

    monkeypatch.setattr(
        client,
        "check_control_projection",
        lambda _issue_id, _expected: next(checks),
    )

    def reconcile(_issue_id, *, expected_record_revision, record):
        reconciled.append((expected_record_revision, dict(record)))
        return {"record_revision": expected_record_revision + 1}

    monkeypatch.setattr(client, "reconcile_control_record", reconcile)

    assert client.reconcile_control_projection("issue-1", [local]) == {
        "missing": [],
        "unexpected": [],
        "mismatched": [],
    }
    assert [record["lifecycle_status"] for _, record in reconciled] == [
        "RUNNING",
        "SUCCEEDED",
    ]
    assert [revision for revision, _ in reconciled] == [2, 3]


def test_control_projection_replays_missing_recovered_run_lifecycle(
    config, monkeypatch
):
    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=lambda *_args, **_kwargs: pytest.fail("network should be mocked"),
    )
    local = _control_record(
        lifecycle_status="INTERRUPTED",
        availability="AVAILABLE",
        attributes={
            "side": "BASELINE",
            "validity": "INVALID",
            "evidence_qualifier": "EXACT",
        },
    )
    checks = iter(
        [
            {"missing": [local], "unexpected": [], "mismatched": []},
            {"missing": [], "unexpected": [], "mismatched": []},
        ]
    )
    reconciled = []
    monkeypatch.setattr(
        client,
        "check_control_projection",
        lambda _issue_id, _expected: next(checks),
    )

    def reconcile(_issue_id, *, expected_record_revision, record):
        reconciled.append((expected_record_revision, dict(record)))
        return {"record_revision": expected_record_revision + 1}

    monkeypatch.setattr(client, "reconcile_control_record", reconcile)

    assert client.reconcile_control_projection("issue-1", [local]) == {
        "missing": [],
        "unexpected": [],
        "mismatched": [],
    }
    assert [record["lifecycle_status"] for _, record in reconciled] == [
        "QUEUED",
        "RUNNING",
        "INTERRUPTED",
    ]
    assert [revision for revision, _ in reconciled] == [0, 1, 2]
    assert reconciled[0][1]["availability"] is None
    assert reconciled[0][1]["attributes"]["validity"] == "UNKNOWN"
    assert reconciled[0][1]["content_digest"] != local["content_digest"]
    assert (
        reconciled[0][1]["content_digest"]
        == reconciled[1][1]["content_digest"]
    )
    assert reconciled[2][1]["availability"] == "AVAILABLE"
    assert reconciled[2][1]["attributes"]["validity"] == "INVALID"


def test_control_contract_accepts_local_terminal_and_qualitative_values(config):
    captured = []
    client = MonitoringAdminClient(
        config,
        OperatorSession("jwt", "admin", "admin@example.com", time.time() + 60),
        request=lambda _method, _url, **kwargs: (
            captured.append(kwargs["json"]["record"])
            or FakeResponse(
                200,
                {
                    "ok": True,
                    "result": {
                        "disposition": "created",
                        "record_revision": 1,
                    },
                },
            )
        ),
    )

    client.reconcile_control_record(
        "018f47a0-1111-7111-8111-111111111111",
        expected_record_revision=0,
        record=_control_record(
            lifecycle_status="INTERRUPTED",
            availability="INCOMPATIBLE",
            attributes={
                "side": "BASELINE",
                "validity": "INVALID",
                "evidence_qualifier": "SUBSTITUTE_INCLUDED",
            },
        ),
    )
    client.reconcile_control_record(
        "018f47a0-1111-7111-8111-111111111111",
        expected_record_revision=0,
        record={
            "record_kind": "COMPARISON",
            "record_id": "comparison_0123456789abcdef0123456789abcdef",
            "lifecycle_status": "CREATED",
            "content_digest": "d" * 64,
            "availability": None,
            "references": {"case_contract_id": "b" * 64},
            "attributes": {"verdict": "NOT_IMPROVED"},
        },
    )
    client.reconcile_control_record(
        "018f47a0-1111-7111-8111-111111111111",
        expected_record_revision=0,
        record={
            "record_kind": "CASE",
            "record_id": "case_0123456789abcdef0123456789abcdef",
            "lifecycle_status": "DRAFT",
            "content_digest": "e" * 64,
            "availability": None,
            "references": {
                "fixture_revision_id": "fixture_0123456789abcdef0123456789abcdef",
                "fixed_snapshot_revision_id": "snapshot_0123456789abcdef0123456789abcdef",
            },
            "attributes": {},
        },
    )

    assert captured[0]["lifecycle_status"] == "INTERRUPTED"
    assert captured[1]["attributes"]["verdict"] == "NOT_IMPROVED"
    assert captured[2]["lifecycle_status"] == "DRAFT"
