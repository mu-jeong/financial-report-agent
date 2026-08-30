from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Mapping

import pytest

from src.core import fixed_snapshot, release_assets
from src.core.operator_monitoring import MonitoringRegistry, MonitoringRegistryError
from src.core.operator_monitoring_service import (
    MonitoringServiceError,
    ReleaseScopedMonitoringService,
)
from src.core.operator_monitoring_service import reproduction_seed_from_raw_report


def _register_fake_release(
    service: ReleaseScopedMonitoringService,
    source_root: Path,
    *,
    app_version: str,
    answer: str,
    latency_ms: float,
) -> release_assets.ReleaseDescriptor:
    app_source = source_root / f"app-{app_version}"
    runtime_source = source_root / f"runtime-{app_version}"
    app_source.mkdir(parents=True)
    runtime_source.mkdir(parents=True)
    (app_source / "answer.txt").write_text(answer, encoding="utf-8")
    (app_source / "runner.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "app_root = Path(os.environ['FINANCE_LLM_RELEASE_APP_ROOT'])\n"
        "bundle_root = Path(os.environ['FINANCE_LLM_RELEASE_BUNDLE_ROOT'])\n"
        "input_path = Path(os.environ['FINANCE_LLM_RUN_INPUT_PATH'])\n"
        "payload = json.loads(input_path.read_text(encoding='utf-8'))\n"
        "profile = json.loads((bundle_root / 'runtime-profile.json').read_text(encoding='utf-8'))\n"
        "artifact = {\n"
        "    'schema_version': 1,\n"
        "    'runner_status': 'SUCCEEDED',\n"
        "    'raw_answer': (app_root / 'answer.txt').read_text(encoding='utf-8'),\n"
        "    'evidence_refs': [{\n"
        "        'role': 'CITED',\n"
        "        'source_uid': 'a' * 64,\n"
        "        'source_sha256': 'a' * 64,\n"
        "        'chunk_uid': 'c' * 64,\n"
        "        'rank': 1,\n"
        "    }],\n"
        "    'route_summary': {\n"
        "        'route': 'vectordb',\n"
        "        'case_contract_id': payload['case_contract_id'],\n"
        "        'fixed_snapshot_revision_id': payload['fixed_snapshot_revision_id'],\n"
        "    },\n"
        "    'runtime_profile': profile,\n"
        f"    'latency_ms': {latency_ms!r},\n"
        "}\n"
        "Path(os.environ['FINANCE_LLM_RUN_ARTIFACT_PATH']).write_text(\n"
        "    json.dumps(artifact, ensure_ascii=False), encoding='utf-8'\n"
        ")\n",
        encoding="utf-8",
    )
    (runtime_source / "runtime.txt").write_text(
        "isolated fixture runtime", encoding="utf-8"
    )
    stage = release_assets.prepare_release_stage(
        service.managed_root,
        app_source=app_source,
        runtime_source=runtime_source,
        runtime_profile={
            "generation_model": f"fixture-model-{app_version}",
            "temperature": 0,
        },
        runner={
            "contract_version": 1,
            "command": ["{python}", "{app_root}/runner.py"],
            "artifact_relative_path": "result.json",
        },
        app_version=app_version,
        git_revision=(
            release_assets.FIRST_BASELINE_GIT_REVISION
            if app_version == release_assets.FIRST_BASELINE_VERSION
            else f"fixture-git-{app_version}"
        ),
    )
    descriptor = release_assets.register_release_stage(
        service.managed_root,
        stage,
        expected_tag_version=f"v{app_version}",
        expected_git_revision=(
            release_assets.FIRST_BASELINE_GIT_REVISION
            if app_version == release_assets.FIRST_BASELINE_VERSION
            else f"fixture-git-{app_version}"
        ),
    )
    service.register_release(descriptor, release_tag=f"v{app_version}")
    return descriptor


