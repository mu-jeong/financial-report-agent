from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from src.retrieval import dispatch as dispatch_module
from src.retrieval.bootstrap import (
    RetrievalBootstrapError,
    RetrievalPaths,
    RuntimeSelection,
    reconcile_and_inspect_runtime,
)
from src.retrieval.dispatch import (
    RetrievalDispatchStateError,
    prime_native_dispatch,
    reset_native_dispatchers,
    resolve_retrieval_dispatch,
)
from tests.retrieval.test_retrieval_bootstrap import _native_install
from tests.retrieval.test_retrieval_repository import _create_catalog


@pytest.fixture(autouse=True)
def _isolated_native_dispatch_registry():
    reset_native_dispatchers()
    yield
    reset_native_dispatchers()


def _selection(tmp_path, *, mode: str = "native") -> RuntimeSelection:
    root = tmp_path.resolve()
    paths = RetrievalPaths(
        data_root=root,
        catalog=root / "retrieval" / "v2" / "catalog.sqlite3",
        v2_root=root / "retrieval" / "v2",
    )
    return RuntimeSelection(
        mode=mode,
        paths=paths,
        active_snapshot_id="snapshot-1" if mode == "native" else None,
        active_build_id="build-1" if mode == "native" else None,
        compatibility_bundle_id=(
            "bundle-1" if mode == "epoch_zero_compatibility" else None
        ),
    )


def test_two_native_resolutions_inspect_and_construct_at_most_once(
    tmp_path,
    monkeypatch,
):
    selection = _selection(tmp_path)
    calls = {"inspect": 0, "repository": 0, "reader": 0, "close": 0}

    def fake_inspect(*_args, **_kwargs):
        calls["inspect"] += 1
        return selection

    class FakeRepository:
        def __init__(self, catalog_path, *, data_root):
            calls["repository"] += 1
            assert catalog_path == selection.paths.catalog
            assert data_root == selection.paths.data_root

        def close(self):
            calls["close"] += 1

    class FakeReader:
        def __init__(self, repository):
            calls["reader"] += 1
            self.repository = repository

    monkeypatch.setattr(dispatch_module, "inspect_runtime", fake_inspect)
    monkeypatch.setattr(dispatch_module, "CatalogRepository", FakeRepository)
    monkeypatch.setattr(dispatch_module, "NativeRetrievalReader", FakeReader)

    first = resolve_retrieval_dispatch(tmp_path / "reports.db")
    second = resolve_retrieval_dispatch(tmp_path / "reports.db")

    assert first.mode == second.mode == "native"
    assert first.native is second.native
    assert first.native is not None
    assert first.native.reader is second.native.reader
    assert first.selection is None
    assert second.selection is None
    assert calls == {"inspect": 1, "repository": 1, "reader": 1, "close": 0}

    reset_native_dispatchers(selection.paths.data_root)
    assert calls["close"] == 1
    rebuilt = resolve_retrieval_dispatch(tmp_path / "reports.db")
    assert rebuilt.native is not first.native
    assert calls == {"inspect": 2, "repository": 2, "reader": 2, "close": 1}


def test_same_process_reconciliation_primes_native_dispatch(tmp_path, monkeypatch):
    legacy, _snapshot = _native_install(tmp_path)
    primed = []
    original_prime = dispatch_module.prime_native_dispatch

    def recording_prime(selection):
        primed.append(selection)
        return original_prime(selection)

    monkeypatch.setattr(dispatch_module, "prime_native_dispatch", recording_prime)

    selection = reconcile_and_inspect_runtime(legacy)
    monkeypatch.setattr(
        dispatch_module,
        "inspect_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("primed native dispatch re-inspected the runtime")
        ),
    )
    resolved = resolve_retrieval_dispatch(legacy)

    assert primed == [selection]
    assert resolved.mode == "native"
    assert resolved.native is not None


