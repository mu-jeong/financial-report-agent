"""File-based issue report storage for Debug Mode diagnostics."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.app_version import get_app_version

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_REPORT_DIR = Path(os.getenv("DEBUG_REPORT_DIR", PROJECT_ROOT / "debug"))
ISSUE_REPORT_CONTACT_EMAIL = "btr0813@naver.com"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _report_file_name(report_id: str, created_at: datetime) -> str:
    return f"issue_report_{created_at.strftime('%Y%m%dT%H%M%SZ')}_{report_id}.txt"


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


def create_issue_report(
    thread_id: str,
    category: str,
    description: str,
    context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Persist a user-submitted problem report as a readable text file under debug/."""
    report_dir = Path(DEBUG_REPORT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)

    report_id = uuid.uuid4().hex[:12]
    created_at = _utc_now()
    report = {
        "id": report_id,
        "thread_id": thread_id,
        "category": category,
        "description": description,
        "context": context or {},
        "app_version": (context or {}).get("app_version") or get_app_version(),
        "created_at": created_at.isoformat(timespec="seconds"),
        "source": (context or {}).get("submitted_from") or "local_chat",
    }
    report_path = report_dir / _report_file_name(report_id, created_at)
    report_path.write_text(
        _format_issue_report_text(report),
        encoding="utf-8",
    )
    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": report_id, "file_path": str(report_path), "json_path": str(json_path)}


def import_issue_report_text(raw_text: str, *, source: str = "email_import") -> dict[str, str]:
    """Persist an emailed/copied issue report text as an imported local artifact."""
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("issue report text is empty")
    report_dir = Path(DEBUG_REPORT_DIR)
    report_dir.mkdir(parents=True, exist_ok=True)

    imported_at = _utc_now()
    report_id = _line_value(text, "Report ID: ") or f"imported_{uuid.uuid4().hex[:12]}"
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
        "id": report_id,
        "thread_id": _line_value(text, "Thread ID: ") or "external_email",
        "category": _line_value(text, "Category: ") or "Imported issue report",
        "description": _section_text(text, "Description"),
        "context": parsed_context,
        "app_version": _line_value(text, "App Version: ") or "unknown",
        "created_at": _line_value(text, "Created At (UTC): ") or imported_at.isoformat(timespec="seconds"),
        "source": source,
    }
    report_path = report_dir / _report_file_name(report_id, imported_at)
    report_path.write_text(text.rstrip() + "\n", encoding="utf-8")
    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": report_id, "file_path": str(report_path), "json_path": str(json_path), "source": source}


def list_issue_reports(thread_id: str | None = None) -> list[dict[str, Any]]:
    """Return debug issue report text files, newest first."""
    report_dir = Path(DEBUG_REPORT_DIR)
    if not report_dir.exists():
        return []

    reports: list[dict[str, Any]] = []
    for report_path in report_dir.glob("issue_report_*.txt"):
        try:
            text = report_path.read_text(encoding="utf-8")
        except OSError:
            continue
        report = {
            "id": "",
            "thread_id": "",
            "category": "",
            "created_at": "",
            "file_path": str(report_path),
            "content": text,
        }
        json_path = report_path.with_suffix(".json")
        if json_path.exists():
            try:
                sidecar = json.loads(json_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                sidecar = {}
            if sidecar:
                report.update(
                    {
                        "id": sidecar.get("id") or report["id"],
                        "thread_id": sidecar.get("thread_id") or report["thread_id"],
                        "category": sidecar.get("category") or report["category"],
                        "created_at": sidecar.get("created_at") or report["created_at"],
                        "app_version": sidecar.get("app_version") or report.get("app_version"),
                        "source": sidecar.get("source") or report.get("source"),
                        "description": sidecar.get("description"),
                        "context": sidecar.get("context") or {},
                        "json_path": str(json_path),
                    }
                )
        for line in text.splitlines():
            if line.startswith("Report ID: "):
                report["id"] = report["id"] or line.removeprefix("Report ID: ").strip()
            elif line.startswith("Thread ID: "):
                report["thread_id"] = report["thread_id"] or line.removeprefix("Thread ID: ").strip()
            elif line.startswith("Category: "):
                report["category"] = report["category"] or line.removeprefix("Category: ").strip()
            elif line.startswith("Created At (UTC): "):
                report["created_at"] = report["created_at"] or line.removeprefix("Created At (UTC): ").strip()
            elif line.startswith("App Version: "):
                report["app_version"] = report.get("app_version") or line.removeprefix("App Version: ").strip()
        if thread_id is not None and report.get("thread_id") != thread_id:
            continue
        reports.append(report)

    return sorted(
        reports,
        key=lambda report: str(report.get("created_at", "")),
        reverse=True,
    )
