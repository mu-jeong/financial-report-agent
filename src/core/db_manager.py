"""Read-only relational access to the active Native V2 retrieval catalog."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from src.configs.config import DATA_ROOT
from src.core.report_metadata import parse_filename


def _read_only_uri(path: Path) -> str:
    encoded = quote(path.resolve().as_posix(), safe="/:")
    return f"file:{encoded}?mode=ro"


def _resolve_retrieval_dispatch(data_root: str | Path):
    """Reuse the process-validated Native V2 reader selection."""

    from src.retrieval.dispatch import resolve_retrieval_dispatch

    return resolve_retrieval_dispatch(data_root, validate_snapshot=True)


def get_connection(*, materialize_reports: bool = True) -> sqlite3.Connection:
    """Open the Native V2 catalog with an optional report projection."""

    dispatch = _resolve_retrieval_dispatch(DATA_ROOT)
    if dispatch.mode != "native":
        raise RuntimeError("normal RDB access requires Native V2")

    connection = sqlite3.connect(_read_only_uri(dispatch.paths.catalog), uri=True)
    try:
        connection.create_function(
            "v2_basename",
            1,
            lambda value: str(value or "").replace("\\", "/").rsplit("/", 1)[-1],
            deterministic=True,
        )
        if materialize_reports:
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute(
                """
                CREATE TEMP TABLE reports AS
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
    except BaseException:
        connection.close()
        raise
    return connection
