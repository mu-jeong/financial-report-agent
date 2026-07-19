from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.migrations.v2.validation import launcher_race
from src.retrieval.bootstrap import inspect_runtime
from src.retrieval.build_service import materialize_candidate
from tests.retrieval.test_retrieval_build_service import (
    DeterministicEmbeddings,
    _native_seed,
    _prepare,
)


def _installed_wave(
    identity: tuple[object, ...],
    *,
    epoch_zero: bool = False,
    blocked: bool = False,
) -> list[dict[str, object]]:
    wave = []
    for label in launcher_race._INSTALL_LABELS:
        for surface, _command in launcher_race._surface_commands(Path(".")):
            disposition = "blocked" if blocked else "selected"
            runtime_identity = (
                None
                if blocked
                else launcher_race._identity_mapping(identity)
            )
            if epoch_zero and surface == "update_guard":
                disposition = "blocked"
                runtime_identity = None
            wave.append(
                {
                    "label": label,
                    "surface": surface,
                    "exit_code": 2 if disposition == "blocked" else 0,
                    "duration_ns": 1,
                    "output_sha256": "00" * 32,
                    "disposition": disposition,
                    "runtime_identity": runtime_identity,
                }
            )
    return wave


def _install_layout(root: Path) -> Path:
    for relative in launcher_race._LAUNCHER_LAYOUT:
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    python = root / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture")
    return root


def test_launcher_race_proves_epoch_zero_fail_closed_and_successor_writable(
    tmp_path: Path,
):
    data_root, sources = _native_seed(tmp_path)

    def build_candidate(writer_lease):
        assert (data_root / "retrieval" / "v2" / "writer.lock").is_file()
        plan = _prepare(
            data_root,
            sources,
            DeterministicEmbeddings(),
            writer_lease=writer_lease,
        )
        return materialize_candidate(
            plan,
            data_root,
            writer_lease=writer_lease,
        )

    raced = launcher_race.publish_candidate_with_launcher_race(
        None,
        data_root,
        candidate_factory=build_candidate,
        worker_count=4,
    )

    assert raced.publication.write_epoch == 1
    assert raced.evidence["passed"] is True
    assert raced.evidence["release_eligible"] is False
    assert raced.evidence["before"]["write_epoch"] == 0
    assert raced.evidence["before"]["write_enabled"] is False
    assert raced.evidence["after"]["write_epoch"] == 1
    assert raced.evidence["after"]["write_enabled"] is True
    counts = raced.evidence["observation_counts"]
    assert counts["launcher:selected"] >= 2
    assert counts["updater:fail_closed"] >= 1
    assert counts["updater:selected"] >= 1


def test_launcher_race_connects_independent_installs_to_the_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data_root, sources = _native_seed(tmp_path)
    plan = _prepare(data_root, sources, DeterministicEmbeddings())
    candidate = materialize_candidate(plan, data_root)
    before = inspect_runtime(data_root / "reports.db", data_root=data_root)
    before_identity = launcher_race._runtime_identity(before)
    roots = {
        "source-default": tmp_path / "source-install",
        "packaged-default": tmp_path / "packaged-install",
    }
    monkeypatch.setattr(
        launcher_race,
        "_validated_install_roots",
        lambda _roots: roots,
    )
    layout_hashes = {label: "aa" * 32 for label in launcher_race._INSTALL_LABELS}
    monkeypatch.setattr(
        launcher_race,
        "_install_layout_hashes",
        lambda _roots: layout_hashes,
    )
    calls = 0

    def fake_run(_roots, _anchor, *, timeout_seconds):
        nonlocal calls
        assert timeout_seconds > 0
        calls += 1
        current = inspect_runtime(data_root / "reports.db", data_root=data_root)
        return _installed_wave(launcher_race._runtime_identity(current))

    monkeypatch.setattr(launcher_race, "_run_installed_wave", fake_run)
    monkeypatch.setattr(
        launcher_race,
        "_start_installed_wave",
        lambda _roots, _anchor: [{"transition": True}],
    )
    finish_calls = 0

    def fake_finish(_processes, *, timeout_seconds):
        nonlocal finish_calls
        assert timeout_seconds > 0
        finish_calls += 1
        if finish_calls == 1:
            return _installed_wave(before_identity, blocked=True)
        current = inspect_runtime(data_root / "reports.db", data_root=data_root)
        return _installed_wave(launcher_race._runtime_identity(current))

    monkeypatch.setattr(launcher_race, "_finish_installed_wave", fake_finish)

    baseline = {
        "schema_version": 1,
        "kind": "v2_epoch_zero_installed_launcher_baseline",
        "runtime_identity": launcher_race._identity_mapping(before_identity),
        "launcher_layout_sha256": layout_hashes,
        "wave": _installed_wave(before_identity, epoch_zero=True),
    }
    raced = launcher_race.publish_candidate_with_launcher_race(
        candidate,
        data_root,
        install_roots=roots,
        installed_baseline=baseline,
        worker_count=4,
    )

    assert calls == 1
    assert raced.evidence["release_eligible"] is True
    assert raced.evidence["installed_process_waves"]["before"]
    assert raced.evidence["installed_process_waves"]["race"]
    assert raced.evidence["installed_process_waves"]["after_concurrent"]
    assert raced.evidence["installed_process_waves"]["after"]


def test_launcher_race_requires_distinct_complete_install_roots(tmp_path: Path):
    source = _install_layout(tmp_path / "source")
    packaged = _install_layout(tmp_path / "packaged")

    validated = launcher_race._validated_install_roots(
        {"source-default": source, "packaged-default": packaged}
    )

    assert validated == {
        "source-default": source.resolve(),
        "packaged-default": packaged.resolve(),
    }
    with pytest.raises(launcher_race.LauncherRaceError, match="distinct"):
        launcher_race._validated_install_roots(
            {"source-default": source, "packaged-default": source}
        )


def test_launcher_process_outcome_redacts_output_and_validates_identity():
    payload = {
        "status": "ok",
        "mode": "native",
        "active_snapshot_id": "snapshot-a",
        "publication_generation": 2,
        "write_epoch": 1,
        "v1_fallback_open": False,
        "degraded": False,
        "write_enabled": True,
    }
    raw = (json.dumps(payload) + "\nsecret-path").encode()

    outcome = launcher_race._process_outcome(
        {
            "label": "source-default",
            "surface": "launcher_guard",
            "started_ns": time.perf_counter_ns(),
        },
        0,
        raw,
        None,
    )

    assert outcome["disposition"] == "selected"
    assert outcome["runtime_identity"]["write_epoch"] == 1
    assert "secret-path" not in json.dumps(outcome)
