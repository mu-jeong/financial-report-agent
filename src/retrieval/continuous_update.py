'''Continuous native retrieval updates backed by transient immutable deltas.

The complete snapshot remains the durable publication unit.  During one update
job, small delta segments make successfully processed reports searchable.  A
single final compaction reuses the visible base/delta vectors and delegates the
durable pointer transition to the existing publication coordinator.
'''

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import numpy as np

from src.retrieval.bootstrap import RuntimeSelection
from src.retrieval.build_service import (
    CandidateChunk,
    CandidateParent,
    CandidateReport,
    CandidateResult,
    EmbeddingsPort,
    ExtractorPort,
    MetadataParser,
    NativeBuildError,
    NativeBuildPlan,
    SourceFileSnapshot,
    _EXCLUSION_POLICY_VERSION,
    _SOURCE_DELETED,
    _SOURCE_EXTRACTION_FAILED,
    _SOURCE_SUPERSEDED,
    _ReusableSnapshot,
    _canonical_extractor_name,
    _canonical_fallback_extractor_name,
    _capture_source_inventory,
    _default_extractor,
    _default_metadata_parser,
    _discover_current_sources,
    _extract_source_records,
    _finalize_native_build_plan,
    _insert_reports,
    _normalize_metadata,
    _positive_size,
    _read_catalog_sources,
    _split_full_corpus,
    _validate_incremental_profile,
    _validate_active_space_canary,
    _validate_source_inventory,
    materialize_candidate,
    prepare_full_corpus_build,
    publish_candidate,
)
from src.retrieval.delta_schema import delta_schema_installed, install_delta_schema
from src.retrieval.garbage_collector import RetrievalGarbageCollector
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
from src.retrieval.manifest import CorpusManifest, ExclusionPolicy, ManifestDecision
from src.retrieval.publication import (
    PublicationOutcome,
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


DEFAULT_CONTINUOUS_REPORT_BATCH_SIZE = 20


@dataclass(frozen=True)
class DeltaReportAction:
    canonical_relative_path: str
    action: str
    report_uid: str | None
    reason_code: str | None = None


@dataclass(frozen=True)
class SelectedSourceSnapshot:
    canonical_relative_path: str
    physical_name: str
    sha256: str


@dataclass(frozen=True)
class DeltaSegmentPlan:
    base_snapshot_id: str
    base_publication_generation: int
    base_write_epoch: int
    profile: EmbeddingProfile
    sequence: int
    segment_id: str
    reports: tuple[CandidateReport, ...]
    parents: tuple[CandidateParent, ...]
    chunks: tuple[CandidateChunk, ...]
    vectors_by_physical_id: np.ndarray
    actions: tuple[DeltaReportAction, ...]
    source_root: Path
    canonical_source_prefix: str
    selected_sources: tuple[SelectedSourceSnapshot, ...]
    attempted_report_uids: tuple[str, ...]
    failed_report_uids: tuple[str, ...]
    deferred_report_count: int


@dataclass(frozen=True)
class DeltaPublicationResult:
    segment_id: str
    sequence: int
    descriptor: SnapshotDescriptor
    attempted_report_uids: tuple[str, ...]
    published_report_uids: tuple[str, ...]
    failed_report_uids: tuple[str, ...]
    deferred_report_count: int


@dataclass(frozen=True)
class ContinuousUpdateResult:
    delta_publications: tuple[DeltaPublicationResult, ...]
    candidate_result: CandidateResult
    publication_outcome: PublicationOutcome
    attempted_report_uids: tuple[str, ...]
    failed_report_uids: tuple[str, ...]


@dataclass(frozen=True)
class _ContinuousContext:
    selection: RuntimeSelection
    source_root: Path
    source_inventory: tuple[SourceFileSnapshot, ...]
    canonical_source_prefix: str
    deleted_relative_paths: tuple[str, ...]
    source_records: tuple[dict[str, Any], ...]
    reports_by_uid: Mapping[str, CandidateReport]
    initial_active_reports_by_path: Mapping[str, str]
    initial_active_report_objects_by_path: Mapping[str, CandidateReport]
    prior_extraction_failure_uids: frozenset[str]
    profile: EmbeddingProfile
    embeddings: EmbeddingsPort
    extractor_name: str
    extractor: ExtractorPort
    fallback_extractor_name: str | None
    allow_extraction_fallback: bool
    parent_chunk_size: int
    child_chunk_size: int
    use_parent_child: bool
    single_chunk_size: int | None
    normalization: str
    prefix_template: str


def execute_continuous_update(
    data_root: str | Path,
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
    extractor: ExtractorPort | None = None,
    metadata_parser: MetadataParser | None = None,
    metric: str = 'l2',
    normalization: str = 'none',
    prefix_template: str = '[Company: {target_name}, Title: {title}]\n',
    canonical_source_prefix: str = 'downloaded',
    deleted_relative_paths: Iterable[str] = (),
    allow_extraction_fallback: bool = True,
    retry_extraction_failures: bool = False,
    batch_size: int = DEFAULT_CONTINUOUS_REPORT_BATCH_SIZE,
    progress_callback: Callable[[DeltaPublicationResult], None] | None = None,
) -> ContinuousUpdateResult | None:
    '''Publish ready reports in sparse batches, then compact exactly once.'''

    root = Path(data_root).resolve(strict=True)
    _positive_size(batch_size, 'batch_size')
    with NativeWriterLock(root) as writer_lease:
        StartupReconciler(root).reconcile(writer_lease=writer_lease)
        preflight = guard_before_retrieval_write(
            root,
            allow_degraded_forward_recovery=True,
            first_successor_writer_lease=writer_lease,
        )
        if preflight.write_epoch == 0 or preflight.degraded:
            # Greenfield publication and degraded forward recovery retain the
            # established complete-successor boundary.  Sparse publication is
            # only valid once the native runtime is writable and healthy.
            plan = prepare_full_corpus_build(
                root,
                source_directory,
                embeddings=embeddings,
                model=model,
                extractor_name=extractor_name,
                fallback_extractor_name=fallback_extractor_name,
                allow_extraction_fallback=allow_extraction_fallback,
                use_parent_child=use_parent_child,
                single_chunk_size=single_chunk_size,
                parent_chunk_size=parent_chunk_size,
                child_chunk_size=child_chunk_size,
                extractor=extractor,
                metadata_parser=metadata_parser,
                metric=metric,
                normalization=normalization,
                prefix_template=prefix_template,
                canonical_source_prefix=canonical_source_prefix,
                deleted_relative_paths=deleted_relative_paths,
                retry_extraction_failures=retry_extraction_failures,
                allow_degraded_forward_recovery=True,
                writer_lease=writer_lease,
            )
            if plan is None:
                return None
            candidate = materialize_candidate(
                plan,
                root,
                writer_lease=writer_lease,
            )
            outcome = publish_candidate(candidate, root, writer_lease=writer_lease)
            failed_report_uids = tuple(
                sorted(
                    entry.report_uid
                    for entry in plan.manifest.entries
                    if entry.reason_code == _SOURCE_EXTRACTION_FAILED
                )
            )
            return ContinuousUpdateResult(
                delta_publications=(),
                candidate_result=candidate,
                publication_outcome=outcome,
                attempted_report_uids=plan.attempted_report_uids,
                failed_report_uids=failed_report_uids,
            )
        _install_delta_extension(root, writer_lease)
        collector = RetrievalGarbageCollector(root)
        collector._reconcile_compacted_delta_artifacts_after_validation(
            writer_lease=writer_lease
        )
        context = _prepare_context(
            root,
            source_directory,
            embeddings=embeddings,
            model=model,
            extractor_name=extractor_name,
            fallback_extractor_name=fallback_extractor_name,
            allow_extraction_fallback=allow_extraction_fallback,
            use_parent_child=use_parent_child,
            single_chunk_size=single_chunk_size,
            parent_chunk_size=parent_chunk_size,
            child_chunk_size=child_chunk_size,
            extractor=extractor,
            metadata_parser=metadata_parser,
            metric=metric,
            normalization=normalization,
            prefix_template=prefix_template,
            canonical_source_prefix=canonical_source_prefix,
            deleted_relative_paths=deleted_relative_paths,
        )
        changed_records = [
            record
            for record in context.source_records
            if context.initial_active_reports_by_path.get(
                str(record['canonical_relative_path'])
            )
            != str(record['report_uid'])
        ]
        eligible_records = [
            record
            for record in changed_records
            if retry_extraction_failures
            or str(record['report_uid'])
            not in context.prior_extraction_failure_uids
        ]
        ready_delta_count = _ready_delta_count(context.selection)
        if (
            not eligible_records
            and not context.deleted_relative_paths
            and ready_delta_count == 0
        ):
            return None

        if eligible_records:
            _validate_active_space_canary(
                context.selection,
                embeddings,
                metric=metric,
                normalization=normalization,
            )

        publications: list[DeltaPublicationResult] = []
        attempted: set[str] = set()
        failed = {
            str(record['report_uid'])
            for record in changed_records
            if str(record['report_uid'])
            in context.prior_extraction_failure_uids
        }
        remaining = list(eligible_records)
        deletion_batch = context.deleted_relative_paths
        while remaining or deletion_batch:
            selected = remaining[:batch_size]
            del remaining[: len(selected)]
            plan = _prepare_delta_segment(
                context,
                selected,
                deletion_batch,
                deferred_report_count=len(remaining),
            )
            deletion_batch = ()
            result = materialize_and_activate_delta(
                plan,
                root,
                writer_lease=writer_lease,
            )
            publications.append(result)
            attempted.update(result.attempted_report_uids)
            failed.update(result.failed_report_uids)
            if progress_callback is not None:
                progress_callback(result)

        _validate_source_inventory(
            context.source_root,
            canonical_source_prefix=context.canonical_source_prefix,
            ignored_paths=set(context.deleted_relative_paths),
            expected=context.source_inventory,
        )
        compaction_plan = _prepare_compaction_plan(context, frozenset(failed))
        candidate = materialize_candidate(
            compaction_plan,
            root,
            writer_lease=writer_lease,
        )
        outcome = publish_candidate(candidate, root, writer_lease=writer_lease)
        return ContinuousUpdateResult(
            delta_publications=tuple(publications),
            candidate_result=candidate,
            publication_outcome=outcome,
            attempted_report_uids=tuple(sorted(attempted)),
            failed_report_uids=tuple(sorted(failed)),
        )


def materialize_and_activate_delta(
    plan: DeltaSegmentPlan,
    data_root: str | Path,
    *,
    writer_lease: WriterLease | None = None,
) -> DeltaPublicationResult:
    '''Publish one immutable segment and make all of its actions visible atomically.'''

    root = Path(data_root).resolve(strict=True)
    if writer_lease is None:
        with NativeWriterLock(root) as owned_lease:
            return materialize_and_activate_delta(
                plan,
                root,
                writer_lease=owned_lease,
            )
    assert_writer_lease_owned(writer_lease, root)
    _validate_selected_sources(
        plan.source_root,
        plan.canonical_source_prefix,
        plan.selected_sources,
    )

    descriptor = SnapshotDescriptor(
        sha256='',
        size_bytes=0,
        dimension=plan.profile.dimension,
        metric=plan.profile.metric,
        ntotal=0,
    )
    if plan.chunks:
        relative_path = f'retrieval/v2/deltas/{plan.segment_id}.faiss'
        final_path = root.joinpath(*relative_path.split('/'))
        staging = root / 'retrieval' / 'v2' / 'staging'
        staging.mkdir(parents=True, exist_ok=True)
        staged = staging / f'delta-{uuid.uuid4().hex[:12]}.faiss'
        descriptor = build_index(
            plan.vectors_by_physical_id,
            range(1, len(plan.chunks) + 1),
            plan.profile.metric,
        ).write(staged)
        try:
            try:
                publish_immutable_artifact(staged, final_path, descriptor)
            except FileExistsError:
                _validate_existing_artifact(final_path, descriptor)
        finally:
            if staged.exists():
                staged.unlink()
    else:
        relative_path = None

    catalog = root / 'retrieval' / 'v2' / 'catalog.sqlite3'
    connection = sqlite3.connect(catalog)
    try:
        configure_catalog_storage(connection, writable=True)
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('BEGIN IMMEDIATE')
        _assert_delta_base_runtime(connection, plan)
        _assert_next_sequence(connection, plan)
        report_ids = _insert_reports(connection, plan.reports)
        _insert_delta_parents_and_chunks(connection, plan, report_ids)
        connection.execute(
            '''
            INSERT INTO retrieval_delta_segments (
                segment_id, base_snapshot_id, base_publication_generation,
                sequence, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                plan.segment_id,
                plan.base_snapshot_id,
                plan.base_publication_generation,
                plan.sequence,
                relative_path,
                descriptor.sha256 or None,
                descriptor.size_bytes,
                descriptor.dimension,
                descriptor.metric,
                descriptor.ntotal,
            ),
        )
        connection.executemany(
            '''
            INSERT INTO retrieval_delta_reports (
                segment_id, canonical_relative_path, action,
                report_uid, reason_code
            ) VALUES (?, ?, ?, ?, ?)
            ''',
            [
                (
                    plan.segment_id,
                    action.canonical_relative_path,
                    action.action,
                    action.report_uid,
                    action.reason_code,
                )
                for action in plan.actions
            ],
        )
        connection.executemany(
            '''
            INSERT INTO retrieval_delta_membership (segment_id, chunk_uid, faiss_id)
            VALUES (?, ?, ?)
            ''',
            [
                (plan.segment_id, chunk.chunk_uid, chunk.physical_id)
                for chunk in plan.chunks
            ],
        )
        connection.execute(
            '''
            UPDATE retrieval_delta_segments
            SET state = 'ready',
                state_changed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            WHERE segment_id = ?
            ''',
            (plan.segment_id,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return DeltaPublicationResult(
        segment_id=plan.segment_id,
        sequence=plan.sequence,
        descriptor=descriptor,
        attempted_report_uids=plan.attempted_report_uids,
        published_report_uids=tuple(
            sorted(
                action.report_uid
                for action in plan.actions
                if action.action == 'upsert' and action.report_uid is not None
            )
        ),
        failed_report_uids=plan.failed_report_uids,
        deferred_report_count=plan.deferred_report_count,
    )


def _prepare_context(
    data_root: Path,
    source_directory: str | Path,
    *,
    embeddings: EmbeddingsPort,
    model: str,
    extractor_name: str,
    fallback_extractor_name: str | None,
    allow_extraction_fallback: bool,
    use_parent_child: bool,
    single_chunk_size: int | None,
    parent_chunk_size: int,
    child_chunk_size: int,
    extractor: ExtractorPort | None,
    metadata_parser: MetadataParser | None,
    metric: str,
    normalization: str,
    prefix_template: str,
    canonical_source_prefix: str,
    deleted_relative_paths: Iterable[str],
) -> _ContinuousContext:
    selection = guard_before_retrieval_write(
        data_root,
        allow_degraded_forward_recovery=True,
    )
    if not selection.is_native or not selection.active_snapshot_id:
        raise NativeBuildError('continuous updates require an active native snapshot')
    source_root = Path(source_directory).resolve(strict=True)
    if not source_root.is_dir() or source_root.is_symlink():
        raise NativeBuildError('source directory must be a real local directory')
    _positive_size(parent_chunk_size, 'parent_chunk_size')
    _positive_size(child_chunk_size, 'child_chunk_size')
    if child_chunk_size > parent_chunk_size:
        raise NativeBuildError('child chunks cannot be larger than parent chunks')
    if not use_parent_child:
        _positive_size(single_chunk_size, 'single_chunk_size')
    deleted = tuple(
        sorted({normalize_relative_path(value) for value in deleted_relative_paths})
    )
    (
        active_reports_by_path,
        active_report_objects_by_path,
        existing_reports,
        prior_failures,
    ) = _read_catalog_sources(selection)
    normalized_prefix = normalize_relative_path(canonical_source_prefix)
    discovered = _discover_current_sources(
        source_root,
        canonical_source_prefix=normalized_prefix,
        active_paths=set(active_reports_by_path),
        deleted_paths=set(deleted),
    )
    source_inventory = _capture_source_inventory(
        source_root,
        canonical_source_prefix=normalized_prefix,
        ignored_paths=set(deleted),
    )
    if {path for path, _ in discovered} != {
        item.canonical_relative_path for item in source_inventory
    }:
        raise NativeBuildError('source corpus changed during discovery')
    source_hashes = {
        item.canonical_relative_path: item.sha256 for item in source_inventory
    }
    parser = metadata_parser or _default_metadata_parser
    records: list[dict[str, Any]] = []
    for canonical_path, path in discovered:
        metadata = parser(path.name)
        if not isinstance(metadata, Mapping):
            raise NativeBuildError(f'source metadata cannot be parsed: {path.name}')
        normalized = _normalize_metadata(metadata, path.name)
        metadata_payload = {
            'broker': normalized['broker'],
            'report_date': normalized['report_date'],
            'report_type': normalized['report_type'],
            'target_name': normalized['target_name'],
            'title': normalized['title'],
        }
        metadata_sha256 = sha256_text(canonical_json(metadata_payload))
        source_sha256 = source_hashes[canonical_path]
        report_uid = compute_report_uid(
            canonical_path,
            source_sha256,
            metadata_sha256,
        )
        records.append(
            {
                **normalized,
                'file_name': path.name,
                'path': path,
                'canonical_relative_path': canonical_path,
                'source_sha256': source_sha256,
                'retrieval_metadata_sha256': metadata_sha256,
                'report_uid': report_uid,
                'existing_report_id': existing_reports.get(report_uid),
            }
        )
    if not records:
        raise NativeBuildError('continuous update discovered no source reports')
    reports = {
        str(record['report_uid']): CandidateReport(
            report_uid=str(record['report_uid']),
            canonical_relative_path=str(record['canonical_relative_path']),
            source_sha256=str(record['source_sha256']),
            retrieval_metadata_sha256=str(record['retrieval_metadata_sha256']),
            report_type=str(record['report_type']),
            report_date=str(record['report_date']),
            target_name=(
                None
                if record['target_name'] is None
                else str(record['target_name'])
            ),
            title=str(record['title']),
            broker=str(record['broker']),
            file_name=str(record['file_name']),
            existing_report_id=record['existing_report_id'],
        )
        for record in records
    }
    canonical_extractor = _canonical_extractor_name(
        extractor_name,
        allow_custom=extractor is not None,
    )
    canonical_fallback = _canonical_fallback_extractor_name(
        fallback_extractor_name,
        requested_engine=canonical_extractor,
        allow_custom=extractor is not None,
    )
    effective_fallback = allow_extraction_fallback and canonical_fallback is not None
    extractor_port = extractor or (
        lambda path, engine: _default_extractor(
            path,
            engine,
            allow_fallback=effective_fallback,
            fallback_engine=canonical_fallback,
        )
    )
    profile = _read_active_profile(selection)
    _validate_incremental_profile(
        profile,
        model=model,
        extractor_name=canonical_extractor,
        fallback_extractor_name=canonical_fallback,
        allow_extraction_fallback=effective_fallback,
        parent_chunk_size=parent_chunk_size,
        child_chunk_size=child_chunk_size,
        use_parent_child=use_parent_child,
        single_chunk_size=single_chunk_size,
        metric=metric,
        normalization=normalization,
        prefix_template=prefix_template,
    )
    return _ContinuousContext(
        selection=selection,
        source_root=source_root,
        source_inventory=source_inventory,
        canonical_source_prefix=normalized_prefix,
        deleted_relative_paths=deleted,
        source_records=tuple(records),
        reports_by_uid=reports,
        initial_active_reports_by_path=active_reports_by_path,
        initial_active_report_objects_by_path=active_report_objects_by_path,
        prior_extraction_failure_uids=prior_failures,
        profile=profile,
        embeddings=embeddings,
        extractor_name=canonical_extractor,
        extractor=extractor_port,
        fallback_extractor_name=canonical_fallback,
        allow_extraction_fallback=effective_fallback,
        parent_chunk_size=parent_chunk_size,
        child_chunk_size=child_chunk_size,
        use_parent_child=use_parent_child,
        single_chunk_size=single_chunk_size,
        normalization=normalization,
        prefix_template=prefix_template,
    )


def _prepare_delta_segment(
    context: _ContinuousContext,
    selected_records: Sequence[dict[str, Any]],
    deleted_relative_paths: Sequence[str],
    *,
    deferred_report_count: int,
) -> DeltaSegmentPlan:
    selected_sources = tuple(
        SelectedSourceSnapshot(
            canonical_relative_path=str(record['canonical_relative_path']),
            physical_name=Path(record['path']).name,
            sha256=str(record['source_sha256']),
        )
        for record in selected_records
    )
    _validate_selected_sources(
        context.source_root,
        context.canonical_source_prefix,
        selected_sources,
    )
    extracted, failed_uids = _extract_source_records(
        selected_records,
        extractor_name=context.extractor_name,
        extractor=context.extractor,
        allow_extraction_fallback=context.allow_extraction_fallback,
        fallback_extractor_name=context.fallback_extractor_name,
    )
    if extracted:
        provisional_parents, provisional_chunks, embedding_texts = _split_full_corpus(
            extracted,
            parent_chunk_size=context.parent_chunk_size,
            child_chunk_size=context.child_chunk_size,
            use_parent_child=context.use_parent_child,
            single_chunk_size=context.single_chunk_size,
            prefix_template=context.prefix_template,
        )
    else:
        provisional_parents, provisional_chunks, embedding_texts = [], [], []
    if embedding_texts:
        try:
            vectors = np.asarray(
                context.embeddings.embed_documents(embedding_texts),
                dtype=np.float32,
            )
        except Exception as exc:
            raise NativeBuildError(f'continuous delta embedding failed: {exc}') from exc
        expected_shape = (len(provisional_chunks), context.profile.dimension)
        if vectors.shape != expected_shape:
            raise NativeBuildError(
                'continuous delta embedding does not match the active vector space'
            )
        if not np.isfinite(vectors).all():
            raise NativeBuildError('continuous delta embedding contains a non-finite vector')
        if context.normalization == 'l2':
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(norms == 0):
                raise NativeBuildError('zero vector cannot be L2-normalized')
            vectors = np.asarray(vectors / norms, dtype=np.float32)
    else:
        vectors = np.empty((0, context.profile.dimension), dtype=np.float32)

    profile_id = context.profile.profile_hash
    parents: list[CandidateParent] = []
    parent_uid_by_key: dict[tuple[str, int], str] = {}
    for parent in provisional_parents:
        parent_uid = compute_parent_uid(
            profile_id,
            str(parent['report_uid']),
            int(parent['parent_order']),
            str(parent['content_sha256']),
        )
        parent_uid_by_key[(str(parent['report_uid']), int(parent['parent_order']))] = (
            parent_uid
        )
        parents.append(
            CandidateParent(
                parent_uid=parent_uid,
                report_uid=str(parent['report_uid']),
                profile_id=profile_id,
                parent_order=int(parent['parent_order']),
                content=str(parent['content']),
                content_sha256=str(parent['content_sha256']),
            )
        )
    provisional_uids: list[str] = []
    for chunk in provisional_chunks:
        parent_uid = parent_uid_by_key[
            (str(chunk['report_uid']), int(chunk['parent_order']))
        ]
        provisional_uids.append(
            compute_chunk_uid(
                profile_id,
                parent_uid,
                int(chunk['child_order']),
                int(chunk['span_start']),
                int(chunk['span_end']),
                str(chunk['embedding_text_sha256']),
            )
        )
    physical_ids = assign_physical_ids(provisional_uids)
    chunks = tuple(
        CandidateChunk(
            chunk_uid=chunk_uid,
            parent_uid=parent_uid_by_key[
                (str(chunk['report_uid']), int(chunk['parent_order']))
            ],
            profile_id=profile_id,
            child_order=int(chunk['child_order']),
            span_start=int(chunk['span_start']),
            span_end=int(chunk['span_end']),
            embedding_text_sha256=str(chunk['embedding_text_sha256']),
            physical_id=physical_ids[chunk_uid],
        )
        for chunk, chunk_uid in zip(provisional_chunks, provisional_uids, strict=True)
    )
    if chunks:
        vector_order = np.argsort(
            np.asarray([chunk.physical_id for chunk in chunks], dtype=np.int64),
            kind='stable',
        )
        ordered_vectors = np.ascontiguousarray(vectors[vector_order], dtype=np.float32)
    else:
        ordered_vectors = np.empty((0, context.profile.dimension), dtype=np.float32)
    ordered_vectors.setflags(write=False)

    successful_uids = {str(record['report_uid']) for record, _ in extracted}
    attempted_uids = tuple(
        sorted(str(record['report_uid']) for record in selected_records)
    )
    actions = [
        DeltaReportAction(
            canonical_relative_path=str(record['canonical_relative_path']),
            action='upsert',
            report_uid=str(record['report_uid']),
        )
        for record in selected_records
        if str(record['report_uid']) in successful_uids
    ]
    actions.extend(
        DeltaReportAction(
            canonical_relative_path=str(record['canonical_relative_path']),
            action='failed',
            report_uid=str(record['report_uid']),
            reason_code=_SOURCE_EXTRACTION_FAILED,
        )
        for record in selected_records
        if str(record['report_uid']) in failed_uids
    )
    actions.extend(
        DeltaReportAction(path, 'delete', None)
        for path in deleted_relative_paths
    )
    actions.sort(key=lambda action: action.canonical_relative_path)
    if not actions:
        raise NativeBuildError('delta batch has no terminal report actions')
    sequence = _next_delta_sequence(context.selection)
    vector_payload_sha256 = hashlib.sha256(
        ordered_vectors.astype('<f4', copy=False).tobytes(order='C')
    ).hexdigest()
    segment_id = canonical_hash(
        'retrieval-delta-segment',
        context.selection.publication_generation,
        context.selection.active_snapshot_id,
        sequence,
        [
            {
                'action': action.action,
                'path': action.canonical_relative_path,
                'reason_code': action.reason_code,
                'report_uid': action.report_uid,
            }
            for action in actions
        ],
        [chunk.chunk_uid for chunk in sorted(chunks, key=lambda item: item.physical_id)],
        vector_payload_sha256,
    )
    reports = tuple(
        sorted(
            (
                context.reports_by_uid[str(record['report_uid'])]
                for record in selected_records
            ),
            key=lambda report: bytes.fromhex(report.report_uid),
        )
    )
    return DeltaSegmentPlan(
        base_snapshot_id=str(context.selection.active_snapshot_id),
        base_publication_generation=context.selection.publication_generation,
        base_write_epoch=context.selection.write_epoch,
        profile=context.profile,
        sequence=sequence,
        segment_id=segment_id,
        reports=reports,
        parents=tuple(parents),
        chunks=chunks,
        vectors_by_physical_id=ordered_vectors,
        actions=tuple(actions),
        source_root=context.source_root,
        canonical_source_prefix=context.canonical_source_prefix,
        selected_sources=selected_sources,
        attempted_report_uids=attempted_uids,
        failed_report_uids=tuple(sorted(failed_uids)),
        deferred_report_count=deferred_report_count,
    )


def _prepare_compaction_plan(
    context: _ContinuousContext,
    failed_report_uids: frozenset[str],
) -> NativeBuildPlan:
    (
        active_reports_by_path,
        active_report_objects_by_path,
        _existing_reports,
        durable_failure_uids,
    ) = _read_catalog_sources(context.selection)
    active_report_uids = set(active_reports_by_path.values())
    reusable = _read_reusable_composite(
        context.selection,
        active_report_uids,
        context.profile,
    )
    if not reusable.chunks:
        raise NativeBuildError('continuous compaction cannot publish an empty corpus')

    physical_ids = assign_physical_ids(chunk.chunk_uid for chunk in reusable.chunks)
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
        for chunk in reusable.chunks
    )
    ordered_chunks = sorted(chunks, key=lambda chunk: chunk.physical_id)
    vectors = np.ascontiguousarray(
        np.vstack(
            [reusable.vectors_by_chunk_uid[chunk.chunk_uid] for chunk in ordered_chunks]
        ),
        dtype=np.float32,
    )
    vectors.setflags(write=False)

    current_by_uid = dict(context.reports_by_uid)
    active_by_uid = {
        report.report_uid: report
        for report in active_report_objects_by_path.values()
    }
    initial_by_uid = {
        report.report_uid: report
        for report in context.initial_active_report_objects_by_path.values()
    }
    reports_by_uid = {**current_by_uid, **initial_by_uid, **active_by_uid}
    decisions: dict[str, ManifestDecision] = {
        report_uid: ManifestDecision.included(report_uid)
        for report_uid in active_report_uids
    }
    known_failures = set(failed_report_uids).union(durable_failure_uids)
    for record in context.source_records:
        report_uid = str(record['report_uid'])
        path = str(record['canonical_relative_path'])
        if active_reports_by_path.get(path) == report_uid:
            continue
        if report_uid not in known_failures:
            raise NativeBuildError(
                'source report is neither visible nor durably failed before compaction'
            )
        decisions[report_uid] = ManifestDecision.excluded(
            report_uid,
            _SOURCE_EXTRACTION_FAILED,
        )
    for path, report_uid in context.initial_active_reports_by_path.items():
        if report_uid in decisions:
            continue
        reason = _SOURCE_DELETED if path in context.deleted_relative_paths else _SOURCE_SUPERSEDED
        decisions[report_uid] = ManifestDecision.excluded(report_uid, reason)
    missing_reports = set(decisions).difference(reports_by_uid)
    if missing_reports:
        raise NativeBuildError(
            'compaction manifest references an unavailable report object'
        )
    manifest = CorpusManifest.build(
        decisions,
        decisions.values(),
        ExclusionPolicy(
            version=_EXCLUSION_POLICY_VERSION,
            excluded_reason_codes=frozenset(
                {
                    _SOURCE_DELETED,
                    _SOURCE_EXTRACTION_FAILED,
                    _SOURCE_SUPERSEDED,
                }
            ),
        ),
    )
    plan_reports = tuple(
        sorted(
            (reports_by_uid[report_uid] for report_uid in decisions),
            key=lambda report: bytes.fromhex(report.report_uid),
        )
    )
    return _finalize_native_build_plan(
        selection=context.selection,
        profile=context.profile,
        reports=plan_reports,
        parents=reusable.parents,
        chunks=chunks,
        manifest=manifest,
        vectors_by_physical_id=vectors,
        deleted_relative_paths=context.deleted_relative_paths,
        source_root=context.source_root,
        canonical_source_prefix=context.canonical_source_prefix,
        source_inventory=context.source_inventory,
        same_space_canary=None,
        build_mode='incremental',
        attempted_report_uids=(),
        deferred_report_count=0,
    )


def _read_reusable_composite(
    selection: RuntimeSelection,
    reusable_report_uids: set[str],
    profile: EmbeddingProfile,
) -> _ReusableSnapshot:
    connection = _open_read_only_catalog(selection.paths.catalog)
    connection.row_factory = sqlite3.Row
    try:
        if not delta_schema_installed(connection):
            raise NativeBuildError('delta schema is unavailable during compaction')
        rows = connection.execute(
            '''
            SELECT membership.artifact_id, membership.artifact_kind,
                   membership.sequence, membership.faiss_id,
                   report.report_uid, parent.parent_uid, parent.profile_id,
                   parent.parent_order, parent.content, parent.content_sha256,
                   chunk.chunk_uid, chunk.child_order, chunk.span_start,
                   chunk.span_end, chunk.embedding_text_sha256
            FROM active_vector_membership AS membership
            JOIN retrieval_chunks AS chunk
              ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent
              ON parent.parent_uid = chunk.parent_uid
             AND parent.profile_id = chunk.profile_id
            JOIN reports AS report ON report.report_id = parent.report_id
            ORDER BY membership.sequence, membership.artifact_id,
                     membership.faiss_id
            '''
        ).fetchall()
        descriptors = _read_active_artifact_descriptors(connection, selection)
    finally:
        connection.close()
    selected = [row for row in rows if str(row['report_uid']) in reusable_report_uids]
    if {str(row['report_uid']) for row in selected} != reusable_report_uids:
        raise NativeBuildError('active composite reports are not fully reusable')
    parent_by_uid: dict[str, CandidateParent] = {}
    chunks: list[CandidateChunk] = []
    vectors_by_chunk_uid: dict[str, np.ndarray] = {}
    rows_by_artifact: dict[str, list[sqlite3.Row]] = {}
    for row in selected:
        rows_by_artifact.setdefault(str(row['artifact_id']), []).append(row)
    for artifact_id, artifact_rows in rows_by_artifact.items():
        try:
            path, descriptor = descriptors[artifact_id]
        except KeyError as exc:
            raise NativeBuildError('active composite artifact descriptor is missing') from exc
        local_ids = [int(row['faiss_id']) for row in artifact_rows]
        artifact_vectors = load_index(path, descriptor).reconstruct(local_ids)
        for row, vector in zip(artifact_rows, artifact_vectors, strict=True):
            if str(row['profile_id']) != profile.profile_hash:
                raise NativeBuildError('active composite profile is inconsistent')
            parent = CandidateParent(
                parent_uid=str(row['parent_uid']),
                report_uid=str(row['report_uid']),
                profile_id=str(row['profile_id']),
                parent_order=int(row['parent_order']),
                content=str(row['content']),
                content_sha256=str(row['content_sha256']),
            )
            previous = parent_by_uid.setdefault(parent.parent_uid, parent)
            if previous != parent:
                raise NativeBuildError('active composite parent identity is inconsistent')
            chunk = CandidateChunk(
                chunk_uid=str(row['chunk_uid']),
                parent_uid=parent.parent_uid,
                profile_id=str(row['profile_id']),
                child_order=int(row['child_order']),
                span_start=int(row['span_start']),
                span_end=int(row['span_end']),
                embedding_text_sha256=str(row['embedding_text_sha256']),
                physical_id=int(row['faiss_id']),
            )
            chunks.append(chunk)
            immutable = np.asarray(vector, dtype=np.float32).copy()
            immutable.setflags(write=False)
            vectors_by_chunk_uid[chunk.chunk_uid] = immutable
    return _ReusableSnapshot(
        profile=profile,
        parents=tuple(parent_by_uid.values()),
        chunks=tuple(chunks),
        vectors_by_chunk_uid=vectors_by_chunk_uid,
    )


def _read_active_artifact_descriptors(
    connection: sqlite3.Connection,
    selection: RuntimeSelection,
) -> dict[str, tuple[Path, SnapshotDescriptor]]:
    row = connection.execute(
        '''
        SELECT snapshot_id, relative_path, file_sha256, size_bytes,
               dimension, metric, ntotal
        FROM vector_snapshots
        WHERE snapshot_id = ?
        ''',
        (selection.active_snapshot_id,),
    ).fetchone()
    if row is None:
        raise NativeBuildError('active base descriptor is unavailable')
    descriptors = {
        str(row['snapshot_id']): (
            _resolve_artifact_path(selection.paths.data_root, str(row['relative_path'])),
            SnapshotDescriptor(
                sha256=str(row['file_sha256']),
                size_bytes=int(row['size_bytes']),
                dimension=int(row['dimension']),
                metric=str(row['metric']),
                ntotal=int(row['ntotal']),
            ),
        )
    }
    for delta in connection.execute(
        '''
        SELECT segment_id, relative_path, file_sha256, size_bytes,
               dimension, metric, ntotal
        FROM retrieval_delta_segments
        WHERE base_snapshot_id = ?
          AND base_publication_generation = ?
          AND state = 'ready'
          AND ntotal > 0
        ''',
        (selection.active_snapshot_id, selection.publication_generation),
    ):
        descriptors[str(delta['segment_id'])] = (
            _resolve_artifact_path(
                selection.paths.data_root,
                str(delta['relative_path']),
            ),
            SnapshotDescriptor(
                sha256=str(delta['file_sha256']),
                size_bytes=int(delta['size_bytes']),
                dimension=int(delta['dimension']),
                metric=str(delta['metric']),
                ntotal=int(delta['ntotal']),
            ),
        )
    return descriptors


def _read_active_profile(selection: RuntimeSelection) -> EmbeddingProfile:
    connection = _open_read_only_catalog(selection.paths.catalog)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            '''
            SELECT profile.profile_id, profile.profile_hash, profile.model,
                   profile.dimension, profile.metric, profile.normalization,
                   profile.prefix_template, profile.extractor,
                   profile.parent_policy_json, profile.child_policy_json
            FROM vector_snapshots AS snapshot
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
            WHERE snapshot.snapshot_id = ?
            ''',
            (selection.active_snapshot_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise NativeBuildError('active embedding profile is unavailable')
    try:
        profile = EmbeddingProfile(
            model=str(row['model']),
            dimension=int(row['dimension']),
            metric=str(row['metric']),
            normalization='l2' if int(row['normalization']) else 'none',
            prefix_template=str(row['prefix_template']),
            extractor=str(row['extractor']),
            parent_policy=json.loads(str(row['parent_policy_json'])),
            child_policy=json.loads(str(row['child_policy_json'])),
        )
    except json.JSONDecodeError as exc:
        raise NativeBuildError('active embedding profile policy is invalid') from exc
    if str(row['profile_id']) != profile.profile_hash or str(row['profile_hash']) != profile.profile_hash:
        raise NativeBuildError('active embedding profile identity is inconsistent')
    return profile


def _insert_delta_parents_and_chunks(
    connection: sqlite3.Connection,
    plan: DeltaSegmentPlan,
    report_ids: Mapping[str, int],
) -> None:
    for parent in plan.parents:
        connection.execute(
            '''
            INSERT OR IGNORE INTO retrieval_parents (
                parent_uid, report_id, profile_id, parent_order,
                content, content_sha256
            ) VALUES (?, ?, ?, ?, ?, ?)
            ''',
            (
                parent.parent_uid,
                report_ids[parent.report_uid],
                parent.profile_id,
                parent.parent_order,
                parent.content,
                parent.content_sha256,
            ),
        )
        actual = connection.execute(
            '''
            SELECT report_id, profile_id, parent_order, content, content_sha256
            FROM retrieval_parents WHERE parent_uid = ?
            ''',
            (parent.parent_uid,),
        ).fetchone()
        expected = (
            report_ids[parent.report_uid],
            parent.profile_id,
            parent.parent_order,
            parent.content,
            parent.content_sha256,
        )
        if actual is None or tuple(actual) != expected:
            raise NativeBuildError('existing parent conflicts with delta')
    for chunk in plan.chunks:
        connection.execute(
            '''
            INSERT OR IGNORE INTO retrieval_chunks (
                chunk_uid, parent_uid, profile_id, child_order,
                span_start, span_end, embedding_text_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
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
        actual = connection.execute(
            '''
            SELECT parent_uid, profile_id, child_order, span_start,
                   span_end, embedding_text_sha256
            FROM retrieval_chunks WHERE chunk_uid = ?
            ''',
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
        if actual is None or tuple(actual) != expected:
            raise NativeBuildError('existing chunk conflicts with delta')


def _install_delta_extension(root: Path, writer_lease: WriterLease) -> None:
    assert_writer_lease_owned(writer_lease, root)
    connection = sqlite3.connect(root / 'retrieval' / 'v2' / 'catalog.sqlite3')
    try:
        install_delta_schema(connection)
    except SchemaError as exc:
        raise NativeBuildError('retrieval delta schema could not be installed') from exc
    finally:
        connection.close()


def _assert_delta_base_runtime(
    connection: sqlite3.Connection,
    plan: DeltaSegmentPlan,
) -> None:
    row = connection.execute(
        '''
        SELECT active_snapshot_id, publication_generation, write_epoch,
               degraded, write_enabled
        FROM retrieval_runtime WHERE runtime_id = 1
        '''
    ).fetchone()
    if row is None:
        raise NativeBuildError('native runtime singleton is missing')
    if tuple(row[:3]) != (
        plan.base_snapshot_id,
        plan.base_publication_generation,
        plan.base_write_epoch,
    ):
        raise NativeBuildError('active runtime changed while the delta was built')
    if bool(row[3]) or not bool(row[4]):
        raise NativeBuildError('native runtime no longer permits delta publication')


def _assert_next_sequence(
    connection: sqlite3.Connection,
    plan: DeltaSegmentPlan,
) -> None:
    maximum = int(
        connection.execute(
            '''
            SELECT COALESCE(MAX(sequence), 0)
            FROM retrieval_delta_segments
            WHERE base_snapshot_id = ?
              AND base_publication_generation = ?
            ''',
            (plan.base_snapshot_id, plan.base_publication_generation),
        ).fetchone()[0]
    )
    if plan.sequence != maximum + 1:
        raise NativeBuildError('delta sequence changed while the segment was built')


def _next_delta_sequence(selection: RuntimeSelection) -> int:
    connection = _open_read_only_catalog(selection.paths.catalog)
    try:
        return int(
            connection.execute(
                '''
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM retrieval_delta_segments
                WHERE base_snapshot_id = ?
                  AND base_publication_generation = ?
                ''',
                (selection.active_snapshot_id, selection.publication_generation),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _ready_delta_count(selection: RuntimeSelection) -> int:
    connection = _open_read_only_catalog(selection.paths.catalog)
    try:
        if not delta_schema_installed(connection):
            return 0
        return int(
            connection.execute(
                '''
                SELECT count(*)
                FROM retrieval_delta_segments
                WHERE base_snapshot_id = ?
                  AND base_publication_generation = ?
                  AND state = 'ready'
                ''',
                (selection.active_snapshot_id, selection.publication_generation),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _validate_selected_sources(
    source_root: Path,
    canonical_source_prefix: str,
    selected_sources: Sequence[SelectedSourceSnapshot],
) -> None:
    prefix = PurePosixPath(canonical_source_prefix)
    for expected in selected_sources:
        canonical = PurePosixPath(expected.canonical_relative_path)
        try:
            relative = canonical.relative_to(prefix)
        except ValueError as exc:
            raise NativeBuildError('delta source path is outside the source prefix') from exc
        if len(relative.parts) != 1:
            raise NativeBuildError('delta source path is not a direct source file')
        if (
            not expected.physical_name
            or expected.physical_name in {'.', '..'}
            or '/' in expected.physical_name
            or '\\' in expected.physical_name
        ):
            raise NativeBuildError('delta physical source name is invalid')
        physical_canonical_path = normalize_relative_path(
            f'{canonical_source_prefix}/{expected.physical_name}'
        )
        if physical_canonical_path != expected.canonical_relative_path:
            raise NativeBuildError(
                'delta physical source does not match its canonical path'
            )
        path = source_root / expected.physical_name
        if path.is_symlink() or not path.is_file():
            raise NativeBuildError('delta source file is no longer available')
        if _sha256_file(path) != expected.sha256:
            raise NativeBuildError('delta source bytes changed before activation')


def _validate_existing_artifact(
    path: Path,
    descriptor: SnapshotDescriptor,
) -> None:
    if not path.is_file() or path.is_symlink():
        raise NativeBuildError('existing delta artifact is not a regular file')
    if path.stat().st_size != descriptor.size_bytes:
        raise NativeBuildError('existing delta artifact size conflicts with the plan')
    if _sha256_file(path) != descriptor.sha256:
        raise NativeBuildError('existing delta artifact hash conflicts with the plan')
    load_index(path, descriptor)


def _resolve_artifact_path(root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or '..' in relative.parts:
        raise NativeBuildError('artifact path is not a safe relative path')
    path = root.joinpath(*relative.parts).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise NativeBuildError('artifact path escapes the data root') from exc
    return path


def _open_read_only_catalog(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path.resolve(strict=True)), safe='/:\\')
    uri = f'file:{encoded}?mode=ro'
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.execute('PRAGMA query_only = ON')
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    'ContinuousUpdateResult',
    'DEFAULT_CONTINUOUS_REPORT_BATCH_SIZE',
    'DeltaPublicationResult',
    'DeltaReportAction',
    'DeltaSegmentPlan',
    'execute_continuous_update',
    'materialize_and_activate_delta',
]
