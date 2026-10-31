import calendar
import sqlite3
from datetime import date, datetime, timedelta
from html import escape

import streamlit as st

from src.configs import config as config_module
from src.core import collection_check_store
from src.core import data_update_jobs
from src.core import status as status_module


WEEKDAY_LABELS = ["월", "화", "수", "목", "금"]


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


def render_report_calendar(db_status: dict) -> None:
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
    if nav_prev_col.button("◀ 이전", key="report_calendar_prev", disabled=current_index == 0, width="stretch"):
        _set_calendar_month(values[current_index - 1])
        st.rerun()
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
        width="stretch",
    ):
        _set_calendar_month(values[current_index + 1])
        st.rerun()

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


def _step_icon(
    status_phase: str | None,
    step: str,
    state: str | None,
    *,
    partial_failure: bool = False,
    download_failure_count: int = 0,
    embedding_failure_count: int = 0,
) -> str:
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


def _status_step_icon(status: dict, step: str) -> str:
    return _step_icon(
        status.get("phase"),
        step,
        status.get("state"),
        partial_failure=bool(status.get("partial_failure")),
        download_failure_count=int(status.get("download_failure_count") or 0),
        embedding_failure_count=int(status.get("embedding_failure_count") or 0),
    )


def _render_update_steps(status: dict) -> None:
    state = status.get("state")
    phase = status.get("phase")
    embed_detail = ""
    if phase == "embed" and status.get("embedding_current") and status.get("embedding_total"):
        embed_detail = f" ({status['embedding_current']}/{status['embedding_total']})"

    rows = [
        ("download", "리포트 다운로드/확인"),
        ("embed", f"문서 처리/검색 반영{embed_detail}"),
        ("done", "완료"),
    ]
    st.markdown(
        "\n".join(
            f"<div style='font-size:0.78rem; line-height:1.35rem; color:#475569;'>"
            f"{_status_step_icon(status, step)} {label}</div>"
            for step, label in rows
        ),
        unsafe_allow_html=True,
    )


def _successful_update_message(status: dict) -> str:
    """Return a success message for current and legacy completed job statuses."""
    if not status.get("partial_failure"):
        return status.get("message", "데이터 업데이트가 완료되었습니다.")
    label = str(status.get("label") or "데이터 업데이트")
    return f"{label}: 데이터 업데이트가 완료되었습니다."


@st.fragment(run_every=2.0)
def render_update_progress() -> None:
    status = data_update_jobs.read_status()
    if not status:
        return

    if history_warning := status.get("collection_check_warning"):
        st.warning(history_warning)

    state = status.get("state")
    message = status.get("message", "데이터 업데이트 상태를 확인 중입니다.")
    if state == "running":
        if not data_update_jobs.is_update_job_active(status):
            st.warning("이전 데이터 업데이트 작업이 중단되었습니다. 새 업데이트를 시작할 수 있습니다.")
            return
        st.info(message)
        if (
            status.get("phase") == "embed"
            and status.get("search_available_during_update")
        ):
            st.caption(
                "업데이트 중에도 기존 문서는 계속 검색할 수 있으며, "
                "처리가 끝난 문서부터 순차적으로 반영됩니다."
            )
        _render_update_steps(status)
    elif state == "succeeded":
        st.success(_successful_update_message(status))
        _render_update_steps(status)
    elif state == "failed":
        st.error(message)
        _render_update_steps(status)


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


def _format_check_time(value: str | None) -> str:
    if not value:
        return "시각 정보 없음"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def _render_matching_collection_check_content(
    selected_dates: list[str],
    selected_categories: list[str],
) -> None:
    try:
        check = collection_check_store.latest_matching_check(
            selected_dates=selected_dates,
            categories=selected_categories,
        )
    except collection_check_store.CollectionCheckStoreError:
        st.caption("수집 확인 이력을 읽지 못했습니다. 기존 문서는 계속 사용할 수 있습니다.")
        return
    if check is None:
        st.caption(
            "이 날짜·카테고리는 아직 수집 확인 기록이 없습니다. "
            "저장된 문서가 있어도 추가 발행분 확인이 끝났다는 뜻은 아닙니다."
        )
        return

    state = check.get("state")
    if state == "running":
        job_status = data_update_jobs.read_status()
        same_job = (
            job_status is not None
            and job_status.get("job_id") == check.get("check_id")
        )
        if same_job and job_status.get("collection_check_warning"):
            state = "unrecorded"
        elif not same_job or not data_update_jobs.is_update_job_active(job_status):
            state = "interrupted"
    state_label = {
        "running": "확인 중",
        "interrupted": "확인 중단됨",
        "unrecorded": "이력 저장 실패 · 작업 상태 확인 필요",
        "completed": "확인 완료",
        "empty": "확인 완료 · 조회된 자료 없음",
        "partial": "확인 완료 · 일부 자료 제외",
        "failed": "목록 확인 실패",
    }.get(state, "상태 확인 필요")
    time_label = _format_check_time(check.get("completed_at") or check.get("attempted_at"))
    details = []
    if state in {"completed", "partial"}:
        details.append(f"확인된 기존/신규 자료 {int(check.get('processed_count') or 0)}건")
    if int(check.get("failed_count") or 0):
        details.append(f"다운로드 제외 {int(check.get('failed_count') or 0)}건")
    detail_text = f" · {' · '.join(details)}" if details else ""
    st.caption(f"마지막 수집 확인: {time_label} · {state_label}{detail_text}")


