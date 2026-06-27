"""File-based issue report storage for Debug Mode diagnostics."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_REPORT_DIR = Path(os.getenv("DEBUG_REPORT_DIR", PROJECT_ROOT / "debug"))


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

    lines = [
        "Finance LLM 문제 신고",
        "====================",
        f"Report ID: {report['id']}",
        f"Created At (UTC): {report['created_at']}",
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
            "- 이 파일 내용을 복사하여 이메일의 내용에 첨부해 개발자에게 전달하세요.",
            "- 민감정보가 포함되어 있으면 전달 전에 해당 부분을 삭제하세요.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _compact_report_metadata(metadata: dict | None) -> dict:
    """Keep debugging signals useful without dumping bulky retrieval payloads."""
    metadata = metadata or {}
    compact = {
        key: metadata.get(key)
        for key in ["status", "job_id", "error", "route", "scope_source"]
        if metadata.get(key) is not None
    }
    if metadata.get("search_scope"):
        compact["search_scope"] = metadata.get("search_scope")
    if isinstance(metadata.get("rerank_info"), list):
        compact["rerank_count"] = len(metadata["rerank_info"])
    return compact


def build_issue_report_context(
    *,
    thread: dict[str, Any],
    messages: list[dict[str, Any]],
    include_conversation: bool,
) -> dict[str, Any]:
    """Build report context while preserving the full selected conversation text."""
    context: dict[str, Any] = {
        "thread_id": thread["id"],
        "thread_name": thread["name"],
        "submitted_from": "streamlit_chat",
        "conversation_message_count": len(messages) if include_conversation else 0,
    }
    if include_conversation:
        context["conversation_messages"] = [
            {
                "id": message.get("id"),
                "role": message.get("role"),
                "created_at": message.get("created_at"),
                "content": message.get("content", "") or "",
                "metadata": _compact_report_metadata(message.get("metadata")),
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
        "created_at": created_at.isoformat(timespec="seconds"),
    }
    report_path = report_dir / _report_file_name(report_id, created_at)
    report_path.write_text(
        _format_issue_report_text(report),
        encoding="utf-8",
    )
    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": report_id, "file_path": str(report_path), "json_path": str(json_path)}


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
        if thread_id is not None and report.get("thread_id") != thread_id:
            continue
        reports.append(report)

    return sorted(
        reports,
        key=lambda report: str(report.get("created_at", "")),
        reverse=True,
    )
