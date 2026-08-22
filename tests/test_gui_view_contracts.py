"""Behavior and state contracts for moving Streamlit views between modules."""

from __future__ import annotations

import ast
import calendar
import queue
import threading
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path


GUI_DIR = Path("apps/gui")
APP_PATH = GUI_DIR / "app.py"
CHAT_JOBS_PATH = GUI_DIR / "chat_jobs.py"
CHAT_VIEWS_PATH = GUI_DIR / "chat_views.py"
DATA_VIEWS_PATH = GUI_DIR / "data_views.py"
MONITORING_VIEWS_PATH = GUI_DIR / "monitoring_views.py"
SEARCH_ENGINE_PATH = GUI_DIR / "search_engine.py"
SIDEBAR_VIEWS_PATH = GUI_DIR / "sidebar_views.py"

CHAT_JOB_FUNCTIONS = {
    "_chat_job_registry",
    "_record_chat_job_event",
    "consume_chat_job_events",
    "_queue_chat_job_toast",
    "show_queued_chat_job_toasts",
    "repair_interrupted_chat_jobs",
    "_search_scope_from_graph_state",
    "latest_search_scope",
    "thread_has_running_job",
    "has_pending_search_engine_job",
    "chat_message_anchor_id",
    "_invoke_graph_with_timeout",
    "_run_chat_response_job",
    "start_chat_response_job",
    "render_chat_job_notifications",
}

CHAT_VIEW_FUNCTIONS = {
    "_search_engine_status_content",
    "render_search_engine_status",
    "_render_issue_report_control",
    "_scroll_to_anchor",
    "_resolve_report_pdf",
    "_open_report_pdf",
    "_render_sources",
    "_render_no_result_actions",
    "_render_message",
    "render_chat",
}

DATA_VIEW_FUNCTIONS = {
    "_parse_iso_date",
    "_month_options",
    "_calendar_day_table_cell_html",
    "_report_calendar_table_html",
    "_set_calendar_month",
    "render_report_calendar",
    "_step_icon",
    "_render_update_steps",
    "render_update_progress",
    "_default_update_range",
    "_update_min_date",
    "render_data_update_controls",
    "render_unembedded_reports",
}

MONITORING_VIEW_FUNCTIONS = {
    "_parse_monitoring_paths",
    "_engine_summary_rows",
    "_restore_monitoring_area_selection",
    "_store_monitoring_area_selection",
    "_render_parsing_engine_evaluation",
    "_all_thread_messages",
    "_latest_saved_evaluation_run",
    "_latest_v2_accuracy_run",
    "_render_experiment_monitoring",
    "_render_answer_metrics",
    "_render_global_monitoring",
    "_render_v2_data_diagnostics",
    "_chat_execution_label",
    "_chat_target_coverage_label",
    "_chat_grounding_label",
    "_chat_execution_status_label",
    "_chat_scope_caption",
    "_chat_performance_timing_rows",
    "_chat_technical_sections",
    "_render_chat_answer_performance",
    "_render_global_chat_diagnostics",
    "_format_chat_token_count",
    "_format_chat_token_rate",
    "_render_chat_latency_table",
    "_render_global_monitoring_area",
    "render_chat_monitoring_page",
    "render_global_monitoring_page",
}

SIDEBAR_VIEW_FUNCTIONS = {
    "_sidebar_rerun",
    "_app_rerun",
    "load_threads",
    "ensure_current_thread",
    "_set_current_thread",
    "_delete_thread_and_select_next",
    "_thread_status_badge",
    "_render_thread_row",
    "render_sidebar",
}

EXPECTED_VIEW_OWNERS = {
    **{name: CHAT_JOBS_PATH for name in CHAT_JOB_FUNCTIONS},
    **{name: CHAT_VIEWS_PATH for name in CHAT_VIEW_FUNCTIONS},
    **{name: DATA_VIEWS_PATH for name in DATA_VIEW_FUNCTIONS},
    **{name: MONITORING_VIEWS_PATH for name in MONITORING_VIEW_FUNCTIONS},
    **{name: SIDEBAR_VIEWS_PATH for name in SIDEBAR_VIEW_FUNCTIONS},
}

EXPLICIT_WIDGET_KEYS = {
    "'active_monitoring_page'",
    "'chat_entry_area'",
    "'report_calendar_prev'",
    "'report_calendar_next'",
    "'update_categories'",
    "'update_date_range'",
    "'update_selected_range'",
    "'issue_report_control'",
    "'retry_search_engine_warmup'",
    "'sidebar_data_status_bottom'",
    "'unembedded_report_display_limit'",
    "'start_unembedded_embedding_job'",
    "'monitoring_area_group'",
    "'monitoring_operations_area'",
    "'monitoring_experiments_area'",
    "'monitoring_diagnostic_thread'",
    "f\"issue_report_category_{current_thread['id']}\"",
    "f\"issue_report_description_{current_thread['id']}\"",
    "f\"issue_report_response_mode_{current_thread['id']}\"",
    "f\"issue_report_selected_response_{current_thread['id']}\"",
    "f\"issue_report_submit_{current_thread['id']}\"",
    "f\"issue_report_include_remote_comment_{current_thread['id']}\"",
    "f\"issue_report_include_remote_content_{current_thread['id']}\"",
    "f\"issue_report_include_remote_turn_trace_{current_thread['id']}\"",
    "f\"no_result_suggestion_{message.get('id', index)}_{suggestion['label']}\"",
    "f\"toggle_issue_report_{current_thread['id']}\"",
    "f'cancel_thread_{thread_id}'",
    "f'delete_thread_{thread_id}'",
    "f'edit_thread_{thread_id}'",
    "f'pin_thread_{thread_id}'",
    "f'rename_input_{thread_id}'",
    "f'report_calendar_year_select_{current_value}'",
    "f'report_calendar_month_select_{selected_year}_{current_value}'",
    "f'save_thread_{thread_id}'",
    "f'thread_{thread_id}'",
    "f'chat_monitoring_selected_response_{current_id}'",
    "f'chat_monitoring_detail_{current_id}_{selected_message_id}'",
    "f'{key_prefix}_open_pdf_{index}'",
}

SESSION_STATE_KEYS = {
    "active_monitoring_page",
    "chat_job_toasts",
    "current_thread_id",
    "editing_thread_id",
    "issue_report_notice",
    "latest_evaluation_run",
    "latest_parsing_evaluation",
    "pending_scroll_anchor",
    "pending_suggested_query",
    "report_calendar_month",
    "search_engine_queue_was_pending",
    "show_data_update_hint",
    "show_issue_report_form",
}


def _gui_sources() -> list[Path]:
    return sorted(GUI_DIR.glob("*.py"))


