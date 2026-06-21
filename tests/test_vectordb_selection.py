from langchain_core.documents import Document

from src.nodes.vectordb import ensure_document_coverage


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
