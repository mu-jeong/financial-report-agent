"""Validate and analyze retained V1/V2 paired retrieval timing samples."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.migrations.v2.validation.performance import (
    analyze_benchmark,
    load_benchmark_evidence,
    write_benchmark_evidence,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the V2 paired-bootstrap p95 release gate"
    )
    parser.add_argument("input", help="redacted raw paired-sample JSON")
    parser.add_argument("output", help="new immutable evidence JSON")
    parser.add_argument("--bootstrap-resamples", type=int, default=10_000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260716)
    args = parser.parse_args(argv)

    raw = load_benchmark_evidence(args.input)
    analysis = analyze_benchmark(
        raw,
        bootstrap_resamples=args.bootstrap_resamples,
        bootstrap_seed=args.bootstrap_seed,
    )
    path = write_benchmark_evidence(args.output, raw, analysis)
    print(
        json.dumps(
            {
                "status": "pass" if analysis["passed"] else "fail",
                "evidence_file": path.name,
                "workloads": {
                    name: result["passed"]
                    for name, result in analysis["workloads"].items()
                },
            },
            ensure_ascii=False,
        )
    )
    return 0 if analysis["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
