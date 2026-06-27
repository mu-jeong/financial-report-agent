import os
import sys
import subprocess
import calendar
import importlib
import json
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.configs import config as config_module

config_module = importlib.reload(config_module)
CRAWLER_CATEGORIES = config_module.CRAWLER_CATEGORIES
MONITORING_MODE = config_module.MONITORING_MODE
REPORT_PDF_DIR = config_module.REPORT_PDF_DIR

from src.core import data_update_jobs
from src.core import conversation_store
from src.core import issue_report_store
from src.core import monitoring
from src.core import pdf_extraction
from src.core import compare_pdf_extractors
from src.core import metadata_filters as metadata_filters_module
from src.core import status as status_module
from src.graphs import main_graph as main_graph_module
from src.nodes import query_rewrite as query_rewrite_module
from src.nodes import search_scope as search_scope_module
from src.nodes import vectordb as vectordb_module
from src.core.chat_ui_helpers import (
    build_clipboard_copy_html,
    build_no_result_suggestions,
    build_scope_notice,
)
from src.core.followup_scope import build_answer_scope_index

data_update_jobs = importlib.reload(data_update_jobs)
conversation_store = importlib.reload(conversation_store)
issue_report_store = importlib.reload(issue_report_store)
monitoring = importlib.reload(monitoring)
pdf_extraction = importlib.reload(pdf_extraction)
compare_pdf_extractors = importlib.reload(compare_pdf_extractors)
metadata_filters_module = importlib.reload(metadata_filters_module)
query_rewrite_module = importlib.reload(query_rewrite_module)
search_scope_module = importlib.reload(search_scope_module)
vectordb_module = importlib.reload(vectordb_module)
main_graph_module = importlib.reload(main_graph_module)
status_module = importlib.reload(status_module)
append_message = conversation_store.append_message
create_thread = conversation_store.create_thread
create_issue_report = issue_report_store.create_issue_report
build_issue_report_context = issue_report_store.build_issue_report_context
build_evaluation_failure_actions = monitoring.build_evaluation_failure_actions
build_chat_trace_debug_hints = monitoring.build_chat_trace_debug_hints
build_chat_trace_issue_context = monitoring.build_chat_trace_issue_context
build_issue_report_rows = monitoring.build_issue_report_rows
build_regression_candidate_dataset = monitoring.build_regression_candidate_dataset
build_regression_candidate_rows = monitoring.build_regression_candidate_rows
list_regression_candidates = monitoring.list_regression_candidates
build_message_monitoring_rows = monitoring.build_message_monitoring_rows
build_message_trace_detail = monitoring.build_message_trace_detail
build_message_trace_summary = monitoring.build_message_trace_summary
build_monitoring_page_labels = monitoring.build_monitoring_page_labels
build_monitoring_tab_labels = monitoring.build_monitoring_tab_labels
build_response_diff = monitoring.build_response_diff
compare_evaluation_runs = monitoring.compare_evaluation_runs
filter_evaluation_runs_by_mode = monitoring.filter_evaluation_runs_by_mode
promote_issue_report_to_eval_candidate = monitoring.promote_issue_report_to_eval_candidate
compact_graph_monitoring_metadata = monitoring.compact_graph_monitoring_metadata
previous_successful_assistant = monitoring.previous_successful_assistant
run_pdf_extraction_comparison = compare_pdf_extractors.run_pdf_extraction_comparison
SUPPORTED_EXTRACTION_ENGINES = compare_pdf_extractors.SUPPORTED_EXTRACTION_ENGINES
delete_thread = conversation_store.delete_thread
get_chat_history = conversation_store.get_chat_history
load_evaluation_dataset = monitoring.load_evaluation_dataset
load_evaluation_snapshot_manifest = monitoring.load_evaluation_snapshot_manifest
list_issue_reports = issue_report_store.list_issue_reports
list_messages = conversation_store.list_messages
list_threads = conversation_store.list_threads
mark_interrupted_running_messages_failed = conversation_store.mark_interrupted_running_messages_failed
rename_thread = conversation_store.rename_thread
set_thread_pinned = conversation_store.set_thread_pinned
run_evaluation_dataset = monitoring.run_evaluation_dataset
select_evaluation_cases = monitoring.select_evaluation_cases
summarize_all_chat_threads = monitoring.summarize_all_chat_threads
summarize_chat_messages = monitoring.summarize_chat_messages
summarize_data_integrity = monitoring.summarize_data_integrity
summarize_issue_reports = monitoring.summarize_issue_reports
summarize_evaluation_dataset = monitoring.summarize_evaluation_dataset
update_message = conversation_store.update_message
user_question_before_message = monitoring.user_question_before_message
validate_evaluation_snapshot = monitoring.validate_evaluation_snapshot
build_unembedded_report_rows = status_module.build_unembedded_report_rows
get_data_status = status_module.get_data_status
list_unembedded_reports = status_module.list_unembedded_reports
from src.utils import citations

citations = importlib.reload(citations)
link_citations_to_sources = citations.link_citations_to_sources
extract_citation_ranks = citations.extract_citation_ranks
normalize_citation_ranks = citations.normalize_citation_ranks
remove_unavailable_citations = citations.remove_unavailable_citations
document_rank_aliases = citations.document_rank_aliases
group_sources_by_document = citations.group_sources_by_document
source_anchor_id = citations.source_anchor_id
graph_app = main_graph_module.graph_app

