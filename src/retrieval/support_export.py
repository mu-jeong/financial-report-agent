"""Privacy-safe support evidence for native retrieval incidents and releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import faiss

from src.retrieval.bootstrap import inspect_runtime, retrieval_paths
from src.retrieval.identity import canonical_json
from src.retrieval.publication import read_durable_floors
from src.retrieval.schema import SchemaError, configure_catalog_storage


class SupportExportError(RuntimeError):
    """Raised when redacted support evidence cannot be proven complete."""


def build_support_payload(
    data_root: str | Path,
) -> dict[str, Any]:
    """Build a deterministic payload with no report/query text or machine paths."""

    paths = retrieval_paths(data_root)
    if not paths.catalog.is_file() or paths.catalog.is_symlink():
        raise SupportExportError("native retrieval catalog is unavailable")
    selection = inspect_runtime(
        paths.data_root,
        validate_snapshot=True,
    )
    connection = _open_read_only(paths.catalog)
    try:
        _validate_catalog(connection)
        runtime = connection.execute(
            """
            SELECT schema_version, active_snapshot_id, active_build_id,
                   predecessor_snapshot_id, publication_generation,
                   write_epoch, degraded, write_enabled
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
        active = connection.execute(
            """
            SELECT snapshot.snapshot_id, snapshot.file_sha256,
                   snapshot.size_bytes, snapshot.dimension, snapshot.metric,
                   snapshot.ntotal, snapshot.state AS snapshot_state,
                   build.build_id, build.source_manifest_sha256,
                   build.included_count, build.excluded_count,
                   build.expected_count, build.state AS build_state,
                   profile.profile_hash,
                   (SELECT COUNT(*) FROM snapshot_membership AS membership
                    WHERE membership.snapshot_id = snapshot.snapshot_id)
                       AS membership_count
            FROM vector_snapshots AS snapshot
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
            WHERE snapshot.snapshot_id = ?
            """,
            (selection.active_snapshot_id,),
        ).fetchone()
        if runtime is None or active is None:
            raise SupportExportError("native runtime has no complete active revision")

        report_counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM reports) AS source_objects,
                (SELECT COUNT(*) FROM active_reports) AS active_reports,
                (SELECT COUNT(*) FROM retrieval_parents) AS parents,
                (SELECT COUNT(*) FROM retrieval_chunks) AS chunks
            """
        ).fetchone()
        snapshot_states = {
            row[0]: int(row[1])
            for row in connection.execute(
                """
                SELECT state, COUNT(*) FROM vector_snapshots
                GROUP BY state ORDER BY state
                """
            )
        }
        build_states = {
            row[0]: int(row[1])
            for row in connection.execute(
                """
                SELECT state, COUNT(*) FROM retrieval_builds
                GROUP BY state ORDER BY state
                """
            )
        }
        publications = [
            {
                "publication_id": row[0],
                "from_snapshot_id": row[1],
                "to_snapshot_id": row[2],
                "phase": row[3],
                "state": row[4],
                "evidence_manifest_sha256": row[5],
                "error_code": row[6],
            }
            for row in connection.execute(
                """
                SELECT publication_id, from_snapshot_id, to_snapshot_id,
                       phase, state, evidence_manifest_sha256, error_code
                FROM publication_runs
                ORDER BY created_at, publication_id
                """
            )
        ]
    finally:
        connection.close()

    floors = [
        {
            "publication_id": floor.publication_id,
            "publication_generation": floor.publication_generation,
            "write_epoch": floor.write_epoch,
            "active_snapshot_id": floor.active_snapshot_id,
            "checkpoint_sha256": floor.checkpoint_sha256,
        }
        for floor in read_durable_floors(paths.data_root)
    ]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": "native_retrieval_support_export",
        "environment": {
            "os": os.name,
            "platform": platform.system(),
            "platform_release": platform.release(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "faiss_version": getattr(faiss, "__version__", "unknown"),
        },
        "runtime": {
            "mode": selection.mode,
            "schema_version": int(runtime[0]),
            "active_snapshot_id": runtime[1],
            "active_build_id": runtime[2],
            "predecessor_snapshot_id": runtime[3],
            "publication_generation": int(runtime[4]),
            "write_epoch": int(runtime[5]),
            "degraded": bool(runtime[6]),
            "write_enabled": bool(runtime[7]),
        },
        "active_revision": {
            "snapshot_id": active[0],
            "snapshot_sha256": active[1],
            "snapshot_size_bytes": int(active[2]),
            "dimension": int(active[3]),
            "metric": active[4],
            "ntotal": int(active[5]),
            "snapshot_state": active[6],
            "build_id": active[7],
            "source_manifest_sha256": active[8],
            "included_count": int(active[9]),
            "excluded_count": int(active[10]),
            "expected_count": int(active[11]),
            "build_state": active[12],
            "profile_hash": active[13],
            "membership_count": int(active[14]),
        },
        "catalog_counts": {
            "source_objects": int(report_counts[0]),
            "active_reports": int(report_counts[1]),
            "parents": int(report_counts[2]),
            "chunks": int(report_counts[3]),
        },
        "state_counts": {
            "snapshots": snapshot_states,
            "builds": build_states,
        },
        "committed_floors": floors,
        "publications": publications,
    }
    payload["manifest_sha256"] = _payload_hash(payload)
    return payload


def write_support_export(path: str | Path, payload: Mapping[str, Any]) -> Path:
    if payload.get("kind") != "native_retrieval_support_export":
        raise SupportExportError("support payload kind is invalid")
    expected_hash = payload.get("manifest_sha256")
    without_hash = dict(payload)
    without_hash.pop("manifest_sha256", None)
    if expected_hash != _payload_hash(without_hash):
        raise SupportExportError("support payload manifest hash is invalid")
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"support export already exists: {target.name}")
    encoded = (canonical_json(dict(payload)) + "\n").encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".support-{uuid.uuid4().hex[:12]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    from src.configs.config import DATA_ROOT

    parser = argparse.ArgumentParser(
        description="Export redacted native retrieval support evidence"
    )
    parser.add_argument("output", help="new JSON output path")
    parser.add_argument("--data-root", default=DATA_ROOT)
    args = parser.parse_args(argv)
    payload = build_support_payload(args.data_root)
    output = write_support_export(args.output, payload)
    print(
        json.dumps(
            {
                "status": "ok",
                "file": output.name,
                "manifest_sha256": payload["manifest_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        configure_catalog_storage(connection)
        connection.execute("PRAGMA query_only = ON")
        return connection
    except SchemaError as exc:
        connection.close()
        raise SupportExportError('native catalog storage mode is invalid') from exc


def _validate_catalog(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise SupportExportError("catalog quick_check failed")
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise SupportExportError("catalog integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise SupportExportError("catalog foreign-key check failed")


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SupportExportError",
    "build_support_payload",
    "write_support_export",
]
