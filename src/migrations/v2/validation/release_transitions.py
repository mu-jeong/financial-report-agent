"""Release-grade copied-root transitions for the native V2 release validation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np

from src.migrations.v2.reconstruct import render_embedding_prefix
from src.retrieval.bootstrap import RuntimeSelection, inspect_runtime
from src.retrieval.build_service import (
    ExtractorPort,
    MetadataParser,
    NativeBuildError,
    parse_extraction_profile,
    prepare_full_corpus_build,
    materialize_candidate,
    publish_candidate,
)
from src.retrieval.identity import canonical_json, sha256_text
from src.retrieval.publication import PublicationError
from src.retrieval.recovery import RecoveryDisposition, StartupReconciler
from src.retrieval.repository import CatalogRepository
from src.migrations.v2.validation.installed_probe import run_probe
from src.retrieval.vector_index import SnapshotDescriptor, load_index
from src.retrieval.writer_lock import NativeWriterLock


class ReleaseTransitionError(RuntimeError):
    """Raised when a release transition cannot be proved end to end."""


class _ReplayEmbeddings:
    def __init__(self, hashes: tuple[str, ...], vectors: np.ndarray) -> None:
        if vectors.ndim != 2 or vectors.shape[0] != len(hashes):
            raise ReleaseTransitionError("replay vector topology is invalid")
        self._hashes = hashes
        self._vectors = np.ascontiguousarray(vectors, dtype=np.float32)
        self.calls = 0
        self.validated_text_counts: list[int] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        observed = tuple(sha256_text(text) for text in texts)
        if observed != self._hashes:
            raise ReleaseTransitionError(
                "replay embedding inputs differ from the protected successor topology"
            )
        self.calls += 1
        self.validated_text_counts.append(len(texts))
        return self._vectors.tolist()


def execute_release_transitions(
    data_root: str | Path,
    protected_root: str | Path,
    source_directory: str | Path,
    query_spec_path: str | Path,
    *,
    extractor: ExtractorPort | None = None,
    metadata_parser: MetadataParser | None = None,
) -> dict[str, Any]:
    """Exercise recovery, forward build, lease blocking, GC, and Gate D search.

    ``data_root`` is intentionally mutated and must be a dedicated copy.
    ``protected_root`` and the source/query inputs are hashed before and after.
    """

    root = _safe_directory(data_root, "dedicated data root")
    protected = _safe_directory(protected_root, "protected data root")
    sources = _safe_directory(source_directory, "source corpus")
    query_path = _safe_file(query_spec_path, "query specification")
    if _same_path(root, protected):
        raise ReleaseTransitionError("transition run requires a dedicated copied root")
    if _is_within(protected, root) or _is_within(root, protected):
        raise ReleaseTransitionError("dedicated and protected roots must not be nested")
    _assert_isolated_trees(root, protected)

    run_id = uuid.uuid4().hex
    started_at = _utc_now()
    events: list[dict[str, Any]] = []
    query_sha256 = _sha256_file(query_path)
    source_before = _tree_manifest(sources)

    protected_proof_before = _protected_proof(protected)
    initial_selection = _inspect(root)
    initial = _runtime(initial_selection)
    protected_runtime = protected_proof_before["runtime"]
    if not _healthy(initial) or initial["predecessor_snapshot_id"] is None:
        raise ReleaseTransitionError(
            "initial transition root must be healthy with a verified predecessor"
        )
    if initial["active_snapshot_id"] == initial["predecessor_snapshot_id"]:
        raise ReleaseTransitionError("active and predecessor snapshots must differ")
    if initial != protected_runtime:
        raise ReleaseTransitionError("dedicated copy runtime differs from protected root")

    initial_snapshots = {
        role: _snapshot_record(root, snapshot_id)
        for role, snapshot_id in (
            ("active", initial["active_snapshot_id"]),
            ("predecessor", initial["predecessor_snapshot_id"]),
        )
    }
    protected_snapshots = protected_proof_before["snapshots"]
    if {
        role: value["file_sha256"] for role, value in initial_snapshots.items()
    } != {
        role: value["file_sha256"] for role, value in protected_snapshots.items()
    }:
        raise ReleaseTransitionError("dedicated snapshot bytes differ from protected root")
    dedicated_catalog_before = _catalog_logical_sha256(_catalog(root))
    protected_catalog_before = protected_proof_before["catalog_logical_sha256"]
    if dedicated_catalog_before != protected_catalog_before:
        raise ReleaseTransitionError("dedicated catalog differs from protected root")
    protected_tree_before = protected_proof_before["tree"]
    _append_event(events, "initial_health_validated")

    replay, build_options = _load_replay_contract(
        root,
        initial_selection,
        allow_custom_extractor=extractor is not None,
    )
    corrupted_snapshot_id = str(initial["active_snapshot_id"])
    corrupted_path = _snapshot_path(root, corrupted_snapshot_id)
    corrupted_sha256_before = _sha256_file(corrupted_path)
    _flip_first_byte(corrupted_path)
    corrupted_sha256_after = _sha256_file(corrupted_path)
    if corrupted_sha256_after == corrupted_sha256_before:
        raise ReleaseTransitionError("active snapshot corruption was not observable")
    _append_event(events, "active_snapshot_corrupted")

    recovered = StartupReconciler(root).reconcile()
    replayed_recovery = StartupReconciler(root).reconcile()
    recovery_after = _runtime(_inspect(root))
    if (
        recovered.disposition is not RecoveryDisposition.PREDECESSOR_DEGRADED
        or replayed_recovery.disposition is not RecoveryDisposition.ACTIVE
        or recovery_after["active_snapshot_id"]
        != initial["predecessor_snapshot_id"]
        or recovery_after["predecessor_snapshot_id"] is not None
        or recovery_after["publication_generation"]
        != initial["publication_generation"] + 1
        or recovery_after["write_epoch"] != initial["write_epoch"]
        or recovery_after["v1_fallback_open"] is not False
        or recovery_after["degraded"] is not True
        or recovery_after["write_enabled"] is not False
        or _snapshot_record(root, corrupted_snapshot_id)["state"] != "failed"
    ):
        raise ReleaseTransitionError("active corruption did not recover to the predecessor")
    _append_event(events, "predecessor_recovery_completed")

    repository = CatalogRepository(_catalog(root), data_root=root)
    request_lease = repository.request()
    session = request_lease.__enter__()
    lease_open = True
    try:
        if session.revision.snapshot_id != recovery_after["active_snapshot_id"]:
            raise ReleaseTransitionError("degraded predecessor lease selected the wrong snapshot")
        leased_snapshot_id = session.revision.snapshot_id
        _append_event(events, "degraded_snapshot_leased")

        forward_result, forward_outcome = _build_candidate(
            root,
            sources,
            replay,
            build_options,
            publish=True,
            extractor=extractor,
            metadata_parser=metadata_parser,
        )
        forward_after = _runtime(_inspect(root))
        if (
            forward_after["active_snapshot_id"] != forward_result.snapshot_id
            or forward_after["predecessor_snapshot_id"] != leased_snapshot_id
            or forward_after["publication_generation"]
            != recovery_after["publication_generation"] + 1
            or forward_after["write_epoch"] != recovery_after["write_epoch"] + 1
            or not _healthy(forward_after)
            or forward_outcome.active_snapshot_id != forward_result.snapshot_id
        ):
            raise ReleaseTransitionError("forward recovery publication is invalid")
        forward_replay_calls = replay.calls
        _append_event(events, "forward_recovery_published")

        candidate_result, _unused = _build_candidate(
            root,
            sources,
            replay,
            build_options,
            publish=False,
            extractor=extractor,
            metadata_parser=metadata_parser,
        )
        candidate_state_before = _candidate_state(root, candidate_result)
        if candidate_state_before != {
            "build_state": "ready",
            "snapshot_state": "ready",
            "running_publications": 0,
        }:
            raise ReleaseTransitionError("lease-blocked successor candidate is not ready")
        _append_event(events, "next_candidate_materialized")

        blocked_error: PublicationError | None = None
        try:
            publish_candidate(candidate_result, root)
        except PublicationError as exc:
            blocked_error = exc
        if blocked_error is None or "leased" not in str(blocked_error).lower():
            raise ReleaseTransitionError("publication was not blocked by the retained lease")
        after_block = _runtime(_inspect(root))
        candidate_state_after = _candidate_state(root, candidate_result)
        if after_block != forward_after or candidate_state_after != candidate_state_before:
            raise ReleaseTransitionError(
                "blocked publication mutated runtime or candidate state: "
                f"runtime={after_block!r}/{forward_after!r}, "
                f"candidate={candidate_state_after!r}/{candidate_state_before!r}"
            )
        _append_event(events, "publication_blocked_while_leased")
    finally:
        if lease_open:
            request_lease.__exit__(None, None, None)
            lease_open = False
        repository.close()
    _append_event(events, "lease_released")

    final_outcome = publish_candidate(candidate_result, root)
    final = _runtime(_inspect(root))
    if (
        final["active_snapshot_id"] != candidate_result.snapshot_id
        or final["predecessor_snapshot_id"] != forward_after["active_snapshot_id"]
        or final["publication_generation"]
        != forward_after["publication_generation"] + 1
        or final["write_epoch"] != forward_after["write_epoch"] + 1
        or not _healthy(final)
        or final_outcome.active_snapshot_id != candidate_result.snapshot_id
    ):
        raise ReleaseTransitionError("post-lease successor publication is invalid")
    _append_event(events, "successor_published")

    retired = _snapshot_record(root, leased_snapshot_id)
    if retired["state"] != "garbage_collected" or retired["file_exists"] is not False:
        raise ReleaseTransitionError("retired predecessor was not garbage-collected")
    _append_event(events, "retired_snapshot_garbage_collected")

    query_value = _read_json(query_path)
    gate_probe = run_probe(
        root,
        query_value,
        samples=2,
        query_spec_sha256=query_sha256,
    )
    probe_runtime = gate_probe["runtime_identity"]
    gate = gate_probe["gate_d_search"]
    if (
        probe_runtime["active_snapshot_id"] != final["active_snapshot_id"]
        or probe_runtime["publication_generation"] != final["publication_generation"]
        or gate["top_rank"] != 1
        or gate["citation_complete"] is not True
    ):
        raise ReleaseTransitionError("Gate D search did not select the final successor")
    _append_event(events, "gate_d_search_validated")

    if _sha256_file(query_path) != query_sha256:
        raise ReleaseTransitionError("query specification changed during transitions")
    source_after = _tree_manifest(sources)
    protected_proof_after = _protected_proof(protected)
    protected_tree_after = protected_proof_after["tree"]
    protected_catalog_after = protected_proof_after["catalog_logical_sha256"]
    protected_unchanged = (
        source_after == source_before
        and protected_tree_after == protected_tree_before
        and protected_catalog_after == protected_catalog_before
        and protected_proof_after["runtime"] == protected_runtime
        and protected_proof_after["snapshots"] == protected_snapshots
    )
    if not protected_unchanged:
        raise ReleaseTransitionError("protected root or source corpus changed")
    _append_event(events, "protected_root_revalidated")

    return {
        "schema_version": 2,
        "kind": "v2_copied_install_release_transitions",
        "passed": True,
        "fixture_only": False,
        "run_id": run_id,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "dedicated_copy": True,
        "protected_root_unchanged": True,
        "copy_proof": {
            "dedicated_catalog_logical_sha256": dedicated_catalog_before,
            "protected_catalog_logical_sha256_before": protected_catalog_before,
            "protected_catalog_logical_sha256_after": protected_catalog_after,
            "protected_tree_sha256_before": protected_tree_before["sha256"],
            "protected_tree_sha256_after": protected_tree_after["sha256"],
            "source_tree_sha256_before": source_before["sha256"],
            "source_tree_sha256_after": source_after["sha256"],
            "query_spec_sha256": query_sha256,
            "initial_snapshot_sha256": {
                role: value["file_sha256"]
                for role, value in initial_snapshots.items()
            },
        },
        "event_sequence": events,
        "initial": initial,
        "recovery": {
            "before": initial,
            "after": recovery_after,
            "corrupted_snapshot_id": corrupted_snapshot_id,
            "corrupted_snapshot_sha256_before": corrupted_sha256_before,
            "corrupted_snapshot_sha256_after": corrupted_sha256_after,
            "recovery_disposition": recovered.disposition.value,
            "replay_disposition": replayed_recovery.disposition.value,
            "failed_snapshot_state": "failed",
        },
        "forward_recovery": {
            "before": recovery_after,
            "after": forward_after,
            "candidate_snapshot_id": forward_result.snapshot_id,
            "embedding": {
                "provider_calls": 0,
                "validated_replay_calls": forward_replay_calls,
                "validated_text_count": replay.validated_text_counts[0],
            },
        },
        "lease_gc": {
            "leased_snapshot_id": leased_snapshot_id,
            "lease_acquired_before_forward_publication": True,
            "blocked_candidate_snapshot_id": candidate_result.snapshot_id,
            "publication_blocked_while_leased": True,
            "blocked_error": type(blocked_error).__name__,
            "blocked_error_sha256": sha256_text(str(blocked_error)),
            "candidate_state_before": candidate_state_before,
            "candidate_state_after": candidate_state_after,
            "runtime_after_block": after_block,
            "lease_released": True,
            "retired_snapshot_id": leased_snapshot_id,
            "retired_snapshot_state": retired["state"],
            "retired_snapshot_deleted": True,
            "validated_replay_calls_total": replay.calls,
        },
        "gate_d_search": {
            "query_id": gate_probe["query_id"],
            "query_text_sha256": gate_probe["query_text_sha256"],
            "query_vector_sha256": gate_probe["query_vector_sha256"],
            "query_spec_sha256": gate_probe["query_spec_sha256"],
            "expected_report_uid": gate["expected_report_uid"],
            "top_report_uid": gate["top_report_uid"],
            "top_rank": gate["top_rank"],
            "citation_complete": gate["citation_complete"],
            "citation_sha256": gate["citation_sha256"],
            "query_generation": gate_probe["query_generation"],
            "snapshot_id": probe_runtime["active_snapshot_id"],
            "publication_generation": probe_runtime["publication_generation"],
        },
        "final": final,
    }


def _build_candidate(
    root: Path,
    sources: Path,
    embeddings: _ReplayEmbeddings,
    options: Mapping[str, Any],
    *,
    publish: bool,
    extractor: ExtractorPort | None,
    metadata_parser: MetadataParser | None,
) -> tuple[Any, Any | None]:
    with NativeWriterLock(root) as writer_lease:
        plan = prepare_full_corpus_build(
            root / "reports.db",
            sources,
            data_root=root,
            embeddings=embeddings,
            extractor=extractor,
            metadata_parser=metadata_parser,
            writer_lease=writer_lease,
            allow_degraded_forward_recovery=True,
            **dict(options),
        )
        result = materialize_candidate(plan, root, writer_lease=writer_lease)
        outcome = (
            publish_candidate(result, root, writer_lease=writer_lease)
            if publish
            else None
        )
        return result, outcome


def _load_replay_contract(
    root: Path,
    selection: RuntimeSelection,
    *,
    allow_custom_extractor: bool = False,
) -> tuple[_ReplayEmbeddings, dict[str, Any]]:
    connection = _open_catalog(_catalog(root))
    connection.row_factory = sqlite3.Row
    try:
        profile = connection.execute(
            """
            SELECT profile.model, profile.metric, profile.normalization,
                   profile.prefix_template, profile.extractor,
                   profile.parent_policy_json, profile.child_policy_json
            FROM retrieval_builds AS build
            JOIN embedding_profiles AS profile ON profile.profile_id = build.profile_id
            WHERE build.build_id = ?
            """,
            (selection.active_build_id,),
        ).fetchone()
        descriptor_row = connection.execute(
            """
            SELECT relative_path, file_sha256, size_bytes, dimension, metric, ntotal
            FROM vector_snapshots WHERE snapshot_id = ?
            """,
            (selection.active_snapshot_id,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT membership.faiss_id, chunk.embedding_text_sha256,
                   parent.content, chunk.span_start, chunk.span_end,
                   report.target_name, report.title, report.report_date,
                   report.report_type, report.broker,
                   report.canonical_relative_path
            FROM snapshot_membership AS membership
            JOIN retrieval_chunks AS chunk ON chunk.chunk_uid = membership.chunk_uid
            JOIN retrieval_parents AS parent ON parent.parent_uid = chunk.parent_uid
            JOIN reports AS report ON report.report_id = parent.report_id
            WHERE membership.snapshot_id = ?
            ORDER BY report.report_uid, parent.parent_order, chunk.child_order
            """,
            (selection.active_snapshot_id,),
        ).fetchall()
    finally:
        connection.close()
    if profile is None or descriptor_row is None or not rows:
        raise ReleaseTransitionError("active successor replay contract is incomplete")

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
    physical_ids = [int(row[0]) for row in rows]
    vectors = index.reconstruct(physical_ids)
    prefix_template = str(profile[3])
    hashes: list[str] = []
    for row in rows:
        content = str(row[2])
        start, end = int(row[3]), int(row[4])
        if start < 0 or end <= start or end > len(content):
            raise ReleaseTransitionError("replay contract contains an invalid span")
        metadata = {
            "target_name": row[5],
            "title": row[6],
            "report_date": row[7],
            "report_type": row[8],
            "broker": row[9],
            "canonical_relative_path": row[10],
        }
        digest = sha256_text(
            render_embedding_prefix(prefix_template, metadata) + content[start:end]
        )
        if digest != str(row[1]):
            raise ReleaseTransitionError("replay embedding text hash is invalid")
        hashes.append(digest)

    parent_policy = _json_object(profile[5], "parent policy")
    child_policy = _json_object(profile[6], "child policy")
    extractor_profile = str(profile[4])
    try:
        extractor_name, fallback_extractor_name = parse_extraction_profile(
            extractor_profile,
            allow_custom=allow_custom_extractor,
        )
    except NativeBuildError as exc:
        raise ReleaseTransitionError(
            f"extractor profile has an unsupported policy: {extractor_profile}"
        ) from exc
    use_parent_child = child_policy.get("algorithm") != "identity-span-v1"
    if use_parent_child:
        if child_policy.get("algorithm") != "langchain-recursive-v1":
            raise ReleaseTransitionError("child chunk policy is unsupported")
        single_chunk_size = int(parent_policy.get("chunk_size", 0))
    else:
        if parent_policy.get("algorithm") != "langchain-recursive-single-level-v1":
            raise ReleaseTransitionError("single-level chunk policy is unsupported")
        single_chunk_size = int(parent_policy.get("chunk_size", 0))
    parent_chunk_size = int(parent_policy.get("chunk_size", 0))
    child_chunk_size = int(child_policy.get("chunk_size", parent_chunk_size))
    if min(parent_chunk_size, child_chunk_size, single_chunk_size) <= 0:
        raise ReleaseTransitionError("chunk profile contains an invalid size")

    options = {
        "model": str(profile[0]),
        "extractor_name": extractor_name,
        "fallback_extractor_name": fallback_extractor_name,
        "allow_extraction_fallback": fallback_extractor_name is not None,
        "parent_chunk_size": parent_chunk_size,
        "child_chunk_size": child_chunk_size,
        "use_parent_child": use_parent_child,
        "single_chunk_size": single_chunk_size,
        "metric": str(profile[1]),
        "normalization": "l2" if int(profile[2]) else "none",
        "prefix_template": prefix_template,
    }
    return _ReplayEmbeddings(tuple(hashes), vectors), options


