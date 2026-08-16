"""Helpers for resolving follow-up questions against prior answer sections."""

from __future__ import annotations

import re
from typing import Any

_KOREAN_ORDINAL_INDEXES = {
    "첫번째": 0,
    "첫째": 0,
    "첫": 0,
    "두번째": 1,
    "둘째": 1,
    "세번째": 2,
    "셋째": 2,
    "네번째": 3,
    "넷째": 3,
    "다섯번째": 4,
    "다섯째": 4,
    "여섯번째": 5,
    "여섯째": 5,
    "일곱번째": 6,
    "일곱째": 6,
    "여덟번째": 7,
    "여덟째": 7,
    "아홉번째": 8,
    "아홉째": 8,
    "열번째": 9,
    "열째": 9,
}
_ORDINAL_TOKEN = (
    r"(?P<numeric>[1-9]\d*)\s*(?:번째|번)"
    r"|(?P<korean>첫\s*번째|첫째|첫|두\s*번째|둘째|세\s*번째|셋째|"
    r"네\s*번째|넷째|다섯\s*번째|다섯째|여섯\s*번째|여섯째|"
    r"일곱\s*번째|일곱째|여덟\s*번째|여덟째|아홉\s*번째|아홉째|"
    r"열\s*번째|열째)"
    r"|(?P<last>마지막|끝)"
)
_REPORT_TYPE_BETWEEN_ORDINAL_AND_DOCUMENT = (
    r"(?:(?:company|industry|economy|개별\s*종목|기업|회사|산업|업종|섹터|"
    r"경제|거시\s*경제)\s*)?"
)
_DOCUMENT_NOUN = r"(?:리포트|보고서|문서|자료)"
_ORDINAL_BEFORE_DOCUMENT_RE = re.compile(
    rf"(?:{_ORDINAL_TOKEN})\s*(?:로\s*)?"
    rf"{_REPORT_TYPE_BETWEEN_ORDINAL_AND_DOCUMENT}{_DOCUMENT_NOUN}",
    re.IGNORECASE,
)

REPORT_TYPE_FILTER_KEY = "report_type"
REPORT_TYPES_FILTER_KEY = "report_types"
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


def parse_ordinal_reference(question: str) -> int | None:
    """Return a zero-based rank only for an ordinal document reference."""
    text = str(question or "")
    match = _ORDINAL_BEFORE_DOCUMENT_RE.search(text)
    if match is None:
        return None
    if match.group("last"):
        return -1
    if numeric := match.group("numeric"):
        return int(numeric) - 1
    korean = re.sub(r"\s+", "", str(match.group("korean") or ""))
    return _KOREAN_ORDINAL_INDEXES.get(korean)


def _ordered_file_names(values: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        file_name = str(value or "").strip()
        if not file_name or file_name == "-" or file_name in seen:
            continue
        seen.add(file_name)
        ordered.append(file_name)
    return ordered


def _ordinal_candidates(
    question: str,
    prior_search_scope: dict[str, Any],
) -> tuple[list[str], dict[str, Any] | None, str | None]:
    sections = _sections_for_prior_scope(prior_search_scope)
    ordinal_phrase = _ORDINAL_BEFORE_DOCUMENT_RE.search(str(question or ""))
    section, matched_alias = _match_section(
        ordinal_phrase.group(0) if ordinal_phrase else "",
        sections,
    )
    if section:
        section_files = _ordered_file_names(section.get("file_names"))
        if section_files:
            return section_files, section, matched_alias
    candidates = _ordered_file_names(prior_search_scope.get("file_names"))
    if not candidates:
        candidates = _ordered_file_names(
            (prior_search_scope.get("search_filters") or {}).get("file_names")
        )
    return candidates, section, matched_alias


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
    base_filters.pop(REPORT_TYPES_FILTER_KEY, None)

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

    search_filters = search_scope.get("search_filters") or {}
    explicit_report_type = search_filters.get(REPORT_TYPE_FILTER_KEY)
    explicit_report_types = search_filters.get(REPORT_TYPES_FILTER_KEY)
    section_ids: list[str]
    if file_names_by_type:
        section_ids = [section["id"] for section in DEFAULT_REPORT_TYPE_SECTIONS if section["id"] in file_names_by_type]
    elif explicit_report_types is not None:
        requested_types = {
            str(report_type) for report_type in explicit_report_types
        }
        section_ids = [
            section["id"]
            for section in DEFAULT_REPORT_TYPE_SECTIONS
            if section["id"] in requested_types
        ]
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
    if not prior_search_scope:
        return {"matched": False, "reason": "no_section_alias_match"}

    ordinal = parse_ordinal_reference(question)
    if ordinal is not None:
        candidates, section, matched_alias = _ordinal_candidates(
            question,
            prior_search_scope,
        )
        selected_index = len(candidates) - 1 if ordinal < 0 else ordinal
        requested_rank = len(candidates) if ordinal < 0 else ordinal + 1
        if selected_index < 0 or selected_index >= len(candidates):
            return {
                "matched": False,
                "reason": "document_ordinal_out_of_range",
                "requested_document_rank": requested_rank,
                "document_count": len(candidates),
            }

        inherited_filters = _date_filters(
            _base_filters_without_file_scope(prior_search_scope)
        )
        added_filters: dict[str, Any] = {}
        if section:
            added_filters.update(
                {
                    key: value
                    for key, value in (section.get("filters") or {}).items()
                    if key not in DATE_FILTER_KEYS
                }
            )
        search_filters = {
            **inherited_filters,
            **added_filters,
            "file_names": [candidates[selected_index]],
        }
        return {
            "matched": True,
            "reason": "matched_ordinal_report_reference",
            "matched_alias": matched_alias or "ordinal_reference",
            "matched_document_rank": selected_index + 1,
            "selected_file_name": candidates[selected_index],
            "inherited_filters": inherited_filters,
            "added_filters": added_filters,
            "dropped_filters": ["file_names"],
            "search_filters": search_filters,
        }

    if not is_section_deep_dive_followup(question):
        return {"matched": False, "reason": "no_section_alias_match"}

    section, matched_alias = _match_section(question, _sections_for_prior_scope(prior_search_scope))
    if not section:
        return {"matched": False, "reason": "no_section_alias_match"}

    prior_filters = _base_filters_without_file_scope(prior_search_scope)
    inherited_filters = _date_filters(prior_filters)
    section_filters = dict(section.get("filters") or {})
    if REPORT_TYPES_FILTER_KEY in current_filters:
        current_filters.pop(REPORT_TYPE_FILTER_KEY, None)
        section_filters.pop(REPORT_TYPE_FILTER_KEY, None)
    elif REPORT_TYPE_FILTER_KEY in current_filters:
        current_filters.pop(REPORT_TYPES_FILTER_KEY, None)
        section_filters.pop(REPORT_TYPES_FILTER_KEY, None)
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
