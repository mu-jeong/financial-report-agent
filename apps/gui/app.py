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
                "v1_fallback_open": selection.v1_fallback_open,
                "degraded": selection.degraded,
                "write_enabled": selection.write_enabled,
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0)


import streamlit as st
from src.configs import config as config_module

config_module = importlib.reload(config_module)
MONITORING_MODE = config_module.MONITORING_MODE

from src.retrieval.bootstrap import reconcile_and_inspect_runtime

_retrieval_startup_error = None
try:
    _retrieval_runtime = reconcile_and_inspect_runtime(config_module.DB_PATH)
except Exception as exc:
    _retrieval_runtime = None
    _retrieval_startup_error = f"{type(exc).__name__}: {exc}"

st.set_page_config(
    page_title="Finance Report Agent",
    layout="wide",
)
if _retrieval_startup_error is not None:
    if _RUNTIME_SMOKE_REQUESTED:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "surface": "gui",
                    "error": "RetrievalBootstrapError",
                    "message": _retrieval_startup_error,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)
    st.error(f"Retrieval startup validation failed: {_retrieval_startup_error}")
    st.stop()

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
from src.utils import citations

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
citations = importlib.reload(citations)

data_views = importlib.import_module("apps.gui.data_views")
chat_jobs = importlib.import_module("apps.gui.chat_jobs")
chat_views = importlib.import_module("apps.gui.chat_views")
sidebar_views = importlib.import_module("apps.gui.sidebar_views")
monitoring_views = importlib.import_module("apps.gui.monitoring_views")

data_views = importlib.reload(data_views)
chat_jobs = importlib.reload(chat_jobs)
chat_views = importlib.reload(chat_views)
sidebar_views = importlib.reload(sidebar_views)
monitoring_views = importlib.reload(monitoring_views)


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


if _RUNTIME_SMOKE_REQUESTED:
    _finish_runtime_smoke(_retrieval_runtime)


threads = sidebar_views.load_threads()
sidebar_views.ensure_current_thread(threads)
current_id = st.session_state.current_thread_id
current_thread = next(thread for thread in threads if thread["id"] == current_id)

chat_jobs.show_queued_chat_job_toasts()
chat_jobs.render_chat_job_notifications(current_id)

with st.sidebar:
    sidebar_views.render_sidebar(current_id)

if MONITORING_MODE:
    active_page = st.session_state.get("active_monitoring_page", "Chat")
    if active_page == "전체 Monitoring":
        monitoring_views.render_global_monitoring_page()
    else:
        chat_tab, chat_monitoring_tab = st.tabs(["Chat", "Chat Monitoring"])
        with chat_tab:
            chat_views.render_chat(current_id, current_thread)
        with chat_monitoring_tab:
            monitoring_views.render_chat_monitoring_page(current_id, current_thread)
else:
    chat_views.render_chat(current_id, current_thread)
