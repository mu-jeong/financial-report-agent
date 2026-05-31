import os
import sys
import subprocess
from pathlib import Path

import streamlit as st

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.configs.config import SAVE_DIR
from src.core.conversation_store import (
    append_message,
    create_thread,
    delete_thread,
    get_chat_history,
    list_messages,
    list_threads,
    rename_thread,
)
from src.core.status import get_data_status
from src.graphs.main_graph import graph_app

REPORT_PDF_DIR = os.getenv("REPORT_PDF_DIR", SAVE_DIR)

st.set_page_config(
    page_title="Finance Report Agent",
    layout="wide",
)


def _sidebar_rerun() -> None:
    st.rerun(scope="fragment")


def _app_rerun() -> None:
    st.rerun(scope="app")


def _load_threads() -> list[dict]:
    threads = list_threads()
    if not threads:
        thread_id = create_thread("새로운 대화")
        threads = list_threads()
        st.session_state.current_thread_id = thread_id
    return threads


def _ensure_current_thread(threads: list[dict]) -> None:
    if "current_thread_id" not in st.session_state:
        st.session_state.current_thread_id = threads[0]["id"]
    thread_ids = {thread["id"] for thread in threads}
    if st.session_state.current_thread_id not in thread_ids:
        st.session_state.current_thread_id = threads[0]["id"]


def _resolve_report_pdf(file_name: str) -> Path | None:
    safe_file_name = Path(file_name).name
    pdf_path = Path(REPORT_PDF_DIR).expanduser() / safe_file_name
    if pdf_path.exists() and pdf_path.is_file():
        return pdf_path
    return None


def _open_report_pdf(file_name: str) -> tuple[bool, str | None]:
    pdf_path = _resolve_report_pdf(file_name)
    if pdf_path is None:
        return False, "PDF 파일을 찾을 수 없습니다. REPORT_PDF_DIR 설정과 파일명을 확인해 주세요."
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(pdf_path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(pdf_path)])
        else:
            subprocess.Popen(["xdg-open", str(pdf_path)])
    except OSError as exc:
        return False, f"PDF를 여는 중 오류가 발생했습니다: {exc}"
    return True, None


def _render_sources(rerank_info: list[dict] | None, *, key_prefix: str) -> None:
    if not rerank_info:
        return
    with st.expander(f"참고한 문서 (Top {len(rerank_info)})", expanded=False):
        for index, info in enumerate(rerank_info):
            rank = info.get("rank", "-")
            file_name = info.get("file_name", "-")
            display_text = (
                f"{rank}. {info.get('target_name', '-')} ({info.get('report_date', '-')}) "
                f"- {info.get('broker', '-')} - {file_name}"
            )
            text_col, open_col = st.columns([0.86, 0.14], gap="small", vertical_alignment="center")
            text_col.write(display_text)
            if open_col.button("열기", key=f"{key_prefix}_open_pdf_{index}", use_container_width=True):
                opened, error_message = _open_report_pdf(file_name)
                if not opened:
                    st.warning(error_message)


def _render_message(message: dict, *, index: int) -> None:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "assistant":
            _render_sources(
                (message.get("metadata") or {}).get("rerank_info"),
                key_prefix=f"message_{index}",
            )


def _set_current_thread(thread_id: str) -> None:
    if st.session_state.current_thread_id != thread_id:
        st.session_state.current_thread_id = thread_id
        st.session_state.editing_thread_id = None
        _app_rerun()


