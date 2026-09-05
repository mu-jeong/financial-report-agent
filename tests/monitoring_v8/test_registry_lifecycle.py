from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.core import release_assets
from src.core.operator_monitoring import (
    ImmutableRecordError,
    MonitoringContractError,
    MonitoringRegistry,
    RevisionConflictError,
)
from src.core.operator_monitoring_service import (
    MonitoringServiceError,
    ReleaseScopedMonitoringService,
)


@pytest.fixture
def registry(tmp_path: Path) -> MonitoringRegistry:
    return MonitoringRegistry(
        tmp_path / "monitoring.sqlite3",
        artifact_root=tmp_path / "artifacts",
    )


def _issue(registry: MonitoringRegistry) -> dict:
    return registry.create_issue(
        source_receipt_id="receipt-001",
        reported_release_id="release-v0.6.1",
        summary={
            "question": "삼성전자의 최근 영업이익을 알려줘",
            "reported_problem": "근거와 다른 수치를 답했습니다.",
        },
    )


def _ready_fixture(registry: MonitoringRegistry, issue_id: str) -> dict:
    fixture = registry.create_fixture_revision(
        issue_id=issue_id,
        question="삼성전자의 최근 영업이익을 알려줘",
        reported_symptom="근거와 다른 수치를 답했습니다.",
        expected_behavior="고정 자료의 영업이익을 사용해야 합니다.",
        typed_checks=[
            {
                "type": "ANSWER_CONTAINS",
                "value": "영업이익",
            }
        ],
        manual_checks=["수치와 근거가 같은 회계기간인지 확인"],
    )
    return registry.mark_fixture_ready(fixture["fixture_revision_id"])


def _snapshot(registry: MonitoringRegistry, *, suffix: str = "1") -> dict:
    return registry.register_fixed_snapshot(
        fixed_snapshot_revision_id=f"snapshot-{suffix}",
        bundle_relpath=f"fixed-snapshots/snapshot-{suffix}",
        bundle_digest=(suffix * 64)[:64],
        manifest={
            "schema_version": 1,
            "catalog_sha256": "a" * 64,
            "index_sha256": "b" * 64,
            "chunk_count": 2,
            "dimension": 4,
            "metric": "l2",
        },
        reader_contract={
            "schema_version": 2,
            "dimension": 4,
            "metric": "l2",
        },
    )


def _ready_case(
    registry: MonitoringRegistry,
    issue_id: str,
    fixture_id: str,
    snapshot_id: str,
) -> dict:
    case = registry.create_case_revision(
        issue_id=issue_id,
        fixture_revision_id=fixture_id,
        fixed_snapshot_revision_id=snapshot_id,
        fixed_clock="2026-08-29T00:00:00Z",
        evaluator={"version": 1, "mode": "typed-plus-manual"},
        reconstruction_lineage={
            "exact_count": 2,
            "exceptions": [],
            "evidence_qualifier": "EXACT",
        },
    )
    return registry.mark_case_ready(
        case["case_revision_id"],
        snapshot_available=True,
    )


def _release(
    registry: MonitoringRegistry,
    release_id: str,
    version: str,
) -> dict:
    return registry.register_release_manifest(
        release_manifest_id=release_id,
        release_tag=version,
        app_version=version.removeprefix("v"),
        manifest_version=1,
        runtime_bundle_digest=(version.encode("utf-8").hex() + "0" * 64)[:64],
        bundle_relpath=f"releases/{release_id}",
        manifest={"runner_contract_version": 1},
    )


