"""Deterministic reconstruction of native V2 chunk spans from V1 documents.

The legacy LangChain docstore duplicated an embedding prefix and a child body.
V2 stores neither value on the chunk row: the prefix is profile-defined and the
body is a slice of the immutable parent.  Conversion therefore has to prove one
and only one ordered span assignment for every legacy parent.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from src.retrieval.identity import (
    IdentityError,
    render_embedding_prefix as _render_prefix,
    sha256_text,
)


class ReconstructionError(ValueError):
    """Raised when legacy child text cannot be mapped uniquely and losslessly."""


@dataclass(frozen=True)
class ReconstructedSpan:
    """One proven parent slice in legacy child order."""

    child_order: int
    span_start: int
    span_end: int
    embedding_text_sha256: str


def render_embedding_prefix(template: str, metadata: dict[str, object]) -> str:
    """Preserve the migration error contract around the permanent renderer."""
    try:
        return _render_prefix(template, metadata)
    except IdentityError as exc:
        raise ReconstructionError(str(exc)) from exc


def strip_embedding_prefix(embedding_text: str, expected_prefix: str) -> str:
    """Remove exactly the configured prefix and prove lossless recomposition."""

    if not isinstance(embedding_text, str):
        raise ReconstructionError("legacy embedding text must be a string")
    if not isinstance(expected_prefix, str) or not expected_prefix:
        raise ReconstructionError("expected embedding prefix must be non-empty")
    if not embedding_text.startswith(expected_prefix):
        raise ReconstructionError("legacy embedding text has the wrong profile prefix")
    body = embedding_text[len(expected_prefix) :]
    if not body:
        raise ReconstructionError("legacy child body must be non-empty")
    if expected_prefix + body != embedding_text:
        raise ReconstructionError("legacy embedding text cannot be reconstructed exactly")
    return body


def resolve_ordered_spans(
    parent_content: str,
    embedding_texts: Sequence[str],
    expected_prefix: str,
    *,
    expected_embedding_hashes: Sequence[str] | None = None,
) -> tuple[ReconstructedSpan, ...]:
    """Resolve the unique strictly-increasing sequence of child start offsets.

    Child spans may overlap; only their starts must increase.  All occurrences
    are considered globally.  The dynamic program caps path counts at two,
    because the migration only needs to distinguish no solution, one solution,
    and ambiguity without enumerating a potentially exponential search tree.
    """

    if not isinstance(parent_content, str) or not parent_content:
        raise ReconstructionError("legacy parent content must be non-empty")
    if isinstance(embedding_texts, (str, bytes)):
        raise ReconstructionError("embedding texts must be an ordered sequence")
    texts = tuple(embedding_texts)
    if not texts:
        raise ReconstructionError("legacy parent must contain at least one child")

    if expected_embedding_hashes is None:
        expected_hashes: tuple[str, ...] | None = None
    else:
        if isinstance(expected_embedding_hashes, (str, bytes)):
            raise ReconstructionError("embedding hashes must be an ordered sequence")
        expected_hashes = tuple(expected_embedding_hashes)
        if len(expected_hashes) != len(texts):
            raise ReconstructionError("embedding hash count must match child count")

    bodies: list[str] = []
    hashes: list[str] = []
    occurrences: list[tuple[int, ...]] = []
    for child_order, embedding_text in enumerate(texts):
        body = strip_embedding_prefix(embedding_text, expected_prefix)
        embedding_hash = sha256_text(embedding_text)
        if expected_hashes is not None and embedding_hash != expected_hashes[child_order].lower():
            raise ReconstructionError(
                f"legacy embedding text hash mismatch at child order {child_order}"
            )
        starts = _all_occurrences(parent_content, body)
        if not starts:
            raise ReconstructionError(
                f"legacy child body is absent from its parent at child order {child_order}"
            )
        bodies.append(body)
        hashes.append(embedding_hash)
        occurrences.append(starts)

    # ways[i][j] is the capped number of valid suffix assignments when child i
    # uses occurrences[i][j].  Strictly-increasing starts permit overlapping
    # spans and make a suffix sum sufficient for the transition.
    ways: list[list[int]] = [[] for _ in occurrences]
    ways[-1] = [1] * len(occurrences[-1])
    for child_index in range(len(occurrences) - 2, -1, -1):
        next_starts = occurrences[child_index + 1]
        next_ways = ways[child_index + 1]
        suffix: list[int] = [0] * (len(next_ways) + 1)
        for index in range(len(next_ways) - 1, -1, -1):
            suffix[index] = min(2, suffix[index + 1] + next_ways[index])

        current: list[int] = []
        for start in occurrences[child_index]:
            first_later = bisect_right(next_starts, start)
            current.append(suffix[first_later])
        ways[child_index] = current

    total_solutions = min(2, sum(ways[0]))
    if total_solutions == 0:
        raise ReconstructionError(
            "legacy children have no strictly-increasing global span assignment"
        )
    if total_solutions > 1:
        raise ReconstructionError(
            "legacy children have multiple valid global span assignments"
        )

    selected_starts: list[int] = []
    previous_start = -1
    for child_index, starts in enumerate(occurrences):
        candidates = [
            start
            for occurrence_index, start in enumerate(starts)
            if start > previous_start and ways[child_index][occurrence_index] > 0
        ]
        if len(candidates) != 1:
            # This should be unreachable when the capped total is exactly one,
            # but retaining the assertion turns an implementation defect into a
            # fail-closed conversion error.
            raise ReconstructionError("unique span proof became internally inconsistent")
        selected = candidates[0]
        selected_starts.append(selected)
        previous_start = selected

    return tuple(
        ReconstructedSpan(
            child_order=child_order,
            span_start=start,
            span_end=start + len(bodies[child_order]),
            embedding_text_sha256=hashes[child_order],
        )
        for child_order, start in enumerate(selected_starts)
    )


def _all_occurrences(content: str, body: str) -> tuple[int, ...]:
    starts: list[int] = []
    search_from = 0
    while True:
        start = content.find(body, search_from)
        if start < 0:
            return tuple(starts)
        starts.append(start)
        search_from = start + 1


__all__ = [
    "ReconstructedSpan",
    "ReconstructionError",
    "render_embedding_prefix",
    "resolve_ordered_spans",
    "strip_embedding_prefix",
]
