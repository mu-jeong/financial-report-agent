import os
import sys
import subprocess
import calendar
import importlib
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

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
from src.core import data_update_jobs

data_update_jobs = importlib.reload(data_update_jobs)
from src.core.status import get_data_status
from src.graphs.main_graph import graph_app
from src.utils.citations import link_citations_to_sources, source_anchor_id

REPORT_PDF_DIR = os.getenv("REPORT_PDF_DIR", SAVE_DIR)
WEEKDAY_LABELS = ["월", "화", "수", "목", "금"]

st.set_page_config(
    page_title="Finance Report Agent",
    layout="wide",
)


def _inject_ui_styles() -> None:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] [data-testid="stHorizontalBlock"] {
            gap: 0.24rem;
        }
        [data-testid="stSidebar"] [data-testid="stSidebarUserContent"] > div {
            min-height: calc(100vh - 2rem);
            display: flex;
            flex-direction: column;
        }
        [data-testid="stSidebar"] .st-key-sidebar_data_status_bottom {
            margin-top: auto;
            padding-top: 0.75rem;
        }
        [data-testid="stSidebar"] .stButton > button {
            min-height: 1.9rem;
            padding: 0.12rem 0.18rem;
            border-radius: 0.65rem;
            font-size: 0.72rem;
            line-height: 0.9rem;
        }
        [data-testid="stSidebar"] .stButton > button p {
            white-space: nowrap;
            font-size: inherit;
            line-height: inherit;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


_inject_ui_styles()


def _sidebar_rerun() -> None:
    st.rerun()


def _app_rerun() -> None:
    st.rerun(scope="app")


def _scroll_to_anchor(anchor_id: str) -> None:
    """Scroll the Streamlit app to an anchor after the current render pass."""
    components.html(
        f"""
        <script>
        const anchorId = {anchor_id!r};
        const scrollToAnchor = () => {{
          const root = window.parent.document;
          const target = root.getElementById(anchorId);
          if (target) {{
            target.scrollIntoView({{behavior: "smooth", block: "start"}});
          }}
        }};
        setTimeout(scrollToAnchor, 120);
        setTimeout(scrollToAnchor, 500);
        </script>
        """,
        height=0,
    )


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


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _month_options(start: date | None, end: date | None) -> list[tuple[str, str]]:
    if start is None or end is None:
        return []

    options: list[tuple[str, str]] = []
    cursor = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)
    while cursor <= end_month:
        value = f"{cursor.year:04d}-{cursor.month:02d}"
        options.append((value, f"{cursor.year}년 {cursor.month}월"))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return options


def _calendar_day_card_html(day: date, count: int) -> str:
    has_data = count > 0
    background = "linear-gradient(135deg, #10b981 0%, #059669 100%)" if has_data else "#f8fafc"
    border = "#059669" if has_data else "#e2e8f0"
    color = "#ffffff" if has_data else "#94a3b8"
    shadow = "0 4px 10px rgba(16,185,129,0.22)" if has_data else "none"
    count_label = f"{count}건" if has_data else "&nbsp;"
    title = f"{day.isoformat()}: {count}건" if has_data else f"{day.isoformat()}: 데이터 없음"
    return (
        "<div "
        f"title='{escape(title)}' "
        "style='min-height:42px; text-align:center; padding:6px 2px 5px; "
        "border-radius:12px; box-sizing:border-box; "
        f"background:{background}; border:1px solid {border}; color:{color}; "
        f"font-size:0.78rem; line-height:1.0rem; font-weight:800; box-shadow:{shadow};'>"
        f"<div style='font-size:0.82rem;'>{day.day}</div>"
        f"<div style='font-size:0.67rem; margin-top:2px; opacity:0.95;'>{count_label}</div>"
        "</div>"
    )


def _calendar_placeholder_html() -> str:
    return "<div style='min-height:42px; border-radius:12px; background:transparent;'>&nbsp;</div>"


def _set_calendar_month(selected_value: str) -> None:
    st.session_state.report_calendar_month = selected_value


