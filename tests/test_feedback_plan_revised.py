from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from src.core import artifact_io, issue_report_store, monitoring
from src.core.feedback_handoff import (
    build_codex_handoff_payload,
    validate_codex_handoff_payload,
    write_codex_handoff,
)
from src.core.reproduction_manifest import (
    build_reproduction_manifest,
)


def _manifest(**changes):
    value = {
        "app_version": "0.6.0",
        "code_revision": "abc123",
        "code_fingerprint": "1" * 64,
        "model_fingerprint": "2" * 64,
        "prompt_fingerprint": "3" * 64,
        "tool_fingerprint": "4" * 64,
        "data_revision": "data-2026-07-29",
        "index_revision": "index-2026-07-29",
        "config_fingerprint": "5" * 64,
        "feature_flags_fingerprint": "6" * 64,
    }
    value.update(changes)
    return build_reproduction_manifest(**value)


def _candidate(
    *,
    candidate_id: str = "candidate_profile",
    profile: str = "balanced",
    hard_checks: list[str] | None = None,
    soft_objectives: list[str] | None = None,
    performance_budget: dict | None = None,
    verification_type: str = "graph_contract",
    triage_status: str = "ready",
    route: str = "vectordb",
):
    hard_checks = hard_checks or ["route_pass"]
    plan = monitoring.build_validation_plan(
        profile,
        hard_checks=hard_checks,
        soft_objectives=soft_objectives,
        performance_budget=performance_budget
        or {
            "max_p95_seconds": 1.0,
            "min_runs": 1,
            "warmup_runs": 0,
            "enforcement": (
                "hard"
                if "performance_p95_pass" in hard_checks
                else "soft"
            ),
        },
        verification_type=verification_type,
    )
    reproduction_input = {
        "question": "그중 가장 많이 올린 증권사는?",
        "requires_prior_scope": True,
        "prior_search_scope": {
            "route": "vectordb",
            "search_filters": {"target_name": "삼성전자"},
            "file_names": ["safe-report.pdf"],
            "answer_scope_index": {
                "sections": [
                    {
                        "id": "section_1",
                        "label": "삼성전자",
                        "filters": {"target_name": "삼성전자"},
                        "file_names": ["safe-report.pdf"],
                    }
                ]
            },
        },
    }
    manual_assertions = (
        [{"id": "answer_grounded", "text": "근거와 답변이 일치한다."}]
        if "manual_assertions_pass" in hard_checks
        else []
    )
    return monitoring.canonicalize_regression_candidate(
        {
            "schema_version": 2,
            "contract_schema_version": 2,
            "id": candidate_id,
            "source_refs": [{"kind": "report", "id": "report_1"}],
            "created_at": "2026-07-29T00:00:00+00:00",
            "updated_at": "2026-07-29T00:00:00+00:00",
            "record_revision": 0,
            "contract_revision": 1,
            "triage_status": triage_status,
            "operator_decision": "accepted",
            "severity": "S2",
            "impact_area": "answer_quality",
            "impact_summary": "이전 기업 범위가 사라진다.",
            "observed": {
                "reproduction_input": reproduction_input,
                "actual": {
                    "route": "rdb",
                    "filters": {},
                    "sources": [],
                    "state": {"followup_scope_intent": "true"},
                },
            },
            "expected": {
                "route": route,
                "filters": {},
                "sources": [],
                "state": {},
                "manual_assertions": manual_assertions,
            },
            "expected_approved_at": "2026-07-29T00:01:00+00:00",
            "expected_approved_by": "local_operator",
            "quality_profile": profile,
            "validation_plan": plan,
            "reproduction_manifest": _manifest(),
            "active_checks": hard_checks,
            "verification_type": verification_type,
            "evidence": {
                "baseline_runs": [],
                "verification_runs": [],
                "manual_reproductions": [],
                "manual_verifications": [],
            },
            "handoffs": [],
        }
    )


def _persist_candidate(path: Path, candidate: dict) -> dict:
    stored = {
        key: value
        for key, value in candidate.items()
        if key not in {"json_path", "integrity_status", "warnings"}
    }
    artifact_io.atomic_write_json(path, stored)
    return monitoring.load_regression_candidate(path)


