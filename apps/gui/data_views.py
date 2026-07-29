import calendar
import sqlite3
from datetime import date, datetime, timedelta
from html import escape

import streamlit as st

from src.configs import config as config_module
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
    if nav_prev_col.button("◀ 이전", key="report_calendar_prev", disabled=current_index == 0, use_container_width=True):
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
        use_container_width=True,
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


@st.fragment(run_every=2.0)
def render_update_progress() -> None:
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


def render_data_update_controls(db_status: dict) -> None:
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
                st.rerun()
        elif selected_categories:
            st.caption("선택 기간 내 업데이트할 임베딩 미완료 평일이 없습니다.")
    else:
        st.caption("업데이트할 시작일과 종료일을 선택해 주세요.")

    if latest_range:
        start_date, end_date = latest_range
        st.caption(f"최신 누락 범위\n\n`{start_date}` ~ `{end_date}`")

    render_update_progress()


def render_unembedded_reports(status: dict) -> None:
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
    list_error: str | None = None
    try:
        reports = (
            status_module.list_unembedded_reports(
                db_path,
                limit=int(display_limit),
            )
            if db_path
            else []
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        reports = []
        list_error = f"{type(exc).__name__}: {exc}"
    rows = status_module.build_unembedded_report_rows(reports)
    retrieval_status = status.get("retrieval") or {}
    native_v2 = retrieval_status.get("mode") in {
        "native",
        "epoch_zero_compatibility",
    }
    native_retry_ready = (
        retrieval_status.get("mode") == "native"
        and int(retrieval_status.get("write_epoch") or 0) > 0
        and bool(retrieval_status.get("active_snapshot_id"))
        and not bool(retrieval_status.get("v1_fallback_open"))
        and (
            bool(retrieval_status.get("write_enabled"))
            or bool(retrieval_status.get("degraded"))
        )
    )
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    elif list_error:
        st.error(
            "미임베딩 문서 목록을 읽지 못했습니다. 데이터 상태의 native "
            f"runtime 오류를 확인해 주세요. ({list_error})"
        )
    else:
        st.success("현재 미임베딩 문서가 없습니다.")

    status_payload = data_update_jobs.read_status()
    job_active = _is_update_job_active(status_payload)
    if job_active:
        st.info("이미 데이터 업데이트/임베딩 작업이 실행 중입니다.")
    if status.get("embedding_limit_active"):
        st.warning("TEST_LIMIT가 설정되어 있습니다. 전체 처리 버튼은 --all로 실행하지만, 설정값을 확인해 주세요.")

    st.markdown("#### 임베딩 시도")
    if native_v2:
        embed_limit = 0
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
    else:
        embed_limit = st.number_input(
            "이번에 처리할 최대 문서 수 (0 = 전체)",
            min_value=0,
            max_value=max(pending_count, 1),
            value=min(pending_count, 20) if pending_count else 0,
            step=1,
            key="unembedded_embedding_limit",
            help="작게 시작해 로그를 확인하거나, 0을 선택해 모든 미임베딩 문서를 처리합니다.",
        )
        button_label = (
            "미임베딩 문서 전체 임베딩 시도"
            if int(embed_limit) == 0
            else f"미임베딩 문서 {int(embed_limit)}건 임베딩 시도"
        )
    if st.button(
        button_label,
        key="start_unembedded_embedding_job",
        disabled=(
            job_active
            or pending_count == 0
            or (native_v2 and not native_retry_ready)
        ),
        use_container_width=True,
    ):
        limit = None if int(embed_limit) == 0 else int(embed_limit)
        data_update_jobs.start_embedding_job(
            label=button_label,
            limit=limit,
            retry_extraction_failures=native_retry_ready,
        )
        st.success("임베딩 작업을 시작했습니다. 아래 진행 상태를 확인하세요.")
        st.rerun(scope="app")

    render_update_progress()
