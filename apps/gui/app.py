import os
import sys
import subprocess
import calendar
import importlib
import threading
import uuid
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.configs.config import SAVE_DIR
from src.core import data_update_jobs
from src.core import conversation_store

data_update_jobs = importlib.reload(data_update_jobs)
conversation_store = importlib.reload(conversation_store)
append_message = conversation_store.append_message
create_thread = conversation_store.create_thread
delete_thread = conversation_store.delete_thread
get_chat_history = conversation_store.get_chat_history
list_messages = conversation_store.list_messages
list_threads = conversation_store.list_threads
rename_thread = conversation_store.rename_thread
update_message = conversation_store.update_message
from src.core.status import get_data_status
from src.utils import citations

citations = importlib.reload(citations)
link_citations_to_sources = citations.link_citations_to_sources
extract_citation_ranks = citations.extract_citation_ranks
normalize_citation_ranks = citations.normalize_citation_ranks
remove_unavailable_citations = citations.remove_unavailable_citations
source_anchor_id = citations.source_anchor_id
from src.graphs.main_graph import graph_app

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


@st.cache_resource
def _chat_job_registry() -> dict:
    return {
        "running_job_ids": set(),
        "events": [],
        "lock": threading.Lock(),
        "graph_lock": threading.Lock(),
    }


def _record_chat_job_event(event: dict, registry: dict | None = None) -> None:
    registry = registry or _chat_job_registry()
    with registry["lock"]:
        registry["events"].append(event)


def _consume_chat_job_events() -> list[dict]:
    registry = _chat_job_registry()
    with registry["lock"]:
        events = list(registry["events"])
        registry["events"].clear()
    return events


def _queue_chat_job_toast(event: dict) -> None:
    st.session_state.setdefault("chat_job_toasts", []).append(event)


def _show_queued_chat_job_toasts() -> None:
    queued_events = st.session_state.pop("chat_job_toasts", [])
    for event in queued_events:
        icon = "✅" if event.get("status") == "succeeded" else "⚠️"
        st.toast(event.get("message", "답변 작업 상태가 변경되었습니다."), icon=icon)


def _thread_has_running_job(messages: list[dict]) -> bool:
    return any(
        message.get("role") == "assistant"
        and (message.get("metadata") or {}).get("status") == "running"
        for message in messages
    )


def _chat_message_anchor_id(message_id: int | str | None, fallback_index: int) -> str:
    if message_id is not None:
        return f"chat_message_id_{message_id}"
    return f"chat_message_{fallback_index}"


def _run_chat_response_job(
    *,
    job_id: str,
    thread_id: str,
    thread_name: str,
    assistant_message_id: int,
    user_query: str,
    prior_history: list[tuple[str, str]],
    registry: dict,
) -> None:
    try:
        config = {"configurable": {"thread_id": thread_id}}
        with registry["graph_lock"]:
            final_state = graph_app.invoke(
                {"question": user_query, "chat_history": prior_history},
                config=config,
            )
        answer = final_state.get("generation", "응답을 생성하지 못했습니다.")
        if "Error" in answer or "차단" in answer:
            answer = f"주의: {answer}"
        rerank_info = final_state.get("rerank_info", []) if final_state.get("route") == "vectordb" else []
        update_message(
            assistant_message_id,
            answer,
            {
                "status": "succeeded",
                "job_id": job_id,
                "rerank_info": rerank_info,
            },
        )
        _record_chat_job_event(
            {
                "status": "succeeded",
                "thread_id": thread_id,
                "thread_name": thread_name,
                "assistant_message_id": assistant_message_id,
                "message": f"'{thread_name}' 답변이 완료되었습니다.",
            },
            registry,
        )
    except Exception as exc:
        update_message(
            assistant_message_id,
            f"오류가 발생했습니다: {exc}",
            {
                "status": "failed",
                "job_id": job_id,
                "error": str(exc),
            },
        )
        _record_chat_job_event(
            {
                "status": "failed",
                "thread_id": thread_id,
                "thread_name": thread_name,
                "assistant_message_id": assistant_message_id,
                "message": f"'{thread_name}' 답변 생성에 실패했습니다.",
            },
            registry,
        )
    finally:
        with registry["lock"]:
            registry["running_job_ids"].discard(job_id)


