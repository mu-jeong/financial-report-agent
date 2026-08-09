"""File-based issue report storage for Debug Mode diagnostics."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.core import artifact_io
from src.core.app_version import get_app_version
from src.core.artifact_io import (
    safe_artifact_token,
    strict_json_loads,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_REPORT_DIR = Path(os.getenv("DEBUG_REPORT_DIR", PROJECT_ROOT / "debug"))
ISSUE_REPORT_CONTACT_EMAIL = "btr0813@naver.com"
_REPORT_WRITE_LOCK = threading.RLock()
_REPORT_KINDS = {"user_feedback", "system_error"}
_REPORT_TARGET_TYPES = {"response", "ui_or_system"}


class IssueReportLoadError(RuntimeError):
    """Raised when an issue report artifact cannot be loaded safely."""


class IssueReportWriteError(RuntimeError):
    """Raised when a canonical report was saved but its companion was not."""

    def __init__(self, message: str, *, canonical_path: str):
        super().__init__(message)
        self.canonical_path = canonical_path


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _report_file_name(report_id: str, created_at: datetime) -> str:
    return (
        f"issue_report_{created_at.strftime('%Y%m%dT%H%M%SZ')}_"
        f"{safe_artifact_token(report_id)}.txt"
    )


def _format_mapping(title: str, values: dict[str, Any]) -> list[str]:
    if not values:
        return [f"{title}: -"]
    lines = [f"{title}:"]
    for key, value in values.items():
        if isinstance(value, (dict, list)):
            rendered_value = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            rendered_value = str(value)
        lines.append(f"- {key}: {rendered_value}")
    return lines


def _format_issue_report_text(report: dict[str, Any]) -> str:
    context = report.get("context") or {}
    conversation_messages = context.get("conversation_messages") or context.get("recent_messages") or []
    context_summary = {
        key: value
        for key, value in context.items()
        if key not in {"conversation_messages", "recent_messages"}
    }

    app_version = report.get("app_version") or get_app_version()
    subject = f"[Finance LLM Issue][v{app_version}][Report ID: {report['id']}] {report['category']}"
    lines = [
        "Finance LLM 문제 신고",
        "====================",
        f"Report ID: {report['id']}",
        f"Created At (UTC): {report['created_at']}",
        f"App Version: {app_version}",
        f"Thread ID: {report['thread_id']}",
        f"Category: {report['category']}",
        "",
        "Description:",
        report["description"],
        "",
        *_format_mapping("Context", context_summary),
        "",
        "Conversation Messages:",
    ]

    if not conversation_messages:
        lines.append("- 첨부된 대화 없음")
    for index, message in enumerate(conversation_messages, 1):
        metadata = message.get("metadata") or {}
        lines.extend(
            [
                "",
                f"--- Message {index} ---",
                f"ID: {message.get('id', '-')}",
                f"Role: {message.get('role', '-')}",
                f"Created At: {message.get('created_at', '-')}",
                "Metadata:",
                json.dumps(metadata, ensure_ascii=False, indent=2) if metadata else "{}",
                "Content:",
                str(message.get("content", "")),
            ]
        )

    lines.extend(
        [
            "",
            "사용 안내:",
            f"- 수신: {ISSUE_REPORT_CONTACT_EMAIL}",
            f"- 제목: {subject}",
            "- 이 파일 내용을 복사하여 이메일 본문에 그대로 붙여넣어 개발자에게 전달하세요.",
            "- 민감정보가 포함되어 있으면 전달 전에 해당 부분을 삭제하세요.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _line_value(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return ""


def _section_text(text: str, section: str) -> str:
    marker = f"{section}:"
    lines = text.splitlines()
    try:
        start = lines.index(marker) + 1
    except ValueError:
        return ""
    collected: list[str] = []
    for line in lines[start:]:
        if line.endswith(":") and line.strip() in {"Context:", "Conversation Messages:", "사용 안내:"}:
            break
        collected.append(line)
    return "\n".join(collected).strip()


def _parse_context_mapping(text: str) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for line in _section_text(text, "Context").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ") or ":" not in stripped:
            continue
        key, value = stripped[2:].split(":", 1)
        context[key.strip()] = value.strip()
    return context


def _parse_json_block(lines: list[str]) -> tuple[dict[str, Any], int]:
    collected: list[str] = []
    for offset, line in enumerate(lines):
        collected.append(line)
        try:
            parsed = json.loads("\n".join(collected))
        except json.JSONDecodeError:
            continue
        return (parsed if isinstance(parsed, dict) else {}, offset + 1)
    return {}, len(lines)


def _parse_conversation_messages(text: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    messages: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- Message "):
            index += 1
            continue
        index += 1
        message: dict[str, Any] = {"metadata": {}, "content": ""}
        while index < len(lines) and not lines[index].startswith("--- Message "):
            line = lines[index]
            if line.startswith("ID: "):
                message["id"] = line.removeprefix("ID: ").strip()
                index += 1
            elif line.startswith("Role: "):
                message["role"] = line.removeprefix("Role: ").strip()
                index += 1
            elif line.startswith("Created At: "):
                message["created_at"] = line.removeprefix("Created At: ").strip()
                index += 1
            elif line == "Metadata:":
                metadata, consumed = _parse_json_block(lines[index + 1 :])
                message["metadata"] = metadata
                index += consumed + 1
            elif line == "Content:":
                index += 1
                content_lines: list[str] = []
                while index < len(lines) and not lines[index].startswith("--- Message "):
                    if lines[index] == "사용 안내:":
                        break
                    content_lines.append(lines[index])
                    index += 1
                message["content"] = "\n".join(content_lines).strip()
            else:
                index += 1
        if message.get("role"):
            messages.append(message)
    return messages


def _message_id_from_context(context: Mapping[str, Any]) -> Any:
    selected = context.get("selected_message")
    if isinstance(selected, Mapping) and selected.get("id") is not None:
        return selected.get("id")
    messages = context.get("conversation_messages") or context.get("recent_messages") or []
    for message in reversed(messages):
        if isinstance(message, Mapping) and message.get("role") == "assistant":
            return message.get("id")
    return None


def _job_id_from_context(context: Mapping[str, Any]) -> Any:
    selected = context.get("selected_message")
    if isinstance(selected, Mapping):
        metadata = selected.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("job_id") is not None:
            return metadata.get("job_id")
    messages = context.get("conversation_messages") or context.get("recent_messages") or []
    for message in reversed(messages):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        metadata = message.get("metadata")
        if isinstance(metadata, Mapping) and metadata.get("job_id") is not None:
            return metadata.get("job_id")
    return None


def canonicalize_report(
    payload: Mapping[str, Any],
    *,
    file_path: str | None = None,
    json_path: str | None = None,
) -> dict[str, Any]:
    """Return a backward-compatible schema-v2 view without writing the source."""
    report = dict(payload)
    report_id = str(report.get("id") or "").strip()
    if not report_id:
        raise IssueReportLoadError("issue report id is missing")

    context_value = report.get("context")
    context = dict(context_value) if isinstance(context_value, Mapping) else {}
    observed_value = report.get("observed")
    observed = dict(observed_value) if isinstance(observed_value, Mapping) else {}
    reproduction_input_value = observed.get("reproduction_input")
    if not isinstance(reproduction_input_value, Mapping):
        reproduction_input_value = context.get("reproduction_input")
    reproduction_input = (
        dict(reproduction_input_value)
        if isinstance(reproduction_input_value, Mapping)
        else {}
    )
    if reproduction_input:
        observed["reproduction_input"] = reproduction_input
    diagnostics_value = report.get("diagnostics")
    diagnostics = dict(diagnostics_value) if isinstance(diagnostics_value, Mapping) else {}
    consent_value = report.get("consent")
    consent = dict(consent_value) if isinstance(consent_value, Mapping) else {}
    privacy_value = report.get("privacy")
    privacy = dict(privacy_value) if isinstance(privacy_value, Mapping) else {}

    legacy_messages = context.get("conversation_messages") or context.get("recent_messages") or []
    selected_message = context.get("selected_message")
    if not isinstance(selected_message, Mapping):
        selected_message = {}
    trace = context.get("trace_detail")
    if not isinstance(trace, Mapping):
        trace = {}

    observed.setdefault(
        "user_question",
        context.get("selected_user_question")
        or context.get("user_question")
        or reproduction_input.get("question")
        or "",
    )
    observed.setdefault(
        "assistant_response_preview",
        selected_message.get("content") or "",
    )
    observed.setdefault("status", selected_message.get("status"))
    selected_metadata = selected_message.get("metadata")
    if not isinstance(selected_metadata, Mapping):
        selected_metadata = {}
    routing = trace.get("routing")
    if not isinstance(routing, Mapping):
        routing = {}
    scope = trace.get("scope")
    if not isinstance(scope, Mapping):
        scope = {}
    query_rewrite = trace.get("query_rewrite")
    if not isinstance(query_rewrite, Mapping):
        query_rewrite = {}
    scope_decision = query_rewrite.get("scope_decision")
    if not isinstance(scope_decision, Mapping):
        scope_decision = {}
    observed.setdefault("route", selected_metadata.get("route"))
    observed.setdefault("latency", selected_metadata.get("latency_seconds"))
    observed.setdefault(
        "selected_sources",
        trace.get("sources") or selected_metadata.get("selected_sources") or [],
    )
    observed.setdefault(
        "search_filters",
        (trace.get("scope") or {}).get("search_filters")
        if isinstance(trace.get("scope"), Mapping)
        else {},
    )
    if legacy_messages:
        observed.setdefault("legacy_conversation", legacy_messages)
    if trace:
        observed.setdefault("trace", dict(trace))

    actual_value = observed.get("actual")
    actual = dict(actual_value) if isinstance(actual_value, Mapping) else {}
    actual.setdefault(
        "route",
        routing.get("route")
        or observed.get("route")
        or selected_metadata.get("route"),
    )
    filters_value = (
        scope.get("search_filters")
        or observed.get("search_filters")
        or selected_metadata.get("search_filters")
        or {}
    )
    actual.setdefault(
        "filters",
        dict(filters_value) if isinstance(filters_value, Mapping) else {},
    )
    sources_value = (
        trace.get("sources")
        or observed.get("selected_sources")
        or selected_metadata.get("selected_sources")
        or []
    )
    actual.setdefault(
        "sources",
        [
            dict(source)
            for source in sources_value
            if isinstance(source, Mapping)
        ]
        if isinstance(sources_value, list)
        else [],
    )
    actual_state: dict[str, Any] = {}
    for key, value in {
        "followup_scope_intent": query_rewrite.get(
            "followup_scope_intent"
        ),
        "scope_source": query_rewrite.get("scope_source"),
        "scope_decision_reason": scope_decision.get("reason"),
        "scope_decision_matched_section_id": scope_decision.get(
            "matched_section_id"
        ),
    }.items():
        if value is not None:
            actual_state[key] = value
    actual.setdefault("state", actual_state)
    observed["actual"] = actual

    diagnostics.setdefault("trace_summary", context.get("trace_summary") or {})
    diagnostics.setdefault("debug_hints", context.get("debug_hints") or [])
    diagnostics.setdefault("error_type", context.get("error_type"))
    diagnostics.setdefault("error_code", context.get("error_code"))
    diagnostics.setdefault("error_signature", context.get("error_signature"))

    contains_full_conversation = bool(legacy_messages)
    consent.setdefault("include_selected_turn", bool(selected_message or observed.get("user_question")))
    consent.setdefault("include_previous_turns", contains_full_conversation)
    privacy.setdefault("redaction_version", 1)
    privacy.setdefault("contains_full_conversation", contains_full_conversation)

    kind = report.get("kind") or (
        "system_error" if report.get("source") == "system" else "user_feedback"
    )
    if kind not in _REPORT_KINDS:
        raise IssueReportLoadError("issue report kind is invalid")
    inferred_response_target = bool(
        _message_id_from_context(context)
        or selected_message
        or str(observed.get("user_question") or "").strip()
        or str(observed.get("assistant_response_preview") or "").strip()
        or str(reproduction_input.get("question") or "").strip()
    )
    report_target_type = (
        report.get("report_target_type")
        or context.get("report_target_type")
        or ("response" if inferred_response_target else "ui_or_system")
    )
    if report_target_type not in _REPORT_TARGET_TYPES:
        raise IssueReportLoadError(
            "issue report target type is invalid"
        )
    try:
        report_contract_version = int(
            report.get("report_contract_version") or 1
        )
    except (TypeError, ValueError) as exc:
        raise IssueReportLoadError(
            "issue report contract version is invalid"
        ) from exc
    if report_contract_version not in {1, 2}:
        raise IssueReportLoadError(
            "issue report contract version is invalid"
        )
    report.update(
        {
            "schema_version": 2,
            "report_contract_version": report_contract_version,
            "id": report_id,
            "kind": kind,
            "report_target_type": report_target_type,
            "source": report.get("source") or context.get("submitted_from") or "local_chat",
            "created_at": report.get("created_at") or "",
            "app_version": report.get("app_version") or context.get("app_version") or "unknown",
            "thread_id": report.get("thread_id") or context.get("thread_id"),
            "message_id": report.get("message_id") or _message_id_from_context(context),
            "job_id": report.get("job_id") or _job_id_from_context(context),
            "category": report.get("category") or "기타",
            "comment": report.get("comment")
            if report.get("comment") is not None
            else report.get("description") or "",
            "consent": consent,
            "observed": observed,
            "diagnostics": diagnostics,
            "privacy": privacy,
        }
    )
    # Legacy consumers still read these fields during the transition.
    report.setdefault("description", report["comment"])
    report.setdefault("context", context)
    if file_path is not None:
        report["file_path"] = file_path
    if json_path is not None:
        report["json_path"] = json_path
    return report


def _parse_issue_report_text_payload(text: str) -> dict[str, Any]:
    context = _parse_context_mapping(text)
    messages = _parse_conversation_messages(text)
    if messages:
        context["conversation_messages"] = messages
        context["conversation_message_count"] = len(messages)
    return {
        "id": _line_value(text, "Report ID: "),
        "thread_id": _line_value(text, "Thread ID: "),
        "category": _line_value(text, "Category: ") or "기타",
        "description": _section_text(text, "Description"),
        "context": context,
        "app_version": _line_value(text, "App Version: ") or "unknown",
        "created_at": _line_value(text, "Created At (UTC): "),
        "source": context.get("submitted_from") or "local_chat",
    }


def load_report(path: str | Path) -> dict[str, Any]:
    """Load a JSON or legacy text report into the canonical schema-v2 view."""
    artifact_path = Path(path)
    try:
        if artifact_path.suffix.lower() == ".json":
            payload = strict_json_loads(
                artifact_path.read_text(encoding="utf-8-sig")
            )
            if not isinstance(payload, Mapping):
                raise IssueReportLoadError("issue report JSON root must be an object")
            text_path = artifact_path.with_suffix(".txt")
            return canonicalize_report(
                payload,
                file_path=str(text_path),
                json_path=str(artifact_path),
            )
        text = artifact_path.read_text(encoding="utf-8")
        payload = _parse_issue_report_text_payload(text)
        return canonicalize_report(
            payload,
            file_path=str(artifact_path),
            json_path=str(artifact_path.with_suffix(".json")),
        )
    except IssueReportLoadError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise IssueReportLoadError(f"cannot load issue report: {artifact_path}") from exc


def _report_warning(
    code: str,
    path: Path,
    *,
    blocking: bool = False,
) -> dict[str, Any]:
    return {"code": code, "path": str(path), "blocking": blocking}


def list_issue_report_artifacts(
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Discover JSON/text report pairs and surface partial or malformed artifacts."""
    report_dir = Path(DEBUG_REPORT_DIR)
    if not report_dir.exists():
        return {"items": [], "warnings": []}

    stems = {
        path.stem
        for pattern in ("issue_report_*.json", "issue_report_*.txt")
        for path in report_dir.glob(pattern)
    }
    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for stem in sorted(stems):
        json_path = report_dir / f"{stem}.json"
        text_path = report_dir / f"{stem}.txt"
        report: dict[str, Any] | None = None
        if json_path.exists():
            json_warning: dict[str, Any] | None = None
            try:
                report = load_report(json_path)
            except IssueReportLoadError:
                json_warning = _report_warning(
                    "malformed_json",
                    json_path,
                    blocking=not text_path.exists(),
                )
                warnings.append(json_warning)
                if text_path.exists():
                    try:
                        report = load_report(text_path)
                    except IssueReportLoadError:
                        json_warning["blocking"] = True
                        warnings.append(
                            _report_warning(
                                "malformed_text",
                                text_path,
                                blocking=True,
                            )
                        )
                        report = None
            if report is not None and not text_path.exists():
                warnings.append(_report_warning("missing_text_companion", text_path))
        elif text_path.exists():
            warnings.append(_report_warning("missing_json_sidecar", json_path))
            try:
                report = load_report(text_path)
            except IssueReportLoadError:
                warnings[-1]["blocking"] = True
                warnings.append(
                    _report_warning(
                        "malformed_text",
                        text_path,
                        blocking=True,
                    )
                )
                report = None

        if report is None:
            continue
        if thread_id is not None and report.get("thread_id") != thread_id:
            continue
        if text_path.exists():
            try:
                report["content"] = text_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                report["content"] = ""
        else:
            report.setdefault("content", "")
        items.append(report)

    return {
        "items": sorted(
            items,
            key=lambda report: str(report.get("created_at") or ""),
            reverse=True,
        ),
        "warnings": warnings,
    }


