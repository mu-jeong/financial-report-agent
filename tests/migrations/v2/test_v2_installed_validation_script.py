from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.migrations.v2 import run_v2_installed_validation as installed_run
from src.migrations.v2.validation.installed_validation import (
    REQUIRED_DISTRIBUTIONS,
)
from src.migrations.v2.validation.release_transitions import (
    ReleaseTransitionError,
    validate_release_transition_evidence,
)


def _runtime(
    *,
    snapshot: str,
    predecessor: str | None,
    generation: int,
    epoch: int,
    degraded: bool,
    write_enabled: bool,
) -> dict[str, object]:
    return {
        "active_snapshot_id": snapshot,
        "predecessor_snapshot_id": predecessor,
        "publication_generation": generation,
        "write_epoch": epoch,
        "v1_fallback_open": False,
        "degraded": degraded,
        "write_enabled": write_enabled,
    }


def _transition_evidence() -> dict[str, object]:
    initial = _runtime(
        snapshot="c" * 64,
        predecessor="a" * 64,
        generation=2,
        epoch=1,
        degraded=False,
        write_enabled=True,
    )
    degraded = _runtime(
        snapshot="a" * 64,
        predecessor=None,
        generation=3,
        epoch=1,
        degraded=True,
        write_enabled=False,
    )
    forward = _runtime(
        snapshot="f" * 64,
        predecessor="a" * 64,
        generation=4,
        epoch=2,
        degraded=False,
        write_enabled=True,
    )
    final = _runtime(
        snapshot="9" * 64,
        predecessor="f" * 64,
        generation=5,
        epoch=3,
        degraded=False,
        write_enabled=True,
    )
    event_names = [
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
    ]
    ready = {
        "build_state": "ready",
        "snapshot_state": "ready",
        "running_publications": 0,
    }
    return {
        "schema_version": 2,
        "kind": "v2_copied_install_release_transitions",
        "passed": True,
        "fixture_only": False,
        "run_id": "release-transition-run",
        "started_at": "2026-07-18T00:00:00Z",
        "completed_at": "2026-07-18T00:01:00Z",
        "dedicated_copy": True,
        "protected_root_unchanged": True,
        "copy_proof": {
            "dedicated_catalog_logical_sha256": "1" * 64,
            "protected_catalog_logical_sha256_before": "1" * 64,
            "protected_catalog_logical_sha256_after": "1" * 64,
            "protected_tree_sha256_before": "2" * 64,
            "protected_tree_sha256_after": "2" * 64,
            "source_tree_sha256_before": "3" * 64,
            "source_tree_sha256_after": "3" * 64,
            "query_spec_sha256": "e" * 64,
            "initial_snapshot_sha256": {
                "active": "d" * 64,
                "predecessor": "b" * 64,
            },
        },
        "event_sequence": [
            {
                "sequence": index,
                "event": name,
                "recorded_at": "2026-07-18T00:00:00Z",
            }
            for index, name in enumerate(event_names, 1)
        ],
        "initial": initial,
        "recovery": {
            "before": initial,
            "after": degraded,
            "corrupted_snapshot_id": "c" * 64,
            "corrupted_snapshot_sha256_before": "d" * 64,
            "corrupted_snapshot_sha256_after": "7" * 64,
            "recovery_disposition": "predecessor_degraded",
            "replay_disposition": "active",
            "failed_snapshot_state": "failed",
        },
        "forward_recovery": {
            "before": degraded,
            "after": forward,
            "candidate_snapshot_id": "f" * 64,
            "embedding": {
                "provider_calls": 0,
                "validated_replay_calls": 1,
                "validated_text_count": 10,
            },
        },
        "lease_gc": {
            "leased_snapshot_id": "a" * 64,
            "lease_acquired_before_forward_publication": True,
            "blocked_candidate_snapshot_id": "9" * 64,
            "publication_blocked_while_leased": True,
            "blocked_error": "PublicationError",
            "blocked_error_sha256": "8" * 64,
            "candidate_state_before": ready,
            "candidate_state_after": ready,
            "runtime_after_block": forward,
            "lease_released": True,
            "retired_snapshot_id": "a" * 64,
            "retired_snapshot_state": "garbage_collected",
            "retired_snapshot_deleted": True,
            "validated_replay_calls_total": 2,
        },
        "gate_d_search": {
            "query_id": "synthetic-skt-successor-query",
            "query_text_sha256": "4" * 64,
            "query_vector_sha256": "5" * 64,
            "query_spec_sha256": "e" * 64,
            "expected_report_uid": "3" * 64,
            "top_report_uid": "3" * 64,
            "top_rank": 1,
            "citation_complete": True,
            "citation_sha256": "6" * 64,
            "query_generation": {
                "provider": "openrouter",
                "model": "baai/bge-m3",
                "input_type": "search_query",
                "provider_calls": 1,
                "attestation_sha256": "7" * 64,
            },
            "snapshot_id": "9" * 64,
            "publication_generation": 5,
        },
        "final": final,
    }


