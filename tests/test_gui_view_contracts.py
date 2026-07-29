"""Behavior and state contracts for moving Streamlit views between modules."""

from __future__ import annotations

import ast
import calendar
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
    "_is_update_job_active",
    "_step_icon",
    "_render_update_steps",
    "render_update_progress",
    "_iter_weekdays",
    "_default_update_range",
    "_update_min_date",
    "render_data_update_controls",
    "render_unembedded_reports",
}

MONITORING_VIEW_FUNCTIONS = {
    "_dimension_rows",
    "_case_type_rows",
    "_parse_monitoring_paths",
    "_engine_summary_rows",
    "_render_parsing_engine_evaluation",
    "_all_thread_messages",
    "_latest_saved_evaluation_run",
    "_run_fixed_snapshot_evaluation",
    "_fixed_snapshot_assets_present",
    "_run_candidate_snapshot_evaluation",
    "_render_experiment_monitoring",
    "_render_global_monitoring",
    "_rerun_candidate_action",
    "_write_and_record_candidate_handoff",
    "_run_and_record_candidate_snapshot",
    "_render_candidate_lifecycle",
    "_render_issue_report_monitoring",
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
    "'email_issue_report_import_text'",
    "'feedback_loop_candidate_selector'",
    "'issue_report_control'",
    "'retry_search_engine_warmup'",
    "'sidebar_data_status_bottom'",
    "'unembedded_report_display_limit'",
    "'unembedded_embedding_limit'",
    "'start_unembedded_embedding_job'",
    "'global_monitoring_category'",
    "f\"issue_report_category_{current_thread['id']}\"",
    "f\"issue_report_description_{current_thread['id']}\"",
    "f\"issue_report_response_mode_{current_thread['id']}\"",
    "f\"issue_report_selected_response_{current_thread['id']}\"",
    "f\"issue_report_submit_{current_thread['id']}\"",
    "f\"issue_report_include_context_{current_thread['id']}\"",
    "f\"candidate_approve_{candidate['id']}\"",
    "f\"candidate_attach_handoff_{item['handoff_id']}\"",
    "f\"candidate_attach_run_{run['run_id']}\"",
    "f\"candidate_close_action_{candidate['id']}\"",
    "f\"candidate_duplicate_reason_{candidate['id']}\"",
    "f\"candidate_duplicate_{candidate['id']}\"",
    "f\"candidate_edit_contract_reason_{candidate['id']}\"",
    "f\"candidate_edit_contract_{candidate['id']}\"",
    "f\"candidate_fixing_{candidate['id']}\"",
    "f\"candidate_handoff_baseline_{candidate['id']}\"",
    "f\"candidate_handoff_confirm_{candidate['id']}\"",
    "f\"candidate_handoff_reason_{candidate['id']}\"",
    "f\"candidate_handoff_save_{candidate['id']}\"",
    "f\"candidate_latency_threshold_{status}_{candidate['id']}\"",
    "f\"candidate_manual_check_{status}_{candidate['id']}_{assertion_id}\"",
    "f\"candidate_manual_confirm_{status}_{candidate['id']}\"",
    "f\"candidate_manual_note_{status}_{candidate['id']}\"",
    "f\"candidate_manual_reason_{status}_{candidate['id']}\"",
    "f\"candidate_record_manual_{status}_{candidate['id']}\"",
    "f\"candidate_mark_reproduced_{candidate['id']}\"",
    "f\"candidate_mark_triaged_{candidate['id']}\"",
    "f\"candidate_mark_verified_{candidate['id']}\"",
    "f\"candidate_needs_expectation_{candidate['id']}\"",
    "f\"candidate_not_reproduced_reason_{candidate['id']}\"",
    "f\"candidate_not_reproducible_{candidate['id']}\"",
    "f\"candidate_ready_{candidate['id']}\"",
    "f\"candidate_rejection_reason_{candidate['id']}\"",
    "f\"candidate_reject_{candidate['id']}\"",
    "f\"candidate_reopen_reason_{candidate['id']}\"",
    "f\"candidate_reopen_{candidate['id']}\"",
    "f\"candidate_repair_handoff_{item['handoff_id']}\"",
    "f\"candidate_run_{run_kind}_{candidate['id']}\"",
    "f\"candidate_snapshot_confirm_{status}_{candidate['id']}\"",
    "f\"no_result_suggestion_{message.get('id', index)}_{suggestion['label']}\"",
    "f\"toggle_issue_report_{current_thread['id']}\"",
    "f'cancel_thread_{thread_id}'",
    "f'delete_thread_{thread_id}'",
    "f'edit_thread_{thread_id}'",
    "f'pin_thread_{thread_id}'",
    "f'rename_input_{thread_id}'",
    "f'repair_issue_report_text_{warning_index}'",
    "f'report_calendar_year_select_{current_value}'",
    "f'report_calendar_month_select_{selected_year}_{current_value}'",
    "f'save_thread_{thread_id}'",
    "f'thread_{thread_id}'",
    "f'chat_monitoring_selected_response_{current_id}'",
    "f'{key_prefix}_open_pdf_{index}'",
}

