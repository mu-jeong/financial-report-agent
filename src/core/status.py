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

from src.configs.config import (
    DB_PATH,
    EMBEDDING_MODEL,
    EXTRACTION_ENGINE,
    FAISS_DIR,
    GENERATION_MODEL,
    SAVE_DIR,
    SEARCH_TOP_K,
    TEST_LIMIT,
    USE_PARENT_CHILD,
    USE_RERANKER,
)


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
                    MIN(report_date) AS min_report_date,
                    MAX(report_date) AS max_report_date
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
    except sqlite3.Error as exc:
        info["error"] = str(exc)

    return info


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


def format_status_text(status: dict[str, Any] | None = None) -> str:
    """Return terminal-friendly multiline status text."""
    return "\n".join(format_status_lines(status or get_data_status()))
