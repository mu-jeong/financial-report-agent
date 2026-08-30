from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from src.core import fixed_snapshot, release_assets
from src.core.operator_monitoring_service import ReleaseScopedMonitoringService


class _GitReleaseRegistry:
    def __init__(self, release_id: str) -> None:
        self.release_id = release_id
        self.queued_profile: dict[str, Any] | None = None

    def list_runs(self, *, issue_id: str) -> list[dict[str, Any]]:
        return []

    def get_case_by_contract(self, case_contract_id: str) -> dict[str, Any]:
        return {
            "case_contract_id": case_contract_id,
            "fixture_revision_id": "fixture-1",
            "fixed_snapshot_revision_id": "snapshot-1",
            "fixed_clock": "2026-08-30T00:00:00+00:00",
            "evidence_qualifier": "EXACT",
        }

    def get_fixture_revision(self, fixture_revision_id: str) -> dict[str, Any]:
        return {
            "fixture_revision_id": fixture_revision_id,
            "body": {"question": "올해 매출은?", "typed_checks": []},
        }

    def get_fixed_snapshot(self, revision_id: str) -> dict[str, Any]:
        return {
            "fixed_snapshot_revision_id": revision_id,
            "bundle_relpath": "fixed-snapshots/snapshot-1",
            "manifest": {"manifest_schema_version": 2},
        }

    def get_release_manifest(self, release_manifest_id: str) -> dict[str, Any]:
        assert release_manifest_id == self.release_id
        return {
            "release_manifest_id": self.release_id,
            "app_version": "0.6.2",
            "manifest_version": 2,
            "runtime_bundle_digest": "c" * 64,
            "bundle_relpath": f"releases/{self.release_id}",
            "manifest": {
                "schema_version": 2,
                "git_revision": "a" * 40,
                "build_digest": "b" * 64,
                "runner_contract_version": 1,
                "snapshot_reader_contract_version": 2,
            },
        }

    def queue_run(self, **values: Any) -> dict[str, Any]:
        self.queued_profile = dict(values["runtime_profile"])
        return {
            **values,
            "run_id": "run-git-release",
            "execution_status": "QUEUED",
            "runtime_profile": self.queued_profile,
        }

    def start_run(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "execution_status": "RUNNING",
            "runtime_profile": self.queued_profile,
        }

    def finish_run(
        self,
        run_id: str,
        *,
        execution_status: str,
        validity: str,
        artifact: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "execution_status": execution_status,
            "validity": validity,
            "runtime_profile": self.queued_profile,
            "artifact": dict(artifact),
        }


def test_service_rebuilds_missing_git_cache_and_executes_with_queued_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    release_id = release_assets.git_release_manifest_id("0.6.2", "a" * 40)
    registry = _GitReleaseRegistry(release_id)
    project_root = tmp_path / "project"
    project_root.mkdir()
    service = ReleaseScopedMonitoringService(
        registry,  # type: ignore[arg-type]
        managed_root=tmp_path / "managed",
        project_root=project_root,
        snapshot_availability=lambda _root, _revision: (
            fixed_snapshot.FixedSnapshotAvailability.AVAILABLE
        ),
    )
    runtime_profile = {
        "environment": {"SEARCH_TOP_K": 8},
        "snapshot_reader": {"manifest_schema_version": 2},
    }
    cache_rebuilt = False

    def inspect_release(_descriptor: release_assets.ReleaseDescriptor):
        return (
            release_assets.ReleaseAvailability.AVAILABLE
            if cache_rebuilt
            else release_assets.ReleaseAvailability.LOCAL_MISSING
        )

    def rebuild_release_cache_from_git(
        managed_root: Path,
        *,
        project_root: Path,
        descriptor: release_assets.ReleaseDescriptor,
        **_kwargs: Any,
    ) -> release_assets.ReleaseDescriptor:
        nonlocal cache_rebuilt
        assert managed_root == service.managed_root
        assert project_root == service.project_root
        cache_rebuilt = True
        return descriptor

    def execute_registered_release(
        _managed_root: Path,
        _descriptor: release_assets.ReleaseDescriptor,
        **kwargs: Any,
    ) -> SimpleNamespace:
        assert kwargs["runtime_profile"] == runtime_profile
        artifact_path = tmp_path / "runner-result.json"
        artifact_path.write_text(
            json.dumps(
                {
                    "runner_status": "SUCCEEDED",
                    "raw_answer": "fixture answer",
                    "evidence_refs": [],
                    "route_summary": {},
                    "runtime_profile": runtime_profile,
                    "latency_ms": 1,
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            returncode=0,
            artifact_path=artifact_path,
            artifact_digest="d" * 64,
            cleanup_warning=None,
        )

    monkeypatch.setattr(release_assets, "inspect_release", inspect_release)
    monkeypatch.setattr(
        release_assets,
        "rebuild_release_cache_from_git",
        rebuild_release_cache_from_git,
    )
    monkeypatch.setattr(
        release_assets,
        "execute_registered_release",
        execute_registered_release,
    )

    run = service.execute_run(
        issue_id="issue-1",
        case_contract_id="case-1",
        release_manifest_id=release_id,
        side="CANDIDATE",
        runtime_profile=runtime_profile,
    )

    assert cache_rebuilt is True
    assert registry.queued_profile == runtime_profile
    assert run["execution_status"] == "SUCCEEDED"
    assert run["validity"] == "VALID"


def test_service_rejects_a_secret_profile_before_persisting_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_id = release_assets.git_release_manifest_id("0.6.2", "a" * 40)
    registry = _GitReleaseRegistry(release_id)
    service = ReleaseScopedMonitoringService(
        registry,  # type: ignore[arg-type]
        managed_root=tmp_path / "managed",
        project_root=tmp_path / "project",
        snapshot_availability=lambda _root, _revision: (
            fixed_snapshot.FixedSnapshotAvailability.AVAILABLE
        ),
    )
    monkeypatch.setattr(
        release_assets,
        "inspect_release",
        lambda _descriptor: release_assets.ReleaseAvailability.AVAILABLE,
    )

    with pytest.raises(release_assets.ReleaseAssetError, match="sensitive key"):
        service.execute_run(
            issue_id="issue-1",
            case_contract_id="case-1",
            release_manifest_id=release_id,
            side="CANDIDATE",
            runtime_profile={
                "environment": {"OPENROUTER_API_KEY": "must-not-persist"}
            },
        )

    assert registry.queued_profile is None
