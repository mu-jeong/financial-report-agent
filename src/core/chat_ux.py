"""User-facing chat UX helpers kept pure and testable."""

from __future__ import annotations

import html
import json
import base64
import re
from typing import Any

from src.core.metadata_filters import infer_search_filters

_DATE_PATTERNS = [
    re.compile(r"20\d{2}\s*년\s*(?P<month>1[0-2]|0?[1-9])\s*월\s*(?P<day>3[01]|[12]\d|0?[1-9])\s*일?"),
    re.compile(r"20\d{2}[-/.](?P<month>1[0-2]|0?[1-9])[-/.](?P<day>3[01]|[12]\d|0?[1-9])"),
    re.compile(r"(?<!\d)(?P<month>1[0-2]|0?[1-9])\s*/\s*(?P<day>3[01]|[12]\d|0?[1-9])"),
]
_DATE_RANGE_PATTERNS = [
    re.compile(r"20\d{2}\s*년\s*(?:1[0-2]|0?[1-9])\s*월\s*(?:3[01]|[12]\d|0?[1-9])\s*일?"),
    re.compile(r"20\d{2}[-/.](?:1[0-2]|0?[1-9])[-/.](?:3[01]|[12]\d|0?[1-9])"),
    re.compile(r"(?<!\d)(?:1[0-2]|0?[1-9])\s*/\s*(?:3[01]|[12]\d|0?[1-9])(?:\s*\([^)]*\))?"),
    re.compile(r"20\d{2}\s*년\s*(?:1[0-2]|0?[1-9])\s*월"),
    re.compile(r"(?:이번주|지난주|전주|금주|다음주|차주|이번달|지난달|전월|금월|다음달|익월|오늘|어제|그제|내일|모레)"),
]
_REPORT_WORD_RE = re.compile(r"\s*(?:발간된|최근|최신|가장|리포트들|리포트|내용|핵심|알려줘|해줘|에서|의)\s*")


def _compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _short_date_label(question: str) -> str | None:
    for pattern in _DATE_PATTERNS:
        match = pattern.search(question)
        if match:
            return f"{int(match.group('month'))}/{int(match.group('day'))}"
    return None


def _strip_temporal_phrases(question: str) -> str:
    cleaned = question
    for pattern in _DATE_RANGE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return _compact_spaces(cleaned)


def build_thread_title(question: str, max_length: int = 32) -> str:
    """Build a concise deterministic title from the first user question."""
    filters = infer_search_filters(question)
    target = filters.get("target_name")
    report_type = filters.get("report_type")
    date_label = _short_date_label(question)

    intent = "비교" if any(keyword in question for keyword in ["비교", "증권사별", "대비"]) else "요약"
    subject = target or ""
    if not subject:
        without_time = _strip_temporal_phrases(question)
        for keyword in ["산업", "업종", "섹터", "기업", "경제"]:
            marker = without_time.find(keyword)
            if marker > 0:
                subject = _REPORT_WORD_RE.sub(" ", without_time[: marker + len(keyword)])
                break
        subject = _compact_spaces(subject) or "새 대화"
    if report_type == "industry" and "산업" not in subject:
        subject = f"{subject} 산업"
    elif report_type == "economy" and "경제" not in subject:
        subject = f"{subject} 경제"

    parts = [subject]
    if date_label:
        parts.append(date_label)
    parts.append("리포트")
    parts.append(intent)
    title = _compact_spaces(" ".join(part for part in parts if part))
    return title[:max_length]


def build_scope_notice(state_or_metadata: dict[str, Any]) -> str | None:
    """Return a short user-facing note about reused or reset retrieval scope."""
    if state_or_metadata.get("scope_notice"):
        return str(state_or_metadata["scope_notice"])
    if state_or_metadata.get("scope_source") != "prior_search_scope":
        return None
    filters = state_or_metadata.get("search_filters") or {}
    temporal_context = state_or_metadata.get("temporal_context")
    has_date_filter = bool(filters.get("report_date_start") or filters.get("report_date_end") or temporal_context)
    if has_date_filter and not filters.get("file_names"):
        return "새 날짜 조건이 있어 검색 범위를 다시 설정했습니다."
    return "직전 답변의 참고 문서 범위 안에서 답변합니다."


def build_no_result_suggestions(question: str, search_filters: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Build retry suggestions for no-result VectorDB answers."""
    search_filters = search_filters or {}
    suggestions: list[dict[str, str]] = []
    base_question = _compact_spaces(question)
    if search_filters.get("report_date_start") or search_filters.get("report_date_end"):
        no_date_query = _strip_temporal_phrases(base_question)
        if no_date_query and no_date_query != base_question:
            suggestions.append({"label": "날짜 조건 없이 다시 검색", "query": no_date_query})
    suggestions.extend(
        [
            {"label": "기업 리포트만 검색", "query": f"{base_question} 기업 리포트"},
            {"label": "산업 리포트까지 포함", "query": f"{base_question} 산업 리포트도 포함"},
            {"label": "데이터 업데이트 열기", "query": "__open_data_update__"},
        ]
    )
    deduped: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for suggestion in suggestions:
        if suggestion["label"] not in seen_labels:
            seen_labels.add(suggestion["label"])
            deduped.append(suggestion)
    return deduped


def build_clipboard_copy_html(text: str, *, button_label: str = "Copy issue report") -> str:
    """Return a small HTML copy button for Streamlit components."""
    payload = json.dumps(base64.b64encode(text.encode("utf-8")).decode("ascii"))
    safe_label = html.escape(button_label)
    return f"""
<button type="button" id="copy-issue-report" style="padding:0.35rem 0.6rem;border-radius:0.45rem;border:1px solid #cbd5e1;background:#f8fafc;cursor:pointer;font-size:0.82rem;">
  {safe_label}
</button>
<span id="copy-issue-report-status" style="margin-left:0.5rem;color:#64748b;font-size:0.78rem;"></span>
<script>
const button = document.getElementById('copy-issue-report');
const status = document.getElementById('copy-issue-report-status');
button.addEventListener('click', async () => {{
  try {{
    const encoded = {payload};
    const binary = atob(encoded);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const text = new TextDecoder('utf-8').decode(bytes);
    await navigator.clipboard.writeText(text);
    status.textContent = 'Copied';
  }} catch (error) {{
    status.textContent = 'Copy failed';
  }}
}});
</script>
""".strip()
