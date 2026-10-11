from __future__ import annotations

import threading
import importlib.util
from pathlib import Path

import pytest

from apps.gui import synthesis_jobs as jobs
from src.core import report_synthesis_store


def _corpus():
    return {"company": "Alpha", "start_date": "2026-07-01", "end_date": "2026-07-31",
            "fingerprint": "fixed-scope", "profile_id": "profile", "revision": {},
            "documents": [], "excluded": [], "warnings": []}


def test_background_job_persists_progress_and_does_not_duplicate_running_work(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.config, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(jobs.report_corpus, "load_report_corpus", lambda *_: _corpus())
    started, release = threading.Event(), threading.Event()
    calls = []

    def fake_synthesis(corpus, *, on_progress, cache):
        calls.append(corpus)
        on_progress({"phase": "synthesizing", "completed": 0, "total": 0})
        started.set()
        assert release.wait(5)
        return {"status": "completed", "content": "durable result"}

    monkeypatch.setattr(jobs.report_synthesis, "synthesize_corpus", fake_synthesis)
    job_id = jobs.start_job("Alpha", "2026-07-01", "2026-07-31", "owner")
    assert started.wait(5)
    thread = jobs._THREADS[job_id]
    try:
        assert jobs.start_job("Alpha", "2026-07-01", "2026-07-31", "owner") == job_id
        assert jobs.get_job(job_id, "other-owner") is None
        assert jobs.get_job(job_id, "owner")["phase"] == "synthesizing"
        assert jobs.list_jobs("owner")[0]["id"] == job_id
        with pytest.raises(ValueError, match="완료 후 삭제"):
            jobs.delete_job(job_id, "owner")
        assert jobs.get_job(job_id, "owner")["status"] == "running"
    finally:
        release.set()
        thread.join(5)
    assert jobs.get_job(job_id, "owner")["result"]["content"] == "durable result"
    assert len(calls) == 1 and job_id not in jobs._THREADS
    with pytest.raises(ValueError, match="실패"):
        jobs.retry_job(job_id, "owner")


def test_explicit_retry_uses_frozen_corpus_and_completed_batch_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.config, "DATA_ROOT", str(tmp_path))
    corpus = _corpus()
    corpus["documents"] = [{"report_uid": "old", "parents": []}]
    monkeypatch.setattr(jobs.report_corpus, "load_report_corpus", lambda *_: corpus)
    monkeypatch.setattr(jobs.report_synthesis, "validate_corpus", lambda _: None)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_synthesis(snapshot, *, on_progress, cache):
        calls.append(snapshot["documents"][0]["report_uid"])
        entered.set()
        assert release.wait(5)
        if len(calls) == 1:
            cache.put_batch("completed-batch", {"section": "saved intermediate"})
            return {"status": "partial", "content": "partial"}
        assert cache.get_batch("completed-batch") == {"section": "saved intermediate"}
        return {"status": "completed", "content": "retried"}

    monkeypatch.setattr(jobs.report_synthesis, "synthesize_corpus", fake_synthesis)
    job_id = jobs.start_job("Alpha", "2026-07-01", "2026-07-31", "owner")
    assert entered.wait(5)
    thread = jobs._THREADS[job_id]
    release.set()
    thread.join(5)
    assert calls == ["old"]
    assert jobs.get_job(job_id, "owner")["status"] == "partial"
    corpus["documents"][0]["report_uid"] = "new"
    entered.clear()
    release.clear()
    new_id = jobs.retry_job(job_id, "owner")
    assert entered.wait(5)
    thread = jobs._THREADS[new_id]
    release.set()
    thread.join(5)
    assert calls == ["old", "old"]
    assert jobs.get_job(new_id, "owner")["status"] == "completed"


