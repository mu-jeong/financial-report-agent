"""Background chat execution and cross-rerun job state."""

import threading
import time
import uuid

import streamlit as st

from src.core import conversation_store
from src.core import monitoring
from src.core.chat_ui_helpers import build_scope_notice
from src.core.followup_scope import build_answer_scope_index
from src.graphs import main_graph as main_graph_module


append_message = conversation_store.append_message
compact_graph_monitoring_metadata = monitoring.compact_graph_monitoring_metadata
graph_app = main_graph_module.graph_app
mark_interrupted_running_messages_failed = (
    conversation_store.mark_interrupted_running_messages_failed
)
update_message = conversation_store.update_message


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


def consume_chat_job_events() -> list[dict]:
    registry = _chat_job_registry()
    with registry["lock"]:
        events = list(registry["events"])
        registry["events"].clear()
    return events


def _queue_chat_job_toast(event: dict) -> None:
    st.session_state.setdefault("chat_job_toasts", []).append(event)


def show_queued_chat_job_toasts() -> None:
    queued_events = st.session_state.pop("chat_job_toasts", [])
    for event in queued_events:
        icon = "✅" if event.get("status") == "succeeded" else "⚠️"
        st.toast(event.get("message", "답변 작업 상태가 변경되었습니다."), icon=icon)


def repair_interrupted_chat_jobs() -> int:
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


def latest_search_scope(messages: list[dict]) -> dict | None:
    """Return the latest successful assistant search scope in the current thread."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        metadata = message.get("metadata") or {}
        if metadata.get("status") in {"running", "failed"} or metadata.get(
            "no_vector_results"
        ):
            continue
        scope = metadata.get("search_scope")
        if isinstance(scope, dict):
            return scope
    return None


def thread_has_running_job(messages: list[dict]) -> bool:
    return any(
        message.get("role") == "assistant"
        and (message.get("metadata") or {}).get("status") == "running"
        for message in messages
    )


def chat_message_anchor_id(
    message_id: int | str | None,
    fallback_index: int,
) -> str:
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
    prior_search_scope: dict | None,
    registry: dict,
) -> None:
    started_at = time.perf_counter()
    try:
        config = {"configurable": {"thread_id": thread_id}}
        graph_input = {"question": user_query}
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
        selected_sources = (
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
            "selected_sources": selected_sources,
        }
        metadata.update(
            compact_graph_monitoring_metadata(
                final_state=final_state,
                latency_seconds=time.perf_counter() - started_at,
                rerank_info=selected_sources,
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


def start_chat_response_job(
    *,
    thread_id: str,
    thread_name: str,
    user_query: str,
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
            "prior_search_scope": prior_search_scope,
            "registry": registry,
        },
        name=f"chat-response-{job_id[:8]}",
        daemon=True,
    ).start()
    return assistant_message_id


@st.fragment(run_every="2s")
def render_chat_job_notifications(current_thread_id: str) -> None:
    should_refresh_current_thread = False
    for event in consume_chat_job_events():
        _queue_chat_job_toast(event)
        if event.get("thread_id") == current_thread_id:
            st.session_state.pending_scroll_anchor = chat_message_anchor_id(
                event.get("assistant_message_id"),
                0,
            )
            should_refresh_current_thread = True
    if should_refresh_current_thread:
        st.rerun(scope="app")
    show_queued_chat_job_toasts()
