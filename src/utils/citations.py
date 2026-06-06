"""Utilities for linking answer citation markers to rendered source anchors."""

from __future__ import annotations

import re

_CITATION_RE = re.compile(r"(?<!!)\[(\d{1,3})\](?!\()")
_BRACKETED_SOURCE_LABEL_RE = re.compile(
    r"(?<!!)\[((?:출처|문서)\s*[:：]?\s*)(\d{1,3})\](?!\()"
)
_UNSAFE_ANCHOR_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def safe_anchor_prefix(value: str) -> str:
    """Return a stable, HTML-id-safe prefix."""
    safe = _UNSAFE_ANCHOR_CHARS_RE.sub("-", str(value)).strip("-")
    return safe or "source"


def source_anchor_id(anchor_prefix: str, rank: int) -> str:
    """Build the source anchor id for a rendered source rank."""
    return f"{safe_anchor_prefix(anchor_prefix)}-source-{rank}"


def remove_unavailable_citations(text: str, *, source_count: int) -> str:
    """Remove citation markers that do not have a rendered source."""
    if not text:
        return text

    def keep_or_remove_source_label(match: re.Match[str]) -> str:
        rank = int(match.group(2))
        if 1 <= rank <= source_count:
            return match.group(0)
        return ""

    def keep_or_remove_plain_citation(match: re.Match[str]) -> str:
        rank = int(match.group(1))
        if 1 <= rank <= source_count:
            return match.group(0)
        return ""

    cleaned = _BRACKETED_SOURCE_LABEL_RE.sub(keep_or_remove_source_label, text)
    cleaned = _CITATION_RE.sub(keep_or_remove_plain_citation, cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def extract_citation_ranks(text: str, *, source_count: int | None = None) -> set[int]:
    """Return citation ranks that are actually referenced in answer text.

    Existing markdown links such as ``[1](...)`` are ignored, matching the
    linking/removal helpers' behavior. When ``source_count`` is provided, only
    ranks that have a corresponding retrieved source are returned.
    """
    if not text:
        return set()

    ranks: set[int] = set()
    ranks.update(int(match.group(2)) for match in _BRACKETED_SOURCE_LABEL_RE.finditer(text))
    ranks.update(int(match.group(1)) for match in _CITATION_RE.finditer(text))

    if source_count is None:
        return ranks
    return {rank for rank in ranks if 1 <= rank <= source_count}


def normalize_citation_ranks(text: str, rank_aliases: dict[int, int] | None = None) -> str:
    """Map duplicate chunk citation ranks to one representative document rank.

    ``rank_aliases`` maps an original citation rank to the representative rank
    that should be displayed. This is useful when multiple retrieved chunks come
    from the same PDF/report but the UI should show a single citation number.
    """
    if not text or not rank_aliases:
        return text

    aliases = {int(rank): int(alias) for rank, alias in rank_aliases.items()}

    def replace_source_label(match: re.Match[str]) -> str:
        rank = int(match.group(2))
        alias = aliases.get(rank, rank)
        return f"[{match.group(1)}{alias}]"

    def replace_plain_citation(match: re.Match[str]) -> str:
        rank = int(match.group(1))
        alias = aliases.get(rank, rank)
        return f"[{alias}]"

    normalized = _BRACKETED_SOURCE_LABEL_RE.sub(replace_source_label, text)
    normalized = _CITATION_RE.sub(replace_plain_citation, normalized)
    # Collapse adjacent duplicate citations caused by mapping [1][2][3] -> [1][1][1].
    normalized = re.sub(r"(\[(\d{1,3})\])(?:\s*\[\2\])+", r"\1", normalized)
    normalized = re.sub(
        r"(\[((?:출처|문서)\s*[:：]?\s*)(\d{1,3})\])(?:\s*\[\2\3\])+",
        r"\1",
        normalized,
    )
    return normalized


def link_citations_to_sources(text: str, *, anchor_prefix: str, source_count: int) -> str:
    """Convert ``[1]``-style answer citations to links targeting source anchors.

    Only citation numbers that have a corresponding source are linked. Existing
    markdown links such as ``[1](...)`` are left untouched.
    """
    if not text or source_count <= 0:
        return text

    def replace(match: re.Match[str]) -> str:
        rank = int(match.group(1))
        if rank < 1 or rank > source_count:
            return match.group(0)
        return f"[\\[{rank}\\]](#{source_anchor_id(anchor_prefix, rank)})"

    def replace_source_label(match: re.Match[str]) -> str:
        rank = int(match.group(2))
        if rank < 1 or rank > source_count:
            return match.group(0)
        return f"[{match.group(1)}{rank}](#{source_anchor_id(anchor_prefix, rank)})"

    linked = _BRACKETED_SOURCE_LABEL_RE.sub(replace_source_label, text)
    linked = _CITATION_RE.sub(replace, linked)
    # Adjacent citations such as [1][2] render as cramped links. Add a small
    # markdown-space between converted citation links while leaving normal text
    # untouched.
    return re.sub(r"(\]\(#[^)]+\))(?=\[\\\[\d{1,3}\\\]\]\(#)", r"\1 ", linked)
