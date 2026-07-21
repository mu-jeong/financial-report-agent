from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from src.retrieval.bootstrap import reconcile_and_inspect_runtime
from src.retrieval.garbage_collector import (
    GarbageCollectionBlocked,
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
from tests.retrieval.test_retrieval_publication import (
    COMPATIBILITY_BUNDLE_ID,
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
        guard_before_retrieval_write(data_root / "reports.db", data_root=data_root)
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
        data_root / "reports.db",
        data_root=data_root,
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


def test_closed_compatibility_bundle_requires_explicit_retention_approval(
    tmp_path: Path,
) -> None:
    data_root, catalog = _published_install(tmp_path)
    bundle = (
        data_root
        / "retrieval"
        / "compat"
        / "v1"
        / COMPATIBILITY_BUNDLE_ID
    )
    collector = RetrievalGarbageCollector(data_root, cache=SnapshotCache())

    retained = collector.collect_compatibility_bundle(COMPATIBILITY_BUNDLE_ID)
    assert retained.state == "retained"
    assert bundle.is_dir()

    collected = collector.collect_compatibility_bundle(
        COMPATIBILITY_BUNDLE_ID,
        validation_window_elapsed=True,
    )
    replay = collector.collect_compatibility_bundle(
        COMPATIBILITY_BUNDLE_ID,
        validation_window_elapsed=True,
    )
    assert collected.state == replay.state == "garbage_collected"
    assert not bundle.exists()

    connection = sqlite3.connect(catalog)
    try:
        assert connection.execute(
            """
            SELECT write_epoch, v1_fallback_open
            FROM retrieval_runtime WHERE runtime_id = 1
            """
        ).fetchone() == (1, 0)
    finally:
        connection.close()
