from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.messages.tool import tool_call_chunk
from langchain_openrouter import ChatOpenRouter

from src.llms import generation_observability as observability


class _RawChunk:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, *, by_alias=False):
        return dict(self.payload)


def test_streaming_measures_usage_ttft_throughput_and_selected_provider(monkeypatch):
    class ChatEndpoint:
        metadata_header = None

        def send(self, *, stream=False, x_open_router_metadata=None):
            self.metadata_header = x_open_router_metadata
            assert stream is True
            return iter(
                [
                    _RawChunk({"id": "gen-1", "model": "model-a"}),
                    _RawChunk({"content": "hello"}),
                    _RawChunk({"content": " world"}),
                    _RawChunk(
                        {
                            "usage": {
                                "prompt_tokens": 8,
                                "completion_tokens": 2,
                                "total_tokens": 10,
                            },
                            "openrouter_metadata": {
                                "strategy": "direct",
                                "attempt": 1,
                                "endpoints": {
                                    "available": [
                                        {
                                            "provider": "Provider A",
                                            "selected": True,
                                        }
                                    ]
                                },
                            },
                        }
                    ),
                ]
            )

    class StreamingModel:
        model_name = "requested-model"

        def __init__(self):
            self.client = SimpleNamespace(chat=ChatEndpoint())

        def stream(self, _messages):
            for raw_chunk in self.client.chat.send(stream=True):
                payload = raw_chunk.model_dump()
                usage = payload.get("usage")
                yield AIMessageChunk(
                    content=payload.get("content", ""),
                    response_metadata=(
                        {
                            "id": payload.get("id"),
                            "model_name": payload.get("model"),
                            "model_provider": "openrouter",
                        }
                        if payload.get("id")
                        else {}
                    ),
                    usage_metadata=(
                        {
                            "input_tokens": usage["prompt_tokens"],
                            "output_tokens": usage["completion_tokens"],
                            "total_tokens": usage["total_tokens"],
                        }
                        if usage
                        else None
                    ),
                )

    clock = iter([0, 50_000_000, 200_000_000, 300_000_000, 400_000_000, 500_000_000])
    monkeypatch.setattr(observability.time, "perf_counter_ns", lambda: next(clock))
    model = StreamingModel()

    message, metrics = observability.invoke_chat_with_observability(
        model,
        [HumanMessage(content="question")],
    )

    assert message.content == "hello world"
    assert model.client.chat.metadata_header == "enabled"
    assert metrics["input_tokens"] == 8
    assert metrics["output_tokens"] == 2
    assert metrics["total_tokens"] == 10
    assert metrics["time_to_first_token_ns"] == 200_000_000
    assert metrics["request_ns"] == 500_000_000
    assert metrics["output_tokens_per_second"] == 4.0
    assert metrics["provider_name"] == "Provider A"
    assert metrics["gateway_provider"] == "openrouter"
    assert metrics["model_name"] == "model-a"
    assert metrics["generation_id"] == "gen-1"
    assert metrics["router_metadata_status"] == "measured"
    assert metrics["status"] == "measured"


def test_chat_openrouter_binding_keeps_usage_and_router_metadata_in_one_stream():
    model = ChatOpenRouter(model="openai/gpt-4o-mini", api_key="test-key")
    observed = {}

    def fake_send(*, stream=False, x_open_router_metadata=None, **_kwargs):
        observed["stream"] = stream
        observed["metadata"] = x_open_router_metadata
        return iter(
            [
                _RawChunk(
                    {
                        "id": "gen-real-contract",
                        "model": "openai/gpt-4o-mini",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "answer"},
                                "finish_reason": None,
                                "native_finish_reason": None,
                            }
                        ],
                    }
                ),
                _RawChunk(
                    {
                        "id": "gen-real-contract",
                        "model": "openai/gpt-4o-mini",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                                "native_finish_reason": "stop",
                            }
                        ],
                        "openrouter_metadata": {
                            "strategy": "direct",
                            "attempt": 1,
                            "endpoints": {
                                "available": [
                                    {"provider": "OpenAI", "selected": True}
                                ]
                            },
                        },
                    }
                ),
                _RawChunk(
                    {
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 3,
                            "total_tokens": 15,
                        },
                    }
                ),
            ]
        )

    model.client.chat.send = fake_send

    message, metrics = observability.invoke_chat_with_observability(
        model.bind_tools([]),
        [HumanMessage(content="question")],
    )

    assert message.content == "answer"
    assert observed == {"stream": True, "metadata": "enabled"}
    assert metrics["input_tokens"] == 12
    assert metrics["output_tokens"] == 3
    assert metrics["provider_name"] == "OpenAI"
    assert metrics["generation_id"] == "gen-real-contract"
    assert metrics["streamed"] is True


