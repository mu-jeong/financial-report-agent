"""Safe, local-only Codex handoff artifacts for feedback candidates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core import artifact_io
from src.core.artifact_io import (
    contains_sensitive_identifier_pattern,
    safe_artifact_token,
    strict_json_loads,
)
from src.core.answer_requirements import (
    AnswerRequirementValidationError,
    canonicalize_answer_requirements,
)
from src.core.monitoring import (
    CandidateValidationError,
    canonicalize_regression_candidate,
    compute_evaluation_run_hash,
    is_current_candidate_contract,
    load_evaluation_run,
    validate_completed_candidate_run_evidence,
)
from src.core.reproduction_manifest import (
    ReproductionManifestError,
    canonicalize_reproduction_manifest,
)

DEFAULT_CODEX_VERIFICATION_COMMANDS = (
    r".\.venv\Scripts\python.exe -m pytest tests/test_feedback_loop.py -q",
    r".\.venv\Scripts\python.exe -m pytest tests/test_monitoring.py -q",
)

_AUTOMATIC_CHECKS = {
    "answer_requirements_pass",
    "route_pass",
    "filter_pass",
    "source_hit",
    "citation_valid",
    "latency_pass",
    "no_result_absent",
    "expected_state_pass",
}
_HANDOFF_HARD_CHECKS = _AUTOMATIC_CHECKS | {
    "manual_assertions_pass",
    "performance_p95_pass",
}
_SOFT_OBJECTIVES = {
    "latency_p95",
    "answer_conciseness",
    "answer_depth",
}
_FILTER_KEYS = {
    "target_name",
    "report_type",
    "report_date",
    "report_date_start",
    "report_date_end",
    "broker",
    "file_names",
}
_STATE_KEYS = {
    "followup_scope_intent",
    "scope_source",
    "scope_decision_reason",
    "scope_decision_matched_section_id",
}
_ANSWER_REQUIREMENT_KEYS = {
    "id",
    "description",
    "answer_terms_any",
    "source_terms_any",
    "require_citation",
}
_SOURCE_KEYS = {"file_name", "report_type"}
_PAYLOAD_KEYS = {
    "handoff_schema_version",
    "candidate",
    "goal",
    "user_impact",
    "provenance",
    "reproduction",
    "observed",
    "expected",
    "acceptance",
    "verification",
}
_PAYLOAD_V2_KEYS = _PAYLOAD_KEYS | {
    "quality",
    "reproduction_manifest",
}
_MANIFEST_KEYS = {
    "handoff_schema_version",
    "kind",
    "handoff_id",
    "candidate_id",
    "contract_revision",
    "candidate_hash",
    "baseline_run_id",
    "baseline_run_hash",
    "created_at",
    "approved_by",
    "approval_reason",
    "markdown_filename",
    "payload",
    "payload_sha256",
    "markdown_sha256",
    "manifest_sha256",
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HANDOFF_ID = re.compile(r"^handoff_[0-9a-f]{12,32}$")
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
_ASSERTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_COMMAND_CREDENTIAL = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
)
_COMMAND_SHELL_META = re.compile(r"[|&;<>`$^%!()]")
_RAW_ERROR_DETAIL = re.compile(
    r"(?im)(?:"
    r"^\s*Traceback \(most recent call last\):"
    r"|^\s*File\s+\"[^\"]+\",\s*line\s+\d+"
    r"|^\s*at\s+[\w.$<>]+\([^)\r\n]*:\d+\)"
    r"|^\s*(?:Caused by:\s*)?[\w.]+(?:Error|Exception)\s*:"
    r"|\bmetadata\.error\b\s*[:=]"
    r")"
)
_REDACTIONS = (
    ("credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")),
    ("credential", re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b")),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
            r"\s*[:=]\s*[^\s&#]+"
        ),
    ),
    ("email", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
    (
        "phone",
        re.compile(r"(?<!\w)(?:\+?82[-.\s]?)?0?1[016789](?:[-.\s]?\d){7,8}(?!\w)"),
    ),
    ("phone", re.compile(r"(?<!\w)\+\d{1,3}(?:[-.\s]?\d){7,14}(?!\w)")),
    ("url_credential", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    (
        "url_secret",
        re.compile(
            r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s?#]+\?[^\s#]*?"
            r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret)=)"
            r"[^&#\s]+"
        ),
    ),
    (
        "path",
        re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/][^\r\n,;]*"),
    ),
    ("path", re.compile(r"(?<![\\/])\\\\[^\r\n,;]*")),
    (
        "path",
        re.compile(
            r"(?<![:/\w])/[^\s/,\r\n;]+(?:/[^\r\n,;]*)?"
        ),
    ),
)


class FeedbackHandoffError(RuntimeError):
    """Raised when a handoff violates its safety or integrity contract."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        super().__init__(code if not detail else f"{code}: {detail}")


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise FeedbackHandoffError(
            "invalid_payload",
            "non-finite or unsupported JSON value",
        ) from exc
    return encoded.encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _markdown_disk_bytes(markdown: str) -> bytes:
    """Return the platform-independent bytes written by atomic_write_text."""

    return markdown.encode("utf-8")


def redact_handoff_text(value: str) -> str:
    """Redact raw error details, identifiers, credentials, and local paths."""

    redacted = str(value)
    if _RAW_ERROR_DETAIL.search(redacted):
        return "[REDACTED:error_detail]"
    for kind, pattern in _REDACTIONS:
        replacement = f"[REDACTED:{kind}]"
        if kind == "url_secret":
            redacted = pattern.sub(lambda match: match.group(1) + replacement, redacted)
        else:
            redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_tree(value: Any) -> Any:
    if isinstance(value, str):
        return redact_handoff_text(value)
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _redact_tree(item) for key, item in value.items()}
    return value


