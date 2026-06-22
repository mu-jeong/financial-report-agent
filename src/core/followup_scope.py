"""Helpers for resolving follow-up questions against prior answer sections."""

from __future__ import annotations

import re
from typing import Any

REPORT_TYPE_FILTER_KEY = "report_type"
DATE_FILTER_KEYS = ("report_date_start", "report_date_end")

DEFAULT_REPORT_TYPE_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "company",
        "label": "개별 종목 분석 리포트",
        "aliases": ("개별종목", "개별 종목", "종목", "기업", "회사", "company"),
        "filters": {REPORT_TYPE_FILTER_KEY: "company"},
    },
    {
        "id": "industry",
        "label": "섹터/산업 리포트",
        "aliases": ("섹터", "산업", "업종", "industry"),
        "filters": {REPORT_TYPE_FILTER_KEY: "industry"},
    },
    {
        "id": "economy",
        "label": "거시경제/전략 리포트",
        "aliases": ("거시경제", "매크로", "전략", "경제", "economy"),
        "filters": {REPORT_TYPE_FILTER_KEY: "economy"},
    },
)

SECTION_DEEP_DIVE_TERMS: tuple[str, ...] = (
    "자세히",
    "좀 더 자세히",
    "상세히",
    "상세하게",
    "더 알려",
    "부분",
    "섹션",
)


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").casefold())


def _normalized_contains(text: str, keyword: str) -> bool:
    return _normalize_text(keyword) in _normalize_text(text)


def is_section_deep_dive_followup(question: str) -> bool:
    """Return whether a question points at a prior answer section for more detail."""
    return any(
        _normalized_contains(question, alias)
        for section in DEFAULT_REPORT_TYPE_SECTIONS
        for alias in section["aliases"]
    ) and any(_normalized_contains(question, term) for term in SECTION_DEEP_DIVE_TERMS)


def _base_filters_without_file_scope(search_scope: dict[str, Any]) -> dict[str, Any]:
    filters = dict((search_scope.get("search_filters") or {}))
    filters.pop("file_names", None)
    return filters


def _date_filters(filters: dict[str, Any]) -> dict[str, Any]:
    return {key: filters[key] for key in DATE_FILTER_KEYS if filters.get(key)}


def _section_template_by_id(section_id: str) -> dict[str, Any] | None:
    return next((section for section in DEFAULT_REPORT_TYPE_SECTIONS if section["id"] == section_id), None)


def _section_from_template(template: dict[str, Any], base_filters: dict[str, Any], file_names: list[str] | None = None) -> dict[str, Any]:
    filters = dict(base_filters)
    filters.update(template["filters"])
    section = {
        "id": template["id"],
        "label": template["label"],
        "aliases": list(template["aliases"]),
        "filters": filters,
    }
    if file_names:
        section["file_names"] = file_names
    return section


def build_answer_scope_index(
    search_scope: dict[str, Any] | None,
    sources: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[str, Any]:
    """Build a small section index from deterministic retrieval metadata.

    The first version intentionally models only report-type sections. It does not
    parse the generated answer text; sections come from metadata/search scope so
    follow-up scope reuse remains deterministic and explainable.
    """
    search_scope = search_scope or {}
    sources = list(sources or [])
    base_filters = _base_filters_without_file_scope(search_scope)
    base_filters.pop(REPORT_TYPE_FILTER_KEY, None)

    file_names_by_type: dict[str, list[str]] = {}
    for source in sources:
        report_type = source.get(REPORT_TYPE_FILTER_KEY)
        file_name = source.get("file_name")
        if report_type not in {"company", "industry", "economy"}:
            continue
        if not file_name or file_name == "-":
            continue
        bucket = file_names_by_type.setdefault(str(report_type), [])
        if file_name not in bucket:
            bucket.append(str(file_name))

    explicit_report_type = (search_scope.get("search_filters") or {}).get(REPORT_TYPE_FILTER_KEY)
    section_ids: list[str]
    if file_names_by_type:
        section_ids = [section["id"] for section in DEFAULT_REPORT_TYPE_SECTIONS if section["id"] in file_names_by_type]
    elif explicit_report_type in {"company", "industry", "economy"}:
        section_ids = [str(explicit_report_type)]
    else:
        section_ids = [section["id"] for section in DEFAULT_REPORT_TYPE_SECTIONS]

    sections: list[dict[str, Any]] = []
    for section_id in section_ids:
        template = _section_template_by_id(section_id)
        if not template:
            continue
        sections.append(_section_from_template(template, base_filters, file_names_by_type.get(section_id)))

    return {"version": 1, "sections": sections}


def _sections_for_prior_scope(prior_search_scope: dict[str, Any]) -> list[dict[str, Any]]:
    answer_scope_index = prior_search_scope.get("answer_scope_index") or {}
    sections = answer_scope_index.get("sections") if isinstance(answer_scope_index, dict) else None
    if sections:
        return list(sections)
    return build_answer_scope_index(prior_search_scope, [])["sections"]


def _match_section(question: str, sections: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | tuple[None, None]:
    for section in sections:
        for alias in section.get("aliases") or []:
            if _normalized_contains(question, str(alias)):
                return section, str(alias)
    return None, None


def resolve_section_followup_scope(
    question: str,
    *,
    current_filters: dict[str, Any] | None,
    prior_search_scope: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve a section-referencing follow-up into concrete search filters."""
    prior_search_scope = prior_search_scope or {}
    current_filters = dict(current_filters or {})
    if not prior_search_scope or not is_section_deep_dive_followup(question):
        return {"matched": False, "reason": "no_section_alias_match"}

    section, matched_alias = _match_section(question, _sections_for_prior_scope(prior_search_scope))
    if not section:
        return {"matched": False, "reason": "no_section_alias_match"}

    prior_filters = _base_filters_without_file_scope(prior_search_scope)
    inherited_filters = _date_filters(prior_filters)
    section_filters = dict(section.get("filters") or {})
    added_filters = {
        key: value
        for key, value in section_filters.items()
        if key not in inherited_filters and prior_filters.get(key) != value
    }
    # Explicit current filters win over the section template when present.
    added_filters.update({key: value for key, value in current_filters.items() if key not in DATE_FILTER_KEYS})

    search_filters = dict(inherited_filters)
    search_filters.update(added_filters)

    dropped_filters = []
    if prior_search_scope.get("file_names") or (prior_search_scope.get("search_filters") or {}).get("file_names"):
        dropped_filters.append("file_names")

    return {
        "matched": True,
        "reason": "matched_prior_section_alias",
        "matched_section_id": section.get("id"),
        "matched_section_label": section.get("label"),
        "matched_alias": matched_alias,
        "inherited_filters": inherited_filters,
        "added_filters": added_filters,
        "dropped_filters": dropped_filters,
        "search_filters": search_filters,
    }
