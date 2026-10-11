"""Durable synthesis jobs and reusable, versioned synthesis results."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.configs import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repair_orphaned_jobs(store, process_token, active_job_ids) -> int:
    """Fail same-process running rows that have no live registered worker."""
    active_job_ids = set(active_job_ids)
    with store._connection() as connection:
        running_ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM synthesis_jobs WHERE status='running' AND process_token=?",
                (process_token,),
            )
        }
        orphaned_ids = running_ids - active_job_ids
        if not orphaned_ids:
            return 0
        timestamp = _now()
        connection.executemany(
            """UPDATE synthesis_jobs SET status='failed', phase='finished',
                error='작업 결과를 저장하지 못해 작업이 중단되었습니다. 다시 시도해 주세요.',
                updated_at=?
                WHERE id=? AND status='running' AND process_token=?""",
            [(timestamp, job_id, process_token) for job_id in orphaned_ids],
        )
        return len(orphaned_ids)


def delete_owned_terminal_job(store, job_id: str, owner_id: str) -> bool:
    """Delete an owned terminal job while retaining reusable synthesis caches."""
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status FROM synthesis_jobs WHERE id=? AND owner_id=?",
            (job_id, owner_id),
        ).fetchone()
        if row is None:
            return False
        if row["status"] == "running":
            raise ValueError("생성 중인 종합 작업은 완료 후 삭제할 수 있습니다.")
        connection.execute(
            "DELETE FROM synthesis_jobs WHERE id=? AND owner_id=?",
            (job_id, owner_id),
        )
        return True


class SynthesisStore:
    def __init__(self, path=None):
        self.path = Path(path or Path(config.DATA_ROOT) / "report_synthesis.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS synthesis_jobs (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL,
                    company TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, corpus_json TEXT NOT NULL,
                    result_json TEXT, error TEXT, process_token TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_synthesis_per_owner
                    ON synthesis_jobs(owner_id) WHERE status='running';
                CREATE INDEX IF NOT EXISTS synthesis_jobs_owner_created
                    ON synthesis_jobs(owner_id, created_at);
                CREATE TABLE IF NOT EXISTS synthesis_document_cache (
                    cache_key TEXT PRIMARY KEY, analysis_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS synthesis_segment_cache (
                    key TEXT PRIMARY KEY, analysis_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS synthesis_batch_cache (
                    key TEXT PRIMARY KEY, output_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _job(row, *, include_corpus=False):
        if row is None:
            return None
        job = dict(row)
        raw_result = job.pop("result_json")
        job["result"] = json.loads(raw_result) if raw_result else None
        raw_corpus = job.pop("corpus_json")
        if include_corpus:
            job["corpus"] = json.loads(raw_corpus)
        return job

    def create_job(self, corpus, owner_id, process_token):
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("작업 소유 세션이 필요합니다.")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute("SELECT * FROM synthesis_jobs WHERE owner_id=? AND status='running'", (owner_id,)).fetchone()
            if running:
                if running["fingerprint"] == corpus["fingerprint"]:
                    return self._job(running), False
                raise ValueError("이미 종합 작업이 진행 중입니다. 완료 후 새 범위로 생성해 주세요.")
            job_id, timestamp = uuid.uuid4().hex, _now()
            connection.execute(
                """INSERT INTO synthesis_jobs
                    (id, owner_id, status, phase, total, company, start_date, end_date,
                     fingerprint, corpus_json, process_token, created_at, updated_at)
                    VALUES (?, ?, 'running', 'preparing', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, owner_id, len(corpus["documents"]), corpus["company"], corpus["start_date"],
                 corpus["end_date"], corpus["fingerprint"], json.dumps(corpus, ensure_ascii=False),
                 process_token, timestamp, timestamp),
            )
            return self._job(connection.execute("SELECT * FROM synthesis_jobs WHERE id=?", (job_id,)).fetchone()), True

    def get_job(self, job_id, owner_id, *, include_corpus=False):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM synthesis_jobs WHERE id=? AND owner_id=?", (job_id, owner_id)).fetchone()
            return self._job(row, include_corpus=include_corpus)

    def list_jobs(self, owner_id, limit=20):
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM synthesis_jobs WHERE owner_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (owner_id, max(1, min(int(limit), 100))),
            ).fetchall()
            return [self._job(row) for row in rows]

    def progress(self, job_id, event):
        phase = event.get("phase", "preparing")
        if phase not in {"preparing", "analyzing", "synthesizing"}:
            raise ValueError("unknown synthesis phase")
        completed, total = max(0, int(event.get("completed", 0))), max(0, int(event.get("total", 0)))
        with self._connection() as connection:
            connection.execute(
                "UPDATE synthesis_jobs SET phase=?, completed=?, total=?, updated_at=? WHERE id=? AND status='running'",
                (phase, min(completed, total), total, _now(), job_id),
            )

    def finish(self, job_id, result=None, error=None):
        status = result["status"] if result else "failed"
        if error is None and result is not None:
            error = result.get("error")
        if status not in {"completed", "partial", "failed", "empty"}:
            raise ValueError("invalid synthesis result status")
        with self._connection() as connection:
            connection.execute(
                """UPDATE synthesis_jobs SET status=?, phase='finished', result_json=?, error=?, updated_at=?
                    WHERE id=? AND status='running'""",
                (status, json.dumps(result, ensure_ascii=False) if result is not None else None, error, _now(), job_id),
            )

    def repair_interrupted(self, process_token):
        # GUI job modules survive reruns. A different token means this server restarted.
        with self._connection() as connection:
            connection.execute(
                """UPDATE synthesis_jobs SET status='failed', phase='finished',
                    error='서버 재시작으로 작업이 중단되었습니다. 완료된 종합 단계는 재사용되므로 다시 시도해 주세요.',
                    updated_at=? WHERE status='running' AND process_token<>?""", (_now(), process_token),
            )

    def get_batch(self, key):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT output_json FROM synthesis_batch_cache WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None

    def put_batch(self, key, output):
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO synthesis_batch_cache (key, output_json, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(output, ensure_ascii=False), _now()),
            )

    def get_document(self, key):
        with self._connection() as connection:
            row = connection.execute("SELECT analysis_json FROM synthesis_document_cache WHERE cache_key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None

    def put_document(self, key, analysis):
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO synthesis_document_cache VALUES (?, ?, ?)",
                (key, json.dumps(analysis, ensure_ascii=False), _now()),
            )

    def get_segment(self, key):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT analysis_json FROM synthesis_segment_cache WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None

    def put_segment(self, key, analysis):
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO synthesis_segment_cache (key, analysis_json, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(analysis, ensure_ascii=False), _now()),
            )
