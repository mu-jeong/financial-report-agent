"""Background chat execution and cross-rerun job state."""

import queue
import threading
import time
import uuid

import streamlit as st

from apps.gui import search_engine
from src.core import conversation_store
from src.core import monitoring
from src.core.chat_ui_helpers import build_scope_notice
from src.core.followup_scope import build_answer_scope_index


append_pending_exchange = conversation_store.append_pending_exchange
compact_graph_monitoring_metadata = monitoring.compact_graph_monitoring_metadata
get_chat_history = conversation_store.get_chat_history
mark_interrupted_running_messages_failed = (
    conversation_store.mark_interrupted_running_messages_failed
)
update_message = conversation_store.update_message


CHAT_RESPONSE_TIMEOUT_SECONDS = 180.0


class _LazyGraphApp:
    """Preserve the graph_app seam while moving its import off the UI thread."""

    @staticmethod
    def prepare():
        return search_engine.wait_for_search_engine(retry_failed=True)

    @staticmethod
    def invoke(graph_input: dict, *, config: dict) -> dict:
        return search_engine.invoke_graph(graph_input, config=config)

    @staticmethod
    def runtime_provenance() -> dict | None:
        return search_engine.get_retrieval_runtime_provenance()


graph_app = _LazyGraphApp()


class PendingSearchEngineQuestionError(RuntimeError):
    """Raised when a second question targets an engine that is still warming."""


class ChatResponseTimeout(TimeoutError):
    """Raised when a graph invocation exceeds the user-visible answer deadline."""