def _provenance(candidate: dict, *, manifest=None):
    return {
        "backend_mode": "synthetic_test",
        "snapshot_id": "synthetic:1",
        "snapshot_available": True,
        "data_revision": "data-2026-07-29",
        "config_fingerprint": "a" * 64,
        "reproduction_manifest": manifest
        or candidate["reproduction_manifest"],
    }


def _state(route: str):
    return {
        "route": route,
        "search_filters": {"target_name": "삼성전자"},
        "generation": "합성 답변 [1]",
        "rerank_info": [
            {"rank": 1, "file_name": "safe-report.pdf"}
        ],
        "no_vector_results": False,
    }


def test_quality_profiles_keep_correctness_floor_and_reject_unknowns():
    with pytest.raises(
        monitoring.CandidateValidationError,
        match="quality_profile",
    ):
        monitoring.build_validation_plan(
            "unknown",
            hard_checks=["route_pass"],
        )
    with pytest.raises(
        monitoring.CandidateValidationError,
        match="performance_p95_pass",
    ):
        monitoring.build_validation_plan(
            "speed_first",
            hard_checks=["route_pass"],
        )

    plan = monitoring.build_validation_plan(
        "speed_first",
        hard_checks=[
            "route_pass",
            "performance_p95_pass",
        ],
    )
    assert plan["quality_profile"] == "speed_first"
    assert plan["performance_budget"]["enforcement"] == "hard"

    no_correctness = _candidate(
        hard_checks=["latency_pass"],
    )
    with pytest.raises(
        monitoring.CandidateValidationError,
        match="correctness or safety",
    ):
        monitoring.build_candidate_evaluation_case(no_correctness)


def test_unified_report_context_defaults_to_selected_turn_without_full_chat(
    tmp_path,
    monkeypatch,
):
    messages = [
        {"id": "u1", "role": "user", "content": "삼성전자 자료를 보여줘"},
        {
            "id": "a1",
            "role": "assistant",
            "content": "첫 답변",
            "metadata": {"status": "succeeded"},
        },
        {"id": "u2", "role": "user", "content": "그중 가장 많이 올린 곳은?"},
        {
            "id": "a2",
            "role": "assistant",
            "content": "문제가 난 답변",
            "metadata": {
                "status": "succeeded",
                "monitoring": {
                    "query_rewrite": {
                        "followup_scope_intent": True
                    },
                    "state_trace": {
                        "input": {
                            "prior_search_scope": {
                                "route": "vectordb",
                                "search_filters": {
                                    "target_name": "삼성전자"
                                },
                                "answer_scope_index": {
                                    "sections": [
                                        {
                                            "id": "section_1",
                                            "label": "삼성전자",
                                            "filters": {
                                                "target_name": "삼성전자"
                                            },
                                            "file_names": [
                                                "safe-report.pdf"
                                            ],
                                        }
                                    ]
                                },
                            }
                        }
                    },
                },
            },
        },
    ]
    thread = {"id": "thread_1", "name": "합성 대화"}
    context = issue_report_store.build_issue_report_submission_context(
        thread=thread,
        messages=messages,
        report_target_type="response",
        selected_message_id="a2",
        include_conversation=False,
    )
    preview = issue_report_store.build_issue_report_preview(
        context=context,
        include_conversation=False,
    )

    assert "conversation_messages" not in context
    assert preview["report_target_type"] == "response"
    assert preview["selected_message_id"] == "a2"
    assert preview["includes_compact_trace"] is True
    assert preview["includes_prior_search_scope"] is True
    assert preview["includes_full_conversation"] is False

    monkeypatch.setattr(
        issue_report_store,
        "DEBUG_REPORT_DIR",
        tmp_path,
    )
    stored = issue_report_store.create_issue_report(
        thread["id"],
        "검색 정확도 이슈",
        "다른 종목이 섞임",
        context,
        report_target_type="response",
    )
    loaded = issue_report_store.load_report(stored["json_path"])
    assert loaded["report_contract_version"] == 2
    assert loaded["report_target_type"] == "response"
    assert loaded["privacy"]["contains_full_conversation"] is False
    promoted = monitoring.promote_issue_report_to_eval_candidate(
        loaded,
        output_dir=tmp_path / "candidates",
    )
    assert promoted["contract_schema_version"] == 2
    assert promoted["reproduction_manifest"]["complete"] is False
    assert promoted["quality_profile"] in {
        "accuracy_first",
        "balanced",
    }

    ui_context = (
        issue_report_store.build_issue_report_submission_context(
            thread=thread,
            messages=messages,
            report_target_type="ui_or_system",
            include_conversation=False,
        )
    )
    assert ui_context["report_target_type"] == "ui_or_system"
    assert "selected_message" not in ui_context
    ui_stored = issue_report_store.create_issue_report(
        thread["id"],
        "버그/기능",
        "검색 버튼을 누르면 빈 화면이 나타난다.",
        ui_context,
        report_target_type="ui_or_system",
    )
    ui_report = issue_report_store.load_report(
        ui_stored["json_path"]
    )
    ui_candidate = (
        monitoring.promote_issue_report_to_eval_candidate(
            ui_report,
            output_dir=tmp_path / "ui_candidates",
        )
    )
    assert ui_candidate["observed"]["reproduction_input"][
        "scenario"
    ].startswith("검색 버튼")


