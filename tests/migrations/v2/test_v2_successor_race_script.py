from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.migrations.v2 import run_v2_first_successor_race
from src.configs import config
from src.core import embed_pipeline
from src.retrieval.recovery import RecoveryDisposition


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
    candidate = SimpleNamespace(
        build_id="build-a",
        snapshot_id="snapshot-a",
        report_count=2,
        parent_count=3,
        chunk_count=4,
        evidence_manifest_sha256="11" * 32,
    )
    publication = SimpleNamespace(
        publication_id="publication-a",
        publication_generation=2,
        write_epoch=1,
        active_snapshot_id="snapshot-a",
        predecessor_snapshot_id="snapshot-seed",
        v1_fallback_open=False,
        checkpoint_sha256="22" * 32,
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

    def fake_materialize(plan, root, *, writer_lease):
        assert writer_lease is lease
        events.append("materialize")
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
    ]
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["passed"] is True
    assert evidence["publication"]["write_epoch"] == 1
    assert str(data_root) not in output.read_text(encoding="utf-8")
    assert str(source_install) not in output.read_text(encoding="utf-8")


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
