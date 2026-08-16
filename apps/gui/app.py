import os
import sys
import importlib
import json

REPOSITORY_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, REPOSITORY_ROOT)
_RUNTIME_SMOKE_REQUESTED = (
    __name__ == "__main__" and sys.argv[1:] == ["--runtime-smoke"]
)


def _finish_runtime_smoke(selection) -> None:
    print(
        json.dumps(
            {
                "status": "ok",
                "surface": "gui",
                "mode": selection.mode,
                "active_snapshot_id": selection.active_snapshot_id,
                "publication_generation": selection.publication_generation,
                "write_epoch": selection.write_epoch,
                "degraded": selection.degraded,
                "write_enabled": selection.write_enabled,
                "initialization_state": selection.initialization_state,
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0)


def _import_or_reload(module_name: str):
    if module := sys.modules.get(module_name):
        return importlib.reload(module)
    return importlib.import_module(module_name)


config_module = _import_or_reload("src.configs.config")
MONITORING_MODE = config_module.MONITORING_MODE

if _RUNTIME_SMOKE_REQUESTED:
    from src.retrieval.bootstrap import reconcile_and_inspect_runtime

    try:
        _retrieval_runtime = reconcile_and_inspect_runtime(
            config_module.DATA_ROOT,
            allow_live_writer_read=True,
            prefer_fast_read=True,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "surface": "gui",
                    "error": "RetrievalBootstrapError",
                    "message": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    _finish_runtime_smoke(_retrieval_runtime)

import streamlit as st

st.set_page_config(
    page_title="Finance Report Agent",
    layout="wide",
)


def _reload_loaded_application_modules() -> None:
    """Retain safe hot reloads without mutating active worker dependencies.

    ``src.core.monitoring`` is intentionally process-persistent because
    Monitoring jobs can still be executing its functions during an app rerun.
    """
    for module_name in (
        "src.core.chat_ui_helpers",
        "src.core.data_update_jobs",
        "src.core.conversation_store",
        "src.core.issue_report_store",
        "src.core.pdf_extraction",
        "src.core.compare_pdf_extractors",
        "src.core.metadata_filters",
        "src.core.status",
        "src.utils.citations",
    ):
        if module := sys.modules.get(module_name):
            importlib.reload(module)


_reload_loaded_application_modules()

# This module owns a process-local registry and intentionally survives reruns.
search_engine = importlib.import_module("apps.gui.search_engine")
data_views = _import_or_reload("apps.gui.data_views")
chat_jobs = _import_or_reload("apps.gui.chat_jobs")
chat_views = _import_or_reload("apps.gui.chat_views")
sidebar_views = _import_or_reload("apps.gui.sidebar_views")
if MONITORING_MODE:
    monitoring_views = _import_or_reload("apps.gui.monitoring_views")


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


threads = sidebar_views.load_threads()
sidebar_views.ensure_current_thread(threads)
current_id = st.session_state.current_thread_id
current_thread = next(thread for thread in threads if thread["id"] == current_id)

chat_jobs.show_queued_chat_job_toasts()
chat_jobs.render_chat_job_notifications(current_id)

with st.sidebar:
    sidebar_status = sidebar_views.render_sidebar(current_id)

if MONITORING_MODE:
    active_page = st.session_state.get("active_monitoring_page", "Chat")
    if active_page == "Monitoring":
        monitoring_views.render_global_monitoring_page(sidebar_status)
    else:
        chat_tab, chat_monitoring_tab = st.tabs(["Chat", "답변 모니터링"])
        with chat_tab:
            chat_views.render_chat(current_id, current_thread)
        with chat_monitoring_tab:
            monitoring_views.render_chat_monitoring_page(current_id, current_thread)
else:
    chat_views.render_chat(current_id, current_thread)

search_engine.release_background_warmup()
