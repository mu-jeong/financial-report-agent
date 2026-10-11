"""Company summaries must combine report evidence before answer generation."""

import pytest
from langchain_core.documents import Document
from langchain_core.messages import AIMessage

from src.nodes import vectordb, vectordb_comparison


def _documents():
    return [
        (Document(page_content=text, metadata={
            "file_name": file_name, "report_uid": file_name,
            "target_name": "Alpha", "broker": broker,
            "report_date": "2026-10-01", "title": "실적 전망",
            "report_type": "company",
        }), score)
        for text, file_name, broker, score in (
            ("수주 증가로 매출 성장이 예상된다.", "a.pdf", "증권사 A", 0.1),
            ("수주 증가의 추가 근거.", "a.pdf", "증권사 A", 0.2),
            ("매출은 성장하지만 원가 상승으로 이익률 개선은 제한된다.", "b.pdf", "증권사 B", 0.3),
        )
    ]


@pytest.mark.parametrize("summary_request", [
    "리포트 정리해서 알려줘", "리포트를 취합해서 알려줘",
    "보고서를 종합해서 알려줘", "리포트 요약해줘",
    "자료를 종합해줘", "문서를 취합해줘",
])
def test_company_summary_selects_distinct_reports_without_a_date_filter(monkeypatch, summary_request):
    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 2)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)

    passages, metrics = vectordb.select_top_passages(
        f"Alpha {summary_request}", _documents(), search_filters={"target_name": "Alpha"},
    )

    assert {item["meta"]["file_name"] for item in passages} == {"a.pdf", "b.pdf"}
    assert metrics["document_coverage_applied"] is True


def test_company_summary_keeps_distinct_reports_when_candidates_exceed_budget(monkeypatch):
    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 2)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    documents = _documents() + [(Document(page_content="추가 조건", metadata={
        **_documents()[2][0].metadata, "file_name": "c.pdf", "report_uid": "c.pdf",
    }), 0.4)]

    passages, metrics = vectordb.select_top_passages(
        "Alpha 리포트 종합해줘", documents,
        search_filters={"target_name": "Alpha"},
    )

    assert [item["meta"]["file_name"] for item in passages] == ["a.pdf", "b.pdf"]
    assert metrics["document_coverage_applied"] is True


def test_over_budget_coverage_preserves_ranked_representatives_and_explicit_scope():
    documents = _documents() + [(Document(page_content="additional evidence", metadata={
        **_documents()[2][0].metadata, "file_name": "c.pdf", "report_uid": "c.pdf",
    }), 0.4)]
    ranked = [
        {"text": documents[index][0].page_content, "score": score, "meta": documents[index][0].metadata}
        for index, score in ((2, 0.9), (0, 0.8))
    ]
    assert vectordb.ensure_document_coverage(ranked, documents, max_passages=2) == ranked
    duplicates = [{"text": doc.page_content, "score": score, "meta": doc.metadata} for doc, score in documents[:2]]
    assert vectordb.ensure_document_coverage(
        duplicates, documents, max_passages=2, required_file_names=["a.pdf", "b.pdf", "c.pdf"],
    ) == duplicates


@pytest.mark.parametrize("path", ["single_company", "comparison"])
@pytest.mark.parametrize("query", ["Alpha 리포트 정리해서 알려줘", "Alpha 실적 전망"])
def test_generation_receives_company_synthesis_policy_and_both_report_sources(monkeypatch, path, query):
    question = "Alpha 리포트 정리해서 알려줘"
    captured = []

    class Model:
        def bind_tools(self, _tools):
            return self

        def invoke(self, messages):
            captured.append(messages[0].content)
            return AIMessage(content="매출 성장이 기대되지만 이익률 개선에는 원가 부담이 남습니다. [1] [2]")

    module = vectordb if path == "single_company" else vectordb_comparison
    monkeypatch.setattr(module, "build_chat_model", lambda **_kwargs: Model())
    documents = _documents()
    if path == "single_company":
        monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 2)
        monkeypatch.setattr(vectordb, "USE_RERANKER", False)
        monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
        monkeypatch.setattr(vectordb, "_retrieve_docs_with_scores", lambda *_args, **_kwargs: (documents, {}))
        result = vectordb.vectordb_node({
            "question": question, "rewritten_query": query,
            "search_filters": {"target_name": "Alpha", "report_type": "company"},
        })
        answer = result["generation"]
        assert result["citation_contract"]["document_count"] == 2
    else:
        candidates = [{
            "stable_id": file_doc.metadata["file_name"], "retrieval_rank": index,
            "target_name": "Alpha", "text": file_doc.page_content,
            "score": score, "meta": file_doc.metadata,
        } for index, (file_doc, score) in enumerate((documents[0], documents[2]))]
        sources, contract = vectordb_comparison.annotate_document_citation_sources([
            {"rank": index, **item["meta"]} for index, item in enumerate(candidates, 1)
        ])
        answer, _messages, _metrics = vectordb_comparison._synthesize_answer(
            question, query, candidates, [], sources, contract,
        )

    assert len(captured) == 1
    prompt = captured[0]
    assert "기업 중심의 종합 분석" in prompt
    assert "증권사별·리포트별 요약을 차례로 나열하지 마세요" in prompt
    assert "사용자가 증권사별·리포트별 정리나 비교를 명시적으로 요청한 경우" in prompt
    assert "수주 증가로 매출 성장이 예상된다." in prompt
    assert "원가 상승으로 이익률 개선은 제한된다." in prompt
    assert "증권사: 증권사 A" in prompt
    assert "증권사: 증권사 B" in prompt
    assert "--- 문서 1 ---" in prompt and "--- 문서 2 ---" in prompt
    assert "[1]" in answer and "[2]" in answer
