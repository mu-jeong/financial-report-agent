"""End-to-end checks for the synthesis UI, job runner, and SQLite store."""

from __future__ import annotations

import importlib.util
import threading
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from apps.gui import synthesis_jobs, synthesis_views
from src.core import report_synthesis_store
from tests.test_synthesis_views import _result


def _corpus(*, company: str = "Alpha", fingerprint: str = "fixed-scope") -> dict:
    return {
        "company": company,
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "fingerprint": fingerprint,
        "profile_id": "profile",
        "revision": {},
        "documents": [{
            "report_uid": "a" * 64,
            "canonical_relative_path": "downloaded/one.pdf",
            "file_name": "one.pdf",
            "report_date": "2026-01-15",
            "broker": "Test Securities",
            "title": "January report",
            "parents": [{"content": "Current report body", "start_index": 0}],
        }],
        "excluded": [],
        "warnings": [],
    }


def _result_for(corpus: dict, content: str) -> dict:
    result = _result()
    result.update({
        "company": corpus["company"],
        "start_date": corpus["start_date"],
        "end_date": corpus["end_date"],
        "fingerprint": corpus["fingerprint"],
        "content": f"{content} [1]",
    })
    return result


def _fresh_jobs_module():
    spec = importlib.util.spec_from_file_location(synthesis_jobs.__name__, synthesis_jobs.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page_harness(tmp_path: Path) -> Path:
    harness = tmp_path / "synthesis_page.py"
    harness.write_text(
        "from apps.gui.synthesis_views import render_synthesis_page\n"
        "render_synthesis_page()\n",
        encoding="utf-8",
    )
    return harness


def test_watcher_reimport_keeps_running_job_visible_until_completion(tmp_path, monkeypatch):
    """A watcher reload must keep the old store and worker usable by the real page."""
    corpus = _corpus()
    started = threading.Event()
    release = threading.Event()

    def synthesize(snapshot, *, on_progress, cache):
        on_progress({"phase": "analyzing", "completed": 0, "total": 1})
        started.set()
        assert release.wait(10)
        return _result_for(snapshot, "survived watcher reload")

    monkeypatch.setattr(synthesis_jobs.config, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(synthesis_views, "_session_owner_id", lambda: "owner")
    monkeypatch.setattr(synthesis_views.report_corpus, "list_companies", lambda: ["Alpha"])
    monkeypatch.setattr(synthesis_views.report_corpus, "load_report_corpus", lambda *_: corpus)
    monkeypatch.setattr(synthesis_jobs.report_synthesis, "synthesize_corpus", synthesize)

    app = AppTest.from_file(str(_page_harness(tmp_path))).run(timeout=30)
    app.radio[0].set_value("직접 선택")
    app.date_input[0].set_value((date(2026, 1, 1), date(2026, 1, 31)))
    thread = None
    job_id = None
    reimported = None
    try:
        next(button for button in app.button if button.label == "종합 생성").click().run(timeout=30)
        assert started.wait(5)
        job_id = app.session_state["synthesis_current_job_id"]
        thread = synthesis_jobs._THREADS[job_id]
        path = str((tmp_path / "report_synthesis.sqlite3").resolve())
        old_store = synthesis_jobs._STORES[path]
        reimported = _fresh_jobs_module()
        monkeypatch.setattr(synthesis_views, "synthesis_jobs", reimported)
        assert reimported._STORES[path] is old_store
        app.run(timeout=30)
        assert not app.exception
        assert any("리포트 분석" in item.value for item in app.info)
        assert reimported.get_job(job_id, "owner")["status"] == "running"
    finally:
        release.set()
        if thread is not None:
            thread.join(10)
    assert thread is not None
    assert not thread.is_alive()
    assert job_id is not None and reimported is not None
    app.run(timeout=30)
    assert not app.exception
    assert reimported.get_job(job_id, "owner")["status"] == "completed"
    assert any("survived watcher reload" in item.value for item in app.markdown)
    assert app.selectbox[0].value == "Alpha"
    assert app.date_input[0].value == (date(2026, 1, 1), date(2026, 1, 31))


def test_orphaned_job_is_shown_as_failed_then_retries_with_saved_scope(tmp_path, monkeypatch):
    """The page should turn a stale running row into an actionable retry."""
    corpus = _corpus(company="Beta", fingerprint="orphaned-scope")
    monkeypatch.setattr(synthesis_jobs.config, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(synthesis_views, "_session_owner_id", lambda: "owner")
    monkeypatch.setattr(synthesis_views.report_corpus, "list_companies", lambda: ["Alpha", "Beta"])
    monkeypatch.setattr(
        synthesis_jobs.report_synthesis,
        "synthesize_corpus",
        lambda snapshot, **_kwargs: _result_for(snapshot, "recovered result"),
    )

    direct_store = report_synthesis_store.SynthesisStore(
        tmp_path / "report_synthesis.sqlite3",
    )
    orphaned, created = direct_store.create_job(
        corpus, "owner", synthesis_jobs._PROCESS_TOKEN,
    )
    assert created and orphaned["status"] == "running"

    app = AppTest.from_file(str(_page_harness(tmp_path))).run(timeout=30)
    assert not app.exception
    repaired = synthesis_jobs.get_job(orphaned["id"], "owner")
    assert repaired["status"] == "failed"
    assert any("작업 결과를 저장하지 못해" in item.value for item in app.error)
    assert app.selectbox[0].value == "Beta"
    assert app.radio[0].value == "직접 선택"
    assert app.date_input[0].value == (date(2026, 1, 1), date(2026, 1, 31))

    retry = next(button for button in app.button if button.label == "실패한 작업 다시 시도")
    retry.click().run(timeout=30)
    new_id = app.session_state["synthesis_current_job_id"]
    thread = synthesis_jobs._THREADS.get(new_id)
    if thread is not None:
        thread.join(10)
        assert not thread.is_alive()
    app.run(timeout=30)

    assert not app.exception
    assert new_id != orphaned["id"]
    assert synthesis_jobs.get_job(new_id, "owner")["status"] == "completed"
    assert any("recovered result" in item.value for item in app.markdown)
    assert app.selectbox[0].value == "Beta"
    assert app.date_input[0].value == (date(2026, 1, 1), date(2026, 1, 31))


def test_sidebar_delete_uses_core_helper_without_touching_other_rows_or_caches(
    tmp_path, monkeypatch,
):
    """Deleting one visible terminal result must leave unrelated durable data intact."""
    monkeypatch.setattr(synthesis_jobs.config, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(synthesis_views, "_session_owner_id", lambda: "owner")
    store = synthesis_jobs._store()
    remaining, _ = store.create_job(_corpus(fingerprint="remaining"), "owner", synthesis_jobs._PROCESS_TOKEN)
    store.finish(remaining["id"], _result_for(_corpus(fingerprint="remaining"), "remaining"))
    target_corpus = _corpus(company="Beta", fingerprint="target")
    target, _ = store.create_job(target_corpus, "owner", synthesis_jobs._PROCESS_TOKEN)
    store.finish(target["id"], _result_for(target_corpus, "target"))
    other, _ = store.create_job(_corpus(fingerprint="other"), "other-owner", synthesis_jobs._PROCESS_TOKEN)
    store.finish(other["id"], _result_for(_corpus(fingerprint="other"), "other"))
    store.put_document("document-cache", {"value": "document"})
    store.put_segment("segment-cache", {"value": "segment"})
    store.put_batch("batch-cache", {"value": "batch"})

    harness = tmp_path / "synthesis_history.py"
    harness.write_text(
        "import streamlit as st\n"
        "from apps.gui import sidebar_views\n"
        "with st.sidebar:\n"
        "    sidebar_views._render_synthesis_history()\n",
        encoding="utf-8",
    )
    app = AppTest.from_file(str(harness)).run(timeout=30)
    assert not app.exception
    app.sidebar.button(key=f"delete_synthesis_{target['id']}").click().run(timeout=30)

    assert not app.exception
    assert store.get_job(target["id"], "owner") is None
    assert store.get_job(remaining["id"], "owner") is not None
    assert store.get_job(other["id"], "other-owner") is not None
    assert store.get_document("document-cache") == {"value": "document"}
    assert store.get_segment("segment-cache") == {"value": "segment"}
    assert store.get_batch("batch-cache") == {"value": "batch"}
    assert any(remaining["id"] in button.key for button in app.sidebar.button)