def test_worker_failure_is_persisted_without_exposing_provider_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.config, "DATA_ROOT", str(tmp_path))
    store = jobs._store()
    job, _ = store.create_job(_corpus(), "owner", jobs._PROCESS_TOKEN)

    def failure(*args, **kwargs):
        raise RuntimeError("secret-provider-request")

    monkeypatch.setattr(jobs.report_synthesis, "synthesize_corpus", failure)
    jobs._run_job(store, job["id"], _corpus())
    result = jobs.get_job(job["id"], "owner")
    assert result["status"] == "failed"
    assert "secret-provider-request" not in result["error"]


def test_terminal_write_failure_is_reconciled_after_worker_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.config, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(jobs.report_corpus, "load_report_corpus", lambda *_: _corpus())
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    synthesis_calls = 0

    def fake_synthesis(corpus, *, on_progress, cache):
        nonlocal synthesis_calls
        call_index = synthesis_calls
        synthesis_calls += 1
        entered[call_index].set()
        assert release[call_index].wait(5)
        return {"status": "completed", "content": f"result-{call_index}"}

    store = jobs._store()
    real_finish = store.finish
    finish_attempts = 0

    def transient_finish_failure(*args, **kwargs):
        nonlocal finish_attempts
        finish_attempts += 1
        if finish_attempts <= 2:
            raise OSError("temporary storage failure")
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(jobs.report_synthesis, "synthesize_corpus", fake_synthesis)
    monkeypatch.setattr(store, "finish", transient_finish_failure)

    orphaned_id = jobs.start_job("Alpha", "2026-07-01", "2026-07-31", "owner")
    assert entered[0].wait(5)
    orphaned_thread = jobs._THREADS[orphaned_id]
    release[0].set()
    orphaned_thread.join(5)

    assert finish_attempts == 2
    assert orphaned_id not in jobs._THREADS
    assert not orphaned_thread.is_alive()
    assert store.get_job(orphaned_id, "owner")["status"] == "running"

    # A stale registry key must not protect a worker that has actually exited.
    with jobs._LOCK:
        jobs._THREADS[orphaned_id] = orphaned_thread
    assert jobs.get_job(orphaned_id, "owner")["status"] == "failed"
    assert orphaned_id not in jobs._THREADS
    replacement_id = jobs.start_job("Alpha", "2026-07-01", "2026-07-31", "owner")
    assert replacement_id != orphaned_id
    assert entered[1].wait(5)
    replacement_thread = jobs._THREADS[replacement_id]
    release[1].set()
    replacement_thread.join(5)
    assert jobs.get_job(replacement_id, "owner")["status"] == "completed"


def test_same_process_reconciliation_is_bounded_and_preserves_persisted_inputs(tmp_path):
    store = jobs.SynthesisStore(tmp_path / "synthesis.sqlite3")
    live, _ = store.create_job(_corpus(), "live-owner", "process")
    orphaned_corpus = {**_corpus(), "fingerprint": "orphaned"}
    orphaned, _ = store.create_job(orphaned_corpus, "orphan-owner", "process")
    other_process, _ = store.create_job(
        {**_corpus(), "fingerprint": "other"}, "other-owner", "other-process",
    )
    store.put_batch("cache-key", {"section": "cached"})

    repaired = jobs._repair_orphaned_jobs(store, "process", {live["id"]})

    assert repaired == 1
    assert store.get_job(live["id"], "live-owner")["status"] == "running"
    saved = store.get_job(orphaned["id"], "orphan-owner", include_corpus=True)
    assert saved["status"] == "failed"
    assert saved["corpus"] == orphaned_corpus
    assert store.get_job(other_process["id"], "other-owner")["status"] == "running"
    assert store.get_batch("cache-key") == {"section": "cached"}


