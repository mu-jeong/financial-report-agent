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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
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
            INSERT OR IGNORE INTO conversation_threads (id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
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


def delete_all_threads() -> None:
    """Delete every persisted conversation thread and message."""
    init_conversation_db()
    with get_connection() as conn:
        conn.execute("DELETE FROM conversation_messages")
        conn.execute("DELETE FROM conversation_threads")
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


def list_threads() -> list[dict[str, Any]]:
    init_conversation_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, created_at, updated_at
            FROM conversation_threads
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


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
