"""Immutable, self-contained Native V2 snapshots for reproduction cases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import numpy as np

from src.retrieval.delta_schema import delta_schema_installed
from src.retrieval.schema import SCHEMA_VERSION, install_schema
from src.retrieval.vector_index import (
    RawVectorIndex,
    SnapshotDescriptor,
    VectorIndexError,
    build_index,
    load_index,
)


MANIFEST_SCHEMA_VERSION = 2
DEFAULT_READER_CONTRACT = f"finance-llm-native-v2-schema-{SCHEMA_VERSION}"
CATALOG_FILENAME = "projected_catalog.sqlite3"
INDEX_FILENAME = "subset.faiss"
MANIFEST_FILENAME = "manifest.json"
_REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
_FIXED_TIMESTAMP = "2000-01-01T00:00:00Z"


class FixedSnapshotError(RuntimeError):
    """Raised when a fixed snapshot cannot be created or trusted."""


class IncompatibleFixedSnapshotError(FixedSnapshotError):
    """Raised when snapshot bytes require a different reader contract."""


class FixedSnapshotAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    LOCAL_MISSING = "LOCAL_MISSING"
    CORRUPT = "CORRUPT"
    INCOMPATIBLE = "INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class FixedSnapshot:
    revision_id: str
    path: Path
    manifest: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OpenedFixedSnapshot(FixedSnapshot):
    catalog_path: Path
    index_path: Path
    report_uids: tuple[str, ...]
    chunk_uids: tuple[str, ...]
    index: RawVectorIndex


@dataclass(frozen=True, slots=True)
class ActiveReportDocument:
    report_uid: str
    canonical_relative_path: str
    report_type: str
    report_date: str
    target_name: str | None
    title: str
    broker: str

    @property
    def file_name(self) -> str:
        return PurePosixPath(self.canonical_relative_path).name


@dataclass(frozen=True, slots=True)
class SnapshotScopeProposal:
    report_uids: tuple[str, ...]
    observed_report_uids: tuple[str, ...]
    filter_matched_report_uids: tuple[str, ...]
    unsupported_filters: tuple[str, ...]


def resolve_active_snapshot_sources(
    data_root: str | Path,
) -> tuple[Path, Path]:
    """Resolve the active Native V2 catalog/index without path fallback."""

    root = Path(data_root).expanduser().resolve(strict=True)
    catalog = root / "retrieval" / "v2" / "catalog.sqlite3"
    if catalog.is_symlink() or not catalog.is_file():
        raise FixedSnapshotError("active Native V2 catalog is unavailable")
    with _readonly_connection(catalog) as connection:
        row = connection.execute(
            """
            SELECT snapshot.relative_path
              FROM retrieval_runtime AS runtime
              JOIN vector_snapshots AS snapshot
                ON snapshot.snapshot_id = runtime.active_snapshot_id
               AND snapshot.build_id = runtime.active_build_id
             WHERE runtime.runtime_id = 1
               AND snapshot.state = 'ready'
            """
        ).fetchone()
    if row is None:
        raise FixedSnapshotError("active Native V2 snapshot is unavailable")
    relative_value = str(row[0])
    relative = PurePosixPath(relative_value)
    if (
        not relative_value
        or "\\" in relative_value
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise FixedSnapshotError("active snapshot path is unsafe")
    snapshot = root.joinpath(*relative.parts).resolve(strict=True)
    try:
        snapshot.relative_to(root)
    except ValueError as exc:
        raise FixedSnapshotError("active snapshot path escaped DATA_ROOT") from exc
    if snapshot.is_symlink() or not snapshot.is_file():
        raise FixedSnapshotError("active Native V2 snapshot is unavailable")
    return catalog.resolve(strict=True), snapshot


def list_active_report_documents(
    source_catalog: str | Path,
) -> tuple[ActiveReportDocument, ...]:
    """List active report metadata for local operator scope selection."""

    catalog = Path(source_catalog).resolve(strict=True)
    with _readonly_connection(catalog) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        if delta_schema_installed(connection):
            rows = connection.execute(
                """
                SELECT report_uid, canonical_relative_path, report_type,
                       report_date, target_name, title, broker
                  FROM active_reports
                 ORDER BY report_date DESC, target_name, broker, title,
                          report_uid
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT DISTINCT report.report_uid,
                       report.canonical_relative_path, report.report_type,
                       report.report_date, report.target_name, report.title,
                       report.broker
                  FROM retrieval_runtime AS runtime
                  JOIN snapshot_membership AS member
                    ON member.snapshot_id = runtime.active_snapshot_id
                  JOIN retrieval_chunks AS chunk
                    ON chunk.chunk_uid = member.chunk_uid
                  JOIN retrieval_parents AS parent
                    ON parent.parent_uid = chunk.parent_uid
                  JOIN reports AS report ON report.report_id = parent.report_id
                 WHERE runtime.runtime_id = 1
                 ORDER BY report.report_date DESC, report.target_name,
                          report.broker, report.title, report.report_uid
                """
            ).fetchall()
    return tuple(
        ActiveReportDocument(
            report_uid=str(row["report_uid"]),
            canonical_relative_path=str(row["canonical_relative_path"]),
            report_type=str(row["report_type"]),
            report_date=str(row["report_date"]),
            target_name=(
                str(row["target_name"])
                if row["target_name"] is not None
                else None
            ),
            title=str(row["title"]),
            broker=str(row["broker"]),
        )
        for row in rows
    )


def propose_report_scope(
    source_catalog: str | Path,
    *,
    observed_report_uids: Iterable[str],
    filters: Mapping[str, Any] | None = None,
) -> SnapshotScopeProposal:
    """Propose all active reports matching safe report-level filters.

    Observed reports are always retained. Unknown filters are reported rather
    than silently interpreted or used to broaden SQL.
    """

    return propose_report_scope_from_documents(
        list_active_report_documents(source_catalog),
        observed_report_uids=observed_report_uids,
        filters=filters,
    )


def propose_report_scope_from_documents(
    active_documents: Iterable[ActiveReportDocument],
    *,
    observed_report_uids: Iterable[str],
    filters: Mapping[str, Any] | None = None,
) -> SnapshotScopeProposal:
    """Propose a scope from one metadata-only active-document snapshot.

    This has the same exact-match behavior as :func:`propose_report_scope`,
    while allowing callers to reuse an already loaded active publication for
    proposal, human review, and search.
    """

    observed = _normalize_identity_selection(
        observed_report_uids, "observed_report_uids"
    )
    supported = {
        "broker",
        "brokers",
        "file_name",
        "file_names",
        "report_date",
        "report_date_end",
        "report_date_start",
        "report_type",
        "report_types",
        "target_name",
        "target_names",
    }
    filter_body = dict(filters or {})
    unsupported = tuple(sorted(set(filter_body) - supported))
    active = {
        document.report_uid: document for document in tuple(active_documents)
    }
    missing_observed = sorted(set(observed) - set(active))
    if missing_observed:
        raise FixedSnapshotError(
            "observed report UID is not present in the active snapshot"
        )

    def values(singular: str, plural: str) -> set[str] | None:
        raw = filter_body.get(plural)
        if raw is None:
            raw = filter_body.get(singular)
        if raw is None or raw == "":
            return None
        sequence = raw if isinstance(raw, (list, tuple)) else [raw]
        return {str(item) for item in sequence if str(item)}

    targets = values("target_name", "target_names")
    report_types = values("report_type", "report_types")
    brokers = values("broker", "brokers")
    file_names = values("file_name", "file_names")
    exact_date = str(filter_body.get("report_date") or "")
    start_date = str(filter_body.get("report_date_start") or "")
    end_date = str(filter_body.get("report_date_end") or "")

    def matches(document: ActiveReportDocument) -> bool:
        date_value = document.report_date
        return not any(
            (
                targets is not None
                and str(document.target_name or "") not in targets,
                report_types is not None
                and document.report_type not in report_types,
                brokers is not None and document.broker not in brokers,
                file_names is not None
                and document.file_name not in file_names,
                bool(exact_date) and date_value != exact_date,
                bool(start_date) and date_value < start_date,
                bool(end_date) and date_value > end_date,
            )
        )

    has_supported_constraint = any(
        value not in (None, "", [], ())
        for key, value in filter_body.items()
        if key in supported
    )
    matched = tuple(
        sorted(
            report_uid
            for report_uid, document in active.items()
            if has_supported_constraint and matches(document)
        )
    )
    scope = tuple(sorted(set(observed) | set(matched)))
    if not scope:
        raise FixedSnapshotError(
            "Snapshot scope requires an observed report or supported filter match"
        )
    return SnapshotScopeProposal(
        report_uids=scope,
        observed_report_uids=observed,
        filter_matched_report_uids=matched,
        unsupported_filters=unsupported,
    )


def create_fixed_snapshot(
    source_catalog: str | Path,
    source_snapshot: str | Path,
    managed_root: str | Path,
    *,
    report_uids: Iterable[str] | None = None,
    chunk_uids: Iterable[str] | None = None,
    reader_contract: str = DEFAULT_READER_CONTRACT,
) -> FixedSnapshot:
    """Project selected active-snapshot reports and publish one READY revision.

    A chunk selection is intentionally widened to the complete active-snapshot
    scope of every report containing a selected chunk. This keeps case inputs
    understandable while still allowing an operator to start from one hit.
    """

    reports = _normalize_identity_selection(report_uids, "report_uids")
    chunks = _normalize_identity_selection(chunk_uids, "chunk_uids")
    if bool(reports) == bool(chunks):
        raise FixedSnapshotError(
            "exactly one non-empty selection of report_uids or chunk_uids is required"
        )
    if not isinstance(reader_contract, str) or not reader_contract.strip():
        raise FixedSnapshotError("reader contract must be a non-empty string")

    catalog_path = Path(source_catalog).resolve(strict=True)
    snapshot_path = Path(source_snapshot).resolve(strict=True)
    root = _prepare_managed_root(managed_root)
    temporary = Path(tempfile.mkdtemp(prefix=".temp-", dir=root))
    try:
        selection = _read_projection_selection(
            catalog_path,
            snapshot_path,
            report_uids=reports,
            chunk_uids=chunks,
        )
        vectors = _reconstruct_selection_vectors(selection)
        subset_descriptor = build_index(
            vectors,
            range(1, len(selection["chunks"]) + 1),
            selection["profile"]["metric"],
        ).write(temporary / INDEX_FILENAME)

        projected_ids = _projected_ids(selection)
        _write_projected_catalog(
            temporary / CATALOG_FILENAME,
            selection,
            subset_descriptor,
            projected_ids,
        )
        manifest = _build_manifest(
            temporary,
            selection,
            subset_descriptor,
            projected_ids,
            reader_contract,
        )
        _write_json(temporary / MANIFEST_FILENAME, manifest)
        opened = _open_artifact_path(
            temporary,
            expected_reader_contract=reader_contract,
        )
        final_path = _managed_revision_path(root, opened.revision_id)
        if final_path.exists():
            existing = _open_artifact_path(
                final_path,
                expected_reader_contract=reader_contract,
            )
            return FixedSnapshot(existing.revision_id, existing.path, existing.manifest)
        try:
            temporary.rename(final_path)
        except OSError as exc:
            if final_path.exists():
                existing = _open_artifact_path(
                    final_path,
                    expected_reader_contract=reader_contract,
                )
                return FixedSnapshot(
                    existing.revision_id, existing.path, existing.manifest
                )
            raise FixedSnapshotError(f"atomic READY publication failed: {exc}") from exc
        temporary = final_path
        published = _open_artifact_path(
            final_path,
            expected_reader_contract=reader_contract,
        )
        return FixedSnapshot(published.revision_id, published.path, published.manifest)
    except (FixedSnapshotError, FileNotFoundError):
        raise
    except (OSError, sqlite3.Error, VectorIndexError, ValueError) as exc:
        raise FixedSnapshotError(f"fixed snapshot creation failed: {exc}") from exc
    finally:
        if temporary.exists() and temporary.name.startswith(".temp-"):
            _remove_managed_temporary(root, temporary)


def open_fixed_snapshot(
    managed_root: str | Path,
    revision_id: str,
    *,
    reader_contract: str = DEFAULT_READER_CONTRACT,
) -> OpenedFixedSnapshot:
    root = _prepare_managed_root(managed_root)
    opened = _open_artifact_path(
        _managed_revision_path(root, revision_id),
        expected_reader_contract=reader_contract,
    )
    if opened.revision_id != revision_id:
        raise FixedSnapshotError(
            "fixed snapshot revision directory does not match its manifest identity"
        )
    return opened


def derive_fixed_snapshot_availability(
    managed_root: str | Path,
    revision_id: str,
    *,
    reader_contract: str = DEFAULT_READER_CONTRACT,
) -> FixedSnapshotAvailability:
    try:
        open_fixed_snapshot(
            managed_root,
            revision_id,
            reader_contract=reader_contract,
        )
    except (FileNotFoundError, NotADirectoryError):
        return FixedSnapshotAvailability.LOCAL_MISSING
    except IncompatibleFixedSnapshotError:
        return FixedSnapshotAvailability.INCOMPATIBLE
    except (FixedSnapshotError, OSError, sqlite3.Error, VectorIndexError, ValueError):
        return FixedSnapshotAvailability.CORRUPT
    return FixedSnapshotAvailability.AVAILABLE


def _read_projection_selection(
    catalog_path: Path,
    snapshot_path: Path,
    *,
    report_uids: tuple[str, ...],
    chunk_uids: tuple[str, ...],
) -> dict[str, Any]:
    with _readonly_connection(catalog_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        runtime = connection.execute(
            """
            SELECT active_snapshot_id, active_build_id, publication_generation,
                   schema_version
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
        if runtime is None or runtime["active_snapshot_id"] is None:
            raise FixedSnapshotError("source catalog has no active Native V2 snapshot")
        snapshot = connection.execute(
            """
            SELECT snapshot.*, build.profile_id
            FROM vector_snapshots AS snapshot
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            WHERE snapshot.snapshot_id = ? AND snapshot.build_id = ?
              AND snapshot.state = 'ready'
              AND build.state IN ('committed_pending_checkpoint', 'fully_complete')
            """,
            (runtime["active_snapshot_id"], runtime["active_build_id"]),
        ).fetchone()
        if snapshot is None:
            raise FixedSnapshotError("source runtime does not point to a READY snapshot")
        descriptor = SnapshotDescriptor(
            sha256=snapshot["file_sha256"],
            size_bytes=snapshot["size_bytes"],
            dimension=snapshot["dimension"],
            metric=snapshot["metric"],
            ntotal=snapshot["ntotal"],
        )
        load_index(snapshot_path, descriptor)

        has_delta_schema = delta_schema_installed(connection)
        active_report_source = (
            "active_reports" if has_delta_schema else "reports"
        )
        if has_delta_schema:
            membership_source = "active_vector_membership"
            membership_artifact_columns = """
                member.artifact_id AS source_artifact_id,
                member.artifact_kind AS source_artifact_kind,
                member.sequence AS source_artifact_sequence,
            """
            membership_runtime_join = ""
            membership_runtime_where = ""
            membership_runtime_params: tuple[Any, ...] = ()
        else:
            membership_source = "snapshot_membership"
            membership_artifact_columns = """
                member.snapshot_id AS source_artifact_id,
                'base' AS source_artifact_kind,
                0 AS source_artifact_sequence,
            """
            membership_runtime_join = ""
            membership_runtime_where = "member.snapshot_id = ? AND"
            membership_runtime_params = (snapshot["snapshot_id"],)

        if chunk_uids:
            placeholders = ",".join("?" for _ in chunk_uids)
            selected_rows = connection.execute(
                f"""
                SELECT DISTINCT report.report_uid
                FROM {membership_source} AS member
                JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = member.chunk_uid
                JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
                JOIN reports AS report ON report.report_id = parent.report_id
                {membership_runtime_join}
                WHERE {membership_runtime_where}
                      member.chunk_uid IN ({placeholders})
                """,
                (*membership_runtime_params, *chunk_uids),
            ).fetchall()
            found_chunks = connection.execute(
                f"""SELECT count(*) FROM {membership_source} AS member
                     WHERE {membership_runtime_where}
                           member.chunk_uid IN ({placeholders})""",
                (*membership_runtime_params, *chunk_uids),
            ).fetchone()[0]
            if found_chunks != len(chunk_uids):
                raise FixedSnapshotError(
                    "selected chunk UID is not present in the effective active snapshot"
                )
            selected_reports = tuple(sorted(row["report_uid"] for row in selected_rows))
        else:
            placeholders = ",".join("?" for _ in report_uids)
            found = connection.execute(
                f"SELECT report_uid FROM {active_report_source} "
                f"WHERE report_uid IN ({placeholders})",
                report_uids,
            ).fetchall()
            selected_reports = tuple(sorted(row["report_uid"] for row in found))
            if len(selected_reports) != len(report_uids):
                raise FixedSnapshotError(
                    "selected report UID is not present in the effective active snapshot"
                )

        placeholders = ",".join("?" for _ in selected_reports)
        report_rows = connection.execute(
            f"SELECT * FROM reports WHERE report_uid IN ({placeholders}) ORDER BY report_uid",
            selected_reports,
        ).fetchall()
        chunk_rows = connection.execute(
            f"""
            SELECT chunk.*, {membership_artifact_columns}
                   member.faiss_id AS source_faiss_id,
                   report.report_uid
            FROM {membership_source} AS member
            JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = member.chunk_uid
            JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
            JOIN reports AS report ON report.report_id = parent.report_id
            {membership_runtime_join}
            WHERE {membership_runtime_where}
                  report.report_uid IN ({placeholders})
            ORDER BY chunk.chunk_uid
            """,
            (*membership_runtime_params, *selected_reports),
        ).fetchall()
        if not chunk_rows:
            raise FixedSnapshotError(
                "selected reports have no effective active snapshot membership"
            )
        reports_with_membership = {row["report_uid"] for row in chunk_rows}
        if set(selected_reports) != reports_with_membership:
            raise FixedSnapshotError(
                "every selected report must have active snapshot membership"
            )
        parent_uids = tuple(sorted({row["parent_uid"] for row in chunk_rows}))
        parent_placeholders = ",".join("?" for _ in parent_uids)
        parent_rows = connection.execute(
            "SELECT * FROM retrieval_parents "
            f"WHERE parent_uid IN ({parent_placeholders}) ORDER BY parent_uid",
            parent_uids,
        ).fetchall()
        profile = connection.execute(
            "SELECT * FROM embedding_profiles WHERE profile_id = ?",
            (snapshot["profile_id"],),
        ).fetchone()
        if profile is None:
            raise FixedSnapshotError("active snapshot embedding profile is missing")
        source_artifacts = _read_source_artifacts(
            connection,
            runtime=dict(runtime),
            base_snapshot=dict(snapshot),
            base_descriptor=descriptor,
            base_snapshot_path=snapshot_path,
            artifact_ids={str(row["source_artifact_id"]) for row in chunk_rows},
            has_delta_schema=has_delta_schema,
        )
        return {
            "runtime": dict(runtime),
            "source_snapshot": dict(snapshot),
            "source_descriptor": descriptor,
            "profile": dict(profile),
            "reports": [dict(row) for row in report_rows],
            "parents": [dict(row) for row in parent_rows],
            "chunks": [dict(row) for row in chunk_rows],
            "selected_report_uids": selected_reports,
            "source_artifacts": source_artifacts,
        }


