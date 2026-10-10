"""Synthesis history preserves ownership and opens the exact saved result."""

from apps.gui import sidebar_views as sidebar
from tests.test_synthesis_views import _FakeStreamlit
import sqlite3

import pytest


def test_history_opens_saved_job_and_restores_its_scope(monkeypatch):
    job = {"id": "older-job", "company": "삼성전자", "start_date": "2026-01-01",
           "end_date": "2026-01-31", "status": "partial"}
    label = "- 삼성전자 리포트 종합 · 2026-01-01 ~ 2026-01-31 · ⚠ 부분 완료"
    ui = _FakeStreamlit(pressed=[label])
    ui.session_state.update(active_user_page="Chat", synthesis_current_job_id="newer-job",
                            synthesis_company="다른 기업", synthesis_confirmed_corpus={"old": True})
    monkeypatch.setattr(sidebar, "st", ui)
    monkeypatch.setattr(sidebar.synthesis_views, "_session_owner_id", lambda: "owner")
    calls = []
    monkeypatch.setattr(sidebar.synthesis_jobs, "list_jobs",
                        lambda owner, limit: calls.append((owner, limit)) or [job])
    sidebar._render_synthesis_history()
    assert calls == [("owner", 100)]
    assert ui.session_state.active_user_page == "기업 리포트 종합"
    assert ui.session_state.synthesis_current_job_id == "older-job"
    assert ui.session_state.synthesis_saved_scope == {
        key: job[key] for key in ("company", "start_date", "end_date")
    }
    assert "synthesis_company" not in ui.session_state
    assert "synthesis_confirmed_corpus" not in ui.session_state
    assert ui.reruns == [{"scope": "app"}]


def test_empty_owned_history_has_no_buttons(monkeypatch):
    ui = _FakeStreamlit()
    monkeypatch.setattr(sidebar, "st", ui)
    monkeypatch.setattr(sidebar.synthesis_views, "_session_owner_id", lambda: "owner")
    monkeypatch.setattr(sidebar.synthesis_jobs, "list_jobs", lambda owner, limit: [])
    sidebar._render_synthesis_history()
    assert not ui.buttons
    assert not ui.reruns