def _render_report_calendar(db_status: dict) -> None:
    date_counts = {
        str(day).strip()[:10]: int(count)
        for day, count in (db_status.get("report_date_counts") or {}).items()
    }
    start = _parse_iso_date(db_status.get("min_report_date"))
    end = _parse_iso_date(db_status.get("max_report_date"))
    options = _month_options(start, end)

    if not options:
        st.caption("리포트 날짜 정보가 없습니다.")
        return

    st.caption(f"데이터 기간: {db_status.get('min_report_date') or '-'} ~ {db_status.get('max_report_date') or '-'}")

    values = [value for value, _ in options]
    current_value = st.session_state.get("report_calendar_month", values[-1])
    if current_value not in values:
        current_value = values[-1]

    year_col, month_col = st.columns([0.58, 0.42], vertical_alignment="center")
    year_values = sorted({int(value[:4]) for value in values})
    current_year = int(current_value[:4])
    current_month = int(current_value[5:7])
    selected_year = year_col.selectbox(
        "연도",
        year_values,
        index=year_values.index(current_year),
        format_func=lambda value: f"{value}년",
        key=f"report_calendar_year_select_{current_value}",
        label_visibility="collapsed",
    )

    months_for_year = [
        int(value[5:7])
        for value in values
        if int(value[:4]) == selected_year
    ]
    default_month = current_month if selected_year == current_year and current_month in months_for_year else months_for_year[-1]
    selected_month = month_col.selectbox(
        "월",
        months_for_year,
        index=months_for_year.index(default_month),
        format_func=lambda value: f"{value}월",
        key=f"report_calendar_month_select_{selected_year}_{current_value}",
        label_visibility="collapsed",
    )

    selected_value = f"{selected_year:04d}-{selected_month:02d}"
    if selected_value in values:
        st.session_state.report_calendar_month = selected_value
    else:
        selected_value = current_value
    current_index = values.index(selected_value)

    nav_prev_col, nav_label_col, nav_next_col = st.columns(
        [0.25, 0.50, 0.25],
        vertical_alignment="center",
    )
    if nav_prev_col.button("◀ 이전", key="report_calendar_prev", disabled=current_index == 0, use_container_width=True):
        _set_calendar_month(values[current_index - 1])
        _sidebar_rerun()
    nav_label_col.markdown(
        "<div style='text-align:center; font-size:0.76rem; font-weight:700; color:#64748b; padding:0.34rem 0;'>"
        "월 이동"
        "</div>",
        unsafe_allow_html=True,
    )
    if nav_next_col.button(
        "다음 ▶",
        key="report_calendar_next",
        disabled=current_index == len(values) - 1,
        use_container_width=True,
    ):
        _set_calendar_month(values[current_index + 1])
        _sidebar_rerun()

    year, month = (int(part) for part in selected_value.split("-"))
    month_prefix = f"{year:04d}-{month:02d}-"
    month_report_count = sum(
        count for day, count in date_counts.items() if str(day).startswith(month_prefix)
    )
    st.caption(f"{year}년 {month}월 리포트 {month_report_count}건")

    selectable_before = date.today()
    header_cols = st.columns(5)
    for col, label in zip(header_cols, WEEKDAY_LABELS):
        col.markdown(
            f"<div style='text-align:center; font-size:0.70rem; color:#64748b; "
            f"font-weight:800; padding:2px 0;'>{label}</div>",
            unsafe_allow_html=True,
        )

    month_weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    for week in month_weeks:
        cols = st.columns(5)
        for day in [day for day in week if day.weekday() < 5]:
            col = cols[day.weekday()]
            if day.month != month:
                col.markdown(_calendar_placeholder_html(), unsafe_allow_html=True)
                continue

            key = day.isoformat()
            count = int(date_counts.get(key, 0) or 0)
            if count > 0:
                col.markdown(_calendar_day_card_html(day, count), unsafe_allow_html=True)
                continue

            if day >= selectable_before:
                col.markdown(_calendar_day_card_html(day, 0), unsafe_allow_html=True)
                continue

            col.markdown(_calendar_day_card_html(day, 0), unsafe_allow_html=True)
    st.markdown(
        """
        <div style="font-size:0.68rem; line-height:1.15rem; color:#64748b; margin-top:0.25rem;">
          <div><b style="color:#059669;">초록색</b>: 임베딩 완료</div>
          <div><b style="color:#64748b;">회색 날짜</b>: 임베딩 미완료/데이터 없음</div>
          <div>월~금 기준으로 표시</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _is_update_job_active(status: dict | None) -> bool:
    return bool(status and status.get("state") == "running")


def _step_icon(status_phase: str | None, step: str, state: str | None) -> str:
    completed_by_phase = {
        "queued": set(),
        "download": set(),
        "embed": {"download"},
        "no_data": {"download"},
        "done": {"download", "embed"},
        "failed": set(),
    }
    if state == "succeeded":
        if status_phase == "no_data":
            return "✅" if step == "download" else "⏭️"
        return "✅"
    if state == "failed":
        return "❌" if step == status_phase else ("✅" if step in completed_by_phase.get(status_phase or "", set()) else "•")
    if step in completed_by_phase.get(status_phase or "", set()):
        return "✅"
    if step == status_phase:
        return "⏳"
    return "•"


def _render_update_steps(status: dict) -> None:
    state = status.get("state")
    phase = status.get("phase")
    embed_detail = ""
    if phase == "embed" and status.get("embedding_current") and status.get("embedding_total"):
        embed_detail = f" ({status['embedding_current']}/{status['embedding_total']})"

    rows = [
        ("download", "리포트 다운로드/확인"),
        ("embed", f"임베딩/검색 인덱스 생성{embed_detail}"),
        ("done", "완료"),
    ]
    st.markdown(
        "\n".join(
            f"<div style='font-size:0.78rem; line-height:1.35rem; color:#475569;'>"
            f"{_step_icon(phase, step, state)} {label}</div>"
            for step, label in rows
        ),
        unsafe_allow_html=True,
    )


@st.fragment(run_every="2s")
def _render_update_progress() -> None:
    status = data_update_jobs.read_status()
    if not status:
        return

    state = status.get("state")
    message = status.get("message", "데이터 업데이트 상태를 확인 중입니다.")
    if state == "running":
        st.info(message)
        _render_update_steps(status)
    elif state == "succeeded":
        st.success(message)
        _render_update_steps(status)
    elif state == "failed":
        st.error(message)
        _render_update_steps(status)


def _iter_weekdays(start_date: date, end_date: date) -> list[date]:
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    days: list[date] = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return days


def _missing_update_dates(start_date: date, end_date: date, date_counts: dict[str, int]) -> list[str]:
    today = date.today()
    missing_dates = [
        day.isoformat()
        for day in _iter_weekdays(start_date, min(end_date, today))
        if int(date_counts.get(day.isoformat(), 0) or 0) == 0
    ]
    return data_update_jobs.normalize_date_list(missing_dates)


def _default_update_range(db_status: dict) -> tuple[date, date]:
    today = date.today()
    latest = _parse_iso_date(db_status.get("max_report_date"))
    if latest and latest < today:
        return date.fromordinal(latest.toordinal() + 1), today
    return today, today


def _update_min_date(db_status: dict) -> date:
    today = date.today()
    db_start = _parse_iso_date(db_status.get("min_report_date"))
    fallback_start = today - timedelta(days=365 * 5)
    if db_start is None:
        return fallback_start
    return min(db_start, fallback_start)


def _render_data_update_controls(db_status: dict) -> None:
    status = data_update_jobs.read_status()
    job_active = _is_update_job_active(status)
    today = date.today()
    latest_range = data_update_jobs.build_update_range(last_date=db_status.get("max_report_date"), today=today)
    date_counts = {
        str(day).strip()[:10]: int(count)
        for day, count in (db_status.get("report_date_counts") or {}).items()
    }

    st.subheader("데이터 업데이트")
    default_start, default_end = _default_update_range(db_status)
    min_update_date = _update_min_date(db_status)
    selected_range = st.date_input(
        "업데이트 기간",
        value=(default_start, default_end),
        min_value=min_update_date,
        max_value=today,
        key="update_date_range",
        help="선택 기간 중 이미 데이터가 있는 평일은 다운로드부터 제외됩니다.",
    )

    if isinstance(selected_range, tuple) and len(selected_range) == 2:
        range_start, range_end = selected_range
        target_dates = _missing_update_dates(range_start, range_end, date_counts)
        skipped_count = len(_iter_weekdays(range_start, min(range_end, today))) - len(target_dates)
        st.caption(
            f"작업 대상: 임베딩 미완료 평일 {len(target_dates)}일"
            + (f" · 임베딩 완료 {skipped_count}일 제외" if skipped_count > 0 else "")
        )
        if target_dates:
            if st.button(
                "선택 기간 업데이트",
                key="update_selected_range",
                disabled=job_active,
                use_container_width=True,
            ):
                data_update_jobs.start_update_job(
                    selected_dates=target_dates,
                    label=f"선택 기간 {len(target_dates)}일",
                )
                _sidebar_rerun()
        else:
            st.caption("선택 기간 내 업데이트할 임베딩 미완료 평일이 없습니다.")
    else:
        st.caption("업데이트할 시작일과 종료일을 선택해 주세요.")

    if latest_range:
        start_date, end_date = latest_range
        st.caption(f"최신 누락 범위\n\n`{start_date}` ~ `{end_date}`")

    _render_update_progress()


def _source_rank(info: dict, fallback_rank: int) -> int:
    try:
        rank = int(info.get("rank", fallback_rank))
    except (TypeError, ValueError):
        rank = fallback_rank
    return max(rank, 1)


def _render_sources(
    rerank_info: list[dict] | None,
    *,
    key_prefix: str,
    anchor_prefix: str,
    expanded: bool = False,
) -> None:
    if not rerank_info:
        return
    with st.expander(f"참고한 문서 (Top {len(rerank_info)})", expanded=expanded):
        for index, info in enumerate(rerank_info):
            rank = _source_rank(info, index + 1)
            file_name = info.get("file_name", "-")
            display_text = (
                f"{rank}. {info.get('target_name', '-')} ({info.get('report_date', '-')}) "
                f"- {info.get('broker', '-')} - {file_name}"
            )
            st.markdown(
                (
                    f"<div id='{source_anchor_id(anchor_prefix, rank)}' "
                    "style='scroll-margin-top: 96px; height: 0; visibility: hidden;'></div>"
                ),
                unsafe_allow_html=True,
            )
            text_col, open_col = st.columns([0.86, 0.14], gap="small", vertical_alignment="center")
            text_col.write(display_text)
            if open_col.button("열기", key=f"{key_prefix}_open_pdf_{index}", use_container_width=True):
                opened, error_message = _open_report_pdf(file_name)
                if not opened:
                    st.warning(error_message)


def _render_message(message: dict, *, index: int) -> None:
    message_anchor_id = f"chat_message_{index}"
    st.markdown(
        f"<div id='{message_anchor_id}' style='scroll-margin-top: 96px;'></div>",
        unsafe_allow_html=True,
    )
    with st.chat_message(message["role"]):
        if message["role"] == "assistant":
            rerank_info = (message.get("metadata") or {}).get("rerank_info") or []
            anchor_prefix = f"message_{index}"
            linked_content = link_citations_to_sources(
                message["content"],
                anchor_prefix=anchor_prefix,
                source_count=len(rerank_info),
            )
            st.markdown(linked_content)
            _render_sources(
                rerank_info,
                key_prefix=f"message_{index}",
                anchor_prefix=anchor_prefix,
                expanded=linked_content != message["content"],
            )
        else:
            st.markdown(message["content"])


def _set_current_thread(thread_id: str) -> None:
    if st.session_state.current_thread_id != thread_id:
        st.session_state.current_thread_id = thread_id
        st.session_state.editing_thread_id = None
        _app_rerun()


def _delete_thread_and_select_next(thread_id: str) -> None:
    was_current = st.session_state.current_thread_id == thread_id
    delete_thread(thread_id)
    st.session_state.editing_thread_id = None
    remaining_threads = list_threads()
    if not remaining_threads:
        st.session_state.current_thread_id = create_thread("새로운 대화")
    elif was_current:
        st.session_state.current_thread_id = remaining_threads[0]["id"]
    _app_rerun()


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
    thread_col, edit_col, delete_col = st.columns([0.72, 0.14, 0.14], gap="small", vertical_alignment="center")
    if thread_col.button(label, key=f"thread_{thread_id}", use_container_width=True):
        _set_current_thread(thread_id)
    if edit_col.button("✎", key=f"edit_thread_{thread_id}", use_container_width=True, help="이름 변경"):
        st.session_state.editing_thread_id = thread_id
        _sidebar_rerun()
    if delete_col.button("×", key=f"delete_thread_{thread_id}", use_container_width=True, help="삭제"):
        _delete_thread_and_select_next(thread_id)


def render_sidebar(current_id: str) -> None:
    threads = _load_threads()

    st.title("Finance Report Agent")

    if st.button("새 대화 시작", use_container_width=True):
        st.session_state.current_thread_id = create_thread(f"대화 {len(threads) + 1}")
        st.session_state.editing_thread_id = None
        _app_rerun()

    st.subheader("대화 목록")
    for thread in threads:
        _render_thread_row(thread, selected=thread["id"] == current_id)

    with st.container(key="sidebar_data_status_bottom"):
        st.divider()
        status = get_data_status()
        db_status = status["db"]
        _render_report_calendar(db_status)
        _render_data_update_controls(db_status)
        st.divider()
        st.subheader("데이터 상태")
        col1, col2 = st.columns(2)
        col1.metric("리포트", f"{db_status['total_reports']}건")
        col2.metric("처리됨", f"{db_status['embedded_reports']}건")


def render_chat(current_id: str, current_thread: dict) -> None:
    st.header(current_thread["name"])

    messages = list_messages(current_id)
    for index, message in enumerate(messages):
        _render_message(message, index=index)

    pending_scroll_anchor = st.session_state.pop("pending_scroll_anchor", None)
    if pending_scroll_anchor:
        _scroll_to_anchor(pending_scroll_anchor)


    if user_query := st.chat_input("질문을 입력해 주세요... (ex: 최근 발행된 현대차 리포트 요약해줘)"):
        if not messages and (current_thread["name"] == "새로운 대화" or current_thread["name"].startswith("대화 ")):
            rename_thread(current_id, user_query[:15] + "...")

        prior_history = get_chat_history(current_id)
        append_message(current_id, "user", user_query)

        with st.chat_message("user"):
            st.markdown(user_query)

        live_anchor_id = f"live_answer_{current_id}_{len(messages)}"
        st.markdown(
            f"<div id='{live_anchor_id}' style='scroll-margin-top: 96px;'></div>",
            unsafe_allow_html=True,
        )
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

            anchor_prefix = f"live_{current_id}_{len(messages)}"
            linked_answer = link_citations_to_sources(
                answer,
                anchor_prefix=anchor_prefix,
                source_count=len(rerank_info),
            )
            st.markdown(linked_answer)
            _render_sources(
                rerank_info,
                key_prefix=f"live_{current_id}_{len(messages)}",
                anchor_prefix=anchor_prefix,
                expanded=linked_answer != answer,
            )
            append_message(current_id, "assistant", answer, {"rerank_info": rerank_info})
        st.session_state.pending_scroll_anchor = f"chat_message_{len(messages) + 1}"
        _scroll_to_anchor(live_anchor_id)
        _app_rerun()


threads = _load_threads()
_ensure_current_thread(threads)
current_id = st.session_state.current_thread_id
current_thread = next(thread for thread in threads if thread["id"] == current_id)

with st.sidebar:
    render_sidebar(current_id)

render_chat(current_id, current_thread)