def _delete_thread_and_select_next(thread_id: str) -> None:
    was_current = st.session_state.current_thread_id == thread_id
    delete_thread(thread_id)
    remaining_threads = list_threads()
    if not remaining_threads:
        st.session_state.current_thread_id = create_thread("새로운 대화")
        _app_rerun()
    elif was_current:
        st.session_state.current_thread_id = remaining_threads[0]["id"]
        _app_rerun()
    else:
        _sidebar_rerun()


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
        if st.button("저장", key=f"save_thread_{thread_id}", use_container_width=True):
            clean_name = new_name.strip() or "새로운 대화"
            rename_thread(thread_id, clean_name)
            st.session_state.editing_thread_id = None
            if selected:
                _app_rerun()
            else:
                _sidebar_rerun()
        if st.button("취소", key=f"cancel_thread_{thread_id}", use_container_width=True):
            st.session_state.editing_thread_id = None
            _sidebar_rerun()
        return

    label = f"> {thread['name']}" if selected else f"- {thread['name']}"
    thread_col, action_col = st.columns([0.82, 0.18], gap="small", vertical_alignment="center")
    if thread_col.button(label, key=f"thread_{thread_id}", use_container_width=True):
        _set_current_thread(thread_id)
    with action_col.popover("", use_container_width=True):
        if st.button("이름 변경", key=f"edit_thread_{thread_id}", use_container_width=True):
            st.session_state.editing_thread_id = thread_id
            _sidebar_rerun()
        if st.button("삭제", key=f"delete_thread_{thread_id}", use_container_width=True):
            _delete_thread_and_select_next(thread_id)


@st.fragment
def render_sidebar(current_id: str) -> None:
    threads = _load_threads()

    st.title("Finance Report Agent")
    st.markdown("증권사 분석 리포트 AI 어시스턴트")
    st.divider()

    status = get_data_status()
    db_status = status["db"]
    st.subheader("데이터 상태")
    col1, col2 = st.columns(2)
    col1.metric("리포트", f"{db_status['total_reports']}건")
    col2.metric("임베딩", f"{db_status['embedded_reports']}건")
    st.divider()

    if st.button("새 대화 시작", use_container_width=True):
        st.session_state.current_thread_id = create_thread(f"대화 {len(threads) + 1}")
        st.session_state.editing_thread_id = None
        _app_rerun()

    st.subheader("대화 목록")
    for thread in threads:
        _render_thread_row(thread, selected=thread["id"] == current_id)


def render_chat(current_id: str, current_thread: dict) -> None:
    st.header(current_thread["name"])

    messages = list_messages(current_id)
    for index, message in enumerate(messages):
        _render_message(message, index=index)


    if user_query := st.chat_input("질문을 입력해 주세요... (ex: 최근 발행된 현대차 리포트 요약해줘)"):
        if not messages and (current_thread["name"] == "새로운 대화" or current_thread["name"].startswith("대화 ")):
            rename_thread(current_id, user_query[:15] + "...")

        prior_history = get_chat_history(current_id)
        append_message(current_id, "user", user_query)

        with st.chat_message("user"):
            st.markdown(user_query)

        with st.chat_message("assistant"):
            with st.spinner("AI가 리포트 내용을 검색하고 분석 중입니다..."):
                config = {"configurable": {"thread_id": current_id}}
                try:
                    final_state = graph_app.invoke(
                        {"question": user_query, "chat_history": prior_history},
                        config=config,
                    )
                    answer = final_state.get("generation", "응답을 생성하지 못했습니다.")
                    if "Error" in answer or "차단" in answer:
                        answer = f"주의: {answer}"
                    rerank_info = final_state.get("rerank_info", []) if final_state.get("route") == "vectordb" else []
                except Exception as exc:
                    answer = f"오류가 발생했습니다: {exc}"
                    rerank_info = []

            st.markdown(answer)
            _render_sources(rerank_info, key_prefix=f"live_{current_id}_{len(messages)}")
            append_message(current_id, "assistant", answer, {"rerank_info": rerank_info})
        _app_rerun()


threads = _load_threads()
_ensure_current_thread(threads)
current_id = st.session_state.current_thread_id
current_thread = next(thread for thread in threads if thread["id"] == current_id)

with st.sidebar:
    render_sidebar(current_id)

render_chat(current_id, current_thread)