def _read_source_artifacts(
    connection: sqlite3.Connection,
    *,
    runtime: Mapping[str, Any],
    base_snapshot: Mapping[str, Any],
    base_descriptor: SnapshotDescriptor,
    base_snapshot_path: Path,
    artifact_ids: set[str],
    has_delta_schema: bool,
) -> list[dict[str, Any]]:
    base_id = str(base_snapshot["snapshot_id"])
    if not artifact_ids:
        raise FixedSnapshotError("snapshot projection has no source artifacts")
    unknown = set(artifact_ids) - {base_id}
    artifacts: list[dict[str, Any]] = []
    if base_id in artifact_ids:
        artifacts.append(
            {
                "artifact_id": base_id,
                "artifact_kind": "base",
                "sequence": 0,
                "path": base_snapshot_path,
                "descriptor": base_descriptor,
            }
        )
    if unknown and not has_delta_schema:
        raise FixedSnapshotError("active membership references an unknown artifact")
    if unknown:
        placeholders = ",".join("?" for _ in unknown)
        rows = connection.execute(
            f"""
            SELECT segment_id, sequence, relative_path, file_sha256,
                   size_bytes, dimension, metric, ntotal
              FROM retrieval_delta_segments
             WHERE base_snapshot_id = ?
               AND base_publication_generation = ?
               AND state = 'ready'
               AND segment_id IN ({placeholders})
             ORDER BY sequence, segment_id
            """,
            (
                base_id,
                int(runtime["publication_generation"]),
                *sorted(unknown),
            ),
        ).fetchall()
        if {str(row["segment_id"]) for row in rows} != unknown:
            raise FixedSnapshotError(
                "active membership references a missing READY delta artifact"
            )
        data_root = _source_data_root(
            base_snapshot_path,
            str(base_snapshot["relative_path"]),
        )
        for row in rows:
            descriptor = SnapshotDescriptor(
                sha256=str(row["file_sha256"] or ""),
                size_bytes=int(row["size_bytes"]),
                dimension=int(row["dimension"]),
                metric=str(row["metric"]),
                ntotal=int(row["ntotal"]),
            )
            if (
                descriptor.dimension != base_descriptor.dimension
                or descriptor.metric != base_descriptor.metric
            ):
                raise FixedSnapshotError(
                    "delta artifact is incompatible with the active vector profile"
                )
            relative_path = row["relative_path"]
            if not isinstance(relative_path, str) or not relative_path:
                raise FixedSnapshotError("non-empty delta artifact has no safe path")
            artifact_path = _resolve_source_artifact(data_root, relative_path)
            load_index(artifact_path, descriptor)
            artifacts.append(
                {
                    "artifact_id": str(row["segment_id"]),
                    "artifact_kind": "delta",
                    "sequence": int(row["sequence"]),
                    "path": artifact_path,
                    "descriptor": descriptor,
                }
            )
    return artifacts


