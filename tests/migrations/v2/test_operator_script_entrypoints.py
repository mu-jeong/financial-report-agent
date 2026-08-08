from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.slow


@pytest.mark.parametrize(
    "relative_path",
    (
        "scripts/migrations/v2/migrate_v2_user.py",
        "scripts/migrations/v2/rebuild_v2_successor.py",
    ),
)
def test_operator_script_can_bootstrap_repository_imports_from_any_directory(
    tmp_path: Path,
    relative_path: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / relative_path), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert "ModuleNotFoundError" not in result.stderr