def test_reproduction_readiness_requires_prior_scope_and_complete_manifest():
    candidate = _candidate()
    incomplete = deepcopy(candidate)
    incomplete["reproduction_manifest"] = _manifest(
        data_revision=None,
        index_revision=None,
    )
    incomplete = monitoring.canonicalize_regression_candidate(
        incomplete
    )
    readiness = monitoring.assess_candidate_reproduction_readiness(
        incomplete
    )
    assert readiness["ready"] is False
    assert "reproduction_manifest" in readiness["missing_fields"]

    missing_scope = deepcopy(candidate)
    missing_scope["observed"]["reproduction_input"].pop(
        "prior_search_scope"
    )
    missing_scope = monitoring.canonicalize_regression_candidate(
        missing_scope
    )
    readiness = monitoring.assess_candidate_reproduction_readiness(
        missing_scope
    )
    assert readiness["requires_prior_scope"] is True
    assert (
        "reproduction_input.prior_search_scope"
        in readiness["missing_fields"]
    )


def test_v2_runner_repeats_and_injects_executable_prior_scope(tmp_path):
    candidate = _candidate(
        performance_budget={
            "max_p95_seconds": 1.0,
            "min_runs": 3,
            "warmup_runs": 1,
            "enforcement": "soft",
        }
    )
    candidate["observed"]["reproduction_input"]["chat_history"] = [
        {"role": "user", "content": "삼성전자 자료를 보여줘"},
        {"role": "assistant", "content": "첫 답변"},
    ]
    candidate = monitoring.canonicalize_regression_candidate(
        candidate
    )
    payloads = []

    def invoke(payload, config=None):
        payloads.append(payload)
        return _state("vectordb")

    run = monitoring.run_candidate_evaluation(
        candidate,
        invoke,
        output_dir=tmp_path,
        run_kind="baseline",
        provenance=_provenance(candidate),
    )

    assert len(payloads) == 4
    assert len(run["results"]) == 3
    assert run["summary"]["sample_count"] == 3
    assert run["summary"]["warmup_runs"] == 1
    assert run["summary"]["status"] == "pass"
    scope = payloads[0]["prior_search_scope"]
    assert scope["search_filters"]["target_name"] == "삼성전자"
    assert scope["answer_scope_index"]["sections"][0]["id"] == "section_1"
    assert payloads[0]["chat_history"] == [
        ["user", "삼성전자 자료를 보여줘"],
        ["assistant", "첫 답변"],
    ]


