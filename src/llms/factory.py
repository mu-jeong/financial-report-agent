"""LLM construction utilities.

Centralizes chat model selection so graph nodes do not depend on a
specific provider implementation.
"""

from __future__ import annotations

from typing import Any

from src.configs.config import (
    GENERATION_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_APP_TITLE,
    OPENROUTER_APP_URL,
    OPENROUTER_DATA_COLLECTION,
)


DEFAULT_CHAT_REQUEST_TIMEOUT_MS = 60_000
DEFAULT_CHAT_MAX_RETRIES = 0


def build_chat_model(temperature: float = 0.2, **kwargs: Any):
    """Build the OpenRouter-backed LangChain chat model."""
    if not OPENROUTER_API_KEY:
        raise ValueError(
            "OPENROUTER_API_KEY is required. "
            "Copy .env.example to .env and set your OpenRouter key."
        )

    from langchain_openrouter import ChatOpenRouter

    openrouter_provider = kwargs.pop("openrouter_provider", None)
    if openrouter_provider is None and OPENROUTER_DATA_COLLECTION:
        openrouter_provider = {"data_collection": OPENROUTER_DATA_COLLECTION}
    kwargs.setdefault("timeout", DEFAULT_CHAT_REQUEST_TIMEOUT_MS)
    kwargs.setdefault("max_retries", DEFAULT_CHAT_MAX_RETRIES)
    if kwargs["max_retries"] == 0 and "retries" not in kwargs:
        model_kwargs = dict(kwargs.get("model_kwargs") or {})
        model_kwargs.setdefault("retries", None)
        kwargs["model_kwargs"] = model_kwargs

    return ChatOpenRouter(
        model=GENERATION_MODEL,
        api_key=OPENROUTER_API_KEY or None,
        temperature=temperature,
        app_url=OPENROUTER_APP_URL or None,
        app_title=OPENROUTER_APP_TITLE or None,
        openrouter_provider=openrouter_provider,
        **kwargs,
    )
