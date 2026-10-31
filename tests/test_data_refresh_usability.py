"""Regression coverage for ordinary-user freshness and recovery controls."""

from __future__ import annotations

import inspect

import pytest
from streamlit.testing.v1 import AppTest

from apps.gui import data_views


def test_selected_scope_check_status_uses_periodic_fragment():
    source = inspect.getsource(data_views._render_matching_collection_check)

    assert "@st.fragment(run_every=2.0)" in source


def test_existing_date_can_be_explicitly_rechecked(tmp_path):
    harness = tmp_path / "data_recheck_harness.py"
    harness.write_text(
        """
import streamlit as st
from datetime import date as RealDate
from unittest.mock import patch
from apps.gui import data_views

class FrozenDate(RealDate):
    @classmethod
    def today(cls):
        return cls(2026, 10, 19)

def start_update_job(**kwargs):
    st.session_state["started_update"] = kwargs
    return {"state": "running"}

today = FrozenDate.today().isoformat()
db_status = {
    "min_report_date": today,
    "max_report_date": today,
    "report_date_type_counts": {today: {"company": 7}},
}

with patch.object(data_views.config_module, "CRAWLER_CATEGORIES", "company"), \
     patch.object(data_views, "date", FrozenDate), \
     patch.object(data_views.data_update_jobs, "read_status", return_value=None), \
     patch.object(data_views.data_update_jobs, "start_update_job", side_effect=start_update_job), \
     patch.object(data_views.collection_check_store, "latest_matching_check", return_value=None):
    data_views.render_data_update_controls(db_status)
""",
        encoding="utf-8",
    )

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    recheck = next(button for button in app.button if button.label == "선택 기간 다시 확인")
    assert not recheck.disabled

    recheck.click().run(timeout=30)

    assert not app.exception
    started = app.session_state["started_update"]
    assert started["selected_dates"] == ["2026-10-19"]
    assert started["categories"] == ["company"]
    assert "재확인" in started["label"]


def test_normal_user_can_retry_unprepared_reports(tmp_path):
    harness = tmp_path / "data_recovery_harness.py"
    harness.write_text(
        """
import streamlit as st
from unittest.mock import patch
from apps.gui import data_views

def start_embedding_job(**kwargs):
    st.session_state["started_recovery"] = kwargs
    return {"state": "running"}

status = {
    "db": {"pending_reports": 2},
    "paths": {"data_root": "synthetic-root"},
    "retrieval": {
        "mode": "native",
        "write_epoch": 3,
        "active_snapshot_id": "snapshot-1",
        "write_enabled": True,
        "degraded": False,
    },
}
reports = [
    {"report_date": "2026-10-18", "broker": "테스트증권", "title": "본문 준비 실패 자료"},
    {"report_date": "2026-10-19", "broker": "예시증권", "title": "검색 반영 대기 자료"},
]

with patch.object(data_views.status_module, "list_unembedded_reports", return_value=reports), \
     patch.object(data_views.data_update_jobs, "read_status", return_value=None), \
     patch.object(data_views.data_update_jobs, "start_embedding_job", side_effect=start_embedding_job):
    data_views.render_data_recovery_controls(status)
""",
        encoding="utf-8",
    )

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    assert app.expander[0].label == "검색에 반영되지 않은 자료 · 2건"
    retry = next(
        button for button in app.button if button.label == "반영되지 않은 자료 전체 다시 처리"
    )
    assert not retry.disabled

    retry.click().run(timeout=30)

    assert not app.exception
    assert app.session_state["started_recovery"] == {
        "label": "반영되지 않은 자료 다시 처리",
        "retry_extraction_failures": True,
    }


def test_stale_running_collection_check_is_labeled_interrupted(monkeypatch):
    captions: list[str] = []
    monkeypatch.setattr(
        data_views.collection_check_store,
        "latest_matching_check",
        lambda **_kwargs: {
            "check_id": "stale-job",
            "state": "running",
            "attempted_at": "2026-10-19T01:00:00+00:00",
            "processed_count": 0,
            "failed_count": 0,
        },
    )
    monkeypatch.setattr(data_views.data_update_jobs, "read_status", lambda: None)
    monkeypatch.setattr(data_views.st, "caption", captions.append)

    data_views._render_matching_collection_check_content(["2026-10-19"], ["company"])

    assert len(captions) == 1
    assert "확인 중단됨" in captions[0]


