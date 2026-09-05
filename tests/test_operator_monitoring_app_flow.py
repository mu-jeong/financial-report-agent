from __future__ import annotations

import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from src.core.monitoring_admin_client import OperatorSession


def _write_harness(path: Path, artifact_root: Path) -> None:
    path.write_text(
        f"""
from pathlib import Path
import apps.gui.operator_monitoring_views as views
from src.core.monitoring_admin_client import OperatorApiConfig

CONFIG = OperatorApiConfig(
    project_url="https://example.supabase.co",
    publishable_key="sb_publishable_fixture",
    function_url="https://example.supabase.co/functions/v1/issue-report-operator",
    artifact_root=Path({str(artifact_root)!r}),
)
ISSUE = {{
    "issue_id": "11111111-1111-4111-8111-111111111111",
    "receipt_id": "22222222-2222-4222-8222-222222222222",
    "state": "OPEN",
    "record_revision": 1,
    "app_version": "0.6.1",
    "reported_release_id": "release-v0.6.1",
    "category": "answer_quality",
    "route": "vectordb",
    "status": "success",
    "latency_ms": 1234,
    "case_diagnostics_status": "AVAILABLE",
    "raw_available": True,
}}

class FakeClient:
    def list_issues(self, *, state=None, limit=50):
        return [dict(ISSUE)] if state in (None, "OPEN") else []

    def get_issue(self, issue_id):
        assert issue_id == ISSUE["issue_id"]
        return dict(ISSUE)

    def view_raw(self, issue_id):
        assert issue_id == ISSUE["issue_id"]
        views.st.session_state["fake_raw_view_calls"] = (
            views.st.session_state.get("fake_raw_view_calls", 0) + 1
        )
        return {{
            "comment": "답변 수치가 다릅니다.",
            "observed": {{
                "selected_question": "영업이익은 얼마인가요?",
                "selected_answer": "8입니다.",
            }},
            "case_diagnostics": {{
                "schema_version": 1,
                "truncated": False,
                "route_observations": [{{
                    "selected_route": "vectordb",
                    "filters": {{"target_name": "삼성전자"}},
                }}],
                "retrieval_observations": [{{
                    "role": "OBSERVED_RESULT",
                    "source_uid": "a" * 64,
                    "source_sha256": "d" * 64,
                    "rank": 1,
                }}],
            }},
        }}

    def transition_issue(self, *args, **kwargs):
        views.st.session_state["fake_transition_args"] = args
        views.st.session_state["fake_transition_kwargs"] = kwargs
        updated = dict(ISSUE)
        updated["state"] = kwargs["target_state"]
        updated["record_revision"] = ISSUE["record_revision"] + 1
        return updated

    def list_control_records(self, issue_id):
        assert issue_id == ISSUE["issue_id"]
        return []

    def reconcile_control_projection(self, issue_id, expected):
        assert issue_id == ISSUE["issue_id"]
        return {{"missing": [], "unexpected": [], "mismatched": []}}

    def check_control_projection(self, issue_id, expected):
        assert issue_id == ISSUE["issue_id"]
        return {{"missing": [], "unexpected": [], "mismatched": []}}

views.operator_surface_enabled = lambda: True
views.load_operator_api_config = lambda: CONFIG
views._client = lambda session: FakeClient()
_real_service = views._service
_documents = (
    views.fixed_snapshot.ActiveReportDocument(
        report_uid="a" * 64,
        canonical_relative_path="company/samsung-observed.pdf",
        report_type="company",
        report_date="2026-08-28",
        target_name="삼성전자",
        title="삼성전자 신고 당시 보고서",
        broker="미래에셋",
    ),
    views.fixed_snapshot.ActiveReportDocument(
        report_uid="b" * 64,
        canonical_relative_path="company/samsung-peer.pdf",
        report_type="company",
        report_date="2026-08-27",
        target_name="삼성전자",
        title="삼성전자 같은 조건 보고서",
        broker="하나증권",
    ),
    views.fixed_snapshot.ActiveReportDocument(
        report_uid="c" * 64,
        canonical_relative_path="industry/semiconductor.pdf",
        report_type="industry",
        report_date="2026-08-26",
        target_name="반도체",
        title="반도체 운영자 추가 보고서",
        broker="한국투자",
    ),
)

class FakeScopeService:
    def __init__(self, registry):
        self.delegate = _real_service(registry)

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def propose_snapshot_scope(self, reproduction_seed, *, data_root):
        return views.fixed_snapshot.SnapshotScopeProposal(
            report_uids=("a" * 64, "b" * 64),
            observed_report_uids=("a" * 64,),
            filter_matched_report_uids=("a" * 64, "b" * 64),
            unsupported_filters=(),
        )

    def list_snapshot_documents(self, *, data_root):
        views.st.session_state["fake_snapshot_document_reads"] = (
            views.st.session_state.get("fake_snapshot_document_reads", 0) + 1
        )
        return _documents

    def create_fixed_snapshot_for_case(self, *, data_root, report_uids):
        views.st.session_state["fake_snapshot_create_calls"] = (
            views.st.session_state.get("fake_snapshot_create_calls", 0) + 1
        )
        snapshot = views.fixed_snapshot.FixedSnapshot(
            revision_id="d" * 64,
            path=CONFIG.artifact_root / "fixed-snapshots" / ("d" * 64),
            manifest={{}},
        )
        return snapshot, {{"fixed_snapshot_revision_id": snapshot.revision_id}}

    def mark_case_ready(self, case_revision_id):
        return self.delegate.registry.mark_case_ready(
            case_revision_id, snapshot_available=True
        )

views._service = lambda registry: FakeScopeService(registry)
_real_snapshot_source_cache_key = views._snapshot_source_cache_key
views._snapshot_source_cache_key = lambda data_root: (
    "fake-active-publication",
    str(CONFIG.artifact_root),
    views.st.session_state.get("fake_publication_generation", 1),
)
# Streamlit's installed AppTest adapter predates segmented_control support.
# Keep the production render path and substitute only that unsupported widget.
views.st.segmented_control = lambda label, options, default=None, key=None, **kwargs: (
    views.st.session_state.get(key, default) if key is not None else default
)
if (
    views.st.session_state.get("fake_seed_operator_defined_case")
    and not views.st.session_state.get("fake_operator_defined_case_id")
):
    registry = views._registry()
    issue = registry.create_issue(
        source_receipt_id="supabase:" + ISSUE["issue_id"],
        reported_release_id="release-v0.6.1",
        summary={{"question": "영업이익은 얼마인가요?"}},
    )
    fixture = registry.create_fixture_revision(
        issue_id=issue["issue_id"],
        question="영업이익은 얼마인가요?",
        reported_symptom="수치가 다릅니다.",
        expected_behavior="선택한 고정 자료로 재현합니다.",
        typed_checks=[{{"type": "ANSWER_CONTAINS", "value": "영업이익"}}],
        manual_checks=["문서 범위 확인"],
    )
    fixture = registry.mark_fixture_ready(fixture["fixture_revision_id"])
    snapshot = registry.register_fixed_snapshot(
        fixed_snapshot_revision_id="d" * 64,
        bundle_relpath="fixed-snapshots/" + ("d" * 64),
        bundle_digest="d" * 64,
        manifest={{"manifest_schema_version": 2, "report_uids": ["a" * 64]}},
        reader_contract={{"manifest_schema_version": 2}},
    )
    case = registry.create_case_revision(
        issue_id=issue["issue_id"],
        fixture_revision_id=fixture["fixture_revision_id"],
        fixed_snapshot_revision_id=snapshot["fixed_snapshot_revision_id"],
        fixed_clock=None,
        evaluator={{"method": "operator-review"}},
        reconstruction_lineage={{
            "schema_version": 1,
            "basis": "OPERATOR_DEFINED",
            "operator_scope_confirmed": False,
            "operator_scope_reason": "",
            "exceptions": [],
            "evidence_qualifier": "PARTIAL",
        }},
    )
    views.st.session_state["fake_operator_defined_case_id"] = case[
        "case_revision_id"
    ]
try:
    views.render_operator_monitoring_page()
finally:
    views._snapshot_source_cache_key = _real_snapshot_source_cache_key
""",
        encoding="utf-8",
    )