def _runtime(selection: RuntimeSelection) -> dict[str, Any]:
    return {
        "active_snapshot_id": selection.active_snapshot_id,
        "predecessor_snapshot_id": selection.predecessor_snapshot_id,
        "publication_generation": selection.publication_generation,
        "write_epoch": selection.write_epoch,
        "v1_fallback_open": selection.v1_fallback_open,
        "degraded": selection.degraded,
        "write_enabled": selection.write_enabled,
    }


def _healthy(value: Mapping[str, Any]) -> bool:
    return (
        value.get("v1_fallback_open") is False
        and value.get("degraded") is False
        and value.get("write_enabled") is True
        and int(value.get("write_epoch", 0)) > 0
    )


def _inspect(root: Path) -> RuntimeSelection:
    selection = inspect_runtime(
        root / "reports.db",
        data_root=root,
        validate_snapshot=True,
    )
    if selection.mode != "native" or selection.active_snapshot_id is None:
        raise ReleaseTransitionError("transition root is not a native runtime")
    return selection


def _snapshot_record(root: Path, snapshot_id: str) -> dict[str, Any]:
    connection = _open_catalog(_catalog(root))
    try:
        row = connection.execute(
            """
            SELECT relative_path, file_sha256, size_bytes, dimension, metric,
                   ntotal, state
            FROM vector_snapshots WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ReleaseTransitionError("snapshot audit row is missing")
    path = _resolve_relative(root, str(row[0]))
    exists = path.is_file()
    if exists and _sha256_file(path) != str(row[1]) and str(row[6]) != "failed":
        raise ReleaseTransitionError("non-failed snapshot bytes do not match the catalog")
    return {
        "snapshot_id": snapshot_id,
        "relative_path": str(row[0]),
        "file_sha256": str(row[1]),
        "size_bytes": int(row[2]),
        "dimension": int(row[3]),
        "metric": str(row[4]),
        "ntotal": int(row[5]),
        "state": str(row[6]),
        "file_exists": exists,
    }


def _protected_proof(root: Path) -> dict[str, Any]:
    """Read a protected WAL catalog through a disposable copy only."""

    tree_before = _tree_manifest(root)
    source_catalog = _catalog(root)
    with tempfile.TemporaryDirectory(prefix="finance-llm-release-proof-") as temporary:
        copied_catalog = Path(temporary) / source_catalog.name
        shutil.copyfile(source_catalog, copied_catalog)
        source_wal = Path(f"{source_catalog}-wal")
        if source_wal.is_file():
            shutil.copyfile(source_wal, Path(f"{copied_catalog}-wal"))
        logical = _catalog_logical_sha256(copied_catalog)
        connection = _open_catalog(copied_catalog)
        try:
            row = connection.execute(
                """
                SELECT active_snapshot_id, predecessor_snapshot_id,
                       publication_generation, write_epoch, v1_fallback_open,
                       degraded, write_enabled
                FROM retrieval_runtime WHERE runtime_id = 1
                """
            ).fetchone()
            if row is None:
                raise ReleaseTransitionError("protected runtime singleton is missing")
            runtime = {
                "active_snapshot_id": row[0],
                "predecessor_snapshot_id": row[1],
                "publication_generation": int(row[2]),
                "write_epoch": int(row[3]),
                "v1_fallback_open": bool(row[4]),
                "degraded": bool(row[5]),
                "write_enabled": bool(row[6]),
            }
            if runtime["active_snapshot_id"] is None or runtime[
                "predecessor_snapshot_id"
            ] is None:
                raise ReleaseTransitionError("protected runtime lineage is incomplete")
            snapshots = {
                role: _snapshot_record_from_connection(root, connection, snapshot_id)
                for role, snapshot_id in (
                    ("active", runtime["active_snapshot_id"]),
                    ("predecessor", runtime["predecessor_snapshot_id"]),
                )
            }
        finally:
            connection.close()
    tree_after = _tree_manifest(root)
    if tree_after != tree_before:
        raise ReleaseTransitionError("protected proof read mutated the protected root")
    return {
        "runtime": runtime,
        "snapshots": snapshots,
        "catalog_logical_sha256": logical,
        "tree": tree_before,
    }


def _snapshot_record_from_connection(
    root: Path,
    connection: sqlite3.Connection,
    snapshot_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT relative_path, file_sha256, size_bytes, dimension, metric,
               ntotal, state
        FROM vector_snapshots WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise ReleaseTransitionError("protected snapshot audit row is missing")
    path = _resolve_relative(root, str(row[0]))
    if not path.is_file() or path.is_symlink() or _sha256_file(path) != str(row[1]):
        raise ReleaseTransitionError("protected snapshot bytes are invalid")
    return {
        "snapshot_id": snapshot_id,
        "relative_path": str(row[0]),
        "file_sha256": str(row[1]),
        "size_bytes": int(row[2]),
        "dimension": int(row[3]),
        "metric": str(row[4]),
        "ntotal": int(row[5]),
        "state": str(row[6]),
        "file_exists": True,
    }


def _snapshot_path(root: Path, snapshot_id: str) -> Path:
    record = _snapshot_record(root, snapshot_id)
    path = _resolve_relative(root, record["relative_path"])
    if not path.is_file() or path.is_symlink():
        raise ReleaseTransitionError("snapshot path is unavailable or unsafe")
    return path


def _candidate_state(root: Path, result: Any) -> dict[str, Any]:
    connection = _open_catalog(_catalog(root))
    try:
        build = connection.execute(
            "SELECT state FROM retrieval_builds WHERE build_id = ?",
            (result.build_id,),
        ).fetchone()
        snapshot = connection.execute(
            "SELECT state FROM vector_snapshots WHERE snapshot_id = ?",
            (result.snapshot_id,),
        ).fetchone()
        running = connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE state = 'running'"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "build_state": build[0] if build else None,
        "snapshot_state": snapshot[0] if snapshot else None,
        "running_publications": int(running),
    }


def _tree_manifest(root: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for path in sorted(_isolated_tree_files(root), key=lambda value: value.as_posix()):
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return {
        "file_count": len(entries),
        "sha256": hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest(),
    }


def _catalog_logical_sha256(catalog: Path) -> str:
    connection = _open_catalog(catalog)
    try:
        connection.execute("BEGIN")
        schema = [
            list(row)
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql FROM sqlite_schema
                WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
                """
            ).fetchall()
        ]
        names = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name
                """
            ).fetchall()
        ]
        tables: dict[str, Any] = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = [
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            ]
            rows = [
                [_sql_value(item) for item in row]
                for row in connection.execute(f"SELECT * FROM {quoted}").fetchall()
            ]
            rows.sort(key=canonical_json)
            tables[name] = {"columns": columns, "rows": rows}
        return hashlib.sha256(
            canonical_json({"schema": schema, "tables": tables}).encode("utf-8")
        ).hexdigest()
    finally:
        connection.close()


def _sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise ReleaseTransitionError("catalog contains an unsupported SQLite value")


def _json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        result = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ReleaseTransitionError(f"{label} is invalid") from exc
    if not isinstance(result, dict):
        raise ReleaseTransitionError(f"{label} is invalid")
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTransitionError("query specification is unreadable") from exc
    if not isinstance(value, dict):
        raise ReleaseTransitionError("query specification must be an object")
    return value


def _flip_first_byte(path: Path) -> None:
    with path.open("r+b") as stream:
        original = stream.read(1)
        if not original:
            raise ReleaseTransitionError("active snapshot is empty")
        stream.seek(0)
        stream.write(bytes([original[0] ^ 0xFF]))
        stream.flush()
        os.fsync(stream.fileno())


def _resolve_relative(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ReleaseTransitionError("snapshot relative path is unsafe")
    path = root.joinpath(*pure.parts).resolve(strict=False)
    if root != path and root not in path.parents:
        raise ReleaseTransitionError("snapshot path escapes the data root")
    return path


def _safe_directory(value: str | Path, label: str) -> Path:
    candidate = Path(value).absolute()
    if _path_has_reparse_component(candidate):
        raise ReleaseTransitionError(f"{label} is unavailable or unsafe")
    path = candidate.resolve(strict=True)
    if not path.is_dir():
        raise ReleaseTransitionError(f"{label} is unavailable or unsafe")
    return path


def _safe_file(value: str | Path, label: str) -> Path:
    candidate = Path(value).absolute()
    if _path_has_reparse_component(candidate):
        raise ReleaseTransitionError(f"{label} is unavailable or unsafe")
    path = candidate.resolve(strict=True)
    if not path.is_file():
        raise ReleaseTransitionError(f"{label} is unavailable or unsafe")
    return path


def _assert_isolated_trees(dedicated: Path, protected: Path) -> None:
    dedicated_files = _isolated_tree_files(dedicated)
    protected_files = _isolated_tree_files(protected)

    def identities(paths: list[Path]) -> set[tuple[int, int]]:
        result: set[tuple[int, int]] = set()
        for path in paths:
            try:
                metadata = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise ReleaseTransitionError(
                    "transition isolation proof could not inspect a filesystem object"
                ) from exc
            identity = (int(metadata.st_dev), int(metadata.st_ino))
            if identity[1] <= 0:
                raise ReleaseTransitionError(
                    "transition isolation proof could not identify a filesystem object"
                )
            result.add(identity)
        return result

    if identities(dedicated_files) & identities(protected_files):
        raise ReleaseTransitionError(
            "dedicated and protected trees share a filesystem object"
        )

    dedicated_by_relative = {
        path.relative_to(dedicated): path for path in dedicated_files
    }
    protected_by_relative = {
        path.relative_to(protected): path for path in protected_files
    }
    for relative_path in dedicated_by_relative.keys() & protected_by_relative.keys():
        try:
            shared = os.path.samefile(
                dedicated_by_relative[relative_path],
                protected_by_relative[relative_path],
            )
        except OSError as exc:
            raise ReleaseTransitionError(
                "transition isolation proof could not inspect a filesystem object"
            ) from exc
        if shared:
            raise ReleaseTransitionError(
                "dedicated and protected trees share a filesystem object"
            )


def _isolated_tree_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        for name in directory_names:
            if _is_reparse_point(current / name):
                raise ReleaseTransitionError(
                    "transition tree contains a symlink or reparse point"
                )
        for name in file_names:
            path = current / name
            if _is_reparse_point(path) or not path.is_file():
                raise ReleaseTransitionError(
                    "transition tree contains an unsafe filesystem object"
                )
            files.append(path)
    return files


def _path_has_reparse_component(path: Path) -> bool:
    return any(_is_reparse_point(component) for component in (path, *path.parents))


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_point = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_point)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_within(child: Path, parent: Path) -> bool:
    return child != parent and parent in child.parents


def _catalog(root: Path) -> Path:
    return root / "retrieval" / "v2" / "catalog.sqlite3"


def _open_catalog(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        f"file:{path.resolve(strict=True).as_posix()}?mode=ro",
        uri=True,
        isolation_level=None,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_release_transition_evidence(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one immutable copied-root transition artifact exactly."""

    _exact_evidence_keys(
        value,
        {
            "schema_version",
            "kind",
            "passed",
            "fixture_only",
            "run_id",
            "started_at",
            "completed_at",
            "dedicated_copy",
            "protected_root_unchanged",
            "copy_proof",
            "event_sequence",
            "initial",
            "recovery",
            "forward_recovery",
            "lease_gc",
            "gate_d_search",
            "final",
        },
        "transition evidence",
    )
    if (
        value.get("schema_version") != 2
        or value.get("kind") != "v2_copied_install_release_transitions"
        or value.get("passed") is not True
        or value.get("fixture_only") is not False
        or value.get("dedicated_copy") is not True
        or value.get("protected_root_unchanged") is not True
        or any(
            not isinstance(value.get(field), str) or not value.get(field)
            for field in ("run_id", "started_at", "completed_at")
        )
    ):
        raise ReleaseTransitionError("transition evidence identity is invalid")

    copy_proof = _evidence_object(value.get("copy_proof"), "copy proof")
    _exact_evidence_keys(
        copy_proof,
        {
            "dedicated_catalog_logical_sha256",
            "protected_catalog_logical_sha256_before",
            "protected_catalog_logical_sha256_after",
            "protected_tree_sha256_before",
            "protected_tree_sha256_after",
            "source_tree_sha256_before",
            "source_tree_sha256_after",
            "query_spec_sha256",
            "initial_snapshot_sha256",
        },
        "copy proof",
    )
    digest_fields = (
        "dedicated_catalog_logical_sha256",
        "protected_catalog_logical_sha256_before",
        "protected_catalog_logical_sha256_after",
        "protected_tree_sha256_before",
        "protected_tree_sha256_after",
        "source_tree_sha256_before",
        "source_tree_sha256_after",
        "query_spec_sha256",
    )
    if any(not _is_sha256(copy_proof.get(field)) for field in digest_fields):
        raise ReleaseTransitionError("copy proof contains an invalid hash")
    if (
        copy_proof["dedicated_catalog_logical_sha256"]
        != copy_proof["protected_catalog_logical_sha256_before"]
        or copy_proof["protected_catalog_logical_sha256_before"]
        != copy_proof["protected_catalog_logical_sha256_after"]
        or copy_proof["protected_tree_sha256_before"]
        != copy_proof["protected_tree_sha256_after"]
        or copy_proof["source_tree_sha256_before"]
        != copy_proof["source_tree_sha256_after"]
    ):
        raise ReleaseTransitionError("protected root or source hashes changed")
    initial_snapshot_hashes = _evidence_object(
        copy_proof.get("initial_snapshot_sha256"),
        "initial snapshot hashes",
    )
    _exact_evidence_keys(
        initial_snapshot_hashes,
        {"active", "predecessor"},
        "initial snapshot hashes",
    )
    if any(
        not _is_sha256(initial_snapshot_hashes.get(role))
        for role in ("active", "predecessor")
    ):
        raise ReleaseTransitionError("initial snapshot hashes are invalid")

    _validate_release_transition_events(value.get("event_sequence"))
    initial = _validated_transition_runtime(value.get("initial"), "initial")
    if (
        not _healthy(initial)
        or initial["predecessor_snapshot_id"] is None
        or initial["predecessor_snapshot_id"] == initial["active_snapshot_id"]
    ):
        raise ReleaseTransitionError("initial runtime is not a healthy lineage")

    recovery = _evidence_object(value.get("recovery"), "recovery")
    _exact_evidence_keys(
        recovery,
        {
            "before",
            "after",
            "corrupted_snapshot_id",
            "corrupted_snapshot_sha256_before",
            "corrupted_snapshot_sha256_after",
            "recovery_disposition",
            "replay_disposition",
            "failed_snapshot_state",
        },
        "recovery",
    )
    recovery_before = _validated_transition_runtime(
        recovery.get("before"), "recovery before"
    )
    recovery_after = _validated_transition_runtime(
        recovery.get("after"), "recovery after"
    )
    if recovery_before != initial:
        raise ReleaseTransitionError("recovery continuity is invalid")
    corruption_before = recovery.get("corrupted_snapshot_sha256_before")
    corruption_after = recovery.get("corrupted_snapshot_sha256_after")
    if (
        recovery.get("corrupted_snapshot_id") != initial["active_snapshot_id"]
        or corruption_before != initial_snapshot_hashes["active"]
        or not _is_sha256(corruption_after)
        or corruption_after == corruption_before
        or recovery.get("recovery_disposition") != "predecessor_degraded"
        or recovery.get("replay_disposition") != "active"
        or recovery.get("failed_snapshot_state") != "failed"
        or recovery_after["active_snapshot_id"]
        != initial["predecessor_snapshot_id"]
        or recovery_after["predecessor_snapshot_id"] is not None
        or recovery_after["publication_generation"]
        != initial["publication_generation"] + 1
        or recovery_after["write_epoch"] != initial["write_epoch"]
        or recovery_after["v1_fallback_open"] is not False
        or recovery_after["degraded"] is not True
        or recovery_after["write_enabled"] is not False
    ):
        raise ReleaseTransitionError("recovery invariants are invalid")

    forward = _evidence_object(value.get("forward_recovery"), "forward recovery")
    _exact_evidence_keys(
        forward,
        {"before", "after", "candidate_snapshot_id", "embedding"},
        "forward recovery",
    )
    forward_before = _validated_transition_runtime(
        forward.get("before"), "forward before"
    )
    forward_after = _validated_transition_runtime(
        forward.get("after"), "forward after"
    )
    if forward_before != recovery_after:
        raise ReleaseTransitionError("forward-recovery continuity is invalid")
    embedding = _evidence_object(forward.get("embedding"), "forward embedding")
    _exact_evidence_keys(
        embedding,
        {"provider_calls", "validated_replay_calls", "validated_text_count"},
        "forward embedding",
    )
    provider_calls = embedding.get("provider_calls")
    replay_calls = embedding.get("validated_replay_calls")
    replay_texts = embedding.get("validated_text_count")
    if (
        not isinstance(provider_calls, int)
        or isinstance(provider_calls, bool)
        or provider_calls != 0
        or not isinstance(replay_calls, int)
        or isinstance(replay_calls, bool)
        or replay_calls <= 0
        or not isinstance(replay_texts, int)
        or isinstance(replay_texts, bool)
        or replay_texts <= 0
        or forward.get("candidate_snapshot_id")
        != forward_after["active_snapshot_id"]
        or forward_after["active_snapshot_id"]
        in {initial["active_snapshot_id"], recovery_after["active_snapshot_id"]}
        or forward_after["predecessor_snapshot_id"]
        != recovery_after["active_snapshot_id"]
        or forward_after["publication_generation"]
        != recovery_after["publication_generation"] + 1
        or forward_after["write_epoch"] != recovery_after["write_epoch"] + 1
        or not _healthy(forward_after)
    ):
        raise ReleaseTransitionError("forward recovery invariants are invalid")

    final = _validated_transition_runtime(value.get("final"), "final")
    if (
        not _healthy(final)
        or final["active_snapshot_id"] == forward_after["active_snapshot_id"]
        or final["predecessor_snapshot_id"] != forward_after["active_snapshot_id"]
        or final["publication_generation"]
        != forward_after["publication_generation"] + 1
        or final["write_epoch"] != forward_after["write_epoch"] + 1
    ):
        raise ReleaseTransitionError("final successor runtime is invalid")

    lease_gc = _evidence_object(value.get("lease_gc"), "lease GC")
    _exact_evidence_keys(
        lease_gc,
        {
            "leased_snapshot_id",
            "lease_acquired_before_forward_publication",
            "blocked_candidate_snapshot_id",
            "publication_blocked_while_leased",
            "blocked_error",
            "blocked_error_sha256",
            "candidate_state_before",
            "candidate_state_after",
            "runtime_after_block",
            "lease_released",
            "retired_snapshot_id",
            "retired_snapshot_state",
            "retired_snapshot_deleted",
            "validated_replay_calls_total",
        },
        "lease GC",
    )
    candidate_before = _evidence_object(
        lease_gc.get("candidate_state_before"), "lease candidate before"
    )
    candidate_after = _evidence_object(
        lease_gc.get("candidate_state_after"), "lease candidate after"
    )
    expected_candidate = {
        "build_state": "ready",
        "snapshot_state": "ready",
        "running_publications": 0,
    }
    _exact_evidence_keys(candidate_before, set(expected_candidate), "candidate before")
    _exact_evidence_keys(candidate_after, set(expected_candidate), "candidate after")
    runtime_after_block = _validated_transition_runtime(
        lease_gc.get("runtime_after_block"), "runtime after lease block"
    )
    total_replay_calls = lease_gc.get("validated_replay_calls_total")
    if (
        lease_gc.get("leased_snapshot_id") != recovery_after["active_snapshot_id"]
        or lease_gc.get("lease_acquired_before_forward_publication") is not True
        or lease_gc.get("blocked_candidate_snapshot_id") != final["active_snapshot_id"]
        or lease_gc.get("publication_blocked_while_leased") is not True
        or lease_gc.get("blocked_error") != "PublicationError"
        or not _is_sha256(lease_gc.get("blocked_error_sha256"))
        or candidate_before != expected_candidate
        or candidate_after != expected_candidate
        or runtime_after_block != forward_after
        or lease_gc.get("lease_released") is not True
        or lease_gc.get("retired_snapshot_id") != recovery_after["active_snapshot_id"]
        or lease_gc.get("retired_snapshot_state") != "garbage_collected"
        or lease_gc.get("retired_snapshot_deleted") is not True
        or not isinstance(total_replay_calls, int)
        or isinstance(total_replay_calls, bool)
        or total_replay_calls <= replay_calls
    ):
        raise ReleaseTransitionError("lease/GC evidence is invalid")

    gate_d = _evidence_object(value.get("gate_d_search"), "Gate D search")
    _exact_evidence_keys(
        gate_d,
        {
            "query_id",
            "query_text_sha256",
            "query_vector_sha256",
            "query_spec_sha256",
            "expected_report_uid",
            "top_report_uid",
            "top_rank",
            "citation_complete",
            "citation_sha256",
            "query_generation",
            "snapshot_id",
            "publication_generation",
        },
        "Gate D search",
    )
    query_generation = _evidence_object(
        gate_d.get("query_generation"), "Gate D query generation"
    )
    _exact_evidence_keys(
        query_generation,
        {"provider", "model", "input_type", "provider_calls", "attestation_sha256"},
        "Gate D query generation",
    )
    if (
        not isinstance(gate_d.get("query_id"), str)
        or not gate_d.get("query_id")
        or not _is_sha256(gate_d.get("query_text_sha256"))
        or not _is_sha256(gate_d.get("query_vector_sha256"))
        or gate_d.get("query_spec_sha256") != copy_proof["query_spec_sha256"]
        or not _is_sha256(gate_d.get("expected_report_uid"))
        or gate_d.get("top_report_uid") != gate_d.get("expected_report_uid")
        or gate_d.get("top_rank") != 1
        or isinstance(gate_d.get("top_rank"), bool)
        or gate_d.get("citation_complete") is not True
        or not _is_sha256(gate_d.get("citation_sha256"))
        or query_generation.get("provider") != "openrouter"
        or not isinstance(query_generation.get("model"), str)
        or not query_generation.get("model")
        or query_generation.get("input_type") != "search_query"
        or query_generation.get("provider_calls") != 1
        or isinstance(query_generation.get("provider_calls"), bool)
        or not _is_sha256(query_generation.get("attestation_sha256"))
        or gate_d.get("snapshot_id") != final["active_snapshot_id"]
        or gate_d.get("publication_generation") != final["publication_generation"]
    ):
        raise ReleaseTransitionError("Gate D evidence is invalid")
    return {
        "run_id": value["run_id"],
        "final_runtime_identity": final,
        "query_spec_sha256": copy_proof["query_spec_sha256"],
        "protected_tree_sha256_after": copy_proof[
            "protected_tree_sha256_after"
        ],
        "source_tree_sha256_after": copy_proof["source_tree_sha256_after"],
    }


