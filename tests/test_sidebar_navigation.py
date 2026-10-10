"""Exercise sidebar navigation across research and monitoring pages."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize(
    ("operator_enabled", "target", "expected_options", "expected_page"),
    [
        (False, "개선 실험", ["Chat", "개선 실험"], "EXPERIMENT PAGE"),
        (True, "Monitoring", ["Chat", "Monitoring", "개선 실험"], "OPERATOR PAGE"),
    ],
)
def test_monitoring_selector_remains_visible_and_leaves_synthesis_page(
    tmp_path, operator_enabled, target, expected_options, expected_page
):
    harness = tmp_path / "sidebar_navigation_harness.py"
    harness.write_text("""
import streamlit as st
from unittest.mock import patch
from apps.gui import sidebar_views

st.session_state.setdefault("active_user_page", "기업 리포트 종합")
st.session_state.setdefault("current_thread_id", "thread-1")
threads = [{"id": "thread-1", "name": "대화", "pinned": False}]
status = {"db": {"total_reports": 0, "embedded_reports": 0}}

with patch.object(sidebar_views, "load_threads", return_value=threads), \
     patch.object(sidebar_views, "_render_thread_row"), \
     patch.object(sidebar_views.status_cache, "get_data_status", return_value=status), \
     patch.object(sidebar_views.data_views, "render_report_calendar"), \
     patch.object(sidebar_views.data_views, "render_data_update_controls"):
    with st.sidebar:
        sidebar_views.render_sidebar(
            "thread-1",
            monitoring_enabled=True,
            operator_monitoring_enabled=__OPERATOR_ENABLED__,
        )

if st.session_state.active_user_page == "기업 리포트 종합":
    st.write("SYNTHESIS PAGE")
elif st.session_state.get("active_monitoring_page") == "Monitoring":
    st.write("OPERATOR PAGE")
elif st.session_state.get("active_monitoring_page") == "개선 실험":
    st.write("EXPERIMENT PAGE")
else:
    st.write("CHAT PAGE")
""".replace("__OPERATOR_ENABLED__", repr(operator_enabled)), encoding="utf-8")

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    assert app.session_state["active_user_page"] == "기업 리포트 종합"
    assert len(app.sidebar.radio) == 2
    assert app.sidebar.radio[1].options == expected_options
    assert ("Monitoring" in app.sidebar.radio[1].options) is operator_enabled
    assert any(item.value == "SYNTHESIS PAGE" for item in app.markdown)

    app.sidebar.radio[1].set_value(target).run(timeout=30)

    assert not app.exception
    assert app.session_state["active_user_page"] == "Chat"
    assert app.session_state["active_monitoring_page"] == target
    assert app.session_state["_user_page_selection"] == "Chat"
    assert any(item.value == expected_page for item in app.markdown)
    assert all(item.value != "SYNTHESIS PAGE" for item in app.markdown)


def test_research_chat_selection_clears_saved_operator_route(tmp_path):
    harness = tmp_path / "sidebar_research_chat_harness.py"
    harness.write_text("""
import streamlit as st
from unittest.mock import patch
from apps.gui import sidebar_views

st.session_state.setdefault("active_user_page", "기업 리포트 종합")
st.session_state.setdefault("active_monitoring_page", "Monitoring")
st.session_state.setdefault("current_thread_id", "thread-1")
threads = [{"id": "thread-1", "name": "대화", "pinned": False}]
status = {"db": {"total_reports": 0, "embedded_reports": 0}}

with patch.object(sidebar_views, "load_threads", return_value=threads), \
     patch.object(sidebar_views, "_render_thread_row"), \
     patch.object(sidebar_views.status_cache, "get_data_status", return_value=status), \
     patch.object(sidebar_views.data_views, "render_report_calendar"), \
     patch.object(sidebar_views.data_views, "render_data_update_controls"):
    with st.sidebar:
        sidebar_views.render_sidebar(
            "thread-1",
            monitoring_enabled=True,
            operator_monitoring_enabled=True,
        )

if st.session_state.active_user_page == "기업 리포트 종합":
    st.write("SYNTHESIS PAGE")
elif st.session_state.get("active_monitoring_page") == "Monitoring":
    st.write("OPERATOR PAGE")
else:
    st.write("CHAT PAGE")
""", encoding="utf-8")

    app = AppTest.from_file(str(harness)).run(timeout=30)

    assert not app.exception
    assert app.sidebar.radio[0].value == "기업 리포트 종합"
    assert app.sidebar.radio[1].value == "Monitoring"
    assert any(item.value == "SYNTHESIS PAGE" for item in app.markdown)

    app.sidebar.radio[0].set_value("Chat").run(timeout=30)

    assert not app.exception
    assert app.session_state["active_user_page"] == "Chat"
    assert app.session_state["active_monitoring_page"] == "Chat"
    assert app.sidebar.radio[0].value == "Chat"
    assert app.sidebar.radio[1].value == "Chat"
    assert any(item.value == "CHAT PAGE" for item in app.markdown)
    assert all(item.value != "OPERATOR PAGE" for item in app.markdown)