def _reconstruct_selection_vectors(selection: Mapping[str, Any]) -> np.ndarray:
    vectors_by_source: dict[tuple[str, int], np.ndarray] = {}
    chunks_by_artifact: dict[str, list[Mapping[str, Any]]] = {}
    for chunk in selection["chunks"]:
        chunks_by_artifact.setdefault(str(chunk["source_artifact_id"]), []).append(
            chunk
        )
    artifacts = {
        str(artifact["artifact_id"]): artifact
        for artifact in selection["source_artifacts"]
    }
    if set(chunks_by_artifact) != set(artifacts):
        raise FixedSnapshotError("source artifact projection is inconsistent")
    for artifact_id, chunks in chunks_by_artifact.items():
        artifact = artifacts[artifact_id]
        index = load_index(artifact["path"], artifact["descriptor"])
        source_ids = [int(chunk["source_faiss_id"]) for chunk in chunks]
        reconstructed = index.reconstruct(source_ids)
        for position, source_id in enumerate(source_ids):
            vectors_by_source[(artifact_id, source_id)] = reconstructed[position]
    ordered = [
        vectors_by_source[
            (str(chunk["source_artifact_id"]), int(chunk["source_faiss_id"]))
        ]
        for chunk in selection["chunks"]
    ]
    if not ordered:
        raise FixedSnapshotError("snapshot projection has no vectors")
    return np.asarray(ordered, dtype=np.float32)


