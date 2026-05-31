from src.llms.embeddings import OpenRouterEmbeddings


class FakeResponse:
    status_code = 200
    text = "OK"

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_openrouter_embeddings_posts_document_batch(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResponse(
            {
                "data": [
                    {"index": 1, "embedding": [3, 4]},
                    {"index": 0, "embedding": [1, 2]},
                ]
            }
        )

    monkeypatch.setattr("src.llms.embeddings.requests.post", fake_post)

    embeddings = OpenRouterEmbeddings(
        model="baai/bge-m3",
        api_key="test-key",
        batch_size=10,
        app_url="https://example.test",
        app_title="finance_llm_test",
        data_collection="deny",
    )

    result = embeddings.embed_documents(["first", "second"])

    assert result == [[1.0, 2.0], [3.0, 4.0]]
    assert captured["url"] == "https://openrouter.ai/api/v1/embeddings"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["headers"]["HTTP-Referer"] == "https://example.test"
    assert captured["headers"]["X-Title"] == "finance_llm_test"
    assert captured["json"] == {
        "model": "baai/bge-m3",
        "input": ["first", "second"],
        "input_type": "search_document",
        "encoding_format": "float",
        "provider": {"data_collection": "deny"},
    }


def test_openrouter_embeddings_posts_query(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["json"] = json
        return FakeResponse({"data": [{"index": 0, "embedding": [0.5, 0.25]}]})

    monkeypatch.setattr("src.llms.embeddings.requests.post", fake_post)

    embeddings = OpenRouterEmbeddings(model="baai/bge-m3", api_key="test-key")

    assert embeddings.embed_query("query") == [0.5, 0.25]
    assert captured["json"]["input"] == "query"
    assert captured["json"]["input_type"] == "search_query"