def test_operator_sees_raw_automatically_and_audit_read_is_cached(tmp_path: Path) -> None:
    harness = tmp_path / "operator_harness.py"
    _write_harness(harness, tmp_path / "managed")
    app = AppTest.from_file(str(harness))
    app.session_state["monitoring_operator_session"] = OperatorSession(
        access_token="memory-only-fixture-token",
        user_id="33333333-3333-4333-8333-333333333333",
        email="admin@example.test",
        expires_at=time.time() + 3600,
    )

    app.run(timeout=20)

    assert not app.exception
    assert any(title.value == "운영 Monitoring" for title in app.title)
    assert any(header.value == "작업함" for header in app.header)
    assert any(subheader.value == "신고 요약" for subheader in app.subheader)
    assert any(subheader.value == "신고 원문" for subheader in app.subheader)
    issue_metrics = {metric.label: metric.value for metric in app.metric}
    assert issue_metrics["미확인"] == "1"
    assert issue_metrics["조치 중"] == "0"
    assert issue_metrics["해결됨"] == "0"
    assert issue_metrics["이슈 아님"] == "0"
    next_state = next(
        selectbox for selectbox in app.selectbox if selectbox.label == "다음 상태"
    )
    assert next_state.value == "조치 시작"
    assert not any(
        button.label == "원문 열람 및 감사기록 남기기"
        for button in app.button
    )
    seed = app.session_state[
        "monitoring_reproduction_seed_11111111-1111-4111-8111-111111111111"
    ]
    assert seed["question"] == "영업이익은 얼마인가요?"
    assert seed["observed_answer"] == "8입니다."
    assert app.session_state["fake_raw_view_calls"] == 1

    app.run(timeout=20)

    assert not app.exception
    assert app.session_state["fake_raw_view_calls"] == 1