# This registry is process-local. Deploy a change that moves this cached function
# only while no answer jobs are in flight; normal reruns retain its qualified key.
@st.cache_resource
def _chat_job_registry() -> dict:
    return {
        "running_job_ids": set(),
        "events": [],
        "lock": threading.Lock(),
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


def has_pending_search_engine_job() -> bool:
    """Return whether one process-wide first question is waiting for warmup."""
    registry = _chat_job_registry()
    with registry["lock"]:
        return bool(registry.setdefault("pending_engine_job_ids", set()))


def chat_message_anchor_id(
    message_id: int | str | None,
    fallback_index: int,
) -> str:
    if message_id is not None:
        return f"chat_message_id_{message_id}"
    return f"chat_message_{fallback_index}"


def _invoke_graph_with_timeout(
    graph_input: dict,
    *,
    config: dict,
    job_id: str,
    timeout_seconds: float = CHAT_RESPONSE_TIMEOUT_SECONDS,
) -> dict:
    """Run one graph call without letting it monopolize later chat jobs.

    Python cannot stop a running synchronous call safely. If the deadline wins,
    the daemon may finish later, but only this coordinator persists its result.
    """
    outcome: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def invoke() -> None:
        try:
            outcome.put(("result", graph_app.invoke(graph_input, config=config)))
        except Exception as exc:
            outcome.put(("error", exc))

    threading.Thread(
        target=invoke,
        name=f"chat-graph-{job_id[:8]}",
        daemon=True,
    ).start()
    try:
        outcome_type, value = outcome.get(timeout=max(0.0, timeout_seconds))
    except queue.Empty as exc:
        raise ChatResponseTimeout(
            f"Chat response timed out after {timeout_seconds:g} seconds."
        ) from exc
    if outcome_type == "error":
        if not isinstance(value, BaseException):
            raise TypeError("Chat graph returned an invalid error result.")
        raise value
    if not isinstance(value, dict):
        raise TypeError("Chat graph returned a non-dictionary result.")
    return value


def _run_chat_response_job(
    *,
    job_id: str,
    thread_id: str,
    thread_name: str,
    assistant_message_id: int,
    user_query: str,
    chat_history: list[tuple[str, str]],
    prior_search_scope: dict | None,
    registry: dict,
    queued_while_warming: bool = False,
) -> None:
    started_at = time.perf_counter()
    engine_queue_released = False
    runtime_provenance = None

    def release_engine_queue() -> bool:
        nonlocal engine_queue_released
        if not queued_while_warming or engine_queue_released:
            return False
        with registry["lock"]:
            pending_job_ids = registry.setdefault(
                "pending_engine_job_ids",
                set(),
            )
            was_pending = job_id in pending_job_ids
            if was_pending:
                registry.setdefault("events", []).append(
                    {
                        "status": "progress",
                        "thread_id": thread_id,
                        "thread_name": thread_name,
                        "assistant_message_id": assistant_message_id,
                        "engine_queue_released": True,
                    }
                )
            pending_job_ids.discard(job_id)
        engine_queue_released = True
        return was_pending

    try:
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": job_id,
            }
        }
        graph_input = {
            "question": user_query,
            "chat_history": chat_history,
        }
        if prior_search_scope:
            graph_input["prior_search_scope"] = prior_search_scope
        prepare_graph = getattr(graph_app, "prepare", None)
        if callable(prepare_graph):
            prepare_graph()
        read_runtime_provenance = getattr(
            graph_app,
            "runtime_provenance",
            None,
        )
        if callable(read_runtime_provenance):
            runtime_provenance = read_runtime_provenance()
        if queued_while_warming:
            update_message(
                assistant_message_id,
                "AI가 리포트 내용을 검색하고 분석 중입니다...",
                {
                    "status": "running",
                    "job_id": job_id,
                    "phase": "answering",
                },
            )
        release_engine_queue()
        final_state = _invoke_graph_with_timeout(
            graph_input,
            config=config,
            job_id=job_id,
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
        if isinstance(runtime_provenance, dict):
            metadata["retrieval_runtime"] = runtime_provenance
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
        release_engine_queue()
        failure_metadata = {
            "status": "failed",
            "job_id": job_id,
            "error": str(exc),
            "latency_seconds": round(time.perf_counter() - started_at, 3),
        }
        if isinstance(runtime_provenance, dict):
            failure_metadata["retrieval_runtime"] = runtime_provenance
        update_message(
            assistant_message_id,
            "답변을 생성하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            failure_metadata,
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
        release_engine_queue()
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
    chat_history = get_chat_history(thread_id)
    engine_state = search_engine.get_search_engine_status()["state"]
    waiting_for_engine = engine_state != "ready"
    registry = _chat_job_registry()
    with registry["lock"]:
        pending_job_ids = registry.setdefault("pending_engine_job_ids", set())
        if waiting_for_engine and pending_job_ids:
            raise PendingSearchEngineQuestionError(
                "Only one question can wait for search-engine warmup."
            )
        if waiting_for_engine:
            pending_job_ids.add(job_id)
        registry["running_job_ids"].add(job_id)

    assistant_content = (
        "검색 엔진을 준비한 뒤 질문을 자동으로 분석합니다. 잠시만 기다려 주세요..."
        if waiting_for_engine
        else "AI가 리포트 내용을 검색하고 분석 중입니다..."
    )
    assistant_metadata = {"status": "running", "job_id": job_id}
    if waiting_for_engine:
        assistant_metadata["phase"] = "waiting_for_engine"
    try:
        _, assistant_message_id = append_pending_exchange(
            thread_id,
            user_query,
            assistant_content,
            assistant_metadata,
        )
    except Exception:
        with registry["lock"]:
            registry["running_job_ids"].discard(job_id)
            registry.setdefault("pending_engine_job_ids", set()).discard(job_id)
        raise

    try:
        threading.Thread(
            target=_run_chat_response_job,
            kwargs={
                "job_id": job_id,
                "thread_id": thread_id,
                "thread_name": thread_name,
                "assistant_message_id": assistant_message_id,
                "user_query": user_query,
                "chat_history": chat_history,
                "prior_search_scope": prior_search_scope,
                "registry": registry,
                "queued_while_warming": waiting_for_engine,
            },
            name=f"chat-response-{job_id[:8]}",
            daemon=True,
        ).start()
    except Exception as exc:
        with registry["lock"]:
            registry["running_job_ids"].discard(job_id)
            registry.setdefault("pending_engine_job_ids", set()).discard(job_id)
        update_message(
            assistant_message_id,
            "답변 작업을 시작하지 못했습니다. 잠시 후 다시 시도해 주세요.",
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
                "message": f"'{thread_name}' 답변 작업을 시작하지 못했습니다.",
                "engine_queue_released": waiting_for_engine,
            },
            registry,
        )
    return assistant_message_id


@st.fragment(run_every=2.0)
def render_chat_job_notifications(current_thread_id: str) -> None:
    should_refresh_app = False
    for event in consume_chat_job_events():
        if event.get("status") in {"succeeded", "failed"}:
            _queue_chat_job_toast(event)
        if event.get("engine_queue_released"):
            should_refresh_app = True
        if event.get("thread_id") == current_thread_id:
            st.session_state.pending_scroll_anchor = chat_message_anchor_id(
                event.get("assistant_message_id"),
                0,
            )
            should_refresh_app = True
    if should_refresh_app:
        st.rerun(scope="app")
    show_queued_chat_job_toasts()
