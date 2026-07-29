"""Shared, safe filesystem primitives for persisted artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SAFE_ARTIFACT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)"
        r"\s*[:=]\s*[^\s&#]+"
    ),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    re.compile(r"(?<!\w)(?:\+?82[-.\s]?)?0?1[016789](?:[-.\s]?\d){7,8}(?!\w)"),
    re.compile(r"(?<!\w)\+\d{1,3}(?:[-.\s]?\d){7,14}(?!\w)"),
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/](?:[^\\/\s]+[\\/])*[^\\/\s]*"),
    re.compile(r"(?<![\\/])\\\\[^\\\s]+\\[^\\\s]+"),
    re.compile(
        r"(?<![:/\w])/[^\s/,\r\n;]+(?:/[^\r\n,;]*)?"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    re.compile(
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s?#]+\?[^\s#]*"
        r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret)="
        r"[^&#\s]+"
    ),
)


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Atomically replace *path* with UTF-8 text and return the target path."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise

    return target


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Serialize a mapping as readable UTF-8 JSON and atomically replace *path*."""

    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"
    return atomic_write_text(path, text)


def strict_json_loads(text: str) -> Any:
    """Decode JSON while rejecting non-standard NaN and Infinity constants."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is not allowed: {value}")

    return json.loads(text, parse_constant=reject_constant)


def contains_sensitive_identifier_pattern(value: str) -> bool:
    """Return whether *value* contains a credential, PII, or machine path."""

    return any(pattern.search(value) is not None for pattern in _SENSITIVE_PATTERNS)


def is_safe_artifact_identifier(value: str) -> bool:
    """Return whether *value* is safe to use unchanged as a path component."""

    return (
        _SAFE_ARTIFACT_IDENTIFIER.fullmatch(value) is not None
        and not contains_sensitive_identifier_pattern(value)
    )


def safe_artifact_token(value: str) -> str:
    """Return *value* when safe, otherwise a stable opaque filesystem token."""

    if is_safe_artifact_identifier(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"id_{digest}"