@st.fragment(run_every=2.0)
def _render_matching_collection_check(
    selected_dates: list[str],
    selected_categories: list[str],
) -> None:
    _render_matching_collection_check_content(selected_dates, selected_categories)


def render_data_update_controls(db_status: dict) -> None:
    status = data_update_jobs.read_status()
    job_active = data_update_jobs.is_update_job_active(status)
    today = date.today()
    latest_range = data_update_jobs.build_update_range(last_date=db_status.get("max_report_date"), today=today)
    st.subheader("데이터 업데이트")
    if st.session_state.pop("show_data_update_hint", False):
        st.info("아래에서 기간과 카테고리를 선택한 뒤 데이터 업데이트를 실행하세요.")
    category_options = data_update_jobs.normalize_update_categories("all")
    default_categories = [
        category
        for category in data_update_jobs.normalize_update_categories(config_module.CRAWLER_CATEGORIES)
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
        help="선택한 카테고리를 외부 자료 목록에서 다시 확인합니다.",
    )
    default_start, default_end = _default_update_range(db_status)
    min_update_date = _update_min_date(db_status)
    selected_range = st.date_input(
        "업데이트 기간",
        value=(default_start, default_end),
        min_value=min_update_date,
        max_value=today,
        key="update_date_range",
        help="기존 문서가 있는 날짜도 다시 확인해 이후 추가된 자료를 찾습니다.",
    )

    if isinstance(selected_range, tuple) and len(selected_range) == 2:
        range_start, range_end = selected_range
        if not selected_categories:
            target_dates = []
            st.caption("업데이트할 카테고리를 하나 이상 선택해 주세요.")
        else:
            target_dates = [
                day.isoformat()
                for day in data_update_jobs.iter_weekdays(
                    range_start,
                    min(range_end, today),
                    today=today,
                )
            ]
            category_label = ", ".join(selected_categories)
            st.caption(
                f"수집 재확인 대상: {category_label} · 평일 {len(target_dates)}일 · "
                "기존 자료가 있는 날짜 포함"
            )
            _render_matching_collection_check(target_dates, selected_categories)
        if selected_categories and target_dates:
            if st.button(
                "선택 기간 다시 확인",
                key="update_selected_range",
                disabled=job_active,
                width="stretch",
            ):
                try:
                    data_update_jobs.start_update_job(
                        selected_dates=target_dates,
                        categories=selected_categories,
                        label=f"선택 기간 재확인 {len(target_dates)}일 ({', '.join(selected_categories)})",
                    )
                except data_update_jobs.DataUpdateJobAlreadyRunning:
                    st.warning("다른 데이터 업데이트 작업이 이미 실행 중입니다.")
                else:
                    st.rerun()
        elif selected_categories:
            st.caption("선택 기간에 확인할 평일이 없습니다.")
    else:
        st.caption("업데이트할 시작일과 종료일을 선택해 주세요.")

    if latest_range:
        start_date, end_date = latest_range
        st.caption(f"최신 누락 범위\n\n`{start_date}` ~ `{end_date}`")

    render_update_progress()


def _native_retry_ready(status: dict) -> bool:
    retrieval_status = status.get("retrieval") or {}
    return (
        retrieval_status.get("mode") == "native"
        and int(retrieval_status.get("write_epoch") or 0) > 0
        and bool(retrieval_status.get("active_snapshot_id"))
        and (
            bool(retrieval_status.get("write_enabled"))
            or bool(retrieval_status.get("degraded"))
        )
    )


