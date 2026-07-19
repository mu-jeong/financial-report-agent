from __future__ import annotations

import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_retrieval_source_never_imports_transition_packages():
    violations: list[str] = []
    for source in sorted((REPOSITORY_ROOT / "src" / "retrieval").glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                modules = (node.module or "",)
            elif isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            else:
                continue
            for module in modules:
                if module == "src.migrations" or module.startswith("src.migrations."):
                    violations.append(
                        f"{source.relative_to(REPOSITORY_ROOT).as_posix()}:{node.lineno}"
                    )

    assert violations == []
