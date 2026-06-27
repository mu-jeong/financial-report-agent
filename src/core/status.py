"""Project runtime/data status helpers.

The functions in this module are intentionally read-only. They are used by the
CLI, Streamlit UI, and tests to make the current local data state visible
without mutating the SQLite DB or FAISS index.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from src.configs import config

DB_PATH = config.DB_PATH
EMBEDDING_MODEL = config.EMBEDDING_MODEL
EXTRACTION_ENGINE = config.EXTRACTION_ENGINE
FAISS_DIR = config.FAISS_DIR
GENERATION_MODEL = config.GENERATION_MODEL
SAVE_DIR = config.SAVE_DIR
SEARCH_TOP_K = config.SEARCH_TOP_K
TEST_LIMIT = config.TEST_LIMIT
UNEMBEDDED_EXTRACTION_ENGINE = getattr(config, "UNEMBEDDED_EXTRACTION_ENGINE", "")
USE_PARENT_CHILD = config.USE_PARENT_CHILD
USE_RERANKER = config.USE_RERANKER


def _safe_count_pdfs(save_dir: str) -> int:
    path = Path(save_dir)
    if not path.exists():
        return 0
    return sum(1 for child in path.iterdir() if child.is_file() and child.suffix.lower() == ".pdf")


def _safe_vector_info(faiss_dir: str) -> dict[str, Any]:
    path = Path(faiss_dir)
    files: list[dict[str, Any]] = []
    total_size = 0

    if path.exists():
        for child in sorted(path.iterdir()):
            if child.is_file():
                size = child.stat().st_size
                files.append({"name": child.name, "size_bytes": size})
                total_size += size

    return {
        "exists": path.exists(),
        "file_count": len(files),
        "total_size_bytes": total_size,
        "files": files,
        "has_faiss_index": (path / "index.faiss").exists(),
        "has_pickle_index": (path / "index.pkl").exists(),
    }


def _safe_db_info(db_path: str) -> dict[str, Any]:
    path = Path(db_path)
    info: dict[str, Any] = {
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "total_reports": 0,
        "embedded_reports": 0,
        "pending_reports": 0,
        "parent_chunks": 0,
        "min_report_date": None,
        "max_report_date": None,
        "report_date_counts": {},
        "report_date_type_counts": {},
        "report_types": {},
        "error": None,
    }
    if not path.exists():
        return info

    try:
        db_uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        with sqlite3.connect(db_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_reports,
                    COALESCE(SUM(CASE WHEN is_embedded = 1 THEN 1 ELSE 0 END), 0) AS embedded_reports,
                    MIN(CASE WHEN report_date IS NOT NULL AND TRIM(report_date) != '' THEN SUBSTR(TRIM(report_date), 1, 10) END) AS min_report_date,
                    MAX(CASE WHEN report_date IS NOT NULL AND TRIM(report_date) != '' THEN SUBSTR(TRIM(report_date), 1, 10) END) AS max_report_date
                FROM reports
                """
            ).fetchone()
            if row:
                total = int(row["total_reports"] or 0)
                embedded = int(row["embedded_reports"] or 0)
                info.update(
                    {
                        "total_reports": total,
                        "embedded_reports": embedded,
                        "pending_reports": max(total - embedded, 0),
                        "min_report_date": row["min_report_date"],
                        "max_report_date": row["max_report_date"],
                    }
                )

            try:
                parent_row = conn.execute("SELECT COUNT(*) AS count FROM parent_chunks").fetchone()
                info["parent_chunks"] = int(parent_row["count"] if parent_row else 0)
            except sqlite3.Error:
                info["parent_chunks"] = 0

            report_types = conn.execute(
                "SELECT report_type, COUNT(*) AS count FROM reports GROUP BY report_type ORDER BY report_type"
            ).fetchall()
            info["report_types"] = {row["report_type"]: int(row["count"]) for row in report_types}

            report_dates = conn.execute(
                """
                SELECT SUBSTR(TRIM(report_date), 1, 10) AS report_date, COUNT(*) AS count
                FROM reports
                WHERE report_date IS NOT NULL AND TRIM(report_date) != ''
                  AND is_embedded = 1
                GROUP BY SUBSTR(TRIM(report_date), 1, 10)
                ORDER BY SUBSTR(TRIM(report_date), 1, 10)
                """
            ).fetchall()
            info["report_date_counts"] = {
                row["report_date"]: int(row["count"])
                for row in report_dates
            }

            report_date_types = conn.execute(
                """
                SELECT
                    SUBSTR(TRIM(report_date), 1, 10) AS report_date,
                    TRIM(report_type) AS report_type,
                    COUNT(*) AS count
                FROM reports
                WHERE report_date IS NOT NULL AND TRIM(report_date) != ''
                  AND report_type IS NOT NULL AND TRIM(report_type) != ''
                  AND is_embedded = 1
                GROUP BY SUBSTR(TRIM(report_date), 1, 10), TRIM(report_type)
                ORDER BY SUBSTR(TRIM(report_date), 1, 10), TRIM(report_type)
                """
            ).fetchall()
            date_type_counts: dict[str, dict[str, int]] = {}
            for row in report_date_types:
                date_key = row["report_date"]
                type_key = row["report_type"]
                date_type_counts.setdefault(date_key, {})[type_key] = int(row["count"])
            info["report_date_type_counts"] = date_type_counts
    except sqlite3.Error as exc:
        info["error"] = str(exc)

    return info