def _terminal_run(
    registry: MonitoringRegistry,
    *,
    issue_id: str,
    case_contract_id: str,
    release_manifest_id: str,
    side: str,
    answer: str,
    reproduced: bool,
) -> dict:
    queued = registry.queue_run(
        issue_id=issue_id,
        case_contract_id=case_contract_id,
        release_manifest_id=release_manifest_id,
        side=side,
    )
    registry.start_run(queued["run_id"])
    return registry.finish_run(
        queued["run_id"],
        execution_status="SUCCEEDED",
        validity="VALID",
        artifact={
            "raw_answer": answer,
            "evidence_refs": [
                {
                    "role": "CITED",
                    "chunk_uid": "b" * 64,
                    "source_uid": "a" * 64,
                    "source_sha256": "c" * 64,
                    "rank": 1,
                }
            ],
            "route_summary": {"route": "retrieval"},
            "check_result": {"reproduced": reproduced, "passed": not reproduced},
            "runtime_profile": {"generation_model": "fixture-model"},
            "latency_ms": 125.0,
        },
    )


def test_issue_resolve_and_reopen_append_events_and_preserve_revision_history(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)

    resolved = registry.transition_issue(
        issue["issue_id"],
        target_status="RESOLVED",
        reason="개선 비교를 확인했습니다.",
        actor_user_id="admin-1",
        expected_record_revision=1,
    )
    reopened = registry.transition_issue(
        issue["issue_id"],
        target_status="OPEN",
        reason="동일 증상이 다시 신고되었습니다.",
        actor_user_id="admin-1",
        expected_record_revision=2,
    )

    assert resolved["status"] == "RESOLVED"
    assert reopened["status"] == "OPEN"
    assert reopened["record_revision"] == 3
    assert [event["event_type"] for event in registry.list_issue_events(issue["issue_id"])] == [
        "CREATED",
        "RESOLVED",
        "REOPENED",
    ]


def test_issue_triage_and_terminal_outcomes_are_distinct_and_audited(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)

    in_progress = registry.transition_issue(
        issue["issue_id"],
        target_status="IN_PROGRESS",
        reason="신고 내용을 확인하기 시작했습니다.",
        actor_user_id="admin-1",
        expected_record_revision=1,
    )
    resolved = registry.transition_issue(
        issue["issue_id"],
        target_status="RESOLVED",
        reason="후보 버전에서 수정 결과를 확인했습니다.",
        actor_user_id="admin-1",
        expected_record_revision=2,
    )
    not_issue = registry.transition_issue(
        issue["issue_id"],
        target_status="NOT_ISSUE",
        reason="추가 확인 결과 제품 결함이 아니었습니다.",
        actor_user_id="admin-1",
        expected_record_revision=3,
    )
    reopened = registry.transition_issue(
        issue["issue_id"],
        target_status="OPEN",
        reason="새 근거가 접수되어 다시 확인합니다.",
        actor_user_id="admin-1",
        expected_record_revision=4,
    )

    assert in_progress["status"] == "IN_PROGRESS"
    assert resolved["status"] == "RESOLVED"
    assert not_issue["status"] == "NOT_ISSUE"
    assert reopened["status"] == "OPEN"
    assert registry.list_issues(status="OPEN") == [reopened]
    assert registry.list_issues(status="IN_PROGRESS") == []
    assert [
        event["event_type"]
        for event in registry.list_issue_events(issue["issue_id"])
    ] == ["CREATED", "IN_PROGRESS", "RESOLVED", "NOT_ISSUE", "REOPENED"]


