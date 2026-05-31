"""Reranker construction and adapters."""

from __future__ import annotations

from typing import Any

import requests

from src.configs.config import (
    FAISS_DIR,
    OPENROUTER_API_KEY,
    OPENROUTER_APP_TITLE,
    OPENROUTER_APP_URL,
    OPENROUTER_DATA_COLLECTION,
    RERANK_MODEL,
    RERANK_PROVIDER,
    RERANK_TIMEOUT,
    get_logger,
)

logger = get_logger(__name__)


class OpenRouterReranker:
    """OpenRouter /rerank adapter that returns project passage dictionaries."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1/rerank",
        app_url: str = "",
        app_title: str = "finance_llm",
        data_collection: str = "deny",
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required when RERANK_PROVIDER=openrouter. "
                "Copy .env.example to .env and set your OpenRouter key."
            )
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.app_url = app_url
        self.app_title = app_title
        self.data_collection = data_collection
        self.timeout = timeout

    def rerank(self, query: str, passages: list[dict], top_n: int) -> list[dict]:
        if not passages or top_n <= 0:
            return []

        payload: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": [str(passage.get("text", "")) for passage in passages],
            "top_n": min(top_n, len(passages)),
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

        response = requests.post(
            self.base_url,
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"OpenRouter rerank request failed: {response.status_code} {response.text}"
            ) from exc

        body = response.json()
        results = body.get("results")
        if not isinstance(results, list):
            raise RuntimeError(f"OpenRouter rerank response missing results: {body}")

        ranked: list[dict] = []
        seen_indexes: set[int] = set()
        for item in results:
            index = item.get("index")
            if not isinstance(index, int) or index < 0 or index >= len(passages):
                continue
            seen_indexes.add(index)
            passage = dict(passages[index])
            if "relevance_score" in item:
                passage["rerank_score"] = float(item["relevance_score"])
            ranked.append(passage)

        # Defensive fallback if a provider returns fewer than requested.
        if len(ranked) < top_n:
            ranked.extend(
                passage
                for index, passage in enumerate(passages)
                if index not in seen_indexes
            )

        return ranked[:top_n]


class FlashRankReranker:
    """Local FlashRank adapter kept as an optional fallback."""

    def __init__(self) -> None:
        logger.info("⏳ [시스템] 로컬 FlashRank 모델을 환경 세팅 중입니다... (최초 1회 다운로드 소요)")
        from flashrank import Ranker, RerankRequest

        self._request_cls = RerankRequest
        self._ranker = Ranker(model_name=RERANK_MODEL, cache_dir=FAISS_DIR)
        logger.info("✅ 로컬 FlashRank 모델 로딩 완료!")

    def rerank(self, query: str, passages: list[dict], top_n: int) -> list[dict]:
        request = self._request_cls(query=query, passages=passages)
        return self._ranker.rerank(request)[:top_n]


class RankerSingleton:
    _instance = None
    _ranker = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RankerSingleton, cls).__new__(cls)
        return cls._instance

    def get_ranker(self):
        if self._ranker is None:
            provider = RERANK_PROVIDER.strip().lower()
            if provider == "openrouter":
                logger.info(f"⏳ [시스템] OpenRouter rerank 모델을 준비합니다: {RERANK_MODEL}")
                self._ranker = OpenRouterReranker(
                    model=RERANK_MODEL,
                    api_key=OPENROUTER_API_KEY or "",
                    app_url=OPENROUTER_APP_URL,
                    app_title=OPENROUTER_APP_TITLE,
                    data_collection=OPENROUTER_DATA_COLLECTION,
                    timeout=RERANK_TIMEOUT,
                )
            elif provider == "flashrank":
                self._ranker = FlashRankReranker()
            else:
                raise ValueError(f"Unsupported RERANK_PROVIDER: {RERANK_PROVIDER!r}")
        return self._ranker


_ranker_singleton = RankerSingleton()


def get_ranker():
    """Return the configured reranker singleton."""
    return _ranker_singleton.get_ranker()
