"""Deterministic full-N conversion of trusted V1 artifacts into a native seed."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from src.migrations.v2.assess import ProvenanceEvidence
from src.migrations.v2.evidence import validate_compatibility_bundle
from src.migrations.v2.legacy_import import (
    LegacyImportError,
    LegacyReconstruction,
    reconstruct_v1_documents,
)
from src.retrieval.identity import (
    EmbeddingProfile,
    assign_physical_ids,
    canonical_hash,
    canonical_json,
    compute_chunk_uid,
    compute_parent_uid,
    compute_report_uid,
    normalize_relative_path,
    sha256_text,
)
from src.retrieval.manifest import (
    CorpusManifest,
    ExclusionPolicy,
    ManifestDecision,
)
from src.retrieval.schema import (
    RETRIEVAL_TABLES,
    SCHEMA_VERSION,
    SchemaError,
    checkpoint_isolated_catalog,
    install_schema,
    require_main_file_only,
)
from src.retrieval.vector_index import (
    SnapshotDescriptor,
    build_index,
    load_index,
    read_faiss_index_file,
)
from src.retrieval.writer_lock import (
    NativeWriterLock,
    WriterLease,
    assert_writer_lease_owned,
)


class ConversionError(RuntimeError):
    """Raised before a V1 conversion can expose an active native seed."""


@dataclass(frozen=True)
class NativeReport:
    report_id: int
    report_uid: str
    canonical_relative_path: str
    source_sha256: str
    retrieval_metadata_sha256: str
    report_type: str
    report_date: str
    target_name: str | None
    title: str
    broker: str
    file_name: str


@dataclass(frozen=True)
class NativeParent:
    parent_uid: str
    report_id: int
    profile_id: str
    parent_order: int
    content: str
    content_sha256: str
    legacy_parent_id: str


@dataclass(frozen=True)
class NativeChunk:
    chunk_uid: str
    parent_uid: str
    profile_id: str
    child_order: int
    span_start: int
    span_end: int
    embedding_text_sha256: str
    legacy_ordinal: int
    legacy_document_id: str
    physical_id: int


@dataclass(frozen=True)
class NativeSeedPlan:
    reconstruction: LegacyReconstruction
    profile: EmbeddingProfile
    reports: tuple[NativeReport, ...]
    parents: tuple[NativeParent, ...]
    chunks: tuple[NativeChunk, ...]
    manifest: CorpusManifest
    build_id: str
    snapshot_id: str
    publication_id: str
    compatibility_bundle_id: str


@dataclass(frozen=True)
class ConversionResult:
    catalog_relative_path: str
    snapshot_relative_path: str
    evidence_manifest_relative_path: str
    build_id: str
    snapshot_id: str
    publication_id: str
    profile_hash: str
    report_count: int
    parent_count: int
    chunk_count: int
    snapshot_sha256: str
    snapshot_size_bytes: int
    max_vector_absolute_error: float


def plan_v1_seed(
    copied_install_root: str | Path,
    *,
    expected_hashes: dict[str, str],
    profile: EmbeddingProfile,
    source_hashes: dict[str, str],
    compatibility_bundle_id: str,
    canonical_relative_paths: dict[str, str] | None = None,
    provenance: ProvenanceEvidence | None = None,
) -> NativeSeedPlan:
    """Build the complete native identity/membership plan without writing files."""

    if not isinstance(profile, EmbeddingProfile):
        raise ConversionError("conversion requires a validated EmbeddingProfile")
    reconstruction = reconstruct_v1_documents(
        copied_install_root,
        expected_hashes=expected_hashes,
        prefix_template=profile.prefix_template,
        child_policy=profile.child_policy,
        provenance=provenance,
    )
    report_rows = _read_report_rows(Path(copied_install_root) / "reports.db")
    report_names = {row["file_name"] for row in report_rows}
    normalized_source_hashes = _validate_source_hashes(source_hashes, report_names)
    relative_paths = _canonical_paths(report_names, canonical_relative_paths)

    provisional_reports: list[dict[str, Any]] = []
    for row in report_rows:
        file_name = row["file_name"]
        retrieval_metadata = {
            "broker": row["broker"],
            "report_date": row["report_date"],
            "report_type": row["report_type"],
            "target_name": row["target_name"],
            "title": row["title"],
        }
        metadata_hash = sha256_text(canonical_json(retrieval_metadata))
        report_uid = compute_report_uid(
            relative_paths[file_name],
            normalized_source_hashes[file_name],
            metadata_hash,
        )
        provisional_reports.append(
            {
                **row,
                "canonical_relative_path": relative_paths[file_name],
                "source_sha256": normalized_source_hashes[file_name],
                "retrieval_metadata_sha256": metadata_hash,
                "report_uid": report_uid,
            }
        )
    provisional_reports.sort(key=lambda row: bytes.fromhex(row["report_uid"]))
    reports = tuple(
        NativeReport(
            report_id=index,
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
        )
        for index, row in enumerate(provisional_reports, start=1)
    )
    reports_by_name = {report.file_name: report for report in reports}
    included_names = {parent.file_name for parent in reconstruction.parents}
    policy = ExclusionPolicy(
        version="legacy-v1-import-v1",
        excluded_reason_codes=frozenset({"legacy_not_vectorized"}),
    )
    decisions = [
        (
            ManifestDecision.included(report.report_uid)
            if report.file_name in included_names
            else ManifestDecision.excluded(report.report_uid, "legacy_not_vectorized")
        )
        for report in reports
    ]
    manifest = CorpusManifest.build(
        [report.report_uid for report in reports],
        decisions,
        policy,
    )

    profile_id = profile.profile_hash
    native_parents: list[NativeParent] = []
    provisional_chunks: list[dict[str, Any]] = []
    parents_by_report: dict[str, list[Any]] = defaultdict(list)
    for parent in reconstruction.parents:
        parents_by_report[parent.file_name].append(parent)
    for file_name, legacy_parents in parents_by_report.items():
        legacy_parents.sort(
            key=lambda parent: (
                parent.canonical_order_key,
                parent.vector_payload_sha256,
                parent.legacy_parent_id,
            )
        )
        report = reports_by_name[file_name]
        for parent_order, legacy_parent in enumerate(legacy_parents):
            parent_uid = compute_parent_uid(
                profile_id,
                report.report_uid,
                parent_order,
                legacy_parent.content_sha256,
            )
            native_parents.append(
                NativeParent(
                    parent_uid=parent_uid,
                    report_id=report.report_id,
                    profile_id=profile_id,
                    parent_order=parent_order,
                    content=legacy_parent.content,
                    content_sha256=legacy_parent.content_sha256,
                    legacy_parent_id=legacy_parent.legacy_parent_id,
                )
            )
            for legacy_child in legacy_parent.children:
                chunk_uid = compute_chunk_uid(
                    profile_id,
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
                        "legacy_document_id": legacy_child.legacy_document_id,
                    }
                )

    physical_ids = assign_physical_ids(item["chunk_uid"] for item in provisional_chunks)
    chunks = tuple(
        NativeChunk(
            **item,
            profile_id=profile_id,
            physical_id=physical_ids[item["chunk_uid"]],
        )
        for item in provisional_chunks
    )
    if len(chunks) != reconstruction.assessment.observable.ntotal:
        raise ConversionError("native chunk count does not equal captured legacy N")
    report_chunk_counts: dict[str, int] = defaultdict(int)
    parent_report_uid = {
        parent.parent_uid: next(
            report.report_uid for report in reports if report.report_id == parent.report_id
        )
        for parent in native_parents
    }
    for chunk in chunks:
        report_chunk_counts[parent_report_uid[chunk.parent_uid]] += 1
    manifest.validate_snapshot_membership(report_chunk_counts)

    build_id = canonical_hash(
        "retrieval-build",
        profile_id,
        manifest.sha256,
        reconstruction.reconstruction_digest,
    )
    membership_hash = sha256_text(
        canonical_json(
            [
                {"chunk_uid": chunk.chunk_uid, "faiss_id": chunk.physical_id}
                for chunk in sorted(chunks, key=lambda value: value.physical_id)
            ]
        )
    )
    snapshot_id = canonical_hash(
        "vector-snapshot",
        build_id,
        membership_hash,
        reconstruction.assessment.observable.vector_payload_sha256,
    )
    publication_id = canonical_hash(
        "seed-publication",
        snapshot_id,
        compatibility_bundle_id,
    )
    return NativeSeedPlan(
        reconstruction=reconstruction,
        profile=profile,
        reports=reports,
        parents=tuple(native_parents),
        chunks=chunks,
        manifest=manifest,
        build_id=build_id,
        snapshot_id=snapshot_id,
        publication_id=publication_id,
        compatibility_bundle_id=compatibility_bundle_id,
    )


def convert_v1_seed(
    copied_install_root: str | Path,
    data_root: str | Path,
    *,
    expected_hashes: dict[str, str],
    profile: EmbeddingProfile,
    source_hashes: dict[str, str],
    compatibility_bundle_id: str,
    canonical_relative_paths: dict[str, str] | None = None,
    provenance: ProvenanceEvidence | None = None,
    writer_lease: WriterLease | None = None,
) -> ConversionResult:
    """Publish an internally serveable epoch-zero seed on an empty V2 root."""

    root = Path(data_root).resolve(strict=True)
    if writer_lease is None:
        with NativeWriterLock(root) as owned_lease:
            return convert_v1_seed(
                copied_install_root,
                root,
                expected_hashes=expected_hashes,
                profile=profile,
                source_hashes=source_hashes,
                compatibility_bundle_id=compatibility_bundle_id,
                canonical_relative_paths=canonical_relative_paths,
                provenance=provenance,
                writer_lease=owned_lease,
            )
    assert_writer_lease_owned(writer_lease, root)
    source_root = Path(copied_install_root).resolve(strict=True)
    validate_compatibility_bundle(root, compatibility_bundle_id)
    plan = plan_v1_seed(
        source_root,
        expected_hashes=expected_hashes,
        profile=profile,
        source_hashes=source_hashes,
        compatibility_bundle_id=compatibility_bundle_id,
        canonical_relative_paths=canonical_relative_paths,
        provenance=provenance,
    )

    v2_root = root / "retrieval" / "v2"
    catalog_path = v2_root / "catalog.sqlite3"
    if catalog_path.exists():
        raise ConversionError("native V2 catalog already exists")
    snapshots = v2_root / "snapshots"
    staging_root = v2_root / "staging" / plan.publication_id
    evidence_root = v2_root / "evidence" / plan.publication_id
    backups = v2_root / "backups"
    for directory in (snapshots, staging_root, evidence_root, backups):
        directory.mkdir(parents=True, exist_ok=True)

    snapshot_relative = f"retrieval/v2/snapshots/{plan.snapshot_id}.faiss"
    snapshot_path = root / snapshot_relative
    if snapshot_path.exists():
        raise ConversionError("native seed snapshot already exists without a catalog")
    legacy_index = read_faiss_index_file(source_root / "vector_db" / "index.faiss")
    chunks_by_physical = sorted(plan.chunks, key=lambda chunk: chunk.physical_id)
    vectors = np.vstack(
        [legacy_index.reconstruct(chunk.legacy_ordinal) for chunk in chunks_by_physical]
    ).astype(np.float32, copy=False)
    raw = build_index(
        vectors,
        [chunk.physical_id for chunk in chunks_by_physical],
        metric=plan.profile.metric,
    )
    descriptor = raw.write(snapshot_path)
    max_error = _validate_vector_parity(raw, legacy_index, chunks_by_physical)

    mapping_payload = {
        "schema_version": 1,
        "assessment_digest": plan.reconstruction.assessment.digest,
        "reconstruction_digest": plan.reconstruction.reconstruction_digest,
        "rows": [
            {
                "chunk_uid": chunk.chunk_uid,
                "faiss_id": chunk.physical_id,
                "legacy_document_id": chunk.legacy_document_id,
                "legacy_ordinal": chunk.legacy_ordinal,
                "parent_uid": chunk.parent_uid,
            }
            for chunk in sorted(plan.chunks, key=lambda value: value.legacy_ordinal)
        ],
    }
    mapping_path = evidence_root / "legacy-mapping.json"
    _write_json_once(mapping_path, mapping_payload)
    mapping_hash = _sha256_file(mapping_path)
    evidence_payload: dict[str, Any] = {
        "schema_version": 1,
        "publication_id": plan.publication_id,
        "build_id": plan.build_id,
        "snapshot_id": plan.snapshot_id,
        "profile_hash": plan.profile.profile_hash,
        "compatibility_bundle_id": plan.compatibility_bundle_id,
        "assessment_digest": plan.reconstruction.assessment.digest,
        "reconstruction_digest": plan.reconstruction.reconstruction_digest,
        "source_manifest_sha256": plan.manifest.sha256,
        "snapshot": {
            "relative_path": snapshot_relative,
            "sha256": descriptor.sha256,
            "size_bytes": descriptor.size_bytes,
            "dimension": descriptor.dimension,
            "metric": descriptor.metric,
            "ntotal": descriptor.ntotal,
        },
        "counts": {
            "reports": len(plan.reports),
            "parents": len(plan.parents),
            "chunks": len(plan.chunks),
        },
        "legacy_mapping": {
            "relative_path": (
                f"retrieval/v2/evidence/{plan.publication_id}/legacy-mapping.json"
            ),
            "sha256": mapping_hash,
        },
        "vector_max_absolute_error": max_error,
        "prohibited_conversion_calls": {
            "api": 0,
            "chunking": 0,
            "crawler": 0,
            "embedding": 0,
            "extraction": 0,
            "network": 0,
            "pdf_reads": 0,
        },
    }
    if plan.reconstruction.replay_claims:
        replay_policy = plan.reconstruction.replay_policy
        if replay_policy is None:
            raise ConversionError("replay evidence is missing its frozen policy")
        evidence_payload["schema_version"] = 2
        evidence_payload["span_reconstruction"] = {
            "claims": [asdict(claim) for claim in plan.reconstruction.replay_claims],
            "method": "ordered-span-v1-with-ambiguity-replay",
            "operations": {
                "embedding": 0,
                "legacy_replay_chunking": len(plan.reconstruction.replay_claims),
                "network": 0,
                "pdf_reads": 0,
                "source_pdf_chunking": 0,
            },
            "policy": replay_policy.canonical_payload,
            "policy_sha256": replay_policy.policy_sha256,
            "replayed_parent_count": len(plan.reconstruction.replay_claims),
            "resolver_version": 2,
        }
    evidence_path = evidence_root / "manifest.json"
    _write_json_once(evidence_path, evidence_payload)
    _validate_conversion_evidence_files(
        evidence_path,
        mapping_path,
        expected_identity={
            "assessment_digest": plan.reconstruction.assessment.digest,
            "build_id": plan.build_id,
            "compatibility_bundle_id": plan.compatibility_bundle_id,
            "profile_hash": plan.profile.profile_hash,
            "publication_id": plan.publication_id,
            "reconstruction_digest": plan.reconstruction.reconstruction_digest,
            "snapshot_id": plan.snapshot_id,
        },
        expected_counts=(len(plan.reports), len(plan.parents), len(plan.chunks)),
    )
    evidence_hash = _sha256_file(evidence_path)
    evidence_relative = f"retrieval/v2/evidence/{plan.publication_id}/manifest.json"

    catalog_staging = staging_root / "catalog.sqlite3"
    connection = sqlite3.connect(catalog_staging)
    try:
        install_schema(connection)
        _populate_seed_catalog(
            connection,
            plan,
            descriptor,
            snapshot_relative,
            evidence_relative,
            evidence_hash,
        )
        connection.commit()
        _write_json_once(
            evidence_root / "commit-intent.json",
            {
                "schema_version": 1,
                "publication_id": plan.publication_id,
                "target_publication_generation": 1,
                "old_write_epoch": 0,
                "new_write_epoch": 0,
                "v1_fallback_floor": "open",
                "snapshot_id": plan.snapshot_id,
                "snapshot_sha256": descriptor.sha256,
                "catalog_state": "candidate",
            },
        )
        _set_publication_phase(connection, plan.publication_id, "commit_intent_durable")
        with connection:
            _transition_build(connection, plan.build_id, "committed_pending_checkpoint")
            connection.execute(
                """
                UPDATE retrieval_runtime
                SET active_snapshot_id = ?, active_build_id = ?,
                    predecessor_snapshot_id = NULL,
                    publication_generation = 1, write_epoch = 0,
                    v1_fallback_open = 1, degraded = 0, write_enabled = 0,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE runtime_id = 1
                """,
                (plan.snapshot_id, plan.build_id),
            )
            _set_publication_phase(
                connection,
                plan.publication_id,
                "committed_pending_checkpoint",
                commit=False,
            )

        checkpoint_path = backups / "catalog-current.sqlite3"
        _write_catalog_checkpoint(connection, checkpoint_path)
        checkpoint_hash = _sha256_file(checkpoint_path)
        _set_publication_phase(connection, plan.publication_id, "checkpoint_validated")
        floor_payload = {
            "schema_version": 1,
            "publication_id": plan.publication_id,
            "publication_generation": 1,
            "write_epoch": 0,
            "v1_fallback_floor": "open",
            "active_snapshot_id": plan.snapshot_id,
            "checkpoint_relative_path": "retrieval/v2/backups/catalog-current.sqlite3",
            "checkpoint_sha256": checkpoint_hash,
        }
        _write_json_once(evidence_root / "committed-floor.json", floor_payload)
        _set_publication_phase(connection, plan.publication_id, "committed_floor_durable")
        with connection:
            _transition_build(connection, plan.build_id, "fully_complete")
            connection.execute(
                """
                UPDATE publication_runs
                SET phase = 'fully_complete', state = 'fully_complete',
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE publication_id = ?
                """,
                (plan.publication_id,),
            )
        _validate_catalog(connection, plan, descriptor)
        try:
            checkpoint_isolated_catalog(connection)
        except SchemaError as exc:
            raise ConversionError(
                'native seed WAL checkpoint did not complete'
            ) from exc
    finally:
        connection.close()
    try:
        require_main_file_only(catalog_staging)
    except SchemaError as exc:
        raise ConversionError(
            'native seed catalog is not main-file-only'
        ) from exc
    _fsync_file(catalog_staging)
    _publish_without_overwrite(catalog_staging, catalog_path)
    _make_evidence_read_only(evidence_root)
    result = ConversionResult(
        catalog_relative_path="retrieval/v2/catalog.sqlite3",
        snapshot_relative_path=snapshot_relative,
        evidence_manifest_relative_path=evidence_relative,
        build_id=plan.build_id,
        snapshot_id=plan.snapshot_id,
        publication_id=plan.publication_id,
        profile_hash=plan.profile.profile_hash,
        report_count=len(plan.reports),
        parent_count=len(plan.parents),
        chunk_count=len(plan.chunks),
        snapshot_sha256=descriptor.sha256,
        snapshot_size_bytes=descriptor.size_bytes,
        max_vector_absolute_error=max_error,
    )
    validate_converted_seed(root, result)
    return result


def validate_converted_seed(data_root: str | Path, result: ConversionResult) -> None:
    root = Path(data_root).resolve(strict=True)
    evidence_path = root / result.evidence_manifest_relative_path
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        mapping_relative = evidence["legacy_mapping"]["relative_path"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ConversionError("native seed conversion evidence cannot be read") from exc
    _validate_conversion_evidence_files(
        evidence_path,
        root / mapping_relative,
        expected_identity={
            "build_id": result.build_id,
            "profile_hash": result.profile_hash,
            "publication_id": result.publication_id,
            "snapshot_id": result.snapshot_id,
        },
        expected_counts=(result.report_count, result.parent_count, result.chunk_count),
    )
    catalog = root / result.catalog_relative_path
    uri = f"file:{catalog.as_posix()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ConversionError("native seed SQLite quick_check failed")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ConversionError("native seed SQLite integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ConversionError("native seed foreign-key check failed")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != set(RETRIEVAL_TABLES):
            raise ConversionError("native seed does not contain exactly nine retrieval tables")
        report_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(reports)")
        }
        if "is_embedded" in report_columns or {"parent_chunks", "report_revisions"} & tables:
            raise ConversionError("native seed contains a forbidden legacy shape")
        runtime = connection.execute(
            """
            SELECT active_snapshot_id, active_build_id, publication_generation,
                   write_epoch, v1_fallback_open, degraded, write_enabled
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
        if runtime != (
            result.snapshot_id,
            result.build_id,
            1,
            0,
            1,
            0,
            0,
        ):
            raise ConversionError("native seed runtime is not the epoch-zero bridge")
        if connection.execute("SELECT COUNT(*) FROM active_reports").fetchone()[0] <= 0:
            raise ConversionError("native seed active_reports is empty")
        descriptor_row = connection.execute(
            """
            SELECT file_sha256, size_bytes, dimension, metric, ntotal
            FROM vector_snapshots WHERE snapshot_id = ? AND state = 'ready'
            """,
            (result.snapshot_id,),
        ).fetchone()
        if descriptor_row is None:
            raise ConversionError("native seed snapshot descriptor is not ready")
        membership = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT chunk_uid), COUNT(DISTINCT faiss_id) "
            "FROM snapshot_membership WHERE snapshot_id = ?",
            (result.snapshot_id,),
        ).fetchone()
        if membership != (result.chunk_count, result.chunk_count, result.chunk_count):
            raise ConversionError("native seed membership is not full-N and unique")
    descriptor = SnapshotDescriptor(
        sha256=descriptor_row[0],
        size_bytes=descriptor_row[1],
        dimension=descriptor_row[2],
        metric=descriptor_row[3],
        ntotal=descriptor_row[4],
    )
    loaded = load_index(root / result.snapshot_relative_path, descriptor)
    if loaded.ntotal != result.chunk_count:
        raise ConversionError("native raw FAISS count differs from membership")


