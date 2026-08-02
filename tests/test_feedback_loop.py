import json
import shutil
from pathlib import Path

import pytest

from src.core import artifact_io, issue_report_store
from src.core.feedback_handoff import (
    discover_candidate_orphan_handoffs,
    write_codex_handoff,
)
from src.core.monitoring import (
    CandidateConflictError,
    CandidateLoadError,
    CandidateTransitionError,
    CandidateValidationError,
    approve_candidate_expectation,
    build_candidate_action_state,
    canonicalize_regression_candidate,
    compute_evaluation_run_hash,
    discover_candidate_orphan_runs,
    evaluate_dataset_case_result,
    list_candidate_evaluation_run_artifacts,
    list_regression_candidate_artifacts,
    load_regression_candidate,
    promote_issue_report_to_eval_candidate,
    record_candidate_handoff,
    record_candidate_manual_evidence,
    record_candidate_run,
    revoke_candidate_expectation,
    run_candidate_evaluation,
    transition_regression_candidate,
    update_regression_candidate,
)


SAMPLE_ROOT = Path()


def _legacy_report_payload() -> dict:
    return {
        "id": "report_fixture_001",
        "thread_id": "thread_fixture",
        "category": "출처 오류",
        "description": "선택된 출처가 기대와 다릅니다.",
        "context": {
            "submitted_from": "chat_monitoring_trace",
            "selected_user_question": "NAVER 최신 리포트 요약",
            "selected_message": {
                "id": "assistant_fixture_001",
                "content": "잘못된 출처를 사용한 합성 답변",
                "metadata": {
                    "job_id": "job_fixture_001",
                    "route": "vectordb",
                    "latency_seconds": 1.25,
                },
            },
            "trace_detail": {
                "routing": {"route": "vectordb"},
                "scope": {"search_filters": {"target_name": "NAVER"}},
                "sources": [
                    {
                        "file_name": "wrong.pdf",
                        "report_type": "company",
                    }
                ],
                "query_rewrite": {
                    "original_question": "NAVER 최신 리포트 요약"
                },
            },
        },
        "app_version": "0.5.1",
        "created_at": "2026-07-26T00:00:00+00:00",
        "source": "chat_monitoring_trace",
        "unknown_legacy_field": {"keep": True},
    }


def _legacy_candidate_payload() -> dict:
    return {
        "id": "candidate_report_fixture_001",
        "status": "candidate",
        "triage_status": "new",
        "operator_decision": "unreviewed",
        "severity": "untriaged",
        "impact_area": "retrieval_source",
        "source": "issue_report",
        "source_report_id": "report_fixture_001",
        "thread_id": "thread_fixture",
        "category": "출처 오류",
        "created_at": "2026-07-26T00:01:00+00:00",
        "eval_case_draft": {
            "id": "issue_report_fixture_001",
            "type": "vectordb_retrieval",
            "question": "NAVER 최신 리포트 요약",
            "expected_route": "vectordb",
            "expected_filters": {"target_name": "NAVER"},
            "expected_sources": [
                {
                    "file_name": "wrong.pdf",
                    "report_type": "company",
                }
            ],
            "expected_state": {},
            "checks": ["route_pass", "source_hit"],
            "review_required": True,
        },
        "unknown_legacy_field": {"keep": True},
    }


def _legacy_report_text() -> str:
    return """Finance LLM 문제 신고
====================
Report ID: report_fixture_001
Created At (UTC): 2026-07-26T00:00:00+00:00
App Version: 0.5.1
Thread ID: thread_fixture
Category: 출처 오류

Description:
선택된 출처가 기대와 다릅니다.

Context:
- submitted_from: chat_monitoring_trace
- selected_user_question: NAVER 최신 리포트 요약

Conversation Messages:
- 첨부된 대화 없음

사용 안내:
- 합성 테스트 자료입니다.
"""


@pytest.fixture(autouse=True)
def _write_ephemeral_legacy_samples(tmp_path) -> None:
    """Build unit-test inputs in pytest temp space, never in tests/fixtures."""
    global SAMPLE_ROOT
    SAMPLE_ROOT = tmp_path / "legacy_samples"
    SAMPLE_ROOT.mkdir()
    report = _legacy_report_payload()
    (SAMPLE_ROOT / "legacy_issue_report_v1.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (SAMPLE_ROOT / "legacy_issue_report_v1.txt").write_text(
        _legacy_report_text(),
        encoding="utf-8",
    )
    (SAMPLE_ROOT / "legacy_candidate_v1.json").write_text(
        json.dumps(
            _legacy_candidate_payload(),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _copy_report_pair(target: Path) -> tuple[Path, Path]:
    target.mkdir(parents=True)
    json_path = target / "issue_report_20260726T000000Z_report_fixture_001.json"
    text_path = json_path.with_suffix(".txt")
    shutil.copy2(SAMPLE_ROOT / "legacy_issue_report_v1.json", json_path)
    shutil.copy2(SAMPLE_ROOT / "legacy_issue_report_v1.txt", text_path)
    return json_path, text_path


def test_legacy_report_load_is_read_only_and_separates_observed(tmp_path):
    json_path, _ = _copy_report_pair(tmp_path / "debug")
    before = json_path.read_bytes()
    before_mtime = json_path.stat().st_mtime_ns

    report = issue_report_store.load_report(json_path)

    assert report["schema_version"] == 2
    assert report["comment"] == "선택된 출처가 기대와 다릅니다."
    assert report["message_id"] == "assistant_fixture_001"
    assert report["job_id"] == "job_fixture_001"
    assert report["observed"]["user_question"] == "NAVER 최신 리포트 요약"
    assert report["observed"]["trace"]["sources"][0]["file_name"] == "wrong.pdf"
    assert report["observed"]["actual"] == {
        "route": "vectordb",
        "filters": {"target_name": "NAVER"},
        "sources": [{"file_name": "wrong.pdf", "report_type": "company"}],
        "state": {},
    }
    assert report["unknown_legacy_field"] == {"keep": True}
    assert json_path.read_bytes() == before
    assert json_path.stat().st_mtime_ns == before_mtime


def test_native_v2_report_observed_actual_is_promoted_without_legacy_context(
    tmp_path,
):
    report = issue_report_store.canonicalize_report(
        {
            "schema_version": 2,
            "id": "native_report",
            "category": "출처 오류",
            "comment": "합성 출처 오류",
            "observed": {
                "user_question": "NAVER 최신 리포트 요약",
                "actual": {
                    "route": "vectordb",
                    "filters": {"target_name": "NAVER"},
                    "sources": [
                        {
                            "file_name": "wrong.pdf",
                            "report_type": "company",
                        }
                    ],
                    "state": {},
                },
            },
        }
    )

    candidate = promote_issue_report_to_eval_candidate(
        report,
        output_dir=tmp_path / "candidates",
    )

    assert candidate["observed"]["reproduction_input"] == {
        "question": "NAVER 최신 리포트 요약"
    }
    assert candidate["observed"]["actual"] == report["observed"]["actual"]
    assert candidate["eval_case_draft"]["expected_route"] == "vectordb"


def test_report_discovery_returns_json_only_and_repairs_text_companion(tmp_path, monkeypatch):
    debug_dir = tmp_path / "debug"
    json_path, text_path = _copy_report_pair(debug_dir)
    text_path.unlink()
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)

    discovered = issue_report_store.list_issue_report_artifacts()

    assert [item["id"] for item in discovered["items"]] == ["report_fixture_001"]
    assert [warning["code"] for warning in discovered["warnings"]] == [
        "missing_text_companion"
    ]

    repaired = issue_report_store.repair_issue_report_text_companion(json_path)

    assert repaired == text_path
    assert "Report ID: report_fixture_001" in text_path.read_text(encoding="utf-8")


def test_report_discovery_falls_back_to_text_when_json_is_malformed(tmp_path, monkeypatch):
    debug_dir = tmp_path / "debug"
    json_path, _ = _copy_report_pair(debug_dir)
    json_path.write_text("{", encoding="utf-8")
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)

    discovered = issue_report_store.list_issue_report_artifacts()

    assert [item["id"] for item in discovered["items"]] == ["report_fixture_001"]
    assert discovered["warnings"] == [
        {
            "code": "malformed_json",
            "path": str(json_path),
            "blocking": False,
        }
    ]


