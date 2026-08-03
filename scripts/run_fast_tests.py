"""Run the default development test lane without intentional slow checks."""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def build_pytest_args(argv: list[str] | None = None) -> list[str]:
    """Build fast-lane arguments without allowing marker overrides."""

    forwarded = list(argv or [])
    if any(
        argument.startswith("-m") and not argument.startswith("--")
        for argument in forwarded
    ):
        raise ValueError(
            "run_fast_tests.py does not accept -m; the fast lane always excludes slow tests"
        )
    return ["-q", "-m", "not slow", *forwarded]


def main(argv: list[str] | None = None) -> int:
    """Run pytest's fast lane from the repository root."""

    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.chdir(REPOSITORY_ROOT)
    repository_root = str(REPOSITORY_ROOT)
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)

    import pytest

    try:
        pytest_args = build_pytest_args(argv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return pytest.main(pytest_args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