def _require_exact_keys(
    value: Any, allowed: set[str], label: str, *, required: set[str] | None = None
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FeedbackHandoffError("invalid_payload", f"{label} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise FeedbackHandoffError(
            "invalid_payload", f"{label} has unsupported keys: {sorted(unknown)}"
        )
    required_keys = allowed if required is None else required
    missing = required_keys - set(value)
    if missing:
        raise FeedbackHandoffError(
            "invalid_payload", f"{label} is missing keys: {sorted(missing)}"
        )
    return value


def _require_string(value: Any, label: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise FeedbackHandoffError("invalid_payload", f"{label} must be a string")
    return value


def _validate_hash(value: Any, label: str) -> None:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise FeedbackHandoffError("invalid_payload", f"{label} is not sha256")


def _validate_timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise FeedbackHandoffError("invalid_payload", f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackHandoffError("invalid_payload", f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise FeedbackHandoffError("invalid_payload", f"{label} needs timezone")


def _validate_command(command: Any) -> str:
    value = _require_string(command, "verification command", allow_empty=False)
    if (
        "\n" in value
        or "\r" in value
        or _COMMAND_SHELL_META.search(value)
        or _COMMAND_CREDENTIAL.search(value)
        or re.match(r"(?i)^[A-Z]:[\\/]", value)
        or value.startswith(("/", "\\\\"))
        or contains_sensitive_identifier_pattern(value)
    ):
        raise FeedbackHandoffError("invalid_verification_command")
    return value


def _copy_allowed_mapping(
    value: Any, keys: set[str], label: str
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FeedbackHandoffError(
            "invalid_payload", f"{label} must be an object"
        )
    mapping = value
    return {key: mapping[key] for key in keys if key in mapping}


def _copy_prior_search_scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"search_filters": {}}
    result: dict[str, Any] = {}
    if value.get("route") in {"vectordb", "rdb"}:
        result["route"] = value["route"]
    result["search_filters"] = _copy_allowed_mapping(
        value.get("search_filters"),
        _FILTER_KEYS,
        "reproduction.prior_search_scope.search_filters",
    )
    file_names = value.get("file_names")
    if isinstance(file_names, list):
        result["file_names"] = [
            str(item) for item in file_names if isinstance(item, str)
        ][:20]
    answer_scope_index = value.get("answer_scope_index")
    sections = (
        answer_scope_index.get("sections")
        if isinstance(answer_scope_index, Mapping)
        else []
    )
    if isinstance(sections, list):
        result["answer_scope_sections"] = [
            {
                "id": str(section.get("id") or ""),
                "label": str(section.get("label") or ""),
                "filters": _copy_allowed_mapping(
                    section.get("filters"),
                    _FILTER_KEYS,
                    "reproduction.prior_search_scope.sections[].filters",
                ),
                "file_names": [
                    str(item)
                    for item in section.get("file_names") or []
                    if isinstance(item, str)
                ][:20],
            }
            for section in sections
            if isinstance(section, Mapping)
        ]
    return result


def _failed_checks(run: Mapping[str, Any], active_checks: Sequence[str]) -> list[str]:
    summary = run.get("summary")
    if isinstance(summary, Mapping) and isinstance(
        summary.get("hard_failed_checks"),
        list,
    ):
        return [
            check
            for check in active_checks
            if check in summary["hard_failed_checks"]
        ]
    results = run.get("results")
    result = results[0] if isinstance(results, list) and results else {}
    if not isinstance(result, Mapping):
        return []
    failed: list[str] = []
    for check in active_checks:
        field = "no_result" if check == "no_result_absent" else check
        passed = not bool(result.get(field)) if check == "no_result_absent" else result.get(field)
        if passed is not True:
            failed.append(check)
    return failed


def _validate_baseline(
    candidate: Mapping[str, Any], baseline_run: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    canonical = canonicalize_regression_candidate(candidate)
    if canonical.get("triage_status") not in {"reproduced", "fixing"}:
        raise FeedbackHandoffError("handoff_not_available")
    if not (
        set(canonical.get("active_checks") or [])
        & _AUTOMATIC_CHECKS
    ):
        raise FeedbackHandoffError("unsupported_verification_type")
    if not canonical.get("expected_approved_at"):
        raise FeedbackHandoffError("expectation_not_approved")
    if (
        baseline_run.get("integrity_status") != "valid"
        or baseline_run.get("run_kind") != "baseline"
        or baseline_run.get("run_status") != "completed"
        or not is_current_candidate_contract(canonical, baseline_run)
        or baseline_run.get("run_hash") != compute_evaluation_run_hash(baseline_run)
    ):
        raise FeedbackHandoffError("invalid_baseline")
    if not _HEX64.fullmatch(str(baseline_run.get("run_hash") or "")):
        raise FeedbackHandoffError("invalid_baseline")
    try:
        validate_completed_candidate_run_evidence(baseline_run)
    except CandidateValidationError as exc:
        raise FeedbackHandoffError("invalid_baseline") from exc
    run_id = str(baseline_run.get("run_id") or "")
    matching_refs = [
        ref
        for ref in (canonical.get("evidence") or {}).get("baseline_runs") or []
        if ref.get("run_id") == run_id
        and ref.get("run_hash") == baseline_run.get("run_hash")
        and ref.get("status") == "fail"
        and is_current_candidate_contract(canonical, ref)
    ]
    if not matching_refs:
        raise FeedbackHandoffError("baseline_not_linked")
    failed = _failed_checks(baseline_run, canonical.get("active_checks") or [])
    if not failed:
        raise FeedbackHandoffError("baseline_must_fail")
    return canonical, dict(matching_refs[0])


def build_codex_handoff_payload(
    candidate: Mapping[str, Any],
    baseline_run: Mapping[str, Any],
    *,
    verification_commands: Sequence[str] = DEFAULT_CODEX_VERIFICATION_COMMANDS,
) -> dict[str, Any]:
    """Build the exact allowlisted handoff payload from a failed baseline."""

    canonical, _ = _validate_baseline(candidate, baseline_run)
    commands = [_validate_command(command) for command in verification_commands]
    if not commands:
        raise FeedbackHandoffError("invalid_verification_command")

    observed = canonical.get("observed") or {}
    actual = observed.get("actual") or {}
    expected = canonical.get("expected") or {}
    provenance = baseline_run.get("provenance") or {}
    payload = {
        "handoff_schema_version": 1,
        "candidate": {
            "id": safe_artifact_token(str(canonical["id"])),
            "contract_revision": int(canonical["contract_revision"]),
            "candidate_hash": canonical["candidate_hash"],
            "severity": canonical.get("severity"),
            "impact_area": canonical.get("impact_area"),
            "verification_type": canonical.get("verification_type"),
        },
        "goal": {
            "summary": (
                "Make the approved checks pass for candidate "
                f"{safe_artifact_token(str(canonical['id']))}."
            )
        },
        "user_impact": {"summary": canonical.get("impact_summary") or ""},
        "provenance": {
            "app_version": baseline_run.get("app_version"),
            "backend_mode": provenance.get("backend_mode"),
            "snapshot_id": provenance.get("snapshot_id"),
            "data_revision": provenance.get("data_revision"),
            "config_fingerprint": provenance.get("config_fingerprint"),
        },
        "reproduction": {
            "question": (observed.get("reproduction_input") or {}).get("question")
            or ""
        },
        "observed": {
            "route": actual.get("route"),
            "filters": _copy_allowed_mapping(actual.get("filters"), _FILTER_KEYS, "observed.filters"),
            "sources": [
                _copy_allowed_mapping(source, _SOURCE_KEYS, "observed.sources[]")
                for source in actual.get("sources") or []
            ],
            "state": _copy_allowed_mapping(actual.get("state"), _STATE_KEYS, "observed.state"),
            "baseline_failed_checks": _failed_checks(
                baseline_run, canonical.get("active_checks") or []
            ),
        },
        "expected": {
            "route": expected.get("route"),
            "filters": _copy_allowed_mapping(expected.get("filters"), _FILTER_KEYS, "expected.filters"),
            "sources": [
                _copy_allowed_mapping(source, _SOURCE_KEYS, "expected.sources[]")
                for source in expected.get("sources") or []
            ],
            "state": _copy_allowed_mapping(expected.get("state"), _STATE_KEYS, "expected.state"),
            "manual_assertions": [
                _copy_allowed_mapping(
                    assertion, {"id", "text"}, "expected.manual_assertions[]"
                )
                for assertion in expected.get("manual_assertions") or []
            ],
            "answer_requirements": [
                _copy_allowed_mapping(
                    requirement,
                    _ANSWER_REQUIREMENT_KEYS,
                    "expected.answer_requirements[]",
                )
                for requirement in expected.get("answer_requirements") or []
            ],
        },
        "acceptance": {"active_checks": list(canonical.get("active_checks") or [])},
        "verification": {"commands": commands},
    }
    if int(canonical.get("contract_schema_version") or 1) >= 2:
        validation_plan = canonical.get("validation_plan") or {}
        reproduction_input = observed.get("reproduction_input") or {}
        payload["handoff_schema_version"] = 2
        payload["reproduction"] = {
            "question": reproduction_input.get("question") or "",
            "prior_search_scope": _copy_prior_search_scope(
                reproduction_input.get("prior_search_scope")
            ),
            "requires_prior_scope": bool(
                reproduction_input.get("requires_prior_scope")
            ),
        }
        payload["quality"] = {
            "profile": canonical.get("quality_profile"),
            "hard_checks": list(
                validation_plan.get("hard_checks") or []
            ),
            "soft_objectives": list(
                validation_plan.get("soft_objectives") or []
            ),
            "performance_budget": dict(
                validation_plan.get("performance_budget") or {}
            ),
        }
        payload["reproduction_manifest"] = dict(
            canonical.get("reproduction_manifest") or {}
        )
    redacted = _redact_tree(payload)
    validate_codex_handoff_payload(redacted)
    return redacted


def validate_codex_handoff_payload(payload: Mapping[str, Any]) -> None:
    """Validate the complete payload schema and every nested allowlist."""

    schema_version = (
        payload.get("handoff_schema_version")
        if isinstance(payload, Mapping)
        else None
    )
    if schema_version not in {1, 2}:
        raise FeedbackHandoffError("invalid_payload", "unsupported schema")
    root = _require_exact_keys(
        payload,
        _PAYLOAD_V2_KEYS
        if schema_version == 2
        else _PAYLOAD_KEYS,
        "payload",
    )

    candidate = _require_exact_keys(
        root["candidate"],
        {"id", "contract_revision", "candidate_hash", "severity", "impact_area", "verification_type"},
        "candidate",
    )
    if safe_artifact_token(_require_string(candidate["id"], "candidate.id")) != candidate["id"]:
        raise FeedbackHandoffError("invalid_payload", "candidate.id is unsafe")
    if not isinstance(candidate["contract_revision"], int) or candidate["contract_revision"] < 0:
        raise FeedbackHandoffError("invalid_payload", "contract_revision is invalid")
    _validate_hash(candidate["candidate_hash"], "candidate_hash")
    if candidate["severity"] not in {"S1", "S2", "S3", "S4"}:
        raise FeedbackHandoffError("invalid_payload", "severity is invalid")
    if candidate["impact_area"] not in {
        "routing", "filter_scope", "retrieval_source", "citation", "latency", "ui", "answer_quality"
    }:
        raise FeedbackHandoffError("invalid_payload", "impact_area is invalid")
    allowed_verification_types = (
        {"graph_contract", "mixed"}
        if schema_version == 2
        else {"graph_contract"}
    )
    if candidate["verification_type"] not in allowed_verification_types:
        raise FeedbackHandoffError("invalid_payload", "verification_type is invalid")

    for name in ("goal", "user_impact"):
        section = _require_exact_keys(root[name], {"summary"}, name)
        _require_string(section["summary"], f"{name}.summary")
    reproduction = _require_exact_keys(
        root["reproduction"],
        (
            {
                "question",
                "prior_search_scope",
                "requires_prior_scope",
            }
            if schema_version == 2
            else {"question"}
        ),
        "reproduction",
    )
    _require_string(reproduction["question"], "reproduction.question")
    if schema_version == 2:
        if not isinstance(
            reproduction["requires_prior_scope"],
            bool,
        ):
            raise FeedbackHandoffError(
                "invalid_payload",
                "requires_prior_scope is invalid",
            )
        prior_scope = _require_exact_keys(
            reproduction["prior_search_scope"],
            {
                "route",
                "search_filters",
                "file_names",
                "answer_scope_sections",
            },
            "reproduction.prior_search_scope",
            required={"search_filters"},
        )
        if prior_scope.get("route") not in {
            None,
            "vectordb",
            "rdb",
        }:
            raise FeedbackHandoffError(
                "invalid_payload",
                "prior_search_scope.route is invalid",
            )
        _require_exact_keys(
            prior_scope["search_filters"],
            _FILTER_KEYS,
            "reproduction.prior_search_scope.search_filters",
            required=set(),
        )
        if "file_names" in prior_scope and (
            not isinstance(prior_scope["file_names"], list)
            or any(
                not isinstance(item, str)
                for item in prior_scope["file_names"]
            )
        ):
            raise FeedbackHandoffError(
                "invalid_payload",
                "prior_search_scope.file_names is invalid",
            )
        for section in prior_scope.get(
            "answer_scope_sections",
            [],
        ):
            section_map = _require_exact_keys(
                section,
                {"id", "label", "filters", "file_names"},
                "reproduction.prior_search_scope.sections[]",
            )
            _require_string(
                section_map["id"],
                "prior_search_scope.sections[].id",
            )
            _require_string(
                section_map["label"],
                "prior_search_scope.sections[].label",
            )
            _require_exact_keys(
                section_map["filters"],
                _FILTER_KEYS,
                "prior_search_scope.sections[].filters",
                required=set(),
            )
            if not isinstance(
                section_map["file_names"],
                list,
            ) or any(
                not isinstance(item, str)
                for item in section_map["file_names"]
            ):
                raise FeedbackHandoffError(
                    "invalid_payload",
                    "prior_search_scope section files are invalid",
                )

    provenance = _require_exact_keys(
        root["provenance"],
        {"app_version", "backend_mode", "snapshot_id", "data_revision", "config_fingerprint"},
        "provenance",
    )
    if provenance["backend_mode"] not in {"native", "synthetic_test"}:
        raise FeedbackHandoffError("invalid_payload", "backend_mode is invalid")
    for key in ("app_version", "data_revision"):
        _require_string(provenance[key], f"provenance.{key}")
    if provenance["snapshot_id"] is not None:
        _require_string(provenance["snapshot_id"], "provenance.snapshot_id")
    _validate_hash(provenance["config_fingerprint"], "config_fingerprint")

    observed = _require_exact_keys(
        root["observed"],
        {"route", "filters", "sources", "state", "baseline_failed_checks"},
        "observed",
    )
    expected = _require_exact_keys(
        root["expected"],
        {
            "route",
            "filters",
            "sources",
            "state",
            "manual_assertions",
            "answer_requirements",
        },
        "expected",
        required={"route", "filters", "sources", "state", "manual_assertions"},
    )
    for label, section in (("observed", observed), ("expected", expected)):
        if section["route"] not in {None, "vectordb", "rdb"}:
            raise FeedbackHandoffError("invalid_payload", f"{label}.route is invalid")
        filters = _require_exact_keys(section["filters"], _FILTER_KEYS, f"{label}.filters", required=set())
        state = _require_exact_keys(section["state"], _STATE_KEYS, f"{label}.state", required=set())
        for key, value in filters.items():
            if key == "file_names":
                if not isinstance(value, list) or any(
                    not isinstance(item, str) for item in value
                ):
                    raise FeedbackHandoffError(
                        "invalid_payload", f"{label}.filters.file_names is invalid"
                    )
            elif value is not None and not isinstance(value, str):
                raise FeedbackHandoffError(
                    "invalid_payload", f"{label}.filters.{key} is invalid"
                )
        for key, value in state.items():
            if value is not None and not isinstance(value, str):
                raise FeedbackHandoffError(
                    "invalid_payload", f"{label}.state.{key} is invalid"
                )
        _validate_string_tree(filters)
        _validate_string_tree(state)
        if not isinstance(section["sources"], list):
            raise FeedbackHandoffError("invalid_payload", f"{label}.sources must be a list")
        for source in section["sources"]:
            source_map = _require_exact_keys(source, _SOURCE_KEYS, f"{label}.sources[]", required=set())
            if any(
                value is not None and not isinstance(value, str)
                for value in source_map.values()
            ):
                raise FeedbackHandoffError(
                    "invalid_payload", f"{label}.sources[] value is invalid"
                )
            _validate_string_tree(source_map)
    _validate_checks(
        observed["baseline_failed_checks"],
        "baseline_failed_checks",
        allowed=(
            _HANDOFF_HARD_CHECKS
            if schema_version == 2
            else _AUTOMATIC_CHECKS
        ),
    )
    if not isinstance(expected["manual_assertions"], list):
        raise FeedbackHandoffError("invalid_payload", "manual_assertions must be a list")
    for assertion in expected["manual_assertions"]:
        row = _require_exact_keys(assertion, {"id", "text"}, "manual_assertion")
        assertion_id = _require_string(row["id"], "manual_assertion.id")
        if (
            _ASSERTION_ID.fullmatch(assertion_id) is None
            or contains_sensitive_identifier_pattern(assertion_id)
        ):
            raise FeedbackHandoffError("invalid_payload", "manual assertion id is unsafe")
        _require_string(row["text"], "manual_assertion.text", allow_empty=False)
    answer_requirements = expected.get("answer_requirements") or []
    try:
        canonical_requirements = canonicalize_answer_requirements(
            answer_requirements
        )
    except AnswerRequirementValidationError as exc:
        raise FeedbackHandoffError(
            "invalid_payload",
            f"answer_requirements is invalid: {exc}",
        ) from exc
    if canonical_requirements != answer_requirements:
        raise FeedbackHandoffError(
            "invalid_payload",
            "answer_requirements is not canonical",
        )
    _validate_string_tree(answer_requirements)

    acceptance = _require_exact_keys(root["acceptance"], {"active_checks"}, "acceptance")
    _validate_checks(
        acceptance["active_checks"],
        "active_checks",
        nonempty=True,
        allowed=(
            _HANDOFF_HARD_CHECKS
            if schema_version == 2
            else _AUTOMATIC_CHECKS
        ),
    )
    if schema_version == 2:
        quality = _require_exact_keys(
            root["quality"],
            {
                "profile",
                "hard_checks",
                "soft_objectives",
                "performance_budget",
            },
            "quality",
        )
        if quality["profile"] not in {
            "accuracy_first",
            "balanced",
            "speed_first",
        }:
            raise FeedbackHandoffError(
                "invalid_payload",
                "quality profile is invalid",
            )
        _validate_checks(
            quality["hard_checks"],
            "quality.hard_checks",
            nonempty=True,
            allowed=_HANDOFF_HARD_CHECKS,
        )
        if quality["hard_checks"] != acceptance["active_checks"]:
            raise FeedbackHandoffError(
                "invalid_payload",
                "quality hard checks do not match acceptance",
            )
        soft_objectives = quality["soft_objectives"]
        if (
            not isinstance(soft_objectives, list)
            or any(
                item not in _SOFT_OBJECTIVES
                for item in soft_objectives
            )
            or len(set(soft_objectives))
            != len(soft_objectives)
        ):
            raise FeedbackHandoffError(
                "invalid_payload",
                "soft objectives are invalid",
            )
        budget = _require_exact_keys(
            quality["performance_budget"],
            {
                "max_p95_seconds",
                "min_runs",
                "warmup_runs",
                "enforcement",
            },
            "quality.performance_budget",
        )
        if (
            isinstance(budget["max_p95_seconds"], bool)
            or not isinstance(
                budget["max_p95_seconds"],
                (int, float),
            )
            or isinstance(budget["min_runs"], bool)
            or not isinstance(budget["min_runs"], int)
            or isinstance(budget["warmup_runs"], bool)
            or not isinstance(budget["warmup_runs"], int)
            or budget["min_runs"] < 1
            or not 0
            <= budget["warmup_runs"]
            < budget["min_runs"]
            or budget["enforcement"] not in {"hard", "soft"}
        ):
            raise FeedbackHandoffError(
                "invalid_payload",
                "performance budget is invalid",
            )
        try:
            canonicalize_reproduction_manifest(
                root["reproduction_manifest"]
            )
        except ReproductionManifestError as exc:
            raise FeedbackHandoffError(
                "invalid_payload",
                "reproduction manifest is invalid",
            ) from exc
    verification = _require_exact_keys(root["verification"], {"commands"}, "verification")
    if not isinstance(verification["commands"], list) or not verification["commands"]:
        raise FeedbackHandoffError("invalid_verification_command")
    for command in verification["commands"]:
        _validate_command(command)
    _validate_string_tree(root)


def _validate_checks(
    value: Any,
    label: str,
    *,
    nonempty: bool = False,
    allowed: set[str] = _AUTOMATIC_CHECKS,
) -> None:
    if not isinstance(value, list) or (nonempty and not value):
        raise FeedbackHandoffError("invalid_payload", f"{label} must be a list")
    if any(
        not isinstance(item, str) or item not in allowed
        for item in value
    ):
        raise FeedbackHandoffError("invalid_payload", f"{label} is invalid")
    if len(set(value)) != len(value):
        raise FeedbackHandoffError("invalid_payload", f"{label} has duplicates")


def _validate_string_tree(value: Any) -> None:
    if isinstance(value, str):
        safety_view = re.sub(r"\[REDACTED:[a-z_]+\]", "", value)
        if contains_sensitive_identifier_pattern(safety_view):
            raise FeedbackHandoffError("unredacted_sensitive_data")
    elif isinstance(value, list):
        for item in value:
            _validate_string_tree(item)
    elif isinstance(value, Mapping):
        for item in value.values():
            _validate_string_tree(item)
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise FeedbackHandoffError("invalid_payload", "unsupported value type")


def _json_block(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def render_codex_handoff_markdown(payload: Mapping[str, Any]) -> str:
    """Render deterministic Markdown only from a validated payload."""

    validate_codex_handoff_payload(payload)
    if payload["handoff_schema_version"] == 2:
        lines = [
            "# Codex 작업 전달물",
            "",
            "## 1. 목표",
            str(payload["goal"]["summary"]),
            "",
            "## 2. 사용자 영향",
            str(payload["user_impact"]["summary"]),
            "",
            "## 3. 품질 프로파일과 검증 계획",
            "```json",
            _json_block(payload["quality"]),
            "```",
            "",
            "## 4. 재현 매니페스트",
            "```json",
            _json_block(payload["reproduction_manifest"]),
            "```",
            "",
            "## 5. 실행 출처",
            "```json",
            _json_block(payload["provenance"]),
            "```",
            "",
            "## 6. 재현 입력",
            "```json",
            _json_block(payload["reproduction"]),
            "```",
            "",
            "## 7. 관측된 동작",
            "```json",
            _json_block(
                {
                    key: payload["observed"][key]
                    for key in (
                        "route",
                        "filters",
                        "sources",
                        "state",
                    )
                }
            ),
            "```",
            "",
            "## 8. 승인된 기대 동작",
            "```json",
            _json_block(payload["expected"]),
            "```",
            "",
            "## 9. 수정 전 실패 검사",
            *[
                f"- `{check}`"
                for check in payload["observed"][
                    "baseline_failed_checks"
                ]
            ],
            "",
            "## 10. 합격 기준",
            *[
                f"- `{check}`"
                for check in payload["acceptance"][
                    "active_checks"
                ]
            ],
            "",
            "## 11. 검증 명령",
            "```powershell",
            *payload["verification"]["commands"],
            "```",
            "",
            "## 12. 금지 자료 안내",
            "전체 대화, 원시 오류·스택, 인증정보, 개인정보, "
            "로컬 절대경로, 보고서 본문은 포함하지 않습니다.",
            "",
        ]
        return "\n".join(lines)
    lines = [
        "# Codex 작업 전달물",
        "",
        "## 1. 목표",
        str(payload["goal"]["summary"]),
        "",
        "## 2. 사용자 영향",
        str(payload["user_impact"]["summary"]),
        "",
        "## 3. 생성 이력",
        "```json",
        _json_block(payload["provenance"]),
        "```",
        "",
        "## 4. 재현 입력",
        str(payload["reproduction"]["question"]),
        "",
        "## 5. 관측된 동작",
        "```json",
        _json_block(
            {
                key: payload["observed"][key]
                for key in ("route", "filters", "sources", "state")
            }
        ),
        "```",
        "",
        "## 6. 승인된 기대 동작",
        "```json",
        _json_block(payload["expected"]),
        "```",
        "",
        "## 7. 수정 전 실패 검사",
        *[f"- `{check}`" for check in payload["observed"]["baseline_failed_checks"]],
        "",
        "## 8. 합격 검사",
        *[f"- `{check}`" for check in payload["acceptance"]["active_checks"]],
        "",
        "## 9. 검증 명령",
        "```powershell",
        *payload["verification"]["commands"],
        "```",
        "",
        "## 10. 금지 자료 안내",
        "전체 대화, 원시 오류·스택, 인증정보, 개인정보, 로컬 경로, 보고서 본문은 포함하지 않습니다.",
        "",
    ]
    return "\n".join(lines)


def _manifest_without_hash(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(manifest)
    value.pop("manifest_sha256", None)
    return value


def write_codex_handoff(
    candidate: Mapping[str, Any],
    baseline_run: Mapping[str, Any],
    *,
    output_dir: str | Path,
    approved_by: str,
    approval_reason: str,
    verification_commands: Sequence[str] = DEFAULT_CODEX_VERIFICATION_COMMANDS,
) -> dict[str, Any]:
    """Write a reviewed local handoff as Markdown plus a hashed manifest."""

    if approved_by != "local_operator":
        raise FeedbackHandoffError("invalid_approval")
    reason = redact_handoff_text(str(approval_reason).strip())
    if not reason:
        raise FeedbackHandoffError("invalid_approval")
    canonical, _ = _validate_baseline(candidate, baseline_run)
    payload = build_codex_handoff_payload(
        canonical, baseline_run, verification_commands=verification_commands
    )
    markdown = render_codex_handoff_markdown(payload)
    handoff_id = f"handoff_{uuid.uuid4().hex[:24]}"
    candidate_token = safe_artifact_token(str(canonical["id"]))
    target_dir = _safe_target_directory(Path(output_dir), candidate_token)
    markdown_path = target_dir / f"{handoff_id}.md"
    manifest_path = target_dir / f"{handoff_id}.manifest.json"
    if markdown_path.exists() or manifest_path.exists():
        raise FeedbackHandoffError("artifact_exists")

    manifest: dict[str, Any] = {
        "handoff_schema_version": payload[
            "handoff_schema_version"
        ],
        "kind": "finance_llm_codex_handoff",
        "handoff_id": handoff_id,
        "candidate_id": candidate_token,
        "contract_revision": int(canonical["contract_revision"]),
        "candidate_hash": canonical["candidate_hash"],
        "baseline_run_id": baseline_run["run_id"],
        "baseline_run_hash": baseline_run["run_hash"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "approved_by": approved_by,
        "approval_reason": reason,
        "markdown_filename": markdown_path.name,
        "payload": payload,
        "payload_sha256": _sha256(_canonical_json_bytes(payload)),
        "markdown_sha256": _sha256(_markdown_disk_bytes(markdown)),
    }
    manifest["manifest_sha256"] = _sha256(
        _canonical_json_bytes(_manifest_without_hash(manifest))
    )
    artifact_io.atomic_write_json(manifest_path, manifest)
    # The manifest is canonical.  If the derived Markdown write is interrupted,
    # discovery keeps the valid manifest and repair can recreate the companion.
    artifact_io.atomic_write_text(markdown_path, markdown)
    result = dict(manifest)
    result.update(
        {
            "manifest_path": str(manifest_path),
            "markdown_path": str(markdown_path),
            "integrity_status": "valid",
            "companion_status": "present",
        }
    )
    return result


def _safe_target_directory(root: Path, candidate_token: str) -> Path:
    root_abs = Path(os.path.abspath(root))
    target = root_abs / candidate_token
    try:
        target.relative_to(root_abs)
    except ValueError as exc:
        raise FeedbackHandoffError("handoff_path_escape") from exc
    target.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(target, root_abs)
    return target


def _reject_symlink_components(path: Path, root: Path) -> None:
    root_abs = Path(os.path.abspath(root))
    path_abs = Path(os.path.abspath(path))
    try:
        relative = path_abs.relative_to(root_abs)
    except ValueError as exc:
        raise FeedbackHandoffError("handoff_path_escape") from exc
    current = root_abs
    paths = [root_abs]
    for part in relative.parts:
        current = current / part
        paths.append(current)
    for item in paths:
        try:
            mode = item.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise FeedbackHandoffError("handoff_symlink")


def _pre_read_regular_file(path: str | Path, root: str | Path) -> tuple[Path, Path]:
    root_abs = Path(os.path.abspath(root))
    candidate = Path(path)
    if not candidate.is_absolute():
        # Discovery may return a cwd-relative path that already includes the
        # cwd-relative root (for example debug/codex_handoffs/id/file.json).
        # Preserve that path when it is already inside root; otherwise treat it
        # as relative to root.
        cwd_candidate = Path(os.path.abspath(candidate))
        try:
            cwd_candidate.relative_to(root_abs)
        except ValueError:
            candidate = root_abs / candidate
        else:
            candidate = cwd_candidate
    candidate_abs = Path(os.path.abspath(candidate))
    try:
        candidate_abs.relative_to(root_abs)
    except ValueError as exc:
        raise FeedbackHandoffError("handoff_path_escape") from exc
    _reject_symlink_components(candidate_abs, root_abs)
    try:
        mode = candidate_abs.lstat().st_mode
    except FileNotFoundError as exc:
        raise FeedbackHandoffError("handoff_missing") from exc
    if stat.S_ISLNK(mode):
        raise FeedbackHandoffError("handoff_symlink")
    if not stat.S_ISREG(mode):
        raise FeedbackHandoffError("handoff_not_regular_file")
    resolved_root = root_abs.resolve()
    resolved_path = candidate_abs.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise FeedbackHandoffError("handoff_path_escape") from exc
    return candidate_abs, resolved_root


def _load_manifest_prechecked(
    manifest_path: str | Path, output_root: str | Path
) -> tuple[dict[str, Any], Path, Path]:
    path, resolved_root = _pre_read_regular_file(manifest_path, output_root)
    try:
        value = strict_json_loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise FeedbackHandoffError("malformed_manifest") from exc
    if not isinstance(value, Mapping):
        raise FeedbackHandoffError("malformed_manifest")
    manifest = dict(value)
    if set(manifest) != _MANIFEST_KEYS:
        raise FeedbackHandoffError("malformed_manifest")
    return manifest, path, resolved_root


def _validate_manifest_fields(manifest: Mapping[str, Any], path: Path) -> None:
    if (
        manifest.get("handoff_schema_version") not in {1, 2}
        or manifest.get("kind") != "finance_llm_codex_handoff"
        or manifest.get("approved_by") != "local_operator"
    ):
        raise FeedbackHandoffError("malformed_manifest")
    handoff_id = str(manifest.get("handoff_id") or "")
    if _HANDOFF_ID.fullmatch(handoff_id) is None:
        raise FeedbackHandoffError("malformed_manifest")
    if manifest.get("markdown_filename") != f"{handoff_id}.md":
        raise FeedbackHandoffError("malformed_manifest")
    filename = str(manifest["markdown_filename"])
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise FeedbackHandoffError("malformed_manifest")
    candidate_id = str(manifest.get("candidate_id") or "")
    if safe_artifact_token(candidate_id) != candidate_id or path.parent.name != candidate_id:
        raise FeedbackHandoffError("malformed_manifest")
    if path.name != f"{handoff_id}.manifest.json":
        raise FeedbackHandoffError("malformed_manifest")
    if not isinstance(manifest.get("contract_revision"), int):
        raise FeedbackHandoffError("malformed_manifest")
    for key in (
        "candidate_hash",
        "baseline_run_hash",
        "payload_sha256",
        "markdown_sha256",
        "manifest_sha256",
    ):
        _validate_hash(manifest.get(key), key)
    if _RUN_ID.fullmatch(str(manifest.get("baseline_run_id") or "")) is None:
        raise FeedbackHandoffError("malformed_manifest")
    _validate_timestamp(manifest.get("created_at"), "created_at")
    _require_string(manifest.get("approval_reason"), "approval_reason", allow_empty=False)
    validate_codex_handoff_payload(manifest.get("payload"))
    _validate_string_tree(manifest)


def validate_codex_handoff_artifacts(
    manifest_path: str | Path,
    *,
    output_root: str | Path,
) -> dict[str, Any]:
    """Validate containment, manifest hashes, payload, and optional companion."""

    manifest, path, _ = _load_manifest_prechecked(manifest_path, output_root)
    try:
        _validate_manifest_fields(manifest, path)
    except FeedbackHandoffError as exc:
        if exc.code == "invalid_payload":
            raise FeedbackHandoffError("malformed_manifest") from exc
        raise
    if _sha256(_canonical_json_bytes(manifest["payload"])) != manifest["payload_sha256"]:
        raise FeedbackHandoffError("handoff_hash_mismatch")
    if _sha256(_canonical_json_bytes(_manifest_without_hash(manifest))) != manifest["manifest_sha256"]:
        raise FeedbackHandoffError("handoff_hash_mismatch")
    expected_markdown = _markdown_disk_bytes(
        render_codex_handoff_markdown(manifest["payload"])
    )
    if _sha256(expected_markdown) != manifest["markdown_sha256"]:
        raise FeedbackHandoffError("handoff_hash_mismatch")

    markdown_path = path.parent / manifest["markdown_filename"]
    companion_status = "missing"
    if markdown_path.exists() or markdown_path.is_symlink():
        _pre_read_regular_file(markdown_path, output_root)
        if _sha256(markdown_path.read_bytes()) != manifest["markdown_sha256"]:
            raise FeedbackHandoffError("handoff_hash_mismatch")
        companion_status = "present"
    result = dict(manifest)
    result.update(
        {
            "manifest_path": str(path),
            "markdown_path": str(markdown_path),
            "integrity_status": "valid",
            "companion_status": companion_status,
        }
    )
    return result


def load_codex_handoff(
    manifest_path: str | Path,
    *,
    output_root: str | Path,
) -> dict[str, Any]:
    """Load one integrity-validated local handoff."""

    return validate_codex_handoff_artifacts(
        manifest_path, output_root=output_root
    )


def list_codex_handoff_artifacts(
    output_dir: str | Path,
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Discover manifests and partial Markdown artifacts without trusting names."""

    root = Path(output_dir)
    if not root.exists():
        return {"items": [], "warnings": []}
    token = safe_artifact_token(candidate_id) if candidate_id is not None else None
    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    manifest_stems: set[tuple[Path, str]] = set()
    for path in root.glob("*/*.manifest.json"):
        handoff_id = path.name.removesuffix(".manifest.json")
        manifest_stems.add((path.parent, handoff_id))
        try:
            item = load_codex_handoff(path, output_root=root)
        except FeedbackHandoffError as exc:
            warnings.append(
                {"code": exc.code, "path": str(path), "blocking": True}
            )
            continue
        if token is not None and item.get("candidate_id") != token:
            continue
        if item.get("companion_status") == "missing":
            warnings.append(
                {"code": "partial_handoff", "path": str(path), "blocking": False}
            )
        items.append(item)
    for path in root.glob("*/*.md"):
        handoff_id = path.stem
        if (path.parent, handoff_id) in manifest_stems:
            continue
        if token is not None and path.parent.name != token:
            continue
        warnings.append(
            {"code": "partial_handoff", "path": str(path), "blocking": True}
        )
    return {
        "items": sorted(
            items, key=lambda item: str(item.get("created_at") or ""), reverse=True
        ),
        "warnings": warnings,
    }


def repair_codex_handoff_markdown(
    manifest_path: str | Path,
    *,
    output_root: str | Path,
) -> Path:
    """Recreate only a missing Markdown companion from a valid manifest."""

    loaded = load_codex_handoff(manifest_path, output_root=output_root)
    markdown_path = Path(loaded["markdown_path"])
    if loaded["companion_status"] == "present":
        raise FeedbackHandoffError("artifact_exists")
    markdown = render_codex_handoff_markdown(loaded["payload"])
    artifact_io.atomic_write_text(markdown_path, markdown)
    return markdown_path


def _load_exact_candidate_baseline(
    candidate: Mapping[str, Any], run_id: str, run_hash: str
) -> Mapping[str, Any] | None:
    refs = (candidate.get("evidence") or {}).get("baseline_runs") or []
    for ref in refs:
        if (
            ref.get("run_id") != run_id
            or ref.get("run_hash") != run_hash
            or ref.get("status") != "fail"
            or not is_current_candidate_contract(candidate, ref)
        ):
            continue
        artifact_path = ref.get("artifact_path")
        if not artifact_path:
            return None
        try:
            run = load_evaluation_run(artifact_path)
        except Exception:
            return None
        if (
            run.get("run_id") == run_id
            and run.get("run_hash") == run_hash
            and run.get("integrity_status") == "valid"
            and run.get("run_kind") == "baseline"
            and run.get("run_status") == "completed"
            and is_current_candidate_contract(candidate, run)
            and _failed_checks(run, candidate.get("active_checks") or [])
        ):
            return run
    return None


def discover_candidate_orphan_handoffs(
    candidate: Mapping[str, Any],
    *,
    output_dir: str | Path,
) -> dict[str, Any]:
    """Classify valid, unlinked disk handoffs against their exact baseline."""

    canonical = canonicalize_regression_candidate(candidate)
    discovered = list_codex_handoff_artifacts(
        output_dir, candidate_id=str(canonical["id"])
    )
    linked_hashes = {
        ref.get("manifest_sha256") for ref in canonical.get("handoffs") or []
    }
    result: dict[str, Any] = {
        "attachable": [],
        "stale": [],
        "warnings": discovered["warnings"],
    }
    for handoff in discovered["items"]:
        if handoff.get("manifest_sha256") in linked_hashes:
            continue
        current = (
            handoff.get("candidate_id")
            == safe_artifact_token(str(canonical["id"]))
            and int(handoff.get("contract_revision") or -1)
            == int(canonical.get("contract_revision") or 0)
            and handoff.get("candidate_hash") == canonical.get("candidate_hash")
        )
        baseline = _load_exact_candidate_baseline(
            canonical,
            str(handoff.get("baseline_run_id") or ""),
            str(handoff.get("baseline_run_hash") or ""),
        )
        content_current = False
        if current and baseline is not None:
            try:
                rebuilt = build_codex_handoff_payload(
                    canonical,
                    baseline,
                    verification_commands=handoff["payload"]["verification"][
                        "commands"
                    ],
                )
                content_current = (
                    _sha256(_canonical_json_bytes(rebuilt))
                    == handoff.get("payload_sha256")
                )
            except FeedbackHandoffError:
                content_current = False
        classified = dict(handoff)
        classified["content_status"] = "current" if content_current else "stale"
        if current and baseline is not None and content_current:
            result["attachable"].append(classified)
        else:
            result["stale"].append(classified)
    return result