def test_report_discovery_marks_both_unreadable_companions_as_blocking(
    tmp_path,
    monkeypatch,
):
    debug_dir = tmp_path / "debug"
    debug_dir.mkdir()
    json_path = debug_dir / "issue_report_20260726T000000Z_broken.json"
    text_path = json_path.with_suffix(".txt")
    json_path.write_bytes(b"\xff")
    text_path.write_bytes(b"\xff")
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)

    discovered = issue_report_store.list_issue_report_artifacts()

    assert discovered["items"] == []
    assert {warning["code"] for warning in discovered["warnings"]} == {
        "malformed_json",
        "malformed_text",
    }
    assert all(
        warning["blocking"] is True
        for warning in discovered["warnings"]
    )


def test_active_monitoring_discovery_ignores_schema_less_reports(
    tmp_path,
    monkeypatch,
):
    debug_dir = tmp_path / "debug"
    _copy_report_pair(debug_dir)
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    created = issue_report_store.create_issue_report(
        "thread-v2",
        "답변 품질",
        "v2 report",
        {"report_target_type": "ui_or_system"},
        report_target_type="ui_or_system",
    )

    discovered = issue_report_store.list_v2_issue_report_artifacts()

    assert [item["id"] for item in discovered["items"]] == [created["id"]]
    assert discovered["warnings"] == []


def test_report_write_failure_keeps_canonical_json_for_recovery(tmp_path, monkeypatch):
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    monkeypatch.setattr(issue_report_store, "get_app_version", lambda: "0.5.1")

    real_write_text = artifact_io.atomic_write_text

    def fail_text_write(path, text):
        if Path(path).suffix == ".txt":
            raise OSError("synthetic companion failure")
        return real_write_text(path, text)

    monkeypatch.setattr(artifact_io, "atomic_write_text", fail_text_write)

    with pytest.raises(issue_report_store.IssueReportWriteError) as raised:
        issue_report_store.create_issue_report(
            "thread_fixture",
            "출처 오류",
            "합성 오류",
            {"submitted_from": "streamlit_chat", "app_version": "0.5.1"},
        )

    canonical_path = Path(raised.value.canonical_path)
    assert canonical_path.exists()
    assert json.loads(canonical_path.read_text(encoding="utf-8"))["schema_version"] == 2
    assert not canonical_path.with_suffix(".txt").exists()

    discovered = issue_report_store.list_issue_report_artifacts()
    assert [item["json_path"] for item in discovered["items"]] == [
        str(canonical_path)
    ]
    assert [warning["code"] for warning in discovered["warnings"]] == [
        "missing_text_companion"
    ]

    monkeypatch.setattr(artifact_io, "atomic_write_text", real_write_text)
    repaired = issue_report_store.repair_issue_report_text_companion(
        canonical_path
    )
    assert repaired.exists()
    assert issue_report_store.list_issue_report_artifacts()["warnings"] == []


def test_email_import_is_idempotent_by_report_id(tmp_path, monkeypatch):
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    text = (SAMPLE_ROOT / "legacy_issue_report_v1.txt").read_text(encoding="utf-8")

    first = issue_report_store.import_issue_report_text(text)
    second = issue_report_store.import_issue_report_text(text)

    assert second["id"] == first["id"]
    assert second["json_path"] == first["json_path"]
    assert len(list(debug_dir.glob("issue_report_*.json"))) == 1
    assert len(list(debug_dir.glob("issue_report_*.txt"))) == 1


@pytest.mark.parametrize(
    "report_id",
    [
        "../escape",
        r"C:\Users\name\case",
        "010-1234-5678",
        "user@example.com",
        "sk-secret-token",
    ],
)
def test_report_and_candidate_generated_paths_cannot_escape_output_root(
    tmp_path,
    monkeypatch,
    report_id,
):
    debug_dir = tmp_path / "debug"
    candidate_dir = tmp_path / "candidates"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)
    raw_text = (
        SAMPLE_ROOT / "legacy_issue_report_v1.txt"
    ).read_text(encoding="utf-8").replace(
        "Report ID: report_fixture_001",
        f"Report ID: {report_id}",
    )

    imported = issue_report_store.import_issue_report_text(raw_text)
    report_path = Path(imported["json_path"]).resolve()
    assert report_path.is_relative_to(debug_dir.resolve())
    assert artifact_io.safe_artifact_token(report_id) in report_path.name
    report = issue_report_store.load_report(report_path)
    assert report["id"] == report_id

    candidate = promote_issue_report_to_eval_candidate(
        report,
        output_dir=candidate_dir,
    )
    candidate_path = Path(candidate["json_path"]).resolve()
    logical_candidate_id = f"candidate_{report_id}"
    assert candidate_path.is_relative_to(candidate_dir.resolve())
    assert candidate_path.name == (
        f"{artifact_io.safe_artifact_token(logical_candidate_id)}.json"
    )
    assert candidate["id"] == logical_candidate_id


def test_all_slice_a_domain_writes_traverse_artifact_io(
    tmp_path,
    monkeypatch,
):
    json_writes: list[Path] = []
    text_writes: list[Path] = []
    real_write_json = artifact_io.atomic_write_json
    real_write_text = artifact_io.atomic_write_text

    def record_json(path, payload):
        json_writes.append(Path(path).resolve())
        return real_write_json(path, payload)

    def record_text(path, text):
        text_writes.append(Path(path).resolve())
        return real_write_text(path, text)

    monkeypatch.setattr(artifact_io, "atomic_write_json", record_json)
    monkeypatch.setattr(artifact_io, "atomic_write_text", record_text)
    debug_dir = tmp_path / "debug"
    monkeypatch.setattr(issue_report_store, "DEBUG_REPORT_DIR", debug_dir)

    imported = issue_report_store.import_issue_report_text(
        (SAMPLE_ROOT / "legacy_issue_report_v1.txt").read_text(
            encoding="utf-8"
        )
    )
    candidate_path, candidate, baseline = _reproduced_fixture_candidate(
        tmp_path
    )
    handoff = write_codex_handoff(
        candidate,
        baseline,
        output_dir=tmp_path / "handoffs",
        approved_by="local_operator",
        approval_reason="합성 전달물 검증",
    )

    assert {
        Path(imported["json_path"]).resolve(),
        candidate_path.resolve(),
        Path(baseline["json_path"]).resolve(),
        Path(handoff["manifest_path"]).resolve(),
    } <= set(json_writes)
    assert {
        Path(imported["file_path"]).resolve(),
        Path(handoff["markdown_path"]).resolve(),
    } <= set(text_writes)


def _promote_fixture_candidate(tmp_path) -> tuple[Path, dict]:
    report = issue_report_store.load_report(
        SAMPLE_ROOT / "legacy_issue_report_v1.json"
    )
    candidate = promote_issue_report_to_eval_candidate(
        report,
        output_dir=tmp_path / "candidates",
    )
    return Path(candidate["json_path"]), candidate