def test_registry_migrates_legacy_closed_without_guessing_its_outcome(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "monitoring-v1.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE monitoring_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO monitoring_meta(key, value) VALUES ('schema_version', '1');
            CREATE TABLE issues (
                issue_id TEXT PRIMARY KEY,
                source_receipt_id TEXT NOT NULL UNIQUE,
                reported_release_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
                summary_json TEXT NOT NULL,
                current_case_contract_id TEXT,
                current_comparison_id TEXT,
                record_revision INTEGER NOT NULL CHECK (record_revision >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE issue_events (
                event_id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL REFERENCES issues(issue_id),
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('CREATED', 'CLOSED', 'REOPENED', 'RAW_VIEWED',
                                   'CASE_READY', 'COMPARISON_CREATED')
                ),
                actor_user_id TEXT,
                reason TEXT,
                details_json TEXT NOT NULL,
                before_revision INTEGER,
                after_revision INTEGER,
                created_at TEXT NOT NULL
            );
            INSERT INTO issues(
                issue_id, source_receipt_id, reported_release_id, status,
                summary_json, record_revision, created_at, updated_at
            ) VALUES (
                'issue-legacy', 'receipt-legacy', 'release-v0.6.1', 'CLOSED',
                '{"category":"legacy"}', 1,
                '2026-08-01T00:00:00+00:00', '2026-08-02T00:00:00+00:00'
            );
            INSERT INTO issue_events(
                event_id, issue_id, event_type, reason, details_json,
                before_revision, after_revision, created_at
            ) VALUES (
                'event-legacy', 'issue-legacy', 'CLOSED', 'legacy close', '{}',
                null, 1, '2026-08-02T00:00:00+00:00'
            );
            """
        )

    registry = MonitoringRegistry(db_path, artifact_root=tmp_path / "artifacts")

    legacy = registry.get_issue("issue-legacy")
    assert legacy["status"] == "CLOSED"
    assert sqlite3.connect(db_path).execute(
        "SELECT value FROM monitoring_meta WHERE key = 'schema_version'"
    ).fetchone() == ("3",)

    categorized = registry.transition_issue(
        "issue-legacy",
        target_status="RESOLVED",
        reason="기존 종료 기록을 해결됨으로 확인했습니다.",
        actor_user_id="admin-1",
        expected_record_revision=1,
    )
    assert categorized["status"] == "RESOLVED"
    assert [
        event["event_type"] for event in registry.list_issue_events("issue-legacy")
    ] == ["CLOSED", "RESOLVED"]


def test_legacy_closed_cannot_be_created_by_a_new_transition(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)

    with pytest.raises(MonitoringContractError, match="illegal Issue status transition"):
        registry.transition_issue(
            issue["issue_id"],
            target_status="CLOSED",
            reason="분류 없는 종료",
            actor_user_id="admin-1",
            expected_record_revision=1,
        )

    assert registry.get_issue(issue["issue_id"])["status"] == "OPEN"
    assert [
        event["event_type"]
        for event in registry.list_issue_events(issue["issue_id"])
    ] == ["CREATED"]


def test_issue_transition_requires_reason_and_expected_revision(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)

    with pytest.raises(MonitoringContractError, match="reason"):
        registry.transition_issue(
            issue["issue_id"],
            target_status="RESOLVED",
            reason=" ",
            actor_user_id="admin-1",
            expected_record_revision=1,
        )

    with pytest.raises(RevisionConflictError):
        registry.transition_issue(
            issue["issue_id"],
            target_status="RESOLVED",
            reason="처리 완료",
            actor_user_id="admin-1",
            expected_record_revision=99,
        )


def test_ready_fixture_is_immutable_and_revision_creates_a_new_identity(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    ready = _ready_fixture(registry, issue["issue_id"])

    with pytest.raises(ImmutableRecordError):
        registry.update_fixture_revision(
            ready["fixture_revision_id"],
            expected_behavior="다른 동작",
        )

    successor = registry.revise_fixture(
        ready["fixture_revision_id"],
        expected_behavior="같은 기간의 수치와 근거를 답해야 합니다.",
    )
    assert successor["lifecycle_status"] == "DRAFT"
    assert successor["fixture_revision_id"] != ready["fixture_revision_id"]
    assert successor["predecessor_fixture_revision_id"] == ready["fixture_revision_id"]
    assert registry.get_fixture_revision(ready["fixture_revision_id"])["lifecycle_status"] == "READY"
    assert [
        row["fixture_revision_id"]
        for row in registry.list_fixture_revisions(issue["issue_id"])
    ] == [ready["fixture_revision_id"], successor["fixture_revision_id"]]


def test_case_contract_changes_when_fixture_snapshot_or_lineage_changes(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot_1 = _snapshot(registry, suffix="1")
    snapshot_2 = _snapshot(registry, suffix="2")
    first = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot_1["fixed_snapshot_revision_id"],
    )
    second = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot_2["fixed_snapshot_revision_id"],
    )

    assert first["case_contract_id"] != second["case_contract_id"]
    with pytest.raises(ImmutableRecordError):
        registry.update_case_revision(
            first["case_revision_id"],
            fixed_clock="2026-08-30T00:00:00Z",
        )

    successor = registry.revise_case(
        first["case_revision_id"],
        fixed_snapshot_revision_id=snapshot_2["fixed_snapshot_revision_id"],
    )
    assert successor["lifecycle_status"] == "DRAFT"
    assert successor["predecessor_case_revision_id"] == first["case_revision_id"]
    assert [
        row["case_revision_id"]
        for row in registry.list_case_revisions(issue["issue_id"])
    ] == [
        first["case_revision_id"],
        second["case_revision_id"],
        successor["case_revision_id"],
    ]


def test_case_ready_rejects_duplicate_contract(registry: MonitoringRegistry) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot["fixed_snapshot_revision_id"],
    )

    duplicate = registry.create_case_revision(
        issue_id=issue["issue_id"],
        fixture_revision_id=fixture["fixture_revision_id"],
        fixed_snapshot_revision_id=snapshot["fixed_snapshot_revision_id"],
        fixed_clock="2026-08-29T00:00:00Z",
        evaluator={"version": 1, "mode": "typed-plus-manual"},
        reconstruction_lineage={
            "exact_count": 2,
            "exceptions": [],
            "evidence_qualifier": "EXACT",
        },
    )

    with pytest.raises(MonitoringContractError, match="identical Case contract"):
        registry.mark_case_ready(
            duplicate["case_revision_id"], snapshot_available=True
        )


def test_case_ready_requires_fixture_snapshot_and_confirmed_lineage(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = registry.create_fixture_revision(
        issue_id=issue["issue_id"],
        question="질문",
        reported_symptom="증상",
        expected_behavior="기대",
        typed_checks=[{"type": "ANSWER_CONTAINS", "value": "기대"}],
    )
    snapshot = _snapshot(registry)
    case = registry.create_case_revision(
        issue_id=issue["issue_id"],
        fixture_revision_id=fixture["fixture_revision_id"],
        fixed_snapshot_revision_id=snapshot["fixed_snapshot_revision_id"],
        fixed_clock="2026-08-29T00:00:00Z",
        evaluator={"version": 1},
        reconstruction_lineage={
            "exact_count": 1,
            "exceptions": [{"kind": "MISSING", "confirmed": False}],
        },
    )

    with pytest.raises(MonitoringContractError, match="Fixture"):
        registry.mark_case_ready(case["case_revision_id"], snapshot_available=True)

    registry.mark_fixture_ready(fixture["fixture_revision_id"])
    with pytest.raises(MonitoringContractError, match="lineage"):
        registry.mark_case_ready(case["case_revision_id"], snapshot_available=True)

    with pytest.raises(MonitoringContractError, match="Snapshot"):
        registry.mark_case_ready(case["case_revision_id"], snapshot_available=False)


def test_operator_defined_lineage_requires_explicit_scope_confirmation(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = registry.create_case_revision(
        issue_id=issue["issue_id"],
        fixture_revision_id=fixture["fixture_revision_id"],
        fixed_snapshot_revision_id=snapshot["fixed_snapshot_revision_id"],
        fixed_clock=None,
        evaluator={"version": 1},
        reconstruction_lineage={
            "basis": "OPERATOR_DEFINED",
            "operator_scope_confirmed": False,
            "operator_scope_reason": "진단 자료가 없어 문서 범위를 직접 확인",
            "exceptions": [],
        },
    )

    with pytest.raises(MonitoringContractError, match="must be confirmed"):
        registry.mark_case_ready(
            case["case_revision_id"], snapshot_available=True
        )

    registry.update_case_revision(
        case["case_revision_id"],
        reconstruction_lineage={
            "basis": "OPERATOR_DEFINED",
            "operator_scope_confirmed": True,
            "operator_scope_reason": "진단 자료가 없어 문서 범위를 직접 확인",
            "exceptions": [],
        },
    )
    ready = registry.mark_case_ready(
        case["case_revision_id"], snapshot_available=True
    )
    assert ready["evidence_qualifier"] == "PARTIAL"


def test_substitute_exception_derives_substitute_qualifier_over_partial_template(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = registry.create_case_revision(
        issue_id=issue["issue_id"],
        fixture_revision_id=fixture["fixture_revision_id"],
        fixed_snapshot_revision_id=snapshot["fixed_snapshot_revision_id"],
        fixed_clock=None,
        evaluator={"version": 1},
        reconstruction_lineage={
            "basis": "REPORT_DIAGNOSTICS",
            "evidence_qualifier": "PARTIAL",
            "exceptions": [
                {
                    "kind": "SUBSTITUTE",
                    "confirmed": True,
                    "reason": "운영 당시 원본 대신 동일 기간의 정정본을 사용",
                }
            ],
        },
    )

    ready = registry.mark_case_ready(
        case["case_revision_id"], snapshot_available=True
    )

    assert ready["evidence_qualifier"] == "SUBSTITUTE_INCLUDED"


def test_run_lifecycle_terminal_artifact_is_create_only_and_retry_is_new(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot["fixed_snapshot_revision_id"],
    )
    release = _release(registry, "release-v0.6.1", "v0.6.1")
    first = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=release["release_manifest_id"],
        side="BASELINE",
        answer="잘못된 답변",
        reproduced=True,
    )
    original_bytes = Path(first["artifact_path"]).read_bytes()

    with pytest.raises(ImmutableRecordError):
        registry.finish_run(
            first["run_id"],
            execution_status="FAILED",
            validity="INVALID",
            artifact={"raw_answer": "덮어쓰기"},
        )

    retry = registry.queue_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=release["release_manifest_id"],
        side="BASELINE",
    )
    assert retry["run_id"] != first["run_id"]
    assert Path(first["artifact_path"]).read_bytes() == original_bytes


def test_run_runtime_profile_is_an_immutable_queued_input(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot["fixed_snapshot_revision_id"],
    )
    release = _release(registry, "release-v0.6.1", "v0.6.1")
    runtime_profile = {
        "environment": {"SEARCH_TOP_K": 7},
        "snapshot_reader": {"manifest_schema_version": 2},
    }

    queued = registry.queue_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=release["release_manifest_id"],
        side="BASELINE",
        runtime_profile=runtime_profile,
    )

    assert queued["runtime_profile"] == runtime_profile
    assert registry.list_runs(issue_id=issue["issue_id"])[0][
        "runtime_profile"
    ] == runtime_profile
    with sqlite3.connect(registry.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="run inputs are immutable"):
            connection.execute(
                "UPDATE runs SET runtime_profile_json = '{}' WHERE run_id = ?",
                (queued["run_id"],),
            )


def test_registry_migrates_schema_v2_runs_to_immutable_runtime_profiles(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "monitoring-v2.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE monitoring_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO monitoring_meta(key, value) VALUES ('schema_version', '2');
            CREATE TABLE release_manifests (
                release_manifest_id TEXT PRIMARY KEY,
                release_tag TEXT NOT NULL UNIQUE,
                app_version TEXT NOT NULL,
                manifest_version INTEGER NOT NULL,
                lifecycle_status TEXT NOT NULL,
                runtime_bundle_digest TEXT NOT NULL UNIQUE,
                bundle_relpath TEXT NOT NULL UNIQUE,
                manifest_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO release_manifests VALUES (
                'release-v2', 'v0.6.2', '0.6.2', 1, 'REGISTERED',
                'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                'releases/release-v2', '{"runner_contract_version":1}',
                '2026-08-01T00:00:00+00:00'
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                issue_id TEXT NOT NULL,
                case_contract_id TEXT NOT NULL,
                release_manifest_id TEXT NOT NULL
                    REFERENCES release_manifests(release_manifest_id),
                side TEXT NOT NULL,
                execution_status TEXT NOT NULL,
                validity TEXT,
                artifact_relpath TEXT,
                artifact_digest TEXT,
                queued_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );
            INSERT INTO runs(
                run_id, issue_id, case_contract_id, release_manifest_id,
                side, execution_status, queued_at
            ) VALUES (
                'run-v2', 'issue-v2', 'case-v2', 'release-v2',
                'CANDIDATE', 'QUEUED', '2026-08-01T00:00:00+00:00'
            );
            CREATE TRIGGER run_input_no_update
            BEFORE UPDATE OF issue_id, case_contract_id, release_manifest_id, side
            ON runs
            BEGIN
                SELECT RAISE(ABORT, 'run inputs are immutable');
            END;
            """
        )

    MonitoringRegistry(db_path, artifact_root=tmp_path / "artifacts")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM monitoring_meta WHERE key = 'schema_version'"
        ).fetchone() == ("3",)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'run_input_no_update'"
        ).fetchone()[0]
        migrated_profile = connection.execute(
            "SELECT runtime_profile_json FROM runs WHERE run_id = 'run-v2'"
        ).fetchone()[0]
        foreign_key_violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
    assert "runtime_profile_json" in columns
    assert "runtime_profile_json" in trigger_sql
    assert migrated_profile == "{}"
    assert foreign_key_violations == []


