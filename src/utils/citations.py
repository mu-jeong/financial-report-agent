"""Utilities for linking answer citation markers to rendered source anchors."""

from __future__ import annotations

import re

_CITATION_RE = re.compile(r"(?<!!)\[(\d{1,3})\](?!\()")
_UNSAFE_ANCHOR_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def safe_anchor_prefix(value: str) -> str:
    """Return a stable, HTML-id-safe prefix."""
    safe = _UNSAFE_ANCHOR_CHARS_RE.sub("-", str(value)).strip("-")
    return safe or "source"


def source_anchor_id(anchor_prefix: str, rank: int) -> str:
    """Build the source anchor id for a rendered source rank."""
    return f"{safe_anchor_prefix(anchor_prefix)}-source-{rank}"


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

    linked = _CITATION_RE.sub(replace, text)
    # Adjacent citations such as [1][2] render as cramped links. Add a small
    # markdown-space between converted citation links while leaving normal text
    # untouched.
    return re.sub(r"(\]\(#[^)]+\))(?=\[\\\[\d{1,3}\\\]\]\(#)", r"\1 ", linked)
