from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from src.retrieval import garbage_collector as garbage_collector_module
from src.retrieval import publication as publication_module
from src.retrieval.bootstrap import reconcile_and_inspect_runtime
from src.retrieval.delta_schema import install_delta_schema
from src.retrieval.garbage_collector import (
    GarbageCollectionBlocked,
    GarbageCollectionError,
    RetrievalGarbageCollector,
)
from src.retrieval.publication import (
    PublicationCoordinator,
    PublicationError,
    PublicationRequest,
)
from src.retrieval.repository import (
    SnapshotCache,
    SnapshotRevision,
    shared_snapshot_cache,
)
from src.retrieval.runtime_guard import (
    RetrievalWriteBlocked,
    guard_before_retrieval_write,
)
from src.retrieval.vector_index import SnapshotDescriptor
from src.retrieval.writer_lock import NativeWriterLock
from tests.retrieval.test_retrieval_publication import (
    _insert_build_and_snapshot,
    make_native_install,
)


class _FakeIndex:
    dimension = 2
    metric = "l2"
    ntotal = 1

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _published_install(tmp_path: Path) -> tuple[Path, Path]:
    data_root, request = make_native_install(tmp_path)
    PublicationCoordinator(data_root).publish(request)
    return data_root, data_root / "retrieval" / "v2" / "catalog.sqlite3"


