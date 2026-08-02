"""Deterministic minimum-answer requirements for regression candidates."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from src.core.artifact_io import is_safe_artifact_identifier
from src.utils.citations import extract_citation_ranks


MAX_ANSWER_REQUIREMENTS = 5
MAX_REQUIREMENT_TERMS = 8
MAX_REQUIREMENT_DESCRIPTION_CHARS = 240
MAX_REQUIREMENT_TERM_CHARS = 100

_REQUIREMENT_KEYS = {
    "id",
    "description",
    "answer_terms_any",
    "source_terms_any",
    "require_citation",
}
_SOURCE_MATCH_FIELDS = (
    "target_name",
    "title",
    "file_name",
    "broker",
    "report_type",
)


class AnswerRequirementValidationError(ValueError):
    """Raised when a minimum-answer requirement is unsafe or ambiguous."""


def _match_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _canonical_terms(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list):
        raise AnswerRequirementValidationError(f"{label} must be a list")
    if len(value) > MAX_REQUIREMENT_TERMS:
        raise AnswerRequirementValidationError(
            f"{label} supports at most {MAX_REQUIREMENT_TERMS} terms"
        )

    terms: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise AnswerRequirementValidationError(
                f"{label} must contain strings"
            )
        term = item.strip()
        normalized = _match_text(term)
        if len(normalized) < 2:
            raise AnswerRequirementValidationError(
                f"{label} terms must contain at least two letters or digits"
            )
        if len(term) > MAX_REQUIREMENT_TERM_CHARS:
            raise AnswerRequirementValidationError(
                f"{label} terms are too long"
            )
        if normalized in seen:
            continue
        seen.add(normalized)
        terms.append(term)
    return terms


def canonicalize_answer_requirements(value: Any) -> list[dict[str, Any]]:
    """Return the strict, bounded representation persisted in a candidate."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise AnswerRequirementValidationError(
            "answer_requirements must be a list"
        )
    if len(value) > MAX_ANSWER_REQUIREMENTS:
        raise AnswerRequirementValidationError(
            f"answer_requirements supports at most {MAX_ANSWER_REQUIREMENTS} items"
        )

    requirements: list[dict[str, Any]] = []
    requirement_ids: set[str] = set()
    for index, item in enumerate(value, 1):
        if not isinstance(item, Mapping):
            raise AnswerRequirementValidationError(
                "answer_requirements must contain objects"
            )
        unknown = set(item) - _REQUIREMENT_KEYS
        if unknown:
            raise AnswerRequirementValidationError(
                "answer requirement contains unsupported keys: "
                f"{sorted(unknown)}"
            )

        requirement_id = str(
            item.get("id") or f"answer_requirement_{index}"
        ).strip()
        if not is_safe_artifact_identifier(requirement_id):
            raise AnswerRequirementValidationError(
                "answer requirement id is unsafe"
            )
        if requirement_id in requirement_ids:
            raise AnswerRequirementValidationError(
                "answer requirement ids must be unique"
            )
        requirement_ids.add(requirement_id)

        description_value = item.get("description")
        if not isinstance(description_value, str):
            raise AnswerRequirementValidationError(
                "answer requirement description must be a string"
            )
        description = description_value.strip()
        if not description:
            raise AnswerRequirementValidationError(
                "answer requirement description is required"
            )
        if len(description) > MAX_REQUIREMENT_DESCRIPTION_CHARS:
            raise AnswerRequirementValidationError(
                "answer requirement description is too long"
            )

        answer_terms = _canonical_terms(
            item.get("answer_terms_any"),
            label="answer_terms_any",
        )
        if not answer_terms:
            raise AnswerRequirementValidationError(
                "answer_terms_any requires at least one term"
            )
        source_terms = _canonical_terms(
            item.get("source_terms_any", []),
            label="source_terms_any",
        )
        require_citation = item.get("require_citation", True)
        if not isinstance(require_citation, bool):
            raise AnswerRequirementValidationError(
                "require_citation must be a boolean"
            )
        if require_citation and not source_terms:
            raise AnswerRequirementValidationError(
                "source_terms_any is required when require_citation is true"
            )

        requirements.append(
            {
                "id": requirement_id,
                "description": description,
                "answer_terms_any": answer_terms,
                "source_terms_any": source_terms,
                "require_citation": require_citation,
            }
        )
    return requirements


def _source_rank(source: Mapping[str, Any], fallback_rank: int) -> int:
    try:
        rank = int(source.get("rank", fallback_rank))
    except (TypeError, ValueError):
        return fallback_rank
    return rank if rank > 0 else fallback_rank


def _matching_source_ranks(
    source_terms: Sequence[str],
    sources: Sequence[Mapping[str, Any]],
) -> list[int]:
    normalized_terms = [_match_text(term) for term in source_terms]
    ranks: list[int] = []
    for fallback_rank, source in enumerate(sources, 1):
        searchable = _match_text(
            " ".join(str(source.get(field) or "") for field in _SOURCE_MATCH_FIELDS)
        )
        if searchable and any(term in searchable for term in normalized_terms):
            ranks.append(_source_rank(source, fallback_rank))
    return list(dict.fromkeys(ranks))


def evaluate_answer_requirements(
    requirements: Any,
    *,
    answer: str,
    sources: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate all requirements using answer text, source metadata, and citations."""

    canonical = canonicalize_answer_requirements(requirements)
    normalized_answer = _match_text(answer)
    cited_ranks = extract_citation_ranks(answer or "", source_count=None)
    results: list[dict[str, Any]] = []

    for requirement in canonical:
        answer_term_pass = any(
            _match_text(term) in normalized_answer
            for term in requirement["answer_terms_any"]
        )
        source_terms = requirement["source_terms_any"]
        matching_ranks = (
            _matching_source_ranks(source_terms, sources)
            if source_terms
            else []
        )
        source_term_pass = not source_terms or bool(matching_ranks)
        cited_matching_ranks = [
            rank for rank in matching_ranks if rank in cited_ranks
        ]
        citation_pass = (
            bool(cited_matching_ranks)
            if requirement["require_citation"]
            else True
        )
        matched_source_rank = (
            cited_matching_ranks[0]
            if cited_matching_ranks
            else matching_ranks[0]
            if matching_ranks
            else None
        )
        passed = answer_term_pass and source_term_pass and citation_pass
        results.append(
            {
                "id": requirement["id"],
                "description": requirement["description"],
                "answer_term_pass": answer_term_pass,
                "source_term_pass": source_term_pass,
                "citation_pass": citation_pass,
                "matched_source_rank": matched_source_rank,
                "passed": passed,
            }
        )

    return {
        "passed": bool(canonical) and all(item["passed"] for item in results),
        "results": results,
    }