SESSION_STATE_KEYS = {
    "active_monitoring_page",
    "chat_job_toasts",
    "current_thread_id",
    "editing_thread_id",
    "issue_report_success",
    "latest_evaluation_run",
    "latest_parsing_evaluation",
    "latest_regression_candidate_run",
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
        "_dimension_rows",
        "_case_type_rows",
        "_parse_monitoring_paths",
        "_engine_summary_rows",
    )

    assert helpers["_dimension_rows"](
        {"monitoring_dimensions": {"routing": 1, "retrieval": 3, "citation": 3}}
    ) == [
        {"dimension": "citation", "case_count": 3},
        {"dimension": "retrieval", "case_count": 3},
        {"dimension": "routing", "case_count": 1},
    ]
    assert helpers["_case_type_rows"]({"case_types": {"vector": 2, "rdb": 1}}) == [
        {"case_type": "rdb", "case_count": 1},
        {"case_type": "vector", "case_count": 2},
    ]
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


def test_each_session_reruns_when_the_process_queue_becomes_available():
    class FakeStreamlit:
        def __init__(self):
            self.session_state = {"search_engine_queue_was_pending": True}
            self.reruns: list[str | None] = []
            self.messages: list[str] = []

        @staticmethod
        def fragment(*, run_every):
            return lambda function: function

        def info(self, message):
            self.messages.append(message)

        def rerun(self, *, scope=None):
            self.reruns.append(scope)

    class FakeSearchEngine:
        @staticmethod
        def start_search_engine_warmup():
            return {"state": "warming"}

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
                lambda status: ("info", "검색 엔진 준비 중")
            ),
        },
    )

    namespace["render_search_engine_status"]()

    assert fake_st.session_state["search_engine_queue_was_pending"] is False
    assert fake_st.reruns == ["app"]


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
        "graph_lock": threading.Lock(),
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
        "graph_lock": threading.Lock(),
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
        "graph_lock": threading.Lock(),
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
        "graph_lock": threading.Lock(),
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
        "graph_lock": threading.Lock(),
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
    class SuccessfulGraph:
        calls: list[tuple] = []

        @classmethod
        def invoke(cls, graph_input, *, config):
            cls.calls.append((graph_input, config))
            return {
                "generation": "완료된 답변",
                "route": "vectordb",
                "rerank_info": [{"file_name": "report.pdf", "rank": 1}],
                "search_filters": {"target_name": "테스트 기업"},
                "temporal_context": {"description": "2026-07"},
                "scope_source": "current_question",
                "no_vector_results": False,
            }

    clock_values = iter([10.0, 11.25])
    updates: list[tuple] = []
    events: list[dict] = []
    registry = {
        "running_job_ids": {"job-1"},
        "events": [],
        "lock": threading.Lock(),
        "graph_lock": threading.Lock(),
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
            "build_answer_scope_index": (
                lambda scope, rerank_info: {
                    "file_names": list(scope.get("file_names") or []),
                    "source_count": len(rerank_info),
                }
            ),
            "build_scope_notice": lambda final_state: "검색 범위 안내",
            "compact_graph_monitoring_metadata": (
                lambda **kwargs: {
                    "route": kwargs["final_state"]["route"],
                    "latency_seconds": kwargs["latency_seconds"],
                }
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
        prior_search_scope={"file_names": ["prior.pdf"]},
        registry=registry,
    )

    assert SuccessfulGraph.calls == [
        (
            {
                "question": "질문",
                "prior_search_scope": {"file_names": ["prior.pdf"]},
            },
            {"configurable": {"thread_id": "thread-1"}},
        )
    ]
    assert updates == [
        (
            7,
            "완료된 답변",
            {
                "status": "succeeded",
                "job_id": "job-1",
                "question": "질문",
                "no_vector_results": False,
                "selected_sources": [{"file_name": "report.pdf", "rank": 1}],
                "route": "vectordb",
                "latency_seconds": 1.25,
                "search_scope": {
                    "route": "vectordb",
                    "search_filters": {"target_name": "테스트 기업"},
                    "temporal_context": {"description": "2026-07"},
                    "scope_source": "current_question",
                    "file_names": ["report.pdf"],
                    "answer_scope_index": {
                        "file_names": ["report.pdf"],
                        "source_count": 1,
                    },
                },
                "scope_notice": "검색 범위 안내",
            },
        )
    ]
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
        "graph_lock": threading.Lock(),
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
            assert isinstance(run_every, ast.Constant)
            assert isinstance(run_every.value, (int, float))


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


class TestSliceACandidateUi:
    """Executable gate for the Slice A operator workflow."""

    test_widget_keys_are_explicit = staticmethod(
        test_gui_widget_and_session_keys_remain_stable_across_module_moves
    )
    test_candidate_view_functions_have_one_owner = staticmethod(
        test_each_extracted_view_function_has_one_owner
    )

    def test_candidate_lifecycle_exposes_approved_evidence_controls(self):
        source = MONITORING_VIEWS_PATH.read_text(encoding="utf-8-sig")

        assert "run_candidate_evaluation_snapshot.py" in source
        assert "monitoring.record_candidate_run(" in source
        assert "monitoring.record_candidate_manual_evidence(" in source
        assert "form_record_revision" in source
        assert "form_revision_key=form_revision_key" in source
        assert '"triaged", "needs_expectation"' in source
        assert "candidate_triage_followup_" in source
        assert "추가 정보 반영 후 후보 재분류" in source
        assert 'to_status="reproduced"' in source
        assert 'to_status="not_reproducible"' in source
        assert 'to_status="verified"' in source
        assert 'to_status="rejected"' in source
        assert 'to_status="duplicate"' in source

    def test_candidate_form_conflict_clears_loaded_revision_without_retry(self):
        class CandidateConflictError(RuntimeError):
            pass

        class FakeMonitoring:
            pass

        FakeMonitoring.CandidateConflictError = CandidateConflictError

        class FakeStreamlit:
            def __init__(self):
                self.session_state = {"loaded_revision": 7}
                self.errors: list[str] = []
                self.rerun_count = 0

            def error(self, message):
                self.errors.append(message)

            def rerun(self):
                self.rerun_count += 1

        fake_st = FakeStreamlit()
        rerun_action = _load_helpers(
            MONITORING_VIEWS_PATH,
            "_rerun_candidate_action",
            extra_namespace={
                "st": fake_st,
                "monitoring": FakeMonitoring,
            },
        )["_rerun_candidate_action"]
        calls = 0

        def conflicting_action():
            nonlocal calls
            calls += 1
            raise CandidateConflictError("stale")

        rerun_action(
            conflicting_action,
            form_revision_key="loaded_revision",
        )

        assert calls == 1
        assert "loaded_revision" not in fake_st.session_state
        assert fake_st.rerun_count == 0
        assert len(fake_st.errors) == 1
        assert "다른 변경이 먼저 저장되었습니다" in fake_st.errors[0]