def test_collection_check_read_failure_is_user_friendly(monkeypatch):
    captions: list[str] = []
    monkeypatch.setattr(
        data_views.collection_check_store,
        "latest_matching_check",
        lambda **_kwargs: (_ for _ in ()).throw(
            data_views.collection_check_store.CollectionCheckStoreError("corrupt")
        ),
    )
    monkeypatch.setattr(data_views.st, "caption", captions.append)

    data_views._render_matching_collection_check_content(["2026-10-19"], ["company"])

    assert captions == [
        "수집 확인 이력을 읽지 못했습니다. 기존 문서는 계속 사용할 수 있습니다."
    ]


@pytest.mark.parametrize("state", ["running", "succeeded", "failed"])
def test_update_progress_keeps_history_warning_separate_from_job_result(tmp_path, state):
    warning = "수집 확인 이력을 저장하지 못했습니다."
    status = {
        "state": state,
        "phase": "embed" if state == "running" else "done",
        "message": "자료 처리 상태",
        "collection_check_warning": warning,
    }
    harness = tmp_path / "history_warning_harness.py"
    harness.write_text(
        f"""
from unittest.mock import patch
from apps.gui import data_views

with patch.object(data_views.data_update_jobs, "read_status", return_value={status!r}), \
     patch.object(data_views.data_update_jobs, "is_update_job_active", return_value=True):
    data_views.render_update_progress()
""",
        encoding="utf-8",
    )

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    assert [item.value for item in app.warning] == [warning]
    result_elements = {"running": app.info, "succeeded": app.success, "failed": app.error}
    assert [item.value for item in result_elements[state]] == ["자료 처리 상태"]


def test_legacy_partial_job_is_shown_as_successful_update(tmp_path):
    status = {
        "state": "succeeded",
        "phase": "done",
        "message": (
            "선택 기간 재확인 10일 (company, industry): 업데이트가 부분 완료되었습니다. "
            "리포트 다운로드 28건 및 문서 처리 90건이 실패했습니다."
        ),
        "label": "선택 기간 재확인 10일 (company, industry)",
        "partial_failure": True,
        "download_failure_count": 28,
        "embedding_failure_count": 90,
    }
    harness = tmp_path / "legacy_partial_update_harness.py"
    harness.write_text(
        f"""
from unittest.mock import patch
from apps.gui import data_views

with patch.object(data_views.data_update_jobs, "read_status", return_value={status!r}):
    data_views.render_update_progress()
""",
        encoding="utf-8",
    )

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    assert not app.warning
    assert [item.value for item in app.success] == [
        "선택 기간 재확인 10일 (company, industry): 데이터 업데이트가 완료되었습니다."
    ]
    assert all("⚠️" not in item.value for item in app.markdown)


def test_partial_collection_check_uses_neutral_exclusion_language(monkeypatch):
    captions: list[str] = []
    monkeypatch.setattr(
        data_views.collection_check_store,
        "latest_matching_check",
        lambda **_kwargs: {
            "state": "partial",
            "completed_at": "2026-10-19T01:00:00+00:00",
            "processed_count": 12,
            "failed_count": 3,
        },
    )
    monkeypatch.setattr(data_views.st, "caption", captions.append)

    data_views._render_matching_collection_check_content(["2026-10-19"], ["company"])

    assert len(captions) == 1
    assert "확인 완료 · 일부 자료 제외" in captions[0]
    assert "다운로드 제외 3건" in captions[0]
    assert "실패" not in captions[0]


def test_unrecorded_collection_result_is_not_shown_as_still_running(monkeypatch):
    captions = []
    monkeypatch.setattr(
        data_views.collection_check_store, "latest_matching_check",
        lambda **_kwargs: {
            "check_id": "current-job", "state": "running",
            "attempted_at": "2026-10-19T01:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        data_views.data_update_jobs, "read_status",
        lambda: {
            "job_id": "current-job", "state": "succeeded",
            "collection_check_warning": "이력 저장 실패",
        },
    )
    monkeypatch.setattr(data_views.st, "caption", captions.append)

    data_views._render_matching_collection_check_content(["2026-10-19"], ["company"])

    assert "이력 저장 실패" in captions[0]
    assert "확인 완료" not in captions[0]
    assert "확인 중" not in captions[0]
