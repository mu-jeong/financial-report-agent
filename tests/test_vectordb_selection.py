from langchain_core.documents import Document

from src.nodes.vectordb import ensure_document_coverage, select_top_passages


def test_ensure_document_coverage_keeps_small_filtered_document_set():
    docs_with_scores = [
        (
            Document(
                page_content="한화 chunk 1",
                metadata={"file_name": "hanwha.pdf", "broker": "한화투자증권"},
            ),
            0.1,
        ),
        (
            Document(
                page_content="한화 chunk 2",
                metadata={"file_name": "hanwha.pdf", "broker": "한화투자증권"},
            ),
            0.2,
        ),
        (
            Document(
                page_content="유안타 chunk 1",
                metadata={"file_name": "yuanta.pdf", "broker": "유안타증권"},
            ),
            0.3,
        ),
    ]
    selected = [
        {
            "text": "한화 chunk 1",
            "score": 0.1,
            "meta": {"file_name": "hanwha.pdf", "broker": "한화투자증권"},
        },
        {
            "text": "한화 chunk 2",
            "score": 0.2,
            "meta": {"file_name": "hanwha.pdf", "broker": "한화투자증권"},
        },
    ]

    covered = ensure_document_coverage(selected, docs_with_scores, max_passages=2)

    assert {item["meta"]["file_name"] for item in covered} == {"hanwha.pdf", "yuanta.pdf"}


def test_ensure_document_coverage_honors_explicit_required_file_scope():
    docs_with_scores = [
        (
            Document(page_content="hanwha recent", metadata={"file_name": "hanwha.pdf"}),
            0.1,
        ),
        (
            Document(page_content="hanwha duplicate", metadata={"file_name": "hanwha.pdf"}),
            0.2,
        ),
        (
            Document(page_content="yuanta selected by prior list", metadata={"file_name": "yuanta.pdf"}),
            0.9,
        ),
        (
            Document(page_content="unrelated", metadata={"file_name": "other.pdf"}),
            0.05,
        ),
    ]
    selected = [
        {"text": "hanwha recent", "score": 0.1, "meta": {"file_name": "hanwha.pdf"}},
        {"text": "hanwha duplicate", "score": 0.2, "meta": {"file_name": "hanwha.pdf"}},
    ]

    covered = ensure_document_coverage(
        selected,
        docs_with_scores,
        max_passages=2,
        required_file_names=["hanwha.pdf", "yuanta.pdf"],
    )

    assert {item["meta"]["file_name"] for item in covered} == {"hanwha.pdf", "yuanta.pdf"}

def test_select_top_passages_skips_document_coverage_for_single_target_deep_dive(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 2)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    docs_with_scores = [
        (
            Document(page_content="삼성전자 HBM 전망 1", metadata={"file_name": "samsung_a.pdf", "target_name": "삼성전자"}),
            0.1,
        ),
        (
            Document(page_content="삼성전자 HBM 전망 2", metadata={"file_name": "samsung_a.pdf", "target_name": "삼성전자"}),
            0.2,
        ),
        (
            Document(page_content="다른 회사 HBM 전망", metadata={"file_name": "other.pdf", "target_name": "다른회사"}),
            0.3,
        ),
    ]

    selected, metrics = select_top_passages(
        "삼성전자 HBM 전망 자세히 알려줘",
        docs_with_scores,
        search_filters={"target_name": "삼성전자"},
    )

    assert [item["meta"]["file_name"] for item in selected] == ["samsung_a.pdf", "samsung_a.pdf"]
    assert metrics == {
        "document_coverage_applied": False,
        "document_coverage_reason": "single_target_deep_dive",
    }


def test_select_top_passages_applies_document_coverage_for_multi_document_intent(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 2)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    docs_with_scores = [
        (
            Document(page_content="삼성전자 리포트 A chunk 1", metadata={"file_name": "samsung_a.pdf", "target_name": "삼성전자"}),
            0.1,
        ),
        (
            Document(page_content="삼성전자 리포트 A chunk 2", metadata={"file_name": "samsung_a.pdf", "target_name": "삼성전자"}),
            0.2,
        ),
        (
            Document(page_content="삼성전자 리포트 B", metadata={"file_name": "samsung_b.pdf", "target_name": "삼성전자"}),
            0.3,
        ),
    ]

    selected, metrics = select_top_passages(
        "이번 주 삼성전자 리포트들 각각 주요 내용 정리해줘",
        docs_with_scores,
        search_filters={"target_name": "삼성전자"},
    )

    assert {item["meta"]["file_name"] for item in selected} == {"samsung_a.pdf", "samsung_b.pdf"}
    assert metrics == {
        "document_coverage_applied": True,
        "document_coverage_reason": "multi_document_intent",
    }