def test_release_cache_digest_is_integrity_metadata_not_release_identity(
    registry: MonitoringRegistry,
) -> None:
    shared_cache_digest = "d" * 64
    first_revision = "1" * 40
    second_revision = "2" * 40
    first_release_id = release_assets.git_release_manifest_id(
        "0.6.2", first_revision
    )
    second_release_id = release_assets.git_release_manifest_id(
        "0.6.3", second_revision
    )

    first = registry.register_release_manifest(
        release_manifest_id=first_release_id,
        release_tag="v0.6.2",
        app_version="0.6.2",
        manifest_version=2,
        runtime_bundle_digest=shared_cache_digest,
        bundle_relpath=f"releases/{first_release_id}",
        manifest={
            "schema_version": 2,
            "app_version": "0.6.2",
            "git_revision": first_revision,
            "runner_contract_version": 1,
        },
    )
    second = registry.register_release_manifest(
        release_manifest_id=second_release_id,
        release_tag="v0.6.3",
        app_version="0.6.3",
        manifest_version=2,
        runtime_bundle_digest=shared_cache_digest,
        bundle_relpath=f"releases/{second_release_id}",
        manifest={
            "schema_version": 2,
            "app_version": "0.6.3",
            "git_revision": second_revision,
            "runner_contract_version": 1,
        },
    )

    assert first["runtime_bundle_digest"] == shared_cache_digest
    assert second["runtime_bundle_digest"] == shared_cache_digest