def _ready_fixture_candidate(tmp_path) -> tuple[Path, dict]:
    path, candidate = _promote_fixture_candidate(tmp_path)
    candidate = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "severity": "S3",
            "impact_area": "retrieval_source",
            "impact_summary": "잘못된 출처 때문에 답변 근거를 신뢰하기 어렵습니다.",
            "operator_decision": "accepted",
        },
        reason="합성 후보 분류",
    )
    candidate = transition_regression_candidate(
        path,
        to_status="triaged",
        expected_record_revision=candidate["record_revision"],
        reason="분류 완료",
    )
    candidate = transition_regression_candidate(
        path,
        to_status="needs_expectation",
        expected_record_revision=candidate["record_revision"],
        reason="수정 기대값 필요",
    )
    candidate = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "expected": {
                "route": "vectordb",
                "filters": {"target_name": "NAVER"},
                "sources": [
                    {"file_name": "correct.pdf", "report_type": "company"}
                ],
                "state": {},
                "manual_assertions": [],
            },
            "active_checks": ["route_pass", "source_hit"],
            "verification_type": "graph_contract",
        },
        reason="합성 기대값과 검사 항목 입력",
    )
    candidate = approve_candidate_expectation(
        path,
        expected_record_revision=candidate["record_revision"],
        reason="기대값 검토 완료",
    )
    candidate = transition_regression_candidate(
        path,
        to_status="ready",
        expected_record_revision=candidate["record_revision"],
        reason="재현 준비 완료",
    )
    return path, candidate


def _provenance() -> dict:
    return {
        "backend_mode": "synthetic_test",
        "snapshot_id": "feedback-loop-unit-test",
        "snapshot_available": True,
        "data_revision": "unit-test-data",
        "config_fingerprint": "a" * 64,
    }


def _reproduced_fixture_candidate(tmp_path) -> tuple[Path, dict, dict]:
    path, candidate = _ready_fixture_candidate(tmp_path)

    def failing_source_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "잘못된 출처 [1]",
            "rerank_info": [
                {"rank": 1, "file_name": "wrong.pdf", "report_type": "company"}
            ],
            "no_vector_results": False,
        }

    baseline = run_candidate_evaluation(
        candidate,
        failing_source_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )
    candidate = record_candidate_run(
        path,
        run=baseline,
        run_kind="baseline",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    candidate = transition_regression_candidate(
        path,
        to_status="reproduced",
        expected_record_revision=candidate["record_revision"],
        reason="합성 출처 오류 재현",
    )
    return path, candidate, baseline


def test_legacy_candidate_load_does_not_approve_observed_actual(tmp_path):
    path = tmp_path / "candidate_report_fixture_001.json"
    shutil.copy2(SAMPLE_ROOT / "legacy_candidate_v1.json", path)
    before = path.read_bytes()

    candidate = load_regression_candidate(path)

    assert candidate["schema_version"] == 2
    assert candidate["observed"]["actual"]["sources"][0]["file_name"] == "wrong.pdf"
    assert candidate["expected"] == {}
    assert candidate["expected_approved_at"] is None
    assert candidate["active_checks"] == []
    assert candidate["unknown_legacy_field"] == {"keep": True}
    assert path.read_bytes() == before


def test_candidate_update_preserves_unknown_fields_and_drops_ephemeral_fields(
    tmp_path,
):
    path = tmp_path / "candidate_report_fixture_001.json"
    shutil.copy2(SAMPLE_ROOT / "legacy_candidate_v1.json", path)
    candidate = load_regression_candidate(path)

    updated = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "severity": "S3",
            "impact_area": "retrieval_source",
            "impact_summary": "합성 영향",
            "operator_decision": "accepted",
        },
        reason="기존 후보 분류",
    )
    stored = json.loads(path.read_text(encoding="utf-8"))

    assert updated["unknown_legacy_field"] == {"keep": True}
    assert stored["unknown_legacy_field"] == {"keep": True}
    assert stored["schema_version"] == 2
    assert "json_path" not in stored
    assert "integrity_status" not in stored
    assert "warnings" not in stored


def test_promotion_is_idempotent_and_keeps_actual_out_of_expected(tmp_path):
    path, first = _promote_fixture_candidate(tmp_path)
    report = issue_report_store.load_report(
        SAMPLE_ROOT / "legacy_issue_report_v1.json"
    )

    second = promote_issue_report_to_eval_candidate(
        report,
        output_dir=path.parent,
    )

    assert first["id"] == second["id"]
    assert first["record_revision"] == second["record_revision"] == 0
    assert second["observed"]["actual"]["sources"][0]["file_name"] == "wrong.pdf"
    assert second["expected"] == {}
    assert len(list(path.parent.glob("*.json"))) == 1


def test_promotion_rejects_existing_candidate_with_different_source_identity(
    tmp_path,
):
    path, candidate = _promote_fixture_candidate(tmp_path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["source_refs"] = [{"kind": "report", "id": "different_report"}]
    artifact_io.atomic_write_json(path, stored)
    before = path.read_bytes()
    report = issue_report_store.load_report(
        SAMPLE_ROOT / "legacy_issue_report_v1.json"
    )

    with pytest.raises(CandidateConflictError):
        promote_issue_report_to_eval_candidate(
            report,
            output_dir=path.parent,
        )

    assert path.read_bytes() == before


def test_candidate_noop_update_keeps_bytes_revisions_and_history(tmp_path):
    path, candidate = _ready_fixture_candidate(tmp_path)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    unchanged = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={"impact_summary": candidate["impact_summary"]},
        reason="동일 값 저장 확인",
    )

    assert unchanged["record_revision"] == candidate["record_revision"]
    assert unchanged["contract_revision"] == candidate["contract_revision"]
    assert unchanged["history"] == candidate["history"]
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_candidate_updates_use_compare_and_swap_and_contract_epochs(tmp_path):
    path, candidate = _ready_fixture_candidate(tmp_path)

    with pytest.raises(CandidateConflictError):
        update_regression_candidate(
            path,
            expected_record_revision=0,
            changes={"impact_summary": "오래된 화면에서 덮어쓰기"},
            reason="충돌 확인",
        )

    old_contract_revision = candidate["contract_revision"]
    old_hash = candidate["candidate_hash"]
    candidate = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "expected": {
                "route": "vectordb",
                "filters": {"target_name": "NAVER"},
                "sources": [
                    {"file_name": "correct-v2.pdf", "report_type": "company"}
                ],
                "state": {},
                "manual_assertions": [],
            }
        },
        reason="기대 출처 변경",
    )

    assert candidate["triage_status"] == "needs_expectation"
    assert candidate["expected_approved_at"] is None
    assert candidate["contract_revision"] == old_contract_revision + 1
    assert candidate["candidate_hash"] != old_hash


def test_expectation_revocation_opens_new_contract_epoch(tmp_path):
    path, candidate = _ready_fixture_candidate(tmp_path)
    old_contract_revision = candidate["contract_revision"]
    old_hash = candidate["candidate_hash"]
    old_evidence = candidate["evidence"]

    revoked = revoke_candidate_expectation(
        path,
        expected_record_revision=candidate["record_revision"],
        reason="기대 결과 다시 편집",
    )

    assert revoked["triage_status"] == "needs_expectation"
    assert revoked["expected_approved_at"] is None
    assert revoked["expected_approved_by"] is None
    assert revoked["contract_revision"] == old_contract_revision + 1
    assert revoked["candidate_hash"] != old_hash
    assert revoked["evidence"] == old_evidence


def test_needs_info_candidate_can_be_reclassified_and_continue(tmp_path):
    path, candidate = _promote_fixture_candidate(tmp_path)
    candidate = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "severity": "S3",
            "impact_area": "answer_quality",
            "impact_summary": "추가 재현 정보가 필요함",
            "operator_decision": "needs_info",
        },
        reason="추가 정보 요청",
    )
    candidate = transition_regression_candidate(
        path,
        to_status="triaged",
        expected_record_revision=candidate["record_revision"],
        reason="추가 정보 대기 분류",
    )

    reclassified = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "impact_summary": "추가 재현 정보가 확보됨",
            "operator_decision": "accepted",
        },
        reason="추가 정보 반영 후 재분류",
    )
    continued = transition_regression_candidate(
        path,
        to_status="needs_expectation",
        expected_record_revision=reclassified["record_revision"],
        reason="기대 결과 작성 시작",
    )

    assert reclassified["operator_decision"] == "accepted"
    assert continued["triage_status"] == "needs_expectation"


