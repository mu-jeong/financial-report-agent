"""Deterministic reconstruction of native V2 chunk spans from V1 documents.

The legacy LangChain docstore duplicated an embedding prefix and a child body.
V2 stores neither value on the chunk row: the prefix is profile-defined and the
body is a slice of the immutable parent.  Conversion therefore has to prove one
and only one ordered span assignment for every legacy parent.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from src.retrieval.identity import (
    IdentityError,
    canonical_json,
    render_embedding_prefix as _render_prefix,
    sha256_text,
)


class ReconstructionError(ValueError):
    """Raised when legacy child text cannot be mapped uniquely and losslessly."""


class AmbiguousSpanError(ReconstructionError):
    """Raised when occurrence matching proves more than one global assignment."""


@dataclass(frozen=True)
class ReconstructedSpan:
    """One proven parent slice in legacy child order."""

    child_order: int
    span_start: int
    span_end: int
    embedding_text_sha256: str


@dataclass(frozen=True)
class LegacyReplayPolicy:
    """Complete semantic input for the migration-owned V1 splitter replay."""

    chunk_size: int
    chunk_overlap: int
    separators: tuple[str, ...]
    keep_separator: bool | Literal["start", "end"]
    strip_whitespace: bool
    is_separator_regex: bool
    length_function: Literal["python-len"] = "python-len"

    @property
    def policy_id(self) -> str:
        return "legacy-recursive-splitter-v1"

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "chunk_overlap": self.chunk_overlap,
            "chunk_size": self.chunk_size,
            "is_separator_regex": self.is_separator_regex,
            "keep_separator": self.keep_separator,
            "length_function": self.length_function,
            "policy_id": self.policy_id,
            "separators": list(self.separators),
            "strip_whitespace": self.strip_whitespace,
        }

    @property
    def policy_sha256(self) -> str:
        return sha256_text(canonical_json(self.canonical_payload))

    def __post_init__(self) -> None:
        if (
            isinstance(self.chunk_size, bool)
            or not isinstance(self.chunk_size, int)
            or self.chunk_size <= 0
        ):
            raise ReconstructionError("replay chunk size must be a positive integer")
        if (
            isinstance(self.chunk_overlap, bool)
            or not isinstance(self.chunk_overlap, int)
            or self.chunk_overlap < 0
            or self.chunk_overlap >= self.chunk_size
        ):
            raise ReconstructionError(
                "replay chunk overlap must be non-negative and smaller than size"
            )
        if not self.separators or not all(
            isinstance(value, str) for value in self.separators
        ):
            raise ReconstructionError("replay separators must be a non-empty string sequence")
        if self.keep_separator not in (True, False, "start", "end"):
            raise ReconstructionError("replay keep_separator is invalid")
        if not isinstance(self.strip_whitespace, bool):
            raise ReconstructionError("replay strip_whitespace must be boolean")
        if not isinstance(self.is_separator_regex, bool):
            raise ReconstructionError("replay is_separator_regex must be boolean")
        if self.length_function != "python-len":
            raise ReconstructionError("replay length function must be python-len")


@dataclass(frozen=True)
class SpanAmbiguity:
    global_assignment_cardinality: Literal["multiple"]
    ambiguous_child_order: int
    local_occurrence_count: int


@dataclass(frozen=True)
class SpanResolution:
    spans: tuple[ReconstructedSpan, ...]
    method: Literal["ordered-span-v1", "legacy-recursive-splitter-v1"]
    ambiguity: SpanAmbiguity | None = None


@dataclass(frozen=True)
class _SpanAnalysis:
    bodies: tuple[str, ...]
    hashes: tuple[str, ...]
    occurrences: tuple[tuple[int, ...], ...]
    ways: tuple[tuple[int, ...], ...]
    total_solutions: int


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

    analysis = _analyze_spans(
        parent_content,
        embedding_texts,
        expected_prefix,
        expected_embedding_hashes=expected_embedding_hashes,
    )
    if analysis.total_solutions == 0:
        raise ReconstructionError(
            "legacy children have no strictly-increasing global span assignment"
        )
    if analysis.total_solutions > 1:
        raise AmbiguousSpanError(
            "legacy children have multiple valid global span assignments"
        )

    return _spans_from_starts(analysis, _select_unique_starts(analysis))


def resolve_ordered_spans_with_replay(
    parent_content: str,
    embedding_texts: Sequence[str],
    expected_prefix: str,
    *,
    replay_policy: LegacyReplayPolicy,
    expected_embedding_hashes: Sequence[str] | None = None,
) -> SpanResolution:
    """Use frozen V1 replay only when the ordinary global proof is ambiguous."""

    analysis = _analyze_spans(
        parent_content,
        embedding_texts,
        expected_prefix,
        expected_embedding_hashes=expected_embedding_hashes,
    )
    if analysis.total_solutions == 0:
        raise ReconstructionError(
            "legacy children have no strictly-increasing global span assignment"
        )
    if analysis.total_solutions == 1:
        spans = _spans_from_starts(analysis, _select_unique_starts(analysis))
        return SpanResolution(spans=spans, method="ordered-span-v1")

    replayed_bodies, replayed_starts = _frozen_split_with_starts(
        parent_content,
        replay_policy,
    )
    if replayed_bodies != analysis.bodies:
        raise ReconstructionError(
            "frozen legacy replay does not exactly reproduce every legacy child"
        )
    if any(
        parent_content[start : start + len(body)] != body
        for body, start in zip(replayed_bodies, replayed_starts, strict=True)
    ):
        raise ReconstructionError("frozen legacy replay produced a non-exact parent slice")
    if any(left >= right for left, right in zip(replayed_starts, replayed_starts[1:])):
        raise ReconstructionError("frozen legacy replay starts are not strictly increasing")

    ambiguous_child_order = _first_globally_ambiguous_child(analysis)
    return SpanResolution(
        spans=_spans_from_starts(analysis, replayed_starts),
        method="legacy-recursive-splitter-v1",
        ambiguity=SpanAmbiguity(
            global_assignment_cardinality="multiple",
            ambiguous_child_order=ambiguous_child_order,
            local_occurrence_count=len(analysis.occurrences[ambiguous_child_order]),
        ),
    )


def frozen_legacy_split(
    parent_content: str,
    policy: LegacyReplayPolicy,
) -> tuple[str, ...]:
    """Return chunks from the migration-owned, dependency-independent V1 replay."""

    return _frozen_split_text(parent_content, policy.separators, policy)


def legacy_replay_policy_from_mapping(
    value: Mapping[str, object],
) -> LegacyReplayPolicy:
    """Build a frozen replay policy without consulting runtime defaults."""

    if not isinstance(value, Mapping):
        raise ReconstructionError("legacy replay requires a child policy mapping")
    required = {"algorithm", "chunk_overlap", "chunk_size", "separators"}
    missing = sorted(required - set(value))
    if missing:
        raise ReconstructionError(
            f"legacy replay child policy is missing semantic field: {missing[0]}"
        )
    if value["algorithm"] != "langchain-recursive-v1":
        raise ReconstructionError("legacy replay child policy algorithm is unsupported")
    frozen_semantics = {
        "is_separator_regex": False,
        "keep_separator": True,
        "length_function": "python-len",
        "strip_whitespace": True,
    }
    for field, expected in frozen_semantics.items():
        if field in value and value[field] != expected:
            raise ReconstructionError(
                f"legacy replay child policy has unsupported {field}"
            )
    separators = value["separators"]
    if isinstance(separators, (str, bytes)) or not isinstance(separators, Sequence):
        raise ReconstructionError("legacy replay separators must be an ordered sequence")
    return LegacyReplayPolicy(
        chunk_size=value["chunk_size"],  # type: ignore[arg-type]
        chunk_overlap=value["chunk_overlap"],  # type: ignore[arg-type]
        separators=tuple(separators),  # type: ignore[arg-type]
        keep_separator=True,
        strip_whitespace=True,
        is_separator_regex=False,
        length_function="python-len",
    )


def _analyze_spans(
    parent_content: str,
    embedding_texts: Sequence[str],
    expected_prefix: str,
    *,
    expected_embedding_hashes: Sequence[str] | None,
) -> _SpanAnalysis:
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

    ways: list[list[int]] = [[] for _ in occurrences]
    ways[-1] = [1] * len(occurrences[-1])
    for child_index in range(len(occurrences) - 2, -1, -1):
        next_starts = occurrences[child_index + 1]
        next_ways = ways[child_index + 1]
        suffix: list[int] = [0] * (len(next_ways) + 1)
        for index in range(len(next_ways) - 1, -1, -1):
            suffix[index] = min(2, suffix[index + 1] + next_ways[index])
        ways[child_index] = [
            suffix[bisect_right(next_starts, start)]
            for start in occurrences[child_index]
        ]
    return _SpanAnalysis(
        bodies=tuple(bodies),
        hashes=tuple(hashes),
        occurrences=tuple(occurrences),
        ways=tuple(tuple(row) for row in ways),
        total_solutions=min(2, sum(ways[0])),
    )


def _select_unique_starts(analysis: _SpanAnalysis) -> tuple[int, ...]:

    selected_starts: list[int] = []
    previous_start = -1
    for child_index, starts in enumerate(analysis.occurrences):
        candidates = [
            start
            for occurrence_index, start in enumerate(starts)
            if start > previous_start and analysis.ways[child_index][occurrence_index] > 0
        ]
        if len(candidates) != 1:
            # This should be unreachable when the capped total is exactly one,
            # but retaining the assertion turns an implementation defect into a
            # fail-closed conversion error.
            raise ReconstructionError("unique span proof became internally inconsistent")
        selected = candidates[0]
        selected_starts.append(selected)
        previous_start = selected
    return tuple(selected_starts)


def _first_globally_ambiguous_child(analysis: _SpanAnalysis) -> int:
    reachable: list[list[bool]] = [[] for _ in analysis.occurrences]
    reachable[0] = [True] * len(analysis.occurrences[0])
    for child_index in range(1, len(analysis.occurrences)):
        previous = analysis.occurrences[child_index - 1]
        previous_reachable = reachable[child_index - 1]
        reachable[child_index] = [
            any(
                can_reach and previous_start < start
                for previous_start, can_reach in zip(
                    previous,
                    previous_reachable,
                    strict=True,
                )
            )
            for start in analysis.occurrences[child_index]
        ]
    for child_index, starts in enumerate(analysis.occurrences):
        valid_choices = sum(
            can_reach and analysis.ways[child_index][occurrence_index] > 0
            for occurrence_index, can_reach in enumerate(reachable[child_index])
        )
        if valid_choices > 1:
            return child_index
    raise ReconstructionError("ambiguous span proof became internally inconsistent")


def _spans_from_starts(
    analysis: _SpanAnalysis,
    starts: Sequence[int],
) -> tuple[ReconstructedSpan, ...]:
    return tuple(
        ReconstructedSpan(
            child_order=child_order,
            span_start=start,
            span_end=start + len(analysis.bodies[child_order]),
            embedding_text_sha256=analysis.hashes[child_order],
        )
        for child_order, start in enumerate(starts)
    )


def _frozen_split_with_starts(
    text: str,
    policy: LegacyReplayPolicy,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    chunks = frozen_legacy_split(text, policy)
    starts: list[int] = []
    index = 0
    previous_chunk_len = 0
    for chunk in chunks:
        offset = index + previous_chunk_len - policy.chunk_overlap
        index = text.find(chunk, max(0, offset))
        if index < 0:
            raise ReconstructionError("frozen legacy replay cannot locate its exact chunk")
        starts.append(index)
        previous_chunk_len = len(chunk)
    return chunks, tuple(starts)


def _frozen_split_text(
    text: str,
    separators: Sequence[str],
    policy: LegacyReplayPolicy,
) -> tuple[str, ...]:
    separator = separators[-1]
    remaining: Sequence[str] = ()
    for index, candidate in enumerate(separators):
        pattern = candidate if policy.is_separator_regex else re.escape(candidate)
        if not candidate:
            separator = candidate
            break
        if re.search(pattern, text):
            separator = candidate
            remaining = separators[index + 1 :]
            break
    pattern = separator if policy.is_separator_regex else re.escape(separator)
    splits = _frozen_regex_split(text, pattern, policy.keep_separator)
    final: list[str] = []
    good: list[str] = []
    merge_separator = "" if policy.keep_separator else separator
    for split in splits:
        if len(split) < policy.chunk_size:
            good.append(split)
            continue
        if good:
            final.extend(_frozen_merge(good, merge_separator, policy))
            good = []
        if remaining:
            final.extend(_frozen_split_text(split, remaining, policy))
        else:
            final.append(split)
    if good:
        final.extend(_frozen_merge(good, merge_separator, policy))
    return tuple(final)


def _frozen_regex_split(
    text: str,
    separator: str,
    keep_separator: bool | Literal["start", "end"],
) -> list[str]:
    if not separator:
        return list(text)
    if not keep_separator:
        return [item for item in re.split(separator, text) if item]
    raw = re.split(f"({separator})", text)
    if keep_separator == "end":
        splits = [raw[index] + raw[index + 1] for index in range(0, len(raw) - 1, 2)]
        if len(raw) % 2 == 0:
            splits += raw[-1:]
        splits = [*splits, raw[-1]]
    else:
        splits = [raw[index] + raw[index + 1] for index in range(1, len(raw), 2)]
        if len(raw) % 2 == 0:
            splits += raw[-1:]
        splits = [raw[0], *splits]
    return [item for item in splits if item]


def _frozen_merge(
    splits: Sequence[str],
    separator: str,
    policy: LegacyReplayPolicy,
) -> list[str]:
    separator_len = len(separator)
    docs: list[str] = []
    current: list[str] = []
    total = 0
    for split in splits:
        split_len = len(split)
        if total + split_len + (separator_len if current else 0) > policy.chunk_size:
            if current:
                joined = separator.join(current)
                if policy.strip_whitespace:
                    joined = joined.strip()
                if joined:
                    docs.append(joined)
                while total > policy.chunk_overlap or (
                    total + split_len + (separator_len if current else 0)
                    > policy.chunk_size
                    and total > 0
                ):
                    total -= len(current[0]) + (separator_len if len(current) > 1 else 0)
                    current = current[1:]
        current.append(split)
        total += split_len + (separator_len if len(current) > 1 else 0)
    joined = separator.join(current)
    if policy.strip_whitespace:
        joined = joined.strip()
    if joined:
        docs.append(joined)
    return docs


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
    "AmbiguousSpanError",
    "LegacyReplayPolicy",
    "ReconstructedSpan",
    "ReconstructionError",
    "SpanAmbiguity",
    "SpanResolution",
    "frozen_legacy_split",
    "legacy_replay_policy_from_mapping",
    "render_embedding_prefix",
    "resolve_ordered_spans",
    "resolve_ordered_spans_with_replay",
    "strip_embedding_prefix",
]
