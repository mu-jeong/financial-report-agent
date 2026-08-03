"""Project runtime/data status helpers.

The functions in this module are intentionally read-only. They are used by the
CLI, Streamlit UI, and tests to make the current local data state visible
without mutating the SQLite DB or FAISS index.
"""

from __future__ import annotations

from collections import Counter
import json
import os
import sqlite3
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from src.configs import config
from src.retrieval.bootstrap import (
    RetrievalBootstrapError,
    RetrievalPaths,
    inspect_runtime,
    retrieval_paths,
)
from src.retrieval.delta_schema import delta_schema_installed

DB_PATH = config.DB_PATH
EMBEDDING_MODEL = config.EMBEDDING_MODEL
EXTRACTION_ENGINE = config.EXTRACTION_ENGINE
FAISS_DIR = config.FAISS_DIR
GENERATION_MODEL = config.GENERATION_MODEL
SAVE_DIR = config.SAVE_DIR
SEARCH_TOP_K = config.SEARCH_TOP_K
TEST_LIMIT = config.TEST_LIMIT
UNEMBEDDED_EXTRACTION_ENGINE = getattr(config, "UNEMBEDDED_EXTRACTION_ENGINE", "")
USE_PARENT_CHILD = config.USE_PARENT_CHILD
USE_RERANKER = config.USE_RERANKER


def _safe_count_pdfs(save_dir: str) -> int:
    path = Path(save_dir)
    if not path.is_dir():
        return 0
    with os.scandir(path) as entries:
        return sum(
            1
            for entry in entries
            if entry.is_file() and entry.name.casefold().endswith(".pdf")
        )


def _safe_vector_info(faiss_dir: str) -> dict[str, Any]:
    path = Path(faiss_dir)
    files: list[dict[str, Any]] = []
    total_size = 0

    if path.exists():
        for child in sorted(path.iterdir()):
            if child.is_file():
                size = child.stat().st_size
                files.append({"name": child.name, "size_bytes": size})
                total_size += size

    return {
        "exists": path.exists(),
        "file_count": len(files),
        "total_size_bytes": total_size,
        "files": files,
        "has_faiss_index": (path / "index.faiss").exists(),
        "has_pickle_index": (path / "index.pkl").exists(),
    }


def _status_retrieval_paths(
    db_path: str | Path,
) -> tuple[RetrievalPaths, Path | None]:
    """Resolve either a V1 anchor or the canonical V2 catalog path.

    ``get_data_status`` exposes the active catalog as ``paths.db_path`` in
    native mode. Callers may pass that value back into a status helper, so it
    must not be interpreted as though its parent were the data root.
    """
    absolute = Path(
        os.path.abspath(os.path.expanduser(str(db_path)))
    )
    is_canonical_catalog = (
        absolute.name.casefold() == "catalog.sqlite3"
        and absolute.parent.name.casefold() == "v2"
        and absolute.parent.parent.name.casefold() == "retrieval"
    )
    data_root = absolute.parents[2] if is_canonical_catalog else None
    paths = (
        retrieval_paths(db_path, data_root=data_root)
        if data_root is not None
        else retrieval_paths(db_path)
    )
    return paths, data_root


