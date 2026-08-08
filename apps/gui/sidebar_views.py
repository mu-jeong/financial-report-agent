"""Conversation navigation and sidebar data-status composition."""

import streamlit as st

from apps.gui import chat_jobs
from apps.gui import data_views
from apps.gui import status_cache
from src.configs import config as config_module
from src.core import conversation_store
from src.core import monitoring


def _sidebar_rerun() -> None:
    st.rerun()


def _app_rerun() -> None:
    st.rerun(scope="app")


def load_threads() -> list[dict]:
    chat_jobs.repair_interrupted_chat_jobs()
    threads = conversation_store.list_threads()
    if not threads:
        thread_id = conversation_store.create_thread("새로운 대화")
        threads = conversation_store.list_threads()
        st.session_state.current_thread_id = thread_id
    return threads


def ensure_current_thread(threads: list[dict]) -> None:
    if "current_thread_id" not in st.session_state:
        st.session_state.current_thread_id = threads[0]["id"]
    thread_ids = {thread["id"] for thread in threads}
    if st.session_state.current_thread_id not in thread_ids:
        st.session_state.current_thread_id = threads[0]["id"]


def _set_current_thread(thread_id: str) -> None:
    if st.session_state.current_thread_id != thread_id:
        st.session_state.current_thread_id = thread_id
        st.session_state.editing_thread_id = None
        st.session_state.show_issue_report_form = False
        _app_rerun()


def _delete_thread_and_select_next(thread_id: str) -> None:
    was_current = st.session_state.current_thread_id == thread_id
    conversation_store.delete_thread(thread_id)
    st.session_state.editing_thread_id = None
    remaining_threads = conversation_store.list_threads()
    if not remaining_threads:
        st.session_state.current_thread_id = conversation_store.create_thread(
            "새로운 대화"
        )
    elif was_current:
        st.session_state.current_thread_id = remaining_threads[0]["id"]
    _app_rerun()


def _thread_status_badge(thread_id: str) -> str:
    messages = conversation_store.list_messages(thread_id)
    if chat_jobs.thread_has_running_job(messages):
        return "⏳"
    if any(
        message.get("role") == "assistant"
        and (message.get("metadata") or {}).get("status") == "failed"
        for message in messages
    ):
        return "⚠"
    return ""


def _render_thread_row(thread: dict, *, selected: bool) -> None:
    editing_id = st.session_state.get("editing_thread_id")
    thread_id = thread["id"]

    if editing_id == thread_id:
        new_name = st.text_input(
            "대화명",
            value=thread["name"],
            key=f"rename_input_{thread_id}",
            label_visibility="collapsed",
        )
        if st.button(
            "저장",
            key=f"save_thread_{thread_id}",
            width="stretch",
        ):
            clean_name = new_name.strip() or "새로운 대화"
            conversation_store.rename_thread(thread_id, clean_name)
            st.session_state.editing_thread_id = None
            if selected:
                _app_rerun()
            else:
                _sidebar_rerun()
        if st.button(
            "취소",
            key=f"cancel_thread_{thread_id}",
            width="stretch",
        ):
            st.session_state.editing_thread_id = None
            _sidebar_rerun()
        return

    badge = _thread_status_badge(thread_id)
    status_prefix = f"{badge} " if badge else ""
    pin_prefix = "★ " if thread.get("pinned") else ""
    label = (
        f"> {status_prefix}{pin_prefix}{thread['name']}"
        if selected
        else f"- {status_prefix}{pin_prefix}{thread['name']}"
    )
    pin_col, thread_col, edit_col, delete_col = st.columns(
        [0.12, 0.60, 0.14, 0.14],
        gap="small",
        vertical_alignment="center",
    )
    pin_label = "★" if thread.get("pinned") else "☆"
    pin_help = "고정 해제" if thread.get("pinned") else "대화 고정"
    if pin_col.button(
        pin_label,
        key=f"pin_thread_{thread_id}",
        width="stretch",
        help=pin_help,
    ):
        conversation_store.set_thread_pinned(
            thread_id,
            not bool(thread.get("pinned")),
        )
        _sidebar_rerun()
    if thread_col.button(
        label,
        key=f"thread_{thread_id}",
        width="stretch",
    ):
        _set_current_thread(thread_id)
    if edit_col.button(
        "✎",
        key=f"edit_thread_{thread_id}",
        width="stretch",
        help="이름 변경",
    ):
        st.session_state.editing_thread_id = thread_id
        _sidebar_rerun()
    if delete_col.button(
        "×",
        key=f"delete_thread_{thread_id}",
        width="stretch",
        help="삭제",
    ):
        _delete_thread_and_select_next(thread_id)


def render_sidebar(current_id: str) -> dict:
    threads = load_threads()

    st.title("Finance Report Agent")
    if config_module.MONITORING_MODE:
        st.caption("Monitoring Mode ON")
        st.radio(
            "화면",
            monitoring.build_monitoring_page_labels(),
            key="active_monitoring_page",
            label_visibility="collapsed",
        )

    if st.button("새 대화 시작", width="stretch"):
        st.session_state.current_thread_id = conversation_store.create_thread(
            f"대화 {len(threads) + 1}"
        )
        st.session_state.editing_thread_id = None
        _app_rerun()

    st.subheader("대화 목록")
    for thread in threads:
        _render_thread_row(thread, selected=thread["id"] == current_id)

    with st.container(key="sidebar_data_status_bottom"):
        st.divider()
        status = status_cache.get_data_status()
        db_status = status["db"]
        data_views.render_report_calendar(db_status)
        data_views.render_data_update_controls(db_status)
        st.divider()
        st.subheader("데이터 상태")
        col1, col2 = st.columns(2)
        col1.metric("리포트", f"{db_status['total_reports']}건")
        col2.metric("처리됨", f"{db_status['embedded_reports']}건")
    return status
