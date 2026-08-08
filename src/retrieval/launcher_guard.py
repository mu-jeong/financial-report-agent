"""Small launcher-facing bootstrap command used by every supported entrypoint."""

from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the retrieval runtime before launch")
    parser.add_argument("--write", action="store_true", help="require an update-safe runtime")
    args = parser.parse_args(argv)

    # Import after argument parsing so packaging/launcher errors remain clear.
    from src.configs.config import DATA_ROOT
    from src.retrieval.bootstrap import (
        RetrievalBootstrapError,
        reconcile_and_inspect_runtime,
    )
    from src.retrieval.runtime_guard import RetrievalWriteBlocked, guard_before_retrieval_write

    try:
        selection = reconcile_and_inspect_runtime(
            DATA_ROOT,
            allow_live_writer_read=not args.write,
            prefer_fast_read=not args.write,
        )
        if args.write:
            selection = guard_before_retrieval_write(
                DATA_ROOT,
                allow_empty_preflight=True,
            )
    except (
        RetrievalBootstrapError,
        RetrievalWriteBlocked,
    ) as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "status": "ok",
                "mode": selection.mode,
                "active_snapshot_id": selection.active_snapshot_id,
                "predecessor_snapshot_id": selection.predecessor_snapshot_id,
                "publication_generation": selection.publication_generation,
                "write_epoch": selection.write_epoch,
                "degraded": selection.degraded,
                "write_enabled": selection.write_enabled,
                "initialization_state": selection.initialization_state,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