def list_v2_issue_report_artifacts(
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Discover only canonical V2 reports for the active monitoring UI."""

    report_dir = Path(DEBUG_REPORT_DIR)
    if not report_dir.exists():
        return {"items": [], "warnings": []}
    items: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for json_path in sorted(report_dir.glob("issue_report_*.json")):
        try:
            payload = strict_json_loads(
                json_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeError, ValueError):
            warnings.append(
                _report_warning("malformed_json", json_path, blocking=True)
            )
            continue
        if not isinstance(payload, Mapping):
            warnings.append(
                _report_warning("malformed_json", json_path, blocking=True)
            )
            continue
        if (
            payload.get("schema_version") != 2
            or payload.get("report_contract_version") != 2
        ):
            continue
        try:
            report = load_report(json_path)
        except IssueReportLoadError:
            warnings.append(
                _report_warning("malformed_json", json_path, blocking=True)
            )
            continue
        if thread_id is not None and report.get("thread_id") != thread_id:
            continue
        text_path = json_path.with_suffix(".txt")
        if text_path.exists():
            try:
                report["content"] = text_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                report["content"] = ""
        else:
            report["content"] = ""
            warnings.append(_report_warning("missing_text_companion", text_path))
        items.append(report)
    return {
        "items": sorted(
            items,
            key=lambda report: str(report.get("created_at") or ""),
            reverse=True,
        ),
        "warnings": warnings,
    }


def repair_issue_report_text_companion(json_path: str | Path) -> Path:
    """Rebuild a missing derived text companion from its canonical JSON."""
    canonical_path = Path(json_path)
    report = load_report(canonical_path)
    text_path = canonical_path.with_suffix(".txt")
    if text_path.exists() and text_path.stat().st_size:
        raise IssueReportWriteError(
            "issue report text companion already exists",
            canonical_path=str(canonical_path),
        )
    try:
        return artifact_io.atomic_write_text(
            text_path,
            _format_issue_report_text(report),
        )
    except OSError as exc:
        raise IssueReportWriteError(
            "failed to repair issue report text companion",
            canonical_path=str(canonical_path),
        ) from exc


def build_issue_report_context(
    *,
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    include_conversation: bool,
) -> dict[str, Any]:
    """Build report context while preserving selected conversation text and metadata."""
    context: dict[str, Any] = {
        "thread_id": thread["id"],
        "thread_name": thread["name"],
        "submitted_from": "streamlit_chat",
        "app_version": get_app_version(),
        "conversation_message_count": len(messages) if include_conversation else 0,
    }
    if include_conversation:
        context["conversation_messages"] = [
            {
                "id": message.get("id"),
                "role": message.get("role"),
                "created_at": message.get("created_at"),
                "content": message.get("content", "") or "",
                "metadata": dict(message.get("metadata") or {}),
            }
            for message in messages
        ]
    return context


def build_issue_report_submission_context(
    *,
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    report_target_type: str,
    selected_message_id: Any = None,
    include_conversation: bool = False,
) -> dict[str, Any]:
    """Build the single report-flow context for response and UI targets."""

    if report_target_type not in _REPORT_TARGET_TYPES:
        raise ValueError("report_target_type is invalid")
    if report_target_type == "response":
        if selected_message_id is None:
            raise ValueError(
                "selected_message_id is required for a response report"
            )
        from src.core import monitoring

        context = monitoring.build_chat_trace_issue_context(
            thread,
            messages,
            selected_message_id=selected_message_id,
        )
    else:
        context = build_issue_report_context(
            thread=thread,
            messages=messages,
            include_conversation=False,
        )
    context["report_target_type"] = report_target_type
    context["app_version"] = get_app_version()
    if include_conversation:
        context["conversation_messages"] = [
            {
                "id": message.get("id"),
                "role": message.get("role"),
                "created_at": message.get("created_at"),
                "content": message.get("content", "") or "",
                "metadata": dict(message.get("metadata") or {}),
            }
            for message in messages
        ]
        context["conversation_message_count"] = len(messages)
    else:
        context.pop("conversation_messages", None)
        context["conversation_message_count"] = 0
    return context


def build_issue_report_preview(
    *,
    context: Mapping[str, Any],
    include_conversation: bool,
) -> dict[str, Any]:
    """Return an allowlisted, user-visible preview before report storage."""

    selected = context.get("selected_message")
    selected = selected if isinstance(selected, Mapping) else {}
    reproduction_input = context.get("reproduction_input")
    reproduction_input = (
        reproduction_input
        if isinstance(reproduction_input, Mapping)
        else {}
    )
    return {
        "report_target_type": context.get("report_target_type")
        or "ui_or_system",
        "selected_message_id": selected.get("id"),
        "selected_question": context.get("selected_user_question")
        or reproduction_input.get("question"),
        "selected_response_preview": selected.get(
            "content_preview"
        ),
        "includes_compact_trace": bool(context.get("trace_detail")),
        "includes_prior_search_scope": bool(
            reproduction_input.get("prior_search_scope")
        ),
        "includes_full_conversation": bool(include_conversation),
        "conversation_message_count": (
            int(context.get("conversation_message_count") or 0)
            if include_conversation
            else 0
        ),
    }


def build_issue_report(
    thread_id: str,
    category: str,
    description: str,
    context: dict[str, Any] | None = None,
    *,
    kind: str | None = None,
    report_target_type: str | None = None,
) -> dict[str, Any]:
    """Build a canonical issue report without writing it to local storage."""
    report_id = uuid.uuid4().hex[:12]
    created_at = _utc_now()
    target_type = (
        report_target_type
        or (context or {}).get("report_target_type")
        or "ui_or_system"
    )
    if target_type not in _REPORT_TARGET_TYPES:
        raise ValueError("report_target_type is invalid")
    normalized_kind = kind or (
        "system_error"
        if target_type == "ui_or_system"
        else "user_feedback"
    )
    if normalized_kind not in _REPORT_KINDS:
        raise ValueError("kind is invalid")
    report = {
        "schema_version": 2,
        "report_contract_version": 2,
        "id": report_id,
        "thread_id": thread_id,
        "category": category,
        "description": description,
        "context": context or {},
        "kind": normalized_kind,
        "report_target_type": target_type,
        "app_version": (context or {}).get("app_version") or get_app_version(),
        "created_at": created_at.isoformat(timespec="seconds"),
        "source": (context or {}).get("submitted_from") or "local_chat",
    }
    return canonicalize_report(report)


def create_issue_report(
    thread_id: str,
    category: str,
    description: str,
    context: dict[str, Any] | None = None,
    *,
    kind: str | None = None,
    report_target_type: str | None = None,
) -> dict[str, str]:
    """Persist a legacy report artifact for offline operator workflows."""
    report = build_issue_report(
        thread_id,
        category,
        description,
        context,
        kind=kind,
        report_target_type=report_target_type,
    )
    report_dir = Path(DEBUG_REPORT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_id = str(report["id"])
    created_at = datetime.fromisoformat(str(report["created_at"]))
    report_path = report_dir / _report_file_name(report_id, created_at)
    json_path = report_path.with_suffix(".json")
    try:
        artifact_io.atomic_write_json(json_path, report)
    except OSError as exc:
        raise IssueReportWriteError(
            "failed to persist canonical issue report",
            canonical_path=str(json_path),
        ) from exc
    try:
        artifact_io.atomic_write_text(
            report_path,
            _format_issue_report_text(report),
        )
    except OSError as exc:
        raise IssueReportWriteError(
            "canonical issue report saved but text companion failed",
            canonical_path=str(json_path),
        ) from exc
    return {"id": report_id, "file_path": str(report_path), "json_path": str(json_path)}


def import_issue_report_text(
    raw_text: str,
    *,
    source: str = "email_import",
) -> dict[str, str]:
    """Persist one imported report idempotently within this process."""
    with _REPORT_WRITE_LOCK:
        return _import_issue_report_text_locked(raw_text, source=source)


def _import_issue_report_text_locked(
    raw_text: str,
    *,
    source: str,
) -> dict[str, str]:
    """Persist an emailed/copied issue report text as an imported local artifact."""
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("issue report text is empty")
    report_dir = Path(DEBUG_REPORT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)

    imported_at = _utc_now()
    report_id = _line_value(text, "Report ID: ") or f"imported_{uuid.uuid4().hex[:12]}"
    for existing in list_v2_issue_report_artifacts()["items"]:
        if existing.get("id") == report_id:
            return {
                "id": report_id,
                "file_path": str(existing.get("file_path") or ""),
                "json_path": str(existing.get("json_path") or ""),
                "source": str(existing.get("source") or source),
            }
    parsed_context = _parse_context_mapping(text)
    parsed_context.update(
        {
            "submitted_from": source,
            "raw_import": True,
            "imported_at": imported_at.isoformat(timespec="seconds"),
        }
    )
    conversation_messages = _parse_conversation_messages(text)
    if conversation_messages:
        parsed_context["conversation_messages"] = conversation_messages
        parsed_context["conversation_message_count"] = len(conversation_messages)
    report = {
        "schema_version": 2,
        "report_contract_version": 2,
        "id": report_id,
        "thread_id": _line_value(text, "Thread ID: ") or "external_email",
        "category": _line_value(text, "Category: ") or "Imported issue report",
        "description": _section_text(text, "Description"),
        "context": parsed_context,
        "app_version": _line_value(text, "App Version: ") or "unknown",
        "created_at": _line_value(text, "Created At (UTC): ") or imported_at.isoformat(timespec="seconds"),
        "source": source,
    }
    report = canonicalize_report(report)
    report_path = report_dir / _report_file_name(report_id, imported_at)
    json_path = report_path.with_suffix(".json")
    try:
        artifact_io.atomic_write_json(json_path, report)
    except OSError as exc:
        raise IssueReportWriteError(
            "failed to persist imported canonical issue report",
            canonical_path=str(json_path),
        ) from exc
    try:
        artifact_io.atomic_write_text(report_path, text.rstrip() + "\n")
    except OSError as exc:
        raise IssueReportWriteError(
            "canonical imported report saved but text companion failed",
            canonical_path=str(json_path),
        ) from exc
    return {"id": report_id, "file_path": str(report_path), "json_path": str(json_path), "source": source}


def list_issue_reports(thread_id: str | None = None) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only discovered report items."""
    return list_issue_report_artifacts(thread_id)["items"]
