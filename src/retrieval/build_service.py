"""Deterministic native snapshot build service.

Every source change produces one complete off-path snapshot. Incremental builds
reuse unchanged immutable chunks and vectors while parsing and embedding only
new or changed reports. Publication is delegated only after the candidate
catalog rows, FAISS bytes, membership, and redacted evidence are validated.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import quote

import numpy as np
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from src.retrieval.bootstrap import RuntimeSelection, inspect_runtime
from src.retrieval.identity import (
    EmbeddingProfile,
    assign_physical_ids,
    canonical_hash,
    canonical_json,
    compute_chunk_uid,
    compute_parent_uid,
    compute_report_uid,
    normalize_relative_path,
    render_embedding_prefix,
    sha256_text,
)
from src.retrieval.manifest import CorpusManifest, ExclusionPolicy, ManifestDecision
from src.retrieval.publication import (
    PublicationCoordinator,
    PublicationOutcome,
    PublicationRequest,
    publish_immutable_artifact,
)
from src.retrieval.recovery import StartupReconciler
from src.retrieval.runtime_guard import guard_before_retrieval_write
from src.retrieval.schema import SchemaError, configure_catalog_storage
from src.retrieval.vector_index import SnapshotDescriptor, build_index, load_index
from src.retrieval.writer_lock import (
    NativeWriterLock,
    WriterLease,
    assert_writer_lease_owned,
)


class NativeBuildError(RuntimeError):
    """Raised when a full-corpus candidate cannot be proved complete."""


class NativeSourceExtractionError(NativeBuildError):
    """Raised when a source PDF cannot be parsed by the declared engine policy."""


_EXCLUSION_POLICY_VERSION = "native-full-corpus-v1"
_SOURCE_DELETED = "source-deleted"
_SOURCE_SUPERSEDED = "source-superseded"
_SNAPSHOT_LINEAGE_TRAILER = b"FINANCE_LLM_V2_SNAPSHOT\0"
_DEFAULT_FALLBACK_ENGINE = "pymupdf"


class EmbeddingsPort(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


ExtractorPort = Callable[[Path, str], Any]
MetadataParser = Callable[[str], Mapping[str, Any] | None]


@dataclass(frozen=True)
class CandidateReport:
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
    existing_report_id: int | None


@dataclass(frozen=True)
class CandidateParent:
    parent_uid: str
    report_uid: str
    profile_id: str
    parent_order: int
    content: str
    content_sha256: str


@dataclass(frozen=True)
class CandidateChunk:
    chunk_uid: str
    parent_uid: str
    profile_id: str
    child_order: int
    span_start: int
    span_end: int
    embedding_text_sha256: str
    physical_id: int


@dataclass(frozen=True)
class SameSpaceCanary:
    sample_count: int
    dimension: int
    minimum_cosine_similarity: float
    maximum_norm_relative_error: float
    self_rank_one_count: int


@dataclass(frozen=True)
class SourceFileSnapshot:
    canonical_relative_path: str
    sha256: str


@dataclass(frozen=True)
class NativeBuildPlan:
    base_snapshot_id: str
    base_publication_generation: int
    base_write_epoch: int
    profile: EmbeddingProfile
    reports: tuple[CandidateReport, ...]
    parents: tuple[CandidateParent, ...]
    chunks: tuple[CandidateChunk, ...]
    manifest: CorpusManifest
    build_id: str
    snapshot_id: str
    publication_id: str
    vector_payload_sha256: str
    vectors_by_physical_id: np.ndarray
    deleted_relative_paths: tuple[str, ...]
    source_root: Path
    canonical_source_prefix: str
    source_inventory: tuple[SourceFileSnapshot, ...]
    same_space_canary: SameSpaceCanary | None
    forward_recovery: bool
    build_mode: str


@dataclass(frozen=True)
class CandidateResult:
    build_id: str
    snapshot_id: str
    publication_id: str
    snapshot_relative_path: str
    evidence_manifest_relative_path: str
    evidence_manifest_sha256: str
    descriptor: SnapshotDescriptor
    report_count: int
    parent_count: int
    chunk_count: int
    source_root: Path
    canonical_source_prefix: str
    deleted_relative_paths: tuple[str, ...]
    source_inventory: tuple[SourceFileSnapshot, ...]


@dataclass(frozen=True)
class _ReusableSnapshot:
    profile: EmbeddingProfile
    parents: tuple[CandidateParent, ...]
    chunks: tuple[CandidateChunk, ...]
    vectors_by_chunk_uid: Mapping[str, np.ndarray]


def prepare_full_corpus_build(
    legacy_db_path: str | Path,
    source_directory: str | Path,
    *,
    embeddings: EmbeddingsPort,
    model: str,
    extractor_name: str,
    parent_chunk_size: int,
    child_chunk_size: int,
    fallback_extractor_name: str | None = None,
    use_parent_child: bool = True,
    single_chunk_size: int | None = None,
    data_root: str | Path | None = None,
    extractor: ExtractorPort | None = None,
    metadata_parser: MetadataParser | None = None,
    metric: str = "l2",
    normalization: str = "none",
    prefix_template: str = "[Company: {target_name}, Title: {title}]\n",
    canonical_source_prefix: str = "downloaded",
    deleted_relative_paths: Iterable[str] = (),
    allow_extraction_fallback: bool = True,
    allow_degraded_forward_recovery: bool = False,
    writer_lease: WriterLease | None = None,
    _reuse_unchanged_vectors: bool = False,
) -> NativeBuildPlan | None:
    """Prepare one deterministic full-corpus successor entirely off path.

    Epoch-zero planning requires the dedicated first-successor writer lease;
    ordinary updater entrypoints remain fail-closed until publication enables
    native writes.
    """

    selection = guard_before_retrieval_write(
        legacy_db_path,
        data_root=data_root,
        allow_degraded_forward_recovery=allow_degraded_forward_recovery,
        first_successor_writer_lease=writer_lease,
    )
    if not selection.is_native:
        raise NativeBuildError("a native epoch-zero seed is required before successor build")
    source_root = Path(source_directory).resolve(strict=True)
    if not source_root.is_dir() or source_root.is_symlink():
        raise NativeBuildError("source directory must be a real local directory")
    _positive_size(parent_chunk_size, "parent_chunk_size")
    _positive_size(child_chunk_size, "child_chunk_size")
    if child_chunk_size > parent_chunk_size:
        raise NativeBuildError("child chunks cannot be larger than parent chunks")
    if not isinstance(use_parent_child, bool):
        raise NativeBuildError("use_parent_child must be a boolean")
    if not use_parent_child:
        _positive_size(single_chunk_size, "single_chunk_size")
    if not isinstance(allow_extraction_fallback, bool):
        raise NativeBuildError("allow_extraction_fallback must be a boolean")

    deleted = tuple(
        sorted({normalize_relative_path(value) for value in deleted_relative_paths})
    )
    active_reports_by_path, existing_reports = _read_catalog_sources(selection)
    active_paths = set(active_reports_by_path)
    normalized_source_prefix = normalize_relative_path(canonical_source_prefix)
    discovered = _discover_current_sources(
        source_root,
        canonical_source_prefix=normalized_source_prefix,
        active_paths=active_paths,
        deleted_paths=set(deleted),
    )
    source_inventory = _capture_source_inventory(
        source_root,
        canonical_source_prefix=normalized_source_prefix,
        ignored_paths=set(deleted),
    )
    discovered_paths = {canonical for canonical, _path in discovered}
    inventory_paths = {
        source.canonical_relative_path for source in source_inventory
    }
    if discovered_paths != inventory_paths:
        raise NativeBuildError("source corpus changed during discovery")
    source_hashes = {
        source.canonical_relative_path: source.sha256 for source in source_inventory
    }
    parser = metadata_parser or _default_metadata_parser
    canonical_extractor_name = _canonical_extractor_name(
        extractor_name,
        allow_custom=extractor is not None,
    )
    canonical_fallback_name = _canonical_fallback_extractor_name(
        fallback_extractor_name,
        requested_engine=canonical_extractor_name,
        allow_custom=extractor is not None,
    )
    effective_allow_fallback = (
        allow_extraction_fallback and canonical_fallback_name is not None
    )
    extractor_port = extractor or (
        lambda path, engine: _default_extractor(
            path,
            engine,
            allow_fallback=effective_allow_fallback,
            fallback_engine=canonical_fallback_name,
        )
    )

    source_records: list[dict[str, Any]] = []
    for canonical_path, path in discovered:
        metadata = parser(path.name)
        if not isinstance(metadata, Mapping):
            raise NativeBuildError(f"source metadata cannot be parsed: {path.name}")
        normalized_metadata = _normalize_metadata(metadata, path.name)
        source_sha256 = source_hashes[canonical_path]
        metadata_payload = {
            "broker": normalized_metadata["broker"],
            "report_date": normalized_metadata["report_date"],
            "report_type": normalized_metadata["report_type"],
            "target_name": normalized_metadata["target_name"],
            "title": normalized_metadata["title"],
        }
        metadata_sha256 = sha256_text(canonical_json(metadata_payload))
        report_uid = compute_report_uid(canonical_path, source_sha256, metadata_sha256)
        record = {
            **normalized_metadata,
            "file_name": path.name,
            "path": path,
            "canonical_relative_path": canonical_path,
            "source_sha256": source_sha256,
            "retrieval_metadata_sha256": metadata_sha256,
            "report_uid": report_uid,
            "existing_report_id": existing_reports.get(report_uid),
        }
        source_records.append(record)

    if not source_records:
        raise NativeBuildError("full-corpus build discovered no source reports")

    candidate_report_uids = {str(record["report_uid"]) for record in source_records}
    if (
        selection.write_epoch == 0
        and not candidate_report_uids.difference(active_reports_by_path.values())
    ):
        raise NativeBuildError(
            "first native successor must include at least one new logical corpus member"
        )

    reports = tuple(
        CandidateReport(
            report_uid=record["report_uid"],
            canonical_relative_path=record["canonical_relative_path"],
            source_sha256=record["source_sha256"],
            retrieval_metadata_sha256=record["retrieval_metadata_sha256"],
            report_type=record["report_type"],
            report_date=record["report_date"],
            target_name=record["target_name"],
            title=record["title"],
            broker=record["broker"],
            file_name=record["file_name"],
            existing_report_id=record["existing_report_id"],
        )
        for record in sorted(source_records, key=lambda item: bytes.fromhex(item["report_uid"]))
    )
    manifest = _build_candidate_manifest(
        reports,
        active_reports_by_path=active_reports_by_path,
        deleted_paths=set(deleted),
    )
    if _reuse_unchanged_vectors:
        return _prepare_incremental_plan(
            selection=selection,
            reports=reports,
            source_records=source_records,
            manifest=manifest,
            active_reports_by_path=active_reports_by_path,
            source_root=source_root,
            source_inventory=source_inventory,
            canonical_source_prefix=normalized_source_prefix,
            deleted_relative_paths=deleted,
            embeddings=embeddings,
            model=model,
            extractor_name=canonical_extractor_name,
            extractor=extractor_port,
            fallback_extractor_name=canonical_fallback_name,
            allow_extraction_fallback=effective_allow_fallback,
            parent_chunk_size=parent_chunk_size,
            child_chunk_size=child_chunk_size,
            use_parent_child=use_parent_child,
            single_chunk_size=single_chunk_size,
            metric=metric,
            normalization=normalization,
            prefix_template=prefix_template,
        )

    canary = (
        _validate_same_space_canary(
            selection,
            embeddings,
            metric=metric,
            normalization=normalization,
        )
        if selection.write_epoch == 0
        else None
    )
    extracted = [
        (
            record,
            _extract_source(
                record["path"],
                canonical_extractor_name,
                extractor_port,
                allow_fallback=effective_allow_fallback,
                fallback_engine=canonical_fallback_name,
            ),
        )
        for record in source_records
    ]

    provisional_parents, provisional_chunks, embedding_texts = _split_full_corpus(
        extracted,
        parent_chunk_size=parent_chunk_size,
        child_chunk_size=child_chunk_size,
        use_parent_child=use_parent_child,
        single_chunk_size=single_chunk_size,
        prefix_template=prefix_template,
    )
    try:
        vectors = np.asarray(embeddings.embed_documents(embedding_texts), dtype=np.float32)
    except Exception as exc:
        raise NativeBuildError(f"full-corpus embedding failed: {exc}") from exc
    if vectors.ndim != 2 or vectors.shape[0] != len(provisional_chunks) or vectors.shape[1] <= 0:
        raise NativeBuildError("embedding result shape does not cover every candidate chunk")
    if canary is not None and vectors.shape[1] != canary.dimension:
        raise NativeBuildError("successor vector dimension differs from the epoch-zero seed")
    if not np.isfinite(vectors).all():
        raise NativeBuildError("embedding result contains a non-finite vector")
    if normalization == "l2":
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise NativeBuildError("zero vector cannot be L2-normalized")
        vectors = np.asarray(vectors / norms, dtype=np.float32)

    if use_parent_child:
        parent_policy = {
            "algorithm": "langchain-recursive-v1",
            "chunk_overlap": int(parent_chunk_size * 0.1),
            "chunk_size": parent_chunk_size,
            "headers": ["#", "##", "###"],
            "separators": ["\n\n", "\n", ". ", " ", ""],
        }
        child_policy = {
            "algorithm": "langchain-recursive-v1",
            "chunk_overlap": int(child_chunk_size * 0.1),
            "chunk_size": child_chunk_size,
            "separators": ["\n\n", "\n", ". ", " ", ""],
            "span_source": "splitter_start_index",
        }
    else:
        parent_policy = {
            "algorithm": "langchain-recursive-single-level-v1",
            "chunk_overlap": int(single_chunk_size * 0.1),
            "chunk_size": single_chunk_size,
            "headers": ["#", "##", "###"],
            "separators": ["\n\n", "\n", ". ", " ", ""],
        }
        child_policy = {
            "algorithm": "identity-span-v1",
            "span_source": "whole-parent",
        }

    profile = EmbeddingProfile(
        model=model,
        dimension=int(vectors.shape[1]),
        metric=metric,
        normalization=normalization,
        prefix_template=prefix_template,
        extractor=format_extraction_profile(
            canonical_extractor_name,
            allow_fallback=effective_allow_fallback,
            fallback_engine=canonical_fallback_name,
            allow_custom=extractor is not None,
        ),
        parent_policy=parent_policy,
        child_policy=child_policy,
    )
    profile_id = profile.profile_hash

    parents: list[CandidateParent] = []
    parent_uid_by_key: dict[tuple[str, int], str] = {}
    for parent in provisional_parents:
        parent_uid = compute_parent_uid(
            profile_id,
            parent["report_uid"],
            parent["parent_order"],
            parent["content_sha256"],
        )
        parent_uid_by_key[(parent["report_uid"], parent["parent_order"])] = parent_uid
        parents.append(
            CandidateParent(
                parent_uid=parent_uid,
                report_uid=parent["report_uid"],
                profile_id=profile_id,
                parent_order=parent["parent_order"],
                content=parent["content"],
                content_sha256=parent["content_sha256"],
            )
        )

    provisional_uids: list[str] = []
    for chunk in provisional_chunks:
        parent_uid = parent_uid_by_key[(chunk["report_uid"], chunk["parent_order"])]
        provisional_uids.append(
            compute_chunk_uid(
                profile_id,
                parent_uid,
                chunk["child_order"],
                chunk["span_start"],
                chunk["span_end"],
                chunk["embedding_text_sha256"],
            )
        )
    physical_ids = assign_physical_ids(provisional_uids)
    chunks = tuple(
        CandidateChunk(
            chunk_uid=chunk_uid,
            parent_uid=parent_uid_by_key[(chunk["report_uid"], chunk["parent_order"])],
            profile_id=profile_id,
            child_order=chunk["child_order"],
            span_start=chunk["span_start"],
            span_end=chunk["span_end"],
            embedding_text_sha256=chunk["embedding_text_sha256"],
            physical_id=physical_ids[chunk_uid],
        )
        for chunk, chunk_uid in zip(provisional_chunks, provisional_uids, strict=True)
    )
    vector_order = np.argsort(
        np.asarray([chunk.physical_id for chunk in chunks], dtype=np.int64),
        kind="stable",
    )
    vectors_by_physical_id = np.ascontiguousarray(vectors[vector_order], dtype=np.float32)
    vectors_by_physical_id.setflags(write=False)
    return _finalize_native_build_plan(
        selection=selection,
        profile=profile,
        reports=reports,
        parents=tuple(parents),
        chunks=chunks,
        manifest=manifest,
        vectors_by_physical_id=vectors_by_physical_id,
        deleted_relative_paths=deleted,
        source_root=source_root,
        canonical_source_prefix=normalized_source_prefix,
        source_inventory=source_inventory,
        same_space_canary=canary,
        build_mode="full",
    )


def prepare_incremental_build(
    legacy_db_path: str | Path,
    source_directory: str | Path,
    **kwargs: Any,
) -> NativeBuildPlan | None:
    """Build a complete successor while reusing every unchanged active vector."""

    if "_reuse_unchanged_vectors" in kwargs:
        raise TypeError("_reuse_unchanged_vectors is internal")
    return prepare_full_corpus_build(
        legacy_db_path,
        source_directory,
        **kwargs,
        _reuse_unchanged_vectors=True,
    )


def _prepare_incremental_plan(
    *,
    selection: RuntimeSelection,
    reports: tuple[CandidateReport, ...],
    source_records: list[dict[str, Any]],
    manifest: CorpusManifest,
    active_reports_by_path: Mapping[str, str],
    source_root: Path,
    source_inventory: tuple[SourceFileSnapshot, ...],
    canonical_source_prefix: str,
    deleted_relative_paths: tuple[str, ...],
    embeddings: EmbeddingsPort,
    model: str,
    extractor_name: str,
    extractor: ExtractorPort,
    fallback_extractor_name: str | None,
    allow_extraction_fallback: bool,
    parent_chunk_size: int,
    child_chunk_size: int,
    use_parent_child: bool,
    single_chunk_size: int | None,
    metric: str,
    normalization: str,
    prefix_template: str,
) -> NativeBuildPlan | None:
    if selection.write_epoch <= 0 or not selection.write_enabled:
        raise NativeBuildError("incremental updates require a writable native runtime")
    changed_records = [
        record
        for record in source_records
        if active_reports_by_path.get(record["canonical_relative_path"])
        != record["report_uid"]
    ]
    if not changed_records and not deleted_relative_paths:
        return None

    changed_uids = {str(record["report_uid"]) for record in changed_records}
    unchanged_uids = {
        report.report_uid for report in reports if report.report_uid not in changed_uids
    }
    reusable = _read_reusable_snapshot(selection, unchanged_uids)
    _validate_incremental_profile(
        reusable.profile,
        model=model,
        extractor_name=extractor_name,
        fallback_extractor_name=fallback_extractor_name,
        allow_extraction_fallback=allow_extraction_fallback,
        parent_chunk_size=parent_chunk_size,
        child_chunk_size=child_chunk_size,
        use_parent_child=use_parent_child,
        single_chunk_size=single_chunk_size,
        metric=metric,
        normalization=normalization,
        prefix_template=prefix_template,
    )

    canary = (
        _validate_same_space_canary(
            selection,
            embeddings,
            metric=metric,
            normalization=normalization,
        )
        if changed_records
        else None
    )
    extracted = [
        (
            record,
            _extract_source(
                record["path"],
                extractor_name,
                extractor,
                allow_fallback=allow_extraction_fallback,
                fallback_engine=fallback_extractor_name,
            ),
        )
        for record in changed_records
    ]
    if extracted:
        provisional_parents, provisional_chunks, embedding_texts = _split_full_corpus(
            extracted,
            parent_chunk_size=parent_chunk_size,
            child_chunk_size=child_chunk_size,
            use_parent_child=use_parent_child,
            single_chunk_size=single_chunk_size,
            prefix_template=prefix_template,
        )
        try:
            new_vectors = np.asarray(
                embeddings.embed_documents(embedding_texts),
                dtype=np.float32,
            )
        except Exception as exc:
            raise NativeBuildError(f"incremental embedding failed: {exc}") from exc
        if (
            new_vectors.ndim != 2
            or new_vectors.shape[0] != len(provisional_chunks)
            or new_vectors.shape[1] != reusable.profile.dimension
        ):
            raise NativeBuildError(
                "incremental embedding result does not match the active vector space"
            )
        if not np.isfinite(new_vectors).all():
            raise NativeBuildError("incremental embedding contains a non-finite vector")
        if normalization == "l2":
            norms = np.linalg.norm(new_vectors, axis=1, keepdims=True)
            if np.any(norms == 0):
                raise NativeBuildError("zero vector cannot be L2-normalized")
            new_vectors = np.asarray(new_vectors / norms, dtype=np.float32)
    else:
        provisional_parents = []
        provisional_chunks = []
        new_vectors = np.empty((0, reusable.profile.dimension), dtype=np.float32)

    profile_id = reusable.profile.profile_hash
    new_parents: list[CandidateParent] = []
    parent_uid_by_key: dict[tuple[str, int], str] = {}
    for parent in provisional_parents:
        parent_uid = compute_parent_uid(
            profile_id,
            parent["report_uid"],
            parent["parent_order"],
            parent["content_sha256"],
        )
        parent_uid_by_key[(parent["report_uid"], parent["parent_order"])] = parent_uid
        new_parents.append(
            CandidateParent(
                parent_uid=parent_uid,
                report_uid=parent["report_uid"],
                profile_id=profile_id,
                parent_order=parent["parent_order"],
                content=parent["content"],
                content_sha256=parent["content_sha256"],
            )
        )

    provisional_uids: list[str] = []
    for chunk in provisional_chunks:
        parent_uid = parent_uid_by_key[(chunk["report_uid"], chunk["parent_order"])]
        provisional_uids.append(
            compute_chunk_uid(
                profile_id,
                parent_uid,
                chunk["child_order"],
                chunk["span_start"],
                chunk["span_end"],
                chunk["embedding_text_sha256"],
            )
        )

    vector_by_chunk_uid = dict(reusable.vectors_by_chunk_uid)
    raw_chunks = list(reusable.chunks)
    for chunk, chunk_uid, vector in zip(
        provisional_chunks,
        provisional_uids,
        new_vectors,
        strict=True,
    ):
        parent_uid = parent_uid_by_key[(chunk["report_uid"], chunk["parent_order"])]
        raw_chunks.append(
            CandidateChunk(
                chunk_uid=chunk_uid,
                parent_uid=parent_uid,
                profile_id=profile_id,
                child_order=chunk["child_order"],
                span_start=chunk["span_start"],
                span_end=chunk["span_end"],
                embedding_text_sha256=chunk["embedding_text_sha256"],
                physical_id=0,
            )
        )
        vector_by_chunk_uid[chunk_uid] = np.asarray(vector, dtype=np.float32)

    if not raw_chunks:
        raise NativeBuildError("incremental update cannot publish an empty corpus")
    physical_ids = assign_physical_ids(chunk.chunk_uid for chunk in raw_chunks)
    chunks = tuple(
        CandidateChunk(
            chunk_uid=chunk.chunk_uid,
            parent_uid=chunk.parent_uid,
            profile_id=chunk.profile_id,
            child_order=chunk.child_order,
            span_start=chunk.span_start,
            span_end=chunk.span_end,
            embedding_text_sha256=chunk.embedding_text_sha256,
            physical_id=physical_ids[chunk.chunk_uid],
        )
        for chunk in raw_chunks
    )
    ordered_chunks = sorted(chunks, key=lambda item: item.physical_id)
    vectors_by_physical_id = np.ascontiguousarray(
        np.vstack([vector_by_chunk_uid[chunk.chunk_uid] for chunk in ordered_chunks]),
        dtype=np.float32,
    )
    vectors_by_physical_id.setflags(write=False)
    return _finalize_native_build_plan(
        selection=selection,
        profile=reusable.profile,
        reports=reports,
        parents=(*reusable.parents, *new_parents),
        chunks=chunks,
        manifest=manifest,
        vectors_by_physical_id=vectors_by_physical_id,
        deleted_relative_paths=deleted_relative_paths,
        source_root=source_root,
        canonical_source_prefix=canonical_source_prefix,
        source_inventory=source_inventory,
        same_space_canary=canary,
        build_mode="incremental",
    )


def _finalize_native_build_plan(
    *,
    selection: RuntimeSelection,
    profile: EmbeddingProfile,
    reports: tuple[CandidateReport, ...],
    parents: tuple[CandidateParent, ...],
    chunks: tuple[CandidateChunk, ...],
    manifest: CorpusManifest,
    vectors_by_physical_id: np.ndarray,
    deleted_relative_paths: tuple[str, ...],
    source_root: Path,
    canonical_source_prefix: str,
    source_inventory: tuple[SourceFileSnapshot, ...],
    same_space_canary: SameSpaceCanary | None,
    build_mode: str,
) -> NativeBuildPlan:
    if build_mode not in {"full", "incremental"}:
        raise NativeBuildError("unknown native build mode")
    _validate_plan_membership(reports, list(parents), chunks, manifest)
    if vectors_by_physical_id.shape != (len(chunks), profile.dimension):
        raise NativeBuildError("candidate vector payload shape is inconsistent")
    vector_payload_sha256 = hashlib.sha256(
        vectors_by_physical_id.astype("<f4", copy=False).tobytes(order="C")
    ).hexdigest()
    topology_sha256 = sha256_text(
        canonical_json(
            {
                "parents": [
                    {
                        "content_sha256": parent.content_sha256,
                        "parent_order": parent.parent_order,
                        "parent_uid": parent.parent_uid,
                        "report_uid": parent.report_uid,
                    }
                    for parent in sorted(parents, key=lambda item: item.parent_uid)
                ],
                "chunks": [
                    {
                        "chunk_uid": chunk.chunk_uid,
                        "embedding_text_sha256": chunk.embedding_text_sha256,
                        "parent_uid": chunk.parent_uid,
                        "span_end": chunk.span_end,
                        "span_start": chunk.span_start,
                    }
                    for chunk in sorted(chunks, key=lambda item: item.chunk_uid)
                ],
            }
        )
    )
    build_id = canonical_hash(
        "retrieval-build" if build_mode == "full" else "retrieval-incremental-build",
        selection.publication_generation,
        selection.active_snapshot_id,
        profile.profile_hash,
        manifest.sha256,
        topology_sha256,
    )
    membership_sha256 = sha256_text(
        canonical_json(
            [
                {"chunk_uid": chunk.chunk_uid, "faiss_id": chunk.physical_id}
                for chunk in sorted(chunks, key=lambda item: item.physical_id)
            ]
        )
    )
    snapshot_id = canonical_hash(
        "vector-snapshot",
        build_id,
        membership_sha256,
        vector_payload_sha256,
    )
    publication_id = canonical_hash(
        "native-publication",
        selection.publication_generation,
        selection.active_snapshot_id,
        snapshot_id,
    )
    _validate_source_inventory(
        source_root,
        canonical_source_prefix=canonical_source_prefix,
        ignored_paths=set(deleted_relative_paths),
        expected=source_inventory,
    )
    return NativeBuildPlan(
        base_snapshot_id=str(selection.active_snapshot_id),
        base_publication_generation=selection.publication_generation,
        base_write_epoch=selection.write_epoch,
        profile=profile,
        reports=reports,
        parents=parents,
        chunks=chunks,
        manifest=manifest,
        build_id=build_id,
        snapshot_id=snapshot_id,
        publication_id=publication_id,
        vector_payload_sha256=vector_payload_sha256,
        vectors_by_physical_id=vectors_by_physical_id,
        deleted_relative_paths=deleted_relative_paths,
        source_root=source_root,
        canonical_source_prefix=canonical_source_prefix,
        source_inventory=source_inventory,
        same_space_canary=same_space_canary,
        forward_recovery=bool(selection.degraded),
        build_mode=build_mode,
    )


def materialize_candidate(
    plan: NativeBuildPlan,
    data_root: str | Path,
    *,
    writer_lease: WriterLease | None = None,
) -> CandidateResult:
    """Write and validate a ready candidate without changing runtime pointers."""

    root = Path(data_root).resolve(strict=True)
    if writer_lease is None:
        with NativeWriterLock(root) as owned_lease:
            return materialize_candidate(
                plan,
                root,
                writer_lease=owned_lease,
            )
    assert_writer_lease_owned(writer_lease, root)
    _validate_plan_source_inventory(plan)
    catalog = root / "retrieval" / "v2" / "catalog.sqlite3"
    if not catalog.is_file() or catalog.is_symlink():
        raise NativeBuildError("native catalog is unavailable")
    _validate_planned_vector_payload(plan)
    existing = _completed_candidate_result(plan, root, catalog)
    if existing is not None:
        return existing

    snapshot_relative = f"retrieval/v2/snapshots/{plan.snapshot_id}.faiss"
    snapshot_path = root.joinpath(*snapshot_relative.split("/"))
    staging = root / "retrieval" / "v2" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    # Keep the staging basename short enough for non-long-path Windows hosts;
    # the content-addressed identity lives in the final filename and catalog.
    staged = staging / f"candidate-{uuid.uuid4().hex[:12]}.faiss"
    index = build_index(
        plan.vectors_by_physical_id,
        range(1, len(plan.chunks) + 1),
        plan.profile.metric,
    )
    if snapshot_path.exists() or snapshot_path.is_symlink():
        descriptor = _descriptor_for_existing_snapshot(snapshot_path, plan)
    else:
        descriptor = _write_lineage_addressed_snapshot(
            index,
            staged,
            snapshot_id=plan.snapshot_id,
        )
        try:
            publish_immutable_artifact(staged, snapshot_path, descriptor)
        except FileExistsError:
            # An interrupted/retried content-addressed build may have already
            # published this path. Adopt only bytes that prove the same plan.
            descriptor = _descriptor_for_existing_snapshot(snapshot_path, plan)
        finally:
            if staged.exists():
                staged.unlink()
        _validate_snapshot_vector_payload(snapshot_path, descriptor, plan)

    _validate_plan_source_inventory(plan)
    connection = sqlite3.connect(catalog)
    connection.row_factory = sqlite3.Row
    try:
        configure_catalog_storage(connection, writable=True)
    except SchemaError as exc:
        connection.close()
        raise NativeBuildError('native catalog storage mode is invalid') from exc
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        _assert_base_runtime(connection, plan)
        _insert_profile(connection, plan.profile)
        report_ids = _insert_reports(connection, plan.reports)
        _insert_build(connection, plan)
        _transition_build(connection, plan.build_id, "cataloging")
        _insert_parents_and_chunks(connection, plan, report_ids)
        _transition_build(connection, plan.build_id, "vector_building")
        connection.execute(
            """
            INSERT INTO vector_snapshots (
                snapshot_id, build_id, relative_path, file_sha256,
                size_bytes, dimension, metric, ntotal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        connection.executemany(
            "INSERT INTO snapshot_membership (snapshot_id, chunk_uid, faiss_id) VALUES (?, ?, ?)",
            [
                (plan.snapshot_id, chunk.chunk_uid, chunk.physical_id)
                for chunk in plan.chunks
            ],
        )
        _transition_build(connection, plan.build_id, "validating")
        _transition_snapshot(connection, plan.snapshot_id, "validating")
        _validate_catalog_candidate(connection, plan, descriptor)
        _transition_snapshot(connection, plan.snapshot_id, "ready")
        _transition_build(connection, plan.build_id, "ready")
        connection.commit()
    except Exception:
        connection.rollback()
        _mark_candidate_failed(connection, plan)
        raise
    finally:
        connection.close()

    evidence_relative, evidence_sha256 = _write_candidate_evidence(plan, descriptor, root)
    return CandidateResult(
        build_id=plan.build_id,
        snapshot_id=plan.snapshot_id,
        publication_id=plan.publication_id,
        snapshot_relative_path=snapshot_relative,
        evidence_manifest_relative_path=evidence_relative,
        evidence_manifest_sha256=evidence_sha256,
        descriptor=descriptor,
        report_count=len(plan.reports),
        parent_count=len(plan.parents),
        chunk_count=len(plan.chunks),
        source_root=plan.source_root,
        canonical_source_prefix=plan.canonical_source_prefix,
        deleted_relative_paths=plan.deleted_relative_paths,
        source_inventory=plan.source_inventory,
    )


def publish_candidate(
    result: CandidateResult,
    data_root: str | Path,
    *,
    writer_lease: WriterLease | None = None,
) -> PublicationOutcome:
    """Publish a previously validated complete candidate."""

    root = Path(data_root).resolve(strict=True)
    if writer_lease is None:
        with NativeWriterLock(root) as owned_lease:
            return publish_candidate(
                result,
                root,
                writer_lease=owned_lease,
            )
    assert_writer_lease_owned(writer_lease, root)
    _validate_candidate_source_inventory(result)
    return PublicationCoordinator(root).publish(
        PublicationRequest(
            publication_id=result.publication_id,
            to_snapshot_id=result.snapshot_id,
            evidence_manifest_relative_path=result.evidence_manifest_relative_path,
            evidence_manifest_sha256=result.evidence_manifest_sha256,
            increment_write_epoch=True,
            enable_writes_on_complete=True,
        ),
        writer_lease=writer_lease,
    )


def execute_full_corpus_successor(
    legacy_db_path: str | Path,
    source_directory: str | Path,
    **kwargs: Any,
) -> tuple[CandidateResult, PublicationOutcome]:
    """Prepare, materialize, and publish one full-corpus native successor."""

    root = Path(kwargs.get("data_root") or Path(legacy_db_path).parent).resolve(strict=True)
    with NativeWriterLock(root) as writer_lease:
        StartupReconciler(root).reconcile(writer_lease=writer_lease)
        plan = prepare_full_corpus_build(
            legacy_db_path,
            source_directory,
            **{
                **kwargs,
                "allow_degraded_forward_recovery": True,
                "writer_lease": writer_lease,
            },
        )
        if plan is None:
            raise NativeBuildError("full-corpus planning unexpectedly produced no candidate")
        result = materialize_candidate(plan, root, writer_lease=writer_lease)
        return result, publish_candidate(
            result,
            root,
            writer_lease=writer_lease,
        )


def execute_incremental_update(
    legacy_db_path: str | Path,
    source_directory: str | Path,
    **kwargs: Any,
) -> tuple[CandidateResult, PublicationOutcome] | None:
    """Publish changed reports while carrying unchanged chunks and vectors forward."""

    root = Path(kwargs.get("data_root") or Path(legacy_db_path).parent).resolve(strict=True)
    with NativeWriterLock(root) as writer_lease:
        StartupReconciler(root).reconcile(writer_lease=writer_lease)
        plan = prepare_incremental_build(
            legacy_db_path,
            source_directory,
            **{
                **kwargs,
                "allow_degraded_forward_recovery": True,
                "writer_lease": writer_lease,
            },
        )
        if plan is None:
            return None
        result = materialize_candidate(plan, root, writer_lease=writer_lease)
        return result, publish_candidate(
            result,
            root,
            writer_lease=writer_lease,
        )


def validate_epoch_zero_same_space_canary(
    legacy_db_path: str | Path,
    *,
    embeddings: EmbeddingsPort,
    data_root: str | Path | None = None,
    metric: str = "l2",
    normalization: str = "none",
) -> SameSpaceCanary:
    """Prove the configured provider still emits the imported V1 vector space.

    This read-only entrypoint lets an off-path migration validate its converted
    epoch-zero seed before that seed becomes visible to any supported launcher.
    The full successor builder performs the same check again before its first
    publication.
    """

    selection = inspect_runtime(
        legacy_db_path,
        data_root=data_root,
        validate_snapshot=True,
    )
    if (
        not selection.is_native
        or selection.write_epoch != 0
        or not selection.v1_fallback_open
        or selection.degraded
        or selection.write_enabled
        or not selection.active_snapshot_id
    ):
        raise NativeBuildError(
            "same-space canary requires a healthy epoch-zero native seed"
        )
    return _validate_same_space_canary(
        selection,
        embeddings,
        metric=metric,
        normalization=normalization,
    )


def _validate_same_space_canary(
    selection: RuntimeSelection,
    embeddings: EmbeddingsPort,
    *,
    metric: str,
    normalization: str,
) -> SameSpaceCanary:
    """Prove the first native writer embeds in the converted seed's space."""

    connection = _open_read_only_catalog(selection.paths.catalog)
    connection.row_factory = sqlite3.Row
    try:
        descriptor_row = connection.execute(
            """
            SELECT snapshot.relative_path, snapshot.file_sha256,
                   snapshot.size_bytes, snapshot.dimension, snapshot.metric,
                   snapshot.ntotal, profile.normalization,
                   profile.prefix_template
            FROM vector_snapshots AS snapshot
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
            WHERE snapshot.snapshot_id = ?
            """,
            (selection.active_snapshot_id,),
        ).fetchone()
        if descriptor_row is None:
            raise NativeBuildError("epoch-zero seed descriptor is missing")
        if descriptor_row[4] != metric:
            raise NativeBuildError("successor metric differs from the epoch-zero seed")
        expected_normalization = "l2" if int(descriptor_row[6]) else "none"
        if expected_normalization != normalization:
            raise NativeBuildError("successor normalization differs from the epoch-zero seed")
        sample_size = min(64, int(descriptor_row[5]))
        if sample_size <= 0:
            raise NativeBuildError("epoch-zero seed contains no canary vectors")
        rows = connection.execute(
            """
            SELECT membership.faiss_id, parent.content, chunk.span_start,
                   chunk.span_end, report.target_name, report.title,
                   report.report_date, report.report_type, report.broker,
                   report.canonical_relative_path
            FROM snapshot_membership AS membership
            JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE membership.snapshot_id = ?
            ORDER BY chunk.chunk_uid
            LIMIT ?
            """,
            (selection.active_snapshot_id, sample_size),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != sample_size:
        raise NativeBuildError("epoch-zero canary membership is incomplete")

    prefix_template = str(descriptor_row[7])
    canary_texts: list[str] = []
    physical_ids: list[int] = []
    for row in rows:
        content = str(row[1])
        start, end = int(row[2]), int(row[3])
        if start < 0 or end <= start or end > len(content):
            raise NativeBuildError("epoch-zero canary contains an invalid child span")
        metadata = {
            "target_name": row[4],
            "title": row[5],
            "report_date": row[6],
            "report_type": row[7],
            "broker": row[8],
            "canonical_relative_path": row[9],
        }
        canary_texts.append(render_embedding_prefix(prefix_template, metadata) + content[start:end])
        physical_ids.append(int(row[0]))

    descriptor = SnapshotDescriptor(
        sha256=str(descriptor_row[1]),
        size_bytes=int(descriptor_row[2]),
        dimension=int(descriptor_row[3]),
        metric=str(descriptor_row[4]),
        ntotal=int(descriptor_row[5]),
    )
    snapshot_path = selection.paths.data_root.joinpath(*str(descriptor_row[0]).split("/"))
    stored = load_index(snapshot_path, descriptor).reconstruct(physical_ids)
    try:
        regenerated = np.asarray(embeddings.embed_documents(canary_texts), dtype=np.float32)
    except Exception as exc:
        raise NativeBuildError(f"same-space canary embedding failed: {exc}") from exc
    if regenerated.shape != stored.shape or regenerated.shape[1] != descriptor.dimension:
        raise NativeBuildError("same-space canary dimension/count mismatch")
    if not np.isfinite(regenerated).all():
        raise NativeBuildError("same-space canary contains a non-finite vector")
    if normalization == "l2":
        norms = np.linalg.norm(regenerated, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise NativeBuildError("same-space canary produced a zero vector")
        regenerated = np.asarray(regenerated / norms, dtype=np.float32)

    stored_norms = np.linalg.norm(stored, axis=1)
    regenerated_norms = np.linalg.norm(regenerated, axis=1)
    if np.any(stored_norms == 0) or np.any(regenerated_norms == 0):
        raise NativeBuildError("same-space canary cannot compare zero-norm vectors")
    cosines = np.sum(stored * regenerated, axis=1) / (stored_norms * regenerated_norms)
    norm_errors = np.abs(regenerated_norms - stored_norms) / stored_norms
    minimum_cosine = float(np.min(cosines))
    maximum_norm_error = float(np.max(norm_errors))
    if minimum_cosine < 0.999:
        raise NativeBuildError("same-space canary cosine threshold failed")
    if maximum_norm_error > 0.01:
        raise NativeBuildError("same-space canary vector-norm threshold failed")

    self_rank_one = _count_text_aware_self_rank_one(
        stored,
        regenerated,
        canary_texts,
        metric=metric,
    )
    if self_rank_one != sample_size:
        raise NativeBuildError("same-space canary stored-vector self-rank-one threshold failed")
    return SameSpaceCanary(
        sample_count=sample_size,
        dimension=descriptor.dimension,
        minimum_cosine_similarity=minimum_cosine,
        maximum_norm_relative_error=maximum_norm_error,
        self_rank_one_count=self_rank_one,
    )


def _count_text_aware_self_rank_one(
    stored: np.ndarray,
    regenerated: np.ndarray,
    canary_texts: list[str],
    *,
    metric: str,
) -> int:
    """Count expected text groups that are first, including numeric ties."""

    if stored.shape != regenerated.shape or len(canary_texts) != len(stored):
        raise NativeBuildError("same-space rank-one inputs are inconsistent")
    equivalent_indices: dict[str, set[int]] = defaultdict(set)
    for index, text in enumerate(canary_texts):
        equivalent_indices[text].add(index)
    count = 0
    for index, query in enumerate(regenerated):
        if metric == "l2":
            scores = np.sum((stored - query) ** 2, axis=1)
            best_score = float(np.min(scores))
        elif metric == "inner_product":
            scores = stored @ query
            best_score = float(np.max(scores))
        else:
            raise NativeBuildError(f"unsupported same-space canary metric: {metric}")
        best_indices = np.flatnonzero(
            np.isclose(scores, best_score, rtol=1e-6, atol=1e-6)
        )
        if any(
            int(candidate) in equivalent_indices[canary_texts[index]]
            for candidate in best_indices
        ):
            count += 1
    return count


def _write_lineage_addressed_snapshot(
    index: Any,
    path: Path,
    *,
    snapshot_id: str,
) -> SnapshotDescriptor:
    """Write FAISS plus a benign lineage footer so identical rebuilds stay unique.

    FAISS readers stop after the serialized index.  The authenticated footer
    makes a rebuilt artifact publication-lineage-addressed without perturbing a
    single vector, logical ID, or search result.
    """

    base = index.write(path)
    trailer = _SNAPSHOT_LINEAGE_TRAILER + bytes.fromhex(snapshot_id)
    with path.open("ab") as stream:
        stream.write(trailer)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = SnapshotDescriptor(
        sha256=_sha256_file(path),
        size_bytes=path.stat().st_size,
        dimension=base.dimension,
        metric=base.metric,
        ntotal=base.ntotal,
    )
    reopened = load_index(path, descriptor)
    if reopened.physical_ids != index.physical_ids:
        raise NativeBuildError("lineage-addressed snapshot changed physical IDs")
    return descriptor


def _validate_planned_vector_payload(plan: NativeBuildPlan) -> None:
    vectors = np.ascontiguousarray(plan.vectors_by_physical_id, dtype=np.float32)
    if vectors.shape != (len(plan.chunks), plan.profile.dimension):
        raise NativeBuildError("planned vector payload shape differs from the build plan")
    actual = hashlib.sha256(
        vectors.astype("<f4", copy=False).tobytes(order="C")
    ).hexdigest()
    if actual != plan.vector_payload_sha256:
        raise NativeBuildError("planned vector payload hash differs from the build plan")


def _descriptor_for_existing_snapshot(
    snapshot_path: Path,
    plan: NativeBuildPlan,
) -> SnapshotDescriptor:
    if snapshot_path.is_symlink() or not snapshot_path.is_file():
        raise NativeBuildError("lineage-addressed snapshot path is not a regular file")
    descriptor = SnapshotDescriptor(
        sha256=_sha256_file(snapshot_path),
        size_bytes=snapshot_path.stat().st_size,
        dimension=plan.profile.dimension,
        metric=plan.profile.metric,
        ntotal=len(plan.chunks),
    )
    _validate_snapshot_vector_payload(snapshot_path, descriptor, plan)
    return descriptor


def _validate_snapshot_vector_payload(
    snapshot_path: Path,
    descriptor: SnapshotDescriptor,
    plan: NativeBuildPlan,
) -> None:
    index = load_index(snapshot_path, descriptor)
    vectors = np.ascontiguousarray(
        index.reconstruct(range(1, descriptor.ntotal + 1)),
        dtype=np.float32,
    )
    actual = hashlib.sha256(
        vectors.astype("<f4", copy=False).tobytes(order="C")
    ).hexdigest()
    if actual != plan.vector_payload_sha256:
        raise NativeBuildError(
            "lineage-addressed snapshot vector payload differs from the build plan"
        )


def _read_catalog_sources(
    selection: RuntimeSelection,
) -> tuple[dict[str, str], dict[str, int]]:
    connection = _open_read_only_catalog(selection.paths.catalog)
    try:
        active_reports: dict[str, str] = {}
        for row in connection.execute(
            "SELECT canonical_relative_path, report_uid FROM active_reports"
        ):
            path = str(row[0])
            report_uid = str(row[1])
            previous = active_reports.setdefault(path, report_uid)
            if previous != report_uid:
                raise NativeBuildError(
                    "active snapshot contains multiple report objects for one source path"
                )
        existing = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT report_uid, report_id FROM reports")
        }
        return active_reports, existing
    finally:
        connection.close()


def _read_reusable_snapshot(
    selection: RuntimeSelection,
    reusable_report_uids: set[str],
) -> _ReusableSnapshot:
    if not selection.active_snapshot_id:
        raise NativeBuildError("incremental base snapshot is unavailable")
    connection = _open_read_only_catalog(selection.paths.catalog)
    connection.row_factory = sqlite3.Row
    try:
        descriptor_row = connection.execute(
            """
            SELECT snapshot.relative_path, snapshot.file_sha256,
                   snapshot.size_bytes, snapshot.dimension, snapshot.metric,
                   snapshot.ntotal, profile.profile_id, profile.profile_hash,
                   profile.model, profile.normalization,
                   profile.prefix_template, profile.extractor,
                   profile.parent_policy_json, profile.child_policy_json
            FROM vector_snapshots AS snapshot
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
            WHERE snapshot.snapshot_id = ?
            """,
            (selection.active_snapshot_id,),
        ).fetchone()
        if descriptor_row is None:
            raise NativeBuildError("incremental base descriptor is missing")
        try:
            parent_policy = json.loads(str(descriptor_row["parent_policy_json"]))
            child_policy = json.loads(str(descriptor_row["child_policy_json"]))
        except json.JSONDecodeError as exc:
            raise NativeBuildError("incremental base profile policy is invalid") from exc
        profile = EmbeddingProfile(
            model=str(descriptor_row["model"]),
            dimension=int(descriptor_row["dimension"]),
            metric=str(descriptor_row["metric"]),
            normalization=("l2" if int(descriptor_row["normalization"]) else "none"),
            prefix_template=str(descriptor_row["prefix_template"]),
            extractor=str(descriptor_row["extractor"]),
            parent_policy=parent_policy,
            child_policy=child_policy,
        )
        if (
            str(descriptor_row["profile_id"]) != profile.profile_hash
            or str(descriptor_row["profile_hash"]) != profile.profile_hash
        ):
            raise NativeBuildError("incremental base profile identity is inconsistent")
        rows = connection.execute(
            """
            SELECT report.report_uid, parent.parent_uid, parent.profile_id,
                   parent.parent_order, parent.content, parent.content_sha256,
                   chunk.chunk_uid, chunk.child_order, chunk.span_start,
                   chunk.span_end, chunk.embedding_text_sha256,
                   membership.faiss_id
            FROM snapshot_membership AS membership
            JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent
              ON parent.parent_uid = chunk.parent_uid
             AND parent.profile_id = chunk.profile_id
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE membership.snapshot_id = ?
            ORDER BY membership.faiss_id
            """,
            (selection.active_snapshot_id,),
        ).fetchall()
    finally:
        connection.close()

    selected = [row for row in rows if str(row["report_uid"]) in reusable_report_uids]
    seen_report_uids = {str(row["report_uid"]) for row in selected}
    if seen_report_uids != reusable_report_uids:
        raise NativeBuildError("unchanged active reports are not fully reusable")
    parent_by_uid: dict[str, CandidateParent] = {}
    chunks: list[CandidateChunk] = []
    physical_ids: list[int] = []
    for row in selected:
        parent_uid = str(row["parent_uid"])
        parent = CandidateParent(
            parent_uid=parent_uid,
            report_uid=str(row["report_uid"]),
            profile_id=str(row["profile_id"]),
            parent_order=int(row["parent_order"]),
            content=str(row["content"]),
            content_sha256=str(row["content_sha256"]),
        )
        existing_parent = parent_by_uid.setdefault(parent_uid, parent)
        if existing_parent != parent:
            raise NativeBuildError("reusable parent identity is inconsistent")
        physical_id = int(row["faiss_id"])
        physical_ids.append(physical_id)
        chunks.append(
            CandidateChunk(
                chunk_uid=str(row["chunk_uid"]),
                parent_uid=parent_uid,
                profile_id=str(row["profile_id"]),
                child_order=int(row["child_order"]),
                span_start=int(row["span_start"]),
                span_end=int(row["span_end"]),
                embedding_text_sha256=str(row["embedding_text_sha256"]),
                physical_id=physical_id,
            )
        )

    descriptor = SnapshotDescriptor(
        sha256=str(descriptor_row["file_sha256"]),
        size_bytes=int(descriptor_row["size_bytes"]),
        dimension=int(descriptor_row["dimension"]),
        metric=str(descriptor_row["metric"]),
        ntotal=int(descriptor_row["ntotal"]),
    )
    snapshot_path = selection.paths.data_root.joinpath(
        *str(descriptor_row["relative_path"]).split("/")
    )
    reused_vectors = load_index(snapshot_path, descriptor).reconstruct(physical_ids)
    vectors_by_chunk_uid: dict[str, np.ndarray] = {}
    for chunk, vector in zip(chunks, reused_vectors, strict=True):
        immutable = np.asarray(vector, dtype=np.float32).copy()
        immutable.setflags(write=False)
        vectors_by_chunk_uid[chunk.chunk_uid] = immutable
    return _ReusableSnapshot(
        profile=profile,
        parents=tuple(parent_by_uid.values()),
        chunks=tuple(chunks),
        vectors_by_chunk_uid=vectors_by_chunk_uid,
    )


def _validate_incremental_profile(
    profile: EmbeddingProfile,
    *,
    model: str,
    extractor_name: str,
    fallback_extractor_name: str | None,
    allow_extraction_fallback: bool,
    parent_chunk_size: int,
    child_chunk_size: int,
    use_parent_child: bool,
    single_chunk_size: int | None,
    metric: str,
    normalization: str,
    prefix_template: str,
) -> None:
    if (
        profile.model != model
        or profile.metric != metric
        or profile.normalization != normalization
        or profile.prefix_template != prefix_template
    ):
        raise NativeBuildError(
            "incremental writer configuration differs from the active embedding profile"
        )
    configured_profile = format_extraction_profile(
        extractor_name,
        allow_fallback=allow_extraction_fallback,
        fallback_engine=fallback_extractor_name,
        allow_custom=True,
    )
    accepted_extractors = {
        configured_profile,
        format_legacy_import_extraction_profile(
            extractor_name,
            allow_fallback=allow_extraction_fallback,
            fallback_engine=fallback_extractor_name,
            allow_custom=True,
        ),
    }
    if profile.extractor not in accepted_extractors:
        raise NativeBuildError(
            "incremental extractor differs from the active embedding profile: "
            f"active={profile.extractor}, requested={configured_profile}; "
            "an extraction-policy change requires a full-corpus successor"
        )

    def policy_size(policy: Mapping[str, Any]) -> int | None:
        value = policy.get("chunk_size", policy.get("size"))
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    if use_parent_child:
        expected_parent_size = parent_chunk_size
        expected_child_size = child_chunk_size
    else:
        expected_parent_size = single_chunk_size
        expected_child_size = None
    if policy_size(profile.parent_policy) != expected_parent_size:
        raise NativeBuildError(
            "incremental parent chunk policy differs from the active profile"
        )
    child_size = policy_size(profile.child_policy)
    if use_parent_child and child_size != expected_child_size:
        raise NativeBuildError(
            "incremental child chunk policy differs from the active profile"
        )
    if not use_parent_child and profile.child_policy.get("algorithm") != "identity-span-v1":
        raise NativeBuildError(
            "incremental single-level policy differs from the active profile"
        )


def _build_candidate_manifest(
    reports: tuple[CandidateReport, ...],
    *,
    active_reports_by_path: Mapping[str, str],
    deleted_paths: set[str],
) -> CorpusManifest:
    """Partition current and retired active report objects with stable reasons."""

    included_by_path = {
        report.canonical_relative_path: report.report_uid for report in reports
    }
    included_uids = set(included_by_path.values())
    decisions = [ManifestDecision.included(report.report_uid) for report in reports]
    for path, report_uid in sorted(active_reports_by_path.items()):
        if report_uid in included_uids:
            continue
        if path in deleted_paths:
            reason = _SOURCE_DELETED
        elif path in included_by_path:
            reason = _SOURCE_SUPERSEDED
        else:
            raise NativeBuildError(
                "active source is absent from the candidate without an exclusion decision"
            )
        decisions.append(ManifestDecision.excluded(report_uid, reason))

    return CorpusManifest.build(
        [decision.report_uid for decision in decisions],
        decisions,
        ExclusionPolicy(
            version=_EXCLUSION_POLICY_VERSION,
            excluded_reason_codes=frozenset({_SOURCE_DELETED, _SOURCE_SUPERSEDED}),
        ),
    )


def _discover_current_sources(
    source_root: Path,
    *,
    canonical_source_prefix: str,
    active_paths: set[str],
    deleted_paths: set[str],
) -> tuple[tuple[str, Path], ...]:
    prefix = normalize_relative_path(canonical_source_prefix)
    discovered: dict[str, Path] = {}
    for path in sorted(source_root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.name.lower().endswith(".pdf"):
            continue
        if path.is_symlink() or not path.is_file():
            raise NativeBuildError(f"source PDF must be a real local file: {path.name}")
        canonical = normalize_relative_path(f"{prefix}/{path.name}")
        if canonical in deleted_paths:
            continue
        if canonical in discovered:
            raise NativeBuildError(f"duplicate canonical source path: {canonical}")
        discovered[canonical] = path
    missing = sorted(active_paths - set(discovered) - deleted_paths)
    if missing:
        raise NativeBuildError(
            "active source is missing without an explicit deletion decision: " + missing[0]
        )
    unknown_deletions = deleted_paths - active_paths
    if unknown_deletions:
        raise NativeBuildError(
            "explicit deletion does not name an active source: "
            + sorted(unknown_deletions)[0]
        )
    return tuple(sorted(discovered.items()))


def _capture_source_inventory(
    source_root: Path,
    *,
    canonical_source_prefix: str,
    ignored_paths: set[str],
) -> tuple[SourceFileSnapshot, ...]:
    inventory: list[SourceFileSnapshot] = []
    for path in sorted(source_root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.name.lower().endswith(".pdf"):
            continue
        if path.is_symlink() or not path.is_file():
            raise NativeBuildError(f"source PDF must be a real local file: {path.name}")
        canonical = normalize_relative_path(
            f"{canonical_source_prefix}/{path.name}"
        )
        if canonical in ignored_paths:
            continue
        inventory.append(
            SourceFileSnapshot(
                canonical_relative_path=canonical,
                sha256=_sha256_file(path),
            )
        )
    return tuple(inventory)


def _validate_source_inventory(
    source_root: Path,
    *,
    canonical_source_prefix: str,
    ignored_paths: set[str],
    expected: tuple[SourceFileSnapshot, ...],
) -> None:
    try:
        current = _capture_source_inventory(
            source_root,
            canonical_source_prefix=canonical_source_prefix,
            ignored_paths=ignored_paths,
        )
    except (OSError, NativeBuildError) as exc:
        raise NativeBuildError("source corpus cannot be revalidated") from exc
    if current != expected:
        raise NativeBuildError(
            "source corpus membership or bytes changed while the candidate was built"
        )


def _validate_plan_source_inventory(plan: NativeBuildPlan) -> None:
    _validate_source_inventory(
        plan.source_root,
        canonical_source_prefix=plan.canonical_source_prefix,
        ignored_paths=set(plan.deleted_relative_paths),
        expected=plan.source_inventory,
    )


def _validate_candidate_source_inventory(result: CandidateResult) -> None:
    _validate_source_inventory(
        result.source_root,
        canonical_source_prefix=result.canonical_source_prefix,
        ignored_paths=set(result.deleted_relative_paths),
        expected=result.source_inventory,
    )


def _default_metadata_parser(file_name: str) -> Mapping[str, Any] | None:
    from src.core.db_manager import parse_filename

    return parse_filename(file_name)


def _default_extractor(
    path: Path,
    engine: str,
    *,
    allow_fallback: bool = True,
    fallback_engine: str | None = _DEFAULT_FALLBACK_ENGINE,
) -> Any:
    """Extract with the explicit policy; ``None`` retains legacy PyMuPDF fallback."""

    from src.core.pdf_extraction import extract_pdf_text

    return extract_pdf_text(
        str(path),
        engine,
        clean=True,
        allow_fallback=allow_fallback,
        fallback_engine=fallback_engine,
    )


def _canonical_extractor_name(engine: str, *, allow_custom: bool) -> str:
    from src.core.pdf_extraction import normalize_engine

    try:
        return normalize_engine(engine)
    except (AttributeError, TypeError, ValueError) as exc:
        if not allow_custom:
            raise NativeBuildError(f"invalid extraction engine: {engine}") from exc
        normalized = str(engine or "").strip().lower()
        if not normalized:
            raise NativeBuildError("custom extraction engine name cannot be empty") from exc
        if "|" in normalized:
            raise NativeBuildError(
                "custom extraction engine name cannot contain policy delimiters"
            ) from exc
        return normalized


def _canonical_fallback_extractor_name(
    engine: str | None,
    *,
    requested_engine: str,
    allow_custom: bool,
) -> str | None:
    raw = _DEFAULT_FALLBACK_ENGINE if engine is None else str(engine or "").strip()
    if not raw:
        return None
    normalized = _canonical_extractor_name(raw, allow_custom=allow_custom)
    return None if normalized == requested_engine else normalized


def format_extraction_profile(
    engine: str,
    *,
    allow_fallback: bool = True,
    fallback_engine: str | None = _DEFAULT_FALLBACK_ENGINE,
    allow_custom: bool = False,
) -> str:
    """Return the canonical fingerprint for a declared extraction policy."""

    primary = _canonical_extractor_name(engine, allow_custom=allow_custom)
    fallback_raw = str(fallback_engine or "").strip()
    if not allow_fallback or not fallback_raw:
        return primary
    fallback = _canonical_extractor_name(fallback_raw, allow_custom=allow_custom)
    if fallback == primary:
        raise NativeBuildError("extraction fallback must differ from primary")
    return f"{primary}|fallback={fallback}"


def format_legacy_import_extraction_profile(
    engine: str,
    *,
    allow_fallback: bool,
    fallback_engine: str | None,
    allow_custom: bool = False,
) -> str:
    """Fingerprint the exact policy permitted for post-migration writes."""

    configured = format_extraction_profile(
        engine,
        allow_fallback=allow_fallback,
        fallback_engine=fallback_engine,
        allow_custom=allow_custom,
    )
    return f"legacy-v1-import|configured={configured}|unattested"


def parse_extraction_profile(
    profile: str,
    *,
    allow_custom: bool,
) -> tuple[str, str | None]:
    """Parse and canonicalize one persisted non-migration extraction policy."""

    raw = str(profile or "").strip().lower()
    primary_raw, separator, fallback_raw = raw.partition("|fallback=")
    if not primary_raw or "|" in primary_raw:
        raise NativeBuildError("extractor profile has an unsupported policy")
    if not separator and "|" in raw:
        raise NativeBuildError("extractor profile has an unsupported policy")
    primary = _canonical_extractor_name(primary_raw, allow_custom=allow_custom)
    if not separator:
        return primary, None
    if not fallback_raw or "|" in fallback_raw:
        raise NativeBuildError("extractor profile has an unsupported fallback policy")
    fallback = _canonical_extractor_name(fallback_raw, allow_custom=allow_custom)
    if fallback == primary:
        raise NativeBuildError("extraction fallback must differ from primary")
    return primary, fallback


def _extract_source(
    path: Path,
    expected_engine: str,
    extractor: ExtractorPort,
    *,
    allow_fallback: bool = True,
    fallback_engine: str | None = _DEFAULT_FALLBACK_ENGINE,
) -> str:
    try:
        result = extractor(path, expected_engine)
    except Exception as exc:
        raise NativeSourceExtractionError(
            f"source extraction failed for {path.name}: {exc}"
        ) from exc
    if isinstance(result, str):
        text = result
        used_engine = expected_engine
    else:
        text = getattr(result, "text", None)
        used_engine = getattr(result, "used_engine", None)
        if not isinstance(used_engine, str) or not used_engine.strip():
            raise NativeBuildError(
                f"structured extractor result must report the engine used for {path.name}"
            )
    allowed_engines = {expected_engine}
    if allow_fallback and fallback_engine and expected_engine != fallback_engine:
        allowed_engines.add(f"{fallback_engine}-fallback")
    if used_engine not in allowed_engines:
        raise NativeBuildError(
            f"extractor used an undeclared engine for {path.name}: {used_engine}"
        )
    if not isinstance(text, str) or not text.strip():
        raise NativeSourceExtractionError(
            f"source extraction produced empty text: {path.name}"
        )
    return text


def _normalize_metadata(metadata: Mapping[str, Any], file_name: str) -> dict[str, Any]:
    required = ("report_type", "report_date", "title", "broker")
    result: dict[str, Any] = {}
    for field in required:
        value = metadata.get(field)
        if not isinstance(value, str) or not value.strip():
            raise NativeBuildError(f"source metadata {field} is missing: {file_name}")
        result[field] = value.strip()
    target = metadata.get("target_name")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        target = None
    result["target_name"] = target.strip() if isinstance(target, str) else None
    return result


def _split_full_corpus(
    extracted: list[tuple[dict[str, Any], str]],
    *,
    parent_chunk_size: int,
    child_chunk_size: int,
    use_parent_child: bool,
    single_chunk_size: int | None,
    prefix_template: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")],
        strip_headers=False,
    )
    parent_splitter = RecursiveCharacterTextSplitter(
        chunk_size=parent_chunk_size,
        chunk_overlap=int(parent_chunk_size * 0.1),
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    child_splitter = RecursiveCharacterTextSplitter(
        chunk_size=child_chunk_size,
        chunk_overlap=int(child_chunk_size * 0.1),
        separators=["\n\n", "\n", ". ", " ", ""],
        add_start_index=True,
    )
    single_splitter = (
        None
        if use_parent_child
        else RecursiveCharacterTextSplitter(
            chunk_size=single_chunk_size,
            chunk_overlap=int(single_chunk_size * 0.1),
            separators=["\n\n", "\n", ". ", " ", ""],
        )
    )
    parents: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    embedding_texts: list[str] = []
    for report, text in sorted(extracted, key=lambda item: item[0]["report_uid"]):
        header_documents = header_splitter.split_text(text)
        parent_documents = (
            parent_splitter.split_documents(header_documents)
            if use_parent_child
            else single_splitter.split_documents(header_documents)
        )
        if not parent_documents:
            raise NativeBuildError(f"source produced no parent chunks: {report['file_name']}")
        prefix = render_embedding_prefix(prefix_template, dict(report))
        for parent_order, parent_document in enumerate(parent_documents):
            content = parent_document.page_content
            if not content:
                raise NativeBuildError("parent chunk content cannot be empty")
            parents.append(
                {
                    "report_uid": report["report_uid"],
                    "parent_order": parent_order,
                    "content": content,
                    "content_sha256": sha256_text(content),
                }
            )
            if use_parent_child:
                child_documents = child_splitter.split_documents([parent_document])
                if not child_documents:
                    raise NativeBuildError("included parent produced no child chunks")
                child_spans = [
                    (
                        child_document.page_content,
                        child_document.metadata.get("start_index"),
                    )
                    for child_document in child_documents
                ]
            else:
                child_spans = [(content, 0)]
            previous_start = -1
            for child_order, (body, start) in enumerate(child_spans):
                if not isinstance(start, int) or start <= previous_start:
                    raise NativeBuildError("child splitter did not produce increasing spans")
                end = start + len(body)
                if content[start:end] != body:
                    raise NativeBuildError("child splitter span does not reproduce its body")
                previous_start = start
                embedding_text = prefix + body
                embedding_texts.append(embedding_text)
                chunks.append(
                    {
                        "report_uid": report["report_uid"],
                        "parent_order": parent_order,
                        "child_order": child_order,
                        "span_start": start,
                        "span_end": end,
                        "embedding_text_sha256": sha256_text(embedding_text),
                    }
                )
    if not chunks:
        raise NativeBuildError("full-corpus build produced no child chunks")
    return parents, chunks, embedding_texts


def _validate_plan_membership(
    reports: tuple[CandidateReport, ...],
    parents: list[CandidateParent],
    chunks: tuple[CandidateChunk, ...],
    manifest: CorpusManifest,
) -> None:
    if len({report.report_uid for report in reports}) != len(reports):
        raise NativeBuildError("candidate report identity is not unique")
    if len({parent.parent_uid for parent in parents}) != len(parents):
        raise NativeBuildError("candidate parent identity is not unique")
    if len({chunk.chunk_uid for chunk in chunks}) != len(chunks):
        raise NativeBuildError("candidate chunk identity is not unique")
    if sorted(chunk.physical_id for chunk in chunks) != list(range(1, len(chunks) + 1)):
        raise NativeBuildError("candidate physical IDs are not dense 1..N")
    parent_to_report = {parent.parent_uid: parent.report_uid for parent in parents}
    counts: dict[str, int] = defaultdict(int)
    for chunk in chunks:
        try:
            counts[parent_to_report[chunk.parent_uid]] += 1
        except KeyError as exc:
            raise NativeBuildError("candidate chunk references an unknown parent") from exc
    manifest.validate_snapshot_membership(counts)


def _assert_base_runtime(connection: sqlite3.Connection, plan: NativeBuildPlan) -> None:
    row = connection.execute(
        """
        SELECT active_snapshot_id, publication_generation, write_epoch,
               degraded, write_enabled, v1_fallback_open
        FROM retrieval_runtime WHERE runtime_id = 1
        """
    ).fetchone()
    if row is None:
        raise NativeBuildError("native runtime singleton is missing")
    if (
        row[0] != plan.base_snapshot_id
        or int(row[1]) != plan.base_publication_generation
        or int(row[2]) != plan.base_write_epoch
    ):
        raise NativeBuildError("active runtime changed while the candidate was built")
    degraded = bool(row[3])
    write_enabled = bool(row[4])
    fallback_open = bool(row[5])
    if plan.forward_recovery:
        if (
            plan.base_write_epoch <= 0
            or not degraded
            or write_enabled
            or fallback_open
        ):
            raise NativeBuildError("native runtime no longer permits forward recovery")
    elif degraded or (plan.base_write_epoch > 0 and not write_enabled):
        raise NativeBuildError("native runtime no longer permits publication")


def _insert_profile(connection: sqlite3.Connection, profile: EmbeddingProfile) -> None:
    value = profile.to_dict()
    connection.execute(
        """
        INSERT OR IGNORE INTO embedding_profiles (
            profile_id, profile_hash, model, dimension, metric, normalization,
            prefix_template, extractor, parent_policy_json, child_policy_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile.profile_hash,
            profile.profile_hash,
            profile.model,
            profile.dimension,
            profile.metric,
            int(profile.normalization == "l2"),
            profile.prefix_template,
            profile.extractor,
            canonical_json(value["parent_policy"]),
            canonical_json(value["child_policy"]),
        ),
    )
    row = connection.execute(
        """
        SELECT profile_hash, model, dimension, metric, normalization,
               prefix_template, extractor, parent_policy_json, child_policy_json
        FROM embedding_profiles WHERE profile_id = ?
        """,
        (profile.profile_hash,),
    ).fetchone()
    expected = (
        profile.profile_hash,
        profile.model,
        profile.dimension,
        profile.metric,
        int(profile.normalization == "l2"),
        profile.prefix_template,
        profile.extractor,
        canonical_json(value["parent_policy"]),
        canonical_json(value["child_policy"]),
    )
    if row is None or tuple(row) != expected:
        raise NativeBuildError("existing embedding profile conflicts with candidate")


def _insert_reports(
    connection: sqlite3.Connection,
    reports: tuple[CandidateReport, ...],
) -> dict[str, int]:
    existing = {
        str(row[0]): int(row[1])
        for row in connection.execute("SELECT report_uid, report_id FROM reports")
    }
    next_id = int(connection.execute("SELECT COALESCE(MAX(report_id), 0) FROM reports").fetchone()[0])
    report_ids: dict[str, int] = {}
    for report in sorted(reports, key=lambda item: bytes.fromhex(item.report_uid)):
        report_id = existing.get(report.report_uid)
        if report_id is None:
            next_id += 1
            report_id = next_id
            connection.execute(
                """
                INSERT INTO reports (
                    report_id, report_uid, canonical_relative_path, source_sha256,
                    retrieval_metadata_sha256, report_type, report_date,
                    target_name, title, broker
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    report.report_uid,
                    report.canonical_relative_path,
                    report.source_sha256,
                    report.retrieval_metadata_sha256,
                    report.report_type,
                    report.report_date,
                    report.target_name,
                    report.title,
                    report.broker,
                ),
            )
        row = connection.execute(
            """
            SELECT canonical_relative_path, source_sha256,
                   retrieval_metadata_sha256, report_type, report_date,
                   target_name, title, broker
            FROM reports WHERE report_id = ? AND report_uid = ?
            """,
            (report_id, report.report_uid),
        ).fetchone()
        expected = (
            report.canonical_relative_path,
            report.source_sha256,
            report.retrieval_metadata_sha256,
            report.report_type,
            report.report_date,
            report.target_name,
            report.title,
            report.broker,
        )
        if row is None or tuple(row) != expected:
            raise NativeBuildError("existing report source conflicts with candidate")
        report_ids[report.report_uid] = report_id
    return report_ids


def _insert_build(connection: sqlite3.Connection, plan: NativeBuildPlan) -> None:
    connection.execute(
        """
        INSERT INTO retrieval_builds (
            build_id, profile_id, source_manifest_json,
            source_manifest_sha256, included_count, excluded_count,
            expected_count, exclusion_policy_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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


def _insert_parents_and_chunks(
    connection: sqlite3.Connection,
    plan: NativeBuildPlan,
    report_ids: dict[str, int],
) -> None:
    for parent in plan.parents:
        connection.execute(
            """
            INSERT OR IGNORE INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order,
                content, content_sha256
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                parent.parent_uid,
                report_ids[parent.report_uid],
                parent.profile_id,
                parent.parent_order,
                parent.content,
                parent.content_sha256,
            ),
        )
        row = connection.execute(
            """
            SELECT report_id, profile_id, parent_order, content, content_sha256
            FROM retrieval_parents WHERE parent_uid = ?
            """,
            (parent.parent_uid,),
        ).fetchone()
        expected = (
            report_ids[parent.report_uid],
            parent.profile_id,
            parent.parent_order,
            parent.content,
            parent.content_sha256,
        )
        if row is None or tuple(row) != expected:
            raise NativeBuildError("existing parent conflicts with candidate")
    for chunk in plan.chunks:
        connection.execute(
            """
            INSERT OR IGNORE INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order,
                span_start, span_end, embedding_text_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.chunk_uid,
                chunk.parent_uid,
                chunk.profile_id,
                chunk.child_order,
                chunk.span_start,
                chunk.span_end,
                chunk.embedding_text_sha256,
            ),
        )
        row = connection.execute(
            """
            SELECT parent_uid, profile_id, child_order, span_start,
                   span_end, embedding_text_sha256
            FROM retrieval_chunks WHERE chunk_uid = ?
            """,
            (chunk.chunk_uid,),
        ).fetchone()
        expected = (
            chunk.parent_uid,
            chunk.profile_id,
            chunk.child_order,
            chunk.span_start,
            chunk.span_end,
            chunk.embedding_text_sha256,
        )
        if row is None or tuple(row) != expected:
            raise NativeBuildError("existing chunk conflicts with candidate")


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


def _validate_catalog_candidate(
    connection: sqlite3.Connection,
    plan: NativeBuildPlan,
    descriptor: SnapshotDescriptor,
) -> None:
    counts = connection.execute(
        """
        SELECT COUNT(*), COUNT(DISTINCT chunk_uid), COUNT(DISTINCT faiss_id),
               MIN(faiss_id), MAX(faiss_id)
        FROM snapshot_membership WHERE snapshot_id = ?
        """,
        (plan.snapshot_id,),
    ).fetchone()
    expected = len(plan.chunks)
    if counts is None or tuple(counts) != (expected, expected, expected, 1, expected):
        raise NativeBuildError("candidate catalog membership is not exact dense N")
    if descriptor.ntotal != expected:
        raise NativeBuildError("candidate raw snapshot ntotal differs from membership")
    report_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            """
            SELECT report.report_uid, COUNT(*)
            FROM snapshot_membership AS membership
            JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE membership.snapshot_id = ?
            GROUP BY report.report_uid
            """,
            (plan.snapshot_id,),
        )
    }
    plan.manifest.validate_snapshot_membership(report_counts)
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise NativeBuildError("candidate catalog foreign-key validation failed")


def _write_candidate_evidence(
    plan: NativeBuildPlan,
    descriptor: SnapshotDescriptor,
    root: Path,
) -> tuple[str, str]:
    evidence_relative = f"retrieval/v2/evidence/{plan.publication_id}/manifest.json"
    evidence_path = root.joinpath(*evidence_relative.split("/"))
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "kind": (
            "native_incremental_candidate"
            if plan.build_mode == "incremental"
            else "native_full_corpus_candidate"
        ),
        "build_mode": plan.build_mode,
        "publication_id": plan.publication_id,
        "base_publication_generation": plan.base_publication_generation,
        "base_snapshot_id": plan.base_snapshot_id,
        "base_write_epoch": plan.base_write_epoch,
        "build_id": plan.build_id,
        "snapshot_id": plan.snapshot_id,
        "profile_hash": plan.profile.profile_hash,
        "source_manifest_sha256": plan.manifest.sha256,
        "vector_payload_sha256": plan.vector_payload_sha256,
        "snapshot_file_sha256": descriptor.sha256,
        "snapshot_size_bytes": descriptor.size_bytes,
        "counts": {
            "reports": len(plan.reports),
            "parents": len(plan.parents),
            "chunks": len(plan.chunks),
            "ntotal": descriptor.ntotal,
        },
        "deleted_relative_paths": list(plan.deleted_relative_paths),
        "forward_recovery": plan.forward_recovery,
        "same_space_canary": (
            None
            if plan.same_space_canary is None
            else {
                "sample_count": plan.same_space_canary.sample_count,
                "dimension": plan.same_space_canary.dimension,
                "minimum_cosine_similarity": plan.same_space_canary.minimum_cosine_similarity,
                "maximum_norm_relative_error": plan.same_space_canary.maximum_norm_relative_error,
                "self_rank_one_count": plan.same_space_canary.self_rank_one_count,
            }
        ),
    }
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    if evidence_path.exists():
        if evidence_path.read_bytes() != encoded:
            raise NativeBuildError("candidate evidence path contains different bytes")
    else:
        with evidence_path.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        evidence_path.chmod(stat.S_IREAD)
    return evidence_relative, hashlib.sha256(encoded).hexdigest()


def _completed_candidate_result(
    plan: NativeBuildPlan,
    root: Path,
    catalog: Path,
) -> CandidateResult | None:
    connection = _open_read_only_catalog(catalog)
    try:
        row = connection.execute(
            """
            SELECT build.state, snapshot.state, snapshot.relative_path,
                   snapshot.file_sha256, snapshot.size_bytes,
                   snapshot.dimension, snapshot.metric, snapshot.ntotal
            FROM retrieval_builds AS build
            LEFT JOIN vector_snapshots AS snapshot ON snapshot.build_id = build.build_id
            WHERE build.build_id = ? AND snapshot.snapshot_id = ?
            """,
            (plan.build_id, plan.snapshot_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    if row[0] not in {"ready", "committed_pending_checkpoint", "fully_complete"} or row[1] != "ready":
        raise NativeBuildError("deterministic candidate already exists in an incomplete state")
    descriptor = SnapshotDescriptor(
        sha256=row[3],
        size_bytes=int(row[4]),
        dimension=int(row[5]),
        metric=row[6],
        ntotal=int(row[7]),
    )
    snapshot_path = root.joinpath(*str(row[2]).split("/"))
    _validate_snapshot_vector_payload(snapshot_path, descriptor, plan)
    evidence_relative, evidence_sha256 = _write_candidate_evidence(plan, descriptor, root)
    return CandidateResult(
        build_id=plan.build_id,
        snapshot_id=plan.snapshot_id,
        publication_id=plan.publication_id,
        snapshot_relative_path=str(row[2]),
        evidence_manifest_relative_path=evidence_relative,
        evidence_manifest_sha256=evidence_sha256,
        descriptor=descriptor,
        report_count=len(plan.reports),
        parent_count=len(plan.parents),
        chunk_count=len(plan.chunks),
        source_root=plan.source_root,
        canonical_source_prefix=plan.canonical_source_prefix,
        deleted_relative_paths=plan.deleted_relative_paths,
        source_inventory=plan.source_inventory,
    )


def _mark_candidate_failed(connection: sqlite3.Connection, plan: NativeBuildPlan) -> None:
    try:
        row = connection.execute(
            "SELECT state FROM vector_snapshots WHERE snapshot_id = ?",
            (plan.snapshot_id,),
        ).fetchone()
        if row is not None and row[0] in {"staged", "validating", "ready"}:
            _transition_snapshot(connection, plan.snapshot_id, "failed")
        row = connection.execute(
            "SELECT state FROM retrieval_builds WHERE build_id = ?",
            (plan.build_id,),
        ).fetchone()
        if row is not None and row[0] in {
            "planned",
            "cataloging",
            "vector_building",
            "validating",
            "ready",
        }:
            _transition_build(connection, plan.build_id, "failed")
        connection.commit()
    except sqlite3.Error:
        connection.rollback()


def _open_read_only_catalog(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).resolve(strict=True)
    uri = f"file:{quote(resolved.as_posix(), safe=':/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5.0)
    try:
        configure_catalog_storage(connection)
        connection.execute('PRAGMA query_only = ON')
        return connection
    except SchemaError as exc:
        connection.close()
        raise NativeBuildError('native catalog storage mode is invalid') from exc


def _positive_size(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise NativeBuildError(f"{name} must be a positive integer")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "CandidateResult",
    "NativeBuildError",
    "NativeSourceExtractionError",
    "NativeBuildPlan",
    "SourceFileSnapshot",
    "execute_full_corpus_successor",
    "execute_incremental_update",
    "materialize_candidate",
    "prepare_full_corpus_build",
    "prepare_incremental_build",
    "publish_candidate",
    "validate_epoch_zero_same_space_canary",
]