def test_operator_can_move_an_unreviewed_issue_into_progress(tmp_path: Path) -> None:
    harness = tmp_path / "operator_transition_harness.py"
    _write_harness(harness, tmp_path / "managed-transition")
    app = AppTest.from_file(str(harness))
    app.session_state["monitoring_operator_session"] = OperatorSession(
        access_token="memory-only-fixture-token",
        user_id="33333333-3333-4333-8333-333333333333",
        email="admin@example.test",
        expires_at=time.time() + 3600,
    )

    app.run(timeout=20)
    next(
        text_area for text_area in app.text_area if text_area.label == "상태 변경 사유"
    ).set_value("신고 내용을 확인하기 시작했습니다.")
    next(
        button for button in app.button if button.label == "상태 변경 저장"
    ).click().run(timeout=20)

    assert not app.exception
    assert app.session_state["fake_transition_args"] == (
        "11111111-1111-4111-8111-111111111111",
    )
    assert app.session_state["fake_transition_kwargs"] == {
        "target_state": "IN_PROGRESS",
        "expected_record_revision": 1,
        "reason": "신고 내용을 확인하기 시작했습니다.",
    }


def test_reproduction_workspace_keeps_selected_issue_across_reruns(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "operator_reproduction_rerun.py"
    _write_harness(harness, tmp_path / "managed-reproduction-rerun")
    app = AppTest.from_file(str(harness))
    app.session_state["monitoring_operator_session"] = OperatorSession(
        access_token="memory-only-fixture-token",
        user_id="33333333-3333-4333-8333-333333333333",
        email="admin@example.test",
        expires_at=time.time() + 3600,
    )

    app.run(timeout=20)
    assert not app.exception
    assert app.session_state["monitoring_selected_issue_id"] == (
        "11111111-1111-4111-8111-111111111111"
    )

    app.session_state["monitoring_operator_workspace"] = "테스트 케이스 설정"
    app.run(timeout=20)
    assert not app.exception
    assert any(
        item.value == "1. Fixture — 같은 질문과 확인 기준 고정"
        for item in app.subheader
    )

    # A button click causes another Streamlit rerun while the inbox selectbox is
    # absent. The durable issue selection must survive that widget cleanup.
    app.run(timeout=20)

    assert not app.exception
    assert app.session_state["monitoring_selected_issue_id"] == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert any(
        item.value == "1. Fixture — 같은 질문과 확인 기준 고정"
        for item in app.subheader
    )
    assert not any(
        item.value == "작업함에서 먼저 신고를 선택하세요."
        for item in app.info
    )

    # Sessions already affected before the fix still retain the fetched issue
    # detail, so the reproduction page can restore the durable selection once.
    del app.session_state["monitoring_selected_issue_id"]
    app.run(timeout=20)

    assert not app.exception
    assert app.session_state["monitoring_selected_issue_id"] == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert any(
        item.value == "1. Fixture — 같은 질문과 확인 기준 고정"
        for item in app.subheader
    )


def test_snapshot_scope_is_human_readable_editable_and_stable_across_reruns(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "operator_snapshot_scope.py"
    _write_harness(harness, tmp_path / "managed-snapshot-scope")
    app = AppTest.from_file(str(harness))
    remote_issue_id = "11111111-1111-4111-8111-111111111111"
    app.session_state["monitoring_operator_session"] = OperatorSession(
        access_token="memory-only-fixture-token",
        user_id="33333333-3333-4333-8333-333333333333",
        email="admin@example.test",
        expires_at=time.time() + 3600,
    )
    app.session_state["monitoring_operator_workspace"] = "테스트 케이스 설정"
    app.session_state["monitoring_selected_issue_id"] = remote_issue_id
    app.session_state["monitoring_selected_issue_detail"] = {
        "issue_id": remote_issue_id
    }

    app.run(timeout=20)

    assert not app.exception
    scope = next(
        dataframe.value
        for dataframe in app.dataframe
        if "포함 근거" in dataframe.value.columns
    )
    assert scope["제목"].tolist() == [
        "삼성전자 신고 당시 보고서",
        "삼성전자 같은 조건 보고서",
    ]
    assert scope["포함 근거"].tolist() == [
        "신고 당시 사용",
        "같은 조건 제안",
    ]
    remove_widget = next(
        widget
        for widget in app.multiselect
        if widget.label == "현재 범위에서 제외할 문서"
    )
    assert any("삼성전자 같은 조건 보고서" in option for option in remove_widget.options)
    assert not any("삼성전자 신고 당시 보고서" in option for option in remove_widget.options)

    add_widget = next(
        widget
        for widget in app.multiselect
        if widget.label == "검색 결과에서 추가할 문서"
    )
    add_widget.set_value(["c" * 64])
    next(
        button for button in app.button if button.label == "선택한 문서 추가"
    ).click().run(timeout=20)

    assert not app.exception
    scope = next(
        dataframe.value
        for dataframe in app.dataframe
        if "포함 근거" in dataframe.value.columns
    )
    assert scope["제목"].tolist() == [
        "삼성전자 신고 당시 보고서",
        "삼성전자 같은 조건 보고서",
        "반도체 운영자 추가 보고서",
    ]
    assert scope["포함 근거"].tolist()[-1] == "운영자 추가"
    assert app.session_state["fake_snapshot_document_reads"] == 1
    assert any(
        "문서 1건을 현재 Snapshot 범위에 추가했습니다" in message.value
        for message in app.success
    )
    snapshot_expander = next(
        expander
        for expander in app.expander
        if expander.label == "신고 근거로 Snapshot 범위 준비"
    )
    assert snapshot_expander.proto.expanded is True
    assert any(
        button.label == "임시 생성·검증 후 FixedSnapshot READY 등록"
        for button in app.button
    )

    remove_widget = next(
        widget
        for widget in app.multiselect
        if widget.label == "현재 범위에서 제외할 문서"
    )
    remove_widget.set_value(["b" * 64])
    next(
        button for button in app.button if button.label == "선택한 문서 제외"
    ).click().run(timeout=20)
    app.run(timeout=20)

    assert not app.exception
    scope = next(
        dataframe.value
        for dataframe in app.dataframe
        if "포함 근거" in dataframe.value.columns
    )
    assert scope["제목"].tolist() == [
        "삼성전자 신고 당시 보고서",
        "반도체 운영자 추가 보고서",
    ]
    assert app.session_state["fake_snapshot_document_reads"] == 1

    app.session_state["fake_publication_generation"] = 2
    app.run(timeout=20)

    assert not app.exception
    assert app.session_state["fake_snapshot_document_reads"] == 2


def test_created_snapshot_revision_id_remains_visible_after_button_rerun(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "operator_snapshot_created_id.py"
    _write_harness(harness, tmp_path / "managed-snapshot-created-id")
    app = AppTest.from_file(str(harness))
    remote_issue_id = "11111111-1111-4111-8111-111111111111"
    app.session_state["monitoring_operator_session"] = OperatorSession(
        access_token="memory-only-fixture-token",
        user_id="33333333-3333-4333-8333-333333333333",
        email="admin@example.test",
        expires_at=time.time() + 3600,
    )
    app.session_state["monitoring_operator_workspace"] = "테스트 케이스 설정"
    app.session_state["monitoring_selected_issue_id"] = remote_issue_id
    app.session_state["monitoring_selected_issue_detail"] = {
        "issue_id": remote_issue_id
    }

    app.run(timeout=20)
    next(
        button
        for button in app.button
        if button.label == "임시 생성·검증 후 FixedSnapshot READY 등록"
    ).click().run(timeout=20)

    assert not app.exception
    assert app.session_state["fake_snapshot_create_calls"] == 1
    revision_input = next(
        widget
        for widget in app.text_input
        if widget.label == "FixedSnapshot revision ID"
    )
    assert revision_input.value == "d" * 64
    assert any(item.value == "d" * 64 for item in app.code)

    app.run(timeout=20)

    assert not app.exception
    revision_input = next(
        widget
        for widget in app.text_input
        if widget.label == "FixedSnapshot revision ID"
    )
    assert revision_input.value == "d" * 64
    assert any(item.value == "d" * 64 for item in app.code)


def test_operator_defined_draft_requires_visible_confirmation_before_ready(
    tmp_path: Path,
) -> None:
    harness = tmp_path / "operator_defined_case_ready.py"
    _write_harness(harness, tmp_path / "managed-operator-defined-ready")
    app = AppTest.from_file(str(harness))
    remote_issue_id = "11111111-1111-4111-8111-111111111111"
    app.session_state["monitoring_operator_session"] = OperatorSession(
        access_token="memory-only-fixture-token",
        user_id="33333333-3333-4333-8333-333333333333",
        email="admin@example.test",
        expires_at=time.time() + 3600,
    )
    app.session_state["monitoring_operator_workspace"] = "테스트 케이스 설정"
    app.session_state["monitoring_selected_issue_id"] = remote_issue_id
    app.session_state["monitoring_selected_issue_detail"] = {
        "issue_id": remote_issue_id
    }
    app.session_state["fake_seed_operator_defined_case"] = True

    app.run(timeout=20)

    assert not app.exception
    confirmation = next(
        widget
        for widget in app.checkbox
        if widget.label == "선택한 문서 범위를 직접 확인했습니다"
    )
    reason = next(
        widget
        for widget in app.text_input
        if widget.label == "직접 선택 범위 확인 사유"
    )
    ready_button = next(
        button for button in app.button if button.label == "Case READY로 고정"
    )
    assert confirmation.value is False
    assert reason.value
    assert ready_button.disabled is True

    confirmation.set_value(True)
    app.run(timeout=20)

    ready_button = next(
        button for button in app.button if button.label == "Case READY로 고정"
    )
    assert ready_button.disabled is False
    ready_button.click().run(timeout=20)

    assert not app.exception
    assert any(item.value == "상태: READY" for item in app.caption)


def _write_completed_cycle_harness(
    path: Path, artifact_root: Path, *, workspace: str
) -> None:
    path.write_text(
        f'''
from pathlib import Path
import apps.gui.operator_monitoring_views as views
from src.core.monitoring_admin_client import OperatorApiConfig

CONFIG = OperatorApiConfig(
    project_url="https://example.supabase.co",
    publishable_key="sb_publishable_fixture",
    function_url="https://example.supabase.co/functions/v1/issue-report-operator",
    artifact_root=Path({str(artifact_root)!r}),
)
ISSUE = {{
    "issue_id": "11111111-1111-4111-8111-111111111111",
    "receipt_id": "22222222-2222-4222-8222-222222222222",
    "state": "OPEN",
    "record_revision": 3,
    "app_version": "0.6.1",
    "reported_release_id": "release-v0.6.1",
    "category": "answer_quality",
    "route": "vectordb",
    "status": "success",
    "latency_ms": 1234,
    "case_diagnostics_status": "AVAILABLE",
    "raw_available": True,
}}

class FakeClient:
    def list_issues(self, *, state=None, limit=50):
        return [dict(ISSUE)]
    def get_issue(self, issue_id):
        return dict(ISSUE)
    def check_control_projection(self, issue_id, expected):
        return {{"missing": [], "unexpected": [], "mismatched": []}}
    def reconcile_control_projection(self, issue_id, expected):
        return {{"missing": [], "unexpected": [], "mismatched": []}}
    def list_control_records(self, issue_id):
        return []
    def view_raw(self, issue_id):
        return {{"comment": "fixture", "observed": {{}}, "case_diagnostics": {{}}}}

views.operator_surface_enabled = lambda: True
views.load_operator_api_config = lambda: CONFIG
views._client = lambda session: FakeClient()
views.st.segmented_control = lambda label, options, default=None, **kwargs: (
    {workspace!r} if label == "운영 작업공간" else default
)

registry = views._registry()
issue = registry.create_issue(
    source_receipt_id="supabase:" + ISSUE["issue_id"],
    reported_release_id="release-v0.6.1",
    summary={{"question": "영업이익은 얼마인가요?", "app_version": "0.6.1"}},
)
fixture = registry.create_fixture_revision(
    issue_id=issue["issue_id"],
    question="영업이익은 얼마인가요?",
    reported_symptom="수치가 다릅니다.",
    expected_behavior="고정 자료의 10을 답합니다.",
    typed_checks=[{{"type": "ANSWER_CONTAINS", "value": "10"}}],
    manual_checks=["회계기간 확인"],
)
fixture = registry.mark_fixture_ready(fixture["fixture_revision_id"])
snapshot = registry.register_fixed_snapshot(
    fixed_snapshot_revision_id="snapshot-ui-001",
    bundle_relpath="fixed-snapshots/snapshot-ui-001",
    bundle_digest="b" * 64,
    manifest={{"manifest_schema_version": 2}},
    reader_contract={{"manifest_schema_version": 2}},
)
case = registry.create_case_revision(
    issue_id=issue["issue_id"],
    fixture_revision_id=fixture["fixture_revision_id"],
    fixed_snapshot_revision_id=snapshot["fixed_snapshot_revision_id"],
    fixed_clock="2026-08-29T00:00:00Z",
    evaluator={{"method": "typed-plus-manual"}},
    reconstruction_lineage={{"basis": "REPORT_DIAGNOSTICS", "exceptions": [], "evidence_qualifier": "EXACT"}},
)
case = registry.mark_case_ready(case["case_revision_id"], snapshot_available=True)

def register_release(release_id, version, digest):
    return registry.register_release_manifest(
        release_manifest_id=release_id,
        release_tag="v" + version,
        app_version=version,
        manifest_version=1,
        runtime_bundle_digest=digest,
        bundle_relpath="releases/" + release_id,
        manifest={{"runner_contract_version": 1, "snapshot_reader_contract_version": 2}},
    )

baseline_release = register_release("release-baseline-ui", "0.6.1", "c" * 64)
candidate_release = register_release("release-candidate-ui", "0.6.2", "d" * 64)

def terminal_run(release_id, side, answer, latency, reproduced, passed):
    run = registry.queue_run(
        issue_id=issue["issue_id"],
        case_contract_id=case["case_contract_id"],
        release_manifest_id=release_id,
        side=side,
    )
    registry.start_run(run["run_id"])
    return registry.finish_run(
        run["run_id"], execution_status="SUCCEEDED", validity="VALID",
        artifact={{
            "raw_answer": answer,
            "evidence_refs": [{{"role": "CITED", "source_uid": "a" * 64, "source_sha256": "b" * 64}}],
            "route_summary": {{"route": "vectordb"}},
            "check_result": {{"reproduced": reproduced, "passed": passed}},
            "runtime_profile": {{"generation_model": "fixture-" + side.lower()}},
            "latency_ms": latency,
            "evidence_qualifier": "EXACT",
        }},
    )

baseline = terminal_run(baseline_release["release_manifest_id"], "BASELINE", "영업이익은 8입니다.", 180.0, True, False)
candidate = terminal_run(candidate_release["release_manifest_id"], "CANDIDATE", "영업이익은 10입니다.", 120.0, False, True)
registry.create_comparison(
    issue_id=issue["issue_id"],
    baseline_run_ids=[baseline["run_id"]],
    candidate_run_ids=[candidate["run_id"]],
    verdict="IMPROVED",
    note="답변과 근거를 정성 검토했습니다.",
    actor_user_id="admin-ui",
)
views.st.session_state["monitoring_selected_issue_id"] = ISSUE["issue_id"]
views.st.session_state["monitoring_selected_issue_detail"] = dict(ISSUE)
views.render_operator_monitoring_page()
''',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("workspace", "expected_subheaders"),
    [
        (
            "테스트 케이스 설정",
            (
                "1. Fixture — 같은 질문과 확인 기준 고정",
                "2. FixedSnapshot과 ReconstructionLineage",
                "3. 신고 버전 Baseline 실행",
            ),
        ),
        ("개선 확인", ("Baseline Run", "Candidate Run")),
    ],
)
def test_operator_completed_cycle_workspaces_render_without_error(
    tmp_path: Path, workspace: str, expected_subheaders: tuple[str, ...]
) -> None:
    harness = tmp_path / f"operator_{workspace}.py"
    _write_completed_cycle_harness(
        harness,
        tmp_path / f"managed-{workspace}",
        workspace=workspace,
    )
    app = AppTest.from_file(str(harness))
    app.session_state["monitoring_operator_session"] = OperatorSession(
        access_token="memory-only-fixture-token",
        user_id="33333333-3333-4333-8333-333333333333",
        email="admin@example.test",
        expires_at=time.time() + 3600,
    )

    app.run(timeout=30)

    assert not app.exception
    assert any(header.value == workspace for header in app.header)
    rendered_subheaders = {item.value for item in app.subheader}
    assert set(expected_subheaders).issubset(rendered_subheaders)
