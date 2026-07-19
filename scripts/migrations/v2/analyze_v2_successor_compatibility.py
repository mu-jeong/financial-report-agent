"""Measure and seal first-successor compatibility deltas without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


from src.retrieval.identity import canonical_json
from src.migrations.v2.validation.installed_probe import run_probe
from src.retrieval.vector_index import SnapshotDescriptor, load_index


class CompatibilityAnalysisError(RuntimeError):
    """Raised when a successor comparison cannot be proved."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyze predecessor/successor retrieval compatibility"
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--query-spec", type=Path, required=True)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve(strict=False)
    try:
        if output.exists() or output.is_symlink():
            raise CompatibilityAnalysisError("output path must be new")
        evidence = analyze_compatibility(args.data_root, args.query_spec, k=args.k)
        _write_immutable_json(output, evidence)
    except (OSError, ValueError, sqlite3.Error, CompatibilityAnalysisError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "passed": evidence["passed"],
                "approved": evidence["approved"],
                "predecessor_vector_queries": evidence["retrieval_quality"][
                    "predecessor_vector_queries"
                ],
                "unique_chunk_content_loss": evidence["structural_delta"][
                    "unique_chunk_content_loss"
                ],
                "evidence_sha256": _sha256_file(output),
            },
            sort_keys=True,
        )
    )
    return 0