def _safe_db_info(db_path: str) -> dict[str, Any]:
    path = Path(db_path)
    info: dict[str, Any] = {
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "total_reports": 0,
        "embedded_reports": 0,
        "pending_reports": 0,
        "parent_chunks": 0,
        "min_report_date": None,
        "max_report_date": None,
        "report_date_counts": {},
        "report_date_type_counts": {},
        "report_types": {},
        "error": None,
    }
    if not path.exists():
        return info

    try:
        db_uri = f"file:{os.path.abspath(db_path)}?mode=ro"
        with sqlite3.connect(db_uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row

            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_reports,
                    COALESCE(SUM(CASE WHEN is_embedded = 1 THEN 1 ELSE 0 END), 0) AS embedded_reports,
                    MIN(CASE WHEN report_date IS NOT NULL AND TRIM(report_date) != '' THEN SUBSTR(TRIM(report_date), 1, 10) END) AS min_report_date,
                    MAX(CASE WHEN report_date IS NOT NULL AND TRIM(report_date) != '' THEN SUBSTR(TRIM(report_date), 1, 10) END) AS max_report_date
                FROM reports
                """
            ).fetchone()
            if row:
                total = int(row["total_reports"] or 0)
                embedded = int(row["embedded_reports"] or 0)
                info.update(
                    {
                        "total_reports": total,
                        "embedded_reports": embedded,
                        "pending_reports": max(total - embedded, 0),
                        "min_report_date": row["min_report_date"],
                        "max_report_date": row["max_report_date"],
                    }
                )

            try:
                parent_row = conn.execute("SELECT COUNT(*) AS count FROM parent_chunks").fetchone()
                info["parent_chunks"] = int(parent_row["count"] if parent_row else 0)
            except sqlite3.Error:
                info["parent_chunks"] = 0

            report_types = conn.execute(
                "SELECT report_type, COUNT(*) AS count FROM reports GROUP BY report_type ORDER BY report_type"
            ).fetchall()
            info["report_types"] = {row["report_type"]: int(row["count"]) for row in report_types}

            report_dates = conn.execute(
                """
                SELECT SUBSTR(TRIM(report_date), 1, 10) AS report_date, COUNT(*) AS count
                FROM reports
                WHERE report_date IS NOT NULL AND TRIM(report_date) != ''
                  AND is_embedded = 1
                GROUP BY SUBSTR(TRIM(report_date), 1, 10)
                ORDER BY SUBSTR(TRIM(report_date), 1, 10)
                """
            ).fetchall()
            info["report_date_counts"] = {
                row["report_date"]: int(row["count"])
                for row in report_dates
            }

            report_date_types = conn.execute(
                """
                SELECT
                    SUBSTR(TRIM(report_date), 1, 10) AS report_date,
                    TRIM(report_type) AS report_type,
                    COUNT(*) AS count
                FROM reports
                WHERE report_date IS NOT NULL AND TRIM(report_date) != ''
                  AND report_type IS NOT NULL AND TRIM(report_type) != ''
                  AND is_embedded = 1
                GROUP BY SUBSTR(TRIM(report_date), 1, 10), TRIM(report_type)
                ORDER BY SUBSTR(TRIM(report_date), 1, 10), TRIM(report_type)
                """
            ).fetchall()
            date_type_counts: dict[str, dict[str, int]] = {}
            for row in report_date_types:
                date_key = row["report_date"]
                type_key = row["report_type"]
                date_type_counts.setdefault(date_key, {})[type_key] = int(row["count"])
            info["report_date_type_counts"] = date_type_counts
    except sqlite3.Error as exc:
        info["error"] = str(exc)

    return info


def _native_delta_status_from_manifest(
    connection: sqlite3.Connection,
    *,
    active_build_id: str,
    active_snapshot_id: str,
    publication_generation: int,
) -> dict[str, Any] | None:
    """Summarize active delta state without repeatedly expanding active views.

    The build manifest fixes the base report membership. Ready delta heads then
    replace or delete reports by canonical path. Older or synthetic catalogs
    that do not carry the structured manifest fall back to the view-based path.
    """

    build = connection.execute(
        """
        SELECT source_manifest_json, included_count
        FROM retrieval_builds
        WHERE build_id = ?
        """,
        (active_build_id,),
    ).fetchone()
    if build is None:
        return None
    try:
        manifest = json.loads(str(build["source_manifest_json"]))
    except (TypeError, ValueError):
        return None
    manifest_reports = manifest.get("reports") if isinstance(manifest, dict) else None
    if not isinstance(manifest_reports, list):
        return None
    included_uids = {
        str(entry.get("report_uid"))
        for entry in manifest_reports
        if (
            isinstance(entry, dict)
            and entry.get("status") == "included"
            and entry.get("report_uid")
        )
    }
    if len(included_uids) != int(build["included_count"]):
        return None

    report_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT report_id, report_uid, canonical_relative_path,
                   report_type, report_date
            FROM reports
            """
        )
    ]
    reports_by_uid = {str(row["report_uid"]): row for row in report_rows}
    if not included_uids <= reports_by_uid.keys():
        return None

    base_membership_rows = list(
        connection.execute(
            """
            SELECT report.canonical_relative_path,
                   COUNT(*) AS membership_count,
                   COUNT(DISTINCT parent.parent_uid) AS parent_count
            FROM snapshot_membership AS membership
            JOIN retrieval_chunks AS chunk
              ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent
              ON parent.parent_uid = chunk.parent_uid
             AND parent.profile_id = chunk.profile_id
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE membership.snapshot_id = ?
            GROUP BY report.canonical_relative_path
            """,
            (active_snapshot_id,),
        )
    )
    base_membership_by_path = {
        str(row["canonical_relative_path"]): int(row["membership_count"])
        for row in base_membership_rows
    }
    base_parents_by_path = {
        str(row["canonical_relative_path"]): int(row["parent_count"])
        for row in base_membership_rows
    }
    active_by_path = {
        str(reports_by_uid[report_uid]["canonical_relative_path"]): reports_by_uid[
            report_uid
        ]
        for report_uid in included_uids
        if base_membership_by_path.get(
            str(reports_by_uid[report_uid]["canonical_relative_path"]),
            0,
        )
        > 0
    }

    heads = [
        dict(row)
        for row in connection.execute(
            """
            WITH ready_segments AS (
                SELECT segment.segment_id, segment.sequence,
                       segment.relative_path
                FROM retrieval_delta_segments AS segment
                WHERE segment.base_snapshot_id = ?
                  AND segment.base_publication_generation = ?
                  AND segment.state = 'ready'
            ),
            ranked_heads AS (
                SELECT action.canonical_relative_path, action.action,
                       action.report_uid, action.segment_id,
                       segment.sequence, segment.relative_path,
                       row_number() OVER (
                           PARTITION BY action.canonical_relative_path
                           ORDER BY segment.sequence DESC, segment.segment_id DESC
                       ) AS position
                FROM retrieval_delta_reports AS action
                JOIN ready_segments AS segment
                  ON segment.segment_id = action.segment_id
                WHERE action.action IN ('upsert', 'delete')
            )
            SELECT canonical_relative_path, action, report_uid, segment_id,
                   sequence, relative_path
            FROM ranked_heads
            WHERE position = 1
            """,
            (active_snapshot_id, publication_generation),
        )
    ]
    for head in heads:
        canonical_path = str(head["canonical_relative_path"])
        active_by_path.pop(canonical_path, None)
        if head["action"] != "upsert":
            continue
        replacement = reports_by_uid.get(str(head["report_uid"]))
        if replacement is None:
            return None
        active_by_path[canonical_path] = replacement

    active_rows = list(active_by_path.values())
    active_uids = {str(row["report_uid"]) for row in active_rows}
    latest_by_path: dict[str, dict[str, Any]] = {}
    for row in report_rows:
        canonical_path = str(row["canonical_relative_path"])
        previous = latest_by_path.get(canonical_path)
        if previous is None or int(row["report_id"]) > int(previous["report_id"]):
            latest_by_path[canonical_path] = row
    latest_rows = list(latest_by_path.values())
    embedded_count = sum(
        1 for row in latest_rows if str(row["report_uid"]) in active_uids
    )

    report_types = Counter(str(row["report_type"]) for row in active_rows)
    report_dates = Counter(str(row["report_date"]) for row in active_rows)
    report_date_types: dict[str, Counter[str]] = {}
    for row in active_rows:
        report_date_types.setdefault(
            str(row["report_date"]),
            Counter(),
        )[str(row["report_type"])] += 1

    delta_membership = {
        (str(row["segment_id"]), str(row["report_uid"])): (
            int(row["membership_count"]),
            int(row["parent_count"]),
        )
        for row in connection.execute(
            """
            SELECT membership.segment_id, report.report_uid,
                   COUNT(*) AS membership_count,
                   COUNT(DISTINCT parent.parent_uid) AS parent_count
            FROM retrieval_delta_membership AS membership
            JOIN retrieval_delta_segments AS segment
              ON segment.segment_id = membership.segment_id
             AND segment.base_snapshot_id = ?
             AND segment.base_publication_generation = ?
             AND segment.state = 'ready'
            JOIN retrieval_chunks AS chunk
              ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent
              ON parent.parent_uid = chunk.parent_uid
             AND parent.profile_id = chunk.profile_id
            JOIN reports AS report ON report.report_id = parent.report_id
            GROUP BY membership.segment_id, report.report_uid
            """,
            (active_snapshot_id, publication_generation),
        )
    }
    replaced_paths = {str(head["canonical_relative_path"]) for head in heads}
    membership_count = sum(
        count
        for canonical_path, count in base_membership_by_path.items()
        if canonical_path not in replaced_paths
    )
    parent_chunk_count = sum(
        count
        for canonical_path, count in base_parents_by_path.items()
        if canonical_path not in replaced_paths
    )
    active_delta_paths: dict[str, int] = {}
    for head in heads:
        if head["action"] != "upsert":
            continue
        member_count, parent_count = delta_membership.get(
            (str(head["segment_id"]), str(head["report_uid"])),
            (0, 0),
        )
        membership_count += member_count
        parent_chunk_count += parent_count
        relative_path = head.get("relative_path")
        if member_count and relative_path:
            active_delta_paths[str(relative_path)] = int(head["sequence"])

    active_dates = [str(row["report_date"]) for row in active_rows]
    total_reports = len(latest_rows)
    return {
        "total_reports": total_reports,
        "embedded_reports": embedded_count,
        "pending_reports": max(total_reports - embedded_count, 0),
        "parent_chunks": parent_chunk_count,
        "min_report_date": min(active_dates) if active_dates else None,
        "max_report_date": max(active_dates) if active_dates else None,
        "report_types": dict(report_types),
        "report_date_counts": dict(report_dates),
        "report_date_type_counts": {
            report_date: dict(counts)
            for report_date, counts in report_date_types.items()
        },
        "membership_count": membership_count,
        "delta_artifact_paths": [
            relative_path
            for relative_path, _sequence in sorted(
                active_delta_paths.items(),
                key=lambda item: (item[1], item[0]),
            )
        ],
    }


