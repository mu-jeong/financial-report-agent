"""Dependency-light redaction for handoff and outbound diagnostic text."""

from __future__ import annotations

import re

_RAW_ERROR_DETAIL = re.compile(
    r"(?im)(?:"
    r"^\s*Traceback \(most recent call last\):"
    r"|^\s*File\s+\"[^\"]+\",\s*line\s+\d+"
    r"|^\s*at\s+[\w.$<>]+\([^)\r\n]*:\d+\)"
    r"|^\s*(?:Caused by:\s*)?[\w.]+(?:Error|Exception)\s*:"
    r"|\bmetadata\.error\b\s*[:=]"
    r")"
)
_REDACTIONS = (
    ("credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")),
    ("credential", re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b")),
    (
        "credential",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
            r"\s*[:=]\s*[^\s&#]+"
        ),
    ),
    ("email", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
    (
        "phone",
        re.compile(r"(?<!\w)(?:\+?82[-.\s]?)?0?1[016789](?:[-.\s]?\d){7,8}(?!\w)"),
    ),
    ("phone", re.compile(r"(?<!\w)\+\d{1,3}(?:[-.\s]?\d){7,14}(?!\w)")),
    ("url_credential", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    (
        "url_secret",
        re.compile(
            r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s?#]+\?[^\s#]*?"
            r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret)=)"
            r"[^&#\s]+"
        ),
    ),
    ("path", re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/][^\r\n,;]*")),
    ("path", re.compile(r"(?<![\\/])\\\\[^\r\n,;]*")),
    (
        "path",
        re.compile(r"(?<![:/\w])/[^\s/,\r\n;]+(?:/[^\r\n,;]*)?"),
    ),
)


def redact_handoff_text(value: str) -> str:
    """Redact raw error details, identifiers, credentials, and local paths."""

    redacted = str(value)
    if _RAW_ERROR_DETAIL.search(redacted):
        return "[REDACTED:error_detail]"
    for kind, pattern in _REDACTIONS:
        replacement = f"[REDACTED:{kind}]"
        if kind == "url_secret":
            redacted = pattern.sub(lambda match: match.group(1) + replacement, redacted)
        else:
            redacted = pattern.sub(replacement, redacted)
    return redacted
