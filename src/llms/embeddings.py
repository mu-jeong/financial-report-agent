"""Embedding model construction utilities."""

from __future__ import annotations

import logging
import math
import random
import time
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

import requests
from langchain_core.embeddings import Embeddings

from src.configs.config import (
    EMBEDDING_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_APP_TITLE,
    OPENROUTER_APP_URL,
    OPENROUTER_DATA_COLLECTION,
)

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 503, 529})
_DEFAULT_MAX_RETRIES = 6
_INITIAL_BACKOFF_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 60.0
_BACKOFF_JITTER_RATIO = 0.2


class OpenRouterEmbeddings(Embeddings):
    """LangChain embeddings wrapper for OpenRouter's embeddings endpoint."""

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1/embeddings",
        batch_size: int = 64,
        timeout: float = 60.0,
        app_url: str = "",
        app_title: str = "finance_llm",
        data_collection: str = "deny",
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        if not api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required for OpenRouter embeddings. "
                "Copy .env.example to .env and set your OpenRouter key."
            )
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise ValueError("max_retries must be a non-negative integer")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.batch_size = batch_size
        self.timeout = timeout
        self.app_url = app_url
        self.app_title = app_title
        self.data_collection = data_collection
        self.max_retries = max_retries

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        embeddings: list[list[float]] = []
        for batch in _batched(texts, self.batch_size):
            embeddings.extend(self._embed(list(batch), input_type="search_document"))
        return embeddings

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text, input_type="search_query")[0]

    def _embed(self, inputs: str | list[str], *, input_type: str) -> list[list[float]]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": inputs,
            "input_type": input_type,
            "encoding_format": "float",
        }
        if self.data_collection:
            payload["provider"] = {"data_collection": self.data_collection}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.app_url:
            headers["HTTP-Referer"] = self.app_url
        if self.app_title:
            headers["X-Title"] = self.app_title

        response = self._post_with_retries(headers=headers, payload=payload)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"OpenRouter embeddings request failed: {response.status_code} {response.text}"
            ) from exc

        body = response.json()
        data = body.get("data")
        if not isinstance(data, list):
            raise RuntimeError(f"OpenRouter embeddings response missing data: {body}")

        sorted_data = sorted(data, key=lambda item: item.get("index", 0))
        return [[float(value) for value in item["embedding"]] for item in sorted_data]

    def _post_with_retries(
        self,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> requests.Response:
        for retry_index in range(self.max_retries + 1):
            response = requests.post(
                self.base_url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
            if (
                response.status_code not in _RETRYABLE_STATUS_CODES
                or retry_index == self.max_retries
            ):
                return response

            delay = _retry_delay_seconds(
                response.headers.get("Retry-After"),
                retry_index=retry_index,
            )
            logger.warning(
                "OpenRouter embeddings returned HTTP %s; retrying in %.2fs (%s/%s)",
                response.status_code,
                delay,
                retry_index + 1,
                self.max_retries,
            )
            time.sleep(delay)

        raise AssertionError("OpenRouter retry loop exited unexpectedly")


def build_embeddings_model() -> Embeddings:
    """Build the OpenRouter embeddings implementation."""
    return OpenRouterEmbeddings(
        model=EMBEDDING_MODEL,
        api_key=OPENROUTER_API_KEY or "",
        app_url=OPENROUTER_APP_URL,
        app_title=OPENROUTER_APP_TITLE,
        data_collection=OPENROUTER_DATA_COLLECTION,
    )


def _batched(items: list[str], batch_size: int) -> Iterable[list[str]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def _retry_delay_seconds(retry_after: str | None, *, retry_index: int) -> float:
    parsed_retry_after = _parse_retry_after(retry_after)
    if parsed_retry_after is not None:
        return parsed_retry_after

    base_delay = min(
        _INITIAL_BACKOFF_SECONDS * (2**retry_index),
        _MAX_BACKOFF_SECONDS,
    )
    jitter = random.uniform(0.0, base_delay * _BACKOFF_JITTER_RATIO)
    return min(base_delay + jitter, _MAX_BACKOFF_SECONDS)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None

    normalized = str(value).strip()
    if not normalized:
        return None

    try:
        delay = float(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return None
        delay = retry_at.timestamp() - time.time()

    if not math.isfinite(delay):
        return None
    return max(0.0, delay)
