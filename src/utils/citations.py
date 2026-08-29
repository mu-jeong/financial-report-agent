"""Utilities for linking answer citation markers to rendered source anchors."""

from __future__ import annotations

import re
from typing import Any

_CITATION_RE = re.compile(r"(?<!!)\[(\d{1,3})\](?!\()")
_BRACKETED_SOURCE_LABEL_RE = re.compile(
    r"(?<!!)\[((?:출처|문서)\s*[:：]?\s*)(\d{1,3})\](?!\()"
)
_UNSAFE_ANCHOR_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_DOCUMENT_LABEL_RE = re.compile(
    r"\ubb38\uc11c\s+(?P<rank>\d{1,3})\s*[:\uff1a]\s*"
)
_DOCUMENT_RANK_RE = re.compile(
    r"\ubb38\uc11c\s+(?P<rank>\d{1,3})(?!\d|\s*(?:\uac1c|\uac74|\uc885|\ud3b8))"
)

CITATION_CONTRACT_VERSION = 2
CITATION_RANK_KIND_DOCUMENT = "document"
CITATION_CONTRACT_VALID = "valid"
CITATION_CONTRACT_LEGACY = "legacy"
CITATION_CONTRACT_INVALID = "invalid"


def safe_anchor_prefix(value: str) -> str:
    """Return a stable, HTML-id-safe prefix."""
    safe = _UNSAFE_ANCHOR_CHARS_RE.sub("-", str(value)).strip("-")
    return safe or "source"


def source_anchor_id(anchor_prefix: str, rank: int) -> str:
    """Build the source anchor id for a rendered source rank."""
    return f"{safe_anchor_prefix(anchor_prefix)}-source-{rank}"


def source_rank(info: dict[str, Any], fallback_rank: int) -> int:
    """Return a safe positive original retrieval rank for a source item."""
    try:
        rank = int(info.get("rank", fallback_rank))
    except (TypeError, ValueError):
        rank = fallback_rank
    return max(rank, 1)


def source_identity(
    info: dict[str, Any],
    *,
    fallback_position: int | None = None,
) -> str:
    """Return a stable document identity for grouping multiple chunks."""
    for key in ("report_uid", "canonical_path", "file_name"):
        value = str(info.get(key) or "").strip()
        if value and value != "-":
            return f"{key}:{value}"
    metadata_values = tuple(
        str(info.get(key) or "").strip()
        for key in ("target_name", "report_date", "broker", "title")
    )
    if any(value and value != "-" for value in metadata_values):
        return "metadata:" + "|".join(metadata_values)
    if fallback_position is not None:
        return f"unknown_position:{fallback_position}"
    # Preserve the historical grouping behavior for unmarked legacy messages.
    return "metadata:" + "|".join(metadata_values)