def _source_data_root(snapshot_path: Path, relative_value: str) -> Path:
    relative = _safe_relative_path(relative_value, "active snapshot")
    candidate = snapshot_path.resolve(strict=True)
    for _part in relative.parts:
        candidate = candidate.parent
    resolved = candidate.resolve(strict=True)
    expected = resolved.joinpath(*relative.parts).resolve(strict=True)
    if expected != snapshot_path.resolve(strict=True):
        raise FixedSnapshotError(
            "active snapshot path does not match its catalog relative path"
        )
    return resolved


def _resolve_source_artifact(data_root: Path, relative_value: str) -> Path:
    relative = _safe_relative_path(relative_value, "delta artifact")
    path = data_root.joinpath(*relative.parts).resolve(strict=True)
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise FixedSnapshotError("delta artifact path escaped DATA_ROOT") from exc
    if path.is_symlink() or not path.is_file():
        raise FixedSnapshotError("delta artifact is unavailable")
    return path


def _safe_relative_path(value: str, label: str) -> PurePosixPath:
    relative = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise FixedSnapshotError(f"{label} path is unsafe")
    return relative


def _projected_ids(selection: Mapping[str, Any]) -> dict[str, str]:
    source_artifacts = _source_artifact_identities(selection)
    identity = {
        "source_snapshot_id": selection["source_snapshot"]["snapshot_id"],
        "source_snapshot_sha256": selection["source_snapshot"]["file_sha256"],
        "source_publication_generation": selection["runtime"][
            "publication_generation"
        ],
        "source_artifacts": source_artifacts,
        "report_uids": selection["selected_report_uids"],
        "chunk_uids": [row["chunk_uid"] for row in selection["chunks"]],
    }
    digest = _sha256_bytes(_canonical_json(identity))
    return {
        "build_id": f"fixed-build-{digest}",
        "snapshot_id": f"fixed-snapshot-{digest}",
    }


