import asyncio
import sys
from types import SimpleNamespace

import pytest
from openrouter import RetryConfig
from openrouter.utils.retries import BackoffStrategy

from src.llms import factory


def _retry_config() -> RetryConfig:
    return RetryConfig(
        strategy="backoff",
        backoff=BackoffStrategy(
            initial_interval=100,
            max_interval=500,
            exponent=2.0,
            max_elapsed_time=1_000,
        ),
        retry_connection_errors=True,
    )


def test_chat_model_has_bounded_request_defaults(monkeypatch):
    captured: dict[str, object] = {}

    class FakeChatOpenRouter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "langchain_openrouter",
        SimpleNamespace(ChatOpenRouter=FakeChatOpenRouter),
    )
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")

    factory.build_chat_model()

    assert captured["timeout"] == 60_000
    assert captured["max_retries"] == 0
    assert captured["model_kwargs"] == {"retries": None}


def test_chat_model_allows_explicit_timeout_overrides(monkeypatch):
    captured: dict[str, object] = {}

    class FakeChatOpenRouter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "langchain_openrouter",
        SimpleNamespace(ChatOpenRouter=FakeChatOpenRouter),
    )
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")

    factory.build_chat_model(timeout=15_000, max_retries=0)

    assert captured["timeout"] == 15_000
    assert captured["max_retries"] == 0
    assert captured["model_kwargs"] == {"retries": None}


def test_chat_model_preserves_explicit_retry_configuration(monkeypatch):
    captured_calls: list[dict[str, object]] = []
    retry_config = _retry_config()

    class FakeChatOpenRouter:
        def __init__(self, **kwargs):
            captured_calls.append(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "langchain_openrouter",
        SimpleNamespace(ChatOpenRouter=FakeChatOpenRouter),
    )
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")

    factory.build_chat_model(
        max_retries=0,
        model_kwargs={"retries": retry_config},
    )
    factory.build_chat_model(max_retries=2)
    factory.build_chat_model(max_retries=0, retries=retry_config)

    assert captured_calls[0]["model_kwargs"] == {"retries": retry_config}
    assert "model_kwargs" not in captured_calls[1]
    assert captured_calls[2]["retries"] is retry_config
    assert "model_kwargs" not in captured_calls[2]


def test_real_chat_model_uses_millisecond_request_timeout(monkeypatch):
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")

    model = factory.build_chat_model()

    try:
        assert model.request_timeout == 60_000
        assert model.client.sdk_configuration.timeout_ms == 60_000
        assert model.max_retries == 0
        assert model._default_params["retries"] is None
    finally:
        model.client.sdk_configuration.client.close()
        asyncio.run(model.client.sdk_configuration.async_client.aclose())


def test_real_chat_model_preserves_explicit_retry_override(monkeypatch):
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")
    retry_config = _retry_config()

    model = factory.build_chat_model(
        max_retries=0,
        model_kwargs={"retries": retry_config},
    )

    try:
        assert model._default_params["retries"] is retry_config
    finally:
        model.client.sdk_configuration.client.close()
        asyncio.run(model.client.sdk_configuration.async_client.aclose())


def test_real_chat_model_accepts_top_level_retry_override(monkeypatch):
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")
    retry_config = _retry_config()

    with pytest.warns(UserWarning, match="retries is not default parameter"):
        model = factory.build_chat_model(max_retries=0, retries=retry_config)

    try:
        assert model._default_params["retries"] is retry_config
    finally:
        model.client.sdk_configuration.client.close()
        asyncio.run(model.client.sdk_configuration.async_client.aclose())


def test_real_chat_model_disables_retries_at_sdk_request_boundary(monkeypatch):
    monkeypatch.setattr(factory, "OPENROUTER_API_KEY", "test-key")
    model = factory.build_chat_model()
    captured: dict[str, object] = {}

    class StopProbe(Exception):
        pass

    def capture_request(
        hook_ctx,
        request,
        is_error_status_code,
        stream=False,
        retry_config=None,
    ):
        captured["retry_config"] = retry_config
        raise StopProbe

    monkeypatch.setattr(model.client.chat, "do_request", capture_request)

    try:
        with pytest.raises(StopProbe):
            model.client.chat.send(
                model="test-model",
                messages=[{"role": "user", "content": "test"}],
                retries=model._default_params["retries"],
            )
        assert captured["retry_config"] is None
    finally:
        model.client.sdk_configuration.client.close()
        asyncio.run(model.client.sdk_configuration.async_client.aclose())