def test_active_checks_only_determine_candidate_result():
    case = {
        "id": "candidate_fixture",
        "question": "NAVER 최신 리포트 요약",
        "expected_route": "vectordb",
        "expected_filters": {},
        "expected_sources": [{"file_name": "correct.pdf"}],
        "expected_state": {},
        "active_checks": ["route_pass"],
    }
    final_state = {
        "route": "vectordb",
        "search_filters": {},
        "generation": "인용이 없는 답변",
        "rerank_info": [{"file_name": "wrong.pdf"}],
        "no_vector_results": True,
    }

    result = evaluate_dataset_case_result(
        case,
        final_state,
        latency_seconds=99.0,
        latency_threshold_seconds=1.0,
    )

    assert result["status"] == "pass"
    assert result["source_hit"] is False
    assert result["failed_checks"] == []


def test_action_state_separates_automatic_and_manual_verification():
    automatic = canonicalize_regression_candidate(
        {
            "id": "candidate_automatic",
            "triage_status": "ready",
            "contract_revision": 1,
            "expected_approved_at": "2026-07-26T00:00:00+00:00",
            "verification_type": "graph_contract",
            "active_checks": ["route_pass"],
            "observed": {
                "reproduction_input": {"question": "합성 자동 검사 질문"},
                "actual": {},
            },
            "expected": {
                "route": "vectordb",
                "filters": {},
                "sources": [],
                "state": {},
                "manual_assertions": [],
            },
        }
    )
    manual = canonicalize_regression_candidate(
        {
            "id": "candidate_manual",
            "triage_status": "ready",
            "contract_revision": 1,
            "expected_approved_at": "2026-07-26T00:00:00+00:00",
            "verification_type": "manual_ui",
            "active_checks": ["manual_assertions_pass"],
            "observed": {
                "reproduction_input": {"question": "합성 수동 검사 질문"},
                "actual": {},
            },
            "expected": {
                "route": None,
                "filters": {},
                "sources": [],
                "state": {},
                "manual_assertions": [
                    {"id": "ui_result_visible", "text": "결과가 화면에 표시됨"}
                ],
            },
        }
    )

    automatic_actions = build_candidate_action_state(automatic)
    manual_actions = build_candidate_action_state(manual)

    assert automatic_actions["run_baseline"]["enabled"] is True
    assert automatic_actions["can_run_baseline"] is True
    assert automatic_actions["can_record_manual_reproduction"] is False
    assert (
        automatic_actions["record_manual_reproduction"]["enabled"] is False
    )
    assert manual_actions["run_baseline"]["enabled"] is False
    assert manual_actions["can_run_baseline"] is False
    assert manual_actions["can_record_manual_reproduction"] is True
    assert (
        manual_actions["record_manual_reproduction"]["enabled"] is True
    )
    for action_state in (automatic_actions, manual_actions):
        assert {
            "can_run_baseline",
            "can_record_manual_reproduction",
            "can_run_verification",
            "can_record_manual_verification",
            "can_preview_handoff",
            "can_start_fixing",
            "can_mark_not_reproducible",
            "blocked_reason",
        } <= set(action_state)


def test_manual_contract_and_checklist_reject_duplicate_assertion_ids(tmp_path):
    with pytest.raises(CandidateValidationError, match="must be unique"):
        canonicalize_regression_candidate(
            {
                "id": "candidate_duplicate_assertions",
                "expected": {
                    "route": None,
                    "filters": {},
                    "sources": [],
                    "state": {},
                    "manual_assertions": [
                        {"id": "visible", "text": "결과 표시"},
                        {"id": "visible", "text": "결과 재확인"},
                    ],
                },
            }
        )

    path = tmp_path / "candidate_manual.json"
    candidate = canonicalize_regression_candidate(
        {
            "id": "candidate_manual",
            "triage_status": "ready",
            "record_revision": 0,
            "contract_revision": 1,
            "expected_approved_at": "2026-07-26T00:00:00+00:00",
            "expected_approved_by": "local_operator",
            "verification_type": "manual_ui",
            "active_checks": ["manual_assertions_pass"],
            "observed": {
                "reproduction_input": {"question": "합성 수동 검사 질문"},
                "actual": {},
            },
            "expected": {
                "route": None,
                "filters": {},
                "sources": [],
                "state": {},
                "manual_assertions": [
                    {"id": "visible", "text": "결과 표시"},
                    {"id": "usable", "text": "결과 사용 가능"},
                ],
            },
        }
    )
    path.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(
        CandidateValidationError,
        match="does not match approved assertions",
    ):
        record_candidate_manual_evidence(
            path,
            evidence_kind="manual_reproduction",
            checklist_results=[
                {"assertion_id": "visible", "passed": False, "note": ""},
                {"assertion_id": "visible", "passed": False, "note": ""},
                {"assertion_id": "usable", "passed": False, "note": ""},
            ],
            expected_record_revision=candidate["record_revision"],
            expected_contract_revision=candidate["contract_revision"],
            expected_candidate_hash=candidate["candidate_hash"],
            reason="중복 수동 검사 거부 확인",
        )


def test_manual_evidence_rejects_unapproved_contract_before_persisting(
    tmp_path,
):
    path = tmp_path / "candidate_manual_unapproved.json"
    candidate = canonicalize_regression_candidate(
        {
            "id": "candidate_manual_unapproved",
            "triage_status": "ready",
            "record_revision": 0,
            "contract_revision": 1,
            "expected_approved_at": None,
            "expected_approved_by": None,
            "verification_type": "manual_ui",
            "active_checks": ["manual_assertions_pass"],
            "observed": {
                "reproduction_input": {"question": "합성 수동 검증 질문"},
                "actual": {},
            },
            "expected": {
                "route": None,
                "filters": {},
                "sources": [],
                "state": {},
                "manual_assertions": [
                    {"id": "visible", "text": "결과가 표시됨"}
                ],
            },
        }
    )
    stored = {
        key: value
        for key, value in candidate.items()
        if key not in {"json_path", "integrity_status", "warnings"}
    }
    artifact_io.atomic_write_json(path, stored)
    before = path.read_bytes()

    with pytest.raises(
        CandidateValidationError,
        match="requires an approved expectation",
    ):
        record_candidate_manual_evidence(
            path,
            evidence_kind="manual_reproduction",
            checklist_results=[
                {
                    "assertion_id": "visible",
                    "passed": False,
                    "note": "",
                }
            ],
            expected_record_revision=candidate["record_revision"],
            expected_contract_revision=candidate["contract_revision"],
            expected_candidate_hash=candidate["candidate_hash"],
            reason="승인 없는 증거 연결 시도",
        )

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "expected",
    [
        {
            "route": "vectordb",
            "filters": {"target_name": ["NAVER"]},
            "sources": [],
            "state": {},
            "manual_assertions": [],
        },
        {
            "route": "vectordb",
            "filters": {"file_names": ["report.pdf", 7]},
            "sources": [],
            "state": {},
            "manual_assertions": [],
        },
        {
            "route": "vectordb",
            "filters": {},
            "sources": [{"file_name": ["report.pdf"]}],
            "state": {},
            "manual_assertions": [],
        },
        {
            "route": "vectordb",
            "filters": {},
            "sources": [],
            "state": {"scope_source": ["previous_turn"]},
            "manual_assertions": [],
        },
    ],
    ids=("filter-string", "file-name-list", "source-string", "state-string"),
)
def test_candidate_expected_rejects_handoff_incompatible_value_types(expected):
    with pytest.raises(CandidateValidationError, match="invalid"):
        canonicalize_regression_candidate(
            {
                "id": "candidate_invalid_expected_type",
                "expected": expected,
            }
        )


