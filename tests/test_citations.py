from copy import deepcopy

from src.utils.citations import (
    CITATION_CONTRACT_INVALID,
    CITATION_CONTRACT_LEGACY,
    CITATION_CONTRACT_VALID,
    annotate_document_citation_sources,
    extract_citation_ranks,
    group_sources_by_persisted_document_rank,
    link_citations_to_sources,
    normalize_citation_ranks,
    remove_unavailable_citations,
    remove_unavailable_document_references,
    safe_anchor_prefix,
    source_anchor_id,
    validate_citation_contract,
)


def test_link_citations_to_sources_links_only_available_source_numbers():
    linked = link_citations_to_sources(
        "요약은 [1]과 [2]를 참고했고 [3]은 범위 밖입니다.",
        anchor_prefix="message 1",
        source_count=2,
    )

    assert r"[\[1\]](#message-1-source-1)" in linked
    assert r"[\[2\]](#message-1-source-2)" in linked
    assert "[3]은 범위 밖" in linked


def test_link_citations_to_sources_keeps_existing_markdown_links():
    linked = link_citations_to_sources(
        "기존 링크 [1](https://example.com)와 새 참조 [2]",
        anchor_prefix="live_thread",
        source_count=2,
    )

    assert "[1](https://example.com)" in linked
    assert r"[\[2\]](#live_thread-source-2)" in linked


def test_link_citations_to_sources_links_bracketed_source_labels():
    linked = link_citations_to_sources(
        "씨알푸드 리스크 - [출처: 5]\n범위 밖 [문서 6]",
        anchor_prefix="message_1",
        source_count=5,
    )

    assert "[출처: 5](#message_1-source-5)" in linked
    assert "[문서 6]" in linked


def test_link_citations_to_sources_separates_adjacent_citations():
    linked = link_citations_to_sources(
        "근거 [1][2][3]",
        anchor_prefix="message",
        source_count=3,
    )

    assert (
        r"[\[1\]](#message-source-1) [\[2\]](#message-source-2) "
        r"[\[3\]](#message-source-3)"
    ) in linked


def test_source_anchor_id_sanitizes_prefix():
    assert safe_anchor_prefix("live thread/1") == "live-thread-1"
    assert source_anchor_id("live thread/1", 7) == "live-thread-1-source-7"


def test_remove_unavailable_citations_removes_out_of_range_references():
    cleaned = remove_unavailable_citations(
        "근거 [1], 범위 밖 [3], 출처 라벨 [문서 4], 유효 라벨 [출처: 2]",
        source_count=2,
    )

    assert "[1]" in cleaned
    assert "[출처: 2]" in cleaned
    assert "[3]" not in cleaned
    assert "[문서 4]" not in cleaned


def test_extract_citation_ranks_returns_only_referenced_available_sources():
    ranks = extract_citation_ranks(
        "근거 [1][3], 출처 라벨 [출처: 2], 기존 링크 [4](https://example.com), 범위 밖 [9]",
        source_count=3,
    )

    assert ranks == {1, 2, 3}


def test_document_citation_annotation_is_non_mutating_and_idempotent():
    sources = [
        {"rank": 1, "report_uid": "report-a", "chunk_uid": "chunk-a1"},
        {"rank": 2, "report_uid": "report-a", "chunk_uid": "chunk-a2"},
        {"rank": 3, "report_uid": "report-b", "chunk_uid": "chunk-b1"},
    ]
    original = deepcopy(sources)

    annotated, contract = annotate_document_citation_sources(sources)
    repeated, repeated_contract = annotate_document_citation_sources(sources)

    assert sources == original
    assert annotated == repeated
    assert contract == repeated_contract == {
        "version": 2,
        "rank_kind": "document",
        "passage_count": 3,
        "document_count": 2,
    }
    assert [source["passage_rank"] for source in annotated] == [1, 2, 3]
    assert [source["document_rank"] for source in annotated] == [1, 1, 2]
    assert validate_citation_contract(annotated, contract)["status"] == CITATION_CONTRACT_VALID


def test_document_citation_annotation_keeps_unidentified_sources_distinct():
    annotated, contract = annotate_document_citation_sources(
        [{"rank": 1}, {"rank": 2}]
    )

    assert [source["document_rank"] for source in annotated] == [1, 2]
    assert contract["document_count"] == 2


def test_kakao_reproduction_maps_seven_passages_to_six_documents():
    file_names = [
        "mirae.pdf",
        "hanwha.pdf",
        "kiwoom.pdf",
        "eugene.pdf",
        "hana.pdf",
        "kyobo.pdf",
        "hana.pdf",
    ]
    annotated, contract = annotate_document_citation_sources(
        [
            {"rank": rank, "file_name": file_name}
            for rank, file_name in enumerate(file_names, 1)
        ]
    )

    assert [source["document_rank"] for source in annotated] == [1, 2, 3, 4, 5, 6, 5]
    assert contract == {
        "version": 2,
        "rank_kind": "document",
        "passage_count": 7,
        "document_count": 6,
    }


def test_document_citation_contract_does_not_treat_marked_invalid_data_as_legacy():
    sources = [{"rank": 1, "passage_rank": 1, "document_rank": 1}]

    assert validate_citation_contract(sources, None)["status"] == CITATION_CONTRACT_LEGACY
    assert validate_citation_contract(sources, {"version": 2})["status"] == (
        CITATION_CONTRACT_INVALID
    )
    inconsistent = {
        "version": 2,
        "rank_kind": "document",
        "passage_count": 1,
        "document_count": 2,
    }
    assert validate_citation_contract(sources, inconsistent)["status"] == (
        CITATION_CONTRACT_INVALID
    )

    boolean_ranks = [{"rank": True, "passage_rank": True, "document_rank": True}]
    boolean_contract = {
        "version": 2,
        "rank_kind": "document",
        "passage_count": True,
        "document_count": True,
    }
    assert validate_citation_contract(boolean_ranks, boolean_contract)["status"] == (
        CITATION_CONTRACT_INVALID
    )


def test_v2_grouping_uses_persisted_document_ranks():
    annotated, contract = annotate_document_citation_sources(
        [
            {"rank": 1, "file_name": "a.pdf"},
            {"rank": 2, "file_name": "a.pdf"},
            {"rank": 3, "file_name": "b.pdf"},
        ]
    )

    groups = group_sources_by_persisted_document_rank(annotated, contract)

    assert [group["document_rank"] for group in groups] == [1, 2]
    assert [group["ranks"] for group in groups] == [[1, 2], [3]]


def test_v2_cleanup_removes_only_out_of_range_document_labels_and_citations():
    text = (
        "### 문서 7: 보수적 관점 [5]\n"
        "문서 2: 유효한 관점 [2] [7]\n"
        "문서 8에서 추가로 확인\n"
        "총 문서 7개와 문서 70건을 검토"
    )

    cleaned = remove_unavailable_document_references(text, source_count=6)

    assert cleaned == (
        "### 보수적 관점 [5]\n"
        "문서 2: 유효한 관점 [2] \n"
        "문서에서 추가로 확인\n"
        "총 문서 7개와 문서 70건을 검토"
    )


def test_normalize_citation_ranks_maps_duplicate_chunk_ranks_to_representative():
    normalized = normalize_citation_ranks(
        "같은 문서 근거 [1][2][3], 다른 문서 [4], 출처 라벨 [출처: 2][출처: 3]",
        {1: 1, 2: 1, 3: 1, 4: 4},
    )

    assert normalized == "같은 문서 근거 [1], 다른 문서 [4], 출처 라벨 [출처: 1]"
