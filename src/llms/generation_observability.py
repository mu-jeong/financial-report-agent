"""Measure answer-generation latency and usage without changing model output.

The helper consumes the normal LangChain stream so time-to-first-token can be
measured at the client boundary.  When the underlying client is OpenRouter, it
also opts into compact router metadata and captures only the selected provider
identity; the full routing payload is deliberately not persisted.
"""

from __future__ import annotations

import inspect
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.utils import message_chunk_to_message


_ROUTER_METADATA_ARGUMENT = "x_open_router_metadata"
_ROUTER_METADATA_ENABLED = "enabled"


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if numeric > 0 else None


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        dumped = dump(by_alias=True)
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def _unwrap_model(model: Any) -> Any:
    """Return the chat model inside a LangChain Runnable binding."""

    current = model
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        bound = getattr(current, "bound", None)
        if bound is None or bound is current:
            break
        current = bound
    return current


def _selected_provider(router_metadata: Mapping[str, Any]) -> str | None:
    endpoints = router_metadata.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, Mapping) else []
    if isinstance(available, list):
        for endpoint in available:
            if isinstance(endpoint, Mapping) and endpoint.get("selected") is True:
                provider = str(endpoint.get("provider") or "").strip()
                if provider:
                    return provider

    attempts = router_metadata.get("attempts")
    if isinstance(attempts, list):
        for attempt in reversed(attempts):
            if not isinstance(attempt, Mapping):
                continue
            status = attempt.get("status")
            if isinstance(status, int) and 200 <= status < 300:
                provider = str(attempt.get("provider") or "").strip()
                if provider:
                    return provider
    return None


class _RouterCapture:
    def __init__(self) -> None:
        self.supported = False
        self.generation_id: str | None = None
        self.model_name: str | None = None
        self.usage: dict[str, Any] = {}
        self.router_metadata: dict[str, Any] = {}

    def record(self, value: Any) -> None:
        payload = _as_mapping(value)
        if not payload:
            return
        generation_id = str(payload.get("id") or "").strip()
        model_name = str(payload.get("model") or "").strip()
        if generation_id:
            self.generation_id = generation_id
        if model_name:
            self.model_name = model_name
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            self.usage = dict(usage)
        router_metadata = payload.get("openrouter_metadata")
        if isinstance(router_metadata, Mapping):
            self.router_metadata = dict(router_metadata)