@pytest.mark.parametrize(
    "active_checks",
    [
        ["unknown_check"],
        ["route_pass", "route_pass"],
    ],
    ids=("unknown", "duplicate"),
)
def test_invalid_active_check_is_rejected_before_graph_invocation(
    tmp_path,
    active_checks,
):
    path, candidate = _ready_fixture_candidate(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["active_checks"] = active_checks
    raw["candidate_hash"] = canonicalize_regression_candidate(raw)["candidate_hash"]
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    candidate = load_regression_candidate(path)
    called = False

    def fake_invoke(payload, config=None):
        nonlocal called
        called = True
        return {}

    with pytest.raises(CandidateValidationError):
        run_candidate_evaluation(
            candidate,
            fake_invoke,
            output_dir=tmp_path / "runs",
            run_kind="baseline",
            provenance=_provenance(),
        )

    assert called is False


def test_slice_a_synthetic_candidate_closes_with_current_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.monitoring.get_app_version", lambda: "0.5.1")
    path, candidate = _ready_fixture_candidate(tmp_path)
    ready_hash = candidate["candidate_hash"]

    def baseline_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "잘못된 출처 [1]",
            "rerank_info": [
                {"rank": 1, "file_name": "wrong.pdf", "report_type": "company"}
            ],
            "no_vector_results": False,
        }

    baseline = run_candidate_evaluation(
        candidate,
        baseline_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )
    assert baseline["results"][0]["failed_checks"] == ["source_hit"]
    candidate = record_candidate_run(
        path,
        run=baseline,
        run_kind="baseline",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    candidate = transition_regression_candidate(
        path,
        to_status="reproduced",
        expected_record_revision=candidate["record_revision"],
        reason="수정 전 출처 오류 재현",
    )
    contract_revision_before_handoff = candidate["contract_revision"]
    candidate_hash_before_handoff = candidate["candidate_hash"]
    record_revision_before_handoff = candidate["record_revision"]
    handoff = write_codex_handoff(
        candidate,
        baseline,
        output_dir=tmp_path / "handoffs",
        approved_by="local_operator",
        approval_reason="민감정보 제거 결과와 전달 내용을 검토함",
    )
    candidate = record_candidate_handoff(
        path,
        handoff=handoff,
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    assert candidate["record_revision"] == record_revision_before_handoff + 1
    assert candidate["contract_revision"] == contract_revision_before_handoff
    assert candidate["candidate_hash"] == candidate_hash_before_handoff
    candidate = transition_regression_candidate(
        path,
        to_status="fixing",
        expected_record_revision=candidate["record_revision"],
        reason="출처 선택 로직 수정 시작",
    )

    def verification_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "올바른 출처 [1]",
            "rerank_info": [
                {
                    "rank": 1,
                    "file_name": "correct.pdf",
                    "report_type": "company",
                }
            ],
            "no_vector_results": False,
        }

    verification = run_candidate_evaluation(
        candidate,
        verification_invoke,
        output_dir=tmp_path / "runs",
        run_kind="verification",
        provenance=_provenance(),
    )
    assert verification["results"][0]["status"] == "pass"
    candidate = record_candidate_run(
        path,
        run=verification,
        run_kind="verification",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    candidate = transition_regression_candidate(
        path,
        to_status="verified",
        expected_record_revision=candidate["record_revision"],
        reason="승인 검사 통과",
    )
    candidate = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "fixed_in_version": "0.5.2",
            "closure_reason": "합성 출처 회귀 해결",
            "suite_exclusion_reason": "test_fixture_only",
        },
        reason="종료 근거 기록",
    )
    candidate = transition_regression_candidate(
        path,
        to_status="closed",
        expected_record_revision=candidate["record_revision"],
        reason="합성 종단 시험 종료",
    )
    reloaded = load_regression_candidate(path)

    assert reloaded["triage_status"] == "closed"
    assert reloaded["candidate_hash"] == ready_hash
    assert reloaded["evidence"]["baseline_runs"][0]["status"] == "fail"
    assert reloaded["evidence"]["verification_runs"][0]["status"] == "pass"
    assert reloaded["handoffs"][0]["handoff_id"] == handoff["handoff_id"]
    assert (
        reloaded["handoffs"][0]["manifest_sha256"]
        == handoff["manifest_sha256"]
    )
    assert reloaded["history"][-1]["to_status"] == "closed"


def test_handoff_orphan_is_rediscovered_attached_and_idempotent(tmp_path):
    path, candidate, baseline = _reproduced_fixture_candidate(tmp_path)
    handoff_root = tmp_path / "handoffs"
    handoff = write_codex_handoff(
        candidate,
        baseline,
        output_dir=handoff_root,
        approved_by="local_operator",
        approval_reason="합성 전달물 검토",
    )

    reloaded = load_regression_candidate(path)
    discovered = discover_candidate_orphan_handoffs(
        reloaded,
        output_dir=handoff_root,
    )

    assert [item["handoff_id"] for item in discovered["attachable"]] == [
        handoff["handoff_id"]
    ]
    before_record_revision = reloaded["record_revision"]
    before_contract_revision = reloaded["contract_revision"]
    before_candidate_hash = reloaded["candidate_hash"]
    attached = record_candidate_handoff(
        path,
        handoff=discovered["attachable"][0],
        expected_record_revision=reloaded["record_revision"],
        expected_contract_revision=reloaded["contract_revision"],
        expected_candidate_hash=reloaded["candidate_hash"],
    )

    assert attached["record_revision"] == before_record_revision + 1
    assert attached["contract_revision"] == before_contract_revision
    assert attached["candidate_hash"] == before_candidate_hash
    assert attached["handoffs"][0]["manifest_sha256"] == handoff["manifest_sha256"]

    duplicate = record_candidate_handoff(
        path,
        handoff=handoff,
        expected_record_revision=attached["record_revision"],
        expected_contract_revision=attached["contract_revision"],
        expected_candidate_hash=attached["candidate_hash"],
    )
    assert duplicate["record_revision"] == attached["record_revision"]
    assert len(duplicate["handoffs"]) == 1


def test_export_metadata_edit_keeps_run_current_but_stales_orphan_handoff(
    tmp_path,
):
    path, candidate, baseline = _reproduced_fixture_candidate(tmp_path)
    handoff_root = tmp_path / "handoffs"
    handoff = write_codex_handoff(
        candidate,
        baseline,
        output_dir=handoff_root,
        approved_by="local_operator",
        approval_reason="합성 전달물 검토",
    )
    contract_revision = candidate["contract_revision"]
    candidate_hash = candidate["candidate_hash"]

    candidate = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={"severity": "S2"},
        reason="전달물에 포함되는 분류 정보 변경",
    )
    discovered = discover_candidate_orphan_handoffs(
        candidate,
        output_dir=handoff_root,
    )

    assert candidate["contract_revision"] == contract_revision
    assert candidate["candidate_hash"] == candidate_hash
    assert discovered["attachable"] == []
    assert [item["handoff_id"] for item in discovered["stale"]] == [
        handoff["handoff_id"]
    ]
    with pytest.raises(CandidateValidationError, match="content is stale"):
        record_candidate_handoff(
            path,
            handoff=handoff,
            expected_record_revision=candidate["record_revision"],
            expected_contract_revision=candidate["contract_revision"],
            expected_candidate_hash=candidate["candidate_hash"],
        )


