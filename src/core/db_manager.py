"""Read-only relational access to the active Native V2 retrieval catalog."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from src.configs.config import DATA_ROOT


def _read_only_uri(path: Path) -> str:
    encoded = quote(path.resolve().as_posix(), safe="/:")
    return f"file:{encoded}?mode=ro"


def _resolve_retrieval_dispatch(data_root: str | Path):
    """Reuse the process-validated Native V2 reader selection."""

    from src.retrieval.dispatch import resolve_retrieval_dispatch

    return resolve_retrieval_dispatch(data_root, validate_snapshot=True)


def get_connection() -> sqlite3.Connection:
    """Open the active Native V2 report projection read-only."""

    dispatch = _resolve_retrieval_dispatch(DATA_ROOT)
    if dispatch.mode != "native":
        raise RuntimeError("normal RDB access requires Native V2")

    connection = sqlite3.connect(_read_only_uri(dispatch.paths.catalog), uri=True)
    connection.create_function(
        "v2_basename",
        1,
        lambda value: str(value or "").replace("\\", "/").rsplit("/", 1)[-1],
        deterministic=True,
    )
    connection.execute(
        """
        CREATE TEMP VIEW reports AS
        SELECT report_id AS id,
               report_type,
               report_date,
               target_name,
               title,
               broker,
               v2_basename(canonical_relative_path) AS file_name,
               1 AS is_embedded
        FROM main.active_reports
        """
    )
    connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    return connection


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