@contextmanager
def _capture_openrouter_response(model: Any) -> Iterator[_RouterCapture]:
    """Capture opt-in router metadata from the existing OpenRouter request."""

    capture = _RouterCapture()
    base_model = _unwrap_model(model)
    chat = getattr(getattr(base_model, "client", None), "chat", None)
    original_send = getattr(chat, "send", None)
    if not callable(original_send):
        yield capture
        return
    try:
        parameters = inspect.signature(original_send).parameters
    except (TypeError, ValueError):
        yield capture
        return
    if _ROUTER_METADATA_ARGUMENT not in parameters:
        yield capture
        return

    capture.supported = True

    def observed_send(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault(_ROUTER_METADATA_ARGUMENT, _ROUTER_METADATA_ENABLED)
        response = original_send(*args, **kwargs)
        if kwargs.get("stream") is True:
            def observed_stream() -> Iterator[Any]:
                for chunk in response:
                    capture.record(chunk)
                    yield chunk

            return observed_stream()
        capture.record(response)
        return response

    chat.send = observed_send
    try:
        yield capture
    finally:
        chat.send = original_send


def _chunk_has_output(chunk: Any) -> bool:
    content = getattr(chunk, "content", None)
    if isinstance(content, str) and content:
        return True
    if isinstance(content, list) and any(bool(part) for part in content):
        return True
    additional = getattr(chunk, "additional_kwargs", None)
    if isinstance(additional, Mapping) and any(
        additional.get(key) for key in ("reasoning_content", "reasoning_details")
    ):
        return True
    return bool(
        getattr(chunk, "tool_call_chunks", None)
        or getattr(chunk, "tool_calls", None)
    )


def _combine_stream(chunks: list[Any]) -> AIMessage:
    if not chunks:
        raise RuntimeError("LLM stream completed without a response chunk")
    if len(chunks) == 1 and isinstance(chunks[0], AIMessage):
        return chunks[0]
    if not all(isinstance(chunk, AIMessageChunk) for chunk in chunks):
        raise TypeError("LLM stream returned an unsupported message type")
    combined = chunks[0]
    for chunk in chunks[1:]:
        combined = combined + chunk
    message = message_chunk_to_message(combined)
    if not isinstance(message, AIMessage):
        raise TypeError("LLM stream did not produce an AIMessage")
    return message


def _usage_metrics(message: AIMessage, capture: _RouterCapture) -> dict[str, int | None]:
    usage = _as_mapping(getattr(message, "usage_metadata", None))
    raw_usage = capture.usage

    input_tokens = _nonnegative_int(
        usage.get("input_tokens")
        if usage.get("input_tokens") is not None
        else raw_usage.get("prompt_tokens")
    )
    output_tokens = _nonnegative_int(
        usage.get("output_tokens")
        if usage.get("output_tokens") is not None
        else raw_usage.get("completion_tokens")
    )
    total_tokens = _nonnegative_int(
        usage.get("total_tokens")
        if usage.get("total_tokens") is not None
        else raw_usage.get("total_tokens")
    )
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    input_details = _as_mapping(usage.get("input_token_details"))
    output_details = _as_mapping(usage.get("output_token_details"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": _nonnegative_int(input_details.get("cache_read")),
        "reasoning_tokens": _nonnegative_int(output_details.get("reasoning")),
    }


def _response_identity(message: AIMessage, model: Any, capture: _RouterCapture) -> dict[str, Any]:
    response_metadata = _as_mapping(getattr(message, "response_metadata", None))
    base_model = _unwrap_model(model)
    router_metadata = capture.router_metadata
    provider_name = _selected_provider(router_metadata)
    gateway_provider = str(response_metadata.get("model_provider") or "").strip() or None
    model_name = (
        capture.model_name
        or str(response_metadata.get("model_name") or "").strip()
        or str(getattr(base_model, "model_name", None) or getattr(base_model, "model", None) or "").strip()
        or None
    )
    generation_id = (
        capture.generation_id
        or str(response_metadata.get("id") or getattr(message, "id", None) or "").strip()
        or None
    )
    if router_metadata:
        router_status = "measured" if provider_name else "partial"
    elif capture.supported:
        router_status = "unavailable"
    else:
        router_status = "unsupported"
    return {
        "provider_name": provider_name,
        "provider_source": "openrouter_router_metadata" if provider_name else None,
        "gateway_provider": gateway_provider,
        "model_name": model_name,
        "generation_id": generation_id,
        "finish_reason": response_metadata.get("finish_reason"),
        "router_metadata_status": router_status,
        "router_strategy": router_metadata.get("strategy"),
        "router_attempt": _nonnegative_int(router_metadata.get("attempt")),
    }


def invoke_chat_with_observability(
    model: Any,
    messages: Sequence[BaseMessage],
) -> tuple[AIMessage, dict[str, Any]]:
    """Invoke one chat completion and return its compact generation metrics."""

    started_ns = time.perf_counter_ns()
    first_output_ns: int | None = None
    streamed = False

    with _capture_openrouter_response(model) as capture:
        stream = getattr(model, "stream", None)
        if callable(stream):
            chunks: list[Any] = []
            try:
                for chunk in stream(messages):
                    observed_ns = time.perf_counter_ns()
                    if first_output_ns is None and _chunk_has_output(chunk):
                        first_output_ns = observed_ns
                    chunks.append(chunk)
            except (AttributeError, NotImplementedError):
                if chunks:
                    raise
                response = model.invoke(messages)
            else:
                response = _combine_stream(chunks)
                streamed = True
        else:
            response = model.invoke(messages)

    finished_ns = time.perf_counter_ns()
    if not isinstance(response, AIMessage):
        raise TypeError("LLM invocation did not return an AIMessage")

    request_ns = max(0, finished_ns - started_ns)
    time_to_first_token_ns = (
        max(0, first_output_ns - started_ns) if first_output_ns is not None else None
    )
    after_first_token_ns = (
        max(0, finished_ns - first_output_ns) if first_output_ns is not None else None
    )
    metrics: dict[str, Any] = {
        "schema_version": 1,
        "streamed": streamed,
        "request_ns": request_ns,
        "time_to_first_token_ns": time_to_first_token_ns,
        "after_first_token_ns": after_first_token_ns,
        **_usage_metrics(response, capture),
        **_response_identity(response, model, capture),
    }
    output_tokens = metrics.get("output_tokens")
    duration_seconds = _positive_number(request_ns / 1_000_000_000)
    metrics["output_tokens_per_second"] = (
        round(output_tokens / duration_seconds, 3)
        if isinstance(output_tokens, int) and duration_seconds is not None
        else None
    )
    metrics["throughput_basis"] = (
        "client_request_to_complete" if metrics["output_tokens_per_second"] is not None else None
    )
    measured_fields = (
        metrics.get("input_tokens"),
        metrics.get("output_tokens"),
        metrics.get("time_to_first_token_ns"),
        metrics.get("provider_name"),
        metrics.get("output_tokens_per_second"),
    )
    if all(value is not None for value in measured_fields):
        metrics["status"] = "measured"
    elif any(value is not None for value in measured_fields):
        metrics["status"] = "partial"
    else:
        metrics["status"] = "not_measured"
    return response, metrics


def _complete_sum(calls: list[dict[str, Any]], key: str) -> int | None:
    values = [_nonnegative_int(call.get(key)) for call in calls]
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def merge_generation_metrics(
    existing: Mapping[str, Any] | None,
    call_metrics: Mapping[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    """Append one generation call and rebuild turn-level aggregate metrics."""

    existing_calls = (existing or {}).get("calls")
    calls = [dict(call) for call in existing_calls if isinstance(call, Mapping)] if isinstance(existing_calls, list) else []
    call = dict(call_metrics)
    call["phase"] = phase
    calls.append(call)

    request_ns = _complete_sum(calls, "request_ns")
    input_tokens = _complete_sum(calls, "input_tokens")
    output_tokens = _complete_sum(calls, "output_tokens")
    total_tokens = _complete_sum(calls, "total_tokens")
    after_first_token_ns = _complete_sum(calls, "after_first_token_ns")
    ttft_values = [
        value
        for value in (_nonnegative_int(item.get("time_to_first_token_ns")) for item in calls)
        if value is not None
    ]
    providers = list(
        dict.fromkeys(
            str(item.get("provider_name"))
            for item in calls
            if item.get("provider_name")
        )
    )
    models = list(
        dict.fromkeys(
            str(item.get("model_name")) for item in calls if item.get("model_name")
        )
    )
    gateways = list(
        dict.fromkeys(
            str(item.get("gateway_provider"))
            for item in calls
            if item.get("gateway_provider")
        )
    )
    throughput = (
        round(output_tokens / (request_ns / 1_000_000_000), 3)
        if output_tokens is not None and request_ns is not None and request_ns > 0
        else None
    )
    core_values = (
        input_tokens,
        output_tokens,
        ttft_values[-1] if ttft_values else None,
        providers[0] if len(providers) == 1 else None,
        throughput,
    )
    router_statuses = [
        str(item.get("router_metadata_status"))
        for item in calls
        if item.get("router_metadata_status")
    ]
    if router_statuses and all(status == "measured" for status in router_statuses):
        router_metadata_status = "measured"
    elif "measured" in router_statuses:
        router_metadata_status = "partial"
    elif router_statuses:
        router_metadata_status = router_statuses[-1]
    else:
        router_metadata_status = "unsupported"
    aggregate = {
        "schema_version": 1,
        "status": (
            "measured"
            if all(value is not None for value in core_values)
            else "partial"
            if any(value is not None for value in core_values)
            else "not_measured"
        ),
        "call_count": len(calls),
        "streamed_call_count": sum(1 for item in calls if item.get("streamed") is True),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_read_tokens": _complete_sum(calls, "cache_read_tokens"),
        "reasoning_tokens": _complete_sum(calls, "reasoning_tokens"),
        "request_ns": request_ns,
        "after_first_token_ns": after_first_token_ns,
        "time_to_first_token_ns": ttft_values[-1] if ttft_values else None,
        "max_time_to_first_token_ns": max(ttft_values) if ttft_values else None,
        "output_tokens_per_second": throughput,
        "throughput_basis": "client_request_to_complete" if throughput is not None else None,
        "provider_name": providers[0] if len(providers) == 1 else None,
        "provider_names": providers,
        "gateway_provider": gateways[0] if len(gateways) == 1 else None,
        "router_metadata_status": router_metadata_status,
        "model_name": models[0] if len(models) == 1 else None,
        "model_names": models,
        "calls": calls,
    }
    return aggregate