def test_verification_failure_stays_fixing(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.monitoring.get_app_version", lambda: "0.5.1")
    path, candidate = _ready_fixture_candidate(tmp_path)

    def failing_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "오류 [1]",
            "rerank_info": [{"rank": 1, "file_name": "wrong.pdf"}],
        }

    baseline = run_candidate_evaluation(
        candidate,
        failing_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )
    candidate = record_candidate_run(
        path,
        run=baseline,
        run_kind="baseline",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    candidate = transition_regression_candidate(
        path,
        to_status="reproduced",
        expected_record_revision=candidate["record_revision"],
        reason="재현",
    )
    candidate = transition_regression_candidate(
        path,
        to_status="fixing",
        expected_record_revision=candidate["record_revision"],
        reason="수정 시작",
    )
    verification = run_candidate_evaluation(
        candidate,
        failing_invoke,
        output_dir=tmp_path / "runs",
        run_kind="verification",
        provenance=_provenance(),
    )
    candidate = record_candidate_run(
        path,
        run=verification,
        run_kind="verification",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )

    with pytest.raises(CandidateTransitionError):
        transition_regression_candidate(
            path,
            to_status="verified",
            expected_record_revision=candidate["record_revision"],
            reason="실패 결과로 검증 시도",
        )

    assert load_regression_candidate(path)["triage_status"] == "fixing"


def test_snapshot_unavailable_is_blocked_before_graph_invocation(tmp_path):
    path, candidate = _ready_fixture_candidate(tmp_path)
    called = False

    def fake_invoke(payload, config=None):
        nonlocal called
        called = True
        return {}

    provenance = _provenance()
    provenance["snapshot_available"] = False
    run = run_candidate_evaluation(
        candidate,
        fake_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=provenance,
    )

    assert called is False
    assert run["run_status"] == "blocked"
    assert run["blocked_reason"] == "snapshot_unavailable"
    with pytest.raises(CandidateValidationError):
        record_candidate_run(
            path,
            run=run,
            run_kind="baseline",
            expected_record_revision=candidate["record_revision"],
            expected_contract_revision=candidate["contract_revision"],
            expected_candidate_hash=candidate["candidate_hash"],
        )


def test_orphan_run_can_be_discovered_and_attached_after_reload(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.monitoring.get_app_version", lambda: "0.5.1")
    path, candidate = _ready_fixture_candidate(tmp_path)

    def failing_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "잘못된 출처 [1]",
            "rerank_info": [{"rank": 1, "file_name": "wrong.pdf"}],
        }

    run = run_candidate_evaluation(
        candidate,
        failing_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )
    reloaded = load_regression_candidate(path)

    recovery = discover_candidate_orphan_runs(
        reloaded,
        run_dir=tmp_path / "runs",
    )

    assert [item["run_id"] for item in recovery["attachable"]] == [run["run_id"]]
    attached = record_candidate_run(
        path,
        run=recovery["attachable"][0],
        run_kind="baseline",
        expected_record_revision=reloaded["record_revision"],
        expected_contract_revision=reloaded["contract_revision"],
        expected_candidate_hash=reloaded["candidate_hash"],
    )
    assert attached["evidence"]["baseline_runs"][0]["run_id"] == run["run_id"]


def test_record_candidate_run_requires_persisted_json_artifact(tmp_path):
    path, candidate = _ready_fixture_candidate(tmp_path)

    def failing_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "잘못된 출처 [1]",
            "rerank_info": [{"rank": 1, "file_name": "wrong.pdf"}],
        }

    run = run_candidate_evaluation(
        candidate,
        failing_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )
    detached = dict(run)
    detached.pop("json_path")

    with pytest.raises(
        CandidateValidationError,
        match="persisted run artifact path is required",
    ):
        record_candidate_run(
            path,
            run=detached,
            run_kind="baseline",
            expected_record_revision=candidate["record_revision"],
            expected_contract_revision=candidate["contract_revision"],
            expected_candidate_hash=candidate["candidate_hash"],
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["provenance"].update(
            {"snapshot_available": False}
        ),
        lambda payload: payload["summary"].update(
            {"passed": 1, "failed": 0}
        ),
        lambda payload: payload["results"][0].update(
            {"failed_checks": []}
        ),
    ],
    ids=[
        "snapshot-unavailable",
        "summary-contradiction",
        "failed-check-contradiction",
    ],
)
def test_record_candidate_run_rejects_semantically_inconsistent_artifact(
    tmp_path,
    mutate,
):
    path, candidate = _ready_fixture_candidate(tmp_path)

    def failing_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "잘못된 출처 [1]",
            "rerank_info": [{"rank": 1, "file_name": "wrong.pdf"}],
        }

    run = run_candidate_evaluation(
        candidate,
        failing_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )
    run_path = Path(run["json_path"])
    forged = json.loads(run_path.read_text(encoding="utf-8"))
    mutate(forged)
    forged["run_hash"] = compute_evaluation_run_hash(forged)
    artifact_io.atomic_write_json(run_path, forged)
    supplied = dict(forged)
    supplied["json_path"] = str(run_path)
    supplied["integrity_status"] = "valid"
    before = path.read_bytes()

    with pytest.raises(CandidateValidationError):
        record_candidate_run(
            path,
            run=supplied,
            run_kind="baseline",
            expected_record_revision=candidate["record_revision"],
            expected_contract_revision=candidate["contract_revision"],
            expected_candidate_hash=candidate["candidate_hash"],
        )

    assert path.read_bytes() == before