def test_watcher_reimport_supports_legacy_cached_store_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.config, "DATA_ROOT", str(tmp_path))
    store = jobs.SynthesisStore(tmp_path / "report_synthesis.sqlite3")
    orphaned, _ = store.create_job(_corpus(), "orphan-owner", jobs._PROCESS_TOKEN)
    terminal, _ = store.create_job(
        {**_corpus(), "fingerprint": "terminal"}, "terminal-owner", jobs._PROCESS_TOKEN,
    )
    store.finish(terminal["id"], {"status": "completed", "content": "done"})

    class LegacyCachedStore:
        _connection = jobs.SynthesisStore._connection
        _job = staticmethod(jobs.SynthesisStore._job)
        get_job = jobs.SynthesisStore.get_job

        def __init__(self, path):
            self.path = path

    legacy_store = LegacyCachedStore(store.path)
    assert not hasattr(legacy_store, "repair_orphaned_jobs")
    assert not hasattr(legacy_store, "delete_owned_terminal_job")
    path = str(store.path.resolve())
    with jobs._LOCK:
        jobs._STORES[path] = legacy_store

    spec = importlib.util.spec_from_file_location(jobs.__name__, jobs.__file__)
    reimported = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reimported)

    assert reimported.SynthesisStore is report_synthesis_store.SynthesisStore
    assert reimported._STORES is jobs._STORES
    assert reimported.get_job(orphaned["id"], "orphan-owner")["status"] == "failed"
    assert reimported.delete_job(terminal["id"], "terminal-owner")
    assert store.get_job(terminal["id"], "terminal-owner") is None


def test_watcher_module_reimport_keeps_running_job_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.config, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(jobs.report_corpus, "load_report_corpus", lambda *_: _corpus())
    started, release = threading.Event(), threading.Event()

    def fake_synthesis(corpus, *, on_progress, cache):
        started.set()
        assert release.wait(5)
        return {"status": "completed", "content": "survived reload"}

    monkeypatch.setattr(jobs.report_synthesis, "synthesize_corpus", fake_synthesis)
    job_id = jobs.start_job("Alpha", "2026-07-01", "2026-07-31", "owner")
    assert started.wait(5)
    thread = jobs._THREADS[job_id]
    try:
        # Execute a new module object as the Streamlit watcher does after eviction.
        spec = importlib.util.spec_from_file_location(jobs.__name__, jobs.__file__)
        reimported = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reimported)
        assert reimported._PROCESS_TOKEN == jobs._PROCESS_TOKEN
        assert reimported._THREADS is jobs._THREADS
        assert reimported._THREADS[job_id] is thread
        # Even re-opening the store must not misclassify a live job as interrupted.
        path = str((Path(tmp_path) / "report_synthesis.sqlite3").resolve())
        with jobs._LOCK:
            jobs._STORES.pop(path)
        assert reimported.get_job(job_id, "owner")["status"] == "running"
        assert reimported.start_job("Alpha", "2026-07-01", "2026-07-31", "owner") == job_id
    finally:
        release.set()
        thread.join(5)
    assert jobs.get_job(job_id, "owner")["result"]["content"] == "survived reload"


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "empty"])
def test_delete_owned_terminal_job_preserves_other_jobs_and_cache(tmp_path, monkeypatch, status):
    monkeypatch.setattr(jobs.config, "DATA_ROOT", str(tmp_path))
    store = jobs._store()
    job, _ = store.create_job(_corpus(), "owner", jobs._PROCESS_TOKEN)
    store.finish(job["id"], {"status": status, "content": "result"})
    other, _ = store.create_job(_corpus(), "other-owner", jobs._PROCESS_TOKEN)
    store.finish(other["id"], {"status": "completed", "content": "other"})
    store.put_batch("shared", {"section": "cached"})

    assert not jobs.delete_job(job["id"], "other-owner")
    assert jobs.get_job(job["id"], "owner") is not None
    # Watcher reimports must work with an already cached store instance.
    spec = importlib.util.spec_from_file_location(jobs.__name__, jobs.__file__)
    reimported = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reimported)
    assert reimported.delete_job(job["id"], "owner")
    assert not jobs.delete_job(job["id"], "owner")
    reopened = jobs.SynthesisStore(store.path)
    assert reopened.get_job(job["id"], "owner") is None
    assert jobs.list_jobs("owner") == []
    assert reopened.get_job(other["id"], "other-owner") is not None
    assert reopened.get_batch("shared") == {"section": "cached"}
