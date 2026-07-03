"""Sanitize generated user-facing answers before display/storage."""

from __future__ import annotations

import re

_REPLACEMENT_CHAR_RE = re.compile("\ufffd+")
_HEADING_EACH_COUNT_RE = re.compile(r"^(#{1,6}\s+.*?)(?:\s*\(각\s*\d+\s*건\))([\s,]*)$", re.MULTILINE)
_EMOJI_AND_DECORATION_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"  # flags
    "\U0001F300-\U0001F5FF"  # symbols and pictographs
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F680-\U0001F6FF"  # transport and map symbols
    "\U0001F700-\U0001F77F"  # alchemical symbols
    "\U0001F780-\U0001F7FF"  # geometric shapes extended
    "\U0001F800-\U0001F8FF"  # supplemental arrows-c
    "\U0001F900-\U0001F9FF"  # supplemental symbols and pictographs
    "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-a
    "\u2600-\u27BF"          # miscellaneous symbols/dingbats
    "]+"
)


def sanitize_generated_answer(answer: str) -> str:
    """Remove fragile decoration from generated answers.

    The app stores and exports chat answers as plain text. Emoji/decorative
    symbols have repeatedly appeared as replacement characters in copied issue
    reports, and aggregate labels like ``(각 2건)`` in headings are misleading
    when only some grouped rows have that count. Keep markdown structure while
    stripping those unstable decorations deterministically.
    """
    text = str(answer or "")
    text = _REPLACEMENT_CHAR_RE.sub("", text)
    text = _EMOJI_AND_DECORATION_RE.sub("", text)
    text = _HEADING_EACH_COUNT_RE.sub(r"\1\2", text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(r"(?m)^(#{1,6})\s+", r"\1 ", text)
    return text.strip()