WEEKDAY_LABELS = ["월", "화", "수", "목", "금"]
MONITORING_EVAL_RUN_DIR = Path("debug") / "evaluation_runs"
MONITORING_REGRESSION_CANDIDATE_DIR = Path("debug") / "regression_candidates"

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
        .st-key-issue_report_control {
            margin-top: 0.2rem;
            padding-bottom: 0.25rem;
        }
        .st-key-issue_report_control .stButton {
            display: flex;
            justify-content: flex-end;
        }
        .st-key-issue_report_control .stButton > button {
            min-height: 1.55rem;
            padding: 0.08rem 0.38rem;
            border-radius: 0.55rem;
            font-size: 0.72rem;
            line-height: 0.85rem;
        }
        .st-key-issue_report_control .stButton > button p {
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


def _repair_interrupted_chat_jobs() -> int:
    """Unlock chats whose background answer thread was lost on app restart."""
    registry = _chat_job_registry()
    with registry["lock"]:
        active_job_ids = set(registry["running_job_ids"])
    return mark_interrupted_running_messages_failed(active_job_ids=active_job_ids)


def _search_scope_from_graph_state(final_state: dict) -> dict | None:
    """Build a reusable retrieval scope from the completed graph state."""
    if final_state.get("no_vector_results"):
        return None
    search_filters = dict(final_state.get("search_filters") or {})
    temporal_context = final_state.get("temporal_context")
    rerank_info = final_state.get("rerank_info") or final_state.get("rdb_sources") or []
    file_names = []
    seen_file_names = set()
    for info in rerank_info:
        file_name = (info or {}).get("file_name")
        if file_name and file_name != "-" and file_name not in seen_file_names:
            seen_file_names.add(file_name)
            file_names.append(file_name)

    if not search_filters and not temporal_context and not file_names:
        return None

    scope = {
        "route": final_state.get("route"),
        "search_filters": search_filters,
        "temporal_context": temporal_context,
        "scope_source": final_state.get("scope_source"),
    }
    if file_names:
        scope["file_names"] = file_names
    scope["answer_scope_index"] = build_answer_scope_index(scope, rerank_info)
    return scope


def _latest_search_scope(messages: list[dict]) -> dict | None:
    """Return the latest successful assistant search scope in the current thread."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        metadata = message.get("metadata") or {}
        if metadata.get("status") in {"running", "failed"} or metadata.get("no_vector_results"):
            continue
        scope = metadata.get("search_scope")
        if isinstance(scope, dict):
            return scope
    return None


def _thread_has_running_job(messages: list[dict]) -> bool:
    return any(
        message.get("role") == "assistant"
        and (message.get("metadata") or {}).get("status") == "running"
        for message in messages
    )


def _render_issue_report_control(
    *,
    current_thread: dict,
    messages: list[dict],
) -> None:
    if report_result := st.session_state.pop("issue_report_success", None):
        st.success(
            "문제 신고 텍스트 파일을 저장했습니다. 아래 경로의 파일 내용을 복사하여 "
            "내용을 이메일에 작성해주세요."
        )
        st.code(report_result["file_path"], language="text")
        try:
            report_text = Path(report_result["file_path"]).read_text(encoding="utf-8")
        except OSError:
            report_text = ""
        if report_text:
            components.html(
                build_clipboard_copy_html(report_text, button_label="신고 내용 복사"),
                height=48,
            )
        st.toast(f"이슈 리포트가 저장되었습니다. (#{report_result['id']})", icon="✅")

    with st.container(key="issue_report_control"):
        _, report_col = st.columns([0.88, 0.12], gap="small", vertical_alignment="center")
        if report_col.button(
            "⚠ 신고",
            key=f"toggle_issue_report_{current_thread['id']}",
            help="현재 대화에서 발생한 문제를 개발자가 확인할 수 있도록 저장합니다.",
            use_container_width=True,
        ):
            st.session_state.show_issue_report_form = not st.session_state.get("show_issue_report_form", False)

        if not st.session_state.get("show_issue_report_form"):
            return

        with st.form(f"issue_report_form_{current_thread['id']}", clear_on_submit=True):
            category = st.selectbox(
                "문제 유형",
                [
                    "답변 품질 문제",
                    "검색/출처 문제",
                    "응답 지연/멈춤",
                    "화면/사용성 문제",
                    "기타",
                ],
                key=f"issue_report_category_{current_thread['id']}",
            )
            description = st.text_area(
                "무슨 문제가 있었나요?",
                placeholder="예: 질문 의도와 다른 리포트를 참고했어요 / 답변이 너무 오래 걸렸어요 / 출처 링크가 열리지 않아요",
                height=110,
                key=f"issue_report_description_{current_thread['id']}",
            )
            include_conversation = st.checkbox(
                "전체 대화 내용을 함께 첨부",
                value=True,
                help="민감한 내용이 있으면 체크를 해제하고 설명만 제출하세요.",
                key=f"issue_report_include_context_{current_thread['id']}",
            )
            submitted = st.form_submit_button(
                "신고 제출",
                use_container_width=True,
            )

        if submitted:
            clean_description = description.strip()
            if not clean_description:
                st.warning("문제 내용을 한 줄 이상 입력해 주세요.")
                return
            report_result = create_issue_report(
                current_thread["id"],
                category,
                clean_description,
                build_issue_report_context(
                    thread=current_thread,
                    messages=messages,
                    include_conversation=include_conversation,
                ),
            )
            st.session_state.show_issue_report_form = False
            st.session_state.issue_report_success = report_result
            _app_rerun()


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
    prior_search_scope: dict | None,
    registry: dict,
) -> None:
    started_at = time.perf_counter()
    try:
        config = {"configurable": {"thread_id": thread_id}}
        graph_input = {"question": user_query, "chat_history": prior_history}
        if prior_search_scope:
            graph_input["prior_search_scope"] = prior_search_scope
        with registry["graph_lock"]:
            final_state = graph_app.invoke(
                graph_input,
                config=config,
            )
        answer = final_state.get("generation", "응답을 생성하지 못했습니다.")
        if "Error" in answer or "차단" in answer:
            answer = f"주의: {answer}"
        rerank_info = (
            final_state.get("rerank_info") or []
            if final_state.get("route") == "vectordb"
            else final_state.get("rdb_sources") or []
        )
        search_scope = _search_scope_from_graph_state(final_state)
        metadata = {
            "status": "succeeded",
            "job_id": job_id,
            "question": user_query,
            "no_vector_results": bool(final_state.get("no_vector_results")),
            "rerank_info": rerank_info,
        }
        metadata.update(
            compact_graph_monitoring_metadata(
                final_state=final_state,
                latency_seconds=time.perf_counter() - started_at,
                rerank_info=rerank_info,
            )
        )
        if search_scope:
            metadata["search_scope"] = search_scope
        if scope_notice := build_scope_notice(final_state):
            metadata["scope_notice"] = scope_notice
        update_message(
            assistant_message_id,
            answer,
            metadata,
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
                "latency_seconds": round(time.perf_counter() - started_at, 3),
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
    prior_search_scope: dict | None = None,
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
            "prior_search_scope": prior_search_scope,
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
    _repair_interrupted_chat_jobs()
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
        return (
            False,
            "PDF 파일을 찾을 수 없습니다. 데이터 업데이트를 다시 실행하거나 REPORT_PDF_DIR 설정을 확인해 주세요.",
        )
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
    return data_update_jobs.iter_weekdays(start_date, end_date)


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
    date_type_counts = {
        str(day).strip()[:10]: {
            str(report_type): int(count)
            for report_type, count in (counts or {}).items()
        }
        for day, counts in (db_status.get("report_date_type_counts") or {}).items()
    }

    st.subheader("데이터 업데이트")
    if st.session_state.pop("show_data_update_hint", False):
        st.info("아래에서 기간과 카테고리를 선택한 뒤 데이터 업데이트를 실행하세요.")
    category_options = data_update_jobs.normalize_update_categories("all")
    default_categories = [
        category
        for category in data_update_jobs.normalize_update_categories(CRAWLER_CATEGORIES)
        if category in category_options
    ] or ["company"]
    selected_categories = st.multiselect(
        "업데이트 카테고리",
        category_options,
        default=default_categories,
        format_func=lambda value: {
            "company": "기업(company)",
            "industry": "산업(industry)",
            "economy": "경제(economy)",
        }.get(value, value),
        key="update_categories",
        help="선택한 카테고리 중 하나라도 비어 있는 날짜를 업데이트 대상으로 포함합니다.",
    )
    default_start, default_end = _default_update_range(db_status)
    min_update_date = _update_min_date(db_status)
    selected_range = st.date_input(
        "업데이트 기간",
        value=(default_start, default_end),
        min_value=min_update_date,
        max_value=today,
        key="update_date_range",
        help="선택 기간 중 선택 카테고리가 모두 임베딩 완료된 평일은 다운로드부터 제외됩니다.",
    )

    if isinstance(selected_range, tuple) and len(selected_range) == 2:
        range_start, range_end = selected_range
        if not selected_categories:
            target_dates = []
            st.caption("업데이트할 카테고리를 하나 이상 선택해 주세요.")
        else:
            target_dates = data_update_jobs.missing_update_dates_by_category(
                range_start,
                range_end,
                date_type_counts,
                selected_categories,
                today=today,
            )
            skipped_count = len(_iter_weekdays(range_start, min(range_end, today))) - len(target_dates)
            category_label = ", ".join(selected_categories)
            st.caption(
                f"작업 대상: {category_label} 미완료 평일 {len(target_dates)}일"
                + (f" · 선택 카테고리 임베딩 완료 {skipped_count}일 제외" if skipped_count > 0 else "")
            )
        if selected_categories and target_dates:
            if st.button(
                "선택 기간 업데이트",
                key="update_selected_range",
                disabled=job_active,
                use_container_width=True,
            ):
                data_update_jobs.start_update_job(
                    selected_dates=target_dates,
                    categories=selected_categories,
                    label=f"선택 기간 {len(target_dates)}일 ({', '.join(selected_categories)})",
                )
                _sidebar_rerun()
        elif selected_categories:
            st.caption("선택 기간 내 업데이트할 임베딩 미완료 평일이 없습니다.")
    else:
        st.caption("업데이트할 시작일과 종료일을 선택해 주세요.")

    if latest_range:
        start_date, end_date = latest_range
        st.caption(f"최신 누락 범위\n\n`{start_date}` ~ `{end_date}`")

    _render_update_progress()


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
    grouped_sources = group_sources_by_document(rerank_info)
    indexed_sources = [
        {**source_group, "display_rank": display_rank}
        for display_rank, source_group in enumerate(grouped_sources, 1)
    ]
    if used_ranks is not None:
        indexed_sources = [
            source_group
            for source_group in indexed_sources
            if source_group["display_rank"] in used_ranks
        ]
        if not indexed_sources:
            return
    with st.expander(f"참고한 문서 ({len(indexed_sources)}개)", expanded=expanded):
        for index, source_group in enumerate(indexed_sources):
            info = source_group["info"]
            display_rank = source_group["display_rank"]
            rank_label = f"[{display_rank}]"
            file_name = info.get("file_name", "-")
            title = info.get("title") or "-"
            display_text = (
                f"{rank_label} {info.get('target_name', '-')} ({info.get('report_date', '-')}) "
                f"- {info.get('broker', '-')} - {title} - {file_name}"
            )
            st.markdown(
                (
                    f"<div id='{source_anchor_id(anchor_prefix, display_rank)}' "
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


def _render_no_result_actions(message: dict, *, index: int) -> None:
    metadata = message.get("metadata") or {}
    if not metadata.get("no_vector_results"):
        return
    suggestions = build_no_result_suggestions(
        metadata.get("question") or message.get("content", ""),
        metadata.get("search_filters") or {},
    )
    if not suggestions:
        return
    columns = st.columns(len(suggestions), gap="small")
    for suggestion, column in zip(suggestions, columns):
        if column.button(
            suggestion["label"],
            key=f"no_result_suggestion_{message.get('id', index)}_{suggestion['label']}",
            use_container_width=True,
        ):
            st.session_state.pending_suggested_query = suggestion["query"]
            _app_rerun()


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
            if scope_notice := metadata.get("scope_notice"):
                st.caption(scope_notice)
            rerank_info = metadata.get("rerank_info") or []
            source_count = len(group_sources_by_document(rerank_info))
            display_content = remove_unavailable_citations(
                message["content"],
                source_count=len(rerank_info),
            )
            display_content = normalize_citation_ranks(
                display_content,
                document_rank_aliases(rerank_info),
            )
            display_content = remove_unavailable_citations(
                display_content,
                source_count=source_count,
            )
            used_ranks = extract_citation_ranks(
                display_content,
                source_count=source_count,
            )
            source_filter_ranks = used_ranks or None
            anchor_prefix = f"message_{index}"
            linked_content = link_citations_to_sources(
                display_content,
                anchor_prefix=anchor_prefix,
                source_count=source_count,
            )
            st.markdown(linked_content)
            _render_sources(
                rerank_info,
                key_prefix=f"message_{index}",
                anchor_prefix=anchor_prefix,
                used_ranks=source_filter_ranks,
                expanded=False,
            )
            _render_no_result_actions(message, index=index)
        else:
            st.markdown(message["content"])


def _set_current_thread(thread_id: str) -> None:
    if st.session_state.current_thread_id != thread_id:
        st.session_state.current_thread_id = thread_id
        st.session_state.editing_thread_id = None
        st.session_state.show_issue_report_form = False
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
    pin_prefix = "★ " if thread.get("pinned") else ""
    label = f"> {status_prefix}{pin_prefix}{thread['name']}" if selected else f"- {status_prefix}{pin_prefix}{thread['name']}"
    pin_col, thread_col, edit_col, delete_col = st.columns([0.12, 0.60, 0.14, 0.14], gap="small", vertical_alignment="center")
    pin_label = "★" if thread.get("pinned") else "☆"
    pin_help = "고정 해제" if thread.get("pinned") else "대화 고정"
    if pin_col.button(pin_label, key=f"pin_thread_{thread_id}", use_container_width=True, help=pin_help):
        set_thread_pinned(thread_id, not bool(thread.get("pinned")))
        _sidebar_rerun()
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
    if MONITORING_MODE:
        st.caption("Monitoring Mode ON")
        st.radio(
            "화면",
            build_monitoring_page_labels(),
            key="active_monitoring_page",
            label_visibility="collapsed",
        )

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

    with st.container(key="chat_entry_area"):
        user_query = st.chat_input(
            "질문을 입력해 주세요... (ex: 최근 발행된 현대차 리포트 요약해줘)",
            disabled=has_running_job,
        )

        _render_issue_report_control(
            current_thread=current_thread,
            messages=messages,
        )

    suggested_query = st.session_state.pop("pending_suggested_query", None)
    if suggested_query and not has_running_job:
        user_query = suggested_query

    if user_query:
        if not messages and (current_thread["name"] == "새로운 대화" or current_thread["name"].startswith("대화 ")):
            thread_name = user_query[:15] + "..."
            rename_thread(current_id, thread_name)
        else:
            thread_name = current_thread["name"]

        prior_history = get_chat_history(current_id)
        prior_search_scope = _latest_search_scope(messages)
        append_message(current_id, "user", user_query)
        assistant_message_id = _start_chat_response_job(
            thread_id=current_id,
            thread_name=thread_name,
            user_query=user_query,
            prior_history=prior_history,
            prior_search_scope=prior_search_scope,
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


def _dimension_rows(summary: dict) -> list[dict]:
    return [
        {"dimension": key, "case_count": value}
        for key, value in sorted(
            (summary.get("monitoring_dimensions") or {}).items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _case_type_rows(summary: dict) -> list[dict]:
    return [
        {"case_type": key, "case_count": value}
        for key, value in sorted((summary.get("case_types") or {}).items())
    ]


def _parse_monitoring_paths(raw_paths: str) -> list[str]:
    """Parse comma/newline-separated paths from the Monitoring UI."""
    paths: list[str] = []
    for part in raw_paths.replace(",", "\n").splitlines():
        cleaned = part.strip().strip('"')
        if cleaned:
            paths.append(cleaned)
    return paths


def _engine_summary_rows(summary: dict) -> list[dict]:
    return [
        {
            "engine": engine,
            "files": values.get("files"),
            "success": values.get("success"),
            "errors": values.get("errors"),
            "avg_elapsed_sec": values.get("avg_elapsed_sec"),
            "avg_char_count": values.get("avg_char_count"),
            "avg_block_count": values.get("avg_block_count"),
            "avg_numeric_line_ratio": values.get("avg_numeric_line_ratio"),
            "avg_korean_line_ratio": values.get("avg_korean_line_ratio"),
            "fallbacks": values.get("fallbacks"),
        }
        for engine, values in sorted((summary or {}).items())
    ]


def _render_parsing_engine_evaluation() -> None:
    st.subheader("Parsing engine evaluation")
    st.caption(
        "Run the same PDF sample through multiple parsing engines and compare extraction quality metrics. "
        "Marker is opt-in because it can be heavy on CPU-only machines."
    )

    default_path = str(Path(REPORT_PDF_DIR).expanduser())
    with st.form("parsing_engine_evaluation_form"):
        path_text = st.text_area(
            "PDF file or directory paths",
            value=default_path,
            help="Use one path per line, or comma-separated paths. Directories are sampled for *.pdf files.",
            height=72,
        )
        default_engines = [
            engine
            for engine in ["pymupdf", "opendataloader"]
            if engine in SUPPORTED_EXTRACTION_ENGINES
        ]
        engines = st.multiselect(
            "Engines",
            options=sorted(SUPPORTED_EXTRACTION_ENGINES),
            default=default_engines,
            help=(
                "Optional parsers are opt-in: opendataloader requires Java, "
                "docling requires `pip install docling`, marker can be heavy, "
                "and pdf-to-markdown requires the @pspdfkit/pdf-to-markdown CLI on PATH."
            ),
        )
        col1, col2, col3 = st.columns(3)
        limit = col1.number_input(
            "Sample limit",
            min_value=0,
            max_value=500,
            value=5,
            help="0 means all matching PDFs.",
        )
        raw = col2.checkbox(
            "Raw output",
            value=False,
            help="Compare raw extractor output before finance-report cleanup filters.",
        )
        write_samples = col3.checkbox(
            "Save samples",
            value=True,
            help="Persist per-engine extracted text samples for manual inspection.",
        )
        sample_chars = st.number_input(
            "Sample characters",
            min_value=0,
            max_value=200_000,
            value=4000,
            step=500,
            help="0 saves full extracted text when samples are enabled.",
        )
        submitted = st.form_submit_button("Run parsing evaluation", use_container_width=True)

    if submitted:
        paths = _parse_monitoring_paths(path_text)
        if not paths:
            st.warning("PDF path를 하나 이상 입력해 주세요.")
        elif not engines:
            st.warning("비교할 parsing engine을 하나 이상 선택해 주세요.")
        else:
            with st.spinner("Parsing engines are running..."):
                try:
                    result = run_pdf_extraction_comparison(
                        paths,
                        engines,
                        limit=int(limit),
                        raw=raw,
                        write_samples=write_samples,
                        sample_chars=int(sample_chars),
                    )
                except Exception as exc:
                    st.error(f"Parsing evaluation failed: {exc}")
                else:
                    st.session_state.latest_parsing_evaluation = result
                    st.success("Parsing evaluation completed.")

    result = st.session_state.get("latest_parsing_evaluation")
    if not result:
        st.caption("아직 실행된 parsing evaluation 결과가 없습니다.")
        return

    st.markdown("#### Latest run")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Run ID", str(result.get("run_id")))
    col2.metric("Files", result.get("file_count", 0))
    col3.metric("Engines", len(result.get("engines") or []))
    col4.metric("Raw", "yes" if result.get("raw") else "no")

    st.markdown("#### Engine summary")
    st.dataframe(
        _engine_summary_rows(result.get("summary") or {}),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("#### Output artifacts")
    st.json(
        {
            "csv_path": result.get("csv_path"),
            "json_path": result.get("json_path"),
            "sample_dir": result.get("sample_dir"),
        }
    )

    rows = result.get("rows") or []
    st.markdown("#### Per-PDF rows")
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        error_rows = [row for row in rows if row.get("status") != "ok"]
        if error_rows:
            with st.expander(f"Errors ({len(error_rows)})", expanded=True):
                st.dataframe(error_rows, use_container_width=True, hide_index=True)
    else:
        st.caption("No row data.")



def _all_thread_messages() -> list[dict]:
    threads = list_threads()
    return [
        {"thread": thread, "messages": list_messages(thread["id"])}
        for thread in threads
    ]


def _latest_saved_evaluation_run(
    exclude_path: str | None = None,
    execution_mode: str | None = None,
) -> dict | None:
    if not MONITORING_EVAL_RUN_DIR.exists():
        return None
    run_paths = sorted(MONITORING_EVAL_RUN_DIR.glob("evaluation_run_*.json"), reverse=True)
    loaded_runs: list[dict] = []
    for run_path in run_paths:
        if exclude_path and str(run_path) == exclude_path:
            continue
        try:
            run = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        loaded_runs.append(run)
    matching_runs = filter_evaluation_runs_by_mode(loaded_runs, execution_mode)
    return matching_runs[0] if matching_runs else None


def _run_fixed_snapshot_evaluation(
    *,
    selected_case_ids: list[str],
    latency_threshold_seconds: float,
) -> dict:
    repo_root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        str(repo_root / "scripts" / "run_evaluation_snapshot.py"),
        "--dataset",
        str(repo_root / "tests" / "fixtures" / "evaluation_dataset.json"),
        "--snapshot-root",
        str(repo_root / "tests" / "fixtures" / "eval_snapshot"),
        "--output-dir",
        str(repo_root / MONITORING_EVAL_RUN_DIR),
        "--latency-threshold-seconds",
        str(latency_threshold_seconds),
    ]
    for case_id in selected_case_ids:
        command.extend(["--case-id", case_id])
    completed = subprocess.run(
        command,
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=None,
    )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    try:
        payload = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Snapshot runner returned non-JSON output. stdout={stdout!r}, stderr={stderr!r}") from exc
    if completed.returncode != 0 or payload.get("status") != "ok":
        detail = payload.get("validation") or payload.get("error") or stderr or stdout
        raise RuntimeError(f"Snapshot evaluation failed: {detail}")
    json_path = Path(payload["json_path"])
    if not json_path.is_absolute():
        json_path = repo_root / json_path
    return json.loads(json_path.read_text(encoding="utf-8"))


def _render_experiment_monitoring() -> None:
    st.subheader("실험 실행")
    st.caption("고정 evaluation dataset을 current data 또는 fixed snapshot 모드로 실행하고 route/filter/source/citation/latency pass/fail을 저장합니다.")
    try:
        dataset = load_evaluation_dataset()
    except FileNotFoundError:
        st.warning("평가셋 fixture를 찾지 못했습니다: tests/fixtures/evaluation_dataset.json")
        return

    mode_label = st.radio(
        "실험 실행 모드",
        ["현재 데이터로 실행", "고정 테스트 snapshot으로 실행"],
        index=1,
        horizontal=True,
        help="baseline 비교는 같은 실행 모드끼리만 의미 있습니다.",
    )
    execution_mode = "fixed_snapshot" if mode_label == "고정 테스트 snapshot으로 실행" else "current_data"
    snapshot_validation = None
    if execution_mode == "current_data":
        st.info("현재 `data/reports.db`와 `data/vector_db`를 사용합니다. DB/index가 바뀌면 baseline 비교가 흔들릴 수 있습니다.")
    else:
        st.info("`tests/fixtures/eval_snapshot`의 고정 DB/index를 별도 Python 프로세스에서 사용합니다.")
        try:
            manifest = load_evaluation_snapshot_manifest()
        except FileNotFoundError:
            st.error("Snapshot manifest를 찾지 못했습니다: tests/fixtures/eval_snapshot/manifest.json")
        else:
            snapshot_validation = validate_evaluation_snapshot(dataset, manifest)
            if snapshot_validation["status"] == "pass":
                st.success("Fixed snapshot validation passed.")
            else:
                st.error("Fixed snapshot validation failed. Snapshot DB/index를 생성한 뒤 실행할 수 있습니다.")
                st.dataframe(snapshot_validation["checks"], use_container_width=True, hide_index=True)

    cases = dataset.get("cases") or []
    case_ids = [str(case.get("id")) for case in cases]
    selected_case_ids = st.multiselect(
        "실행할 테스트 케이스",
        options=case_ids,
        default=case_ids,
        format_func=lambda case_id: next(
            (f"{case_id} · {case.get('question', '')}" for case in cases if str(case.get("id")) == case_id),
            case_id,
        ),
        help="개수가 아니라 실제로 실행할 테스트 케이스를 선택합니다.",
    )
    latency_threshold = st.number_input("Latency threshold seconds", min_value=1.0, max_value=300.0, value=30.0, step=1.0)
    selected_cases = select_evaluation_cases(dataset, selected_case_ids)
    st.caption(f"선택된 테스트: {len(selected_cases)}개")
    snapshot_ready = execution_mode == "current_data" or (snapshot_validation or {}).get("status") == "pass"
    if st.button("Run selected evaluation cases", use_container_width=True, disabled=not selected_cases or not snapshot_ready):
        with st.spinner("Evaluation dataset 실행 중..."):
            try:
                if execution_mode == "fixed_snapshot":
                    run = _run_fixed_snapshot_evaluation(
                        selected_case_ids=selected_case_ids,
                        latency_threshold_seconds=float(latency_threshold),
                    )
                else:
                    run = run_evaluation_dataset(
                        dataset,
                        graph_app.invoke,
                        output_dir=MONITORING_EVAL_RUN_DIR,
                        selected_case_ids=selected_case_ids,
                        latency_threshold_seconds=float(latency_threshold),
                        execution_mode="current_data",
                        data_source={"db_path": "data/reports.db", "faiss_dir": "data/vector_db"},
                    )
            except Exception as exc:
                st.error(f"Evaluation run failed: {exc}")
            else:
                st.session_state.latest_evaluation_run = run
                st.success("Evaluation run saved.")

    latest_run = st.session_state.get("latest_evaluation_run")
    if latest_run and (latest_run.get("execution_mode") or "current_data") != execution_mode:
        latest_run = None
    run = latest_run or _latest_saved_evaluation_run(execution_mode=execution_mode)
    if not run:
        st.caption("아직 저장된 evaluation run이 없습니다.")
        return

    st.markdown("#### Latest run summary")
    st.caption(f"Execution mode: `{run.get('execution_mode') or 'current_data'}`")
    summary = run.get("summary") or {}
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Cases", summary.get("case_count", 0))
    col2.metric("Passed", summary.get("passed", 0))
    col3.metric("Failed", summary.get("failed", 0))
    col4.metric("Pass rate", f"{summary.get('pass_rate', 0) * 100:.1f}%")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Source hit", f"{summary.get('source_hit_rate', 0) * 100:.1f}%")
    col2.metric("Citation valid", f"{summary.get('citation_valid_rate', 0) * 100:.1f}%")
    col3.metric("No-result", f"{summary.get('no_result_rate', 0) * 100:.1f}%")
    latency = summary.get("avg_latency_seconds")
    col4.metric("Avg latency", "-" if latency is None else f"{latency:.2f}s")

    previous = _latest_saved_evaluation_run(
        exclude_path=run.get("json_path"),
        execution_mode=run.get("execution_mode") or "current_data",
    )
    comparison = compare_evaluation_runs(run, previous)
    if comparison:
        st.markdown("#### Previous run comparison")
        st.caption("같은 execution mode의 이전 run과만 비교합니다.")
        st.dataframe([comparison], use_container_width=True, hide_index=True)

    st.markdown("#### Run artifacts")
    st.code(run.get("json_path") or "", language="text")
    st.markdown("#### Case results")
    results = run.get("results") or []
    st.dataframe(results, use_container_width=True, hide_index=True)

    failure_actions = build_evaluation_failure_actions(results)
    st.markdown("#### Failure triage")
    if failure_actions:
        st.warning("Fail 케이스는 아래 권장 조치 기준으로 다음 작업을 선택하세요.")
        st.dataframe(failure_actions, use_container_width=True, hide_index=True)
        failed_case_ids = [str(row["case_id"]) for row in failure_actions if row.get("case_id")]
        if st.button("Rerun failed cases only", use_container_width=True):
            with st.spinner("Failed cases 재실행 중..."):
                try:
                    if (run.get("execution_mode") or "current_data") == "fixed_snapshot":
                        rerun = _run_fixed_snapshot_evaluation(
                            selected_case_ids=failed_case_ids,
                            latency_threshold_seconds=float(latency_threshold),
                        )
                    else:
                        rerun = run_evaluation_dataset(
                            dataset,
                            graph_app.invoke,
                            output_dir=MONITORING_EVAL_RUN_DIR,
                            selected_case_ids=failed_case_ids,
                            latency_threshold_seconds=float(latency_threshold),
                            execution_mode="current_data",
                            data_source={"db_path": "data/reports.db", "faiss_dir": "data/vector_db"},
                        )
                except Exception as exc:
                    st.error(f"Failed-case rerun failed: {exc}")
                else:
                    st.session_state.latest_evaluation_run = rerun
                    st.success("Failed cases rerun saved.")
                    st.rerun()
    else:
        st.success("현재 run에는 triage가 필요한 fail 케이스가 없습니다.")


def _render_global_monitoring(status: dict) -> None:
    st.subheader("전체 Monitoring")
    st.caption("모든 대화와 저장소 상태를 집계해 운영 품질을 봅니다. 개별 chat 원문은 기본 노출하지 않습니다.")
    thread_messages = _all_thread_messages()
    summary = summarize_all_chat_threads(thread_messages)
    integrity = summarize_data_integrity(status)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Threads", summary["thread_count"])
    col2.metric("Assistant", summary["assistant_message_count"])
    col3.metric("Failure rate", f"{summary['failure_rate'] * 100:.1f}%")
    col4.metric("No-result rate", f"{summary['no_result_rate'] * 100:.1f}%")

    col1, col2, col3 = st.columns(3)
    avg_latency = summary.get("avg_latency_seconds")
    p95_latency = summary.get("p95_latency_seconds")
    col1.metric("Avg latency", "-" if avg_latency is None else f"{avg_latency:.2f}s")
    col2.metric("P95 latency", "-" if p95_latency is None else f"{p95_latency:.2f}s")
    col3.metric("Integrity issues", integrity["warning_count"] + integrity["fail_count"])

    left, right = st.columns(2)
    with left:
        st.markdown("#### Status counts")
        st.dataframe([{"status": key, "count": value} for key, value in sorted(summary["statuses"].items())], use_container_width=True, hide_index=True)
    with right:
        st.markdown("#### Route counts")
        st.dataframe([{"route": key, "count": value} for key, value in sorted(summary["routes"].items())], use_container_width=True, hide_index=True)

    st.markdown("#### Data integrity checks")
    st.dataframe(
        [{"check": key, **value} for key, value in integrity["checks"].items()],
        use_container_width=True,
        hide_index=True,
    )
    failures = summary.get("recent_failures") or []
    st.markdown("#### Recent failed responses")
    if failures:
        st.dataframe(failures, use_container_width=True, hide_index=True)
    else:
        st.caption("최근 실패 응답이 없습니다.")


def _render_issue_report_monitoring() -> None:
    st.subheader("Issue reports")
    st.caption("사용자 신고를 전체 개선 루프의 입력으로 모아 봅니다. 필요하면 실패 케이스를 regression 후보로 승격할 수 있습니다.")
    reports = list_issue_reports()
    thread_names = {thread["id"]: thread["name"] for thread in list_threads()}
    summary = summarize_issue_reports(reports)

    col1, col2, col3 = st.columns(3)
    col1.metric("Reports", summary["report_count"])
    col2.metric("Threads", summary["thread_count"])
    col3.metric("Categories", len(summary["categories"]))

    if summary["categories"]:
        st.markdown("#### Category counts")
        st.dataframe(
            [{"category": category, "count": count} for category, count in sorted(summary["categories"].items(), key=lambda item: (-item[1], item[0]))],
            use_container_width=True,
            hide_index=True,
        )

    rows = build_issue_report_rows(reports, thread_names=thread_names)
    st.markdown("#### Report rows")
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
        selected_report_id = st.selectbox("상세 보기", options=[row["id"] for row in rows])
        selected = next((report for report in reports if report.get("id") == selected_report_id), None)
        if selected:
            st.code(selected.get("file_path") or "", language="text")
            if st.button("Regression suite 후보로 저장", use_container_width=True):
                candidate = promote_issue_report_to_eval_candidate(
                    selected,
                    output_dir=MONITORING_REGRESSION_CANDIDATE_DIR,
                )
                st.success("Regression candidate artifact를 저장했습니다.")
                st.code(candidate["json_path"], language="text")
            with st.expander("원문 보기", expanded=False):
                st.text(selected.get("content") or "")
    else:
        st.caption("저장된 issue report가 없습니다.")

    st.markdown("#### Regression candidates")
    candidates = list_regression_candidates(MONITORING_REGRESSION_CANDIDATE_DIR)
    candidate_rows = build_regression_candidate_rows(candidates)
    if not candidate_rows:
        st.caption("저장된 regression candidate가 없습니다.")
        return

    st.dataframe(candidate_rows, use_container_width=True, hide_index=True)
    draft_candidates = [candidate for candidate in candidates if candidate.get("eval_case_draft")]
    if not draft_candidates:
        st.info("아직 evaluation case draft가 있는 candidate가 없습니다. Chat Monitoring trace issue를 후보로 저장하면 draft가 생성됩니다.")
        return

    candidate_ids = [str(candidate.get("id")) for candidate in draft_candidates]
    selected_candidate_ids = st.multiselect(
        "실행할 regression candidate draft",
        options=candidate_ids,
        default=candidate_ids,
        format_func=lambda candidate_id: next(
            (
                f"{candidate_id} · {(candidate.get('eval_case_draft') or {}).get('question', '')}"
                for candidate in draft_candidates
                if str(candidate.get("id")) == candidate_id
            ),
            candidate_id,
        ),
        help="정식 fixture 반영 전, 선택한 candidate draft만 current data 기준으로 재현 실행합니다.",
    )
    selected_dataset = build_regression_candidate_dataset(draft_candidates, selected_candidate_ids)
    st.caption(f"선택된 draft: {len(selected_dataset['cases'])}개")
    if selected_dataset["cases"]:
        with st.expander("선택된 evaluation case draft JSON", expanded=False):
            st.json(selected_dataset)
    if st.button("Run selected regression candidate drafts", use_container_width=True, disabled=not selected_dataset["cases"]):
        with st.spinner("Regression candidate draft 실행 중..."):
            try:
                run = run_evaluation_dataset(
                    selected_dataset,
                    graph_app.invoke,
                    output_dir=MONITORING_EVAL_RUN_DIR,
                    selected_case_ids=[case.get("id") for case in selected_dataset["cases"]],
                    execution_mode="regression_candidate_current_data",
                    data_source={
                        "db_path": "data/reports.db",
                        "faiss_dir": "data/vector_db",
                        "candidate_ids": selected_candidate_ids,
                    },
                )
            except Exception as exc:
                st.error(f"Regression candidate run failed: {exc}")
            else:
                st.session_state.latest_regression_candidate_run = run
                st.success("Regression candidate run saved.")
                st.code(run.get("json_path") or "", language="text")

    latest_candidate_run = st.session_state.get("latest_regression_candidate_run")
    if latest_candidate_run:
        st.markdown("#### Latest regression candidate run")
        st.json(latest_candidate_run.get("summary") or {})
        st.dataframe(latest_candidate_run.get("results") or [], use_container_width=True, hide_index=True)

def render_chat_monitoring_page(current_id: str, current_thread: dict) -> None:
    """Render metrics for the currently selected chat only."""
    st.header("Chat Monitoring")
    st.caption(f"현재 선택된 chat: {current_thread['name']}")
    messages = list_messages(current_id)
    summary = summarize_chat_messages(messages)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Messages", summary["message_count"])
    col2.metric("Assistant", summary["assistant_message_count"])
    col3.metric("Avg sources", f"{summary['avg_rerank_source_count']:.1f}")
    latency = summary["avg_latency_seconds"]
    col4.metric("Avg latency", "-" if latency is None else f"{latency:.2f}s")

    left, right = st.columns(2)
    with left:
        st.markdown("#### Status counts")
        st.json(summary["statuses"])
    with right:
        st.markdown("#### Route counts")
        st.json(summary["routes"])

    st.markdown("#### Assistant response rows")
    rows = build_message_monitoring_rows(messages)
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.caption("아직 모니터링할 assistant 응답이 없습니다.")

    st.markdown("#### 응답 선택 상세")
    selectable_rows = [row for row in rows if row.get("message_id") is not None]
    if selectable_rows:
        label_by_id = {row["message_id"]: row.get("label", str(row["message_id"])) for row in selectable_rows}
        selected_message_id = st.selectbox(
            "상세 볼 응답 선택",
            [row["message_id"] for row in selectable_rows],
            index=len(selectable_rows) - 1,
            format_func=lambda message_id: label_by_id.get(message_id, str(message_id)),
            key=f"chat_monitoring_selected_response_{current_id}",
        )
        selected_message = next(
            (message for message in messages if message.get("id") == selected_message_id),
            None,
        )
        if selected_message:
            selected_user_question = user_question_before_message(messages, selected_message_id)
            previous_message = previous_successful_assistant(messages, selected_message_id)
            detail = build_message_trace_detail(selected_message, user_question=selected_user_question)
            diff = build_response_diff(selected_message, previous_message)
            hints = build_chat_trace_debug_hints(
                selected_message,
                previous_message,
                user_question=selected_user_question,
            )

            trace_summary = build_message_trace_summary(detail, diff=diff, hints=hints)
            trace_tabs = st.tabs([
                "Trace summary",
                "Scope / routing",
                "Advanced diagnostics",
            ])
            with trace_tabs[0]:
                st.json(trace_summary)
                st.markdown("#### Debug hints")
                if hints:
                    for hint in hints:
                        st.warning(hint)
                else:
                    st.success("현재 선택 응답에서 자동 감지된 흔한 RAG 실패 패턴은 없습니다.")

                st.markdown("#### Previous vs selected diff")
                if diff:
                    st.json(diff)
                else:
                    st.caption("비교할 이전 성공 assistant 응답이 없습니다.")
            with trace_tabs[1]:
                st.markdown("##### Query rewrite / follow-up")
                st.json(detail["query_rewrite"])
                st.markdown("##### Scope / filters")
                st.json(detail["scope"])
                st.markdown("##### Routing")
                st.json(detail["routing"])
            with trace_tabs[2]:
                with st.expander("State transitions", expanded=True):
                    st.json(detail["state_transitions"])
                with st.expander("Retrieval / rerank", expanded=False):
                    st.json(detail["retrieval"])
                with st.expander("Answer / citations", expanded=False):
                    st.json(detail["answer"])

            if st.button(
                "Create issue report with selected trace",
                key=f"chat_monitoring_issue_report_{current_id}_{selected_message_id}",
            ):
                report = create_issue_report(
                    current_id,
                    "Chat Monitoring trace",
                    "Selected response trace from Chat Monitoring",
                    build_chat_trace_issue_context(
                        current_thread,
                        messages,
                        selected_message_id=selected_message_id,
                    ),
                )
                st.success(f"Issue report saved: {report['file_path']}")
    else:
        st.caption("상세 trace를 표시할 assistant 응답이 없습니다.")

def _render_unembedded_reports(status: dict) -> None:
    """Render DB reports that have not been embedded yet and allow embedding retry."""
    db_status = status["db"]
    db_path = (status.get("paths") or {}).get("db_path")
    pending_count = int(db_status.get("pending_reports") or 0)
    st.subheader("DB에는 있지만 임베딩되지 않은 문서")
    st.caption(
        "RDB에는 존재하지만 VectorDB 검색/상세 답변에는 아직 사용되지 않는 리포트입니다. "
        "이 목록이 남아 있으면 RDB 목록과 VectorDB 상세 답변의 source coverage가 달라질 수 있습니다."
    )

    col1, col2 = st.columns(2)
    col1.metric("미임베딩 문서", f"{pending_count}건")
    col2.metric("검색 커버리지", f"{status['search_coverage_ratio'] * 100:.1f}%")

    display_limit = st.number_input(
        "표시할 미임베딩 문서 수",
        min_value=10,
        max_value=1000,
        value=min(max(pending_count, 10), 200),
        step=10,
        key="unembedded_report_display_limit",
        help="최근 report_date 순으로 표시합니다. 전체 임베딩 버튼은 표시 개수와 무관하게 모든 pending 문서를 대상으로 합니다.",
    )
    rows = build_unembedded_report_rows(
        list_unembedded_reports(db_path, limit=int(display_limit)) if db_path else []
    )
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.success("현재 미임베딩 문서가 없습니다.")

    status_payload = data_update_jobs.read_status()
    job_active = _is_update_job_active(status_payload)
    if job_active:
        st.info("이미 데이터 업데이트/임베딩 작업이 실행 중입니다.")
    if status.get("embedding_limit_active"):
        st.warning("TEST_LIMIT가 설정되어 있습니다. 전체 처리 버튼은 --all로 실행하지만, 설정값을 확인해 주세요.")

    st.markdown("#### 임베딩 시도")
    embed_limit = st.number_input(
        "이번에 처리할 최대 문서 수 (0 = 전체)",
        min_value=0,
        max_value=max(pending_count, 1),
        value=min(pending_count, 20) if pending_count else 0,
        step=1,
        key="unembedded_embedding_limit",
        help="작게 시작해 로그를 확인하거나, 0을 선택해 모든 미임베딩 문서를 처리합니다.",
    )
    button_label = "미임베딩 문서 전체 임베딩 시도" if int(embed_limit) == 0 else f"미임베딩 문서 {int(embed_limit)}건 임베딩 시도"
    if st.button(
        button_label,
        key="start_unembedded_embedding_job",
        disabled=job_active or pending_count == 0,
        use_container_width=True,
    ):
        limit = None if int(embed_limit) == 0 else int(embed_limit)
        data_update_jobs.start_embedding_job(
            label=button_label,
            limit=limit,
        )
        st.success("임베딩 작업을 시작했습니다. 아래 진행 상태를 확인하세요.")
        _app_rerun()

    _render_update_progress()


def render_global_monitoring_page() -> None:
    """Render global Monitoring Mode pages that do not depend on a selected chat."""
    st.header("Monitoring Mode")
    st.caption(
        "성능개선을 위한 지표 모니터링 화면입니다. parsing, chunking, retrieval/rerank, "
        "모델 변경에 따른 답변 안정성, latency/비용을 같은 기준선으로 비교하기 위한 정보를 모읍니다."
    )

    status = get_data_status()
    db_status = status["db"]
    vector_status = status["vector_db"]
    config = status["config"]

    (
        data_tab,
        unembedded_tab,
        experiment_tab,
        eval_tab,
        parsing_tab,
        global_monitoring_tab,
        issue_tab,
    ) = st.tabs([label for label in build_monitoring_tab_labels() if label != "Chat Monitoring"])

    with data_tab:
        st.subheader("데이터 준비 상태")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("리포트", f"{db_status['total_reports']}건")
        col2.metric("임베딩 완료", f"{db_status['embedded_reports']}건")
        col3.metric("미완료", f"{db_status['pending_reports']}건")
        col4.metric("검색 커버리지", f"{status['search_coverage_ratio'] * 100:.1f}%")

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("FAISS", "있음" if vector_status["has_faiss_index"] else "없음")
        col2.metric("Vector files", f"{vector_status['file_count']}개")
        col3.metric("Parent chunks", f"{db_status['parent_chunks']}건")
        col4.metric("PDF", f"{status['downloaded_pdfs']}개")

        st.subheader("현재 파이프라인 설정")
        st.json(
            {
                "generation_model": config["generation_model"],
                "embedding_model": config["embedding_model"],
                "extraction_engine": config["extraction_engine"],
                "unembedded_extraction_engine": config.get("unembedded_extraction_engine"),
                "use_parent_child": config["use_parent_child"],
                "use_reranker": config["use_reranker"],
                "search_top_k": config["search_top_k"],
                "test_limit": config["test_limit"],
            }
        )

        st.subheader("날짜별 데이터 캘린더 원천")
        date_counts = [
            {
                "report_date": report_date,
                "embedded_count": count,
                **(db_status.get("report_date_type_counts") or {}).get(report_date, {}),
            }
            for report_date, count in (db_status.get("report_date_counts") or {}).items()
        ]
        st.dataframe(date_counts, use_container_width=True, hide_index=True)

    with unembedded_tab:
        _render_unembedded_reports(status)

    with experiment_tab:
        _render_experiment_monitoring()

    with eval_tab:
        st.subheader("고정 평가 테스트셋")
        try:
            dataset = load_evaluation_dataset()
            summary = summarize_evaluation_dataset(dataset)
        except FileNotFoundError:
            st.warning("평가셋 fixture를 찾지 못했습니다: tests/fixtures/evaluation_dataset.json")
            return

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Version", summary["version"])
        col2.metric("Cases", summary["case_count"])
        col3.metric("Expected sources", summary["expected_source_count"])
        col4.metric("Snapshot", summary["snapshot_date"] or "-")

        stability_policy = summary.get("stability_policy") or {}
        st.info(
            "테스트셋은 변경 사유가 생기기 전까지 고정합니다. "
            f"정책: `{stability_policy.get('policy', '-')}`"
        )

        left, right = st.columns(2)
        with left:
            st.markdown("#### Route case coverage")
            st.dataframe(_case_type_rows(summary), use_container_width=True, hide_index=True)
        with right:
            st.markdown("#### Monitoring dimensions")
            st.dataframe(_dimension_rows(summary), use_container_width=True, hide_index=True)

        with st.expander("변경 허용 사유"):
            st.write(stability_policy.get("allowed_change_reasons") or [])
        with st.expander("평가 케이스 목록"):
            st.dataframe(
                [
                    {
                        "id": case.get("id"),
                        "type": case.get("type"),
                        "route": case.get("expected_route"),
                        "dimensions": ", ".join(case.get("monitoring_dimensions", [])),
                        "question": case.get("question"),
                    }
                    for case in dataset.get("cases", [])
                ],
                use_container_width=True,
                hide_index=True,
            )

    with parsing_tab:
        _render_parsing_engine_evaluation()

    with global_monitoring_tab:
        _render_global_monitoring(status)

    with issue_tab:
        _render_issue_report_monitoring()


threads = _load_threads()
_ensure_current_thread(threads)
current_id = st.session_state.current_thread_id
current_thread = next(thread for thread in threads if thread["id"] == current_id)

_show_queued_chat_job_toasts()
_render_chat_job_notifications(current_id)

with st.sidebar:
    render_sidebar(current_id)

if MONITORING_MODE:
    active_page = st.session_state.get("active_monitoring_page", "Chat")
    if active_page == "전체 Monitoring":
        render_global_monitoring_page()
    else:
        chat_tab, chat_monitoring_tab = st.tabs(["Chat", "Chat Monitoring"])
        with chat_tab:
            render_chat(current_id, current_thread)
        with chat_monitoring_tab:
            render_chat_monitoring_page(current_id, current_thread)
else:
    render_chat(current_id, current_thread)