def _start_chat_response_job(
    *,
    thread_id: str,
    thread_name: str,
    user_query: str,
    prior_history: list[tuple[str, str]],
) -> int:
    job_id = str(uuid.uuid4())
    assistant_message_id = append_message(
        thread_id,
        "assistant",
        "AI가 리포트 내용을 검색하고 분석 중입니다...",
        {"status": "running", "job_id": job_id},
    )
    registry = _chat_job_registry()
    with registry["lock"]:
        registry["running_job_ids"].add(job_id)
    threading.Thread(
        target=_run_chat_response_job,
        kwargs={
            "job_id": job_id,
            "thread_id": thread_id,
            "thread_name": thread_name,
            "assistant_message_id": assistant_message_id,
            "user_query": user_query,
            "prior_history": prior_history,
            "registry": registry,
        },
        name=f"chat-response-{job_id[:8]}",
        daemon=True,
    ).start()
    return assistant_message_id


@st.fragment(run_every="2s")
def _render_chat_job_notifications(current_thread_id: str) -> None:
    should_refresh_current_thread = False
    for event in _consume_chat_job_events():
        _queue_chat_job_toast(event)
        if event.get("thread_id") == current_thread_id:
            st.session_state.pending_scroll_anchor = _chat_message_anchor_id(
                event.get("assistant_message_id"),
                0,
            )
            should_refresh_current_thread = True
    if should_refresh_current_thread:
        st.rerun(scope="app")
    _show_queued_chat_job_toasts()