@pytest.mark.parametrize("error", [sqlite3.DatabaseError("invalid database"), OSError("unavailable storage")])
def test_unavailable_history_does_not_break_chat_sidebar(monkeypatch, error):
    ui = _FakeStreamlit()
    monkeypatch.setattr(sidebar, "st", ui)
    monkeypatch.setattr(sidebar.synthesis_views, "_session_owner_id", lambda: "owner")

    def unavailable(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(sidebar.synthesis_jobs, "list_jobs", unavailable)
    sidebar._render_synthesis_history()
    assert any(kind == "warning" and "종합 기록" in message for kind, message in ui.messages)
    assert not ui.buttons


def test_running_history_cannot_be_deleted(monkeypatch):
    ui = _FakeStreamlit(pressed=["×"])
    monkeypatch.setattr(sidebar, "st", ui)
    monkeypatch.setattr(sidebar.synthesis_views, "_session_owner_id", lambda: "owner")
    job = {"id": "running", "company": "Alpha", "start_date": "2026-07-01",
           "end_date": "2026-07-31", "status": "running"}
    monkeypatch.setattr(sidebar.synthesis_jobs, "list_jobs", lambda *_args, **_kwargs: [job])
    deleted = []
    monkeypatch.setattr(sidebar.synthesis_jobs, "delete_job", lambda *args: deleted.append(args))
    sidebar._render_synthesis_history()
    button = next(kwargs for label, kwargs in ui.buttons if label == "×")
    assert button["disabled"]
    assert not deleted and not ui.reruns


def test_deleting_other_history_preserves_open_result_and_chat(monkeypatch):
    ui = _FakeStreamlit()
    ui.session_state.update(synthesis_current_job_id="selected", synthesis_saved_scope={"saved": True},
                            synthesis_company="Alpha", current_thread_id="followup", active_user_page="Chat")
    monkeypatch.setattr(sidebar, "st", ui)
    deleted = []
    monkeypatch.setattr(sidebar.synthesis_jobs, "delete_job", lambda *args: deleted.append(args) or True)
    expected = dict(ui.session_state)
    sidebar._delete_synthesis_job("other", "owner")
    assert deleted == [("other", "owner")]
    assert dict(ui.session_state) == expected
    assert ui.reruns == [{"scope": "app"}]


@pytest.mark.parametrize("error", [sqlite3.DatabaseError("private details"), OSError("private details"),
                                   ValueError("생성 중인 종합 작업은 완료 후 삭제할 수 있습니다.")])
def test_delete_failure_keeps_selected_result_and_hides_storage_details(monkeypatch, error):
    ui = _FakeStreamlit()
    ui.session_state.update(synthesis_current_job_id="selected", synthesis_company="Alpha")
    monkeypatch.setattr(sidebar, "st", ui)

    def failure(*_args):
        raise error

    monkeypatch.setattr(sidebar.synthesis_jobs, "delete_job", failure)
    sidebar._delete_synthesis_job("selected", "owner")
    assert ui.session_state.synthesis_current_job_id == "selected"
    assert ui.session_state.synthesis_company == "Alpha"
    assert any(kind == "warning" for kind, _ in ui.messages)
    assert all("private details" not in message for _, message in ui.messages)
    assert not ui.reruns


@pytest.mark.parametrize("external", [False, True])
def test_delete_selected_history_switches_to_remaining_then_clears_last_result(tmp_path, monkeypatch, external):
    from streamlit.testing.v1 import AppTest
    from tests.test_synthesis_jobs import _corpus

    monkeypatch.setattr(sidebar.synthesis_jobs.config, "DATA_ROOT", str(tmp_path))
    store = sidebar.synthesis_jobs._store()
    older, _ = store.create_job(_corpus(), "owner", sidebar.synthesis_jobs._PROCESS_TOKEN)
    store.finish(older["id"], {"status": "completed", "content": "older"})
    newest, _ = store.create_job({**_corpus(), "company": "Beta"}, "owner", sidebar.synthesis_jobs._PROCESS_TOKEN)
    store.finish(newest["id"], {"status": "completed", "content": "newest"})
    monkeypatch.setattr(sidebar.synthesis_views, "_session_owner_id", lambda: "owner")
    harness = tmp_path / "history_deletion.py"
    harness.write_text('''
import streamlit as st
from apps.gui import sidebar_views, synthesis_views

if not st.session_state.get("seeded"):
    st.session_state.update(seeded=True, active_user_page="기업 리포트 종합",
                            current_thread_id="followup", synthesis_company="stale draft",
                            synthesis_confirmed_corpus={"stale": True})
with st.sidebar:
    sidebar_views._render_synthesis_history()
    st.caption("데이터 상태")
job = synthesis_views._latest_owned_job("owner")
st.text_input("회사", value=job["company"] if job else "", key="synthesis_company")
st.write(job["result"]["content"] if job else "EMPTY FORM")
''', encoding="utf-8")
    app = AppTest.from_file(str(harness)).run(timeout=30)
    assert not app.exception
    assert app.session_state["synthesis_current_job_id"] == newest["id"]
    if external:
        assert sidebar.synthesis_jobs.delete_job(newest["id"], "owner")
        app.run(timeout=30)
    else:
        app.sidebar.button(key=f"delete_synthesis_{newest['id']}").click().run(timeout=30)
    assert not app.exception
    assert app.session_state["synthesis_current_job_id"] == older["id"]
    assert app.text_input[0].value == "Alpha"
    assert any(item.value == "older" for item in app.markdown)
    assert "synthesis_confirmed_corpus" not in app.session_state
    assert app.session_state["current_thread_id"] == "followup"
    if external:
        assert sidebar.synthesis_jobs.delete_job(older["id"], "owner")
        app.run(timeout=30)
    else:
        app.sidebar.button(key=f"delete_synthesis_{older['id']}").click().run(timeout=30)
    assert not app.exception
    assert "synthesis_current_job_id" not in app.session_state
    assert app.text_input[0].value == ""
    assert any(item.value == "EMPTY FORM" for item in app.markdown)
    assert store.list_jobs("owner") == []
    # AppTest merges pre-rerun deltas from the click run, without the browser's
    # stale-element cleanup. A fresh run checks the settled empty sidebar.
    app.run(timeout=30)
    assert not app.exception
    assert not app.sidebar.button
