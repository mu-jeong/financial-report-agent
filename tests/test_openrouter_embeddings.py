from src.llms.embeddings import OpenRouterEmbeddings


class FakeResponse:
    def __init__(self, body, *, status_code=200, text="OK", headers=None):
        self._body = body
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)

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


def test_openrouter_embeddings_retries_429_using_retry_after(monkeypatch):
    responses = iter(
        [
            FakeResponse(
                {},
                status_code=429,
                text='{"error":{"message":"engine overloaded"}}',
                headers={"Retry-After": "7"},
            ),
            FakeResponse({"data": [{"index": 0, "embedding": [1, 2]}]}),
        ]
    )
    sleeps = []

    monkeypatch.setattr(
        "src.llms.embeddings.requests.post",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr("src.llms.embeddings.time.sleep", sleeps.append)

    embeddings = OpenRouterEmbeddings(model="baai/bge-m3", api_key="test-key")

    assert embeddings.embed_documents(["first"]) == [[1.0, 2.0]]
    assert sleeps == [7.0]


def test_openrouter_embeddings_retries_transient_errors_with_backoff(monkeypatch):
    responses = iter(
        [
            FakeResponse({}, status_code=503, text="unavailable"),
            FakeResponse({}, status_code=529, text="overloaded"),
            FakeResponse({"data": [{"index": 0, "embedding": [1, 2]}]}),
        ]
    )
    sleeps = []

    monkeypatch.setattr(
        "src.llms.embeddings.requests.post",
        lambda *args, **kwargs: next(responses),
    )
    monkeypatch.setattr("src.llms.embeddings.time.sleep", sleeps.append)
    monkeypatch.setattr("src.llms.embeddings.random.uniform", lambda low, high: 0.0)

    embeddings = OpenRouterEmbeddings(model="baai/bge-m3", api_key="test-key")

    assert embeddings.embed_documents(["first"]) == [[1.0, 2.0]]
    assert sleeps == [1.0, 2.0]


def test_openrouter_embeddings_does_not_retry_non_transient_error(monkeypatch):
    calls = 0
    sleeps = []

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse({}, status_code=401, text="invalid API key")

    monkeypatch.setattr("src.llms.embeddings.requests.post", fake_post)
    monkeypatch.setattr("src.llms.embeddings.time.sleep", sleeps.append)

    embeddings = OpenRouterEmbeddings(model="baai/bge-m3", api_key="test-key")

    try:
        embeddings.embed_documents(["first"])
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected an OpenRouter request failure")

    assert calls == 1
    assert sleeps == []


def test_openrouter_embeddings_stops_after_retry_budget(monkeypatch):
    calls = 0
    sleeps = []

    def fake_post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse({}, status_code=429, text="overloaded")

    monkeypatch.setattr("src.llms.embeddings.requests.post", fake_post)
    monkeypatch.setattr("src.llms.embeddings.time.sleep", sleeps.append)
    monkeypatch.setattr("src.llms.embeddings.random.uniform", lambda low, high: 0.0)

    embeddings = OpenRouterEmbeddings(
        model="baai/bge-m3",
        api_key="test-key",
        max_retries=2,
    )

    try:
        embeddings.embed_documents(["first"])
    except RuntimeError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("expected an OpenRouter request failure")

    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_openrouter_embeddings_retries_only_the_failed_batch(monkeypatch):
    requested_batches = []
    responses = iter(
        [
            FakeResponse({"data": [{"index": 0, "embedding": [1, 0]}]}),
            FakeResponse({}, status_code=429, text="overloaded"),
            FakeResponse({"data": [{"index": 0, "embedding": [0, 1]}]}),
        ]
    )

    def fake_post(*args, **kwargs):
        requested_batches.append(kwargs["json"]["input"])
        return next(responses)

    monkeypatch.setattr("src.llms.embeddings.requests.post", fake_post)
    monkeypatch.setattr("src.llms.embeddings.time.sleep", lambda delay: None)

    embeddings = OpenRouterEmbeddings(
        model="baai/bge-m3",
        api_key="test-key",
        batch_size=1,
    )

    assert embeddings.embed_documents(["first", "second"]) == [
        [1.0, 0.0],
        [0.0, 1.0],
    ]
    assert requested_batches == [["first"], ["second"], ["second"]]
