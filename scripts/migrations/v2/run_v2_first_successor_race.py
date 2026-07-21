"""Build and publish the first successor while installed launchers race it."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.retrieval.build_service import (
    materialize_candidate,
    prepare_full_corpus_build,
)
from src.retrieval.identity import canonical_json
from src.migrations.v2.validation.launcher_race import (
    capture_epoch_zero_installed_baseline,
    publish_candidate_with_launcher_race,
)
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from src.retrieval.writer_lock import WriterLease


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish the first native successor under a source/package launcher race"
        )
    )
    parser.add_argument("--source-install", type=Path, required=True)
    parser.add_argument("--packaged-install", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if args.workers < 2:
        parser.error("--workers must be at least 2")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    from src.configs import config
    from src.core.embed_pipeline import build_embeddings_fn

    data_root = Path(config.DB_PATH).resolve(strict=False).parent
    source_root = Path(config.SAVE_DIR).resolve(strict=True)
    install_roots = {
        "source-default": args.source_install,
        "packaged-default": args.packaged_install,
    }
    baseline = capture_epoch_zero_installed_baseline(
        data_root,
        install_roots,
        process_timeout_seconds=args.timeout_seconds,
    )
    embeddings = build_embeddings_fn()

    def build_candidate(writer_lease: WriterLease):
        recovery = StartupReconciler(data_root).reconcile(
            writer_lease=writer_lease,
        )
        if recovery.disposition == RecoveryDisposition.FAIL_CLOSED:
            raise RuntimeError("startup reconciliation failed closed")
        plan = prepare_full_corpus_build(
            config.DB_PATH,
            source_root,
            data_root=data_root,
            embeddings=embeddings,
            model=config.EMBEDDING_MODEL,
            extractor_name=config.EXTRACTION_ENGINE,
            parent_chunk_size=config.PARENT_CHUNK_SIZE,
            child_chunk_size=config.CHILD_CHUNK_SIZE,
            metric="l2",
            normalization="none",
            allow_degraded_forward_recovery=True,
            writer_lease=writer_lease,
        )
        return materialize_candidate(
            plan,
            data_root,
            writer_lease=writer_lease,
        )

    raced = publish_candidate_with_launcher_race(
        None,
        data_root,
        candidate_factory=build_candidate,
        install_roots=install_roots,
        installed_baseline=baseline,
        worker_count=args.workers,
        process_timeout_seconds=args.timeout_seconds,
    )
    candidate = raced.candidate

    payload = {
        "schema_version": 1,
        "kind": "v2_first_successor_execution",
        "candidate": {
            "build_id": candidate.build_id,
            "snapshot_id": candidate.snapshot_id,
            "report_count": candidate.report_count,
            "parent_count": candidate.parent_count,
            "chunk_count": candidate.chunk_count,
            "evidence_manifest_sha256": candidate.evidence_manifest_sha256,
        },
        "publication": {
            "publication_id": raced.publication.publication_id,
            "publication_generation": raced.publication.publication_generation,
            "write_epoch": raced.publication.write_epoch,
            "active_snapshot_id": raced.publication.active_snapshot_id,
            "predecessor_snapshot_id": raced.publication.predecessor_snapshot_id,
            "v1_fallback_open": raced.publication.v1_fallback_open,
            "checkpoint_sha256": raced.publication.checkpoint_sha256,
        },
        "launcher_race": raced.evidence,
        "passed": bool(raced.evidence.get("passed"))
        and bool(raced.evidence.get("release_eligible")),
    }
    _write_immutable_json(args.output, payload)
    print(
        json.dumps(
            {
                "status": "passed" if payload["passed"] else "failed",
                "evidence": args.output.name,
                "write_epoch": raced.publication.write_epoch,
            },
            ensure_ascii=False,
        )
    )
    return 0 if payload["passed"] else 1


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(f"successor race evidence already exists: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.parent / f".successor-race-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