def test_speed_profile_uses_repeated_p95_as_hard_gate(
    tmp_path,
    monkeypatch,
):
    candidate = _candidate(
        profile="speed_first",
        hard_checks=[
            "route_pass",
            "performance_p95_pass",
        ],
        soft_objectives=["answer_conciseness"],
        performance_budget={
            "max_p95_seconds": 0.15,
            "min_runs": 3,
            "warmup_runs": 0,
            "enforcement": "hard",
        },
    )
    times = iter([0.0, 0.1, 1.0, 1.2, 2.0, 2.4])
    monkeypatch.setattr(
        monitoring.time,
        "perf_counter",
        lambda: next(times),
    )

    run = monitoring.run_candidate_evaluation(
        candidate,
        lambda payload, config=None: _state("vectordb"),
        output_dir=tmp_path,
        run_kind="baseline",
        provenance=_provenance(candidate),
    )

    assert run["summary"]["p95_latency_seconds"] == 0.4
    assert run["summary"]["performance_p95_pass"] is False
    assert run["summary"]["hard_failed_checks"] == [
        "performance_p95_pass"
    ]
    assert monitoring._run_result_status(run) == "fail"


def test_manifest_mismatch_blocks_before_graph_invocation(tmp_path):
    candidate = _candidate()
    actual_manifest = _manifest(data_revision="different-data")
    invoked = False

    def invoke(payload, config=None):
        nonlocal invoked
        invoked = True
        return _state("vectordb")

    run = monitoring.run_candidate_evaluation(
        candidate,
        invoke,
        output_dir=tmp_path,
        run_kind="baseline",
        provenance=_provenance(
            candidate,
            manifest=actual_manifest,
        ),
    )

    assert invoked is False
    assert run["run_status"] == "blocked"
    assert run["blocked_reason"].startswith(
        "reproduction_manifest_mismatch"
    )