def test_v2_release_registry_rejects_an_id_not_derived_from_version_and_commit(
    registry: MonitoringRegistry,
) -> None:
    with pytest.raises(MonitoringContractError, match="version and Git commit"):
        registry.register_release_manifest(
            release_manifest_id="f" * 64,
            release_tag="v0.6.2",
            app_version="0.6.2",
            manifest_version=2,
            runtime_bundle_digest="d" * 64,
            bundle_relpath="releases/" + "f" * 64,
            manifest={
                "schema_version": 2,
                "app_version": "0.6.2",
                "git_revision": "1" * 40,
                "runner_contract_version": 1,
            },
        )


def test_baseline_must_use_the_release_reported_by_the_issue(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot["fixed_snapshot_revision_id"],
    )
    _release(registry, "release-v0.6.1", "v0.6.1")
    other = _release(registry, "release-v0.6.2", "v0.6.2")

    with pytest.raises(MonitoringContractError, match="reported release"):
        registry.queue_run(
            issue_id=issue["issue_id"],
            case_contract_id=case["case_contract_id"],
            release_manifest_id=other["release_manifest_id"],
            side="BASELINE",
        )


def test_comparison_is_immutable_and_rejudgment_supersedes_latest(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot["fixed_snapshot_revision_id"],
    )
    baseline_release = _release(registry, "release-v0.6.1", "v0.6.1")
    candidate_release = _release(registry, "release-v0.6.2", "v0.6.2")
    baseline = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=baseline_release["release_manifest_id"],
        side="BASELINE",
        answer="기준 답변",
        reproduced=True,
    )
    candidate_1 = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=candidate_release["release_manifest_id"],
        side="CANDIDATE",
        answer="후보 답변 1",
        reproduced=False,
    )
    first = registry.create_comparison(
        issue_id=issue["issue_id"],
        baseline_run_ids=[baseline["run_id"]],
        candidate_run_ids=[candidate_1["run_id"]],
        verdict="INCONCLUSIVE",
        note="한 번 더 실행해 봅니다.",
        actor_user_id="admin-1",
    )
    candidate_2 = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=candidate_release["release_manifest_id"],
        side="CANDIDATE",
        answer="후보 답변 2",
        reproduced=False,
    )
    second = registry.create_comparison(
        issue_id=issue["issue_id"],
        baseline_run_ids=[baseline["run_id"]],
        candidate_run_ids=[candidate_1["run_id"], candidate_2["run_id"]],
        verdict="IMPROVED",
        note="답변과 근거가 개선됐습니다.",
        actor_user_id="admin-1",
        supersedes_comparison_id=first["comparison_id"],
    )

    assert second["supersedes_comparison_id"] == first["comparison_id"]
    assert registry.get_comparison(first["comparison_id"])["verdict"] == "INCONCLUSIVE"
    with pytest.raises(ImmutableRecordError):
        registry.update_comparison(first["comparison_id"], verdict="IMPROVED")


