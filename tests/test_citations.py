from src.utils.citations import (
    extract_citation_ranks,
    link_citations_to_sources,
    normalize_citation_ranks,
    remove_unavailable_citations,
    safe_anchor_prefix,
    source_anchor_id,
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


def test_normalize_citation_ranks_maps_duplicate_chunk_ranks_to_representative():
    normalized = normalize_citation_ranks(
        "같은 문서 근거 [1][2][3], 다른 문서 [4], 출처 라벨 [출처: 2][출처: 3]",
        {1: 1, 2: 1, 3: 1, 4: 4},
    )

    assert normalized == "같은 문서 근거 [1], 다른 문서 [4], 출처 라벨 [출처: 1]"