def test_mixed_contract_requires_both_automatic_and_manual_evidence(
    tmp_path,
):
    candidate = _candidate(
        candidate_id="candidate_mixed",
        hard_checks=[
            "route_pass",
            "manual_assertions_pass",
        ],
        verification_type="mixed",
    )
    candidate_path = tmp_path / "candidate_mixed.json"
    candidate = _persist_candidate(candidate_path, candidate)

    baseline = monitoring.run_candidate_evaluation(
        candidate,
        lambda payload, config=None: _state("rdb"),
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(candidate),
    )
    candidate = monitoring.record_candidate_run(
        candidate_path,
        run=baseline,
        run_kind="baseline",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    assert (
        monitoring.build_candidate_action_state(candidate)[
            "mark_reproduced"
        ]["enabled"]
        is False
    )

    candidate = monitoring.record_candidate_manual_evidence(
        candidate_path,
        evidence_kind="manual_reproduction",
        checklist_results=[
            {
                "assertion_id": "answer_grounded",
                "passed": False,
                "note": "근거 불일치 재현",
            }
        ],
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
        reason="수동 실패를 확인함",
    )
    assert (
        monitoring.build_candidate_action_state(candidate)[
            "mark_reproduced"
        ]["enabled"]
        is True
    )
    candidate = monitoring.transition_regression_candidate(
        candidate_path,
        to_status="reproduced",
        expected_record_revision=candidate["record_revision"],
        reason="자동·수동 실패 확인",
    )
    candidate = monitoring.transition_regression_candidate(
        candidate_path,
        to_status="fixing",
        expected_record_revision=candidate["record_revision"],
        reason="수정 시작",
    )

    verification = monitoring.run_candidate_evaluation(
        candidate,
        lambda payload, config=None: _state("vectordb"),
        output_dir=tmp_path / "runs",
        run_kind="verification",
        provenance=_provenance(candidate),
    )
    candidate = monitoring.record_candidate_run(
        candidate_path,
        run=verification,
        run_kind="verification",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    assert (
        monitoring.build_candidate_action_state(candidate)[
            "mark_verified"
        ]["enabled"]
        is False
    )

    candidate = monitoring.record_candidate_manual_evidence(
        candidate_path,
        evidence_kind="manual_verification",
        checklist_results=[
            {
                "assertion_id": "answer_grounded",
                "passed": True,
                "note": "근거 일치",
            }
        ],
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
        reason="수동 통과를 확인함",
    )
    assert (
        monitoring.build_candidate_action_state(candidate)[
            "mark_verified"
        ]["enabled"]
        is True
    )
    candidate = monitoring.transition_regression_candidate(
        candidate_path,
        to_status="verified",
        expected_record_revision=candidate["record_revision"],
        reason="자동·수동 검증 통과",
    )
    candidate = monitoring.update_regression_candidate(
        candidate_path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "fixed_in_version": "0.6.1",
            "closure_reason": "이전 범위를 유지하도록 수정함",
            "suite_exclusion_reason": (
                "실제 회귀 데이터셋은 데이터 준비 후 편입"
            ),
        },
        reason="합성 검증 종료 정보 저장",
    )
    candidate = monitoring.transition_regression_candidate(
        candidate_path,
        to_status="closed",
        expected_record_revision=candidate["record_revision"],
        reason="종료 근거 확인",
    )
    assert candidate["triage_status"] == "closed"


def test_speed_first_candidate_closes_with_repeated_p95_evidence(
    tmp_path,
    monkeypatch,
):
    candidate = _candidate(
        candidate_id="candidate_speed_e2e",
        profile="speed_first",
        hard_checks=[
            "route_pass",
            "performance_p95_pass",
        ],
        soft_objectives=["answer_conciseness"],
        performance_budget={
            "max_p95_seconds": 0.15,
            "min_runs": 3,
            "warmup_runs": 0,
            "enforcement": "hard",
        },
    )
    path = tmp_path / "candidate_speed_e2e.json"
    candidate = _persist_candidate(path, candidate)
    baseline_times = iter(
        [0.0, 0.1, 1.0, 1.2, 2.0, 2.4]
    )
    monkeypatch.setattr(
        monitoring.time,
        "perf_counter",
        lambda: next(baseline_times),
    )
    baseline = monitoring.run_candidate_evaluation(
        candidate,
        lambda payload, config=None: _state("vectordb"),
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(candidate),
    )
    candidate = monitoring.record_candidate_run(
        path,
        run=baseline,
        run_kind="baseline",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="reproduced",
        expected_record_revision=candidate["record_revision"],
        reason="반복 p95 예산 초과 재현",
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="fixing",
        expected_record_revision=candidate["record_revision"],
        reason="지연 개선 시작",
    )

    verification_times = iter(
        [0.0, 0.05, 1.0, 1.06, 2.0, 2.07]
    )
    monkeypatch.setattr(
        monitoring.time,
        "perf_counter",
        lambda: next(verification_times),
    )
    verification = monitoring.run_candidate_evaluation(
        candidate,
        lambda payload, config=None: _state("vectordb"),
        output_dir=tmp_path / "runs",
        run_kind="verification",
        provenance=_provenance(candidate),
    )
    assert verification["summary"]["p95_latency_seconds"] == 0.07
    candidate = monitoring.record_candidate_run(
        path,
        run=verification,
        run_kind="verification",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="verified",
        expected_record_revision=candidate["record_revision"],
        reason="동일 조건 반복 p95 통과",
    )
    candidate = monitoring.update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "fixed_in_version": "0.6.1",
            "closure_reason": "반복 p95 예산을 충족함",
            "suite_exclusion_reason": (
                "실제 성능 데이터셋은 데이터 준비 후 편입"
            ),
        },
        reason="속도 합성 검증 종료 정보 저장",
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="closed",
        expected_record_revision=candidate["record_revision"],
        reason="종료 근거 확인",
    )
    assert candidate["triage_status"] == "closed"


