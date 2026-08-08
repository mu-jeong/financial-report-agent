"""Import a trusted V1 corpus into the current Native V2 control plane.

The importer reuses the captured FAISS vectors and reconstructed V1 chunks.
It never parses a PDF or calls an embedding provider.  V1-specific objects are
translated at this boundary and are not exposed to the normal runtime.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from src.migrations.v2.assess import ProvenanceEvidence
from src.migrations.v2.legacy_import import reconstruct_v1_documents
from src.retrieval.build_service import (
    CandidateChunk,
    CandidateParent,
    CandidateReport,
    CandidateResult,
    NativeBuildPlan,
    materialize_candidate,
    prepare_imported_legacy_build,
    publish_candidate,
)
from src.retrieval.identity import (
    EmbeddingProfile,
    assign_physical_ids,
    canonical_json,
    compute_chunk_uid,
    compute_parent_uid,
    compute_report_uid,
    normalize_relative_path,
    sha256_text,
)
from src.retrieval.manifest import CorpusManifest, ExclusionPolicy, ManifestDecision
from src.retrieval.publication import PublicationOutcome
from src.retrieval.vector_index import load_index, read_faiss_index_file
from src.retrieval.writer_lock import (
    WriterLease,
    WriterLockError,
    ensure_native_runtime_directory,
)


class V1ImportError(RuntimeError):
    """Raised when legacy data cannot be imported without changing meaning."""


@dataclass(frozen=True)
class V1ImportPlan:
    profile: EmbeddingProfile
    reports: tuple[CandidateReport, ...]
    parents: tuple[CandidateParent, ...]
    chunks: tuple[CandidateChunk, ...]
    manifest: CorpusManifest
    vectors_by_physical_id: np.ndarray
    assessment_digest: str
    reconstruction_digest: str
    vector_payload_sha256: str


@dataclass(frozen=True)
class V1ImportResult:
    candidate: CandidateResult
    publication: PublicationOutcome
    vector_count: int
    max_vector_absolute_error: float
    cleanup_marker_relative_path: str


def plan_v1_import(
    legacy_root: str | Path,
    *,
    expected_hashes: dict[str, str],
    profile: EmbeddingProfile,
    source_hashes: dict[str, str],
    canonical_relative_paths: dict[str, str] | None = None,
    provenance: ProvenanceEvidence | None = None,
) -> V1ImportPlan:
    """Translate every V1 report, parent, chunk, and vector without writes."""

    if not isinstance(profile, EmbeddingProfile):
        raise V1ImportError("V1 import requires a validated embedding profile")
    root = Path(legacy_root).resolve(strict=True)
    reconstruction = reconstruct_v1_documents(
        root,
        expected_hashes=expected_hashes,
        prefix_template=profile.prefix_template,
        child_policy=profile.child_policy,
        provenance=provenance,
    )
    report_rows = _read_report_rows(root / "reports.db")
    report_names = {row["file_name"] for row in report_rows}
    normalized_hashes = _validate_source_hashes(source_hashes, report_names)
    relative_paths = _canonical_paths(report_names, canonical_relative_paths)

    provisional_reports: list[dict[str, Any]] = []
    for row in report_rows:
        file_name = row["file_name"]
        metadata = {
            "broker": row["broker"],
            "report_date": row["report_date"],
            "report_type": row["report_type"],
            "target_name": row["target_name"],
            "title": row["title"],
        }
        metadata_sha256 = sha256_text(canonical_json(metadata))
        report_uid = compute_report_uid(
            relative_paths[file_name],
            normalized_hashes[file_name],
            metadata_sha256,
        )
        provisional_reports.append(
            {
                **row,
                "canonical_relative_path": relative_paths[file_name],
                "source_sha256": normalized_hashes[file_name],
                "retrieval_metadata_sha256": metadata_sha256,
                "report_uid": report_uid,
            }
        )
    provisional_reports.sort(key=lambda row: bytes.fromhex(row["report_uid"]))
    reports = tuple(
        CandidateReport(
            report_uid=row["report_uid"],
            canonical_relative_path=row["canonical_relative_path"],
            source_sha256=row["source_sha256"],
            retrieval_metadata_sha256=row["retrieval_metadata_sha256"],
            report_type=row["report_type"],
            report_date=row["report_date"],
            target_name=row["target_name"],
            title=row["title"],
            broker=row["broker"],
            file_name=row["file_name"],
            existing_report_id=None,
        )
        for row in provisional_reports
    )
    reports_by_name = {report.file_name: report for report in reports}
    included_names = {parent.file_name for parent in reconstruction.parents}
    manifest = CorpusManifest.build(
        [report.report_uid for report in reports],
        [
            (
                ManifestDecision.included(report.report_uid)
                if report.file_name in included_names
                else ManifestDecision.excluded(report.report_uid, "legacy-not-vectorized")
            )
            for report in reports
        ],
        ExclusionPolicy(
            version="legacy-v1-import-v1",
            excluded_reason_codes=frozenset({"legacy-not-vectorized"}),
        ),
    )

    parents: list[CandidateParent] = []
    provisional_chunks: list[dict[str, Any]] = []
    parents_by_report: dict[str, list[Any]] = defaultdict(list)
    for parent in reconstruction.parents:
        parents_by_report[parent.file_name].append(parent)
    for file_name, legacy_parents in parents_by_report.items():
        legacy_parents.sort(
            key=lambda item: (
                item.canonical_order_key,
                item.vector_payload_sha256,
                item.legacy_parent_id,
            )
        )
        report = reports_by_name[file_name]
        for parent_order, legacy_parent in enumerate(legacy_parents):
            parent_uid = compute_parent_uid(
                profile.profile_hash,
                report.report_uid,
                parent_order,
                legacy_parent.content_sha256,
            )
            parents.append(
                CandidateParent(
                    parent_uid=parent_uid,
                    report_uid=report.report_uid,
                    profile_id=profile.profile_hash,
                    parent_order=parent_order,
                    content=legacy_parent.content,
                    content_sha256=legacy_parent.content_sha256,
                )
            )
            for legacy_child in legacy_parent.children:
                chunk_uid = compute_chunk_uid(
                    profile.profile_hash,
                    parent_uid,
                    legacy_child.child_order,
                    legacy_child.span.span_start,
                    legacy_child.span.span_end,
                    legacy_child.span.embedding_text_sha256,
                )
                provisional_chunks.append(
                    {
                        "chunk_uid": chunk_uid,
                        "parent_uid": parent_uid,
                        "child_order": legacy_child.child_order,
                        "span_start": legacy_child.span.span_start,
                        "span_end": legacy_child.span.span_end,
                        "embedding_text_sha256": legacy_child.span.embedding_text_sha256,
                        "legacy_ordinal": legacy_child.legacy_ordinal,
                    }
                )

    physical_ids = assign_physical_ids(item["chunk_uid"] for item in provisional_chunks)
    chunks = tuple(
        CandidateChunk(
            chunk_uid=item["chunk_uid"],
            parent_uid=item["parent_uid"],
            profile_id=profile.profile_hash,
            child_order=item["child_order"],
            span_start=item["span_start"],
            span_end=item["span_end"],
            embedding_text_sha256=item["embedding_text_sha256"],
            physical_id=physical_ids[item["chunk_uid"]],
        )
        for item in provisional_chunks
    )
    if len(chunks) != reconstruction.assessment.observable.ntotal:
        raise V1ImportError("native chunk count differs from the V1 FAISS count")

    report_uid_by_parent = {parent.parent_uid: parent.report_uid for parent in parents}
    report_chunk_counts: dict[str, int] = defaultdict(int)
    for chunk in chunks:
        report_chunk_counts[report_uid_by_parent[chunk.parent_uid]] += 1
    manifest.validate_snapshot_membership(report_chunk_counts)

    legacy_ordinal_by_uid = {
        item["chunk_uid"]: int(item["legacy_ordinal"])
        for item in provisional_chunks
    }
    legacy_index = read_faiss_index_file(root / "vector_db" / "index.faiss")
    ordered_chunks = sorted(chunks, key=lambda item: item.physical_id)
    vectors = np.vstack(
        [
            np.asarray(
                legacy_index.reconstruct(legacy_ordinal_by_uid[chunk.chunk_uid]),
                dtype=np.float32,
            )
            for chunk in ordered_chunks
        ]
    )
    vectors = np.ascontiguousarray(vectors, dtype=np.float32)
    if vectors.shape != (len(chunks), profile.dimension):
        raise V1ImportError("V1 vector payload shape differs from its embedding profile")
    if not np.isfinite(vectors).all():
        raise V1ImportError("V1 vector payload contains a non-finite value")
    vectors.setflags(write=False)
    vector_payload_sha256 = hashlib.sha256(
        vectors.astype("<f4", copy=False).tobytes(order="C")
    ).hexdigest()
    return V1ImportPlan(
        profile=profile,
        reports=reports,
        parents=tuple(parents),
        chunks=chunks,
        manifest=manifest,
        vectors_by_physical_id=vectors,
        assessment_digest=reconstruction.assessment.digest,
        reconstruction_digest=reconstruction.reconstruction_digest,
        vector_payload_sha256=vector_payload_sha256,
    )


def execute_v1_import(
    legacy_root: str | Path,
    data_root: str | Path,
    source_directory: str | Path,
    *,
    expected_hashes: dict[str, str],
    profile: EmbeddingProfile,
    source_hashes: dict[str, str],
    writer_lease: WriterLease,
    provenance: ProvenanceEvidence | None = None,
) -> V1ImportResult:
    """Materialize and publish a first Native V2 snapshot from V1 vectors."""

    plan = plan_v1_import(
        legacy_root,
        expected_hashes=expected_hashes,
        profile=profile,
        source_hashes=source_hashes,
        provenance=provenance,
    )
    native_plan: NativeBuildPlan = prepare_imported_legacy_build(
        data_root,
        source_directory,
        profile=plan.profile,
        reports=plan.reports,
        parents=plan.parents,
        chunks=plan.chunks,
        manifest=plan.manifest,
        vectors_by_physical_id=plan.vectors_by_physical_id,
        writer_lease=writer_lease,
    )
    candidate = materialize_candidate(
        native_plan,
        data_root,
        writer_lease=writer_lease,
    )
    snapshot = load_index(
        Path(data_root).resolve(strict=True) / candidate.snapshot_relative_path,
        candidate.descriptor,
    )
    imported_vectors = snapshot.reconstruct(range(1, len(plan.chunks) + 1))
    maximum_error = float(
        np.max(np.abs(imported_vectors - plan.vectors_by_physical_id))
    )
    if maximum_error > 1e-6:
        raise V1ImportError("published candidate changed a legacy vector value")
    cleanup_marker_relative_path = _write_cleanup_marker(
        Path(data_root).resolve(strict=True),
        plan,
        candidate,
        expected_hashes=expected_hashes,
        source_hashes=source_hashes,
    )
    publication = publish_candidate(
        candidate,
        data_root,
        writer_lease=writer_lease,
    )
    return V1ImportResult(
        candidate=candidate,
        publication=publication,
        vector_count=len(plan.chunks),
        max_vector_absolute_error=maximum_error,
        cleanup_marker_relative_path=cleanup_marker_relative_path,
    )


def _write_cleanup_marker(
    data_root: Path,
    plan: V1ImportPlan,
    candidate: CandidateResult,
    *,
    expected_hashes: dict[str, str],
    source_hashes: dict[str, str],
) -> str:
    """Persist the narrow authorization needed for post-publication V1 deletion."""

    relative = "retrieval/v2/migration/v1-cleanup.json"
    marker_root = _ensure_plain_marker_directory(data_root)
    path = marker_root / "v1-cleanup.json"
    payload = {
        "schema_version": 1,
        "kind": "native-v2-v1-cleanup",
        "snapshot_id": candidate.snapshot_id,
        "build_id": candidate.build_id,
        "publication_id": candidate.publication_id,
        "assessment_digest": plan.assessment_digest,
        "reconstruction_digest": plan.reconstruction_digest,
        "vector_payload_sha256": plan.vector_payload_sha256,
        "vector_count": len(plan.chunks),
        "v1_artifact_sha256": {
            key: expected_hashes[key] for key in sorted(expected_hashes)
        },
        "source_pdf_sha256": {
            key: source_hashes[key] for key in sorted(source_hashes)
        },
    }
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    if _path_entry_exists(path):
        if _is_redirected(path) or not path.is_file() or path.read_bytes() != encoded:
            raise V1ImportError("V1 cleanup marker conflicts with this import")
        return relative
    try:
        with path.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(marker_root)
    except OSError as exc:
        raise V1ImportError(f"V1 cleanup marker cannot be persisted: {exc}") from exc
    return relative


def _ensure_plain_marker_directory(data_root: Path) -> Path:
    """Create the migration marker directory without following redirects."""

    try:
        v2_root = ensure_native_runtime_directory(data_root)
    except WriterLockError as exc:
        raise V1ImportError(str(exc)) from exc
    marker_root = v2_root / "migration"
    if _is_redirected(marker_root):
        raise V1ImportError("V1 cleanup marker directory cannot be redirected")
    if _path_entry_exists(marker_root):
        if not marker_root.is_dir():
            raise V1ImportError("V1 cleanup marker path must be a directory")
    else:
        try:
            marker_root.mkdir()
        except OSError as exc:
            raise V1ImportError("V1 cleanup marker directory cannot be created") from exc
    if _is_redirected(marker_root):
        raise V1ImportError("V1 cleanup marker directory cannot be redirected")
    try:
        resolved = marker_root.resolve(strict=True)
        resolved.relative_to(v2_root)
    except (OSError, ValueError) as exc:
        raise V1ImportError("V1 cleanup marker directory escapes DATA_ROOT") from exc
    if resolved != marker_root:
        raise V1ImportError("V1 cleanup marker directory cannot be redirected")
    return marker_root


def _path_entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise V1ImportError(f"V1 cleanup path cannot be inspected: {exc}") from exc
    return True


def _is_redirected(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise V1ImportError(f"V1 cleanup path cannot be inspected: {exc}") from exc
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_report_rows(path: Path) -> list[dict[str, Any]]:
    resolved = path.resolve(strict=True)
    uri = f"file:{resolved.as_posix()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT file_name, report_type, report_date, target_name, title, broker
                FROM reports ORDER BY file_name
                """
            ).fetchall()
    except sqlite3.Error as exc:
        raise V1ImportError(f"V1 report catalog cannot be read: {exc}") from exc
    if not rows:
        raise V1ImportError("V1 report catalog is empty")
    return [dict(row) for row in rows]


def _validate_source_hashes(
    values: dict[str, str],
    expected_names: set[str],
) -> dict[str, str]:
    if not isinstance(values, dict) or set(values) != expected_names:
        raise V1ImportError("source hashes must cover every V1 report exactly")
    normalized: dict[str, str] = {}
    for name, digest in values.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            raise V1ImportError(f"invalid source hash for V1 report: {name}")
        normalized[name] = digest.lower()
    return normalized


def _canonical_paths(
    names: set[str],
    supplied: dict[str, str] | None,
) -> dict[str, str]:
    values = supplied or {name: f"downloaded/{name}" for name in names}
    if set(values) != names:
        raise V1ImportError("canonical source paths must cover every V1 report")
    return {name: normalize_relative_path(values[name]) for name in names}


__all__ = [
    "V1ImportError",
    "V1ImportPlan",
    "V1ImportResult",
    "execute_v1_import",
    "plan_v1_import",
]