def _populate_seed_catalog(
    connection: sqlite3.Connection,
    plan: NativeSeedPlan,
    descriptor: SnapshotDescriptor,
    snapshot_relative: str,
    evidence_relative: str,
    evidence_hash: str,
) -> None:
    connection.execute(
        "INSERT INTO publication_runs (publication_id) VALUES (?)",
        (plan.publication_id,),
    )
    connection.execute(
        """
        INSERT INTO embedding_profiles (
            profile_id, profile_hash, model, dimension, metric, normalization,
            prefix_template, extractor, parent_policy_json, child_policy_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plan.profile.profile_hash,
            plan.profile.profile_hash,
            plan.profile.model,
            plan.profile.dimension,
            plan.profile.metric,
            int(plan.profile.normalization == "l2"),
            plan.profile.prefix_template,
            plan.profile.extractor,
            canonical_json(dict(plan.profile.parent_policy)),
            canonical_json(dict(plan.profile.child_policy)),
        ),
    )
    connection.execute(
        """
        INSERT INTO retrieval_builds (
            build_id, profile_id, source_manifest_json, source_manifest_sha256,
            included_count, excluded_count, expected_count,
            exclusion_policy_version, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned')
        """,
        (
            plan.build_id,
            plan.profile.profile_hash,
            plan.manifest.canonical_json,
            plan.manifest.sha256,
            plan.manifest.included_count,
            plan.manifest.excluded_count,
            plan.manifest.discovered_count,
            plan.manifest.exclusion_policy.version,
        ),
    )
    _transition_build(connection, plan.build_id, "cataloging")
    connection.executemany(
        """
        INSERT INTO reports (
            report_id, report_uid, canonical_relative_path, source_sha256,
            retrieval_metadata_sha256, report_type, report_date, target_name,
            title, broker
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                report.report_id,
                report.report_uid,
                report.canonical_relative_path,
                report.source_sha256,
                report.retrieval_metadata_sha256,
                report.report_type,
                report.report_date,
                report.target_name,
                report.title,
                report.broker,
            )
            for report in plan.reports
        ],
    )
    connection.executemany(
        """
        INSERT INTO retrieval_parents (
            parent_uid, report_id, profile_id, parent_order, content, content_sha256
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                parent.parent_uid,
                parent.report_id,
                parent.profile_id,
                parent.parent_order,
                parent.content,
                parent.content_sha256,
            )
            for parent in plan.parents
        ],
    )
    connection.executemany(
        """
        INSERT INTO retrieval_chunks (
            chunk_uid, parent_uid, profile_id, child_order,
            span_start, span_end, embedding_text_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                chunk.chunk_uid,
                chunk.parent_uid,
                chunk.profile_id,
                chunk.child_order,
                chunk.span_start,
                chunk.span_end,
                chunk.embedding_text_sha256,
            )
            for chunk in plan.chunks
        ],
    )
    _set_publication_phase(connection, plan.publication_id, "catalog_written")
    _transition_build(connection, plan.build_id, "vector_building")
    connection.execute(
        """
        INSERT INTO vector_snapshots (
            snapshot_id, build_id, relative_path, file_sha256, size_bytes,
            dimension, metric, ntotal, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged')
        """,
        (
            plan.snapshot_id,
            plan.build_id,
            snapshot_relative,
            descriptor.sha256,
            descriptor.size_bytes,
            descriptor.dimension,
            descriptor.metric,
            descriptor.ntotal,
        ),
    )
    connection.execute(
        """
        UPDATE publication_runs
        SET to_snapshot_id = ?, evidence_manifest_relative_path = ?,
            evidence_manifest_sha256 = ?
        WHERE publication_id = ?
        """,
        (plan.snapshot_id, evidence_relative, evidence_hash, plan.publication_id),
    )
    for phase in (
        "artifact_written",
        "artifact_durable",
        "artifact_published",
        "artifact_validated",
    ):
        _set_publication_phase(connection, plan.publication_id, phase)
    connection.executemany(
        "INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id) VALUES (?, ?, ?)",
        [
            (plan.snapshot_id, chunk.chunk_uid, chunk.physical_id)
            for chunk in plan.chunks
        ],
    )
    _transition_build(connection, plan.build_id, "validating")
    _transition_snapshot(connection, plan.snapshot_id, "validating")
    _transition_snapshot(connection, plan.snapshot_id, "ready")
    _transition_build(connection, plan.build_id, "ready")
    _set_publication_phase(
        connection,
        plan.publication_id,
        "rollback_backup_validated",
    )