def _scroll_to_anchor(anchor_id: str, *, offset_px: int = 104) -> None:
    """Scroll the Streamlit app to an anchor after the current render pass."""
    components.html(
        f"""
        <script>
        const anchorId = {anchor_id!r};
        const offsetPx = {offset_px};
        const scrollToAnchor = () => {{
          const root = window.parent.document;
          const target = root.getElementById(anchorId);
          if (target) {{
            const scrollTop = Math.max(
              0,
              target.getBoundingClientRect().top + window.parent.scrollY - offsetPx
            );
            window.parent.scrollTo({{top: scrollTop, behavior: "auto"}});

            const scrollContainer = root.querySelector('[data-testid="stAppViewContainer"]');
            if (scrollContainer) {{
              const containerTop = Math.max(
                0,
                target.getBoundingClientRect().top
                  - scrollContainer.getBoundingClientRect().top
                  + scrollContainer.scrollTop
                  - offsetPx
              );
              scrollContainer.scrollTo({{top: containerTop, behavior: "auto"}});
            }}
          }}
        }};
        [0, 80, 180, 400, 800, 1400, 2200].forEach((delay) => {{
          setTimeout(scrollToAnchor, delay);
        }});
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


def _calendar_day_table_cell_html(day: date, count: int, *, in_month: bool) -> str:
    if not in_month:
        return (
            "<td style='width:20%; padding:5px 2px; border-bottom:1px solid #f1f5f9;'>"
            "&nbsp;</td>"
        )

    has_data = count > 0
    is_today = day == date.today()
    if count >= 51:
        background = "#ecfdf5"
        day_color = "#064e3b"
        count_color = "#047857"
    elif count >= 21:
        background = "#f0fdf4"
        day_color = "#047857"
        count_color = "#059669"
    elif count > 0:
        background = "#ffffff"
        day_color = "#047857"
        count_color = "#10b981"
    else:
        background = "#ffffff"
        day_color = "#94a3b8"
        count_color = "#cbd5e1"
    count_label = str(count) if has_data else "–"
    title_prefix = f"{day.isoformat()} · 오늘" if is_today else day.isoformat()
    title = f"{title_prefix}: {count}건" if has_data else f"{title_prefix}: 데이터 없음"
    today_badge_style = (
        "border:1.5px solid #3b82f6; color:#1d4ed8; background:#eff6ff;"
        if is_today
        else "border:1.5px solid transparent;"
    )
    return (
        "<td "
        f"title='{escape(title)}' "
        "style='width:20%; text-align:center; padding:5px 2px 6px; "
        f"background:{background}; border-bottom:1px solid #f1f5f9; "
        "font-size:0.80rem; line-height:1.05rem;'>"
        f"<div style='display:inline-flex; align-items:center; justify-content:center; "
        f"min-width:22px; height:22px; border-radius:999px; box-sizing:border-box; "
        f"font-weight:800; color:{day_color}; {today_badge_style}'>{day.day}</div>"
        f"<div style='margin-top:2px; font-size:0.75rem; font-weight:800; color:{count_color};'>"
        f"{count_label}"
        "</div>"
        "</td>"
    )


def _report_calendar_table_html(
    month_weeks: list[list[date]],
    month: int,
    date_counts: dict[str, int],
) -> str:
    header_cells = "".join(
        "<th style='padding:4px 2px 5px; text-align:center; font-size:0.72rem; "
        "font-weight:800; color:#64748b; border-bottom:1px solid #f1f5f9;'>"
        f"{label}</th>"
        for label in WEEKDAY_LABELS
    )
    rows: list[str] = []
    for week in month_weeks:
        cells = []
        for day in [day for day in week if day.weekday() < 5]:
            count = int(date_counts.get(day.isoformat(), 0) or 0)
            cells.append(_calendar_day_table_cell_html(day, count, in_month=day.month == month))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<table style='width:100%; border-collapse:collapse; table-layout:fixed; "
        "margin-top:0.2rem;'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


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

    month_weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(year, month)
    st.markdown(
        _report_calendar_table_html(month_weeks, month, date_counts),
        unsafe_allow_html=True,
    )
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
    return data_update_jobs.is_update_job_active(status)


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
        if not data_update_jobs.is_update_job_active(status):
            st.warning("이전 데이터 업데이트 작업이 중단되었습니다. 새 업데이트를 시작할 수 있습니다.")
            return
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


def _source_identity(info: dict) -> str:
    file_name = str(info.get("file_name") or "").strip()
    if file_name and file_name != "-":
        return file_name
    return "|".join(
        str(info.get(key) or "").strip()
        for key in ("target_name", "report_date", "broker", "title")
    )


def _group_sources_by_document(rerank_info: list[dict]) -> list[dict]:
    grouped: list[dict] = []
    index_by_identity: dict[str, int] = {}
    for index, info in enumerate(rerank_info):
        rank = _source_rank(info, index + 1)
        identity = _source_identity(info)
        if identity in index_by_identity:
            grouped[index_by_identity[identity]]["ranks"].append(rank)
            continue
        index_by_identity[identity] = len(grouped)
        grouped.append({"info": info, "ranks": [rank]})
    return grouped


def _source_rank_aliases(rerank_info: list[dict]) -> dict[int, int]:
    aliases: dict[int, int] = {}
    for source_group in _group_sources_by_document(rerank_info):
        ranks = sorted(source_group["ranks"])
        if not ranks:
            continue
        representative_rank = ranks[0]
        for rank in ranks:
            aliases[rank] = representative_rank
    return aliases


def _render_sources(
    rerank_info: list[dict] | None,
    *,
    key_prefix: str,
    anchor_prefix: str,
    used_ranks: set[int] | None = None,
    expanded: bool = False,
) -> None:
    if not rerank_info:
        return
    grouped_sources = _group_sources_by_document(rerank_info)
    if used_ranks is not None:
        grouped_sources = [
            source_group
            for source_group in grouped_sources
            if any(rank in used_ranks for rank in source_group["ranks"])
        ]
        if not grouped_sources:
            return
    with st.expander(f"참고한 문서 ({len(grouped_sources)}개)", expanded=expanded):
        for index, source_group in enumerate(grouped_sources):
            info = source_group["info"]
            ranks = source_group["ranks"]
            rank_label = f"[{min(ranks)}]"
            file_name = info.get("file_name", "-")
            title = info.get("title") or "-"
            display_text = (
                f"{rank_label} {info.get('target_name', '-')} ({info.get('report_date', '-')}) "
                f"- {info.get('broker', '-')} - {title} - {file_name}"
            )
            st.markdown(
                "".join(
                    f"<div id='{source_anchor_id(anchor_prefix, rank)}' "
                    "style='scroll-margin-top: 96px; height: 0; visibility: hidden;'></div>"
                    for rank in source_group["ranks"]
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
    message_anchor_id = _chat_message_anchor_id(message.get("id"), index)
    st.markdown(
        (
            f"<div id='{message_anchor_id}' "
            "style='scroll-margin-top: 104px; height: 1px;'></div>"
        ),
        unsafe_allow_html=True,
    )
    with st.chat_message(message["role"]):
        if message["role"] == "assistant":
            metadata = message.get("metadata") or {}
            status = metadata.get("status")
            if status == "running":
                st.info(message["content"])
                return
            if status == "failed":
                st.error(message["content"])
                return
            rerank_info = metadata.get("rerank_info") or []
            display_content = remove_unavailable_citations(
                message["content"],
                source_count=len(rerank_info),
            )
            display_content = normalize_citation_ranks(
                display_content,
                _source_rank_aliases(rerank_info),
            )
            used_ranks = extract_citation_ranks(
                display_content,
                source_count=len(rerank_info),
            )
            anchor_prefix = f"message_{index}"
            linked_content = link_citations_to_sources(
                display_content,
                anchor_prefix=anchor_prefix,
                source_count=len(rerank_info),
            )
            st.markdown(linked_content)
            _render_sources(
                rerank_info,
                key_prefix=f"message_{index}",
                anchor_prefix=anchor_prefix,
                used_ranks=used_ranks,
                expanded=linked_content != display_content,
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


def _thread_status_badge(thread_id: str) -> str:
    messages = list_messages(thread_id)
    if _thread_has_running_job(messages):
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

    badge = _thread_status_badge(thread_id)
    status_prefix = f"{badge} " if badge else ""
    label = f"> {status_prefix}{thread['name']}" if selected else f"- {status_prefix}{thread['name']}"
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

    has_running_job = _thread_has_running_job(messages)
    if has_running_job:
        st.caption("이 대화의 답변을 백그라운드에서 생성 중입니다. 다른 대화로 이동해도 작업은 계속됩니다.")

    if user_query := st.chat_input(
        "질문을 입력해 주세요... (ex: 최근 발행된 현대차 리포트 요약해줘)",
        disabled=has_running_job,
    ):
        if not messages and (current_thread["name"] == "새로운 대화" or current_thread["name"].startswith("대화 ")):
            thread_name = user_query[:15] + "..."
            rename_thread(current_id, thread_name)
        else:
            thread_name = current_thread["name"]

        prior_history = get_chat_history(current_id)
        append_message(current_id, "user", user_query)
        assistant_message_id = _start_chat_response_job(
            thread_id=current_id,
            thread_name=thread_name,
            user_query=user_query,
            prior_history=prior_history,
        )

        with st.chat_message("user"):
            st.markdown(user_query)

        live_anchor_id = _chat_message_anchor_id(assistant_message_id, len(messages) + 1)
        st.markdown(
            (
                f"<div id='{live_anchor_id}' "
                "style='scroll-margin-top: 104px; height: 1px;'></div>"
            ),
            unsafe_allow_html=True,
        )
        with st.chat_message("assistant"):
            st.info("AI가 리포트 내용을 검색하고 분석 중입니다...")
        st.session_state.pending_scroll_anchor = live_anchor_id
        _scroll_to_anchor(live_anchor_id)
        _app_rerun()


threads = _load_threads()
_ensure_current_thread(threads)
current_id = st.session_state.current_thread_id
current_thread = next(thread for thread in threads if thread["id"] == current_id)

_show_queued_chat_job_toasts()
_render_chat_job_notifications(current_id)

with st.sidebar:
    render_sidebar(current_id)

render_chat(current_id, current_thread)
