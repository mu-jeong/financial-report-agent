from src.utils.ranker import OpenRouterReranker


class FakeResponse:
    status_code = 200
    text = "OK"

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_openrouter_reranker_posts_documents_and_maps_results(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse(
            {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.5},
                ]
            }
        )

    monkeypatch.setattr("src.utils.ranker.requests.post", fake_post)

    reranker = OpenRouterReranker(
        model="cohere/rerank-v3.5",
        api_key="test-key",
        app_url="https://example.test",
        app_title="finance_llm_test",
        data_collection="deny",
        timeout=12,
    )
    passages = [
        {"text": "first", "meta": {"id": 1}, "score": 1.0},
        {"text": "second", "meta": {"id": 2}, "score": 2.0},
    ]

    result = reranker.rerank("query", passages, top_n=2)

    assert [item["text"] for item in result] == ["second", "first"]
    assert [item["rerank_score"] for item in result] == [0.9, 0.5]
    assert captured["url"] == "https://openrouter.ai/api/v1/rerank"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["headers"]["HTTP-Referer"] == "https://example.test"
    assert captured["headers"]["X-Title"] == "finance_llm_test"
    assert captured["timeout"] == 12
    assert captured["json"] == {
        "model": "cohere/rerank-v3.5",
        "query": "query",
        "documents": ["first", "second"],
        "top_n": 2,
        "provider": {"data_collection": "deny"},
    }