def test_admin_can_reproduce_compare_rejudge_close_and_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_root = tmp_path / "monitoring"
    registry = MonitoringRegistry(
        managed_root / "registry.sqlite3",
        artifact_root=managed_root / "registry-artifacts",
    )
    service = ReleaseScopedMonitoringService(
        registry,
        managed_root=managed_root,
        snapshot_availability=lambda _root, _revision: (
            fixed_snapshot.FixedSnapshotAvailability.AVAILABLE
        ),
    )

    # A production report is imported as a summarized Issue.  Raw details stay
    # behind the separately audited Supabase operator endpoint.
    issue = service.import_remote_issue(
        {
            "issue_id": "remote-report-001",
            "reported_release_id": "release-v0.6.1",
            "app_version": "0.6.1",
            "question": "영업이익은 얼마인가요?",
            "reported_problem": "답변이 고정 자료의 수치와 다릅니다.",
            "case_diagnostics_status": "CAPTURED",
        }
    )
    fixture = registry.create_fixture_revision(
        issue_id=issue["issue_id"],
        question="영업이익은 얼마인가요?",
        reported_symptom="고정 자료와 다른 수치를 답합니다.",
        expected_behavior="영업이익 10을 답하고 고정 근거를 인용합니다.",
        typed_checks=[
            {"type": "ANSWER_CONTAINS", "value": "영업이익 10"},
            {"type": "CITATION_PRESENT"},
        ],
        manual_checks=["답변과 근거가 같은 회계기간인지 운영자가 확인"],
    )
    fixture = registry.mark_fixture_ready(fixture["fixture_revision_id"])

    snapshot_id = "snapshot-fixture-001"
    snapshot_path = managed_root / "fixed-snapshots" / snapshot_id
    snapshot_path.mkdir(parents=True)
    with sqlite3.connect(snapshot_path / "projected_catalog.sqlite3") as connection:
        connection.execute(
            "CREATE TABLE reports(report_uid TEXT, source_sha256 TEXT)"
        )
        connection.execute(
            "INSERT INTO reports VALUES (?, ?)", ("a" * 64, "b" * 64)
        )
    (snapshot_path / "subset.faiss").write_bytes(b"index")
    (snapshot_path / "manifest.json").write_text(
        json.dumps(
            {
                "fixed_snapshot_revision_id": snapshot_id,
                "manifest_schema_version": 2,
                "reader_contract": "finance-llm-native-v2-schema-2",
                "vector": {"dimension": 2, "metric": "l2"},
            }
        ),
        encoding="utf-8",
    )
    registry.register_fixed_snapshot(
        fixed_snapshot_revision_id=snapshot_id,
        bundle_relpath=f"fixed-snapshots/{snapshot_id}",
        bundle_digest="b" * 64,
        manifest={
            "manifest_schema_version": 2,
            "reader_contract": "finance-llm-native-v2-schema-2",
            "vector": {"dimension": 2, "metric": "l2"},
            "counts": {"reports": 1, "chunks": 1},
        },
        reader_contract={"contract": "NATIVE_V2", "schema_version": 1},
    )
    reproduction_seed = reproduction_seed_from_raw_report(
        {
            "comment": "고정 자료와 다른 수치를 답합니다.",
            "observed": {
                "selected_question": "영업이익은 얼마인가요?",
                "selected_answer": "영업이익은 8입니다.",
            },
            "case_diagnostics": {
                "schema_version": 1,
                "truncated": False,
                "route_observations": [],
                "retrieval_observations": [
                    {
                        "role": "OBSERVED_RESULT",
                        "source_uid": "a" * 64,
                        "source_sha256": "b" * 64,
                        "rank": 1,
                    }
                ],
            },
        }
    )
    lineage = service.build_reconstruction_lineage(
        reproduction_seed,
        fixed_snapshot_revision_id=snapshot_id,
    )
    assert lineage["exact_count"] == 1
    assert lineage["exceptions"] == []
    case = registry.create_case_revision(
        issue_id=issue["issue_id"],
        fixture_revision_id=fixture["fixture_revision_id"],
        fixed_snapshot_revision_id=snapshot_id,
        fixed_clock="2026-08-29T00:00:00Z",
        evaluator={"version": 1, "mode": "typed-plus-manual"},
        reconstruction_lineage=lineage,
    )
    case = service.mark_case_ready(case["case_revision_id"])

    baseline_release = _register_fake_release(
        service,
        tmp_path / "release-sources",
        app_version="0.6.1",
        answer="영업이익은 8입니다.",
        latency_ms=180.0,
    )
    candidate_release = _register_fake_release(
        service,
        tmp_path / "release-sources",
        app_version="0.6.2",
        answer="영업이익 10입니다.",
        latency_ms=120.0,
    )

    baseline_lifecycle: list[str] = []
    baseline_progress: list[dict[str, object]] = []
    baseline = service.execute_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=baseline_release.release_manifest_id,
        side="BASELINE",
        lifecycle_callback=lambda run: baseline_lifecycle.append(
            str(run["execution_status"])
        ),
        progress_callback=lambda event: baseline_progress.append(dict(event)),
    )
    candidate_1 = service.execute_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=candidate_release.release_manifest_id,
        side="CANDIDATE",
    )

    assert baseline["artifact"]["check_result"]["reproduced"] is True
    assert baseline_lifecycle == [
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
    ]
    assert [event["stage"] for event in baseline_progress] == [
        "PREFLIGHT",
        "ASSETS_READY",
        "QUEUED",
        "EXECUTING",
        "VALIDATING_RESULT",
        "SAVING_RESULT",
        "SUCCEEDED",
    ]
    assert [event["step"] for event in baseline_progress] == list(range(1, 8))
    assert {event["total_steps"] for event in baseline_progress} == {7}
    assert baseline_progress[-1]["run_id"] == baseline["run_id"]

    preflight_failure_progress: list[dict[str, object]] = []
    with pytest.raises(MonitoringRegistryError):
        service.execute_run(
            issue_id=issue["issue_id"],
            case_contract_id=case["case_contract_id"],
            release_manifest_id="release-does-not-exist",
            side="CANDIDATE",
            progress_callback=lambda event: preflight_failure_progress.append(
                dict(event)
            ),
        )
    assert [event["stage"] for event in preflight_failure_progress] == [
        "PREFLIGHT",
        "FAILED",
    ]
    assert preflight_failure_progress[-1]["step"] == 7
    assert "run_id" not in preflight_failure_progress[-1]
    assert candidate_1["artifact"]["check_result"]["passed"] is True
    assert baseline["case_contract_id"] == candidate_1["case_contract_id"]
    assert (
        baseline["artifact"]["route_summary"]["fixed_snapshot_revision_id"]
        == candidate_1["artifact"]["route_summary"]["fixed_snapshot_revision_id"]
        == snapshot_id
    )
    first_view = service.comparison_view(
        baseline_run_ids=[baseline["run_id"]],
        candidate_run_ids=[candidate_1["run_id"]],
    )
    assert first_view["baseline"]["latency_median_ms"] == 180.0
    assert first_view["candidate"]["latency_median_ms"] == 120.0

    first_judgment = registry.create_comparison(
        issue_id=issue["issue_id"],
        baseline_run_ids=[baseline["run_id"]],
        candidate_run_ids=[candidate_1["run_id"]],
        verdict="INCONCLUSIVE",
        note="한 번 더 실행해 응답 안정성을 정성적으로 확인합니다.",
        actor_user_id="admin-1",
    )
    candidate_2 = service.execute_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=candidate_release.release_manifest_id,
        side="CANDIDATE",
    )
    second_view = service.comparison_view(
        baseline_run_ids=[baseline["run_id"]],
        candidate_run_ids=[candidate_1["run_id"], candidate_2["run_id"]],
    )
    assert second_view["candidate"]["valid_run_count"] == 2
    assert second_view["candidate"]["latency_range_ms"] == [120.0, 120.0]
    final_judgment = registry.create_comparison(
        issue_id=issue["issue_id"],
        baseline_run_ids=[baseline["run_id"]],
        candidate_run_ids=[candidate_1["run_id"], candidate_2["run_id"]],
        verdict="IMPROVED",
        note="동일 조건에서 답변·근거가 개선되었고 속도도 함께 확인했습니다.",
        actor_user_id="admin-1",
        supersedes_comparison_id=first_judgment["comparison_id"],
    )
    assert final_judgment["supersedes_comparison_id"] == first_judgment["comparison_id"]
    assert registry.derive_issue_progress(issue["issue_id"]) == {
        "reproduction": "REPRODUCED",
        "comparison": "IMPROVED",
        "next_action": "CLOSE_ISSUE",
    }
    projection = service.build_control_projection(issue["issue_id"])
    assert [record["record_kind"] for record in projection] == [
        "FIXTURE",
        "FIXED_SNAPSHOT",
        "CASE",
        "RELEASE",
        "RELEASE",
        "RUN",
        "RUN",
        "RUN",
        "COMPARISON",
        "COMPARISON",
    ]
    assert all(
        set(record)
        == {
            "record_kind",
            "record_id",
            "lifecycle_status",
            "content_digest",
            "availability",
            "references",
            "attributes",
        }
        for record in projection
    )
    serialized_projection = json.dumps(projection, ensure_ascii=False)
    assert "영업이익은 8입니다" not in serialized_projection
    assert "artifact_relpath" not in serialized_projection
    assert str(managed_root) not in serialized_projection

    before_resolution = registry.get_issue(issue["issue_id"])
    resolved = registry.transition_issue(
        issue["issue_id"],
        target_status="RESOLVED",
        reason="동일 Fixture·FixedSnapshot 비교와 정성 검토를 완료했습니다.",
        actor_user_id="admin-1",
        expected_record_revision=before_resolution["record_revision"],
    )
    reopened = registry.transition_issue(
        issue["issue_id"],
        target_status="OPEN",
        reason="동일 증상의 신규 신고가 들어와 후속 확인합니다.",
        actor_user_id="admin-1",
        expected_record_revision=resolved["record_revision"],
    )

    assert reopened["status"] == "OPEN"
    assert len(registry.list_runs(issue_id=issue["issue_id"])) == 3
    assert len(registry.list_comparisons(issue["issue_id"])) == 2
    assert [
        event["event_type"] for event in registry.list_issue_events(issue["issue_id"])
    ][-2:] == ["RESOLVED", "REOPENED"]
    assert all(
        release["app_version"] != "0.6.0"
        for release in registry.list_release_manifests()
    )

    execution_failure_progress: list[dict[str, object]] = []

    def fail_release_execution(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("fixture provider unavailable")

    with monkeypatch.context() as patch_context:
        patch_context.setattr(
            release_assets,
            "execute_registered_release",
            fail_release_execution,
        )
        with pytest.raises(
            MonitoringServiceError,
            match="fixture provider unavailable",
        ):
            service.execute_run(
                issue_id=issue["issue_id"],
                case_contract_id=case["case_contract_id"],
                release_manifest_id=candidate_release.release_manifest_id,
                side="CANDIDATE",
                progress_callback=lambda event: execution_failure_progress.append(
                    dict(event)
                ),
            )

    assert [event["stage"] for event in execution_failure_progress] == [
        "PREFLIGHT",
        "ASSETS_READY",
        "QUEUED",
        "EXECUTING",
        "SAVING_RESULT",
        "FAILED",
    ]
    failed_run = registry.get_run(
        str(execution_failure_progress[-1]["run_id"])
    )
    assert failed_run["execution_status"] == "FAILED"
    assert failed_run["validity"] == "INVALID"
    assert failed_run["artifact"]["error_type"] == "RuntimeError"
    assert (
        failed_run["artifact"]["error_message"]
        == "fixture provider unavailable"
    )

    broken_observer_stages: list[str] = []

    def broken_progress_observer(event: Mapping[str, object]) -> None:
        broken_observer_stages.append(str(event["stage"]))
        raise RuntimeError("fixture UI observer failed")

    observer_safe_run = service.execute_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=candidate_release.release_manifest_id,
        side="CANDIDATE",
        progress_callback=broken_progress_observer,
    )
    assert observer_safe_run["execution_status"] == "SUCCEEDED"
    assert broken_observer_stages == [
        "PREFLIGHT",
        "ASSETS_READY",
        "QUEUED",
        "EXECUTING",
        "VALIDATING_RESULT",
        "SAVING_RESULT",
        "SUCCEEDED",
    ]

    broken_lifecycle_statuses: list[str] = []

    def broken_lifecycle_observer(run: Mapping[str, object]) -> None:
        broken_lifecycle_statuses.append(str(run["execution_status"]))
        if run["execution_status"] == "SUCCEEDED":
            raise RuntimeError("fixture terminal projection is unavailable")

    projection_safe_run = service.execute_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=candidate_release.release_manifest_id,
        side="CANDIDATE",
        lifecycle_callback=broken_lifecycle_observer,
    )

    assert projection_safe_run["execution_status"] == "SUCCEEDED"
    assert registry.get_run(projection_safe_run["run_id"])[
        "execution_status"
    ] == "SUCCEEDED"
    assert broken_lifecycle_statuses == ["QUEUED", "RUNNING", "SUCCEEDED"]
    assert len(projection_safe_run["projection_sync_warnings"]) == 1
