from __future__ import annotations

import hashlib
import json

import pytest

from src.retrieval import bootstrap as bootstrap_module
from src.migrations.v2.validation import (
    copied_install_benchmark as copied_benchmark_module,
)
from src.retrieval import dispatch as dispatch_module
from src.migrations.v2.validation.copied_install_benchmark import (
    create_factory,
    create_successor_factory,
)
from src.retrieval.build_service import materialize_candidate, publish_candidate
from src.migrations.v2.compatibility import V1CompatibilityReader
from src.retrieval.dispatch import reset_native_dispatchers
from src.migrations.v2.validation.performance import PerformanceEvidenceError, REQUIRED_WORKLOADS
from tests.retrieval.test_retrieval_build_service import (
    DeterministicEmbeddings,
    VECTORS,
    _native_seed,
    _prepare,
)


def test_copied_install_factory_opens_both_epoch_zero_readers_without_path_leak(
    tmp_path,
    monkeypatch,
):
    reset_native_dispatchers()
    data_root, _sources = _native_seed(tmp_path)
    query_id = hashlib.sha256(b"opaque-query-1").hexdigest()
    workloads = {
        "unfiltered": {"scope": None},
        "empty": {"scope": {"empty": True}},
        "narrow": {"scope": {"file_names": ["a.pdf"]}},
        "broad": {"scope": {"report_types": ["company"]}},
        "near_universe": {
            "scope": {"report_types": ["company", "industry"]},
        },
        "prior_scope": {
            "scope": {"prior_scope": {"file_names": ["a.pdf"]}},
        },
    }
    assert set(workloads) == set(REQUIRED_WORKLOADS)
    specification = {
        "schema_version": 1,
        "kind": "v2_retrieval_query_vectors",
        "k": 1,
        "queries": [{"query_id": query_id, "vector": VECTORS[0].tolist()}],
        "workloads": workloads,
    }
    input_path = tmp_path / "query-vectors.json"
    input_path.write_text(json.dumps(specification), encoding="utf-8")
    monkeypatch.setenv("V2_BENCHMARK_DATA_ROOT", str(data_root))
    monkeypatch.setenv("V2_BENCHMARK_INPUT", str(input_path))
    calls = []
    dispatch_calls = []
    original_search = V1CompatibilityReader.search
    original_prime = copied_benchmark_module.prime_native_dispatch

    def recording_search(self, query_vector, **kwargs):
        calls.append(kwargs)
        return original_search(self, query_vector, **kwargs)

    monkeypatch.setattr(V1CompatibilityReader, "search", recording_search)
    monkeypatch.setattr(
        copied_benchmark_module,
        "prime_native_dispatch",
        lambda selection: dispatch_calls.append(selection.paths.data_root)
        or original_prime(selection),
    )

    factory = create_factory(process_id="process-1", seed=20260716)
    empty_payload = factory.queries["empty"][0].payload
    direct_payload = factory.queries["unfiltered"][0].payload
    v1_empty = factory.v1_probe("empty", empty_payload)
    v2_empty = factory.v2_probe("empty", empty_payload)
    v2_direct = factory.v2_probe("unfiltered", direct_payload)

    assert v1_empty.faiss_calls == 1
    assert calls[0] == {"k": 2, "fetch_k": 2}
    factory.v1_probe("unfiltered", direct_payload)
    assert calls[1] == {"k": 1, "fetch_k": 1}
    assert v2_empty.strategy == "empty"
    assert v2_empty.faiss_calls == 0
    assert v2_empty.faiss_candidates == 0
    assert v2_direct.strategy == "direct"
    assert factory.engine == "paired"
    assert dispatch_calls == [data_root]
    assert str(data_root) not in json.dumps(factory.environment)
    assert str(input_path) not in json.dumps(factory.environment)

    original_v1_reader = copied_benchmark_module.V1CompatibilityReader
    monkeypatch.setattr(
        copied_benchmark_module,
        "V1CompatibilityReader",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("V2 cold worker opened V1")
        ),
    )
    v2_only = create_factory(
        process_id="process-1-cold-v2",
        seed=20260716,
        engine="v2",
    )
    assert v2_only.engine == "v2"
    assert dispatch_calls == [data_root, data_root]
    assert v2_only.environment == factory.environment
    monkeypatch.setattr(
        copied_benchmark_module,
        "V1CompatibilityReader",
        original_v1_reader,
    )

    monkeypatch.setattr(
        dispatch_module,
        "CatalogRepository",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("V1 cold worker opened the native repository")
        ),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "load_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("V1 cold worker validated the native FAISS snapshot")
        ),
    )
    v1_only = create_factory(
        process_id="process-1-cold-v1",
        seed=20260716,
        engine="v1",
    )
    assert v1_only.engine == "v1"
    assert v1_only.environment == factory.environment
    reset_native_dispatchers()