def test_comparison_rejects_invalid_or_different_case_runs(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot_1 = _snapshot(registry, suffix="1")
    snapshot_2 = _snapshot(registry, suffix="2")
    case_1 = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot_1["fixed_snapshot_revision_id"],
    )
    case_2 = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot_2["fixed_snapshot_revision_id"],
    )
    baseline_release = _release(registry, "release-v0.6.1", "v0.6.1")
    candidate_release = _release(registry, "release-v0.6.2", "v0.6.2")
    baseline = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case_1["case_contract_id"],
        release_manifest_id=baseline_release["release_manifest_id"],
        side="BASELINE",
        answer="기준",
        reproduced=True,
    )
    candidate = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case_2["case_contract_id"],
        release_manifest_id=candidate_release["release_manifest_id"],
        side="CANDIDATE",
        answer="후보",
        reproduced=False,
    )

    with pytest.raises(MonitoringContractError, match="same case_contract_id"):
        registry.create_comparison(
            issue_id=issue["issue_id"],
            baseline_run_ids=[baseline["run_id"]],
            candidate_run_ids=[candidate["run_id"]],
            verdict="IMPROVED",
            note="비교 불가",
            actor_user_id="admin-1",
        )


def test_progress_is_derived_from_evidence_and_issue_reopen_keeps_all_artifacts(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot["fixed_snapshot_revision_id"],
    )
    baseline_release = _release(registry, "release-v0.6.1", "v0.6.1")
    candidate_release = _release(registry, "release-v0.6.2", "v0.6.2")
    baseline = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=baseline_release["release_manifest_id"],
        side="BASELINE",
        answer="기준",
        reproduced=True,
    )
    candidate = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=candidate_release["release_manifest_id"],
        side="CANDIDATE",
        answer="후보",
        reproduced=False,
    )
    comparison = registry.create_comparison(
        issue_id=issue["issue_id"],
        baseline_run_ids=[baseline["run_id"]],
        candidate_run_ids=[candidate["run_id"]],
        verdict="IMPROVED",
        note="정성적으로 개선됨",
        actor_user_id="admin-1",
    )
    progress_before = registry.derive_issue_progress(issue["issue_id"])
    resolved = registry.transition_issue(
        issue["issue_id"],
        target_status="RESOLVED",
        reason="개선 확인",
        actor_user_id="admin-1",
        expected_record_revision=3,
    )
    registry.transition_issue(
        issue["issue_id"],
        target_status="OPEN",
        reason="재신고",
        actor_user_id="admin-1",
        expected_record_revision=resolved["record_revision"],
    )

    progress_after = registry.derive_issue_progress(issue["issue_id"])
    assert progress_before["reproduction"] == "REPRODUCED"
    assert progress_before["comparison"] == "IMPROVED"
    assert progress_before["next_action"] == "CLOSE_ISSUE"
    assert progress_after == progress_before
    assert registry.get_run(baseline["run_id"])["artifact"]["raw_answer"] == "기준"
    assert registry.get_comparison(comparison["comparison_id"])["verdict"] == "IMPROVED"
    assert len(registry.list_issue_events(issue["issue_id"])) >= 5


