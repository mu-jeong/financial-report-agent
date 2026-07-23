from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.migrations.v2 import assemble_v2_release_gate as release_gate
from scripts.migrations.v2 import run_v2_first_successor_race
from src.configs import config
from src.core import embed_pipeline
from src.retrieval.build_service import materialize_candidate, publish_candidate
from src.retrieval.recovery import RecoveryDisposition
from tests.retrieval.test_retrieval_build_service import (
    DeterministicEmbeddings,
    _native_seed,
    _prepare,
)


def test_successor_race_script_holds_one_build_to_publish_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data_root = tmp_path / "data-root"
    data_root.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()
    source_install = tmp_path / "source-install"
    packaged_install = tmp_path / "packaged-install"
    source_install.mkdir()
    packaged_install.mkdir()
    output = tmp_path / "race.json"
    events: list[str] = []
    baseline = {"kind": "baseline"}
    build_id = "10" * 32
    snapshot_id = "20" * 32
    publication_id = "30" * 32
    seed_snapshot_id = "40" * 32
    candidate_manifest_sha256 = "50" * 32
    candidate = SimpleNamespace(
        build_id=build_id,
        snapshot_id=snapshot_id,
        publication_id=publication_id,
        report_count=2,
        parent_count=3,
        chunk_count=4,
        evidence_manifest_sha256=candidate_manifest_sha256,
    )
    publication = SimpleNamespace(
        publication_id=publication_id,
        publication_generation=2,
        write_epoch=1,
        active_snapshot_id=snapshot_id,
        predecessor_snapshot_id=seed_snapshot_id,
        v1_fallback_open=False,
        checkpoint_sha256="60" * 32,
    )

    monkeypatch.setattr(config, "DB_PATH", str(data_root / "reports.db"))
    monkeypatch.setattr(config, "SAVE_DIR", str(sources))
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "model-a")
    monkeypatch.setattr(config, "EXTRACTION_ENGINE", "extractor-a")
    monkeypatch.setattr(config, "PARENT_CHUNK_SIZE", 40)
    monkeypatch.setattr(config, "CHILD_CHUNK_SIZE", 20)
    monkeypatch.setattr(
        embed_pipeline,
        "build_embeddings_fn",
        lambda: events.append("embeddings") or object(),
    )
    monkeypatch.setattr(
        run_v2_first_successor_race,
        "capture_epoch_zero_installed_baseline",
        lambda *_args, **_kwargs: events.append("baseline") or baseline,
    )

    class FakeReconciler:
        def __init__(self, root):
            assert Path(root) == data_root

        def reconcile(self, *, writer_lease):
            assert writer_lease is lease
            events.append("reconcile")
            return SimpleNamespace(disposition=RecoveryDisposition.ACTIVE)

    def fake_prepare(*_args, **kwargs):
        events.append("prepare")
        assert kwargs["allow_degraded_forward_recovery"] is True
        return "plan"

    def fake_materialize(plan, root, *, writer_lease=None):
        if writer_lease is lease:
            events.append("materialize")
        else:
            assert writer_lease is not None
            events.append("replay")
        assert plan == "plan"
        assert Path(root) == data_root
        return candidate

    def fake_publish(result, root, **kwargs):
        events.append("publish")
        assert result is None
        assert Path(root) == data_root
        assert kwargs["installed_baseline"] is baseline
        events.append("lock-enter")
        built_candidate = kwargs["candidate_factory"](lease)
        events.append("lock-exit")
        return SimpleNamespace(
            candidate=built_candidate,
            publication=publication,
            evidence={"passed": True, "release_eligible": True},
        )

    monkeypatch.setattr(
        run_v2_first_successor_race,
        "StartupReconciler",
        FakeReconciler,
    )
    monkeypatch.setattr(
        run_v2_first_successor_race,
        "prepare_full_corpus_build",
        fake_prepare,
    )
    monkeypatch.setattr(
        run_v2_first_successor_race,
        "materialize_candidate",
        fake_materialize,
    )
    monkeypatch.setattr(
        run_v2_first_successor_race,
        "publish_candidate_with_launcher_race",
        fake_publish,
    )
    def fake_replay_publish(replayed, root, *, writer_lease):
        assert replayed is candidate
        assert Path(root) == data_root
        assert writer_lease is not None
        events.append("replay-publication")
        return publication

    monkeypatch.setattr(
        run_v2_first_successor_race,
        "publish_candidate",
        fake_replay_publish,
    )

    lease = object()

    result = run_v2_first_successor_race.main(
        [
            "--source-install",
            str(source_install),
            "--packaged-install",
            str(packaged_install),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert events == [
        "baseline",
        "embeddings",
        "publish",
        "lock-enter",
        "reconcile",
        "prepare",
        "materialize",
        "lock-exit",
        "replay",
        "replay-publication",
    ]
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["passed"] is True
    assert evidence["kind"] == "v2_first_successor_execution_replay"
    assert evidence["publication"]["write_epoch"] == 1
    assert evidence["replay"] == {
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "candidate_reused": True,
        "embedding_api_calls": 0,
        "reason": (
            "completed deterministic candidate and publication reused "
            "without embedding provider calls"
        ),
        "source_publication_id": publication_id,
    }
    assert str(data_root) not in output.read_text(encoding="utf-8")
    assert str(source_install) not in output.read_text(encoding="utf-8")

    launcher_calls: list[dict] = []

    def fake_validate_launcher(
        value,
        *,
        seed_snapshot_id,
        active_snapshot_id,
        publication_id,
        publication_generation,
        write_epoch,
    ):
        launcher_calls.append(value)
        assert seed_snapshot_id == expected_seed_snapshot_id
        assert active_snapshot_id == snapshot_id
        assert publication_id == expected_publication_id
        assert publication_generation == 2
        assert write_epoch == 1
        return {
            "active_runtime_identity": {"mode": "native"},
            "launcher_layout_sha256": "70" * 32,
        }

    expected_seed_snapshot_id = seed_snapshot_id
    expected_publication_id = publication_id
    monkeypatch.setattr(
        release_gate,
        "_validate_launcher_race",
        fake_validate_launcher,
    )
    summary = release_gate._validate_successor_race(
        evidence,
        output,
        {
            "conversion": {
                "validation": {
                    "snapshot_id": seed_snapshot_id,
                }
            }
        },
    )
    assert launcher_calls == [evidence["launcher_race"]]
    assert summary["candidate_manifest_sha256"] == candidate_manifest_sha256
    mutations = (
        lambda value: value.update(kind="v2_first_successor_execution"),
        lambda value: value.pop("replay"),
        lambda value: value["replay"].update(unexpected=True),
        lambda value: value["replay"].update(candidate_manifest_sha256="80" * 32),
        lambda value: value["replay"].update(candidate_reused=False),
        lambda value: value["replay"].update(embedding_api_calls=1),
        lambda value: value["replay"].update(embedding_api_calls=False),
        lambda value: value["replay"].update(reason=" "),
        lambda value: value["replay"].update(source_publication_id="90" * 32),
    )
    for mutate in mutations:
        tampered = json.loads(json.dumps(evidence))
        mutate(tampered)
        with pytest.raises(release_gate.ReleaseGateError, match="successor"):
            release_gate._validate_successor_race(
                tampered,
                output,
                {
                    "conversion": {
                        "validation": {
                            "snapshot_id": seed_snapshot_id,
                        }
                    }
                },
            )


def test_successor_replay_rejects_a_different_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = SimpleNamespace(
        publication_id="10" * 32,
        evidence_manifest_sha256="20" * 32,
    )
    different = SimpleNamespace(
        publication_id="10" * 32,
        evidence_manifest_sha256="30" * 32,
    )
    counter = run_v2_first_successor_race._EmbeddingCallCounter(object())
    monkeypatch.setattr(
        run_v2_first_successor_race,
        "materialize_candidate",
        lambda *_args, **_kwargs: different,
    )

    with pytest.raises(RuntimeError, match="did not reuse"):
        run_v2_first_successor_race._replay_candidate(
            "plan",
            candidate,
            SimpleNamespace(publication_id=candidate.publication_id),
            tmp_path,
            counter,
        )


def test_successor_replay_rejects_embedding_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeEmbeddings:
        def embed_documents(self, texts):
            assert texts == ["unexpected"]
            return [[0.0]]

    candidate = SimpleNamespace(
        publication_id="10" * 32,
        evidence_manifest_sha256="20" * 32,
    )
    counter = run_v2_first_successor_race._EmbeddingCallCounter(FakeEmbeddings())

    def fake_materialize(*_args, **_kwargs):
        counter.embed_documents(["unexpected"])
        return candidate

    monkeypatch.setattr(
        run_v2_first_successor_race,
        "materialize_candidate",
        fake_materialize,
    )

    with pytest.raises(RuntimeError, match="embedding provider"):
        run_v2_first_successor_race._replay_candidate(
            "plan",
            candidate,
            SimpleNamespace(publication_id=candidate.publication_id),
            tmp_path,
            counter,
        )


def test_successor_replay_rejects_a_different_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = SimpleNamespace(
        publication_id="10" * 32,
        evidence_manifest_sha256="20" * 32,
    )
    publication = SimpleNamespace(publication_id=candidate.publication_id)
    counter = run_v2_first_successor_race._EmbeddingCallCounter(object())
    monkeypatch.setattr(
        run_v2_first_successor_race,
        "materialize_candidate",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        run_v2_first_successor_race,
        "publish_candidate",
        lambda *_args, **_kwargs: SimpleNamespace(publication_id="30" * 32),
    )

    with pytest.raises(RuntimeError, match="completed publication"):
        run_v2_first_successor_race._replay_candidate(
            "plan",
            candidate,
            publication,
            tmp_path,
            counter,
        )


def test_successor_replay_reuses_a_fully_published_real_candidate(
    tmp_path: Path,
):
    data_root, sources = _native_seed(tmp_path)
    counter = run_v2_first_successor_race._EmbeddingCallCounter(
        DeterministicEmbeddings()
    )
    plan = _prepare(data_root, sources, counter)
    candidate = materialize_candidate(plan, data_root)
    publication = publish_candidate(candidate, data_root)
    calls_before_replay = counter.calls

    replay = run_v2_first_successor_race._replay_candidate(
        plan,
        candidate,
        publication,
        data_root,
        counter,
    )

    assert replay["candidate_reused"] is True
    assert replay["embedding_api_calls"] == 0
    assert replay["candidate_manifest_sha256"] == candidate.evidence_manifest_sha256
    assert replay["source_publication_id"] == publication.publication_id
    assert counter.calls == calls_before_replay


@pytest.mark.parametrize(
    "arguments",
    (
        ["--workers", "1"],
        ["--timeout-seconds", "0"],
    ),
)
def test_successor_race_script_rejects_unsafe_limits(
    tmp_path: Path,
    arguments: list[str],
):
    base = [
        "--source-install",
        str(tmp_path / "source"),
        "--packaged-install",
        str(tmp_path / "package"),
        "--output",
        str(tmp_path / "output.json"),
    ]
    with pytest.raises(SystemExit, match="2"):
        run_v2_first_successor_race.main(base + arguments)
