"""User-facing chat UX helpers kept pure and testable."""

from __future__ import annotations

import html
import json
import base64
import re
from typing import Any

_DATE_RANGE_PATTERNS = [
    re.compile(r"20\d{2}\s*년\s*(?:1[0-2]|0?[1-9])\s*월\s*(?:3[01]|[12]\d|0?[1-9])\s*일?"),
    re.compile(r"20\d{2}[-/.](?:1[0-2]|0?[1-9])[-/.](?:3[01]|[12]\d|0?[1-9])"),
    re.compile(r"(?<!\d)(?:1[0-2]|0?[1-9])\s*/\s*(?:3[01]|[12]\d|0?[1-9])(?:\s*\([^)]*\))?"),
    re.compile(r"20\d{2}\s*년\s*(?:1[0-2]|0?[1-9])\s*월"),
    re.compile(r"(?:이번주|지난주|전주|금주|다음주|차주|이번달|지난달|전월|금월|다음달|익월|오늘|어제|그제|내일|모레)"),
]
_NUMERIC_TILDE_RE = re.compile(r"(?<![\\~])~(?!~)(?=[ \t]*\d)")
_BACKTICK_CODE_RE = re.compile(r"(?P<delimiter>`+).*?(?P=delimiter)", re.DOTALL)
_TILDE_FENCE_RE = re.compile(r"^[ \t]{0,3}(~{3,})(.*)$")


def _compact_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _strip_temporal_phrases(question: str) -> str:
    cleaned = question
    for pattern in _DATE_RANGE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return _compact_spaces(cleaned)


def _has_temporal_phrase(question: str) -> bool:
    return any(pattern.search(str(question or "")) for pattern in _DATE_RANGE_PATTERNS)


def _escape_numeric_tildes_outside_inline_code(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in _BACKTICK_CODE_RE.finditer(text):
        parts.append(_NUMERIC_TILDE_RE.sub(r"\\~", text[cursor:match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(_NUMERIC_TILDE_RE.sub(r"\\~", text[cursor:]))
    return "".join(parts)


def escape_numeric_tildes_for_markdown(text: str) -> str:
    """Escape financial range tildes without changing code or raw chat data."""
    escaped: list[str] = []
    plain_lines: list[str] = []
    fence_length = 0

    def flush_plain_lines() -> None:
        if plain_lines:
            escaped.append(_escape_numeric_tildes_outside_inline_code("".join(plain_lines)))
            plain_lines.clear()

    for line in str(text or "").splitlines(keepends=True):
        fence_match = _TILDE_FENCE_RE.match(line)
        if fence_length:
            escaped.append(line)
            if (
                fence_match
                and len(fence_match.group(1)) >= fence_length
                and not fence_match.group(2).strip()
            ):
                fence_length = 0
            continue
        if fence_match:
            flush_plain_lines()
            fence_length = len(fence_match.group(1))
            escaped.append(line)
            continue
        plain_lines.append(line)

    flush_plain_lines()
    return "".join(escaped)


def build_scope_notice(state_or_metadata: dict[str, Any]) -> str | None:
    """Return a short user-facing note about reused or reset retrieval scope."""
    if state_or_metadata.get("scope_notice"):
        return str(state_or_metadata["scope_notice"])
    if state_or_metadata.get("scope_source") != "prior_search_scope":
        return None
    filters = state_or_metadata.get("search_filters") or {}
    temporal_context = state_or_metadata.get("temporal_context")
    has_date_filter = bool(filters.get("report_date_start") or filters.get("report_date_end") or temporal_context)
    if has_date_filter and not filters.get("file_names") and _has_temporal_phrase(state_or_metadata.get("question", "")):
        return "새 날짜 조건이 있어 검색 범위를 다시 설정했습니다."
    if has_date_filter:
        return "직전 답변의 검색 조건을 이어받아 답변합니다."
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