def _baseline() -> dict[str, object]:
    transition = _transition_evidence()
    runtime = {"mode": "native", **transition["final"]}
    return {
        "runtime_identity": runtime,
        "catalog_sha256": "1" * 64,
        "catalog_logical_sha256": "2" * 64,
        "snapshot_sha256": "3" * 64,
        "snapshots": {
            "active": {
                "snapshot_id": "9" * 64,
                "relative_path": "retrieval/v2/snapshots/active.faiss",
                "sha256": "3" * 64,
                "size_bytes": 100,
                "dimension": 2,
                "metric": "l2",
                "ntotal": 4,
            },
            "predecessor": {
                "snapshot_id": "f" * 64,
                "relative_path": "retrieval/v2/snapshots/predecessor.faiss",
                "sha256": "4" * 64,
                "size_bytes": 90,
                "dimension": 2,
                "metric": "l2",
                "ntotal": 3,
            },
        },
        "writer_lock": False,
        "staging_entries": 0,
    }


def test_transition_validator_is_exact_and_returns_input_integrity() -> None:
    evidence = _transition_evidence()

    summary = validate_release_transition_evidence(evidence)

    assert summary["final_runtime_identity"] == evidence["final"]
    assert summary["protected_tree_sha256_after"] == "2" * 64
    assert summary["source_tree_sha256_after"] == "3" * 64
    invalid = json.loads(json.dumps(evidence))
    invalid["unexpected"] = True
    with pytest.raises(ReleaseTransitionError, match="fields"):
        validate_release_transition_evidence(invalid)


def test_environment_fingerprint_uses_correct_pymupdf_name_and_detects_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installs: dict[str, Path] = {}
    for label in ("source-default", "packaged-default"):
        root = tmp_path / label
        python = root / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"same-python")
        installs[label] = root

    packages = {name: "1.0" for name in REQUIRED_DISTRIBUTIONS}
    assert "PyMuPDF" in packages
    assert "ayMuPDF" not in packages

    def completed(*_args, **_kwargs):
        payload = {
            "status": "ok",
            "python_version": "3.11.9",
            "implementation": "CPython",
            "packages": packages,
        }
        return SimpleNamespace(
            returncode=0,
            stdout=(json.dumps(payload) + "\n").encode(),
            stderr=b"",
        )

    monkeypatch.setattr(installed_run.subprocess, "run", completed)
    captured = installed_run._capture_installed_environments(
        installs,
        timeout_seconds=30,
    )
    assert captured["source-default"]["semantic_sha256"] == captured[
        "packaged-default"
    ]["semantic_sha256"]

    expected = json.loads(json.dumps(captured))
    expected["packaged-default"]["packages"]["numpy"] = "2.0"
    with pytest.raises(installed_run.InstalledValidationRunError, match="fingerprint"):
        installed_run._validate_installed_environments(
            installs,
            expected,
            timeout_seconds=30,
        )


def test_run_validation_executes_four_guards_and_two_distinct_probes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = _baseline()
    installs = {
        "source-default": tmp_path / "source",
        "packaged-default": tmp_path / "package",
    }
    for root in installs.values():
        root.mkdir()
    calls: list[tuple[str, bool]] = []

    def guard(label, _root, _anchor, *, write, timeout):
        assert timeout == 30
        calls.append((label, write))
        return {
            "install": label,
            "write_guard": write,
            "duration_ns": 1,
            "output_sha256": "8" * 64,
            "runtime_identity": baseline["runtime_identity"],
        }

    def probe(label, _root, _data_root, _query_spec, *, samples, timeout):
        assert samples == 5
        assert timeout == 30
        return {
            "install": label,
            "runtime_identity": {
                "active_snapshot_id": "9" * 64,
                "publication_generation": 5,
            },
        }

    monkeypatch.setattr(installed_run, "_validate_install_layouts", lambda *_: {})
    monkeypatch.setattr(installed_run, "_run_guard", guard)
    monkeypatch.setattr(installed_run, "_run_installed_probe", probe)
    monkeypatch.setattr(installed_run, "_capture_baseline", lambda _root: baseline)

    result = installed_run._run_validation(
        data_root=tmp_path,
        installs=installs,
        baseline=baseline,
        timeout_seconds=30,
        search_samples=5,
        expected_layout_hash="7" * 64,
        query_spec=tmp_path / "query.json",
    )

    assert calls == [
        ("source-default", False),
        ("source-default", True),
        ("packaged-default", False),
        ("packaged-default", True),
    ]
    assert [probe["install"] for probe in result["search_probes"]] == [
        "source-default",
        "packaged-default",
    ]


def test_layout_and_retained_artifact_drift_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    installs = {"source-default": tmp_path / "source"}
    monkeypatch.setattr(
        installed_run,
        "_install_layout_hashes",
        lambda _installs: {"source-default": "changed"},
    )
    with pytest.raises(installed_run.InstalledValidationRunError, match="layout"):
        installed_run._validate_install_layouts(installs, "expected")

    query = tmp_path / "query.json"
    transition = tmp_path / "transition.json"
    query.write_text("query", encoding="utf-8")
    transition.write_text("transition", encoding="utf-8")
    paths = {"query": query, "transition": transition}
    expected = {name: installed_run._sha256_file(path) for name, path in paths.items()}
    query.write_text("changed", encoding="utf-8")
    with pytest.raises(installed_run.InstalledValidationRunError, match="hash"):
        installed_run._validate_artifact_hashes(paths, expected)