def render_data_recovery_controls(status: dict) -> None:
    """Expose compact failed/unprepared document recovery to ordinary users."""
    db_status = status.get("db") or {}
    pending_count = int(db_status.get("pending_reports") or 0)
    data_root = (status.get("paths") or {}).get("data_root")
    reports: list[dict] = []
    list_failed = False
    if pending_count and data_root:
        try:
            reports = status_module.list_unembedded_reports(data_root, limit=5)
        except (OSError, sqlite3.Error, ValueError):
            list_failed = True

    with st.expander(f"검색에 반영되지 않은 자료 · {pending_count}건"):
        if pending_count == 0:
            st.caption("현재 다시 처리할 자료가 없습니다.")
            return

        st.caption(
            "다운로드됐지만 본문 준비에 실패했거나 검색에 아직 반영되지 않은 자료입니다. "
            "전체 재처리는 정상 자료를 유지한 채 이 자료들을 다시 확인합니다."
        )
        for report in reports:
            title = str(report.get("title") or report.get("file_name") or "제목 없음")
            report_date = str(report.get("report_date") or "날짜 미상")
            broker = str(report.get("broker") or "발행처 미상")
            st.caption(f"• {report_date} · {broker} · {title}")
        if pending_count > len(reports) and reports:
            st.caption(f"외 {pending_count - len(reports)}건")
        if list_failed:
            st.caption("자료 목록을 읽지 못했지만 전체 재처리는 시도할 수 있습니다.")

        current_status = data_update_jobs.read_status()
        job_active = data_update_jobs.is_update_job_active(current_status)
        retry_ready = _native_retry_ready(status)
        if job_active:
            st.caption("다른 데이터 작업이 진행 중입니다.")
        elif not retry_ready:
            st.caption("현재 데이터 저장소를 복구한 뒤 다시 처리할 수 있습니다.")
        if st.button(
            "반영되지 않은 자료 전체 다시 처리",
            key="retry_unprepared_reports_sidebar",
            disabled=job_active or not retry_ready,
            width="stretch",
        ):
            try:
                data_update_jobs.start_embedding_job(
                    label="반영되지 않은 자료 다시 처리",
                    retry_extraction_failures=True,
                )
            except data_update_jobs.DataUpdateJobAlreadyRunning:
                st.warning("다른 데이터 작업이 이미 실행 중입니다.")
            else:
                st.rerun(scope="app")


def render_unembedded_reports(status: dict) -> None:
    """Render DB reports that have not been embedded yet and allow embedding retry."""
    db_status = status["db"]
    data_root = (status.get("paths") or {}).get("data_root")
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
    list_error: str | None = None
    try:
        reports = (
            status_module.list_unembedded_reports(
                data_root,
                limit=int(display_limit),
            )
            if data_root
            else []
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        reports = []
        list_error = f"{type(exc).__name__}: {exc}"
    rows = status_module.build_unembedded_report_rows(reports)
    native_retry_ready = _native_retry_ready(status)
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)
    elif list_error:
        st.error(
            "미임베딩 문서 목록을 읽지 못했습니다. 데이터 상태의 native "
            f"runtime 오류를 확인해 주세요. ({list_error})"
        )
    else:
        st.success("현재 미임베딩 문서가 없습니다.")

    status_payload = data_update_jobs.read_status()
    job_active = data_update_jobs.is_update_job_active(status_payload)
    if job_active:
        st.info("이미 데이터 업데이트/임베딩 작업이 실행 중입니다.")

    st.markdown("#### 임베딩 시도")
    if native_retry_ready:
        st.info(
            "V2는 일반 업데이트에서 이전 파싱 실패를 건너뜁니다. "
            "아래 버튼을 누를 때만 실패 문서를 다시 파싱하며, 재실패해도 "
            "다른 문서 처리는 계속됩니다."
        )
    else:
        st.warning(
            "현재 V2는 쓰기 가능한 active 상태가 아닙니다. "
            "V2 활성화 또는 복구를 완료한 뒤 재시도할 수 있습니다."
        )
    button_label = "모든 파싱 실패/미임베딩 문서 다시 처리"
    if st.button(
        button_label,
        key="start_unembedded_embedding_job",
        disabled=(
            job_active
            or pending_count == 0
            or not native_retry_ready
        ),
        width="stretch",
    ):
        try:
            data_update_jobs.start_embedding_job(
                label=button_label,
                retry_extraction_failures=native_retry_ready,
            )
        except data_update_jobs.DataUpdateJobAlreadyRunning:
            st.warning("다른 데이터 업데이트 작업이 이미 실행 중입니다.")
        else:
            st.success("임베딩 작업을 시작했습니다. 아래 진행 상태를 확인하세요.")
            st.rerun(scope="app")

    render_update_progress()
