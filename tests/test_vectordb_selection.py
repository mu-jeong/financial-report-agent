from langchain_core.documents import Document

from src.nodes.vectordb import (
    ensure_document_coverage,
    required_file_names_from_prior_scope,
    select_top_passages,
)


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


def test_select_top_passages_applies_document_coverage_for_date_bounded_target_summary(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 3)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    docs_with_scores = [
        (
            Document(
                page_content="SK하이닉스 6월 22일 chunk 1",
                metadata={
                    "file_name": "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
                    "target_name": "SK하이닉스",
                    "report_date": "2026-06-22",
                    "report_type": "company",
                },
            ),
            0.1,
        ),
        (
            Document(
                page_content="SK하이닉스 6월 22일 chunk 2",
                metadata={
                    "file_name": "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
                    "target_name": "SK하이닉스",
                    "report_date": "2026-06-22",
                    "report_type": "company",
                },
            ),
            0.2,
        ),
        (
            Document(
                page_content="SK하이닉스 6월 25일 ADR",
                metadata={
                    "file_name": "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
                    "target_name": "SK하이닉스",
                    "report_date": "2026-06-25",
                    "report_type": "company",
                },
            ),
            0.3,
        ),
    ]

    selected, metrics = select_top_passages(
        "해당 기간 내에 발간된 sk하이닉스에 대한 리포트 정리해서 내용을 알려줘",
        docs_with_scores,
        search_filters={
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-25",
            "target_name": "SK하이닉스",
            "report_type": "company",
        },
    )

    assert {item["meta"]["file_name"] for item in selected} == {
        "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
        "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
    }
    assert metrics == {
        "document_coverage_applied": True,
        "document_coverage_reason": "date_bounded_target_report_set",
    }


def test_required_file_names_from_prior_scope_keeps_matching_target_period_files_only():
    prior_scope = {
        "file_names": [
            "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
            "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배.pdf",
            "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
            "company_2026-06-25_삼성전자_미래에셋증권_생산능력.pdf",
            "industry_2026-06-22_반도체_iM증권_산업 전망.pdf",
        ]
    }

    required = required_file_names_from_prior_scope(
        "해당 기간 내에 발간된 sk하이닉스에 대한 리포트 정리해서 내용을 알려줘",
        prior_scope,
        {
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-25",
            "target_name": "SK하이닉스",
            "report_type": "company",
        },
    )

    assert required == [
        "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
        "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배.pdf",
        "company_2026-06-25_SK하이닉스_IBK투자증권_ADR 발행.pdf",
    ]


def test_required_file_names_from_prior_scope_keeps_target_files_for_target_with_prior_dates():
    prior_scope = {
        "file_names": [
            "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
            "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배.pdf",
            "company_2026-06-25_삼성전자_미래에셋증권_생산능력.pdf",
        ]
    }

    required = required_file_names_from_prior_scope(
        "SK하이닉스 알려줘",
        prior_scope,
        {
            "report_date_start": "2026-06-22",
            "report_date_end": "2026-06-26",
            "target_name": "SK하이닉스",
            "report_type": "company",
        },
    )

    assert required == [
        "company_2026-06-22_SK하이닉스_iM증권_2Q26 영업이익 전망.pdf",
        "company_2026-06-22_SK하이닉스_한화투자증권_PE 10배.pdf",
    ]


def test_select_top_passages_applies_document_coverage_for_section_followup(monkeypatch):
    import src.nodes.vectordb as vectordb

    monkeypatch.setattr(vectordb, "SEARCH_TOP_K", 3)
    monkeypatch.setattr(vectordb, "USE_RERANKER", False)
    monkeypatch.setattr(vectordb, "RECENCY_WEIGHT", 0)
    docs_with_scores = [
        (
            Document(
                page_content="넥스트바이오메디컬 논문 상세 chunk 1",
                metadata={"file_name": "nextbio.pdf", "target_name": "넥스트바이오메디컬", "report_type": "company"},
            ),
            0.1,
        ),
        (
            Document(
                page_content="넥스트바이오메디컬 논문 상세 chunk 2",
                metadata={"file_name": "nextbio.pdf", "target_name": "넥스트바이오메디컬", "report_type": "company"},
            ),
            0.2,
        ),
        (
            Document(
                page_content="넥스트바이오메디컬 논문 상세 chunk 3",
                metadata={"file_name": "nextbio.pdf", "target_name": "넥스트바이오메디컬", "report_type": "company"},
            ),
            0.3,
        ),
        (
            Document(
                page_content="삼성E&A 수주 모멘텀",
                metadata={"file_name": "samsung_ea.pdf", "target_name": "삼성E&A", "report_type": "company"},
            ),
            0.9,
        ),
        (
            Document(
                page_content="리가켐바이오 ADC 이벤트",
                metadata={"file_name": "ligachem.pdf", "target_name": "리가켐바이오", "report_type": "company"},
            ),
            1.0,
        ),
    ]

    selected, metrics = select_top_passages(
        "개별 종목에 대해 좀 더 자세히 작성해줘",
        docs_with_scores,
        search_filters={
            "report_date_start": "2026-06-15",
            "report_date_end": "2026-06-21",
            "report_type": "company",
        },
        scope_decision={"reason": "matched_prior_section_alias", "matched_section_id": "company"},
    )

    assert {item["meta"]["file_name"] for item in selected} == {
        "nextbio.pdf",
        "samsung_ea.pdf",
        "ligachem.pdf",
    }
    assert metrics == {
        "document_coverage_applied": True,
        "document_coverage_reason": "section_followup_scope",
    }
