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
    "chat_message_anchor_id",
    "_run_chat_response_job",
    "start_chat_response_job",
    "render_chat_job_notifications",
}

CHAT_VIEW_FUNCTIONS = {
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
    "_render_experiment_monitoring",
    "_render_global_monitoring",
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
    "'issue_report_control'",
    "'sidebar_data_status_bottom'",
    "'unembedded_report_display_limit'",
    "'unembedded_embedding_limit'",
    "'start_unembedded_embedding_job'",
    "'global_monitoring_category'",
    "f\"issue_report_category_{current_thread['id']}\"",
    "f\"issue_report_description_{current_thread['id']}\"",
    "f\"issue_report_include_context_{current_thread['id']}\"",
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
            "오류가 발생했습니다: graph failed",
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


def test_app_reimports_gui_modules_before_reloading_them():
    app_source = APP_PATH.read_text(encoding="utf-8-sig")

    for module_name in (
        "chat_jobs",
        "chat_views",
        "data_views",
        "monitoring_views",
        "sidebar_views",
    ):
        import_statement = (
            f'{module_name} = importlib.import_module("apps.gui.{module_name}")'
        )
        reload_statement = f"{module_name} = importlib.reload({module_name})"

        assert import_statement in app_source
        assert app_source.index(import_statement) < app_source.index(reload_statement)


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
        "compare_pdf_extractors",
        "data_views",
        "monitoring_views",
        "sidebar_views",
    ):
        assert f"{module_name} = importlib.reload({module_name})" in app_source

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