def _load_helpers(
    path: Path,
    *names: str,
    extra_namespace: dict[str, object] | None = None,
) -> dict[str, object]:
    """Load pure helper definitions without executing Streamlit rendering code."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    assert {node.name for node in selected} == set(names)

    namespace: dict[str, object] = {
        "WEEKDAY_LABELS": ["월", "화", "수", "목", "금"],
        "calendar": calendar,
        "date": date,
        "datetime": datetime,
        "escape": escape,
        "timedelta": timedelta,
    }
    if extra_namespace:
        namespace.update(extra_namespace)
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def _helper_owner(name: str) -> Path:
    owners = []
    for path in _gui_sources():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
            for node in tree.body
        ):
            owners.append(path)
    assert len(owners) == 1, (name, owners)
    return owners[0]


def test_data_view_pure_helpers_preserve_current_outputs():
    helpers = _load_helpers(
        DATA_VIEWS_PATH,
        "_parse_iso_date",
        "_month_options",
        "_calendar_day_table_cell_html",
        "_report_calendar_table_html",
        "_step_icon",
    )

    parse_iso_date = helpers["_parse_iso_date"]
    month_options = helpers["_month_options"]
    day_cell = helpers["_calendar_day_table_cell_html"]
    table_html = helpers["_report_calendar_table_html"]
    step_icon = helpers["_step_icon"]

    assert parse_iso_date("2026-07-24") == date(2026, 7, 24)
    assert parse_iso_date("") is None
    assert parse_iso_date("2026-02-30") is None
    assert month_options(date(2025, 12, 10), date(2026, 2, 3)) == [
        ("2025-12", "2025년 12월"),
        ("2026-01", "2026년 1월"),
        ("2026-02", "2026년 2월"),
    ]

    populated = day_cell(date(2020, 1, 2), 3, in_month=True)
    assert "2020-01-02: 3건" in populated
    assert ">3</div>" in populated
    assert "&nbsp;" in day_cell(date(2020, 1, 1), 0, in_month=False)

    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(2020, 1)
    rendered_table = table_html(weeks, 1, {"2020-01-02": 3})
    assert all(f">{label}</th>" in rendered_table for label in ["월", "화", "수", "목", "금"])
    assert "2020-01-02: 3건" in rendered_table

    assert step_icon("embed", "download", "running") == "✅"
    assert step_icon("embed", "embed", "running") == "⏳"
    assert step_icon("no_data", "embed", "succeeded") == "⏭️"
    assert step_icon("download", "download", "failed") == "❌"


def test_monitoring_view_pure_helpers_preserve_current_outputs():
    helpers = _load_helpers(
        MONITORING_VIEWS_PATH,
        "_parse_monitoring_paths",
        "_engine_summary_rows",
    )

    assert helpers["_parse_monitoring_paths"]('a.pdf, "b.pdf"\n\nc.pdf') == [
        "a.pdf",
        "b.pdf",
        "c.pdf",
    ]
    assert helpers["_engine_summary_rows"](
        {
            "zeta": {"files": 2, "success": 1},
            "alpha": {"files": 1, "success": 1},
        }
    )[0]["engine"] == "alpha"


def test_chat_monitoring_helpers_prioritize_persisted_execution_evidence():
    helpers = _load_helpers(
        MONITORING_VIEWS_PATH,
        "_chat_execution_label",
        "_chat_target_coverage_label",
        "_chat_grounding_label",
        "_chat_execution_status_label",
        "_chat_scope_caption",
        "_chat_technical_sections",
    )

    execution = {
        "strategy": "company_comparison",
        "execution_mode": "send",
        "requested_target_count": 2,
        "available_target_count": 2,
        "retrieval_concurrency_limit": 2,
        "observed_peak_retrieval_concurrency": 2,
    }
    assert helpers["_chat_execution_label"](execution) == "Send 병렬 실행 (동시성 2)"
    assert helpers["_chat_target_coverage_label"](execution) == "2/2 성공"
    assert helpers["_chat_grounding_label"]("linked") == "연결됨"
    assert helpers["_chat_execution_status_label"]("partial") == "일부 대상 누락"
    assert helpers["_chat_execution_label"](
        {"strategy": "company_comparison"}
    ) == "복수 기업 비교 · 방식 미계측"
    assert helpers["_chat_scope_caption"](
        {
            "scope": {
                "search_filters": {
                    "report_date_start": "2026-08-10",
                    "report_date_end": "2026-08-15",
                    "report_type": "company",
                    "target_names": ["삼성전자", "SK하이닉스"],
                }
            }
        }
    ) == "검색 범위 · 기간 2026-08-10~2026-08-15 · 유형 기업 · 대상 삼성전자, SK하이닉스"
    legacy_sections = helpers["_chat_technical_sections"](
        {
            "timing": {"total_seconds": 25.188},
            "used_chunks": None,
        }
    )
    assert legacy_sections["timing"] == {"total_seconds": 25.188}
    assert legacy_sections["generation_performance"] == {}
    assert legacy_sections["execution"] == {}
    assert legacy_sections["retrieval_k"] == {}
    assert legacy_sections["used_chunks"] == []


def test_monitoring_exposes_cleanup_backlog_in_user_language():
    source = MONITORING_VIEWS_PATH.read_text(encoding="utf-8-sig")

    assert "검색 데이터 정리 대기" in source
    assert "pending_cleanup_file_count" in source


def test_issue_reporting_uses_retry_only_outbox_without_local_file_ui():
    chat_source = CHAT_VIEWS_PATH.read_text(encoding="utf-8-sig")
    monitoring_source = MONITORING_VIEWS_PATH.read_text(encoding="utf-8-sig")

    assert "issue_report_store.build_issue_report(" in chat_source
    assert "issue_report_outbox.queue_report(" in chat_source
    assert "issue_report_store.create_issue_report(" not in chat_source
    assert "issue_report_outbox.queue_saved_report(" not in chat_source
    assert "로컬 신고 파일" not in chat_source
    assert "file_path" not in chat_source
    assert "json_path" not in chat_source
    assert "전체 대화 첨부" not in chat_source
    assert '"experiments": ("evaluation", "parsing")' in monitoring_source


def test_issue_reporting_acknowledges_only_after_durable_queueing():
    chat_source = CHAT_VIEWS_PATH.read_text(encoding="utf-8-sig")

    assert '"message": "신고가 접수되었습니다."' in chat_source
    assert "접수 후 전송과 재시도는 백그라운드에서 처리" in chat_source
    assert "delivery_result = issue_report_outbox.queue_report(" in chat_source
    assert "신고를 제출하지 못했습니다" in chat_source
    assert "전송 실패 시 자동으로 다시 시도합니다" not in chat_source


def test_monitoring_defaults_to_speed_accuracy_and_defers_problem_detail():
    source = MONITORING_VIEWS_PATH.read_text(encoding="utf-8-sig")

    assert '"응답 속도"' in source
    assert '"답변 정확도"' in source
    assert 'key="monitoring_area_group"' in source
    assert 'key="monitoring_operations_area"' in source
    assert 'key="monitoring_experiments_area"' in source
    assert 'key="monitoring_problem_area"' not in source
    assert "accuracy_failure_count" in source
    assert "monitoring.is_verified_native_v2_evaluation_run(latest)" in source
    assert "monitoring.build_native_v2_evaluation_data_source" in source
    assert "if not monitoring.is_native_v2_status(status):" in source
    assert source.index("if not monitoring.is_native_v2_status(status):") < source.index(
        "data_views.render_unembedded_reports(status)"
    )
    assert "global_monitoring_category" not in source
    assert "build_monitoring_tab_labels" not in source


def test_monitoring_groups_horizontal_navigation_by_operator_purpose():
    source = MONITORING_VIEWS_PATH.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(MONITORING_VIEWS_PATH))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_MONITORING_AREA_GROUPS"
            for target in node.targets
        )
    )

    assert ast.literal_eval(assignment.value) == {
        "operations": ("summary", "response", "search_data"),
        "experiments": ("evaluation", "parsing"),
    }
    assert '"operations": "운영 모니터링"' in source
    assert '"experiments": "성능 개선 실험"' in source
    assert source.count("st.segmented_control(") >= 2


def test_monitoring_restores_each_groups_last_selected_area():
    class FakeStreamlit:
        def __init__(self):
            self.session_state = {}

    fake_st = FakeStreamlit()
    helpers = _load_helpers(
        MONITORING_VIEWS_PATH,
        "_restore_monitoring_area_selection",
        "_store_monitoring_area_selection",
        extra_namespace={"st": fake_st},
    )
    restore = helpers["_restore_monitoring_area_selection"]
    store = helpers["_store_monitoring_area_selection"]

    restore(
        "monitoring_operations_area",
        "monitoring_operations_area_selection",
        ("summary", "response", "search_data"),
    )
    fake_st.session_state["monitoring_operations_area"] = "response"
    store(
        "monitoring_operations_area",
        "monitoring_operations_area_selection",
    )
    del fake_st.session_state["monitoring_operations_area"]

    restore(
        "monitoring_experiments_area",
        "monitoring_experiments_area_selection",
        ("evaluation", "parsing", "issues"),
    )
    fake_st.session_state["monitoring_experiments_area"] = "parsing"
    store(
        "monitoring_experiments_area",
        "monitoring_experiments_area_selection",
    )
    del fake_st.session_state["monitoring_experiments_area"]

    restore(
        "monitoring_operations_area",
        "monitoring_operations_area_selection",
        ("summary", "response", "search_data"),
    )

    assert fake_st.session_state["monitoring_operations_area"] == "response"
    assert fake_st.session_state["monitoring_experiments_area_selection"] == "parsing"


def test_current_problem_rows_include_detail_and_next_check():
    source = MONITORING_VIEWS_PATH.read_text(encoding="utf-8-sig")
    function_start = source.index("def _render_global_monitoring(")
    function_end = source.index("\ndef _render_v2_data_diagnostics", function_start)
    function_source = source[function_start:function_end]

    assert '"세부 정보": value.get("detail")' in function_source
    assert '"다음 확인": _V2_CHECK_ACTIONS.get(' in function_source


def test_chat_monitoring_exposes_turn_observability_and_global_keeps_accuracy():
    source = MONITORING_VIEWS_PATH.read_text(encoding="utf-8-sig")
    chat_start = source.index("def render_chat_monitoring_page")
    global_start = source.index("def render_global_monitoring_page")
    chat_source = source[chat_start:global_start]
    global_source = source[global_start:]

    assert "summarize_chat_latency_metrics" in chat_source
    assert '"최근 답변 총시간"' in chat_source
    assert '"현재 대화 평균"' in chat_source
    assert '"RDB 평균 조회시간"' in chat_source
    assert '"Vector DB 평균 검색시간"' in chat_source
    assert "답변 정확도" not in chat_source
    assert "_latest_v2_accuracy_run" not in chat_source
    assert "performance_first=True" in chat_source
    assert "답변별 성능과 근거" in chat_source
    assert "대화 전체 속도 추이" in chat_source
    assert "_render_global_chat_diagnostics" in source
    assert "Send 병렬 실행" in source
    assert "Send 직렬 실행" in source
    assert "선택 답변 총시간" in source
    assert "대상별 근거" in source
    assert "병목 확인" in source
    assert "모델 생성" in source
    assert "입력 토큰" in source
    assert "출력 토큰" in source
    assert "최초 토큰" in source
    assert "실제 provider" in source
    assert "초당 생성 토큰" in source
    assert "대상별 검색" in source
    assert "답변에 사용된 문서" in source
    assert '"technical": "기술 세부정보"' in source
    assert 'diagnostic_view == "technical"' in source
    assert "detail[\"retrieval_k\"]" in source
    performance_start = source.index("def _render_chat_answer_performance")
    performance_end = source.index("\ndef _render_global_chat_diagnostics", performance_start)
    performance_source = source[performance_start:performance_end]
    assert "_chat_technical_sections(detail)" in performance_source
    assert 'technical["generation_performance"]' in performance_source
    assert 'detail["execution"]' not in performance_source
    assert "detail[\"state_status\"]" in source
    assert "detail[\"used_chunks\"]" in source
    assert "detail[\"used_documents\"]" in source
    assert "_render_chat_latency_table" in chat_source
    assert "_render_answer_metrics" in global_source


def test_chat_generation_formatters_keep_missing_values_explicit():
    helpers = _load_helpers(
        MONITORING_VIEWS_PATH,
        "_format_chat_token_count",
        "_format_chat_token_rate",
    )

    assert helpers["_format_chat_token_count"](12345) == "12,345"
    assert helpers["_format_chat_token_count"](None) == "측정 전"
    assert helpers["_format_chat_token_rate"](42.345) == "42.3 tok/s"
    assert helpers["_format_chat_token_rate"](None) == "측정 전"


def test_chat_execution_label_reports_measured_send_concurrency():
    execution_label = _load_helpers(
        MONITORING_VIEWS_PATH,
        "_chat_execution_label",
    )["_chat_execution_label"]

    assert execution_label(
        {
            "strategy": "company_comparison",
            "execution_mode": "send",
            "retrieval_concurrency_limit": 1,
            "observed_peak_retrieval_concurrency": 1,
        }
    ) == "Send 직렬 실행 (동시성 1)"
    assert execution_label(
        {
            "strategy": "company_comparison",
            "execution_mode": "send",
            "retrieval_concurrency_limit": 5,
            "observed_peak_retrieval_concurrency": 3,
        }
    ) == "Send 병렬 실행 (동시성 3)"
    assert execution_label(
        {
            "strategy": "company_comparison",
            "execution_mode": "send",
            "retrieval_concurrency_limit": 5,
        }
    ) == "Send 비교 · 실측 동시성 미계측 (상한 5)"


def test_chat_job_and_view_pure_helpers_preserve_current_outputs():
    anchor_id = _load_helpers(
        _helper_owner("chat_message_anchor_id"),
        "chat_message_anchor_id",
    )["chat_message_anchor_id"]
    latest_scope = _load_helpers(
        _helper_owner("latest_search_scope"),
        "latest_search_scope",
    )["latest_search_scope"]
    has_running_job = _load_helpers(
        _helper_owner("thread_has_running_job"),
        "thread_has_running_job",
    )["thread_has_running_job"]

    assert anchor_id(42, 3) == "chat_message_id_42"
    assert anchor_id(None, 3) == "chat_message_3"

    messages = [
        {
            "role": "assistant",
            "metadata": {
                "status": "succeeded",
                "search_scope": {"file_names": ["older.pdf"]},
            },
        },
        {
            "role": "assistant",
            "metadata": {
                "status": "failed",
                "search_scope": {"file_names": ["failed.pdf"]},
            },
        },
        {
            "role": "assistant",
            "metadata": {
                "status": "succeeded",
                "search_scope": {"file_names": ["latest.pdf"]},
            },
        },
    ]
    assert latest_scope(messages) == {"file_names": ["latest.pdf"]}
    assert not has_running_job(messages)
    messages.append(
        {"role": "assistant", "metadata": {"status": "running"}}
    )
    assert has_running_job(messages)


def test_search_engine_status_copy_explains_background_queue_behavior():
    status_content = _load_helpers(
        CHAT_VIEWS_PATH,
        "_search_engine_status_content",
    )["_search_engine_status_content"]

    assert status_content({"state": "warming"}) == (
        "info",
        "검색 엔진을 준비하고 있습니다. 첫 질문은 한 건까지 바로 입력할 수 있으며, "
        "준비가 끝나면 자동으로 처리됩니다.",
    )
    assert status_content({"state": "ready"}) == (
        "caption",
        "검색 엔진 준비 완료",
    )
    assert status_content({"state": "failed"}) == (
        "error",
        "검색 엔진을 준비하지 못했습니다. 다시 준비한 뒤 질문을 처리할 수 있습니다.",
    )


def test_reference_document_buttons_rerun_only_the_sources_fragment():
    tree = ast.parse(
        CHAT_VIEWS_PATH.read_text(encoding="utf-8-sig"),
        filename=str(CHAT_VIEWS_PATH),
    )
    render_sources = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_render_sources"
    )

    assert [ast.unparse(decorator) for decorator in render_sources.decorator_list] == [
        "st.fragment"
    ]


def test_each_session_reruns_when_the_process_queue_becomes_available():
    class FakeStreamlit:
        def __init__(self):
            self.session_state = {"search_engine_queue_was_pending": True}
            self.fragment_intervals: list[float | None] = []
            self.reruns: list[str | None] = []
            self.messages: list[str] = []

        def fragment(self, *, run_every):
            self.fragment_intervals.append(run_every)
            return lambda function: function

        def info(self, message):
            self.messages.append(message)

        caption = info

        def rerun(self, *, scope=None):
            self.reruns.append(scope)

    class FakeSearchEngine:
        @staticmethod
        def start_search_engine_warmup():
            return {"state": "ready"}

    class FakeChatJobs:
        pending_states = iter((True, False))

        @staticmethod
        def has_pending_search_engine_job():
            return next(FakeChatJobs.pending_states)

    fake_st = FakeStreamlit()
    namespace = _load_helpers(
        CHAT_VIEWS_PATH,
        "render_search_engine_status",
        extra_namespace={
            "st": fake_st,
            "search_engine": FakeSearchEngine(),
            "chat_jobs": FakeChatJobs(),
            "_search_engine_status_content": (
                lambda status: ("info", "검색 엔진 준비 중")
            ),
        },
    )

    namespace["render_search_engine_status"]()

    assert fake_st.session_state["search_engine_queue_was_pending"] is False
    assert fake_st.fragment_intervals == [1.0]
    assert fake_st.reruns == ["app"]


def test_search_status_polling_stops_after_terminal_state_without_changing_fragment_identity():
    class FakeStreamlit:
        def __init__(self):
            self.session_state = {"search_engine_queue_was_pending": False}
            self.fragment_intervals: list[float | None] = []
            self.fragment_names: list[str] = []
            self.reruns: list[str | None] = []
            self.messages: list[str] = []

        def fragment(self, *, run_every):
            self.fragment_intervals.append(run_every)

            def decorate(function):
                self.fragment_names.append(function.__qualname__)
                return function

            return decorate

        def info(self, message):
            self.messages.append(message)

        caption = info

        def rerun(self, *, scope=None):
            self.reruns.append(scope)

    class FakeSearchEngine:
        states = iter(("warming", "ready", "ready", "ready"))

        @staticmethod
        def start_search_engine_warmup():
            return {"state": next(FakeSearchEngine.states)}

    class FakeChatJobs:
        @staticmethod
        def has_pending_search_engine_job():
            return False

    fake_st = FakeStreamlit()
    namespace = _load_helpers(
        CHAT_VIEWS_PATH,
        "render_search_engine_status",
        extra_namespace={
            "st": fake_st,
            "search_engine": FakeSearchEngine(),
            "chat_jobs": FakeChatJobs(),
            "_search_engine_status_content": (
                lambda status: ("info", f"검색 엔진 {status['state']}")
            ),
        },
    )

    namespace["render_search_engine_status"]()
    namespace["render_search_engine_status"]()

    assert fake_st.fragment_intervals == [1.0, None]
    assert fake_st.fragment_names[0] == fake_st.fragment_names[1]
    assert fake_st.reruns == ["app"]


def test_failed_search_status_retry_rebuilds_the_app_to_restart_polling():
    class FakeStreamlit:
        def __init__(self):
            self.session_state = {"search_engine_queue_was_pending": False}
            self.fragment_intervals: list[float | None] = []
            self.reruns: list[str | None] = []

        def fragment(self, *, run_every):
            self.fragment_intervals.append(run_every)
            return lambda function: function

        @staticmethod
        def error(_message):
            return None

        @staticmethod
        def button(*_args, **_kwargs):
            return True

        def rerun(self, *, scope=None):
            self.reruns.append(scope)

    class FakeSearchEngine:
        retry_calls = 0

        @staticmethod
        def start_search_engine_warmup():
            return {"state": "failed"}

        @classmethod
        def retry_search_engine_warmup(cls):
            cls.retry_calls += 1

    class FakeChatJobs:
        @staticmethod
        def has_pending_search_engine_job():
            return False

    fake_st = FakeStreamlit()
    namespace = _load_helpers(
        CHAT_VIEWS_PATH,
        "render_search_engine_status",
        extra_namespace={
            "st": fake_st,
            "search_engine": FakeSearchEngine(),
            "chat_jobs": FakeChatJobs(),
            "_search_engine_status_content": (
                lambda _status: ("error", "검색 엔진 준비 실패")
            ),
        },
    )

    namespace["render_search_engine_status"]()

    assert fake_st.fragment_intervals == [None]
    assert FakeSearchEngine.retry_calls == 1
    assert fake_st.reruns == ["app"]


def test_search_status_fragment_uses_a_stable_region_after_dynamic_chat_history():
    tree = ast.parse(
        CHAT_VIEWS_PATH.read_text(encoding="utf-8-sig"),
        filename=str(CHAT_VIEWS_PATH),
    )
    render_chat = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "render_chat"
    )
    container_lines = {
        target.id: node.lineno
        for node in render_chat.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, ast.Call)
        and ast.unparse(node.value.func) == "st.container"
    }
    history_region = next(
        node
        for node in render_chat.body
        if isinstance(node, ast.With)
        and ast.unparse(node.items[0].context_expr) == "chat_history_region"
    )
    status_region = next(
        node
        for node in render_chat.body
        if isinstance(node, ast.With)
        and ast.unparse(node.items[0].context_expr) == "search_engine_status_region"
    )

    assert container_lines["chat_history_region"] < history_region.lineno
    assert container_lines["search_engine_status_region"] < history_region.lineno
    assert any(
        isinstance(node, ast.Call) and ast.unparse(node.func) == "_render_message"
        for node in ast.walk(history_region)
    )
    assert any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func) == "render_search_engine_status"
        for node in ast.walk(status_region)
    )
    assert history_region.lineno < status_region.lineno


def test_warming_engine_queues_exactly_one_chat_worker():
    class PendingQuestionError(RuntimeError):
        pass

    class FakeUuid:
        values = iter(("job-12345678", "job-87654321"))

        @staticmethod
        def uuid4():
            return next(FakeUuid.values)

    class FakeThread:
        created: list["FakeThread"] = []

        def __init__(self, *, target, kwargs, name, daemon):
            self.target = target
            self.kwargs = kwargs
            self.name = name
            self.daemon = daemon
            self.started = False
            self.__class__.created.append(self)

        def start(self):
            self.started = True

    class FakeThreading:
        Thread = FakeThread

    class FakeSearchEngine:
        @staticmethod
        def get_search_engine_status():
            return {"state": "warming"}

    exchanges: list[tuple] = []
    registry = {
        "running_job_ids": set(),
        "pending_engine_job_ids": set(),
        "events": [],
        "lock": threading.Lock(),
    }
    worker_target = object()
    namespace = _load_helpers(
        CHAT_JOBS_PATH,
        "start_chat_response_job",
        extra_namespace={
            "uuid": FakeUuid,
            "threading": FakeThreading,
            "search_engine": FakeSearchEngine(),
            "append_pending_exchange": (
                lambda *args: exchanges.append(args) or (3, 7)
            ),
            "get_chat_history": lambda thread_id: [("사용자", "이전 질문")],
            "_chat_job_registry": lambda: registry,
            "_run_chat_response_job": worker_target,
            "PendingSearchEngineQuestionError": PendingQuestionError,
        },
    )

    assistant_message_id = namespace["start_chat_response_job"](
        thread_id="thread-1",
        thread_name="테스트 대화",
        user_query="질문",
    )

    assert assistant_message_id == 7
    assert exchanges == [
        (
            "thread-1",
            "질문",
            "검색 엔진을 준비한 뒤 질문을 자동으로 분석합니다. 잠시만 기다려 주세요...",
            {
                "status": "running",
                "job_id": "job-12345678",
                "phase": "waiting_for_engine",
            },
        )
    ]
    assert registry["running_job_ids"] == {"job-12345678"}
    assert registry["pending_engine_job_ids"] == {"job-12345678"}
    assert len(FakeThread.created) == 1
    worker = FakeThread.created[0]
    assert worker.target is worker_target
    assert worker.started is True
    assert worker.daemon is True
    assert worker.name == "chat-response-job-1234"
    assert worker.kwargs["chat_history"] == [("사용자", "이전 질문")]

    render_tree = ast.parse(
        CHAT_VIEWS_PATH.read_text(encoding="utf-8-sig"),
        filename=str(CHAT_VIEWS_PATH),
    )
    queue_calls = [
        node
        for node in ast.walk(render_tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "chat_jobs.start_chat_response_job"
    ]
    assert len(queue_calls) == 1

    try:
        namespace["start_chat_response_job"](
            thread_id="thread-2",
            thread_name="다른 대화",
            user_query="두 번째 질문",
        )
    except RuntimeError as exc:
        assert "one question" in str(exc)
    else:
        raise AssertionError("a second warming-engine question was queued")

    assert len(exchanges) == 1
    assert len(FakeThread.created) == 1
    assert registry["running_job_ids"] == {"job-12345678"}
    assert registry["pending_engine_job_ids"] == {"job-12345678"}

    render_source = CHAT_VIEWS_PATH.read_text(encoding="utf-8-sig")
    assert "chat_jobs.has_pending_search_engine_job()" in render_source
    assert "disabled=chat_input_locked" in render_source
    assert (
        'conversation_store.append_message(current_id, "user", user_query)'
        not in render_source
    )


def test_chat_worker_start_failure_releases_queue_and_marks_message_failed():
    class FakeUuid:
        @staticmethod
        def uuid4():
            return "job-12345678"

    class FailingThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        @staticmethod
        def start():
            raise RuntimeError("thread start failed")

    class FakeThreading:
        Thread = FailingThread

    class FakeSearchEngine:
        @staticmethod
        def get_search_engine_status():
            return {"state": "warming"}

    exchanges: list[tuple] = []
    updates: list[tuple] = []
    events: list[dict] = []
    registry = {
        "running_job_ids": set(),
        "pending_engine_job_ids": set(),
        "events": [],
        "lock": threading.Lock(),
    }
    namespace = _load_helpers(
        CHAT_JOBS_PATH,
        "start_chat_response_job",
        extra_namespace={
            "uuid": FakeUuid,
            "threading": FakeThreading,
            "search_engine": FakeSearchEngine(),
            "append_pending_exchange": (
                lambda *args: exchanges.append(args) or (3, 7)
            ),
            "get_chat_history": lambda thread_id: [],
            "update_message": lambda *args: updates.append(args),
            "_record_chat_job_event": (
                lambda event, target_registry=None: events.append(event)
            ),
            "_chat_job_registry": lambda: registry,
            "_run_chat_response_job": object(),
            "PendingSearchEngineQuestionError": RuntimeError,
        },
    )

    assistant_message_id = namespace["start_chat_response_job"](
        thread_id="thread-1",
        thread_name="테스트 대화",
        user_query="질문",
    )

    assert assistant_message_id == 7
    assert len(exchanges) == 1
    assert updates == [
        (
            7,
            "답변 작업을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.",
            {
                "status": "failed",
                "job_id": "job-12345678",
                "error": "thread start failed",
            },
        )
    ]
    assert events == [
        {
            "status": "failed",
            "thread_id": "thread-1",
            "thread_name": "테스트 대화",
            "assistant_message_id": 7,
            "message": "'테스트 대화' 답변 작업을 시작하지 못했습니다.",
            "engine_queue_released": True,
        }
    ]
    assert registry["running_job_ids"] == set()
    assert registry["pending_engine_job_ids"] == set()


def test_warmup_queue_slot_releases_before_graph_answering():
    class PreparedGraph:
        prepare_calls = 0

        @classmethod
        def prepare(cls):
            cls.prepare_calls += 1

        @staticmethod
        def invoke(*args, **kwargs):
            raise RuntimeError("stop after warmup")

    clock_values = iter([10.0, 10.5])
    updates: list[tuple] = []
    events: list[dict] = []
    registry = {
        "running_job_ids": {"job-1"},
        "pending_engine_job_ids": {"job-1"},
        "events": [],
        "lock": threading.Lock(),
    }
    namespace = _load_helpers(
        _helper_owner("_run_chat_response_job"),
        "_run_chat_response_job",
        extra_namespace={
            "time": type(
                "Clock",
                (),
                {"perf_counter": staticmethod(lambda: next(clock_values))},
            ),
            "graph_app": PreparedGraph(),
            "_invoke_graph_with_timeout": (
                lambda graph_input, *, config, job_id: PreparedGraph.invoke(
                    graph_input,
                    config=config,
                )
            ),
            "update_message": lambda *args: updates.append(args),
            "_record_chat_job_event": (
                lambda event, target_registry=None: events.append(event)
            ),
        },
    )

    namespace["_run_chat_response_job"](
        job_id="job-1",
        thread_id="thread-1",
        thread_name="테스트 대화",
        assistant_message_id=7,
        user_query="질문",
        chat_history=[],
        prior_search_scope=None,
        registry=registry,
        queued_while_warming=True,
    )

    assert PreparedGraph.prepare_calls == 1
    assert updates[0][2]["phase"] == "answering"
    assert updates[1][2]["status"] == "failed"
    assert registry["events"] == [{
        "status": "progress",
        "thread_id": "thread-1",
        "thread_name": "테스트 대화",
        "assistant_message_id": 7,
        "engine_queue_released": True,
    }]
    assert events[0]["status"] == "failed"
    assert "engine_queue_released" not in events[0]
    assert registry["pending_engine_job_ids"] == set()
    assert registry["running_job_ids"] == set()


def test_timed_out_graph_invocation_does_not_serialize_later_questions():
    first_invocation_started = threading.Event()
    release_first_invocation = threading.Event()

    class ConcurrentGraph:
        @staticmethod
        def invoke(graph_input, *, config):
            if graph_input["question"] == "stuck":
                first_invocation_started.set()
                release_first_invocation.wait(timeout=2)
                return {"generation": "late"}
            return {
                "generation": "fast",
                "checkpoint_ns": config["configurable"]["checkpoint_ns"],
            }

    namespace = _load_helpers(
        CHAT_JOBS_PATH,
        "_invoke_graph_with_timeout",
        extra_namespace={
            "queue": queue,
            "threading": threading,
            "graph_app": ConcurrentGraph(),
            "ChatResponseTimeout": TimeoutError,
            "CHAT_RESPONSE_TIMEOUT_SECONDS": 180.0,
        },
    )
    invoke_with_timeout = namespace["_invoke_graph_with_timeout"]
    outcomes: dict[str, object] = {}

    def run_stuck_invocation():
        try:
            invoke_with_timeout(
                {"question": "stuck"},
                config={
                    "configurable": {
                        "thread_id": "thread-1",
                        "checkpoint_ns": "job-1",
                    }
                },
                job_id="job-1",
                timeout_seconds=0.05,
            )
        except Exception as exc:
            outcomes["stuck"] = exc

    first_job = threading.Thread(target=run_stuck_invocation, daemon=True)
    first_job.start()
    assert first_invocation_started.wait(timeout=1)

    fast_result = invoke_with_timeout(
        {"question": "fast"},
        config={
            "configurable": {
                "thread_id": "thread-2",
                "checkpoint_ns": "job-2",
            }
        },
        job_id="job-2",
        timeout_seconds=0.5,
    )
    first_job.join(timeout=1)
    release_first_invocation.set()

    assert fast_result == {"generation": "fast", "checkpoint_ns": "job-2"}
    assert isinstance(outcomes.get("stuck"), TimeoutError)

    run_source = CHAT_JOBS_PATH.read_text(encoding="utf-8-sig")
    assert 'registry["graph_lock"]' not in run_source
    assert "_invoke_graph_with_timeout(" in run_source


def test_warmup_queue_release_survives_progress_update_failure():
    class PreparedGraph:
        @staticmethod
        def prepare():
            return None

        @staticmethod
        def invoke(*args, **kwargs):
            raise AssertionError("graph must not run after the update failure")

    clock_values = iter([10.0, 10.5])
    update_calls = 0
    events: list[dict] = []
    registry = {
        "running_job_ids": {"job-1"},
        "pending_engine_job_ids": {"job-1"},
        "events": [],
        "lock": threading.Lock(),
    }

    def update_message(*args):
        nonlocal update_calls
        update_calls += 1
        if update_calls == 1:
            raise RuntimeError("progress write failed")

    namespace = _load_helpers(
        _helper_owner("_run_chat_response_job"),
        "_run_chat_response_job",
        extra_namespace={
            "time": type(
                "Clock",
                (),
                {"perf_counter": staticmethod(lambda: next(clock_values))},
            ),
            "graph_app": PreparedGraph(),
            "_invoke_graph_with_timeout": (
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError("graph must not run after the update failure")
                )
            ),
            "update_message": update_message,
            "_record_chat_job_event": (
                lambda event, target_registry=None: events.append(event)
            ),
        },
    )

    namespace["_run_chat_response_job"](
        job_id="job-1",
        thread_id="thread-1",
        thread_name="테스트 대화",
        assistant_message_id=7,
        user_query="질문",
        chat_history=[],
        prior_search_scope=None,
        registry=registry,
        queued_while_warming=True,
    )

    assert update_calls == 2
    assert any(
        event.get("engine_queue_released")
        for event in registry["events"]
    )
    assert events[-1]["status"] == "failed"
    assert registry["pending_engine_job_ids"] == set()
    assert registry["running_job_ids"] == set()


def test_chat_job_failure_is_persisted_and_unlocks_the_job():
    class FailingGraph:
        @staticmethod
        def invoke(*args, **kwargs):
            raise RuntimeError("graph failed")

    clock_values = iter([10.0, 12.3456])
    updates: list[tuple] = []
    events: list[dict] = []
    registry = {
        "running_job_ids": {"job-1"},
        "events": [],
        "lock": threading.Lock(),
    }
    namespace = _load_helpers(
        _helper_owner("_run_chat_response_job"),
        "_run_chat_response_job",
        extra_namespace={
            "time": type(
                "Clock",
                (),
                {"perf_counter": staticmethod(lambda: next(clock_values))},
            ),
            "graph_app": FailingGraph(),
            "_invoke_graph_with_timeout": (
                lambda graph_input, *, config, job_id: FailingGraph.invoke(
                    graph_input,
                    config=config,
                )
            ),
            "update_message": lambda *args: updates.append(args),
            "_record_chat_job_event": (
                lambda event, target_registry=None: events.append(event)
            ),
        },
    )

    namespace["_run_chat_response_job"](
        job_id="job-1",
        thread_id="thread-1",
        thread_name="테스트 대화",
        assistant_message_id=7,
        user_query="질문",
        chat_history=[],
        prior_search_scope=None,
        registry=registry,
    )

    assert updates == [
        (
            7,
            "답변을 생성하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            {
                "status": "failed",
                "job_id": "job-1",
                "error": "graph failed",
                "latency_seconds": 2.346,
            },
        )
    ]
    assert events == [
        {
            "status": "failed",
            "thread_id": "thread-1",
            "thread_name": "테스트 대화",
            "assistant_message_id": 7,
            "message": "'테스트 대화' 답변 생성에 실패했습니다.",
        }
    ]
    assert registry["running_job_ids"] == set()


def test_chat_job_success_persists_scope_monitoring_and_event():
    from src.core.monitoring import compact_graph_monitoring_metadata

    class SuccessfulGraph:
        calls: list[tuple] = []

        @staticmethod
        def runtime_provenance():
            return {
                "mode": "native",
                "active_snapshot_id": "snapshot-v2",
                "active_build_id": "build-v2",
                "publication_generation": 3,
                "write_epoch": 2,
                "degraded": False,
            }

        @classmethod
        def invoke(cls, graph_input, *, config):
            cls.calls.append((graph_input, config))
            return {
                "generation": "완료된 답변",
                "route": "vectordb",
                "rerank_info": [
                    {
                        "file_name": "report.pdf",
                        "rank": 1,
                        "chunk_uid": "chunk-1",
                        "report_uid": "report-1",
                    }
                ],
                "search_filters": {"target_name": "테스트 기업"},
                "temporal_context": {"description": "2026-07"},
                "scope_source": "current_question",
                "no_vector_results": False,
                "monitoring_metrics": {
                    "retrieval": {
                        "search_top_k": 20,
                        "requested_k": 160,
                        "fetch_k": 48,
                        "selected_source_count": 1,
                    }
                },
            }

    clock_values = iter([10.0, 11.25])
    updates: list[tuple] = []
    events: list[dict] = []
    registry = {
        "running_job_ids": {"job-1"},
        "events": [],
        "lock": threading.Lock(),
    }
    namespace = _load_helpers(
        _helper_owner("_run_chat_response_job"),
        "_search_scope_from_graph_state",
        "_run_chat_response_job",
        extra_namespace={
            "time": type(
                "Clock",
                (),
                {"perf_counter": staticmethod(lambda: next(clock_values))},
            ),
            "graph_app": SuccessfulGraph(),
            "_invoke_graph_with_timeout": (
                lambda graph_input, *, config, job_id: SuccessfulGraph.invoke(
                    graph_input,
                    config=config,
                )
            ),
            "build_answer_scope_index": (
                lambda scope, rerank_info: {
                    "file_names": list(scope.get("file_names") or []),
                    "source_count": len(rerank_info),
                }
            ),
            "build_scope_notice": lambda final_state: "검색 범위 안내",
            "compact_graph_monitoring_metadata": compact_graph_monitoring_metadata,
            "update_message": lambda *args: updates.append(args),
            "_record_chat_job_event": (
                lambda event, target_registry=None: events.append(event)
            ),
        },
    )

    namespace["_run_chat_response_job"](
        job_id="job-1",
        thread_id="thread-1",
        thread_name="테스트 대화",
        assistant_message_id=7,
        user_query="질문",
        chat_history=[("사용자", "이전 질문"), ("AI", "이전 답변")],
        prior_search_scope={"file_names": ["prior.pdf"]},
        registry=registry,
    )

    assert SuccessfulGraph.calls == [
        (
            {
                "question": "질문",
                "chat_history": [("사용자", "이전 질문"), ("AI", "이전 답변")],
                "prior_search_scope": {"file_names": ["prior.pdf"]},
            },
            {
                "configurable": {
                    "thread_id": "thread-1",
                    "checkpoint_ns": "job-1",
                }
            },
        )
    ]
    assert len(updates) == 1
    message_id, answer, persisted = updates[0]
    assert message_id == 7
    assert answer == "완료된 답변"
    assert persisted["status"] == "succeeded"
    assert persisted["route"] == "vectordb"
    assert persisted["latency_seconds"] == 1.25
    assert persisted["selected_sources"][0]["chunk_uid"] == "chunk-1"
    assert persisted["selected_sources"][0]["report_uid"] == "report-1"
    assert persisted["monitoring"]["timing"]["total_seconds"] == 1.25
    assert persisted["monitoring"]["retrieval"]["search_top_k"] == 20
    assert persisted["monitoring"]["retrieval"]["requested_k"] == 160
    assert persisted["monitoring"]["retrieval"]["fetch_k"] == 48
    assert persisted["monitoring"]["state_snapshot"]["route"] == "vectordb"
    assert persisted["search_scope"]["file_names"] == ["report.pdf"]
    assert persisted["scope_notice"] == "검색 범위 안내"
    assert events == [
        {
            "status": "succeeded",
            "thread_id": "thread-1",
            "thread_name": "테스트 대화",
            "assistant_message_id": 7,
            "message": "'테스트 대화' 답변이 완료되었습니다.",
        }
    ]
    assert registry["running_job_ids"] == set()


def test_chat_job_events_are_drained_once_and_repair_uses_active_ids():
    registry = {
        "running_job_ids": {"job-1", "job-2"},
        "events": [{"status": "succeeded", "thread_id": "thread-1"}],
        "lock": threading.Lock(),
    }
    repair_calls: list[set[str]] = []
    namespace = _load_helpers(
        _helper_owner("consume_chat_job_events"),
        "consume_chat_job_events",
        "repair_interrupted_chat_jobs",
        extra_namespace={
            "_chat_job_registry": lambda: registry,
            "mark_interrupted_running_messages_failed": (
                lambda *, active_job_ids: repair_calls.append(active_job_ids) or 2
            ),
        },
    )

    assert namespace["consume_chat_job_events"]() == [
        {"status": "succeeded", "thread_id": "thread-1"}
    ]
    assert namespace["consume_chat_job_events"]() == []
    assert namespace["repair_interrupted_chat_jobs"]() == 2
    assert repair_calls == [{"job-1", "job-2"}]


def test_current_thread_completion_event_sets_anchor_and_requests_app_rerun():
    class SessionState:
        pending_scroll_anchor: str | None = None

    class FakeStreamlit:
        def __init__(self):
            self.session_state = SessionState()
            self.reruns: list[str | None] = []

        @staticmethod
        def fragment(*, run_every):
            return lambda function: function

        def rerun(self, *, scope=None):
            self.reruns.append(scope)

    fake_st = FakeStreamlit()
    queued_events: list[dict] = []
    show_calls: list[bool] = []
    events = [
        {
            "thread_id": "thread-1",
            "assistant_message_id": 7,
            "status": "progress",
        },
        {
            "thread_id": "thread-2",
            "assistant_message_id": 8,
            "status": "succeeded",
        },
    ]
    namespace = _load_helpers(
        _helper_owner("render_chat_job_notifications"),
        "render_chat_job_notifications",
        extra_namespace={
            "st": fake_st,
            "consume_chat_job_events": lambda: events,
            "_queue_chat_job_toast": queued_events.append,
            "chat_message_anchor_id": (
                lambda message_id, fallback_index: f"chat_message_id_{message_id}"
            ),
            "show_queued_chat_job_toasts": lambda: show_calls.append(True),
        },
    )

    namespace["render_chat_job_notifications"]("thread-1")

    assert queued_events == [events[1]]
    assert fake_st.session_state.pending_scroll_anchor == "chat_message_id_7"
    assert fake_st.reruns == ["app"]
    assert show_calls == [True]


def test_engine_queue_release_reruns_app_from_another_conversation():
    class SessionState:
        pending_scroll_anchor: str | None = None

    class FakeStreamlit:
        def __init__(self):
            self.session_state = SessionState()
            self.reruns: list[str | None] = []

        @staticmethod
        def fragment(*, run_every):
            return lambda function: function

        def rerun(self, *, scope=None):
            self.reruns.append(scope)

    fake_st = FakeStreamlit()
    namespace = _load_helpers(
        _helper_owner("render_chat_job_notifications"),
        "render_chat_job_notifications",
        extra_namespace={
            "st": fake_st,
            "consume_chat_job_events": lambda: [
                {
                    "thread_id": "thread-1",
                    "assistant_message_id": 7,
                    "status": "progress",
                    "engine_queue_released": True,
                }
            ],
            "_queue_chat_job_toast": lambda event: None,
            "chat_message_anchor_id": (
                lambda message_id, fallback_index: f"chat_message_id_{message_id}"
            ),
            "show_queued_chat_job_toasts": lambda: None,
        },
    )

    namespace["render_chat_job_notifications"]("thread-2")

    assert fake_st.session_state.pending_scroll_anchor is None
    assert fake_st.reruns == ["app"]


def test_initial_render_defers_search_graph_imports_to_search_engine_loader():
    forbidden_modules = {
        "src.graphs",
        "src.graphs.main_graph",
        "src.nodes",
        "src.nodes.query_rewrite",
        "src.nodes.search_scope",
        "src.nodes.vectordb",
    }
    for path in (APP_PATH, CHAT_JOBS_PATH, MONITORING_VIEWS_PATH):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert imported_modules.isdisjoint(forbidden_modules), path

    app_source = APP_PATH.read_text(encoding="utf-8-sig")
    for eager_name in (
        "main_graph_module",
        "query_rewrite_module",
        "search_scope_module",
        "vectordb_module",
    ):
        assert eager_name not in app_source

    loader_source = SEARCH_ENGINE_PATH.read_text(encoding="utf-8-sig")
    assert 'importlib.import_module("src.graphs.main_graph")' in loader_source
    assert loader_source.index("reconcile_and_inspect_runtime") < loader_source.index(
        'importlib.import_module("src.graphs.main_graph")'
    )


def test_initial_status_import_does_not_load_native_vector_runtime():
    data_update_source = Path("src/core/data_update_jobs.py").read_text(
        encoding="utf-8-sig"
    )
    bootstrap_source = Path("src/retrieval/bootstrap.py").read_text(
        encoding="utf-8-sig"
    )
    status_source = Path("src/core/status.py").read_text(encoding="utf-8-sig")

    assert "\nfrom src.retrieval.runtime_guard import" not in data_update_source
    assert "\nfrom src.retrieval.vector_index import" not in bootstrap_source
    assert "inspect_runtime(" in status_source
    assert "validate_snapshot=False" in status_source

    for path in _gui_sources():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) == "st.fragment"
            ):
                continue
            run_every = next(
                keyword.value
                for keyword in node.keywords
                if keyword.arg == "run_every"
            )
            if isinstance(run_every, ast.Constant):
                assert isinstance(run_every.value, (int, float))
                continue
            assert isinstance(run_every, ast.IfExp)
            assert isinstance(run_every.body, ast.Constant)
            assert isinstance(run_every.body.value, (int, float))
            assert isinstance(run_every.orelse, ast.Constant)
            assert run_every.orelse.value is None


def test_gui_widget_and_session_keys_remain_stable_across_module_moves():
    trees = [
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for path in _gui_sources()
    ]
    widget_keys = {
        ast.unparse(keyword.value)
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "key" and not isinstance(keyword.value, ast.Lambda)
    }
    assert widget_keys == EXPLICIT_WIDGET_KEYS

    combined_source = "\n".join(
        path.read_text(encoding="utf-8-sig") for path in _gui_sources()
    )
    for key in SESSION_STATE_KEYS:
        assert f'"{key}"' in combined_source or f".{key}" in combined_source


def test_each_extracted_view_function_has_one_owner():
    owners: dict[str, list[Path]] = {}
    for path in _gui_sources():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owners.setdefault(node.name, []).append(path)

    for function_name, expected_path in EXPECTED_VIEW_OWNERS.items():
        assert owners.get(function_name) == [expected_path], function_name


def test_unembedded_report_view_contains_a_sqlite_error_boundary():
    source = DATA_VIEWS_PATH.read_text(encoding="utf-8-sig")

    assert "except (OSError, sqlite3.Error, ValueError) as exc:" in source
    assert "미임베딩 문서 목록을 읽지 못했습니다." in source


def test_app_imports_or_reloads_gui_modules_once_per_run():
    app_source = APP_PATH.read_text(encoding="utf-8-sig")

    for module_name in (
        "chat_jobs",
        "chat_views",
        "data_views",
        "monitoring_views",
        "sidebar_views",
    ):
        assert (
            f'{module_name} = _import_or_reload("apps.gui.{module_name}")'
            in app_source
        )

    assert (
        'search_engine = importlib.import_module("apps.gui.search_engine")'
        in app_source
    )
    assert 'search_engine = _import_or_reload("apps.gui.search_engine")' not in app_source


def test_app_reloads_chat_ui_helpers_before_importing_chat_views():
    app_source = APP_PATH.read_text(encoding="utf-8-sig")
    app_tree = ast.parse(app_source, filename=str(APP_PATH))
    reload_function = next(
        node
        for node in app_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_reload_loaded_application_modules"
    )
    reloaded_modules = {
        node.value
        for node in ast.walk(reload_function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "src.core.chat_ui_helpers" in reloaded_modules


def test_app_only_composes_extracted_views_and_leaf_modules_do_not_import_app():
    app_source = APP_PATH.read_text(encoding="utf-8-sig")
    app_tree = ast.parse(app_source, filename=str(APP_PATH))
    app_calls = {
        ast.unparse(node.func)
        for node in ast.walk(app_tree)
        if isinstance(node, ast.Call)
    }
    assert {
        "chat_jobs.render_chat_job_notifications",
        "chat_jobs.show_queued_chat_job_toasts",
        "chat_views.render_chat",
        "monitoring_views.render_chat_monitoring_page",
        "monitoring_views.render_global_monitoring_page",
        "sidebar_views.ensure_current_thread",
        "sidebar_views.load_threads",
        "sidebar_views.render_sidebar",
    } <= app_calls
    for module_name in (
        "chat_jobs",
        "chat_views",
        "data_views",
        "monitoring_views",
        "sidebar_views",
    ):
        assert (
            f'{module_name} = _import_or_reload("apps.gui.{module_name}")'
            in app_source
        )

    sidebar_tree = ast.parse(
        SIDEBAR_VIEWS_PATH.read_text(encoding="utf-8-sig"),
        filename=str(SIDEBAR_VIEWS_PATH),
    )
    sidebar_calls = {
        ast.unparse(node.func)
        for node in ast.walk(sidebar_tree)
        if isinstance(node, ast.Call)
    }
    assert {
        "data_views.render_data_update_controls",
        "data_views.render_report_calendar",
    } <= sidebar_calls

    for path in (
        CHAT_JOBS_PATH,
        CHAT_VIEWS_PATH,
        DATA_VIEWS_PATH,
        MONITORING_VIEWS_PATH,
        SIDEBAR_VIEWS_PATH,
    ):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "apps.gui.app" not in imported_modules

        top_level_calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]
        assert not top_level_calls