def test_response_free_ui_candidate_uses_manual_scenario_and_closes(
    tmp_path,
):
    candidate = _candidate(
        candidate_id="candidate_ui_e2e",
        hard_checks=["manual_assertions_pass"],
        verification_type="manual_ui",
    )
    reproduction_input = candidate["observed"]["reproduction_input"]
    reproduction_input.clear()
    reproduction_input.update(
        {
            "scenario": "검색 버튼을 누르면 빈 화면이 나타난다.",
            "report_target_type": "ui_or_system",
        }
    )
    candidate = monitoring.canonicalize_regression_candidate(
        candidate
    )
    assert monitoring.assess_candidate_reproduction_readiness(
        candidate
    )["ready"] is True
    path = tmp_path / "candidate_ui_e2e.json"
    candidate = _persist_candidate(path, candidate)

    candidate = monitoring.record_candidate_manual_evidence(
        path,
        evidence_kind="manual_reproduction",
        checklist_results=[
            {
                "assertion_id": "answer_grounded",
                "passed": False,
                "note": "빈 화면 재현",
            }
        ],
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
        reason="UI 오류 재현",
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="reproduced",
        expected_record_revision=candidate["record_revision"],
        reason="수동 UI 실패 확인",
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="fixing",
        expected_record_revision=candidate["record_revision"],
        reason="UI 수정 시작",
    )
    candidate = monitoring.record_candidate_manual_evidence(
        path,
        evidence_kind="manual_verification",
        checklist_results=[
            {
                "assertion_id": "answer_grounded",
                "passed": True,
                "note": "정상 화면 확인",
            }
        ],
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
        reason="UI 수정 확인",
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="verified",
        expected_record_revision=candidate["record_revision"],
        reason="수동 UI 검증 통과",
    )
    candidate = monitoring.update_regression_candidate(
        path,
        expected_record_revision=candidate["record_revision"],
        changes={
            "fixed_in_version": "0.6.1",
            "closure_reason": "빈 화면 오류 수정",
            "suite_exclusion_reason": (
                "UI 시나리오는 승인된 수동 회귀로 유지"
            ),
        },
        reason="UI 합성 검증 종료 정보 저장",
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="closed",
        expected_record_revision=candidate["record_revision"],
        reason="종료 근거 확인",
    )
    assert candidate["triage_status"] == "closed"


def test_v2_handoff_contains_quality_manifest_and_safe_scope(tmp_path):
    candidate = _candidate(
        candidate_id="candidate_handoff_v2",
        hard_checks=[
            "route_pass",
            "manual_assertions_pass",
        ],
        verification_type="mixed",
    )
    path = tmp_path / "candidate_handoff_v2.json"
    candidate = _persist_candidate(path, candidate)
    baseline = monitoring.run_candidate_evaluation(
        candidate,
        lambda payload, config=None: _state("rdb"),
        output_dir=tmp_path / "runs",
        run_kind="baseline",
        provenance=_provenance(candidate),
    )
    candidate = monitoring.record_candidate_run(
        path,
        run=baseline,
        run_kind="baseline",
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    candidate = monitoring.record_candidate_manual_evidence(
        path,
        evidence_kind="manual_reproduction",
        checklist_results=[
            {
                "assertion_id": "answer_grounded",
                "passed": False,
                "note": "",
            }
        ],
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
        reason="수동 실패 확인",
    )
    candidate = monitoring.transition_regression_candidate(
        path,
        to_status="reproduced",
        expected_record_revision=candidate["record_revision"],
        reason="재현 완료",
    )

    payload = build_codex_handoff_payload(candidate, baseline)
    validate_codex_handoff_payload(payload)

    assert payload["handoff_schema_version"] == 2
    assert payload["quality"]["profile"] == "balanced"
    assert payload["reproduction_manifest"]["complete"] is True
    assert payload["reproduction"]["requires_prior_scope"] is True
    assert (
        payload["reproduction"]["prior_search_scope"][
            "search_filters"
        ]["target_name"]
        == "삼성전자"
    )
    written = write_codex_handoff(
        candidate,
        baseline,
        output_dir=tmp_path / "handoffs",
        approved_by="local_operator",
        approval_reason="프로필과 익명화 결과를 확인함",
    )
    candidate = monitoring.record_candidate_handoff(
        path,
        handoff=written,
        expected_record_revision=candidate["record_revision"],
        expected_contract_revision=candidate["contract_revision"],
        expected_candidate_hash=candidate["candidate_hash"],
    )
    assert candidate["handoffs"][0]["handoff_id"] == written["handoff_id"]


def test_low_sample_incidents_are_improvement_eligible_not_reference_only():
    metric = monitoring.summarize_incident_metric(
        incident_count=2,
        sample_count=5,
    )
    assert metric["low_sample"] is True
    assert metric["improvement_eligible"] is True
    assert metric["automatic_decision_allowed"] is False
    assert "reference" not in metric["policy"]