def _safe_native_info(
    db_path: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    paths, data_root = _status_retrieval_paths(db_path)
    try:
        # The background search-engine warmup owns full FAISS validation.
        # Sidebar status needs catalog metadata only and must remain cheap.
        selection = inspect_runtime(
            db_path,
            data_root=data_root,
            validate_snapshot=False,
            catalog_validation="read",
        )
    except Exception as exc:
        return _unavailable_native_info(paths, exc)
    if selection.mode == "legacy_v1":
        return None
    db = {
        "exists": True,
        "size_bytes": paths.catalog.stat().st_size,
        "total_reports": 0,
        "embedded_reports": 0,
        "pending_reports": 0,
        "parent_chunks": 0,
        "min_report_date": None,
        "max_report_date": None,
        "report_date_counts": {},
        "report_date_type_counts": {},
        "report_types": {},
        "error": None,
    }
    retrieval: dict[str, Any] = {
        "mode": "native",
        "catalog_path": str(paths.catalog),
        "schema_version": None,
        "publication_generation": None,
        "write_epoch": None,
        "active_build_id": None,
        "active_snapshot_id": None,
        "predecessor_snapshot_id": None,
        "v1_fallback_open": None,
        "compatibility_bundle_id": None,
        "degraded": None,
        "write_enabled": None,
        "membership_count": 0,
        "profile_hash": None,
        "build_state": None,
        "snapshot_state": None,
        "error": None,
    }
    vector_db: dict[str, Any] = {
        "exists": False,
        "file_count": 0,
        "total_size_bytes": 0,
        "files": [],
        "has_faiss_index": False,
        "has_pickle_index": False,
        "legacy_pickle_bridge": False,
        "snapshot_id": None,
        "ntotal": 0,
    }
    try:
        retrieval.update(
            {
                "mode": selection.mode,
                "publication_generation": selection.publication_generation,
                "write_epoch": selection.write_epoch,
                "active_build_id": selection.active_build_id,
                "active_snapshot_id": selection.active_snapshot_id,
                "predecessor_snapshot_id": selection.predecessor_snapshot_id,
                "v1_fallback_open": selection.v1_fallback_open,
                "compatibility_bundle_id": selection.compatibility_bundle_id,
                "degraded": selection.degraded,
                "write_enabled": selection.write_enabled,
            }
        )
        connection = sqlite3.connect(paths.catalog)
        connection.row_factory = sqlite3.Row
        try:
            has_delta_schema = delta_schema_installed(connection)
            if has_delta_schema:
                retrieval.update(
                    _pending_cleanup_summary(connection, paths.data_root)
                )
            runtime = connection.execute(
                "SELECT schema_version FROM retrieval_runtime WHERE runtime_id = 1"
            ).fetchone()
            retrieval["schema_version"] = int(runtime[0]) if runtime else None
            manifest_status = None
            if (
                has_delta_schema
                and selection.active_build_id
                and selection.active_snapshot_id
            ):
                manifest_status = _native_delta_status_from_manifest(
                    connection,
                    active_build_id=str(selection.active_build_id),
                    active_snapshot_id=str(selection.active_snapshot_id),
                    publication_generation=int(selection.publication_generation),
                )
            if manifest_status is not None:
                db.update(
                    {
                        key: manifest_status[key]
                        for key in (
                            "total_reports",
                            "embedded_reports",
                            "pending_reports",
                            "parent_chunks",
                            "min_report_date",
                            "max_report_date",
                            "report_types",
                            "report_date_counts",
                            "report_date_type_counts",
                        )
                    }
                )
            else:
                summary = connection.execute(
                    """
                    WITH latest AS (
                        SELECT canonical_relative_path, MAX(report_id) AS report_id
                        FROM reports GROUP BY canonical_relative_path
                    )
                    SELECT COUNT(*) AS total_reports,
                           COALESCE(SUM(CASE WHEN active.report_uid IS NOT NULL THEN 1 ELSE 0 END), 0)
                               AS embedded_reports,
                           MIN(CASE WHEN active.report_uid IS NOT NULL THEN report.report_date END)
                               AS min_report_date,
                           MAX(CASE WHEN active.report_uid IS NOT NULL THEN report.report_date END)
                               AS max_report_date
                    FROM latest
                    JOIN reports AS report ON report.report_id = latest.report_id
                    LEFT JOIN active_reports AS active ON active.report_uid = report.report_uid
                    """
                ).fetchone()
                total = int(summary["total_reports"] or 0)
                embedded = int(summary["embedded_reports"] or 0)
                db.update(
                    {
                        "total_reports": total,
                        "embedded_reports": embedded,
                        "pending_reports": max(total - embedded, 0),
                        "min_report_date": summary["min_report_date"],
                        "max_report_date": summary["max_report_date"],
                    }
                )
                membership_source = (
                    "active_vector_membership"
                    if has_delta_schema
                    else "snapshot_membership"
                )
                membership_filter = (
                    "" if has_delta_schema else "WHERE membership.snapshot_id = ?"
                )
                membership_parameters = (
                    () if has_delta_schema else (selection.active_snapshot_id,)
                )
                db["parent_chunks"] = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(DISTINCT parent.parent_uid)
                        FROM {membership_source} AS membership
                        JOIN retrieval_chunks AS chunk
                          ON chunk.chunk_uid = membership.chunk_uid
                        JOIN retrieval_parents AS parent
                          ON parent.parent_uid = chunk.parent_uid
                        {membership_filter}
                        """,
                        membership_parameters,
                    ).fetchone()[0]
                )
                type_rows = connection.execute(
                    "SELECT report_type, COUNT(*) AS count "
                    "FROM active_reports GROUP BY report_type"
                ).fetchall()
                db["report_types"] = {
                    row["report_type"]: int(row["count"]) for row in type_rows
                }
                date_rows = connection.execute(
                    "SELECT report_date, COUNT(*) AS count "
                    "FROM active_reports GROUP BY report_date"
                ).fetchall()
                db["report_date_counts"] = {
                    row["report_date"]: int(row["count"]) for row in date_rows
                }
                date_type_rows = connection.execute(
                    """
                    SELECT report_date, report_type, COUNT(*) AS count
                    FROM active_reports GROUP BY report_date, report_type
                    """
                ).fetchall()
                date_types: dict[str, dict[str, int]] = {}
                for row in date_type_rows:
                    date_types.setdefault(row["report_date"], {})[
                        row["report_type"]
                    ] = int(row["count"])
                db["report_date_type_counts"] = date_types
            snapshot = connection.execute(
                """
                SELECT snapshot.relative_path, snapshot.size_bytes, snapshot.ntotal,
                       snapshot.state AS snapshot_state, build.state AS build_state,
                       profile.profile_hash,
                       (SELECT COUNT(*) FROM snapshot_membership AS membership
                        WHERE membership.snapshot_id = snapshot.snapshot_id) AS membership_count
                FROM vector_snapshots AS snapshot
                JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
                JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
                WHERE snapshot.snapshot_id = ?
                """,
                (selection.active_snapshot_id,),
            ).fetchone()
            if snapshot:
                snapshot_path = paths.data_root.joinpath(*snapshot["relative_path"].split("/"))
                size = snapshot_path.stat().st_size if snapshot_path.is_file() else 0
                retrieval.update(
                    {
                        "membership_count": int(snapshot["membership_count"]),
                        "profile_hash": snapshot["profile_hash"],
                        "build_state": snapshot["build_state"],
                        "snapshot_state": snapshot["snapshot_state"],
                    }
                )
                vector_db.update(
                    {
                        "exists": snapshot_path.parent.exists(),
                        "file_count": int(snapshot_path.is_file()),
                        "total_size_bytes": size,
                        "files": (
                            [{"name": snapshot_path.name, "size_bytes": size}]
                            if snapshot_path.is_file()
                            else []
                        ),
                        "has_faiss_index": selection.mode == "native" and snapshot_path.is_file(),
                        "legacy_pickle_bridge": selection.mode == "epoch_zero_compatibility",
                        "snapshot_id": selection.active_snapshot_id,
                        "ntotal": int(snapshot["ntotal"]),
                    }
                )
                if has_delta_schema:
                    delta_summary = connection.execute(
                        """
                        SELECT COALESCE(MAX(sequence), 0) AS generation,
                               COUNT(*) AS segment_count
                        FROM retrieval_delta_segments
                        WHERE base_snapshot_id = ?
                          AND base_publication_generation = ?
                          AND state = 'ready'
                        """,
                        (
                            selection.active_snapshot_id,
                            selection.publication_generation,
                        ),
                    ).fetchone()
                    if manifest_status is None:
                        active_membership_count = int(
                            connection.execute(
                                "SELECT COUNT(*) FROM active_vector_membership"
                            ).fetchone()[0]
                        )
                        delta_artifacts = connection.execute(
                            """
                            SELECT segment.relative_path
                            FROM retrieval_delta_segments AS segment
                            WHERE segment.base_snapshot_id = ?
                              AND segment.base_publication_generation = ?
                              AND segment.state = 'ready'
                              AND segment.relative_path IS NOT NULL
                              AND EXISTS (
                                  SELECT 1
                                  FROM active_vector_membership AS membership
                                  WHERE membership.artifact_kind = 'delta'
                                    AND membership.artifact_id = segment.segment_id
                              )
                            ORDER BY segment.sequence, segment.segment_id
                            """,
                            (
                                selection.active_snapshot_id,
                                selection.publication_generation,
                            ),
                        ).fetchall()
                        delta_artifact_paths = [
                            str(artifact["relative_path"])
                            for artifact in delta_artifacts
                        ]
                    else:
                        active_membership_count = int(
                            manifest_status["membership_count"]
                        )
                        delta_artifact_paths = list(
                            manifest_status["delta_artifact_paths"]
                        )
                    retrieval.update(
                        {
                            "delta_generation": int(delta_summary["generation"]),
                            "delta_segment_count": int(delta_summary["segment_count"]),
                            "membership_count": active_membership_count,
                        }
                    )
                    vector_db["ntotal"] = active_membership_count
                    for relative_path in delta_artifact_paths:
                        artifact_path = paths.data_root.joinpath(
                            *relative_path.split("/")
                        )
                        artifact_size = (
                            artifact_path.stat().st_size if artifact_path.is_file() else 0
                        )
                        if artifact_path.is_file():
                            vector_db["files"].append(
                                {"name": artifact_path.name, "size_bytes": artifact_size}
                            )
                            vector_db["file_count"] += 1
                            vector_db["total_size_bytes"] += artifact_size
            if selection.mode == "epoch_zero_compatibility":
                bundle = (
                    paths.data_root
                    / "retrieval"
                    / "compat"
                    / "v1"
                    / str(selection.compatibility_bundle_id)
                )
                compat_index = bundle / "index.faiss"
                size = compat_index.stat().st_size if compat_index.is_file() else 0
                vector_db.update(
                    {
                        "exists": bundle.is_dir(),
                        "file_count": int(compat_index.is_file()),
                        "total_size_bytes": size,
                        "files": (
                            [{"name": "compat/index.faiss", "size_bytes": size}]
                            if compat_index.is_file()
                            else []
                        ),
                        "has_faiss_index": compat_index.is_file(),
                        "has_pickle_index": False,
                        "legacy_pickle_bridge": True,
                    }
                )
        finally:
            connection.close()
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        db["error"] = message
        retrieval["error"] = message
    return db, vector_db, retrieval


def _pending_cleanup_summary(
    connection: sqlite3.Connection,
    data_root: Path,
) -> dict[str, Any]:
    has_gc_ledger = connection.execute(
        """
        SELECT 1 FROM sqlite_schema
        WHERE type = 'table' AND name = 'retrieval_delta_artifact_gc'
        """
    ).fetchone() is not None
    gc_join = (
        "LEFT JOIN retrieval_delta_artifact_gc AS artifact_gc "
        "ON artifact_gc.segment_id = segment.segment_id"
        if has_gc_ledger
        else ""
    )
    gc_filter = "AND artifact_gc.segment_id IS NULL" if has_gc_ledger else ""
    rows = connection.execute(
        f"""
        SELECT segment.relative_path, segment.state_changed_at,
               CAST(
                   MAX(
                       0.0,
                       (julianday('now') - julianday(segment.state_changed_at))
                           * 86400.0
                   ) AS INTEGER
               ) AS age_seconds
        FROM retrieval_delta_segments AS segment
        {gc_join}
        WHERE segment.relative_path IS NOT NULL
          AND (
              segment.state = 'compacted'
              OR (
                  segment.state IN ('ready', 'failed')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM retrieval_runtime AS runtime
                      WHERE runtime.runtime_id = 1
                        AND runtime.active_snapshot_id = segment.base_snapshot_id
                        AND runtime.publication_generation =
                            segment.base_publication_generation
                  )
              )
          )
          {gc_filter}
        ORDER BY segment.state_changed_at, segment.segment_id
        """
    ).fetchall()
    root = Path(data_root).resolve(strict=True)
    pending: list[tuple[str, int, int]] = []
    for row in rows:
        relative_text = str(row["relative_path"])
        relative = PurePosixPath(relative_text)
        if (
            relative.is_absolute()
            or relative.as_posix() != relative_text
            or any(part in {"", ".", ".."} for part in relative.parts)
            or (relative.parts and ":" in relative.parts[0])
        ):
            raise ValueError("pending cleanup artifact path is not canonical")
        lexical_path = root.joinpath(*relative.parts)
        try:
            file_stat = lexical_path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(file_stat.st_mode):
            raise ValueError("pending cleanup artifact path is a symbolic link")
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("pending cleanup artifact is not a regular file")
        try:
            artifact_path = lexical_path.resolve(strict=True)
        except FileNotFoundError:
            continue
        try:
            artifact_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("pending cleanup artifact escapes the data root") from exc
        pending.append(
            (
                str(row["state_changed_at"]),
                int(row["age_seconds"] or 0),
                int(file_stat.st_size),
            )
        )
    return {
        "pending_cleanup_file_count": len(pending),
        "pending_cleanup_size_bytes": sum(item[2] for item in pending),
        "oldest_pending_cleanup_at": pending[0][0] if pending else None,
        "oldest_pending_cleanup_age_seconds": (
            max(item[1] for item in pending) if pending else 0
        ),
    }


def _unavailable_native_info(
    paths,
    error: Exception,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    message = f"{type(error).__name__}: {error}"
    db = {
        "exists": paths.catalog.is_file(),
        "size_bytes": paths.catalog.stat().st_size if paths.catalog.is_file() else 0,
        "total_reports": 0,
        "embedded_reports": 0,
        "pending_reports": 0,
        "parent_chunks": 0,
        "min_report_date": None,
        "max_report_date": None,
        "report_date_counts": {},
        "report_date_type_counts": {},
        "report_types": {},
        "error": message,
    }
    vector_db = {
        "exists": False,
        "file_count": 0,
        "total_size_bytes": 0,
        "files": [],
        "has_faiss_index": False,
        "has_pickle_index": False,
        "legacy_pickle_bridge": False,
        "snapshot_id": None,
        "ntotal": 0,
    }
    retrieval = {
        "mode": "unavailable",
        "catalog_path": str(paths.catalog),
        "schema_version": None,
        "publication_generation": None,
        "write_epoch": None,
        "active_build_id": None,
        "active_snapshot_id": None,
        "predecessor_snapshot_id": None,
        "v1_fallback_open": None,
        "compatibility_bundle_id": None,
        "degraded": True,
        "write_enabled": False,
        "membership_count": 0,
        "profile_hash": None,
        "build_state": None,
        "snapshot_state": None,
        "error": message,
    }
    return db, vector_db, retrieval


def _reports_columns(conn: sqlite3.Connection) -> set[str]:
    return {row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()}


def list_unembedded_reports(db_path: str = DB_PATH, *, limit: int = 200) -> list[dict[str, Any]]:
    """Return recent reports that exist in SQLite but are not embedded yet."""
    native_paths, data_root = _status_retrieval_paths(db_path)
    try:
        selection = inspect_runtime(
            db_path,
            data_root=data_root,
            validate_snapshot=False,
            catalog_validation="read",
        )
    except RetrievalBootstrapError:
        return []
    if selection.mode != "legacy_v1":
        safe_limit = max(1, int(limit or 1))
        connection = sqlite3.connect(native_paths.catalog)
        connection.row_factory = sqlite3.Row
        try:
            rows = list(
                connection.execute(
                    """
                    SELECT report.report_id AS id, report.report_date, report.report_type,
                           report.target_name, report.title, report.broker,
                           report.canonical_relative_path,
                           CASE
                               WHEN json_extract(decision.value, '$.reason_code')
                                    = 'source-extraction-failed'
                               THEN profile.extractor
                               ELSE NULL
                           END AS embedding_extraction_engine,
                           CASE
                               WHEN json_extract(decision.value, '$.reason_code')
                                    = 'source-extraction-failed'
                               THEN 'NativeSourceExtractionError: primary and fallback extraction failed'
                               ELSE NULL
                           END AS embedding_last_error,
                           CASE
                               WHEN json_extract(decision.value, '$.reason_code')
                                    = 'source-extraction-failed'
                               THEN build.created_at
                               ELSE NULL
                           END AS embedding_last_attempt_at
                    FROM retrieval_runtime AS runtime
                    JOIN retrieval_builds AS build
                      ON build.build_id = runtime.active_build_id
                    JOIN embedding_profiles AS profile
                      ON profile.profile_id = build.profile_id
                    JOIN json_each(build.source_manifest_json, '$.reports') AS decision
                    JOIN reports AS report
                      ON report.report_uid
                         = json_extract(decision.value, '$.report_uid')
                    LEFT JOIN active_reports AS active ON active.report_uid = report.report_uid
                    WHERE runtime.runtime_id = 1
                      AND active.report_uid IS NULL
                      AND json_extract(decision.value, '$.status') = 'excluded'
                      AND json_extract(decision.value, '$.reason_code') IN (
                          'legacy_not_vectorized',
                          'source-extraction-failed'
                      )
                    ORDER BY report.report_date DESC, report.report_id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            )
            if delta_schema_installed(connection):
                delta_rows = connection.execute(
                    """
                    WITH ranked_failures AS (
                        SELECT action.action, action.report_uid, action.reason_code,
                               segment.created_at,
                               row_number() OVER (
                                   PARTITION BY action.canonical_relative_path
                                   ORDER BY segment.sequence DESC, segment.segment_id DESC
                               ) AS position
                        FROM retrieval_runtime AS runtime
                        JOIN retrieval_delta_segments AS segment
                          ON segment.base_snapshot_id = runtime.active_snapshot_id
                         AND segment.base_publication_generation = runtime.publication_generation
                         AND segment.state = 'ready'
                        JOIN retrieval_delta_reports AS action
                          ON action.segment_id = segment.segment_id
                        WHERE runtime.runtime_id = 1
                    )
                    SELECT report.report_id AS id, report.report_date, report.report_type,
                           report.target_name, report.title, report.broker,
                           report.canonical_relative_path,
                           profile.extractor AS embedding_extraction_engine,
                           CASE
                               WHEN failure.reason_code = 'source-extraction-failed'
                               THEN 'NativeSourceExtractionError: primary and fallback extraction failed'
                               ELSE 'NativeUpdateError: ' || failure.reason_code
                           END AS embedding_last_error,
                           failure.created_at AS embedding_last_attempt_at
                    FROM ranked_failures AS failure
                    JOIN reports AS report ON report.report_uid = failure.report_uid
                    JOIN retrieval_runtime AS runtime ON runtime.runtime_id = 1
                    JOIN retrieval_builds AS build
                      ON build.build_id = runtime.active_build_id
                    JOIN embedding_profiles AS profile
                      ON profile.profile_id = build.profile_id
                    LEFT JOIN active_reports AS active
                      ON active.report_uid = report.report_uid
                    WHERE failure.position = 1
                      AND failure.action = 'failed'
                      AND active.report_uid IS NULL
                    ORDER BY report.report_date DESC, report.report_id DESC
                    """
                ).fetchall()
                by_path = {
                    str(row["canonical_relative_path"]): row for row in rows
                }
                by_path.update(
                    {
                        str(row["canonical_relative_path"]): row
                        for row in delta_rows
                    }
                )
                rows = sorted(
                    by_path.values(),
                    key=lambda row: (str(row["report_date"]), int(row["id"])),
                    reverse=True,
                )[:safe_limit]
        finally:
            connection.close()
        return [
            {
                **dict(row),
                "file_name": str(row["canonical_relative_path"]).split("/")[-1],
            }
            for row in rows
        ]
    path = Path(db_path)
    if not path.exists():
        return []
    safe_limit = max(1, int(limit or 1))
    db_uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        columns = _reports_columns(conn)
        error_expr = "TRIM(embedding_last_error)" if "embedding_last_error" in columns else "NULL"
        attempted_expr = "TRIM(embedding_last_attempt_at)" if "embedding_last_attempt_at" in columns else "NULL"
        engine_expr = "TRIM(embedding_extraction_engine)" if "embedding_extraction_engine" in columns else "NULL"
        rows = conn.execute(
            f"""
            SELECT
                id,
                SUBSTR(TRIM(report_date), 1, 10) AS report_date,
                TRIM(report_type) AS report_type,
                TRIM(target_name) AS target_name,
                TRIM(title) AS title,
                TRIM(broker) AS broker,
                TRIM(file_name) AS file_name,
                {engine_expr} AS embedding_extraction_engine,
                {error_expr} AS embedding_last_error,
                {attempted_expr} AS embedding_last_attempt_at
            FROM reports
            WHERE COALESCE(is_embedded, 0) = 0
            ORDER BY SUBSTR(TRIM(report_date), 1, 10) DESC, id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _preview_text(value: Any, max_chars: int = 120) -> str:
    compact = " ".join(str(value or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1] + "…"


def build_unembedded_report_rows(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build safe table rows for the unembedded-report Monitoring tab."""
    return [
        {
            "report_date": report.get("report_date"),
            "report_type": report.get("report_type"),
            "target_name": _preview_text(report.get("target_name"), 80),
            "broker": report.get("broker"),
            "title": _preview_text(report.get("title"), 120),
            "file_name": report.get("file_name"),
            "embedding_extraction_engine": _preview_text(report.get("embedding_extraction_engine") or "-", 80),
            "embedding_last_error": _preview_text(report.get("embedding_last_error") or "-", 200),
            "embedding_last_attempt_at": report.get("embedding_last_attempt_at") or "-",
        }
        for report in reports
    ]


def get_data_status(
    *,
    save_dir: str = SAVE_DIR,
    db_path: str = DB_PATH,
    faiss_dir: str = FAISS_DIR,
    _native_only: bool = False,
) -> dict[str, Any]:
    """Return a read-only snapshot of local data, index, and config state."""
    native = _safe_native_info(db_path)
    if native is None:
        if _native_only:
            paths, _ = _status_retrieval_paths(db_path)
            db, vector_db, retrieval = _unavailable_native_info(
                paths,
                RuntimeError("Native V2 retrieval status is unavailable"),
            )
            effective_db_path = str(paths.catalog)
            effective_faiss_dir = str(paths.v2_root / "snapshots")
        else:
            db = _safe_db_info(db_path)
            vector_db = _safe_vector_info(faiss_dir)
            retrieval = {"mode": "legacy_v1"}
            effective_db_path = db_path
            effective_faiss_dir = faiss_dir
    else:
        db, vector_db, retrieval = native
        paths, _ = _status_retrieval_paths(db_path)
        effective_db_path = str(paths.catalog)
        effective_faiss_dir = str(paths.v2_root / "snapshots")
    downloaded_pdfs = _safe_count_pdfs(save_dir)

    embedding_limit_active = bool(TEST_LIMIT and TEST_LIMIT > 0)
    search_coverage_ratio = (
        db["embedded_reports"] / db["total_reports"]
        if db.get("total_reports")
        else 0.0
    )

    return {
        "paths": {
            "save_dir": save_dir,
            "db_path": effective_db_path,
            "faiss_dir": effective_faiss_dir,
        },
        "downloaded_pdfs": downloaded_pdfs,
        "db": db,
        "vector_db": vector_db,
        "retrieval": retrieval,
        "config": {
            "generation_model": GENERATION_MODEL,
            "embedding_model": EMBEDDING_MODEL,
            "test_limit": TEST_LIMIT,
            "search_top_k": SEARCH_TOP_K,
            "use_reranker": USE_RERANKER,
            "use_parent_child": USE_PARENT_CHILD,
            "extraction_engine": EXTRACTION_ENGINE,
            "unembedded_extraction_engine": UNEMBEDDED_EXTRACTION_ENGINE,
        },
        "embedding_limit_active": embedding_limit_active,
        "search_coverage_ratio": search_coverage_ratio,
    }


def get_native_v2_data_status(
    *,
    save_dir: str = SAVE_DIR,
    db_path: str = DB_PATH,
) -> dict[str, Any]:
    """Return Monitoring status without reading legacy DB/vector artifacts."""

    return get_data_status(
        save_dir=save_dir,
        db_path=db_path,
        _native_only=True,
    )


def format_bytes(size: int) -> str:
    """Format bytes using compact binary units."""
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_duration(seconds: int) -> str:
    """Format a non-negative duration for operator-facing status output."""

    value = max(int(seconds), 0)
    if value < 60:
        return f"{value}초"
    minutes = value // 60
    if minutes < 60:
        return f"{minutes}분"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간"
    days = hours // 24
    remaining_hours = hours % 24
    return f"{days}일 {remaining_hours}시간" if remaining_hours else f"{days}일"


def format_status_lines(status: dict[str, Any]) -> list[str]:
    """Format a status snapshot for terminal output."""
    db = status["db"]
    vector_db = status["vector_db"]
    config = status["config"]
    ratio = status["search_coverage_ratio"] * 100
    retrieval = status.get("retrieval") or {"mode": "legacy_v1"}

    lines = [
        "Finance LLM 데이터 상태",
        "-" * 60,
        f"다운로드 PDF: {status['downloaded_pdfs']}건",
        (
            "SQLite 리포트: "
            f"{db['total_reports']}건 "
            f"(임베딩 완료 {db['embedded_reports']}건 / 대기 {db['pending_reports']}건)"
        ),
        f"검색 커버리지: {ratio:.1f}%",
        f"리포트 기간: {db['min_report_date'] or '-'} ~ {db['max_report_date'] or '-'}",
        f"Parent Chunks: {db['parent_chunks']}건",
        (
            "FAISS 인덱스: "
            f"{'있음' if vector_db['has_faiss_index'] else '없음'} "
            f"({vector_db['file_count']}개 파일, {format_bytes(vector_db['total_size_bytes'])})"
        ),
        (
            "현재 설정: "
            f"TEST_LIMIT={config['test_limit']}, "
            f"SEARCH_TOP_K={config['search_top_k']}, "
            f"RERANKER={config['use_reranker']}, "
            f"PARENT_CHILD={config['use_parent_child']}, "
            f"EXTRACTION={config['extraction_engine']}"
        ),
        f"모델: generation={config['generation_model']}, embedding={config['embedding_model']}",
    ]

    if retrieval.get("mode") != "legacy_v1":
        lines.extend(
            [
                (
                    "Native retrieval: "
                    f"mode={retrieval.get('mode')}, "
                    f"generation={retrieval.get('publication_generation')}, "
                    f"epoch={retrieval.get('write_epoch')}"
                ),
                (
                    "Native snapshot: "
                    f"build={retrieval.get('build_state')}, "
                    f"snapshot={retrieval.get('snapshot_state')}, "
                    f"members={retrieval.get('membership_count')}/{vector_db.get('ntotal', 0)}, "
                    f"degraded={retrieval.get('degraded')}, "
                    f"write_enabled={retrieval.get('write_enabled')}"
                ),
            ]
        )
        if retrieval.get("mode") == "epoch_zero_compatibility":
            lines.append(
                "Warning: sealed V1 compatibility bundle is serving the epoch-zero bridge; "
                "native writes are blocked."
            )
        pending_cleanup_count = int(
            retrieval.get("pending_cleanup_file_count") or 0
        )
        if pending_cleanup_count:
            lines.append(
                "검색 데이터 정리 대기: "
                f"{pending_cleanup_count}개 파일, "
                f"{format_bytes(int(retrieval.get('pending_cleanup_size_bytes') or 0))}, "
                "최장 "
                f"{format_duration(int(retrieval.get('oldest_pending_cleanup_age_seconds') or 0))}"
            )

    if db.get("error"):
        lines.append(f"DB 상태 확인 오류: {db['error']}")
    if status["embedding_limit_active"] and db["pending_reports"] > 0:
        lines.append(
            "주의: TEST_LIMIT가 켜져 있어 임베딩 파이프라인 1회 실행 시 일부 문서만 처리됩니다."
        )
    if vector_db["exists"] and vector_db["has_pickle_index"]:
        lines.append(
            "주의: FAISS index.pkl은 pickle 기반입니다. 직접 생성한 신뢰 가능한 인덱스만 로드하세요."
        )

    return lines


READINESS_LABELS = {
    "ready": "검색 가능",
    "warning": "주의 필요",
    "blocked": "준비 필요",
}


def assess_readiness(status: dict[str, Any]) -> dict[str, Any]:
    """Classify whether the app is ready for non-developer search usage.

    The readiness result is intentionally action-oriented so Quick Start, CLI
    status, and the Streamlit sidebar can all tell users what to do next.
    """
    db = status["db"]
    vector_db = status["vector_db"]
    retrieval = status.get("retrieval") or {"mode": "legacy_v1"}
    messages: list[str] = []
    next_actions: list[str] = []
    level = "ready"

    def block(message: str, action: str) -> None:
        nonlocal level
        level = "blocked"
        messages.append(message)
        next_actions.append(action)

    def warn(message: str, action: str) -> None:
        nonlocal level
        if level != "blocked":
            level = "warning"
        messages.append(message)
        next_actions.append(action)

    if db.get("error"):
        block(
            f"SQLite 상태를 확인하지 못했습니다: {db['error']}",
            ".env의 DB_PATH 설정과 data/reports.db 파일을 확인하세요.",
        )
    elif not db.get("exists") or db["total_reports"] == 0:
        block(
            "검색할 리포트 메타데이터가 없습니다.",
            "RUN_QUICKSTART.bat을 다시 실행하거나 python -m src.core.report_crawler를 실행하세요.",
        )

    if not vector_db["has_faiss_index"]:
        block(
            "FAISS 검색 인덱스가 없습니다.",
            "python -m src.core.embed_pipeline --all 로 임베딩 인덱스를 생성하세요.",
        )
    elif db["embedded_reports"] == 0:
        block(
            "임베딩 완료 리포트가 없어 검색할 수 없습니다.",
            "python -m src.core.embed_pipeline --all 로 리포트를 임베딩하세요.",
        )

    if db["total_reports"] > 0 and db["pending_reports"] > 0:
        warn(
            f"아직 임베딩되지 않은 리포트가 {db['pending_reports']}건 있습니다.",
            "누락 없이 검색하려면 python -m src.core.embed_pipeline --all 을 한 번 더 실행하세요.",
        )

    if status["embedding_limit_active"] and db["pending_reports"] > 0:
        warn(
            "TEST_LIMIT가 켜져 있어 임베딩 파이프라인 1회 실행 시 일부 문서만 처리될 수 있습니다.",
            "전체 처리하려면 .env에서 TEST_LIMIT=0으로 설정하거나 --all 실행을 유지하세요.",
        )

    if vector_db["exists"] and vector_db["has_pickle_index"]:
        warn(
            "FAISS index.pkl은 pickle 기반입니다.",
            "직접 생성한 신뢰 가능한 인덱스만 로드하고 외부에서 받은 index.pkl은 사용하지 마세요.",
        )

    if retrieval.get("mode") != "legacy_v1":
        if retrieval.get("membership_count") != vector_db.get("ntotal"):
            block(
                "Native snapshot membership does not match raw FAISS ntotal.",
                "Run startup recovery; do not reopen the legacy index path.",
            )
        if retrieval.get("degraded"):
            warn(
                "Native retrieval is serving a verified predecessor in degraded read-only mode.",
                "Run a complete forward build before enabling writes.",
            )
        if retrieval.get("mode") == "epoch_zero_compatibility":
            warn(
                "The sealed epoch-zero V1 compatibility bundle is active.",
                "Repair the converted seed before attempting the first native successor.",
            )

    if not messages:
        messages.append("리포트 DB와 FAISS 인덱스가 준비되어 질문할 수 있습니다.")
        next_actions.append("Streamlit GUI에서 질문을 입력하세요.")

    return {
        "level": level,
        "label": READINESS_LABELS[level],
        "messages": messages,
        "next_actions": _dedupe_preserve_order(next_actions),
    }


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def format_readiness_lines(status: dict[str, Any]) -> list[str]:
    """Return user-facing readiness lines for terminals and Quick Start."""
    readiness = assess_readiness(status)
    lines = [
        "",
        f"Quick Start 준비 상태: {readiness['label']}",
        "-" * 60,
    ]
    lines.extend(f"- {message}" for message in readiness["messages"])
    if readiness["next_actions"]:
        lines.append("다음 행동:")
        lines.extend(f"  {index}. {action}" for index, action in enumerate(readiness["next_actions"], 1))
    return lines


def format_readiness_text(status: dict[str, Any] | None = None) -> str:
    """Return terminal-friendly readiness text."""
    return "\n".join(format_readiness_lines(status or get_data_status()))


def format_status_text(status: dict[str, Any] | None = None) -> str:
    """Return terminal-friendly multiline status text."""
    return "\n".join(format_status_lines(status or get_data_status()))