def _reports_columns(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}


def list_unembedded_reports(db_path: str = DB_PATH, *, limit: int = 200) -> list[dict[str, Any]]:
    """Return recent reports that exist in SQLite but are not embedded yet."""
    path = Path(db_path)
    if not path.exists():
        return []
    safe_limit = max(1, int(limit or 1))
    db_uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        columns = _reports_columns(conn)
        error_expr = "TRIM(embedding_last_error)" if "embedding_last_error" in columns else "NULL"
        attempted_expr = "TRIM(embedding_last_attempt_at)" if "embedding_last_attempt_at" in columns else "NULL"
        engine_expr = "TRIM(embedding_extraction_engine)" if "embedding_extraction_engine" in columns else "NULL"
        rows = conn.execute(
            f"""
            SELECT
                id,
                SUBSTR(TRIM(report_date), 1, 10) AS report_date,
                TRIM(report_type) AS report_type,
                TRIM(target_name) AS target_name,
                TRIM(title) AS title,
                TRIM(broker) AS broker,
                TRIM(file_name) AS file_name,
                {engine_expr} AS embedding_extraction_engine,
                {error_expr} AS embedding_last_error,
                {attempted_expr} AS embedding_last_attempt_at
            FROM reports
            WHERE COALESCE(is_embedded, 0) = 0
            ORDER BY SUBSTR(TRIM(report_date), 1, 10) DESC, id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _preview_text(value: Any, max_chars: int = 120) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1] + "…"


def build_unembedded_report_rows(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build safe table rows for the unembedded-report Monitoring tab."""
    return [
        {
            "report_date": report.get("report_date"),
            "report_type": report.get("report_type"),
            "target_name": _preview_text(report.get("target_name"), 80),
            "broker": report.get("broker"),
            "title": _preview_text(report.get("title"), 120),
            "file_name": report.get("file_name"),
            "embedding_extraction_engine": _preview_text(report.get("embedding_extraction_engine") or "-", 80),
            "embedding_last_error": _preview_text(report.get("embedding_last_error") or "-", 200),
            "embedding_last_attempt_at": report.get("embedding_last_attempt_at") or "-",
        }
        for report in reports
    ]


def get_data_status(
    *,
    save_dir: str = SAVE_DIR,
    db_path: str = DB_PATH,
    faiss_dir: str = FAISS_DIR,
) -> dict[str, Any]:
    """Return a read-only snapshot of local data, index, and config state."""
    db = _safe_db_info(db_path)
    vector_db = _safe_vector_info(faiss_dir)
    downloaded_pdfs = _safe_count_pdfs(save_dir)

    embedding_limit_active = bool(TEST_LIMIT and TEST_LIMIT > 0)
    search_coverage_ratio = (
        db["embedded_reports"] / db["total_reports"]
        if db.get("total_reports")
        else 0.0
    )

    return {
        "paths": {
            "save_dir": save_dir,
            "db_path": db_path,
            "faiss_dir": faiss_dir,
        },
        "downloaded_pdfs": downloaded_pdfs,
        "db": db,
        "vector_db": vector_db,
        "config": {
            "generation_model": GENERATION_MODEL,
            "embedding_model": EMBEDDING_MODEL,
            "test_limit": TEST_LIMIT,
            "search_top_k": SEARCH_TOP_K,
            "use_reranker": USE_RERANKER,
            "use_parent_child": USE_PARENT_CHILD,
            "extraction_engine": EXTRACTION_ENGINE,
            "unembedded_extraction_engine": UNEMBEDDED_EXTRACTION_ENGINE,
        },
        "embedding_limit_active": embedding_limit_active,
        "search_coverage_ratio": search_coverage_ratio,
    }


def format_bytes(size: int) -> str:
    """Format bytes using compact binary units."""
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_status_lines(status: dict[str, Any]) -> list[str]:
    """Format a status snapshot for terminal output."""
    db = status["db"]
    vector_db = status["vector_db"]
    config = status["config"]
    ratio = status["search_coverage_ratio"] * 100

    lines = [
        "Finance LLM 데이터 상태",
        "-" * 60,
        f"다운로드 PDF: {status['downloaded_pdfs']}건",
        (
            "SQLite 리포트: "
            f"{db['total_reports']}건 "
            f"(임베딩 완료 {db['embedded_reports']}건 / 대기 {db['pending_reports']}건)"
        ),
        f"검색 커버리지: {ratio:.1f}%",
        f"리포트 기간: {db['min_report_date'] or '-'} ~ {db['max_report_date'] or '-'}",
        f"Parent Chunks: {db['parent_chunks']}건",
        (
            "FAISS 인덱스: "
            f"{'있음' if vector_db['has_faiss_index'] else '없음'} "
            f"({vector_db['file_count']}개 파일, {format_bytes(vector_db['total_size_bytes'])})"
        ),
        (
            "현재 설정: "
            f"TEST_LIMIT={config['test_limit']}, "
            f"SEARCH_TOP_K={config['search_top_k']}, "
            f"RERANKER={config['use_reranker']}, "
            f"PARENT_CHILD={config['use_parent_child']}, "
            f"EXTRACTION={config['extraction_engine']}"
        ),
        f"모델: generation={config['generation_model']}, embedding={config['embedding_model']}",
    ]

    if db.get("error"):
        lines.append(f"DB 상태 확인 오류: {db['error']}")
    if status["embedding_limit_active"] and db["pending_reports"] > 0:
        lines.append(
            "주의: TEST_LIMIT가 켜져 있어 임베딩 파이프라인 1회 실행 시 일부 문서만 처리됩니다."
        )
    if vector_db["exists"] and vector_db["has_pickle_index"]:
        lines.append(
            "주의: FAISS index.pkl은 pickle 기반입니다. 직접 생성한 신뢰 가능한 인덱스만 로드하세요."
        )

    return lines


READINESS_LABELS = {
    "ready": "검색 가능",
    "warning": "주의 필요",
    "blocked": "준비 필요",
}


def assess_readiness(status: dict[str, Any]) -> dict[str, Any]:
    """Classify whether the app is ready for non-developer search usage.

    The readiness result is intentionally action-oriented so Quick Start, CLI
    status, and the Streamlit sidebar can all tell users what to do next.
    """
    db = status["db"]
    vector_db = status["vector_db"]
    messages: list[str] = []
    next_actions: list[str] = []
    level = "ready"

    def block(message: str, action: str) -> None:
        nonlocal level
        level = "blocked"
        messages.append(message)
        next_actions.append(action)

    def warn(message: str, action: str) -> None:
        nonlocal level
        if level != "blocked":
            level = "warning"
        messages.append(message)
        next_actions.append(action)

    if db.get("error"):
        block(
            f"SQLite 상태를 확인하지 못했습니다: {db['error']}",
            ".env의 DB_PATH 설정과 data/reports.db 파일을 확인하세요.",
        )
    elif not db.get("exists") or db["total_reports"] == 0:
        block(
            "검색할 리포트 메타데이터가 없습니다.",
            "RUN_QUICKSTART.bat을 다시 실행하거나 python -m src.core.report_crawler를 실행하세요.",
        )

    if not vector_db["has_faiss_index"]:
        block(
            "FAISS 검색 인덱스가 없습니다.",
            "python -m src.core.embed_pipeline --all 로 임베딩 인덱스를 생성하세요.",
        )
    elif db["embedded_reports"] == 0:
        block(
            "임베딩 완료 리포트가 없어 검색할 수 없습니다.",
            "python -m src.core.embed_pipeline --all 로 리포트를 임베딩하세요.",
        )

    if db["total_reports"] > 0 and db["pending_reports"] > 0:
        warn(
            f"아직 임베딩되지 않은 리포트가 {db['pending_reports']}건 있습니다.",
            "누락 없이 검색하려면 python -m src.core.embed_pipeline --all 을 한 번 더 실행하세요.",
        )

    if status["embedding_limit_active"] and db["pending_reports"] > 0:
        warn(
            "TEST_LIMIT가 켜져 있어 임베딩 파이프라인 1회 실행 시 일부 문서만 처리될 수 있습니다.",
            "전체 처리하려면 .env에서 TEST_LIMIT=0으로 설정하거나 --all 실행을 유지하세요.",
        )

    if vector_db["exists"] and vector_db["has_pickle_index"]:
        warn(
            "FAISS index.pkl은 pickle 기반입니다.",
            "직접 생성한 신뢰 가능한 인덱스만 로드하고 외부에서 받은 index.pkl은 사용하지 마세요.",
        )

    if not messages:
        messages.append("리포트 DB와 FAISS 인덱스가 준비되어 질문할 수 있습니다.")
        next_actions.append("Streamlit GUI에서 질문을 입력하세요.")

    return {
        "level": level,
        "label": READINESS_LABELS[level],
        "messages": messages,
        "next_actions": _dedupe_preserve_order(next_actions),
    }


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def format_readiness_lines(status: dict[str, Any]) -> list[str]:
    """Return user-facing readiness lines for terminals and Quick Start."""
    readiness = assess_readiness(status)
    lines = [
        "",
        f"Quick Start 준비 상태: {readiness['label']}",
        "-" * 60,
    ]
    lines.extend(f"- {message}" for message in readiness["messages"])
    if readiness["next_actions"]:
        lines.append("다음 행동:")
        lines.extend(f"  {index}. {action}" for index, action in enumerate(readiness["next_actions"], 1))
    return lines


def format_readiness_text(status: dict[str, Any] | None = None) -> str:
    """Return terminal-friendly readiness text."""
    return "\n".join(format_readiness_lines(status or get_data_status()))


def format_status_text(status: dict[str, Any] | None = None) -> str:
    """Return terminal-friendly multiline status text."""
    return "\n".join(format_status_lines(status or get_data_status()))
