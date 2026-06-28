"""Application version helpers.

The user-facing version is intentionally declared near the top of README.md so
emailed issue reports can identify the installed program version without relying
on package metadata.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_APP_VERSION = "unknown"


@lru_cache(maxsize=1)
def get_app_version() -> str:
    """Return the Finance LLM application version declared in README.md."""
    readme_path = PROJECT_ROOT / "README.md"
    try:
        readme = readme_path.read_text(encoding="utf-8-sig")
    except OSError:
        return DEFAULT_APP_VERSION
    match = re.search(r"^>\s*Version:\s*`?([^`\s]+)`?\s*$", readme, flags=re.MULTILINE)
    if not match:
        return DEFAULT_APP_VERSION
    return match.group(1).strip() or DEFAULT_APP_VERSION