def annotate_document_citation_sources(
    sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Copy passage sources and attach deterministic document citation ranks."""
    annotated: list[dict[str, Any]] = []
    rank_by_identity: dict[str, int] = {}

    for position, raw_source in enumerate(sources, 1):
        if not isinstance(raw_source, dict):
            raise ValueError("citation source must be a mapping")
        info = dict(raw_source)
        passage_rank = source_rank(info, position)
        if passage_rank != position:
            raise ValueError("citation source ranks must be contiguous in selected order")

        identity = source_identity(info, fallback_position=position)
        document_rank = rank_by_identity.setdefault(identity, len(rank_by_identity) + 1)
        info["rank"] = passage_rank
        info["passage_rank"] = passage_rank
        info["document_rank"] = document_rank
        annotated.append(info)

    contract = {
        "version": CITATION_CONTRACT_VERSION,
        "rank_kind": CITATION_RANK_KIND_DOCUMENT,
        "passage_count": len(annotated),
        "document_count": len(rank_by_identity),
    }
    return annotated, contract


def validate_citation_contract(
    sources: list[dict[str, Any]],
    contract: Any,
) -> dict[str, Any]:
    """Classify an absent legacy contract or validate a marked v2 contract."""
    if contract is None:
        return {"status": CITATION_CONTRACT_LEGACY, "document_count": None}

    if (
        not isinstance(contract, dict)
        or not sources
        or type(contract.get("version")) is not int
        or contract.get("version") != CITATION_CONTRACT_VERSION
        or contract.get("rank_kind") != CITATION_RANK_KIND_DOCUMENT
        or any(
            _strict_positive_int(contract.get(key)) is None
            for key in ("passage_count", "document_count")
        )
    ):
        return {"status": CITATION_CONTRACT_INVALID, "document_count": None}

    if any(
        not isinstance(source, dict)
        or any(
            _strict_positive_int(source.get(key)) is None
            for key in ("rank", "passage_rank", "document_rank")
        )
        for source in sources
    ):
        return {"status": CITATION_CONTRACT_INVALID, "document_count": None}

    try:
        expected_sources, expected_contract = annotate_document_citation_sources(sources)
    except ValueError:
        return {"status": CITATION_CONTRACT_INVALID, "document_count": None}

    if (
        any(
            contract.get(key) != expected_contract[key]
            for key in ("passage_count", "document_count")
        )
        or any(
            source.get(key) != expected.get(key)
            for source, expected in zip(sources, expected_sources)
            for key in ("rank", "passage_rank", "document_rank")
        )
    ):
        return {"status": CITATION_CONTRACT_INVALID, "document_count": None}

    return {
        "status": CITATION_CONTRACT_VALID,
        "document_count": expected_contract["document_count"],
    }


def _strict_positive_int(value: Any) -> int | None:
    return value if type(value) is int and value > 0 else None


def group_sources_by_persisted_document_rank(
    sources: list[dict[str, Any]],
    contract: Any,
) -> list[dict[str, Any]]:
    """Group valid v2 sources using persisted document ranks only."""
    validation = validate_citation_contract(sources, contract)
    if validation["status"] != CITATION_CONTRACT_VALID:
        return []

    grouped_by_rank: dict[int, dict[str, Any]] = {}
    for position, info in enumerate(sources, 1):
        document_rank = int(info["document_rank"])
        group = grouped_by_rank.get(document_rank)
        if group is None:
            group = {
                "info": info,
                "ranks": [],
                "document_rank": document_rank,
            }
            grouped_by_rank[document_rank] = group
        group["ranks"].append(source_rank(info, position))
    return [grouped_by_rank[rank] for rank in sorted(grouped_by_rank)]


def remove_unavailable_document_references(
    text: str,
    *,
    source_count: int,
) -> str:
    """Remove out-of-range v2 citations and visible document labels.

    This is intentionally separate from the legacy cleanup path: historical
    answers keep their stored ``문서 N:`` prose exactly as it was rendered
    before document-ranked citations were introduced.
    """
    cleaned = remove_unavailable_citations(text, source_count=source_count)

    def keep_or_remove_label(match: re.Match[str]) -> str:
        rank = int(match.group("rank"))
        return match.group(0) if 1 <= rank <= source_count else ""

    cleaned = _DOCUMENT_LABEL_RE.sub(keep_or_remove_label, cleaned)

    def keep_or_remove_rank(match: re.Match[str]) -> str:
        rank = int(match.group("rank"))
        return match.group(0) if 1 <= rank <= source_count else "\ubb38\uc11c"

    return _DOCUMENT_RANK_RE.sub(keep_or_remove_rank, cleaned)


def group_sources_by_document(rerank_info: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group retrieved chunk-level sources into document-level source entries."""
    grouped: list[dict[str, Any]] = []
    index_by_identity: dict[str, int] = {}
    for index, info in enumerate(rerank_info):
        rank = source_rank(info, index + 1)
        identity = source_identity(info)
        if identity in index_by_identity:
            grouped[index_by_identity[identity]]["ranks"].append(rank)
            continue
        index_by_identity[identity] = len(grouped)
        grouped.append({"info": info, "ranks": [rank]})
    return grouped


def document_rank_aliases(rerank_info: list[dict[str, Any]]) -> dict[int, int]:
    """Map original chunk ranks to sequential document-level display ranks."""
    aliases: dict[int, int] = {}
    for display_rank, source_group in enumerate(group_sources_by_document(rerank_info), 1):
        for rank in source_group["ranks"]:
            aliases[rank] = display_rank
    return aliases


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
