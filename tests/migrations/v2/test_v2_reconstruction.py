from __future__ import annotations

import pytest

from src.migrations.v2.reconstruct import (
    ReconstructionError,
    render_embedding_prefix,
    resolve_ordered_spans,
    strip_embedding_prefix,
)
from src.retrieval.identity import sha256_text


PREFIX = "[Company: A, Title: Result]\n"


def test_prefix_rendering_and_stripping_are_exact():
    metadata = {"target_name": "A", "title": "Result"}
    prefix = render_embedding_prefix(
        "[Company: {target_name}, Title: {title}]\n", metadata
    )

    assert prefix == PREFIX
    assert strip_embedding_prefix(prefix + "body", prefix) == "body"


def test_prefix_failure_blocks_wrong_report_or_missing_profile_field():
    with pytest.raises(ReconstructionError, match="cannot be rendered"):
        render_embedding_prefix("{missing}\n", {"title": "Result"})
    with pytest.raises(ReconstructionError, match="wrong profile prefix"):
        strip_embedding_prefix("[Company: B]\nbody", PREFIX)


def test_resolver_accepts_overlapping_children_with_increasing_starts():
    parent = "abcdefghij"
    texts = [PREFIX + "abcdef", PREFIX + "defghi", PREFIX + "ghij"]

    spans = resolve_ordered_spans(parent, texts, PREFIX)

    assert [(span.span_start, span.span_end) for span in spans] == [
        (0, 6),
        (3, 9),
        (6, 10),
    ]
    assert spans[1].span_start < spans[0].span_end
    assert [span.embedding_text_sha256 for span in spans] == [
        sha256_text(text) for text in texts
    ]


def test_repeated_body_is_accepted_when_global_order_makes_one_sequence_unique():
    parent = "same--middle--same"
    texts = [PREFIX + "same", PREFIX + "middle", PREFIX + "same"]

    spans = resolve_ordered_spans(parent, texts, PREFIX)

    assert [span.span_start for span in spans] == [0, 6, 14]


def test_multiple_global_sequences_fail_closed():
    parent = "same--same--same"
    texts = [PREFIX + "same", PREFIX + "same"]

    with pytest.raises(ReconstructionError, match="multiple valid"):
        resolve_ordered_spans(parent, texts, PREFIX)


def test_missing_or_non_monotonic_sequence_fails_closed():
    with pytest.raises(ReconstructionError, match="absent from its parent"):
        resolve_ordered_spans("alpha", [PREFIX + "missing"], PREFIX)
    with pytest.raises(ReconstructionError, match="no strictly-increasing"):
        resolve_ordered_spans(
            "second--first",
            [PREFIX + "first", PREFIX + "second"],
            PREFIX,
        )


def test_expected_embedding_hash_must_match_every_child():
    text = PREFIX + "body"
    with pytest.raises(ReconstructionError, match="hash mismatch"):
        resolve_ordered_spans(
            "body",
            [text],
            PREFIX,
            expected_embedding_hashes=["00" * 32],
        )

