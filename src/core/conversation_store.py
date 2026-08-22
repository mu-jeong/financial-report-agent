"""Persistent conversation storage for CLI and Streamlit sessions."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from src.configs.config import CONVERSATION_DB_PATH


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    db_path = db_path or CONVERSATION_DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _repair_legacy_thread_name(name: str) -> str:
    """Normalize placeholder names created while Korean UI strings were mojibake."""
    stripped = (name or "").strip()
    if not stripped:
        return "새로운 대화"

    has_mojibake_marker = "\ufffd" in stripped or "\x80" in stripped
    looks_like_question_placeholder = (
        stripped.count("?") >= 2
        and len(stripped) <= 12
        and not any(ch.isalnum() for ch in stripped.replace("?", ""))
    )
    if has_mojibake_marker or looks_like_question_placeholder:
        return "새로운 대화"

    return name


def _repair_legacy_thread_names(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, name FROM conversation_threads").fetchall()
    for row in rows:
        repaired = _repair_legacy_thread_name(row["name"])
        if repaired != row["name"]:
            conn.execute(
                "UPDATE conversation_threads SET name = ?, updated_at = ? WHERE id = ?",
                (repaired, _utc_now(), row["id"]),
            )


def init_conversation_db(db_path: str | None = None) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_threads (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        thread_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(conversation_threads)").fetchall()
        }
        if "pinned" not in thread_columns:
            conn.execute(
                "ALTER TABLE conversation_threads ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                metadata TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE
            )
            """
        )
        _repair_legacy_thread_names(conn)
        conn.commit()


def create_thread(name: str = "새로운 대화", thread_id: str | None = None) -> str:
    init_conversation_db()
    now = _utc_now()
    new_id = thread_id or str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO conversation_threads (id, name, pinned, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (new_id, name, now, now),
        )
        conn.commit()
    return new_id


def ensure_thread(thread_id: str, name: str = "기본 대화") -> str:
    create_thread(name=name, thread_id=thread_id)
    return thread_id


def rename_thread(thread_id: str, name: str) -> None:
    init_conversation_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE conversation_threads SET name = ?, updated_at = ? WHERE id = ?",
            (name, _utc_now(), thread_id),
        )
        conn.commit()


def delete_thread(thread_id: str) -> None:
    init_conversation_db()
    with get_connection() as conn:
        conn.execute("DELETE FROM conversation_messages WHERE thread_id = ?", (thread_id,))
        conn.execute("DELETE FROM conversation_threads WHERE id = ?", (thread_id,))
        conn.commit()


def set_thread_pinned(thread_id: str, pinned: bool) -> None:
    """Pin or unpin a conversation thread in the sidebar ordering."""
    init_conversation_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE conversation_threads SET pinned = ?, updated_at = ? WHERE id = ?",
            (1 if pinned else 0, _utc_now(), thread_id),
        )
        conn.commit()


def append_message(
    thread_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> int:
    init_conversation_db()
    now = _utc_now()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    with get_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO conversation_messages (thread_id, role, content, metadata, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (thread_id, role, content, metadata_json, now),
        )
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE id = ?",
            (now, thread_id),
        )
        conn.commit()
        return int(cursor.lastrowid)


def append_pending_exchange(
    thread_id: str,
    user_content: str,
    assistant_content: str,
    assistant_metadata: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Persist a user question and its running assistant placeholder atomically."""
    init_conversation_db()
    now = _utc_now()
    assistant_metadata_json = json.dumps(
        assistant_metadata or {},
        ensure_ascii=False,
    )
    with get_connection() as conn:
        user_cursor = conn.execute(
            """
            INSERT INTO conversation_messages (thread_id, role, content, metadata, created_at)
            VALUES (?, 'user', ?, '{}', ?)
            """,
            (thread_id, user_content, now),
        )
        assistant_cursor = conn.execute(
            """
            INSERT INTO conversation_messages (thread_id, role, content, metadata, created_at)
            VALUES (?, 'assistant', ?, ?, ?)
            """,
            (
                thread_id,
                assistant_content,
                assistant_metadata_json,
                now,
            ),
        )
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE id = ?",
            (now, thread_id),
        )
        conn.commit()
        return int(user_cursor.lastrowid), int(assistant_cursor.lastrowid)


def update_message(
    message_id: int,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Update an existing conversation message and touch its parent thread."""
    init_conversation_db()
    now = _utc_now()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT thread_id FROM conversation_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            return
        conn.execute(
            """
            UPDATE conversation_messages
            SET content = ?, metadata = ?
            WHERE id = ?
            """,
            (content, metadata_json, message_id),
        )
        conn.execute(
            "UPDATE conversation_threads SET updated_at = ? WHERE id = ?",
            (now, row["thread_id"]),
        )
        conn.commit()


def mark_interrupted_running_messages_failed(
    active_job_ids: set[str] | list[str] | tuple[str, ...] | None = None,
) -> int:
    """Mark persisted assistant messages from interrupted background jobs as failed.

    Streamlit answer generation runs in daemon threads. If the app process exits
    before a thread updates its placeholder message, the DB can retain
    ``status=running`` forever and the chat input stays locked. On app startup or
    rerun, callers can pass the in-memory active job ids; any other running
    assistant message is treated as interrupted and unlocked.
    """
    init_conversation_db()
    active_jobs = {str(job_id) for job_id in (active_job_ids or []) if job_id}
    repaired_count = 0
    now = _utc_now()
    interrupted_content = (
        "이전 답변 생성 작업이 앱 종료 또는 재시작으로 중단되었습니다. "
        "필요하면 다시 질문해 주세요."
    )

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, thread_id, metadata
            FROM conversation_messages
            WHERE role = 'assistant'
            """
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if metadata.get("status") != "running":
                continue
            job_id = metadata.get("job_id")
            if job_id and str(job_id) in active_jobs:
                continue
            metadata.update(
                {
                    "status": "failed",
                    "error": "interrupted_background_job",
                    "interrupted_at": now,
                }
            )
            conn.execute(
                """
                UPDATE conversation_messages
                SET content = ?, metadata = ?
                WHERE id = ?
                """,
                (interrupted_content, json.dumps(metadata, ensure_ascii=False), row["id"]),
            )
            conn.execute(
                "UPDATE conversation_threads SET updated_at = ? WHERE id = ?",
                (now, row["thread_id"]),
            )
            repaired_count += 1
        conn.commit()
    return repaired_count


def list_threads() -> list[dict[str, Any]]:
    init_conversation_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, pinned, created_at, updated_at
            FROM conversation_threads
            ORDER BY pinned DESC, updated_at DESC
            """
        ).fetchall()
    return [
        {
            **dict(row),
            "pinned": bool(row["pinned"]),
        }
        for row in rows
    ]


def list_messages(thread_id: str) -> list[dict[str, Any]]:
    init_conversation_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, role, content, metadata, created_at
            FROM conversation_messages
            WHERE thread_id = ?
            ORDER BY id ASC
            """,
            (thread_id,),
        ).fetchall()

    messages: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        messages.append(
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "metadata": metadata,
                "created_at": row["created_at"],
            }
        )
    return messages


def get_chat_history(thread_id: str, limit: int | None = None) -> list[tuple[str, str]]:
    messages = list_messages(thread_id)
    messages = [
        message
        for message in messages
        if (message.get("metadata") or {}).get("status") not in {"running", "failed"}
    ]
    if limit:
        messages = messages[-limit:]
    role_map = {"user": "사용자", "assistant": "AI"}
    return [(role_map.get(message["role"], message["role"]), message["content"]) for message in messages]
