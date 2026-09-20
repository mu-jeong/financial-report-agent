"""Architectural regression checks that keep the long-lived branch additive."""

import ast
from pathlib import Path
import subprocess
import sys


EXTENSION = Path(__file__).resolve().parents[1]
ROOT = EXTENSION.parents[1]


def imports(path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_upstream_dependencies_stay_in_adapter():
    for path in EXTENSION.glob("*.py"):
        if path.name == "upstream_adapter.py":
            continue
        assert not any(name == "src" or name.startswith("src.") for name in imports(path)), path


def test_existing_application_has_no_integration_dependency():
    for directory in (ROOT / "src", ROOT / "apps"):
        for path in directory.rglob("*.py"):
            assert not any(name == "mcp" or name.startswith(("mcp.", "integrations.finance_mcp")) for name in imports(path)), path


def test_import_does_not_initialize_application_or_mcp_transport():
    code = "import sys; import integrations.finance_mcp; import integrations.finance_mcp.service; assert not any(n == 'src' or n.startswith('src.') or n == 'mcp' or n.startswith('mcp.') for n in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
