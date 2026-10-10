"""Background RDB report synthesis with durable, owner-scoped job state.

This module must survive Streamlit reruns, just like chat_jobs. Worker threads
never call Streamlit and use only the frozen corpus persisted at submission.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from src.configs import config
from src.core import report_corpus, report_synthesis
from src.core.report_synthesis_store import SynthesisStore


@st.cache_resource
def _synthesis_job_registry() -> dict:
    # Streamlit's file watcher can evict this module without stopping workers.
    # Retain the registry and process identity using the same pattern as chat_jobs.
    return {"process_token": uuid.uuid4().hex, "lock": threading.RLock(),
            "stores": {}, "threads": {}}


_REGISTRY = _synthesis_job_registry()
_PROCESS_TOKEN = _REGISTRY["process_token"]
_LOCK = _REGISTRY["lock"]
_STORES: dict[str, SynthesisStore] = _REGISTRY["stores"]
_THREADS: dict[str, threading.Thread] = _REGISTRY["threads"]


def _repair_orphaned_jobs(store, process_token, active_job_ids) -> int:
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
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.executemany(
            """UPDATE synthesis_jobs SET status='failed', phase='finished',
                error='작업 결과를 저장하지 못해 작업이 중단되었습니다. 다시 시도해 주세요.',
                updated_at=?
                WHERE id=? AND status='running' AND process_token=?""",
            [(timestamp, job_id, process_token) for job_id in orphaned_ids],
        )
        return len(orphaned_ids)


def _store():
    path = str((Path(config.DATA_ROOT) / "report_synthesis.sqlite3").resolve())
    with _LOCK:
        if path not in _STORES:
            store = SynthesisStore(path)
            store.repair_interrupted(_PROCESS_TOKEN)
            _STORES[path] = store
        store = _STORES[path]
        # Keep recovery outside the cached class API: a watcher reload may retain
        # an instance created from the previous SynthesisStore class object.
        active_job_ids = {
            job_id for job_id, thread in _THREADS.items() if thread.is_alive()
        }
        _repair_orphaned_jobs(store, _PROCESS_TOKEN, active_job_ids)
        for job_id, thread in list(_THREADS.items()):
            if not thread.is_alive():
                _THREADS.pop(job_id, None)
        return store


def _run_job(store, job_id, corpus):
    try:
        result = report_synthesis.synthesize_corpus(
            corpus, on_progress=lambda event: store.progress(job_id, event), cache=store,
        )
        store.finish(job_id, result)
    except Exception as exc:
        config.get_logger(__name__).exception("Report synthesis job failed: %s", job_id)
        # Provider exception strings can carry URLs or request data. Persist a safe user message.
        message = str(exc) if isinstance(exc, ValueError) else "종합 생성에 실패했습니다. 설정과 연결을 확인한 뒤 재시도해 주세요."
        try:
            store.finish(job_id, error=message)
        except Exception:
            config.get_logger(__name__).exception(
                "Failed to persist report synthesis failure: %s", job_id,
            )
    finally:
        with _LOCK:
            _THREADS.pop(job_id, None)


def _start_corpus(corpus, owner_id):
    report_synthesis.validate_corpus(corpus)
    with _LOCK:
        store = _store()
        job, created = store.create_job(corpus, owner_id, _PROCESS_TOKEN)
        if not created:
            return job["id"]
        thread = threading.Thread(target=_run_job, args=(store, job["id"], corpus), daemon=True,
                                  name=f"report-synthesis-{job['id'][:8]}")
        _THREADS[job["id"]] = thread
        try:
            thread.start()
        except RuntimeError:
            _THREADS.pop(job["id"], None)
            store.finish(job["id"], error="종합 작업을 시작할 수 없습니다. 다시 시도해 주세요.")
            raise
    return job["id"]


def start_job(company: str, start_date: str, end_date: str, owner_id: str) -> str:
    corpus = report_corpus.load_report_corpus(company, start_date, end_date)
    return _start_corpus(corpus, owner_id)


def get_job(job_id: str, owner_id: str) -> dict | None:
    return _store().get_job(job_id, owner_id)


def list_jobs(owner_id: str, limit=20) -> list[dict]:
    return _store().list_jobs(owner_id, limit)


def delete_job(job_id: str, owner_id: str) -> bool:
    """Delete an owned terminal job while retaining reusable synthesis caches."""
    with _LOCK:
        store = _store()
        # Use the existing connection API so cached pre-reload store instances
        # remain usable. Serialize the status check and delete across processes.
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


def retry_job(job_id: str, owner_id: str) -> str:
    job = _store().get_job(job_id, owner_id, include_corpus=True)
    if not job:
        raise ValueError("이 세션의 종합 작업을 찾을 수 없습니다.")
    if job["status"] == "running":
        return job_id
    if job["status"] not in {"failed", "partial"}:
        raise ValueError("실패하거나 일부 완료된 작업만 재시도할 수 있습니다.")
    return _start_corpus(job["corpus"], owner_id)
