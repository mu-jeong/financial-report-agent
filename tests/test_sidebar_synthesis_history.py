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
