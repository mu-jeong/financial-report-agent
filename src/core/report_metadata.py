"""Dependency-light parsing of report filename metadata."""

from __future__ import annotations

from datetime import datetime


def parse_filename(file_name: str) -> dict[str, str] | None:
    """Parse ``type_date_target_broker_title.pdf`` metadata."""

    if not file_name.lower().endswith(".pdf"):
        return None

    parts = file_name[:-4].split("_", 4)
    if len(parts) < 5:
        return None

    report_type, report_date, target_name, broker, title = parts
    try:
        datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        return None

    return {
        "report_type": report_type,
        "report_date": report_date,
        "target_name": target_name,
        "broker": broker,
        "title": title,
    }
