from __future__ import annotations

import sqlite3

import pytest

from src.core.report_synthesis_store import SynthesisStore


def _corpus(fingerprint="scope-a"):
    return {"company": "Alpha", "start_date": "2026-07-01", "end_date": "2026-07-31",
            "fingerprint": fingerprint, "documents": [{"parents": [{"content": "fixed input"}]}], "excluded": []}


def test_result_corpus_and_cache_survive_reopening_with_owner_isolation(tmp_path):
    path = tmp_path / "synthesis.sqlite3"
    store = SynthesisStore(path)
    job, created = store.create_job(_corpus(), "owner", "process-a")
    assert created
    store.put_document("versioned-key", {"claims": [{"quote": "fixed input"}]})
    store.put_batch("batch-key", {"section": "기간 흐름", "statements": []})
    store.progress(job["id"], {"phase": "synthesizing", "completed": 1, "total": 1})
    store.finish(job["id"], {"status": "completed", "content": "result"})
    reopened = SynthesisStore(path)
    saved = reopened.get_job(job["id"], "owner", include_corpus=True)
    assert saved["result"]["content"] == "result"
    assert saved["corpus"]["documents"][0]["parents"][0]["content"] == "fixed input"
    assert reopened.get_document("versioned-key")["claims"][0]["quote"] == "fixed input"
    assert reopened.get_batch("batch-key") == {"section": "기간 흐름", "statements": []}
    assert reopened.get_job(job["id"], "other-owner") is None
    assert reopened.list_jobs("other-owner") == []


def test_duplicate_running_scope_reuses_job_but_different_scope_is_blocked(tmp_path):
    store = SynthesisStore(tmp_path / "synthesis.sqlite3")
    job, _ = store.create_job(_corpus(), "owner", "process")
    duplicate, created = store.create_job(_corpus(), "owner", "process")
    assert not created and duplicate["id"] == job["id"]
    with pytest.raises(ValueError, match="이미"):
        store.create_job(_corpus("different"), "owner", "process")
    assert store.create_job(_corpus("different"), "another-owner", "process")[1]


def test_restart_repairs_interrupted_job_and_preserves_cache_and_frozen_input(tmp_path):
    store = SynthesisStore(tmp_path / "synthesis.sqlite3")
    job, _ = store.create_job(_corpus(), "owner", "old-process")
    store.put_batch("cache-key", {"section": "완료된 중간 종합"})
    store.repair_interrupted("old-process")
    assert store.get_job(job["id"], "owner")["status"] == "running"
    store.repair_interrupted("new-process")
    saved = store.get_job(job["id"], "owner", include_corpus=True)
    assert saved["status"] == "failed" and "완료된 종합 단계" in saved["error"]
    assert saved["corpus"] == _corpus()
    assert store.get_batch("cache-key") == {"section": "완료된 중간 종합"}
    store.finish(job["id"], {"status": "completed"})
    assert store.get_job(job["id"], "owner")["status"] == "failed"


def test_segment_cache_survives_reopening_and_is_independent_from_document_cache(tmp_path):
    path = tmp_path / "synthesis.sqlite3"
    store = SynthesisStore(path)
    store.put_segment("shared-key", {"claims": [{"quote": "segment"}]})
    store.put_document("shared-key", {"claims": [{"quote": "document"}]})

    reopened = SynthesisStore(path)
    assert reopened.get_segment("shared-key") == {"claims": [{"quote": "segment"}]}
    assert reopened.get_document("shared-key") == {"claims": [{"quote": "document"}]}


def test_new_job_starts_preparing_and_accepts_preparing_progress(tmp_path):
    store = SynthesisStore(tmp_path / "synthesis.sqlite3")
    job, _ = store.create_job(_corpus(), "owner", "process")
    assert job["phase"] == "preparing"

    store.progress(job["id"], {"phase": "preparing", "completed": 1, "total": 3})
    saved = store.get_job(job["id"], "owner")
    assert (saved["phase"], saved["completed"], saved["total"]) == ("preparing", 1, 3)


@pytest.mark.parametrize("stored_value", ["not-json", "[]", '"scalar"'])
def test_segment_cache_ignores_corrupt_or_non_dict_values(tmp_path, stored_value):
    path = tmp_path / "synthesis.sqlite3"
    store = SynthesisStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO synthesis_segment_cache (key, analysis_json, created_at) VALUES (?, ?, ?)",
            ("bad-key", stored_value, "2026-10-06T00:00:00+00:00"),
        )

    assert store.get_segment("bad-key") is None


@pytest.mark.parametrize("stored_value", ["not-json", "[]", '"scalar"'])
def test_batch_cache_ignores_corrupt_or_non_dict_values(tmp_path, stored_value):
    path = tmp_path / "synthesis.sqlite3"
    store = SynthesisStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO synthesis_batch_cache (key, output_json, created_at) VALUES (?, ?, ?)",
            ("bad-key", stored_value, "2026-10-06T00:00:00+00:00"),
        )

    assert store.get_batch("bad-key") is None
