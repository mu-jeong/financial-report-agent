"""Run the optional finance report MCP server over stdio."""

from __future__ import annotations

import argparse
import math
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path


_SCRIPT_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_SCRIPT_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPO_ROOT))


def _absolute_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("must be an absolute path")
    return path.resolve(strict=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local finance research MCP server.")
    parser.add_argument(
        "--repo-root",
        type=_absolute_path,
        default=_SCRIPT_REPO_ROOT,
        help="Absolute repository root (defaults to the root containing this script).",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=None,
        help="Report data root. Relative paths are resolved from --repo-root.",
    )
    parser.add_argument(
        "--embedding-timeout",
        type=float,
        default=30.0,
        help="OpenRouter query embedding timeout in seconds.",
    )
    return parser


def _resolve_data_root(repo_root: Path, command_line: Path | None) -> Path:
    selected = command_line
    if selected is None:
        configured = os.environ.get("DATA_ROOT", "").strip()
        selected = Path(configured).expanduser() if configured else repo_root / "data"
    if not selected.is_absolute():
        selected = repo_root / selected
    return selected.resolve(strict=False)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = args.repo_root
    if not repo_root.is_dir():
        _parser().error(f"repository root does not exist: {repo_root}")
    if not math.isfinite(args.embedding_timeout) or args.embedding_timeout <= 0:
        _parser().error("--embedding-timeout must be greater than zero")

    os.chdir(repo_root)
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # Import-time diagnostics from optional upstream libraries must not enter the
    # JSON-RPC stream. The SDK itself diverts fd 1 to stderr while serving.
    with redirect_stdout(sys.stderr):
        from dotenv import load_dotenv

        load_dotenv(repo_root / ".env", override=False)
        data_root = _resolve_data_root(repo_root, args.data_root)

        from integrations.finance_mcp.service import ResearchService

        def build_adapter():
            from integrations.finance_mcp.upstream_adapter import UpstreamAdapter

            return UpstreamAdapter(data_root, embedding_timeout=args.embedding_timeout)

        service = ResearchService(adapter_factory=build_adapter)

        from integrations.finance_mcp.server import create_server, run_stdio

        server = create_server(service)

    run_stdio(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