def test_tampered_run_cannot_drive_lifecycle_transition(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.monitoring.get_app_version", lambda: "0.5.1")
    path, candidate = _ready_fixture_candidate(tmp_path)

    def failing_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "잘못된 출처 [1]",
            "rerank_info": [{"rank": 1, "file_name": "wrong.pdf"}],
        }

    run = run_candidate_evaluation(
        candidate,
        failing_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )
    candidate = record_candidate_run(
        path,
        run=run,
        run_kind="baseline",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    run_path = Path(run["json_path"])
    tampered = json.loads(run_path.read_text(encoding="utf-8"))
    tampered["results"][0]["status"] = "pass"
    run_path.write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(CandidateTransitionError):
        transition_regression_candidate(
            path,
            to_status="reproduced",
            expected_record_revision=candidate["record_revision"],
            reason="손상된 실행 결과 사용 시도",
        )

    assert load_regression_candidate(path)["triage_status"] == "ready"


def test_candidate_hash_mismatch_is_blocking_and_excluded_from_listing(tmp_path):
    path, _ = _promote_fixture_candidate(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["observed"]["reproduction_input"]["question"] = "변조된 질문"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    discovered = list_regression_candidate_artifacts(path.parent)

    assert discovered["items"] == []
    assert discovered["warnings"] == [
        {
            "code": "candidate_hash_mismatch",
            "path": str(path),
            "blocking": True,
        }
    ]


def test_v2_candidate_without_hash_is_blocking_and_excluded_from_listing(
    tmp_path,
):
    path, _ = _promote_fixture_candidate(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("candidate_hash")
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(CandidateLoadError, match="candidate_hash_missing"):
        load_regression_candidate(path)
    discovered = list_regression_candidate_artifacts(path.parent)

    assert discovered["items"] == []
    assert discovered["warnings"] == [
        {
            "code": "candidate_hash_missing",
            "path": str(path),
            "blocking": True,
        }
    ]


def test_malformed_candidate_revision_is_a_blocking_listing_warning(tmp_path):
    path, _ = _promote_fixture_candidate(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["record_revision"] = "not-a-revision"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    discovered = list_regression_candidate_artifacts(path.parent)

    assert discovered["items"] == []
    assert discovered["warnings"] == [
        {
            "code": "malformed_json",
            "path": str(path),
            "blocking": True,
        }
    ]


@pytest.mark.parametrize(
    "invalid_bytes",
    [b"\xff", b'{"schema_version": 2, "value": NaN}'],
)
def test_invalid_candidate_encoding_or_non_finite_json_is_blocking(
    tmp_path,
    invalid_bytes,
):
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    path = candidate_dir / "candidate_invalid.json"
    path.write_bytes(invalid_bytes)

    discovered = list_regression_candidate_artifacts(candidate_dir)

    assert discovered["items"] == []
    assert discovered["warnings"] == [
        {
            "code": "malformed_json",
            "path": str(path),
            "blocking": True,
        }
    ]


def test_invalid_evaluation_run_encoding_is_a_blocking_warning(tmp_path):
    run_dir = tmp_path / "runs"
    run_dir.mkdir()
    path = run_dir / "evaluation_run_20260726T001100Z_abcd1234.json"
    path.write_bytes(b"\xff")

    discovered = list_candidate_evaluation_run_artifacts(run_dir)

    assert discovered["items"] == []
    assert discovered["warnings"] == [
        {
            "code": "malformed_run",
            "path": str(path),
            "blocking": True,
        }
    ]


def test_duplicate_transition_rejects_malformed_target_candidate(tmp_path):
    path, candidate = _promote_fixture_candidate(tmp_path)
    target_path = path.parent / "candidate_target.json"
    target_path.write_text("{", encoding="utf-8")
    candidate = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={"duplicate_of": "candidate_target"},
        reason="중복 대상 지정",
    )

    with pytest.raises(CandidateTransitionError, match="missing or invalid"):
        transition_regression_candidate(
            path,
            to_status="duplicate",
            expected_record_revision=candidate["record_revision"],
            reason="합성 중복 처리",
        )


def _manual_ready_fixture_candidate(tmp_path) -> tuple[Path, dict]:
    path, candidate = _ready_fixture_candidate(tmp_path)
    candidate = update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "expected": {
                "route": None,
                "filters": {},
                "sources": [],
                "state": {},
                "manual_assertions": [
                    {
                        "id": "answer_visible",
                        "text": "답변이 화면에 표시됨",
                    },
                    {
                        "id": "source_grounded",
                        "text": "답변 근거가 올바름",
                    },
                ],
            },
            "active_checks": ["manual_assertions_pass"],
            "verification_type": "manual_ui",
        },
        reason="수동 검증 계약으로 변경",
    )
    assert candidate["triage_status"] == "needs_expectation"
    candidate = approve_candidate_expectation(
        path,
        expected_record_revision=candidate["record_revision"],
        reason="수동 검증 계약 승인",
    )
    candidate = transition_regression_candidate(
        path,
        to_status="ready",
        expected_record_revision=candidate["record_revision"],
        reason="수동 재현 준비 완료",
    )
    return path, candidate


def test_manual_evidence_drives_reproduction_and_verification_lifecycle(
    tmp_path,
):
    path, candidate = _manual_ready_fixture_candidate(tmp_path)
    contract_revision = candidate["contract_revision"]
    candidate_hash = candidate["candidate_hash"]
    record_revision = candidate["record_revision"]

    candidate = record_candidate_manual_evidence(
        path,
        evidence_kind="manual_reproduction",
        checklist_results=[
            {
                "assertion_id": "answer_visible",
                "passed": True,
                "note": "표시 확인",
            },
            {
                "assertion_id": "source_grounded",
                "passed": False,
                "note": "잘못된 출처",
            },
        ],
        expected_record_revision=record_revision,
        expected_contract_revision=contract_revision,
        expected_candidate_hash=candidate_hash,
        reason="수정 전 수동 재현",
    )

    assert candidate["record_revision"] == record_revision + 1
    assert candidate["contract_revision"] == contract_revision
    assert candidate["candidate_hash"] == candidate_hash
    assert (
        candidate["evidence"]["manual_reproductions"][-1]["outcome"]
        == "reproduced"
    )
    actions = build_candidate_action_state(candidate)
    assert actions["mark_reproduced"]["enabled"] is True
    assert actions["mark_not_reproducible"]["enabled"] is False

    candidate = transition_regression_candidate(
        path,
        to_status="reproduced",
        expected_record_revision=candidate["record_revision"],
        reason="수동 재현 결과 확정",
    )
    candidate = transition_regression_candidate(
        path,
        to_status="fixing",
        expected_record_revision=candidate["record_revision"],
        reason="수정 시작",
    )
    candidate = record_candidate_manual_evidence(
        path,
        evidence_kind="manual_verification",
        checklist_results=[
            {
                "assertion_id": "answer_visible",
                "passed": True,
                "note": "표시 확인",
            },
            {
                "assertion_id": "source_grounded",
                "passed": True,
                "note": "출처 확인",
            },
        ],
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
        reason="수정 후 수동 검증",
    )

    assert (
        candidate["evidence"]["manual_verifications"][-1]["outcome"]
        == "passed"
    )
    assert build_candidate_action_state(candidate)["mark_verified"][
        "enabled"
    ] is True
    candidate = transition_regression_candidate(
        path,
        to_status="verified",
        expected_record_revision=candidate["record_revision"],
        reason="수동 검증 통과 확정",
    )
    assert candidate["triage_status"] == "verified"


def test_manual_non_reproduction_only_enables_terminal_alternative(tmp_path):
    path, candidate = _manual_ready_fixture_candidate(tmp_path)
    candidate = record_candidate_manual_evidence(
        path,
        evidence_kind="manual_reproduction",
        checklist_results=[
            {
                "assertion_id": "answer_visible",
                "passed": True,
                "note": "",
            },
            {
                "assertion_id": "source_grounded",
                "passed": True,
                "note": "",
            },
        ],
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
        reason="오류가 재현되지 않음",
    )

    actions = build_candidate_action_state(candidate)
    assert actions["mark_reproduced"]["enabled"] is False
    assert actions["mark_not_reproducible"]["enabled"] is True
    assert actions["can_mark_not_reproducible"] is True
    with pytest.raises(CandidateTransitionError):
        transition_regression_candidate(
            path,
            to_status="reproduced",
            expected_record_revision=candidate["record_revision"],
            reason="잘못된 재현 확정 시도",
        )
    candidate = transition_regression_candidate(
        path,
        to_status="not_reproducible",
        expected_record_revision=candidate["record_revision"],
        reason="현재 계약에서 재현되지 않음",
    )
    assert candidate["triage_status"] == "not_reproducible"


def test_baseline_pass_enables_not_reproducible_but_not_reproduced(tmp_path):
    path, candidate = _ready_fixture_candidate(tmp_path)

    def passing_invoke(payload, config=None):
        return {
            "route": "vectordb",
            "search_filters": {"target_name": "NAVER"},
            "generation": "올바른 출처 [1]",
            "rerank_info": [
                {
                    "rank": 1,
                    "file_name": "correct.pdf",
                    "report_type": "company",
                }
            ],
            "no_vector_results": False,
        }

    run = run_candidate_evaluation(
        candidate,
        passing_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )
    candidate = record_candidate_run(
        path,
        run=run,
        run_kind="baseline",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )

    assert run["results"][0]["status"] == "pass"
    assert build_candidate_action_state(candidate)["mark_reproduced"][
        "enabled"
    ] is False
    with pytest.raises(CandidateTransitionError):
        transition_regression_candidate(
            path,
            to_status="reproduced",
            expected_record_revision=candidate["record_revision"],
            reason="통과 실행을 실패로 오인",
        )
    candidate = transition_regression_candidate(
        path,
        to_status="not_reproducible",
        expected_record_revision=candidate["record_revision"],
        reason="수정 전 오류가 재현되지 않음",
    )
    assert candidate["triage_status"] == "not_reproducible"


def test_candidate_runner_persists_safe_error_attempt_without_evidence(
    tmp_path,
):
    path, candidate = _ready_fixture_candidate(tmp_path)
    original_revision = candidate["record_revision"]
    secret = "customer@example.com sk-abcdefghijklmnop"

    def failing_invoke(payload, config=None):
        raise RuntimeError(secret)

    run = run_candidate_evaluation(
        candidate,
        failing_invoke,
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(),
    )

    assert run["run_status"] == "error"
    assert run["error_type"] == "RuntimeError"
    assert run["error_stage"] == "graph_invoke"
    assert secret not in Path(run["json_path"]).read_text(encoding="utf-8")
    recovery = discover_candidate_orphan_runs(
        load_regression_candidate(path),
        run_dir=tmp_path / "runs",
    )
    assert [item["run_id"] for item in recovery["failed_attempts"]] == [
        run["run_id"]
    ]
    with pytest.raises(CandidateValidationError):
        record_candidate_run(
            path,
            run=run,
            run_kind="baseline",
            expected_record_revision=candidate["record_revision"],
            expected_contract_revision=candidate["contract_revision"],
            expected_candidate_hash=candidate["candidate_hash"],
        )
    assert load_regression_candidate(path)["record_revision"] == original_revision


def test_candidate_lifecycle_rejects_undeclared_transitions(tmp_path):
    new_path, new_candidate = _promote_fixture_candidate(tmp_path / "new")
    with pytest.raises(CandidateTransitionError):
        transition_regression_candidate(
            new_path,
            to_status="ready",
            expected_record_revision=new_candidate["record_revision"],
            reason="단계 건너뛰기",
        )

    ready_path, ready_candidate = _ready_fixture_candidate(tmp_path / "ready")
    with pytest.raises(CandidateTransitionError):
        transition_regression_candidate(
            ready_path,
            to_status="verified",
            expected_record_revision=ready_candidate["record_revision"],
            reason="검증 건너뛰기",
        )

    fixing_path, fixing_candidate, _ = _reproduced_fixture_candidate(
        tmp_path / "fixing"
    )
    fixing_candidate = transition_regression_candidate(
        fixing_path,
        to_status="fixing",
        expected_record_revision=fixing_candidate["record_revision"],
        reason="수정 시작",
    )
    with pytest.raises(CandidateTransitionError):
        transition_regression_candidate(
            fixing_path,
            to_status="closed",
            expected_record_revision=fixing_candidate["record_revision"],
            reason="검증 건너뛰기",
        )


class TestA1StorageSchema:
    test_legacy_report_is_read_only = staticmethod(
        test_legacy_report_load_is_read_only_and_separates_observed
    )
    test_native_v2_report_mapping = staticmethod(
        test_native_v2_report_observed_actual_is_promoted_without_legacy_context
    )
    test_missing_text_repair = staticmethod(
        test_report_discovery_returns_json_only_and_repairs_text_companion
    )
    test_malformed_json_fallback = staticmethod(
        test_report_discovery_falls_back_to_text_when_json_is_malformed
    )
    test_unreadable_pair_is_blocking = staticmethod(
        test_report_discovery_marks_both_unreadable_companions_as_blocking
    )
    test_interrupted_companion_write = staticmethod(
        test_report_write_failure_keeps_canonical_json_for_recovery
    )
    test_import_is_idempotent = staticmethod(
        test_email_import_is_idempotent_by_report_id
    )
    test_generated_paths_are_contained = staticmethod(
        test_report_and_candidate_generated_paths_cannot_escape_output_root
    )
    test_all_domain_writes_use_atomic_boundary = staticmethod(
        test_all_slice_a_domain_writes_traverse_artifact_io
    )


class TestA2CandidateLifecycle:
    test_legacy_candidate_is_unapproved = staticmethod(
        test_legacy_candidate_load_does_not_approve_observed_actual
    )
    test_updates_preserve_unknown_fields = staticmethod(
        test_candidate_update_preserves_unknown_fields_and_drops_ephemeral_fields
    )
    test_promotion_is_idempotent = staticmethod(
        test_promotion_is_idempotent_and_keeps_actual_out_of_expected
    )
    test_promotion_source_collision_is_rejected = staticmethod(
        test_promotion_rejects_existing_candidate_with_different_source_identity
    )
    test_noop_update_is_read_only = staticmethod(
        test_candidate_noop_update_keeps_bytes_revisions_and_history
    )
    test_compare_and_swap_and_contract_epochs = staticmethod(
        test_candidate_updates_use_compare_and_swap_and_contract_epochs
    )
    test_expectation_revocation_starts_new_epoch = staticmethod(
        test_expectation_revocation_opens_new_contract_epoch
    )
    test_needs_info_can_be_reclassified = staticmethod(
        test_needs_info_candidate_can_be_reclassified_and_continue
    )
    test_hash_mismatch_is_blocking = staticmethod(
        test_candidate_hash_mismatch_is_blocking_and_excluded_from_listing
    )
    test_missing_hash_is_blocking = staticmethod(
        test_v2_candidate_without_hash_is_blocking_and_excluded_from_listing
    )
    test_malformed_revision_is_blocking = staticmethod(
        test_malformed_candidate_revision_is_a_blocking_listing_warning
    )
    test_invalid_candidate_json_is_blocking = staticmethod(
        test_invalid_candidate_encoding_or_non_finite_json_is_blocking
    )
    test_invalid_duplicate_target_is_rejected = staticmethod(
        test_duplicate_transition_rejects_malformed_target_candidate
    )
    test_undeclared_transitions_are_rejected = staticmethod(
        test_candidate_lifecycle_rejects_undeclared_transitions
    )


class TestA3ActiveChecks:
    test_only_active_checks_decide_result = staticmethod(
        test_active_checks_only_determine_candidate_result
    )
    test_manual_and_automatic_actions_are_separate = staticmethod(
        test_action_state_separates_automatic_and_manual_verification
    )
    test_invalid_check_stops_before_graph = staticmethod(
        test_invalid_active_check_is_rejected_before_graph_invocation
    )


class TestA4Evidence:
    test_manual_evidence_requires_exact_assertions = staticmethod(
        test_manual_contract_and_checklist_reject_duplicate_assertion_ids
    )
    test_manual_evidence_requires_approval = staticmethod(
        test_manual_evidence_rejects_unapproved_contract_before_persisting
    )
    test_failed_verification_stays_fixing = staticmethod(
        test_verification_failure_stays_fixing
    )
    test_missing_snapshot_is_blocked = staticmethod(
        test_snapshot_unavailable_is_blocked_before_graph_invocation
    )
    test_orphan_run_can_be_attached = staticmethod(
        test_orphan_run_can_be_discovered_and_attached_after_reload
    )
    test_run_requires_disk_artifact = staticmethod(
        test_record_candidate_run_requires_persisted_json_artifact
    )
    test_run_evidence_must_be_semantically_consistent = staticmethod(
        test_record_candidate_run_rejects_semantically_inconsistent_artifact
    )
    test_tampered_run_is_rejected = staticmethod(
        test_tampered_run_cannot_drive_lifecycle_transition
    )
    test_invalid_run_json_is_blocking = staticmethod(
        test_invalid_evaluation_run_encoding_is_a_blocking_warning
    )
    test_manual_lifecycle_uses_current_evidence = staticmethod(
        test_manual_evidence_drives_reproduction_and_verification_lifecycle
    )
    test_manual_non_reproduction_is_terminal = staticmethod(
        test_manual_non_reproduction_only_enables_terminal_alternative
    )
    test_baseline_pass_is_non_reproducing = staticmethod(
        test_baseline_pass_enables_not_reproducible_but_not_reproduced
    )
    test_graph_error_attempt_is_safe = staticmethod(
        test_candidate_runner_persists_safe_error_attempt_without_evidence
    )


class TestA5Handoff:
    test_orphan_handoff_can_be_attached = staticmethod(
        test_handoff_orphan_is_rediscovered_attached_and_idempotent
    )
    test_contract_edit_stales_handoff_only = staticmethod(
        test_export_metadata_edit_keeps_run_current_but_stales_orphan_handoff
    )


class TestA7EndToEnd:
    test_synthetic_candidate_closes_with_current_evidence = staticmethod(
        test_slice_a_synthetic_candidate_closes_with_current_evidence
    )
