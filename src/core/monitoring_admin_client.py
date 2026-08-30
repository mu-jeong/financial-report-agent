"""Memory-only Supabase Auth and administrator API client.

The public publishable key identifies the application. A short-lived user JWT
identifies the operator. Refresh tokens are intentionally discarded and never
persisted by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests


DEFAULT_TIMEOUT_SECONDS = 12.0
_TRANSIENT_GATEWAY_STATUS_CODES = frozenset({502, 503, 504})
_SAFE_GET_ATTEMPTS = 2
_CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_STATUSES = {
    "FIXTURE": frozenset({"DRAFT", "READY"}),
    "CASE": frozenset({"DRAFT", "READY"}),
    "FIXED_SNAPSHOT": frozenset({"READY"}),
    "RELEASE": frozenset({"REGISTERED"}),
    "RUN": frozenset(
        {"QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
    ),
    "COMPARISON": frozenset({"CREATED"}),
}
_CONTROL_AVAILABILITY = frozenset(
    {"AVAILABLE", "MISSING", "CORRUPT", "INCOMPATIBLE", "UNKNOWN"}
)
_CONTROL_REFERENCE_KEYS = frozenset(
    {
        "fixture_revision_id",
        "fixed_snapshot_revision_id",
        "release_manifest_id",
        "case_contract_id",
        "supersedes_comparison_id",
    }
)
_CONTROL_REFERENCE_KEYS_BY_KIND = {
    "FIXTURE": frozenset(),
    "CASE": frozenset(
        {"fixture_revision_id", "fixed_snapshot_revision_id", "case_contract_id"}
    ),
    "FIXED_SNAPSHOT": frozenset(),
    "RELEASE": frozenset(),
    "RUN": frozenset(
        {"fixed_snapshot_revision_id", "release_manifest_id", "case_contract_id"}
    ),
    "COMPARISON": frozenset(
        {
            "case_contract_id",
            "supersedes_comparison_id",
        }
    ),
}
_CONTROL_ATTRIBUTE_KEYS = frozenset(
    {"side", "validity", "verdict", "evidence_qualifier"}
)
_CONTROL_ATTRIBUTE_VALUES = {
    "side": frozenset({"BASELINE", "CANDIDATE"}),
    "validity": frozenset({"VALID", "INVALID", "UNKNOWN"}),
    "verdict": frozenset(
        {
            "IMPROVED",
            "NOT_IMPROVED",
            "REGRESSED",
            "INCONCLUSIVE",
        }
    ),
    "evidence_qualifier": frozenset(
        {"EXACT", "PARTIAL", "SUBSTITUTE_INCLUDED", "UNKNOWN"}
    ),
}
_CONTROL_ATTRIBUTE_KEYS_BY_KIND = {
    "FIXTURE": frozenset(),
    "CASE": frozenset({"evidence_qualifier"}),
    "FIXED_SNAPSHOT": frozenset(),
    "RELEASE": frozenset(),
    "RUN": frozenset({"side", "validity", "evidence_qualifier"}),
    "COMPARISON": frozenset({"verdict"}),
}
_TERMINAL_RUN_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
)
_ISSUE_STATES = frozenset(
    {"OPEN", "IN_PROGRESS", "RESOLVED", "NOT_ISSUE", "CLOSED"}
)
_ISSUE_ACTION_BY_TARGET = {
    "OPEN": "reopen",
    "IN_PROGRESS": "start",
    "RESOLVED": "resolve",
    "NOT_ISSUE": "dismiss",
}


def _run_bridge_record(
    record: Mapping[str, Any], *, lifecycle_status: str
) -> dict[str, Any]:
    """Build a metadata-only pre-terminal Run record for recovery replay."""

    bridged = dict(record)
    bridged["lifecycle_status"] = lifecycle_status
    bridged["availability"] = None
    attributes = dict(bridged.get("attributes") or {})
    attributes["validity"] = "UNKNOWN"
    bridged["attributes"] = attributes
    references = dict(bridged.get("references") or {})
    bridged["content_digest"] = hashlib.sha256(
        json.dumps(
            {
                "record_kind": "RUN",
                "run_id": bridged["record_id"],
                "case_contract_id": references["case_contract_id"],
                "release_manifest_id": references["release_manifest_id"],
                "side": attributes["side"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return _validate_control_record(bridged)


class OperatorApiError(RuntimeError):
    """Base error returned by the administrator boundary."""

    def __init__(self, code: str, *, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class OperatorUnauthorizedError(OperatorApiError):
    """The login or short-lived access token is not valid."""


class OperatorForbiddenError(OperatorApiError):
    """The authenticated user is not an active monitoring administrator."""


class OperatorConflictError(OperatorApiError):
    """An Issue or reconciliation record changed since it was loaded."""


def _valid_service_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    if (
        parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    return parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def _service_origin(value: str) -> tuple[str, str, int] | None:
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.hostname is None:
        return None
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.casefold(), effective_port


def _valid_operator_endpoint_pair(
    project_url: str | None,
    function_url: str | None,
) -> bool:
    if not _valid_service_url(project_url) or not _valid_service_url(function_url):
        return False
    assert project_url is not None and function_url is not None
    project = urlparse(project_url)
    function = urlparse(function_url)
    return bool(
        project.path.rstrip("/") == ""
        and function.path.rstrip("/")
        == "/functions/v1/issue-report-operator"
        and _service_origin(project_url) == _service_origin(function_url)
    )


@dataclass(frozen=True, slots=True)
class OperatorApiConfig:
    project_url: str | None
    publishable_key: str | None
    function_url: str | None
    artifact_root: Path
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        return bool(
            _valid_operator_endpoint_pair(self.project_url, self.function_url)
            and self.publishable_key
            and self.publishable_key.startswith("sb_publishable_")
            and 0 < float(self.timeout_seconds) <= 60
        )


@dataclass(frozen=True, slots=True)
class OperatorSession:
    access_token: str = field(repr=False)
    user_id: str
    email: str
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


def _response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise OperatorApiError(
            "invalid_server_response",
            status_code=getattr(response, "status_code", None),
        ) from exc
    if not isinstance(payload, Mapping):
        raise OperatorApiError(
            "invalid_server_response",
            status_code=getattr(response, "status_code", None),
        )
    return dict(payload)


def sign_in_with_password(
    config: OperatorApiConfig,
    *,
    email: str,
    password: str,
    post: Callable[..., Any] = requests.post,
    now: float | None = None,
) -> OperatorSession:
    """Sign in once and discard the returned refresh token."""

    if not config.configured:
        raise OperatorApiError("operator_api_not_configured")
    normalized_email = str(email or "").strip()
    if not normalized_email or not password:
        raise OperatorUnauthorizedError("login failed", status_code=401)
    assert config.project_url is not None
    assert config.publishable_key is not None
    try:
        response = post(
            f"{config.project_url.rstrip('/')}/auth/v1/token?grant_type=password",
            headers={
                "apikey": config.publishable_key,
                "content-type": "application/json",
            },
            json={"email": normalized_email, "password": password},
            timeout=float(config.timeout_seconds),
        )
    except requests.RequestException as exc:
        raise OperatorApiError("auth_unavailable") from exc
    payload = _response_payload(response)
    if int(response.status_code) != 200:
        raise OperatorUnauthorizedError("login failed", status_code=401)
    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    user = payload.get("user")
    if (
        not isinstance(access_token, str)
        or not access_token
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, (int, float))
        or float(expires_in) <= 0
        or not isinstance(user, Mapping)
        or not isinstance(user.get("id"), str)
        or not isinstance(user.get("email"), str)
    ):
        raise OperatorApiError("invalid_auth_response")
    # The payload may also contain refresh_token. It is deliberately not copied.
    return OperatorSession(
        access_token=access_token,
        user_id=str(user["id"]),
        email=str(user["email"]),
        expires_at=(time.time() if now is None else float(now)) + float(expires_in),
    )


def load_operator_api_config() -> OperatorApiConfig:
    """Load non-secret public endpoints and the local managed-root path."""

    from src.configs import config as config_module

    return OperatorApiConfig(
        project_url=config_module.MONITORING_SUPABASE_URL,
        publishable_key=config_module.MONITORING_SUPABASE_PUBLISHABLE_KEY,
        function_url=config_module.MONITORING_OPERATOR_API_URL,
        artifact_root=Path(config_module.MONITORING_ARTIFACT_ROOT).expanduser(),
    )


def production_monitoring_enabled(
    *,
    deployment_environment: str,
    monitoring_mode: bool,
    config: OperatorApiConfig,
) -> bool:
    """Return whether the production-only operator surface may be exposed."""

    return (
        str(deployment_environment).strip().lower() == "production"
        and bool(monitoring_mode)
        and config.configured
    )


class MonitoringAdminClient:
    """Small requests-based client for the authenticated Edge Function."""

    def __init__(
        self,
        config: OperatorApiConfig,
        session: OperatorSession,
        *,
        request: Callable[..., Any] = requests.request,
    ) -> None:
        if not config.configured:
            raise OperatorApiError("operator_api_not_configured")
        self.config = config
        self.session = session
        self._request_fn = request

    def _headers(self) -> dict[str, str]:
        assert self.config.publishable_key is not None
        return {
            "apikey": self.config.publishable_key,
            "Authorization": f"Bearer {self.session.access_token}",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.session.expired:
            raise OperatorUnauthorizedError("session_expired", status_code=401)
        assert self.config.function_url is not None
        kwargs: dict[str, Any] = {
            "headers": self._headers(),
            "timeout": float(self.config.timeout_seconds),
        }
        if params is not None:
            kwargs["params"] = dict(params)
        if json_body is not None:
            kwargs["json"] = dict(json_body)
        request_url = (
            f"{self.config.function_url.rstrip('/')}/{path.lstrip('/')}"
        )
        attempts = _SAFE_GET_ATTEMPTS if method.upper() == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self._request_fn(
                    method,
                    request_url,
                    **kwargs,
                )
            except requests.RequestException as exc:
                raise OperatorApiError("operator_api_unavailable") from exc
            status_code = int(response.status_code)
            if (
                status_code in _TRANSIENT_GATEWAY_STATUS_CODES
                and attempt + 1 < attempts
            ):
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                continue
            try:
                payload = _response_payload(response)
            except OperatorApiError as exc:
                if status_code in _TRANSIENT_GATEWAY_STATUS_CODES:
                    raise OperatorApiError(
                        "operator_api_unavailable",
                        status_code=status_code,
                    ) from exc
                raise
            break
        if 200 <= status_code < 300 and payload.get("ok") is True:
            return payload
        code = str(payload.get("code") or "operator_api_error")
        if status_code == 401:
            raise OperatorUnauthorizedError(code, status_code=status_code)
        if status_code == 403:
            raise OperatorForbiddenError(code, status_code=status_code)
        if status_code == 409:
            raise OperatorConflictError(code, status_code=status_code)
        raise OperatorApiError(code, status_code=status_code)

    def list_issues(
        self,
        *,
        state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized_state = str(state).upper() if state else None
        if normalized_state is not None and normalized_state not in _ISSUE_STATES:
            raise ValueError("state must be a supported Issue lifecycle state")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        payload = self._request(
            "GET",
            "issues",
            params={"state": normalized_state, "limit": limit},
        )
        issues = payload.get("issues")
        if not isinstance(issues, list) or not all(
            isinstance(item, Mapping) for item in issues
        ):
            raise OperatorApiError("invalid_server_response")
        return [dict(item) for item in issues]

    def get_issue(self, issue_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"issues/{issue_id}")
        issue = payload.get("issue")
        if not isinstance(issue, Mapping):
            raise OperatorApiError("invalid_server_response")
        return dict(issue)

    def view_raw(self, issue_id: str) -> dict[str, Any]:
        payload = self._request("POST", f"issues/{issue_id}/raw")
        report = payload.get("raw_report")
        if not isinstance(report, Mapping):
            raise OperatorApiError("raw_unavailable", status_code=410)
        return dict(report)

    def transition_issue(
        self,
        issue_id: str,
        *,
        target_state: str,
        expected_record_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        state = str(target_state).upper()
        if state not in _ISSUE_ACTION_BY_TARGET:
            raise ValueError(
                "target_state must be a writable Issue lifecycle state"
            )
        if (
            isinstance(expected_record_revision, bool)
            or not isinstance(expected_record_revision, int)
            or expected_record_revision < 1
        ):
            raise ValueError("expected_record_revision must be positive")
        reason_text = str(reason or "").strip()
        if not reason_text or len(reason_text) > 2000:
            raise ValueError("reason must contain 1 to 2000 characters")
        action = _ISSUE_ACTION_BY_TARGET[state]
        payload = self._request(
            "POST",
            f"issues/{issue_id}/{action}",
            json_body={
                "expected_record_revision": expected_record_revision,
                "reason": reason_text,
            },
        )
        issue = payload.get("issue")
        if not isinstance(issue, Mapping):
            raise OperatorApiError("invalid_server_response")
        return dict(issue)

    def list_control_records(self, issue_id: str) -> list[dict[str, Any]]:
        """Read the metadata-only reconciliation projection for one Issue."""

        payload = self._request("GET", f"issues/{issue_id}/control")
        records = payload.get("records")
        if not isinstance(records, list) or not all(
            isinstance(item, Mapping) for item in records
        ):
            raise OperatorApiError("invalid_server_response")
        return [dict(item) for item in records]

    def reconcile_control_record(
        self,
        issue_id: str,
        *,
        expected_record_revision: int,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create, idempotently confirm, or CAS-update one canonical record."""

        if (
            isinstance(expected_record_revision, bool)
            or not isinstance(expected_record_revision, int)
            or expected_record_revision < 0
        ):
            raise ValueError("expected_record_revision must be non-negative")
        canonical = _validate_control_record(record)
        payload = self._request(
            "PUT",
            f"issues/{issue_id}/control",
            json_body={
                "expected_record_revision": expected_record_revision,
                "record": canonical,
            },
        )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise OperatorApiError("invalid_server_response")
        return dict(result)

    def check_control_projection(
        self,
        issue_id: str,
        expected_records: list[Mapping[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Compare local canonical metadata with Supabase without mutating either side."""

        expected = [_validate_control_record(item) for item in expected_records]
        actual = self.list_control_records(issue_id)
        expected_by_key = {
            (item["record_kind"], item["record_id"]): item for item in expected
        }
        if len(expected_by_key) != len(expected):
            raise ValueError("expected_records contains duplicate record identities")
        actual_by_key = {
            (str(item.get("record_kind")), str(item.get("record_id"))): item
            for item in actual
        }
        missing = [
            item for key, item in expected_by_key.items() if key not in actual_by_key
        ]
        unexpected = [
            dict(item) for key, item in actual_by_key.items() if key not in expected_by_key
        ]
        mismatched: list[dict[str, Any]] = []
        for key, local in expected_by_key.items():
            remote = actual_by_key.get(key)
            if remote is None:
                continue
            remote_projection = {field: remote.get(field) for field in local}
            if remote_projection != local:
                mismatched.append({"local": local, "remote": dict(remote)})
        return {
            "missing": missing,
            "unexpected": unexpected,
            "mismatched": mismatched,
        }

    def reconcile_control_projection(
        self,
        issue_id: str,
        expected_records: list[Mapping[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """CAS-reconcile expected metadata, then return any unresolved drift.

        Unexpected remote identities are never removed or overwritten. A Run
        that was not mirrored before an operator-process interruption is
        replayed as QUEUED -> RUNNING -> terminal. Existing QUEUED Runs also
        pass through RUNNING before a terminal state is written, preserving an
        explicit remote lifecycle without uploading local artifacts.
        """

        expected = [_validate_control_record(item) for item in expected_records]
        diff = self.check_control_projection(issue_id, expected)
        if diff["unexpected"]:
            return diff

        for record in diff["missing"]:
            if (
                record["record_kind"] == "RUN"
                and record["lifecycle_status"] != "QUEUED"
            ):
                queued = _run_bridge_record(record, lifecycle_status="QUEUED")
                result = self.reconcile_control_record(
                    issue_id,
                    expected_record_revision=0,
                    record=queued,
                )
                revision = result.get("record_revision")
                if (
                    isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or revision < 1
                ):
                    raise OperatorApiError("invalid_server_response")
                running = _run_bridge_record(record, lifecycle_status="RUNNING")
                result = self.reconcile_control_record(
                    issue_id,
                    expected_record_revision=revision,
                    record=running,
                )
                revision = result.get("record_revision")
                if (
                    isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or revision < 1
                ):
                    raise OperatorApiError("invalid_server_response")
                if record["lifecycle_status"] in _TERMINAL_RUN_STATUSES:
                    self.reconcile_control_record(
                        issue_id,
                        expected_record_revision=revision,
                        record=record,
                    )
            else:
                self.reconcile_control_record(
                    issue_id,
                    expected_record_revision=0,
                    record=record,
                )

        for mismatch in diff["mismatched"]:
            local = dict(mismatch["local"])
            remote = dict(mismatch["remote"])
            revision = remote.get("record_revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise OperatorApiError("invalid_server_response")
            if (
                local["record_kind"] == "RUN"
                and remote.get("lifecycle_status") == "QUEUED"
                and local["lifecycle_status"] in _TERMINAL_RUN_STATUSES
            ):
                bridge = {
                    field: remote.get(field)
                    for field in (
                        "record_kind",
                        "record_id",
                        "lifecycle_status",
                        "content_digest",
                        "availability",
                        "references",
                        "attributes",
                    )
                }
                bridge["lifecycle_status"] = "RUNNING"
                result = self.reconcile_control_record(
                    issue_id,
                    expected_record_revision=revision,
                    record=bridge,
                )
                revision = result.get("record_revision")
                if (
                    isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or revision < 1
                ):
                    raise OperatorApiError("invalid_server_response")
            self.reconcile_control_record(
                issue_id,
                expected_record_revision=revision,
                record=local,
            )

        return self.check_control_projection(issue_id, expected)


def _validate_control_record(record: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "record_kind",
        "record_id",
        "lifecycle_status",
        "content_digest",
        "availability",
        "references",
        "attributes",
    }
    if set(record) != required:
        raise ValueError("control record fields do not match the canonical contract")
    kind = record["record_kind"]
    record_id = record["record_id"]
    status = record["lifecycle_status"]
    digest = record["content_digest"]
    availability = record["availability"]
    references = record["references"]
    attributes = record["attributes"]
    if (
        not isinstance(kind, str)
        or not isinstance(status, str)
        or kind not in _CONTROL_STATUSES
        or status not in _CONTROL_STATUSES[kind]
    ):
        raise ValueError("invalid control record kind or lifecycle_status")
    if not isinstance(record_id, str) or not _CONTROL_ID_RE.fullmatch(record_id):
        raise ValueError("invalid control record_id")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise ValueError("invalid control content_digest")
    if availability is not None and (
        not isinstance(availability, str)
        or availability not in _CONTROL_AVAILABILITY
    ):
        raise ValueError("invalid control availability")
    if not isinstance(references, Mapping) or not set(references).issubset(
        _CONTROL_REFERENCE_KEYS
    ) or not set(references).issubset(_CONTROL_REFERENCE_KEYS_BY_KIND[kind]):
        raise ValueError("invalid control references")
    canonical_references: dict[str, str] = {}
    for key, value in references.items():
        if isinstance(value, str) and _CONTROL_ID_RE.fullmatch(value):
            canonical_references[str(key)] = value
        else:
            raise ValueError("invalid control reference value")
    if not isinstance(attributes, Mapping) or not set(attributes).issubset(
        _CONTROL_ATTRIBUTE_KEYS
    ) or not set(attributes).issubset(_CONTROL_ATTRIBUTE_KEYS_BY_KIND[kind]):
        raise ValueError("invalid control attributes")
    canonical_attributes: dict[str, str] = {}
    for key, value in attributes.items():
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 32
            or not re.fullmatch(r"[A-Z_]+", value)
            or value not in _CONTROL_ATTRIBUTE_VALUES[str(key)]
        ):
            raise ValueError("invalid control attribute value")
        canonical_attributes[str(key)] = value
    if kind in {"FIXTURE", "CASE", "COMPARISON"} and availability is not None:
        raise ValueError("non-artifact control availability must be null")
    if status == "READY" and kind == "CASE":
        if set(canonical_references) != {
            "fixture_revision_id",
            "fixed_snapshot_revision_id",
            "case_contract_id",
        } or set(canonical_attributes) != {"evidence_qualifier"}:
            raise ValueError("ready Case control metadata is incomplete")
    if kind == "COMPARISON":
        if "case_contract_id" not in canonical_references or set(
            canonical_attributes
        ) != {"verdict"}:
            raise ValueError("Comparison control metadata is incomplete")
    if kind == "RUN":
        if set(canonical_references) != {
            "fixed_snapshot_revision_id",
            "release_manifest_id",
            "case_contract_id",
        } or set(canonical_attributes) != {
            "side",
            "validity",
            "evidence_qualifier",
        }:
            raise ValueError("Run control metadata is incomplete")
        validity = canonical_attributes["validity"]
        if status in {"QUEUED", "RUNNING"} and (
            validity != "UNKNOWN" or availability is not None
        ):
            raise ValueError("non-terminal Run control metadata is invalid")
        if status == "SUCCEEDED" and validity not in {"VALID", "INVALID"}:
            raise ValueError("successful Run validity is invalid")
        if status in {"FAILED", "CANCELLED", "INTERRUPTED"} and validity != "INVALID":
            raise ValueError("unsuccessful Run must be INVALID")
    return {
        "record_kind": kind,
        "record_id": record_id,
        "lifecycle_status": status,
        "content_digest": digest,
        "availability": availability,
        "references": canonical_references,
        "attributes": canonical_attributes,
    }


__all__ = [
    "MonitoringAdminClient",
    "OperatorApiConfig",
    "OperatorApiError",
    "OperatorConflictError",
    "OperatorForbiddenError",
    "OperatorSession",
    "OperatorUnauthorizedError",
    "load_operator_api_config",
    "production_monitoring_enabled",
    "sign_in_with_password",
]