def _validate_release_transition_events(value: Any) -> None:
    expected = (
        "initial_health_validated",
        "active_snapshot_corrupted",
        "predecessor_recovery_completed",
        "degraded_snapshot_leased",
        "forward_recovery_published",
        "next_candidate_materialized",
        "publication_blocked_while_leased",
        "lease_released",
        "successor_published",
        "retired_snapshot_garbage_collected",
        "gate_d_search_validated",
        "protected_root_revalidated",
    )
    if not isinstance(value, list) or len(value) != len(expected):
        raise ReleaseTransitionError("transition event sequence is incomplete")
    for sequence, (item, event) in enumerate(zip(value, expected, strict=True), 1):
        if (
            not isinstance(item, Mapping)
            or set(item) != {"sequence", "event", "recorded_at"}
            or item.get("sequence") != sequence
            or isinstance(item.get("sequence"), bool)
            or item.get("event") != event
            or not isinstance(item.get("recorded_at"), str)
            or not item.get("recorded_at")
        ):
            raise ReleaseTransitionError("transition event sequence is invalid")


def _validated_transition_runtime(value: Any, label: str) -> dict[str, Any]:
    runtime = _evidence_object(value, label)
    _exact_evidence_keys(
        runtime,
        {
            "active_snapshot_id",
            "predecessor_snapshot_id",
            "publication_generation",
            "write_epoch",
            "v1_fallback_open",
            "degraded",
            "write_enabled",
        },
        label,
    )
    snapshot = runtime.get("active_snapshot_id")
    predecessor = runtime.get("predecessor_snapshot_id")
    generation = runtime.get("publication_generation")
    epoch = runtime.get("write_epoch")
    if (
        not _is_sha256(snapshot)
        or (predecessor is not None and not _is_sha256(predecessor))
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation <= 0
        or not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or epoch <= 0
        or any(
            not isinstance(runtime.get(field), bool)
            for field in ("v1_fallback_open", "degraded", "write_enabled")
        )
    ):
        raise ReleaseTransitionError(f"{label} runtime identity is invalid")
    return dict(runtime)


def _exact_evidence_keys(
    value: Mapping[str, Any],
    expected: set[str],
    label: str,
) -> None:
    if set(value) != expected:
        raise ReleaseTransitionError(f"{label} fields are invalid")


def _evidence_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseTransitionError(f"{label} is invalid")
    return value


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _append_event(events: list[dict[str, Any]], event: str) -> None:
    events.append(
        {
            "sequence": len(events) + 1,
            "event": event,
            "recorded_at": _utc_now(),
        }
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def capture_tree_manifest(root: str | Path) -> dict[str, Any]:
    """Hash one read-only input tree without exposing its absolute path."""

    return _tree_manifest(_safe_directory(root, "evidence tree"))


__all__ = [
    "ReleaseTransitionError",
    "capture_tree_manifest",
    "execute_release_transitions",
    "validate_release_transition_evidence",
]