def _validate_catalog(
    connection: sqlite3.Connection,
    plan: NativeSeedPlan,
    descriptor: SnapshotDescriptor,
) -> None:
    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise ConversionError("candidate catalog quick_check failed")
    if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise ConversionError("candidate catalog integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ConversionError("candidate catalog foreign-key check failed")
    counts = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM reports),
          (SELECT COUNT(*) FROM retrieval_parents),
          (SELECT COUNT(*) FROM retrieval_chunks),
          (SELECT COUNT(*) FROM snapshot_membership WHERE snapshot_id = ?)
        """,
        (plan.snapshot_id,),
    ).fetchone()
    if counts != (
        len(plan.reports),
        len(plan.parents),
        len(plan.chunks),
        descriptor.ntotal,
    ):
        raise ConversionError("candidate catalog full-N counts do not match")


def _read_report_rows(path: Path) -> list[dict[str, Any]]:
    resolved = path.resolve(strict=True)
    uri = f"file:{resolved.as_posix()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT file_name, report_type, report_date, target_name, title, broker
            FROM reports ORDER BY file_name
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _validate_source_hashes(values: dict[str, str], expected_names: set[str]) -> dict[str, str]:
    if not isinstance(values, dict) or set(values) != expected_names:
        missing = sorted(expected_names - set(values or {}))
        extra = sorted(set(values or {}) - expected_names)
        detail = []
        if missing:
            detail.append(f"missing={len(missing)}")
        if extra:
            detail.append(f"extra={len(extra)}")
        raise ConversionError(
            "source SHA-256 evidence must cover every discovered V1 report"
            + (f" ({', '.join(detail)})" if detail else "")
        )
    normalized: dict[str, str] = {}
    for name, digest in values.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)
        ):
            raise ConversionError(f"invalid source SHA-256 evidence for report: {name}")
        normalized[name] = digest.lower()
    return normalized


def _canonical_paths(
    names: set[str],
    supplied: dict[str, str] | None,
) -> dict[str, str]:
    values = supplied or {name: f"downloaded/{name}" for name in names}
    if set(values) != names:
        raise ConversionError("canonical relative paths must cover every report")
    return {name: normalize_relative_path(values[name]) for name in names}


def _validate_vector_parity(raw, legacy_index, chunks: list[NativeChunk]) -> float:
    maximum = 0.0
    for chunk in chunks:
        legacy = np.asarray(legacy_index.reconstruct(chunk.legacy_ordinal), dtype=np.float32)
        native = raw.reconstruct([chunk.physical_id])[0]
        error = float(np.max(np.abs(legacy - native)))
        maximum = max(maximum, error)
        if error > 1e-6:
            raise ConversionError(
                f"native vector parity exceeds 1e-6 at physical ID {chunk.physical_id}"
            )
    return maximum


def _transition_build(connection: sqlite3.Connection, build_id: str, state: str) -> None:
    connection.execute(
        """
        UPDATE retrieval_builds
        SET state = ?, state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE build_id = ?
        """,
        (state, build_id),
    )


def _transition_snapshot(connection: sqlite3.Connection, snapshot_id: str, state: str) -> None:
    connection.execute(
        """
        UPDATE vector_snapshots
        SET state = ?, state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE snapshot_id = ?
        """,
        (state, snapshot_id),
    )


def _set_publication_phase(
    connection: sqlite3.Connection,
    publication_id: str,
    phase: str,
    *,
    commit: bool = True,
) -> None:
    connection.execute(
        """
        UPDATE publication_runs
        SET phase = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE publication_id = ?
        """,
        (phase, publication_id),
    )
    if commit:
        connection.commit()


def _write_catalog_checkpoint(connection: sqlite3.Connection, target: Path) -> None:
    if target.exists():
        raise ConversionError("catalog checkpoint already exists")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        checkpoint = sqlite3.connect(temporary)
        try:
            connection.backup(checkpoint)
            checkpoint.commit()
            try:
                checkpoint_isolated_catalog(checkpoint)
            except SchemaError as exc:
                raise ConversionError(
                    'catalog checkpoint WAL did not truncate'
                ) from exc
        finally:
            checkpoint.close()
        try:
            require_main_file_only(temporary)
        except SchemaError as exc:
            raise ConversionError(
                'catalog checkpoint is not main-file-only'
            ) from exc
        _fsync_file(temporary)
        check = sqlite3.connect(
            f"file:{temporary.as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            if check.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ConversionError("catalog checkpoint integrity check failed")
            if check.execute("PRAGMA foreign_key_check").fetchall():
                raise ConversionError("catalog checkpoint foreign-key check failed")
        finally:
            check.close()
        _publish_without_overwrite(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_conversion_evidence_files(
    manifest_path: Path,
    mapping_path: Path,
    *,
    expected_identity: dict[str, str],
    expected_counts: tuple[int, int, int],
) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversionError("conversion evidence is not readable canonical JSON") from exc
    base_keys = {
        "assessment_digest",
        "build_id",
        "compatibility_bundle_id",
        "counts",
        "legacy_mapping",
        "profile_hash",
        "prohibited_conversion_calls",
        "publication_id",
        "reconstruction_digest",
        "schema_version",
        "snapshot",
        "snapshot_id",
        "source_manifest_sha256",
        "vector_max_absolute_error",
    }
    if not isinstance(manifest, dict) or manifest.get("schema_version") not in (1, 2):
        raise ConversionError("conversion evidence schema version is invalid")
    expected_keys = base_keys | (
        {"span_reconstruction"} if manifest["schema_version"] == 2 else set()
    )
    if set(manifest) != expected_keys:
        raise ConversionError("conversion evidence fields are invalid")
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        raise ConversionError("conversion evidence identity is invalid")
    if manifest["counts"] != dict(zip(("reports", "parents", "chunks"), expected_counts)):
        raise ConversionError("conversion evidence counts are invalid")
    mapping_descriptor = manifest["legacy_mapping"]
    if not isinstance(mapping_descriptor, dict) or set(mapping_descriptor) != {
        "relative_path",
        "sha256",
    } or mapping_descriptor["sha256"] != _sha256_file(mapping_path):
        raise ConversionError("conversion mapping descriptor is invalid")
    if not isinstance(mapping, dict) or set(mapping) != {
        "assessment_digest",
        "reconstruction_digest",
        "rows",
        "schema_version",
    }:
        raise ConversionError("conversion mapping fields are invalid")
    if (
        mapping["schema_version"] != 1
        or mapping["assessment_digest"] != manifest["assessment_digest"]
        or mapping["reconstruction_digest"] != manifest["reconstruction_digest"]
        or not isinstance(mapping["rows"], list)
        or len(mapping["rows"]) != expected_counts[2]
    ):
        raise ConversionError("conversion mapping identity or count is invalid")
    if manifest["schema_version"] == 2:
        _validate_span_reconstruction_evidence(manifest["span_reconstruction"])


def _validate_span_reconstruction_evidence(value: Any) -> None:
    required = {
        "claims",
        "method",
        "operations",
        "policy",
        "policy_sha256",
        "replayed_parent_count",
        "resolver_version",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ConversionError("span reconstruction evidence fields are invalid")
    claims = value["claims"]
    if (
        value["resolver_version"] != 2
        or value["method"] != "ordered-span-v1-with-ambiguity-replay"
        or not isinstance(claims, list)
        or not claims
        or value["replayed_parent_count"] != len(claims)
    ):
        raise ConversionError("span reconstruction evidence identity is invalid")
    operations = value["operations"]
    if operations != {
        "embedding": 0,
        "legacy_replay_chunking": len(claims),
        "network": 0,
        "pdf_reads": 0,
        "source_pdf_chunking": 0,
    }:
        raise ConversionError("span reconstruction operation evidence is invalid")
    policy = value["policy"]
    if not isinstance(policy, dict) or value["policy_sha256"] != sha256_text(
        canonical_json(policy)
    ):
        raise ConversionError("span reconstruction policy digest is invalid")
    claim_keys = {
        "ambiguous_child_order",
        "full_sequence_replay_matched",
        "global_assignment_cardinality",
        "legacy_parent_id",
        "local_occurrence_count",
        "method",
        "policy_id",
        "policy_sha256",
        "selected_start",
    }
    for claim in claims:
        if (
            not isinstance(claim, dict)
            or set(claim) != claim_keys
            or claim["method"] != "legacy-recursive-splitter-v1"
            or claim["policy_id"] != "legacy-recursive-splitter-v1"
            or claim["policy_sha256"] != value["policy_sha256"]
            or claim["global_assignment_cardinality"] != "multiple"
            or claim["full_sequence_replay_matched"] is not True
            or not isinstance(claim["legacy_parent_id"], str)
            or not claim["legacy_parent_id"]
        ):
            raise ConversionError("span reconstruction claim is invalid")
        for field in (
            "ambiguous_child_order",
            "local_occurrence_count",
            "selected_start",
        ):
            item = claim[field]
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ConversionError("span reconstruction claim offset is invalid")
        if claim["local_occurrence_count"] < 2:
            raise ConversionError("span reconstruction ambiguity count is invalid")


def _write_json_once(path: Path, value: dict[str, Any]) -> None:
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_without_overwrite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except FileExistsError:
        raise ConversionError(f"publication target already exists: {target.name}") from None
    except OSError as exc:
        raise ConversionError(f"atomic non-overwriting publication failed: {exc}") from exc
    source.unlink()


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _make_evidence_read_only(path: Path) -> None:
    for item in path.iterdir():
        item.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


__all__ = [
    "ConversionError",
    "ConversionResult",
    "NativeChunk",
    "NativeParent",
    "NativeReport",
    "NativeSeedPlan",
    "convert_v1_seed",
    "plan_v1_seed",
    "validate_converted_seed",
]