def test_streaming_preserves_tool_call_chunks(monkeypatch):
    class ToolStreamingModel:
        def stream(self, _messages):
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    tool_call_chunk(
                        name="get_stock_price",
                        args='{"company_name":',
                        id="call-1",
                        index=0,
                    )
                ],
            )
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    tool_call_chunk(name=None, args='"A"}', id=None, index=0)
                ],
                usage_metadata={
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "total_tokens": 5,
                },
            )

    clock = iter([0, 100, 200, 300])
    monkeypatch.setattr(observability.time, "perf_counter_ns", lambda: next(clock))

    message, metrics = observability.invoke_chat_with_observability(
        ToolStreamingModel(),
        [HumanMessage(content="price")],
    )

    assert message.tool_calls == [
        {
            "name": "get_stock_price",
            "args": {"company_name": "A"},
            "id": "call-1",
            "type": "tool_call",
        }
    ]
    assert metrics["time_to_first_token_ns"] == 100
    assert metrics["streamed"] is True


def test_non_streaming_model_falls_back_without_inventing_ttft(monkeypatch):
    class InvokeOnlyModel:
        model_name = "model-b"

        def invoke(self, _messages):
            return AIMessage(
                content="answer",
                response_metadata={"model_provider": "other-gateway"},
                usage_metadata={
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "total_tokens": 25,
                },
            )

    clock = iter([1_000_000_000, 2_000_000_000])
    monkeypatch.setattr(observability.time, "perf_counter_ns", lambda: next(clock))

    message, metrics = observability.invoke_chat_with_observability(
        InvokeOnlyModel(),
        [HumanMessage(content="question")],
    )

    assert message.content == "answer"
    assert metrics["streamed"] is False
    assert metrics["time_to_first_token_ns"] is None
    assert metrics["output_tokens_per_second"] == 5.0
    assert metrics["gateway_provider"] == "other-gateway"
    assert metrics["provider_name"] is None
    assert metrics["status"] == "partial"


def test_turn_metrics_aggregate_tool_and_followup_calls():
    first = {
        "streamed": True,
        "request_ns": 1_000_000_000,
        "after_first_token_ns": 600_000_000,
        "time_to_first_token_ns": 400_000_000,
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
        "cache_read_tokens": 0,
        "reasoning_tokens": 0,
        "provider_name": "Provider A",
        "gateway_provider": "openrouter",
        "model_name": "model-a",
        "router_metadata_status": "measured",
    }
    second = {
        **first,
        "request_ns": 2_000_000_000,
        "after_first_token_ns": 1_500_000_000,
        "time_to_first_token_ns": 500_000_000,
        "input_tokens": 50,
        "output_tokens": 20,
        "total_tokens": 70,
    }

    aggregate = observability.merge_generation_metrics(
        None,
        first,
        phase="tool_request",
    )
    aggregate = observability.merge_generation_metrics(
        aggregate,
        second,
        phase="tool_followup",
    )

    assert aggregate["call_count"] == 2
    assert aggregate["streamed_call_count"] == 2
    assert aggregate["input_tokens"] == 150
    assert aggregate["output_tokens"] == 30
    assert aggregate["total_tokens"] == 180
    assert aggregate["request_ns"] == 3_000_000_000
    assert aggregate["time_to_first_token_ns"] == 500_000_000
    assert aggregate["max_time_to_first_token_ns"] == 500_000_000
    assert aggregate["output_tokens_per_second"] == 10.0
    assert [call["phase"] for call in aggregate["calls"]] == [
        "tool_request",
        "tool_followup",
    ]