def test_saved_artifact_digest_detects_external_tampering(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot["fixed_snapshot_revision_id"],
    )
    release = _release(registry, "release-v0.6.1", "v0.6.1")
    run = _terminal_run(
        registry,
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=release["release_manifest_id"],
        side="BASELINE",
        answer="원래 답변",
        reproduced=True,
    )
    path = Path(run["artifact_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["raw_answer"] = "변조된 답변"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ImmutableRecordError, match="digest"):
        registry.get_run(run["run_id"])


def test_incomplete_runs_are_recovered_once_as_interrupted_invalid_artifacts(
    registry: MonitoringRegistry,
) -> None:
    issue = _issue(registry)
    fixture = _ready_fixture(registry, issue["issue_id"])
    snapshot = _snapshot(registry)
    case = _ready_case(
        registry,
        issue["issue_id"],
        fixture["fixture_revision_id"],
        snapshot["fixed_snapshot_revision_id"],
    )
    baseline_release = _release(registry, "release-v0.6.1", "v0.6.1")
    candidate_release = _release(registry, "release-v0.6.2", "v0.6.2")
    queued = registry.queue_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=baseline_release["release_manifest_id"],
        side="BASELINE",
    )
    running = registry.queue_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=candidate_release["release_manifest_id"],
        side="CANDIDATE",
    )
    registry.start_run(running["run_id"])

    service = ReleaseScopedMonitoringService(
        registry,
        managed_root=registry.artifact_root,
    )
    with pytest.raises(MonitoringServiceError, match="requires confirmation"):
        service.recover_incomplete_runs()
    recovered = service.recover_incomplete_runs(
        operator_confirmed_no_active_process=True
    )

    assert {run["run_id"] for run in recovered} == {
        queued["run_id"],
        running["run_id"],
    }
    for run in recovered:
        restored = registry.get_run(run["run_id"])
        assert restored["execution_status"] == "INTERRUPTED"
        assert restored["validity"] == "INVALID"
        assert restored["artifact"]["recovery"] == "OPERATOR_CONFIRMED"
        assert "terminal result" in restored["artifact"]["invalid_reason"]
    assert service.recover_incomplete_runs(
        operator_confirmed_no_active_process=True
    ) == []
