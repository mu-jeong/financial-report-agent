"""LLM construction utilities.

Centralizes chat model selection so graph nodes do not depend on a
specific provider implementation.
"""

from __future__ import annotations

from typing import Any

from src.configs.config import (
    GEMINI_API_KEY,
    GENERATION_MODEL,
    LLM_PROVIDER,
    OPENROUTER_API_KEY,
    OPENROUTER_APP_TITLE,
    OPENROUTER_APP_URL,
    OPENROUTER_DATA_COLLECTION,
)


def build_chat_model(temperature: float = 0.2, **kwargs: Any):
    """Build the configured LangChain chat model.

    OpenRouter is the default generation provider. Gemini remains supported
    as a fallback provider and is still used separately for embeddings.
    """
    provider = LLM_PROVIDER.lower().strip()

    if provider == "openrouter":
        if not OPENROUTER_API_KEY:
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter. "
                "Copy .env.example to .env and set your OpenRouter key."
            )

        from langchain_openrouter import ChatOpenRouter

        openrouter_provider = kwargs.pop("openrouter_provider", None)
        if openrouter_provider is None and OPENROUTER_DATA_COLLECTION:
            openrouter_provider = {"data_collection": OPENROUTER_DATA_COLLECTION}

        return ChatOpenRouter(
            model=GENERATION_MODEL,
            api_key=OPENROUTER_API_KEY or None,
            temperature=temperature,
            app_url=OPENROUTER_APP_URL or None,
            app_title=OPENROUTER_APP_TITLE or None,
            openrouter_provider=openrouter_provider,
            **kwargs,
        )

    if provider == "gemini":
        if not GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY is required when LLM_PROVIDER=gemini."
            )

        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=GENERATION_MODEL,
            google_api_key=GEMINI_API_KEY,
            temperature=temperature,
            **kwargs,
        )

    raise ValueError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER!r}")