def test_successor_factory_pins_both_revisions_in_one_checkpointed_catalog(
    tmp_path,
    monkeypatch,
):
    reset_native_dispatchers()
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())
    publish_candidate(materialize_candidate(plan, data_root), data_root)
    query_id = hashlib.sha256(b"opaque-query-1").hexdigest()
    specification = {
        "schema_version": 1,
        "kind": "v2_retrieval_query_vectors",
        "k": 1,
        "queries": [{"query_id": query_id, "vector": VECTORS[0].tolist()}],
        "workloads": {
            "unfiltered": {"scope": None},
            "empty": {"scope": {"empty": True}},
            "narrow": {"scope": {"file_names": ["a.pdf"]}},
            "broad": {"scope": {"report_types": ["company"]}},
            "near_universe": {
                "scope": {"report_types": ["company", "industry"]},
            },
            "prior_scope": {
                "scope": {"prior_scope": {"file_names": ["a.pdf"]}},
            },
        },
    }
    input_path = tmp_path / "successor-query-vectors.json"
    input_path.write_text(json.dumps(specification), encoding="utf-8")
    monkeypatch.setenv("V2_BENCHMARK_DATA_ROOT", str(data_root))
    monkeypatch.setenv("V2_BENCHMARK_INPUT", str(input_path))

    with pytest.raises(PerformanceEvidenceError, match="epoch-zero bridge"):
        create_factory(process_id="epoch-zero-only", seed=20260716)

    monkeypatch.setattr(
        copied_benchmark_module,
        "prime_native_dispatch",
        lambda _selection: (_ for _ in ()).throw(
            AssertionError("successor benchmark opened the live catalog reader")
        ),
    )
    factory = create_successor_factory(
        process_id="successor-pair",
        seed=20260716,
    )
    payload = factory.queries["unfiltered"][0].payload
    baseline = factory.v1_probe("unfiltered", payload)
    candidate = factory.v2_probe("unfiltered", payload)

    environment = factory.environment
    assert baseline.strategy == candidate.strategy == "direct"
    assert environment["benchmark_pair"] == (
        "native_predecessor_vs_native_successor"
    )
    assert environment["catalog_policy"] == (
        "shared_checkpointed_catalog_clone_pinned_revisions"
    )
    assert environment["baseline_snapshot_id"] != environment["candidate_snapshot_id"]
    assert environment["baseline_snapshot_id"] == plan.base_snapshot_id
    assert environment["candidate_snapshot_id"] == plan.snapshot_id
    assert environment["baseline_ntotal"] == 2
    assert environment["candidate_ntotal"] == len(plan.chunks)
    assert environment["write_epoch"] == 1
    assert environment["v1_fallback_open"] is False
    assert str(data_root) not in json.dumps(environment)
    assert str(input_path) not in json.dumps(environment)
    reset_native_dispatchers()


def test_pinned_pair_closes_a_created_repository_when_the_second_reader_fails(
    tmp_path,
    monkeypatch,
):
    data_root, _sources = _native_seed(tmp_path)
    catalog = data_root / "retrieval" / "v2" / "catalog.sqlite3"
    temporary = tmp_path / "benchmark-clone"

    class Repository:
        closed = False

        def close(self):
            self.closed = True

    repository = Repository()
    calls = 0

    def pinned_reader(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return copied_benchmark_module._PinnedSnapshotReader(
                repository=repository,
                reader=object(),
            )
        raise RuntimeError("second reader failed")

    def make_temporary(*_args, **_kwargs):
        temporary.mkdir()
        return str(temporary)

    monkeypatch.setattr(
        copied_benchmark_module.tempfile,
        "mkdtemp",
        make_temporary,
    )
    monkeypatch.setattr(
        copied_benchmark_module,
        "_pinned_snapshot_reader",
        pinned_reader,
    )

    with pytest.raises(RuntimeError, match="second reader failed"):
        copied_benchmark_module._pinned_snapshot_pair(
            catalog,
            data_root,
            predecessor_snapshot_id="1" * 64,
            active_snapshot_id="2" * 64,
            publication_generation=1,
            include_predecessor=True,
            include_active=True,
        )

    assert repository.closed is True
    assert not temporary.exists()


def test_pinned_pair_cleanup_attempts_every_close_after_one_close_fails(tmp_path):
    temporary = tmp_path / "benchmark-clone"
    temporary.mkdir()
    (temporary / "catalog.sqlite3").write_bytes(b"fixture")
    calls = []

    class Repository:
        def __init__(self, name, *, fail=False):
            self.name = name
            self.fail = fail

        def close(self):
            calls.append(self.name)
            if self.fail:
                raise RuntimeError("close failed")

    copied_benchmark_module._close_pinned_pair(
        (Repository("first", fail=True), Repository("second")),
        temporary,
    )

    assert calls == ["first", "second"]
    assert not temporary.exists()