def analyze_compatibility(
    data_root: str | Path,
    query_spec_path: str | Path,
    *,
    k: int,
) -> dict[str, Any]:
    root = _safe_directory(data_root)
    query_path = _safe_file(query_spec_path)
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise CompatibilityAnalysisError("comparison k must be positive")
    catalog = _catalog(root)
    connection = sqlite3.connect(
        f"file:{catalog.resolve(strict=True).as_posix()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        runtime = connection.execute(
            """
            SELECT active_snapshot_id, predecessor_snapshot_id,
                   publication_generation, write_epoch, v1_fallback_open,
                   degraded, write_enabled
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
        if (
            runtime is None
            or runtime["predecessor_snapshot_id"] is None
            or int(runtime["write_epoch"]) <= 0
            or bool(runtime["v1_fallback_open"])
            or bool(runtime["degraded"])
            or not bool(runtime["write_enabled"])
        ):
            raise CompatibilityAnalysisError(
                "comparison requires a healthy first successor and predecessor"
            )
        active = _load_snapshot(connection, root, str(runtime["active_snapshot_id"]))
        predecessor = _load_snapshot(
            connection,
            root,
            str(runtime["predecessor_snapshot_id"]),
        )
    finally:
        connection.close()

    active_reports = _group_reports(active["rows"])
    predecessor_reports = _group_reports(predecessor["rows"])
    common_uids = sorted(set(active_reports) & set(predecessor_reports))
    new_uids = sorted(set(active_reports) - set(predecessor_reports))
    missing_uids = sorted(set(predecessor_reports) - set(active_reports))
    active_content_keys = {_content_key(row) for row in active["rows"]}
    predecessor_content_keys = {
        _content_key(row) for row in predecessor["rows"]
    }
    multiplicity_deltas: list[dict[str, Any]] = []
    parent_order_deltas: list[dict[str, Any]] = []
    unique_loss = len(predecessor_content_keys - active_content_keys)
    removed_occurrences = 0
    for report_uid in common_uids:
        active_report = active_reports[report_uid]
        predecessor_report = predecessor_reports[report_uid]
        active_chunks = Counter(active_report["chunk_hashes"])
        predecessor_chunks = Counter(predecessor_report["chunk_hashes"])
        active_parents = Counter(active_report["parent_hashes"])
        predecessor_parents = Counter(predecessor_report["parent_hashes"])
        lost_hashes = set(predecessor_chunks) - set(active_chunks)
        removed = sum(
            max(0, count - active_chunks.get(chunk_hash, 0))
            for chunk_hash, count in predecessor_chunks.items()
        )
        removed_occurrences += removed
        if removed and not lost_hashes:
            multiplicity_deltas.append(
                _report_delta(report_uid, predecessor_report, active_report)
            )
        if (
            predecessor_chunks == active_chunks
            and predecessor_parents == active_parents
            and predecessor_report["parent_hashes"] != active_report["parent_hashes"]
        ):
            parent_order_deltas.append(
                _report_delta(report_uid, predecessor_report, active_report)
            )

    predecessor_rows = predecessor["rows_by_id"]
    active_rows = active["rows_by_id"]
    predecessor_ids = sorted(predecessor_rows)
    vectors = predecessor["index"].reconstruct(predecessor_ids)
    expected_rank_one = 0
    expected_within_k = 0
    source_top_one_equal = 0
    citation_complete = 0
    source_recalls: list[float] = []
    for physical_id, query in zip(predecessor_ids, vectors, strict=True):
        expected_row = predecessor_rows[physical_id]
        expected_key = _content_key(expected_row)
        active_hits = active["index"].search(query, min(k, active["descriptor"].ntotal))
        predecessor_hits = predecessor["index"].search(
            query,
            min(k, predecessor["descriptor"].ntotal),
        )
        active_hit_rows = [active_rows[hit.physical_id] for hit in active_hits]
        active_keys = [_content_key(row) for row in active_hit_rows]
        rank = next(
            (index for index, key in enumerate(active_keys, 1) if key == expected_key),
            None,
        )
        expected_rank_one += rank == 1
        expected_within_k += rank is not None
        if active_hit_rows:
            source_top_one_equal += (
                active_hit_rows[0]["canonical_relative_path"]
                == expected_row["canonical_relative_path"]
            )
            citation_complete += _citation_complete(active_hit_rows[0])
        predecessor_sources = {
            predecessor_rows[hit.physical_id]["canonical_relative_path"]
            for hit in predecessor_hits
        }
        active_sources = {
            row["canonical_relative_path"] for row in active_hit_rows
        }
        source_recalls.append(
            len(predecessor_sources & active_sources) / len(predecessor_sources)
            if predecessor_sources
            else 1.0
        )

    query_value = _read_json(query_path)
    gate_probe = run_probe(
        root,
        query_value,
        samples=2,
        query_spec_sha256=_sha256_file(query_path),
    )
    gate_passed = (
        gate_probe["passed"] is True
        and gate_probe["gate_d_search"]["top_rank"] == 1
        and gate_probe["gate_d_search"]["citation_complete"] is True
    )
    query_count = len(predecessor_ids)
    structural_passed = (
        not missing_uids
        and unique_loss == 0
        and all(_content_key(row) in active_content_keys for row in predecessor_rows.values())
    )
    quality_passed = (
        expected_rank_one == query_count
        and expected_within_k == query_count
        and source_top_one_equal == query_count
        and citation_complete == query_count
        and gate_passed
    )
    return {
        "schema_version": 1,
        "kind": "v2_successor_compatibility_exception_evidence",
        "passed": structural_passed and quality_passed,
        "approved": False,
        "release_eligible": False,
        "compatibility_exception_required": bool(
            multiplicity_deltas or parent_order_deltas
        ),
        "runtime": {
            "active_snapshot_id": runtime["active_snapshot_id"],
            "predecessor_snapshot_id": runtime["predecessor_snapshot_id"],
            "publication_generation": int(runtime["publication_generation"]),
            "write_epoch": int(runtime["write_epoch"]),
        },
        "snapshot_descriptors": {
            "active": _descriptor_payload(active),
            "predecessor": _descriptor_payload(predecessor),
        },
        "structural_delta": {
            "active_report_count": len(active_reports),
            "predecessor_report_count": len(predecessor_reports),
            "common_report_count": len(common_uids),
            "new_report_count": len(new_uids),
            "new_report_uids": new_uids,
            "missing_report_count": len(missing_uids),
            "missing_report_uids": missing_uids,
            "unique_chunk_content_loss": unique_loss,
            "removed_duplicate_occurrences": removed_occurrences,
            "multiplicity_delta_report_count": len(multiplicity_deltas),
            "multiplicity_delta_reports": multiplicity_deltas,
            "parent_order_delta_report_count": len(parent_order_deltas),
            "parent_order_delta_reports": parent_order_deltas,
        },
        "retrieval_quality": {
            "k": k,
            "predecessor_vector_queries": query_count,
            "expected_content_rank_one": expected_rank_one,
            "expected_content_within_k": expected_within_k,
            "source_top_one_equal": source_top_one_equal,
            "citation_complete_top_one": citation_complete,
            "minimum_source_set_recall_at_k": min(source_recalls),
            "mean_source_set_recall_at_k": sum(source_recalls) / len(source_recalls),
            "gate_d_query_passed": gate_passed,
            "gate_d_query_spec_sha256": _sha256_file(query_path),
            "gate_d_expected_report_uid": gate_probe["gate_d_search"][
                "expected_report_uid"
            ],
        },
        "proposed_exception": {
            "reason_codes": [
                "collapse_exact_v1_duplicate_occurrences",
                "canonicalize_parent_order_from_source_rebuild",
            ],
            "unique_content_loss_allowed": 0,
            "citation_loss_allowed": 0,
            "approval_required": True,
        },
    }


def _load_snapshot(
    connection: sqlite3.Connection,
    root: Path,
    snapshot_id: str,
) -> dict[str, Any]:
    descriptor_row = connection.execute(
        """
        SELECT relative_path, file_sha256, size_bytes, dimension, metric, ntotal
        FROM vector_snapshots WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if descriptor_row is None:
        raise CompatibilityAnalysisError("snapshot descriptor is missing")
    descriptor = SnapshotDescriptor(
        sha256=str(descriptor_row[1]),
        size_bytes=int(descriptor_row[2]),
        dimension=int(descriptor_row[3]),
        metric=str(descriptor_row[4]),
        ntotal=int(descriptor_row[5]),
    )
    index = load_index(
        root.joinpath(*PurePosixPath(str(descriptor_row[0])).parts),
        descriptor,
    )
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT membership.faiss_id, report.report_uid,
                   report.canonical_relative_path, report.report_type,
                   report.report_date, report.target_name, report.title,
                   report.broker, parent.parent_order, parent.content_sha256,
                   chunk.child_order, chunk.embedding_text_sha256
            FROM snapshot_membership AS membership
            JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE membership.snapshot_id = ?
            ORDER BY membership.faiss_id
            """,
            (snapshot_id,),
        ).fetchall()
    ]
    if len(rows) != descriptor.ntotal:
        raise CompatibilityAnalysisError("snapshot membership count is invalid")
    return {
        "snapshot_id": snapshot_id,
        "descriptor": descriptor,
        "index": index,
        "rows": rows,
        "rows_by_id": {int(row["faiss_id"]): row for row in rows},
    }


def _group_reports(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        rows,
        key=lambda row: (
            row["report_uid"],
            int(row["parent_order"]),
            int(row["child_order"]),
        ),
    )
    for row in ordered:
        report = grouped.setdefault(
            str(row["report_uid"]),
            {
                "canonical_relative_path": str(row["canonical_relative_path"]),
                "parent_hashes": [],
                "chunk_hashes": [],
                "seen_parent_orders": set(),
            },
        )
        parent_order = int(row["parent_order"])
        if parent_order not in report["seen_parent_orders"]:
            report["parent_hashes"].append(str(row["content_sha256"]))
            report["seen_parent_orders"].add(parent_order)
        report["chunk_hashes"].append(str(row["embedding_text_sha256"]))
    for report in grouped.values():
        report.pop("seen_parent_orders")
    return grouped


def _report_delta(
    report_uid: str,
    predecessor: dict[str, Any],
    active: dict[str, Any],
) -> dict[str, Any]:
    return {
        "report_uid": report_uid,
        "canonical_relative_path": active["canonical_relative_path"],
        "predecessor_parent_count": len(predecessor["parent_hashes"]),
        "active_parent_count": len(active["parent_hashes"]),
        "predecessor_chunk_count": len(predecessor["chunk_hashes"]),
        "active_chunk_count": len(active["chunk_hashes"]),
    }


def _descriptor_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    descriptor = snapshot["descriptor"]
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "sha256": descriptor.sha256,
        "size_bytes": descriptor.size_bytes,
        "dimension": descriptor.dimension,
        "metric": descriptor.metric,
        "ntotal": descriptor.ntotal,
    }


def _content_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["report_uid"]), str(row["embedding_text_sha256"])


def _citation_complete(row: dict[str, Any]) -> bool:
    return all(
        isinstance(row.get(field), str) and bool(row[field].strip())
        for field in (
            "canonical_relative_path",
            "report_type",
            "report_date",
            "title",
            "broker",
        )
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompatibilityAnalysisError("query specification is unreadable") from exc
    if not isinstance(value, dict):
        raise CompatibilityAnalysisError("query specification must be an object")
    return value


def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".compat-{uuid.uuid4().hex[:12]}.tmp"
    try:
        encoded = (canonical_json(payload) + "\n").encode("utf-8")
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
        os.chmod(path, stat.S_IREAD)
    finally:
        temporary.unlink(missing_ok=True)


def _catalog(root: Path) -> Path:
    direct = root / "catalog.sqlite3"
    return direct if direct.is_file() else root / "retrieval" / "v2" / "catalog.sqlite3"


def _safe_directory(value: str | Path) -> Path:
    path = Path(value).resolve(strict=True)
    if not path.is_dir() or path.is_symlink():
        raise CompatibilityAnalysisError("data root is unavailable or unsafe")
    return path


def _safe_file(value: str | Path) -> Path:
    path = Path(value).resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise CompatibilityAnalysisError("query specification is unavailable or unsafe")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