def test_main_seals_one_shot_evidence_without_duration_or_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "dedicated"
    protected_root = tmp_path / "protected"
    source_pdfs = tmp_path / "pdfs"
    source_install = tmp_path / "source-install"
    packaged_install = tmp_path / "packaged-install"
    for path in (
        data_root,
        protected_root,
        source_pdfs,
        source_install,
        packaged_install,
    ):
        path.mkdir()
    query_path = tmp_path / "query.json"
    query_path.write_text('{"query":true}\n', encoding="utf-8")
    transition = _transition_evidence()
    query_sha256 = installed_run._sha256_file(query_path)
    transition["copy_proof"]["query_spec_sha256"] = query_sha256
    transition["gate_d_search"]["query_spec_sha256"] = query_sha256
    baseline = _baseline()
    environments = {
        label: {
            "python_version": "3.11.9",
            "implementation": "CPython",
            "packages": {name: "1.0" for name in REQUIRED_DISTRIBUTIONS},
            "python_executable_sha256": "8" * 64,
            "semantic_sha256": "7" * 64,
        }
        for label in ("source-default", "packaged-default")
    }
    validation = {
        "recorded_at": "2026-07-18T00:02:00Z",
        "passed": True,
        "guard_outcomes": [],
        "search_probes": [],
        "runtime_identity_sha256": "1" * 64,
        "catalog_sha256": baseline["catalog_sha256"],
        "snapshot_sha256": baseline["snapshot_sha256"],
    }
    monkeypatch.setattr(installed_run, "_is_admin", lambda: False)
    monkeypatch.setattr(
        installed_run,
        "_validated_install_roots",
        lambda _roots: {
            "source-default": source_install,
            "packaged-default": packaged_install,
        },
    )
    monkeypatch.setattr(
        installed_run,
        "_install_layout_hashes",
        lambda _roots: {
            "source-default": "6" * 64,
            "packaged-default": "6" * 64,
        },
    )
    monkeypatch.setattr(
        installed_run,
        "_capture_installed_environments",
        lambda *_args, **_kwargs: environments,
    )
    monkeypatch.setattr(
        installed_run,
        "_validate_installed_environments",
        lambda *_args, **_kwargs: environments,
    )
    monkeypatch.setattr(installed_run, "execute_release_transitions", lambda *_: transition)
    monkeypatch.setattr(installed_run, "_capture_baseline", lambda _root: baseline)
    monkeypatch.setattr(installed_run, "_run_validation", lambda **_kwargs: validation)
    monkeypatch.setattr(installed_run, "_validate_install_layouts", lambda *_: {})
    monkeypatch.setattr(
        installed_run,
        "capture_tree_manifest",
        lambda path: {
            "sha256": (
                "2" * 64 if Path(path) == protected_root else "3" * 64
            )
        },
    )
    output = tmp_path / "installed-validation.json"
    transition_output = tmp_path / "release-transitions.json"

    result = installed_run.main(
        [
            "--data-root",
            str(data_root),
            "--protected-root",
            str(protected_root),
            "--source-pdfs",
            str(source_pdfs),
            "--query-spec",
            str(query_path),
            "--source-install",
            str(source_install),
            "--packaged-install",
            str(packaged_install),
            "--transition-output",
            str(transition_output),
            "--output",
            str(output),
            "--search-samples",
            "5",
        ]
    )

    assert result == 0
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["kind"] == "v2_installed_validation"
    assert evidence["fixture_only"] is False
    assert evidence["release_eligible"] is False
    assert "duration_seconds" not in evidence
    assert "interval_seconds" not in evidence
    assert "state" not in evidence
    assert output.stat().st_mode & stat.S_IWRITE == 0


def test_cli_rejects_removed_duration_and_state_options() -> None:
    with pytest.raises(SystemExit):
        installed_run.main(["--duration-hours", "72"])
    with pytest.raises(SystemExit):
        installed_run.main(["--state", "state.json"])


def test_writable_paths_must_be_distinct_and_outside_inputs(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    with pytest.raises(installed_run.InstalledValidationRunError, match="outside"):
        installed_run._validate_writable_paths(
            {"output": protected / "evidence.json"},
            {"protected": protected},
        )
    duplicate = tmp_path / "same.json"
    with pytest.raises(installed_run.InstalledValidationRunError, match="distinct"):
        installed_run._validate_distinct_paths(
            {"output": duplicate, "transition": duplicate}
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_immutable_writer_rejects_reparse_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "unsafe" / "evidence.json"
    target.parent.mkdir()
    monkeypatch.setattr(installed_run, "_path_has_reparse_component", lambda _p: True)
    with pytest.raises(installed_run.InstalledValidationRunError, match="unsafe"):
        installed_run._write_immutable_json(target, {"passed": True})