def _add_retired_snapshot(
    data_root: Path,
    catalog: Path,
    *,
    snapshot_id: str = "snapshot-retired",
    vector: tuple[float, float] = (0.5, 0.5),
) -> SnapshotRevision:
    build_id = snapshot_id.replace("snapshot", "build", 1)
    connection = sqlite3.connect(catalog)
    try:
        _insert_build_and_snapshot(
            connection,
            data_root,
            build_id=build_id,
            snapshot_id=snapshot_id,
            vector=vector,
            chunk_uid="60" * 32,
        )
        row = connection.execute(
            """
            SELECT relative_path, file_sha256, size_bytes,
                   dimension, metric, ntotal
            FROM vector_snapshots WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        connection.commit()
    finally:
        connection.close()
    descriptor = SnapshotDescriptor(
        sha256=row[1],
        size_bytes=row[2],
        dimension=row[3],
        metric=row[4],
        ntotal=row[5],
    )
    return SnapshotRevision(
        catalog_path=catalog,
        publication_generation=2,
        snapshot_id=snapshot_id,
        build_id=build_id,
        profile_id="profile-v2",
        snapshot_path=data_root / row[0],
        descriptor=descriptor,
    )


def _add_delta_artifact(
    data_root: Path,
    catalog: Path,
    *,
    base_snapshot_id: str,
    base_publication_generation: int = 0,
    sequence: int = 1,
    state: str = "compacted",
) -> tuple[str, Path]:
    payload = f"compacted-delta:{base_snapshot_id}:{sequence}".encode("utf-8")
    segment_id = hashlib.sha256(payload).hexdigest()
    relative_path = f"retrieval/v2/deltas/{segment_id}.faiss"
    artifact_path = data_root.joinpath(*relative_path.split("/"))
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(payload)

    connection = sqlite3.connect(catalog)
    try:
        install_delta_schema(connection)
        dimension, metric = connection.execute(
            "SELECT dimension, metric FROM vector_snapshots WHERE snapshot_id = ?",
            (base_snapshot_id,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO retrieval_delta_segments (
                segment_id, base_snapshot_id, base_publication_generation,
                sequence, relative_path, file_sha256, size_bytes,
                dimension, metric, ntotal, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                segment_id,
                base_snapshot_id,
                base_publication_generation,
                sequence,
                relative_path,
                hashlib.sha256(payload).hexdigest(),
                len(payload),
                dimension,
                metric,
                state,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return segment_id, artifact_path


def _new_publication_request(data_root: Path, snapshot_id: str) -> PublicationRequest:
    relative = f"retrieval/v2/evidence/publication-{snapshot_id}/manifest.json"
    manifest = data_root / relative
    manifest.parent.mkdir(parents=True)
    connection = sqlite3.connect(data_root / "retrieval" / "v2" / "catalog.sqlite3")
    try:
        runtime = connection.execute(
            """
            SELECT active_snapshot_id, publication_generation, write_epoch
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
        build_id = connection.execute(
            "SELECT build_id FROM vector_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "kind": "native_full_corpus_candidate",
                "publication_id": f"publication-{snapshot_id}",
                "build_id": build_id,
                "snapshot_id": snapshot_id,
                "base_snapshot_id": runtime[0],
                "base_publication_generation": runtime[1],
                "base_write_epoch": runtime[2],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    manifest.write_bytes(payload)
    return PublicationRequest(
        publication_id=f"publication-{snapshot_id}",
        to_snapshot_id=snapshot_id,
        evidence_manifest_relative_path=relative,
        evidence_manifest_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _snapshot_state(catalog: Path, snapshot_id: str) -> str:
    connection = sqlite3.connect(catalog)
    try:
        return connection.execute(
            "SELECT state FROM vector_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()[0]
    finally:
        connection.close()


def _snapshot_revision(
    data_root: Path,
    catalog: Path,
    snapshot_id: str,
) -> SnapshotRevision:
    connection = sqlite3.connect(catalog)
    try:
        row = connection.execute(
            """
            SELECT snapshot.build_id, build.profile_id, snapshot.relative_path,
                   snapshot.file_sha256, snapshot.size_bytes,
                   snapshot.dimension, snapshot.metric, snapshot.ntotal,
                   runtime.publication_generation
            FROM vector_snapshots AS snapshot
            JOIN retrieval_builds AS build ON build.build_id = snapshot.build_id
            CROSS JOIN retrieval_runtime AS runtime
            WHERE snapshot.snapshot_id = ? AND runtime.runtime_id = 1
            """,
            (snapshot_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return SnapshotRevision(
        catalog_path=catalog,
        publication_generation=int(row[8]),
        snapshot_id=snapshot_id,
        build_id=str(row[0]),
        profile_id=str(row[1]),
        snapshot_path=data_root.joinpath(*str(row[2]).split("/")),
        descriptor=SnapshotDescriptor(
            sha256=str(row[3]),
            size_bytes=int(row[4]),
            dimension=int(row[5]),
            metric=str(row[6]),
            ntotal=int(row[7]),
        ),
    )


def test_next_publication_blocks_on_leased_predecessor_then_retires_it(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    seed = _snapshot_revision(data_root, catalog, "snapshot-seed")
    next_revision = _add_retired_snapshot(
        data_root,
        catalog,
        snapshot_id="snapshot-next",
        vector=(0.25, 0.75),
    )
    request = _new_publication_request(data_root, next_revision.snapshot_id)
    lease = shared_snapshot_cache(data_root).lease(seed)
    lease.__enter__()

    with pytest.raises(PublicationError, match="predecessor is still leased"):
        PublicationCoordinator(data_root).publish(request)
    assert _snapshot_state(catalog, seed.snapshot_id) == "ready"
    assert seed.snapshot_path.is_file()
    connection = sqlite3.connect(catalog)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM publication_runs WHERE publication_id = ?",
            (request.publication_id,),
        ).fetchone() == (0,)
    finally:
        connection.close()

    lease.__exit__(None, None, None)
    outcome = PublicationCoordinator(data_root).publish(request)

    assert outcome.active_snapshot_id == next_revision.snapshot_id
    assert outcome.predecessor_snapshot_id == "snapshot-successor"
    assert _snapshot_state(catalog, seed.snapshot_id) == "garbage_collected"
    assert not seed.snapshot_path.exists()


def test_publication_collects_compacted_artifacts_with_their_retired_base(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    _segment_id, delta_path = _add_delta_artifact(
        data_root,
        catalog,
        base_snapshot_id="snapshot-seed",
    )
    next_revision = _add_retired_snapshot(
        data_root,
        catalog,
        snapshot_id="snapshot-next",
        vector=(0.25, 0.75),
    )

    outcome = PublicationCoordinator(data_root).publish(
        _new_publication_request(data_root, next_revision.snapshot_id)
    )

    assert outcome.active_snapshot_id == next_revision.snapshot_id
    assert _snapshot_state(catalog, "snapshot-seed") == "garbage_collected"
    assert not delta_path.exists()


def test_production_fast_startup_sweeps_compacted_artifact_gc(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    retired = _add_retired_snapshot(data_root, catalog)
    collected = RetrievalGarbageCollector(data_root).collect_snapshot(
        retired.snapshot_id
    )
    assert collected.deleted is True
    _segment_id, delta_path = _add_delta_artifact(
        data_root,
        catalog,
        base_snapshot_id=retired.snapshot_id,
    )

    selection = reconcile_and_inspect_runtime(
        data_root,
        prefer_fast_read=True,
    )

    assert selection.is_native
    assert not delta_path.exists()


def test_compacted_artifact_cleanup_retries_after_a_locked_file(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    retired = _add_retired_snapshot(data_root, catalog)
    RetrievalGarbageCollector(data_root).collect_snapshot(retired.snapshot_id)
    segment_id, delta_path = _add_delta_artifact(
        data_root,
        catalog,
        base_snapshot_id=retired.snapshot_id,
    )

    def deny_delete(_path: Path) -> None:
        raise PermissionError("simulated Windows open handle")

    pending = RetrievalGarbageCollector(
        data_root,
        remove_file=deny_delete,
    ).reconcile_compacted_delta_artifacts()

    assert len(pending) == 1
    assert pending[0].segment_id == segment_id
    assert pending[0].deleted is False
    assert delta_path.is_file()

    assert RetrievalGarbageCollector(data_root).reconcile_pending_snapshots() == ()
    assert not delta_path.exists()


def test_compacted_artifact_gc_never_deletes_a_path_swapped_before_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    retired = _add_retired_snapshot(data_root, catalog)
    RetrievalGarbageCollector(data_root).collect_snapshot(retired.snapshot_id)
    segment_id, delta_path = _add_delta_artifact(
        data_root,
        catalog,
        base_snapshot_id=retired.snapshot_id,
    )
    foreign_payload = b"foreign-file-must-not-be-deleted"
    real_replace = garbage_collector_module.os.replace
    swapped = False

    def swap_then_replace(source, destination):
        nonlocal swapped
        if Path(source) == delta_path and not swapped:
            swapped = True
            delta_path.unlink()
            delta_path.write_bytes(foreign_payload)
        return real_replace(source, destination)

    monkeypatch.setattr(
        garbage_collector_module.os,
        "replace",
        swap_then_replace,
    )

    with pytest.raises(
        GarbageCollectionError,
        match="immutable descriptor",
    ):
        RetrievalGarbageCollector(data_root).reconcile_compacted_delta_artifacts()

    assert swapped is True
    assert delta_path.read_bytes() == foreign_payload
    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            "SELECT 1 FROM retrieval_delta_artifact_gc WHERE segment_id = ?",
            (segment_id,),
        ).fetchone() is None


def test_compacted_artifact_cleanup_records_ledger_and_second_sweep_is_empty(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    retired = _add_retired_snapshot(data_root, catalog)
    RetrievalGarbageCollector(data_root).collect_snapshot(retired.snapshot_id)
    segment_id, delta_path = _add_delta_artifact(
        data_root,
        catalog,
        base_snapshot_id=retired.snapshot_id,
    )
    collector = RetrievalGarbageCollector(data_root)

    first = collector.reconcile_compacted_delta_artifacts()
    second = collector.reconcile_compacted_delta_artifacts()

    assert [item.segment_id for item in first] == [segment_id]
    assert first[0].deleted is True
    assert second == ()
    assert not delta_path.exists()
    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            """
            SELECT segment_id FROM retrieval_delta_artifact_gc
            WHERE segment_id = ?
            """,
            (segment_id,),
        ).fetchone() == (segment_id,)


def test_standalone_gc_runs_one_full_catalog_integrity_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, _catalog = _published_install(tmp_path)
    original_validate = publication_module._validate_catalog_integrity
    validations = 0

    def count_full_validation(connection):
        nonlocal validations
        validations += 1
        original_validate(connection)

    monkeypatch.setattr(
        publication_module,
        "_validate_catalog_integrity",
        count_full_validation,
    )

    assert RetrievalGarbageCollector(data_root).reconcile_pending_snapshots() == ()
    assert validations == 1


def test_public_gc_with_external_writer_lease_still_runs_full_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, _catalog = _published_install(tmp_path)
    original_validate = publication_module._validate_catalog_integrity
    validations = 0

    def count_full_validation(connection):
        nonlocal validations
        validations += 1
        original_validate(connection)

    monkeypatch.setattr(
        publication_module,
        "_validate_catalog_integrity",
        count_full_validation,
    )

    with NativeWriterLock(data_root) as writer_lease:
        outcomes = RetrievalGarbageCollector(data_root).reconcile_pending_snapshots(
            writer_lease=writer_lease
        )

    assert outcomes == ()
    assert validations == 1


def test_postcommit_gc_failure_is_normalized_on_startup_and_collected_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    with sqlite3.connect(catalog) as connection:
        active_snapshot_id, generation = connection.execute(
            """
            SELECT active_snapshot_id, publication_generation
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone()
    segment_id, delta_path = _add_delta_artifact(
        data_root,
        catalog,
        base_snapshot_id=str(active_snapshot_id),
        base_publication_generation=int(generation),
        state="ready",
    )
    next_revision = _add_retired_snapshot(
        data_root,
        catalog,
        snapshot_id="snapshot-next",
        vector=(0.25, 0.75),
    )
    real_reconcile = (
        RetrievalGarbageCollector._reconcile_pending_snapshots_after_validation
    )

    def fail_after_commit(self, *, writer_lease):
        raise GarbageCollectionError("simulated postcommit cleanup failure")

    monkeypatch.setattr(
        RetrievalGarbageCollector,
        "_reconcile_pending_snapshots_after_validation",
        fail_after_commit,
    )
    outcome = PublicationCoordinator(data_root).publish(
        _new_publication_request(data_root, next_revision.snapshot_id)
    )
    assert outcome.active_snapshot_id == next_revision.snapshot_id
    assert outcome.cleanup_pending is True
    assert "postcommit cleanup failure" in str(outcome.cleanup_error)
    monkeypatch.setattr(
        RetrievalGarbageCollector,
        "_reconcile_pending_snapshots_after_validation",
        real_reconcile,
    )

    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            "SELECT active_snapshot_id FROM retrieval_runtime WHERE runtime_id = 1"
        ).fetchone() == (next_revision.snapshot_id,)
        assert connection.execute(
            "SELECT state FROM retrieval_delta_segments WHERE segment_id = ?",
            (segment_id,),
        ).fetchone() == ("ready",)
    assert delta_path.is_file()

    selection = reconcile_and_inspect_runtime(
        data_root,
    )
    assert selection.active_snapshot_id == next_revision.snapshot_id
    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            "SELECT state FROM retrieval_delta_segments WHERE segment_id = ?",
            (segment_id,),
        ).fetchone() == ("compacted",)
    assert delta_path.is_file()

    later_revision = _add_retired_snapshot(
        data_root,
        catalog,
        snapshot_id="snapshot-later",
        vector=(0.75, 0.25),
    )
    PublicationCoordinator(data_root).publish(
        _new_publication_request(data_root, later_revision.snapshot_id)
    )

    assert not delta_path.exists()
    with sqlite3.connect(catalog) as connection:
        assert connection.execute(
            """
            SELECT segment_id FROM retrieval_delta_artifact_gc
            WHERE segment_id = ?
            """,
            (segment_id,),
        ).fetchone() == (segment_id,)


def test_compacted_artifact_cleanup_waits_for_a_lazy_cache_lease(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    retired = _add_retired_snapshot(data_root, catalog)
    RetrievalGarbageCollector(data_root).collect_snapshot(retired.snapshot_id)
    segment_id, delta_path = _add_delta_artifact(
        data_root,
        catalog,
        base_snapshot_id=retired.snapshot_id,
    )
    payload = delta_path.read_bytes()
    cache = SnapshotCache()
    revision = SnapshotRevision(
        catalog_path=catalog,
        publication_generation=3,
        snapshot_id=segment_id,
        build_id="compacted-delta",
        profile_id="profile-v2",
        snapshot_path=delta_path,
        descriptor=SnapshotDescriptor(
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            dimension=2,
            metric="l2",
            ntotal=1,
        ),
    )
    lease = cache.lease(revision)
    lease.__enter__()
    collector = RetrievalGarbageCollector(data_root, cache=cache)

    pending = collector.reconcile_compacted_delta_artifacts()

    assert len(pending) == 1
    assert pending[0].deleted is False
    assert delta_path.is_file()

    lease.__exit__(None, None, None)
    completed = collector.reconcile_compacted_delta_artifacts()
    assert len(completed) == 1
    assert completed[0].deleted is True
    assert not delta_path.exists()


def test_leased_snapshot_stays_pending_and_blocks_the_next_publication(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    retired = _add_retired_snapshot(data_root, catalog)
    next_revision = _add_retired_snapshot(
        data_root,
        catalog,
        snapshot_id="snapshot-next",
        vector=(0.25, 0.75),
    )
    request = _new_publication_request(data_root, next_revision.snapshot_id)
    fake = _FakeIndex()
    cache = SnapshotCache(loader=lambda _path, _descriptor: fake)
    lease = cache.acquire(retired)
    assert lease.__enter__() is fake

    collector = RetrievalGarbageCollector(data_root, cache=cache)
    outcome = collector.collect_snapshot(retired.snapshot_id)

    assert outcome.state == "garbage_pending"
    assert outcome.deleted is False
    assert retired.snapshot_path.is_file()
    assert _snapshot_state(catalog, retired.snapshot_id) == "garbage_pending"
    with pytest.raises(RetrievalWriteBlocked, match="garbage"):
        guard_before_retrieval_write(data_root)
    with pytest.raises(PublicationError, match="garbage"):
        PublicationCoordinator(data_root).publish(request)

    lease.__exit__(None, None, None)
    completed = collector.reconcile_pending_snapshots()
    assert len(completed) == 1
    assert completed[0].state == "garbage_collected"
    assert not retired.snapshot_path.exists()
    assert fake.closed is True
    assert _snapshot_state(catalog, retired.snapshot_id) == "garbage_collected"


def test_permission_error_remains_pending_until_clean_startup_retry(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    retired = _add_retired_snapshot(data_root, catalog)

    def deny_delete(_path: Path) -> None:
        raise PermissionError("simulated Windows open handle")

    outcome = RetrievalGarbageCollector(
        data_root,
        cache=SnapshotCache(),
        remove_file=deny_delete,
    ).collect_snapshot(retired.snapshot_id)

    assert outcome.state == "garbage_pending"
    assert retired.snapshot_path.exists()
    assert _snapshot_state(catalog, retired.snapshot_id) == "garbage_pending"

    selection = reconcile_and_inspect_runtime(
        data_root,
    )
    assert selection.is_native
    assert not retired.snapshot_path.exists()
    assert _snapshot_state(catalog, retired.snapshot_id) == "garbage_collected"


def test_active_and_verified_predecessor_are_never_marked_for_gc(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    collector = RetrievalGarbageCollector(data_root, cache=SnapshotCache())

    with pytest.raises(GarbageCollectionBlocked, match="predecessor"):
        collector.collect_snapshot("snapshot-successor")
    with pytest.raises(GarbageCollectionBlocked, match="predecessor"):
        collector.collect_snapshot("snapshot-seed")

    assert _snapshot_state(catalog, "snapshot-successor") == "ready"
    assert _snapshot_state(catalog, "snapshot-seed") == "ready"