def _source_artifact_identities(
    selection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "artifact_id": str(artifact["artifact_id"]),
            "artifact_kind": str(artifact["artifact_kind"]),
            "sequence": int(artifact["sequence"]),
            "sha256": artifact["descriptor"].sha256,
            "size_bytes": artifact["descriptor"].size_bytes,
            "dimension": artifact["descriptor"].dimension,
            "metric": artifact["descriptor"].metric,
            "ntotal": artifact["descriptor"].ntotal,
        }
        for artifact in sorted(
            selection["source_artifacts"],
            key=lambda value: (
                int(value["sequence"]),
                str(value["artifact_id"]),
            ),
        )
    ]


def _write_projected_catalog(
    catalog_path: Path,
    selection: Mapping[str, Any],
    descriptor: SnapshotDescriptor,
    projected_ids: Mapping[str, str],
) -> None:
    connection = sqlite3.connect(catalog_path)
    try:
        install_schema(connection)
        connection.execute("DROP TRIGGER retrieval_runtime_monotonic_update")
        connection.execute(
            "UPDATE retrieval_runtime SET created_at=?, updated_at=? WHERE runtime_id=1",
            (_FIXED_TIMESTAMP, _FIXED_TIMESTAMP),
        )
        connection.commit()
        install_schema(connection)
        _insert_row(connection, "embedding_profiles", selection["profile"])
        report_id_by_uid: dict[str, int] = {}
        for report_id, source in enumerate(selection["reports"], 1):
            row = dict(source)
            row["report_id"] = report_id
            _insert_row(connection, "reports", row)
            report_id_by_uid[row["report_uid"]] = report_id
        source_report_uid_by_id = {
            row["report_id"]: row["report_uid"] for row in selection["reports"]
        }
        for source in selection["parents"]:
            row = dict(source)
            row["report_id"] = report_id_by_uid[
                source_report_uid_by_id[source["report_id"]]
            ]
            _insert_row(connection, "retrieval_parents", row)
        for source in selection["chunks"]:
            row = {key: value for key, value in source.items() if key in {
                "chunk_uid", "parent_uid", "profile_id", "child_order",
                "span_start", "span_end", "embedding_text_sha256", "created_at"
            }}
            _insert_row(connection, "retrieval_chunks", row)

        source_manifest = _canonical_json(
            {
                "kind": "fixed_snapshot_projection",
                "source_snapshot_id": selection["source_snapshot"]["snapshot_id"],
                "source_publication_generation": selection["runtime"][
                    "publication_generation"
                ],
                "source_artifacts": _source_artifact_identities(selection),
                "report_uids": selection["selected_report_uids"],
            }
        )
        connection.execute(
            """
            INSERT INTO retrieval_builds (
                build_id, profile_id, source_manifest_json,
                source_manifest_sha256, included_count, excluded_count,
                expected_count, exclusion_policy_version, created_at,
                state_changed_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, 'fixed-snapshot-v1', ?, ?)
            """,
            (
                projected_ids["build_id"],
                selection["profile"]["profile_id"],
                source_manifest.decode("utf-8"),
                _sha256_bytes(source_manifest),
                len(selection["chunks"]),
                len(selection["chunks"]),
                _FIXED_TIMESTAMP,
                _FIXED_TIMESTAMP,
            ),
        )
        connection.execute(
            """
            INSERT INTO vector_snapshots (
                snapshot_id, build_id, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal, created_at, state_changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                projected_ids["snapshot_id"],
                projected_ids["build_id"],
                INDEX_FILENAME,
                descriptor.sha256,
                descriptor.size_bytes,
                descriptor.dimension,
                descriptor.metric,
                descriptor.ntotal,
                _FIXED_TIMESTAMP,
                _FIXED_TIMESTAMP,
            ),
        )
        for faiss_id, chunk in enumerate(selection["chunks"], 1):
            connection.execute(
                "INSERT INTO snapshot_membership VALUES (?, ?, ?)",
                (projected_ids["snapshot_id"], chunk["chunk_uid"], faiss_id),
            )
        for state in ("cataloging", "vector_building", "validating"):
            connection.execute(
                "UPDATE retrieval_builds SET state=? WHERE build_id=?",
                (state, projected_ids["build_id"]),
            )
        for state in ("validating", "ready"):
            connection.execute(
                "UPDATE vector_snapshots SET state=? WHERE snapshot_id=?",
                (state, projected_ids["snapshot_id"]),
            )
        for state in ("ready", "committed_pending_checkpoint", "fully_complete"):
            connection.execute(
                "UPDATE retrieval_builds SET state=? WHERE build_id=?",
                (state, projected_ids["build_id"]),
            )
        connection.execute(
            """
            UPDATE retrieval_runtime
            SET active_snapshot_id=?, active_build_id=?,
                publication_generation=1, write_epoch=0, degraded=0,
                write_enabled=0, updated_at=?
            WHERE runtime_id=1
            """,
            (
                projected_ids["snapshot_id"],
                projected_ids["build_id"],
                _FIXED_TIMESTAMP,
            ),
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise FixedSnapshotError("projected catalog failed foreign-key validation")
        connection.commit()
    finally:
        connection.close()


def _build_manifest(
    artifact_path: Path,
    selection: Mapping[str, Any],
    descriptor: SnapshotDescriptor,
    projected_ids: Mapping[str, str],
    reader_contract: str,
) -> dict[str, Any]:
    chunk_mapping = [
        {"faiss_id": faiss_id, "chunk_uid": row["chunk_uid"]}
        for faiss_id, row in enumerate(selection["chunks"], 1)
    ]
    files = {
        CATALOG_FILENAME: _file_identity(artifact_path / CATALOG_FILENAME),
        INDEX_FILENAME: _file_identity(artifact_path / INDEX_FILENAME),
    }
    revision_material = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "reader_contract": reader_contract,
        "files": files,
        "source_snapshot_id": selection["source_snapshot"]["snapshot_id"],
        "source_snapshot_sha256": selection["source_snapshot"]["file_sha256"],
        "source_publication_generation": selection["runtime"][
            "publication_generation"
        ],
        "source_artifacts": _source_artifact_identities(selection),
        "report_uids": list(selection["selected_report_uids"]),
        "chunk_mapping_sha256": _sha256_bytes(_canonical_json(chunk_mapping)),
        "projected_build_id": projected_ids["build_id"],
        "projected_snapshot_id": projected_ids["snapshot_id"],
        "vector": {
            "sha256": descriptor.sha256,
            "size_bytes": descriptor.size_bytes,
            "dimension": descriptor.dimension,
            "metric": descriptor.metric,
            "ntotal": descriptor.ntotal,
        },
    }
    revision_id = _sha256_bytes(_canonical_json(revision_material))
    return {
        **revision_material,
        "revision_id": revision_id,
        "lifecycle": "READY",
        "chunk_mapping": chunk_mapping,
        "counts": {
            "reports": len(selection["reports"]),
            "parents": len(selection["parents"]),
            "chunks": len(selection["chunks"]),
        },
    }


def _open_artifact_path(
    artifact_path: Path,
    *,
    expected_reader_contract: str,
) -> OpenedFixedSnapshot:
    if artifact_path.is_symlink():
        raise FixedSnapshotError("fixed snapshot directory must not be a symbolic link")
    path = artifact_path.resolve(strict=True)
    if not path.is_dir():
        raise NotADirectoryError(path)
    manifest_path = path / MANIFEST_FILENAME
    catalog_path = path / CATALOG_FILENAME
    index_path = path / INDEX_FILENAME
    for file_path in (manifest_path, catalog_path, index_path):
        if file_path.is_symlink():
            raise FixedSnapshotError(
                f"fixed snapshot file must not be a symbolic link: {file_path.name}"
            )
    manifest = _read_manifest(manifest_path)
    if manifest.get("reader_contract") != expected_reader_contract:
        raise IncompatibleFixedSnapshotError(
            "fixed snapshot reader contract is incompatible"
        )
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise IncompatibleFixedSnapshotError("fixed snapshot manifest schema is incompatible")
    if manifest.get("lifecycle") != "READY":
        raise FixedSnapshotError("fixed snapshot is not READY")
    revision_id = manifest.get("revision_id")
    _validate_revision_id(revision_id)
    for filename, file_path in (
        (CATALOG_FILENAME, catalog_path),
        (INDEX_FILENAME, index_path),
    ):
        expected = manifest.get("files", {}).get(filename)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        if expected != _file_identity(file_path):
            raise FixedSnapshotError(f"{filename} bytes do not match manifest")
    revision_material = {
        key: manifest[key]
        for key in (
            "manifest_schema_version",
            "reader_contract",
            "files",
            "source_snapshot_id",
            "source_snapshot_sha256",
            "source_publication_generation",
            "source_artifacts",
            "report_uids",
            "chunk_mapping_sha256",
            "projected_build_id",
            "projected_snapshot_id",
            "vector",
        )
    }
    if _sha256_bytes(_canonical_json(revision_material)) != revision_id:
        raise FixedSnapshotError("fixed snapshot revision identity is invalid")

    vector = manifest["vector"]
    descriptor = SnapshotDescriptor(
        sha256=vector["sha256"],
        size_bytes=vector["size_bytes"],
        dimension=vector["dimension"],
        metric=vector["metric"],
        ntotal=vector["ntotal"],
    )
    index = load_index(index_path, descriptor)
    with _readonly_connection(catalog_path) as connection:
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise FixedSnapshotError("projected catalog integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise FixedSnapshotError("projected catalog foreign-key check failed")
        runtime = connection.execute(
            "SELECT * FROM retrieval_runtime WHERE runtime_id=1"
        ).fetchone()
        if (
            runtime is None
            or runtime["active_snapshot_id"] != manifest["projected_snapshot_id"]
            or runtime["active_build_id"] != manifest["projected_build_id"]
        ):
            raise FixedSnapshotError("projected runtime identity is invalid")
        snapshot = connection.execute(
            "SELECT * FROM vector_snapshots WHERE snapshot_id=?",
            (manifest["projected_snapshot_id"],),
        ).fetchone()
        if snapshot is None or snapshot["state"] != "ready":
            raise FixedSnapshotError("projected vector snapshot is not READY")
        if (
            snapshot["relative_path"] != INDEX_FILENAME
            or snapshot["file_sha256"] != descriptor.sha256
            or snapshot["size_bytes"] != descriptor.size_bytes
            or snapshot["dimension"] != descriptor.dimension
            or snapshot["metric"] != descriptor.metric
            or snapshot["ntotal"] != descriptor.ntotal
        ):
            raise FixedSnapshotError("projected vector descriptor is inconsistent")
        membership = connection.execute(
            """
            SELECT faiss_id, chunk_uid FROM snapshot_membership
            WHERE snapshot_id=? ORDER BY faiss_id
            """,
            (manifest["projected_snapshot_id"],),
        ).fetchall()
        mapping = [dict(row) for row in membership]
        if mapping != manifest["chunk_mapping"]:
            raise FixedSnapshotError("FAISS ID to chunk mapping is inconsistent")
        if _sha256_bytes(_canonical_json(mapping)) != manifest["chunk_mapping_sha256"]:
            raise FixedSnapshotError("chunk mapping digest is invalid")
        if tuple(row["faiss_id"] for row in membership) != index.physical_ids:
            raise FixedSnapshotError("FAISS IDs do not match projected membership")
        report_uids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT report_uid FROM reports ORDER BY report_uid"
            ).fetchall()
        )
        if report_uids != tuple(manifest["report_uids"]):
            raise FixedSnapshotError("projected report scope is inconsistent")
        counts = manifest["counts"]
        actual_counts = {
            "reports": connection.execute("SELECT count(*) FROM reports").fetchone()[0],
            "parents": connection.execute(
                "SELECT count(*) FROM retrieval_parents"
            ).fetchone()[0],
            "chunks": connection.execute(
                "SELECT count(*) FROM retrieval_chunks"
            ).fetchone()[0],
        }
        if actual_counts != counts or actual_counts["chunks"] != index.ntotal:
            raise FixedSnapshotError("projected row counts are inconsistent")
    return OpenedFixedSnapshot(
        revision_id=revision_id,
        path=path,
        manifest=manifest,
        catalog_path=catalog_path,
        index_path=index_path,
        report_uids=report_uids,
        chunk_uids=tuple(row["chunk_uid"] for row in membership),
        index=index,
    )


def _insert_row(connection: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    columns = tuple(row)
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
        tuple(row[column] for column in columns),
    )


def _normalize_identity_selection(
    values: Iterable[str] | None,
    label: str,
) -> tuple[str, ...]:
    if values is None:
        return ()
    normalized = tuple(values)
    if any(not isinstance(value, str) or not _REVISION_RE.fullmatch(value) for value in normalized):
        raise FixedSnapshotError(f"{label} must contain lowercase SHA-256 identities")
    if len(normalized) != len(set(normalized)):
        raise FixedSnapshotError(f"{label} must not contain duplicates")
    return tuple(sorted(normalized))


def _prepare_managed_root(managed_root: str | Path) -> Path:
    root = Path(managed_root)
    if root.is_symlink():
        raise FixedSnapshotError("managed root must not be a symbolic link")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise FixedSnapshotError("managed root must not be a symbolic link")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise FixedSnapshotError("managed root must be a directory")
    return resolved


def _managed_revision_path(root: Path, revision_id: str) -> Path:
    _validate_revision_id(revision_id)
    target = root / revision_id
    if target.parent.resolve() != root:
        raise FixedSnapshotError("revision path escapes managed root")
    return target


def _validate_revision_id(revision_id: object) -> None:
    if not isinstance(revision_id, str) or not _REVISION_RE.fullmatch(revision_id):
        raise FixedSnapshotError("revision ID must be a lowercase SHA-256 digest")


def _remove_managed_temporary(root: Path, temporary: Path) -> None:
    resolved = temporary.resolve()
    if resolved.parent != root or not resolved.name.startswith(".temp-"):
        raise FixedSnapshotError("temporary cleanup target escapes managed root")
    shutil.rmtree(resolved)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixedSnapshotError("fixed snapshot manifest is invalid JSON") from exc
    if not isinstance(value, dict):
        raise FixedSnapshotError("fixed snapshot manifest must be an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    data = _canonical_json(value) + b"\n"
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _file_identity(path: Path) -> dict[str, Any]:
    return {"sha256": _sha256_file(path), "size_bytes": path.stat().st_size}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