def test_lax_cold_resolution_cannot_poison_strict_snapshot_validation(tmp_path):
    legacy, snapshot = _native_install(tmp_path, epoch=1)
    snapshot.write_bytes(b"corrupt")

    with pytest.raises(
        RetrievalDispatchStateError,
        match="requires active snapshot validation",
    ):
        resolve_retrieval_dispatch(legacy, validate_snapshot=False)

    with pytest.raises(RetrievalBootstrapError, match="fallback closure"):
        resolve_retrieval_dispatch(legacy)


def test_concurrent_native_first_resolution_constructs_once(tmp_path, monkeypatch):
    selection = _selection(tmp_path)
    calls = {"inspect": 0, "repository": 0, "reader": 0}
    calls_lock = threading.Lock()

    def increment(name: str) -> None:
        with calls_lock:
            calls[name] += 1

    def fake_inspect(*_args, **_kwargs):
        increment("inspect")
        time.sleep(0.01)
        return selection

    class FakeRepository:
        def __init__(self, *_args, **_kwargs):
            increment("repository")

        def close(self):
            return None

    class FakeReader:
        def __init__(self, repository):
            increment("reader")
            self.repository = repository

    monkeypatch.setattr(dispatch_module, "inspect_runtime", fake_inspect)
    monkeypatch.setattr(dispatch_module, "CatalogRepository", FakeRepository)
    monkeypatch.setattr(dispatch_module, "NativeRetrievalReader", FakeReader)

    with ThreadPoolExecutor(max_workers=8) as pool:
        resolved = list(
            pool.map(
                lambda _offset: resolve_retrieval_dispatch(
                    tmp_path / "reports.db"
                ),
                range(16),
            )
        )

    assert len({id(item.native) for item in resolved}) == 1
    assert calls == {"inspect": 1, "repository": 1, "reader": 1}


def test_reused_native_dispatch_observes_next_publication_generation(tmp_path):
    catalog, _rows = _create_catalog(tmp_path, generation=7)
    root = tmp_path.resolve()
    selection = RuntimeSelection(
        mode="native",
        paths=RetrievalPaths(
            data_root=root,
            catalog=catalog.resolve(),
            v2_root=root,
        ),
        active_snapshot_id="snapshot-1",
        active_build_id="build-1",
        publication_generation=7,
    )
    holder = prime_native_dispatch(selection)

    first = holder.reader.search(np.asarray([0.0, 5.0], dtype=np.float32), 1)
    connection = sqlite3.connect(catalog)
    try:
        connection.execute(
            "UPDATE retrieval_runtime SET publication_generation = 8 "
            "WHERE runtime_id = 1"
        )
        connection.commit()
    finally:
        connection.close()
    second_holder = prime_native_dispatch(selection)
    second = second_holder.reader.search(
        np.asarray([0.0, 5.0], dtype=np.float32),
        1,
    )

    assert second_holder is holder
    assert first.revision.publication_generation == 7
    assert second.revision.publication_generation == 8


def test_legacy_and_compatibility_modes_are_reinspected_and_never_cached(
    tmp_path,
    monkeypatch,
):
    selections = [
        _selection(tmp_path, mode="legacy_v1"),
        _selection(tmp_path, mode="epoch_zero_compatibility"),
    ]
    calls = []

    def fake_inspect(*_args, **_kwargs):
        calls.append(True)
        return selections.pop(0)

    monkeypatch.setattr(dispatch_module, "inspect_runtime", fake_inspect)
    monkeypatch.setattr(
        dispatch_module,
        "CatalogRepository",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("non-native dispatch must not construct a repository")
        ),
    )

    legacy = resolve_retrieval_dispatch(tmp_path / "reports.db")
    compatibility = resolve_retrieval_dispatch(tmp_path / "reports.db")

    assert legacy.mode == "legacy_v1"
    assert compatibility.mode == "epoch_zero_compatibility"
    assert legacy.native is compatibility.native is None
    assert legacy.selection is not None
    assert compatibility.selection is not None
    assert len(calls) == 2
