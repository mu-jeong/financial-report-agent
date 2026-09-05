from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

import apps.gui.operator_monitoring_views as monitoring_views
from src.core.fixed_snapshot import ActiveReportDocument
from src.core.monitoring_admin_client import OperatorApiError
from src.core.operator_monitoring_service import (
    MonitoringServiceError,
    snapshot_observed_report_uids,
)


VIEW_PATH = Path("apps/gui/operator_monitoring_views.py")
APP_PATH = Path("apps/gui/app.py")
SIDEBAR_PATH = Path("apps/gui/sidebar_views.py")


def test_operator_api_version_drift_has_an_actionable_message() -> None:
    assert monitoring_views._error_message(
        OperatorApiError("method_not_allowed", status_code=405)
    ) == (
        "운영자 API 배포 버전이 현재 앱보다 오래되었습니다. "
        "Supabase migration과 issue-report-operator Function 배포 상태를 확인하세요."
    )


def _snapshot_document(
    uid: str,
    *,
    title: str,
    target: str,
    broker: str,
    report_type: str = "company",
    report_date: str = "2026-08-29",
) -> ActiveReportDocument:
    return ActiveReportDocument(
        report_uid=uid,
        canonical_relative_path=f"reports/{uid}.pdf",
        report_type=report_type,
        report_date=report_date,
        target_name=target,
        title=title,
        broker=broker,
    )


def test_snapshot_search_uses_human_metadata_and_bounds_results() -> None:
    documents = (
        _snapshot_document(
            "a",
            title="반도체 실적 점검",
            target="삼성전자",
            broker="미래에셋",
        ),
        _snapshot_document(
            "b",
            title="반도체 전망",
            target="SK하이닉스",
            broker="미래에셋",
        ),
        _snapshot_document(
            "c",
            title="은행 산업 전망",
            target="은행",
            broker="하나증권",
            report_type="industry",
        ),
    )

    matched, total = monitoring_views._search_snapshot_documents(
        documents,
        query="삼성전자 미래에셋",
    )
    bounded, bounded_total = monitoring_views._search_snapshot_documents(
        documents,
        broker="미래에셋",
        limit=1,
    )

    assert [document.report_uid for document in matched] == ["a"]
    assert total == 1
    assert [document.report_uid for document in bounded] == ["a"]
    assert bounded_total == 2
    assert "반도체 실적 점검" in monitoring_views._snapshot_document_label(
        documents[0]
    )
    assert "삼성전자" in monitoring_views._snapshot_document_label(
        documents[0]
    )
    assert monitoring_views._snapshot_filter_summary(
        {
            "target_names": ["삼성전자", "SK하이닉스"],
            "report_date_start": "2026-08-01",
        }
    ) == "대상: 삼성전자, SK하이닉스 · 시작일: 2026-08-01"


def test_snapshot_search_filters_by_report_date_range() -> None:
    documents = (
        _snapshot_document(
            "a",
            title="초기 보고서",
            target="삼성전자",
            broker="미래에셋",
            report_date="2026-01-15",
        ),
        _snapshot_document(
            "b",
            title="중기 보고서",
            target="삼성전자",
            broker="미래에셋",
            report_date="2026-06-30",
        ),
        _snapshot_document(
            "c",
            title="최신 보고서",
            target="삼성전자",
            broker="미래에셋",
            report_date="2026-08-29",
        ),
        _snapshot_document(
            "d",
            title="날짜 없는 보고서",
            target="삼성전자",
            broker="미래에셋",
            report_date="",
        ),
    )

    matched, total = monitoring_views._search_snapshot_documents(
        documents,
        report_date_start="2026-06-01",
        report_date_end="2026-08-31",
    )
    assert [document.report_uid for document in matched] == ["b", "c"]
    assert total == 2

    open_started, open_started_total = monitoring_views._search_snapshot_documents(
        documents,
        report_date_start="2026-06-01",
    )
    assert [document.report_uid for document in open_started] == ["b", "c"]
    assert open_started_total == 2

    open_ended, open_ended_total = monitoring_views._search_snapshot_documents(
        documents,
        report_date_end="2026-01-31",
    )
    assert [document.report_uid for document in open_ended] == ["a"]
    assert open_ended_total == 1


def test_snapshot_date_filter_normalizes_widget_values() -> None:
    assert monitoring_views._snapshot_date_filter(()) == (None, None)
    assert monitoring_views._snapshot_date_filter(None) == (None, None)
    assert monitoring_views._snapshot_date_filter(
        (date(2026, 6, 1), date(2026, 8, 31))
    ) == ("2026-06-01", "2026-08-31")
    assert monitoring_views._snapshot_date_filter(
        (date(2026, 8, 29),)
    ) == ("2026-08-29", "2026-08-29")
    assert monitoring_views._snapshot_date_filter(
        date(2026, 8, 29)
    ) == ("2026-08-29", "2026-08-29")


def test_snapshot_revision_for_scope_recovers_latest_exact_match() -> None:
    snapshots = [
        {
            "fixed_snapshot_revision_id": "older",
            "manifest": {"report_uids": ["a"]},
        },
        {
            "fixed_snapshot_revision_id": "different",
            "manifest": {"report_uids": ["a", "b"]},
        },
        {
            "fixed_snapshot_revision_id": "current",
            "manifest": {"report_uids": ["a"]},
        },
    ]

    assert monitoring_views._snapshot_revision_for_scope(
        snapshots, ("a",)
    ) == "current"
    assert monitoring_views._snapshot_revision_for_scope(
        snapshots, ("missing",)
    ) is None


def test_snapshot_selection_preserves_operator_choices_and_observed_evidence() -> None:
    documents = (
        _snapshot_document("a", title="A", target="A사", broker="A증권"),
        _snapshot_document("b", title="B", target="B사", broker="B증권"),
        _snapshot_document("c", title="C", target="C사", broker="C증권"),
    )

    initialized = monitoring_views._reconcile_snapshot_selection(
        documents,
        current_uids=None,
        proposed_uids=("a", "b"),
        observed_uids=("a",),
    )
    operator_edited = monitoring_views._reconcile_snapshot_selection(
        documents,
        current_uids=["c", "missing"],
        proposed_uids=("a", "b"),
        observed_uids=("a",),
    )

    assert initialized == ("a", "b")
    assert operator_edited == ("a", "c")
    assert monitoring_views._snapshot_inclusion_reason(
        "a", observed_uids={"a"}, filter_matched_uids={"a", "b"}
    ) == "신고 당시 사용"
    assert monitoring_views._snapshot_inclusion_reason(
        "b", observed_uids={"a"}, filter_matched_uids={"a", "b"}
    ) == "같은 조건 제안"
    assert monitoring_views._snapshot_inclusion_reason(
        "c", observed_uids={"a"}, filter_matched_uids={"a", "b"}
    ) == "운영자 추가"


def test_snapshot_document_cache_key_tracks_catalog_wal_and_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    source_index = tmp_path / "active.faiss"
    catalog.write_bytes(b"catalog")
    source_index.write_bytes(b"index")
    monkeypatch.setattr(
        monitoring_views.fixed_snapshot,
        "resolve_active_snapshot_sources",
        lambda data_root: (catalog, source_index),
    )

    initial = monitoring_views._snapshot_source_cache_key(tmp_path)
    catalog.write_bytes(b"updated-catalog")
    with_updated_catalog = monitoring_views._snapshot_source_cache_key(tmp_path)
    Path(f"{catalog}-wal").write_bytes(b"wal")
    with_wal = monitoring_views._snapshot_source_cache_key(tmp_path)
    source_index.write_bytes(b"updated-index")
    with_updated_index = monitoring_views._snapshot_source_cache_key(tmp_path)

    assert with_updated_catalog != initial
    assert with_wal != with_updated_catalog
    assert with_updated_index != with_wal


def test_snapshot_observed_uids_are_valid_deduplicated_report_identities() -> None:
    valid_uid = "a" * 64
    seed = {
        "case_diagnostics": {
            "retrieval_observations": [
                {"source_uid": valid_uid},
                {"source_uid": "not-a-report-uid"},
                {"source_uid": valid_uid},
            ]
        }
    }

    assert snapshot_observed_report_uids(seed) == (valid_uid,)


def test_operator_monitoring_has_exactly_three_top_workspaces() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(VIEW_PATH))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_WORKSPACES" for target in node.targets)
    )

    assert ast.literal_eval(assignment.value) == ("작업함", "테스트 케이스 설정", "개선 확인")
    assert "설정 · 재현 자산 경고" in source
    assert '"정확도 평가"' not in source
    assert '"문서 읽기 품질 비교"' not in source


def test_issue_lifecycle_labels_separate_queue_progress_and_outcomes() -> None:
    assert monitoring_views._issue_state_label("OPEN") == "미확인"
    assert monitoring_views._issue_state_label("IN_PROGRESS") == "조치 중"
    assert monitoring_views._issue_state_label("RESOLVED") == "해결됨"
    assert monitoring_views._issue_state_label("NOT_ISSUE") == "이슈 아님"
    assert monitoring_views._issue_state_label("CLOSED") == "종료(미분류)"

    assert monitoring_views._available_issue_transitions("OPEN") == (
        ("IN_PROGRESS", "조치 시작"),
        ("RESOLVED", "해결됨으로 종료"),
        ("NOT_ISSUE", "이슈 아님으로 종료"),
    )
    assert monitoring_views._available_issue_transitions("IN_PROGRESS") == (
        ("OPEN", "미확인으로 되돌리기"),
        ("RESOLVED", "해결됨으로 종료"),
        ("NOT_ISSUE", "이슈 아님으로 종료"),
    )
    assert monitoring_views._available_issue_transitions("RESOLVED") == (
        ("OPEN", "다시 열기"),
        ("NOT_ISSUE", "이슈 아님으로 재분류"),
    )
    assert monitoring_views._available_issue_transitions("NOT_ISSUE") == (
        ("OPEN", "다시 열기"),
        ("RESOLVED", "해결됨으로 재분류"),
    )
    assert monitoring_views._available_issue_transitions("CLOSED") == (
        ("RESOLVED", "해결됨으로 분류"),
        ("NOT_ISSUE", "이슈 아님으로 분류"),
        ("OPEN", "다시 열기"),
    )


def test_work_inbox_exposes_status_counts_and_explicit_transition_reason() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    work_inbox = source.split("def _render_work_inbox(", 1)[1].split(
        "def _fixture_state_key(", 1
    )[0]

    for label in ("미확인", "조치 중", "해결됨", "이슈 아님", "종료(미분류)"):
        assert label in source
    assert "_render_issue_state_counts(all_issues)" in work_inbox
    assert 'st.selectbox("다음 상태"' in work_inbox
    assert "상태 변경 사유" in work_inbox
    assert "이슈 종료" not in work_inbox
    assert "registry.transition_issue(" not in work_inbox


def test_app_and_sidebar_fail_closed_around_production_operator_gate() -> None:
    app_source = APP_PATH.read_text(encoding="utf-8")
    sidebar_source = SIDEBAR_PATH.read_text(encoding="utf-8")

    assert "production_monitoring_enabled(" in app_source
    assert "if OPERATOR_MONITORING_ENABLED:" in app_source
    assert "if MONITORING_MODE:" in app_source
    assert "monitoring_enabled=MONITORING_MODE" in app_source
    assert "operator_monitoring_enabled=OPERATOR_MONITORING_ENABLED" in app_source
    assert "if monitoring_enabled:" in sidebar_source
    assert "if config_module.MONITORING_MODE:" not in sidebar_source


def test_default_release_runner_uses_registered_runtime_entrypoint_without_dummy_args() -> None:
    runner = monitoring_views.release_assets.default_release_runner()

    assert runner["command"] == [
        "{python}",
        "{runtime_root}/reproduction_runner.py",
    ]
    assert "{app_root}" not in runner["command"]


def test_release_registration_builds_project_sources_without_operator_paths() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    release_registration = source.split(
        "def _render_release_registration(", 1
    )[1].split("def _render_asset_settings(", 1)[0]

    assert 'st.text_input("배포 app package 경로")' not in release_registration
    assert 'st.text_input("고정 runtime/runner 경로")' not in release_registration
    assert "release_assets.inspect_current_project_release(" in release_registration
    assert "release_assets.prepare_current_project_release_stage(" in release_registration
    assert "project_root=settings_module.BASE_DIR" in release_registration
    assert 'app_version = st.text_input("app version"' not in release_registration
    assert 'git_revision = st.text_input("Git revision"' not in release_registration


def test_release_registration_derives_read_only_identity_from_current_project() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    release_registration = source.split(
        "def _render_release_registration(", 1
    )[1].split("def _render_asset_settings(", 1)[0]

    assert 'with st.form("register_release"):' in release_registration
    assert 'with st.form("register_release_stage"):' not in release_registration
    assert '"STAGED bundle 경로"' not in release_registration
    assert 'release_tag = st.text_input("공식 tag"' not in release_registration
    assert "release_assets.prepare_current_project_release_stage(" in release_registration
    assert "release_assets.register_release_stage(" in release_registration
    assert 'release_tag = f"v{current_identity.app_version}"' in release_registration
    assert "expected_git_revision=current_identity.git_revision" in release_registration
    assert "value=current_identity.app_version" in release_registration
    assert "value=current_identity.git_revision" in release_registration
    assert release_registration.count("disabled=True") >= 2


def test_asset_settings_warns_without_manual_exact_restore_controls() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    asset_settings = source.split("def _render_asset_settings(", 1)[1].split(
        "def render_operator_monitoring_page(", 1
    )[0]

    assert "warnings = _asset_warnings(registry)" in asset_settings
    assert "누락 cache는 실행 시 등록 commit에서 자동 재생성" in asset_settings
    assert 'st.markdown("**가용성 복구**")' not in asset_settings
    assert "def _render_exact_restore(" not in source
    assert "restore_release_bundle(" not in source
    assert "restore_fixed_snapshot(" not in source


def test_default_release_profile_snapshots_every_non_secret_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {}
    for index, key in enumerate(
        sorted(monitoring_views.release_assets.RUNTIME_PROFILE_ENVIRONMENT_KEYS)
    ):
        value = f"configured-{index}"
        monkeypatch.setattr(monitoring_views.config_module, key, value)
        expected[key] = value
    monkeypatch.setattr(
        monitoring_views.config_module,
        "OPENROUTER_API_KEY",
        "must-not-be-persisted",
    )

    profile = monitoring_views._default_release_runtime_profile()

    assert profile == {
        "environment": expected,
        "snapshot_reader": {
            "manifest_schema_version": monitoring_views.fixed_snapshot.MANIFEST_SCHEMA_VERSION,
        },
    }
    assert "OPENROUTER_API_KEY" not in profile["environment"]


def test_login_discards_password_and_refresh_token_is_never_requested() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")

    assert 'st.session_state.pop("monitoring_login_password", None)' in source
    assert "refresh_token" not in source
    assert "password=password" in source
    assert "st.session_state[_SESSION_KEY] = session" in source


def test_work_inbox_displays_raw_automatically_and_caches_the_audited_read() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    work_inbox = source.split("def _render_work_inbox(", 1)[1].split(
        "def _fixture_state_key(", 1
    )[0]

    assert "신고 요약" in source
    assert "신고 원문" in source
    assert "원문 열람 및 감사기록 남기기" not in source
    assert "신고 근거 명시적으로 열람하고 범위 제안" not in source
    assert "_load_raw_report(client" in source
    assert "monitoring_raw_report_" in source
    assert "client.view_raw" in source
    assert work_inbox.index("_render_summary(issue)") < work_inbox.index(
        "_load_raw_report(client"
    )


def test_comparison_exposes_required_side_by_side_evidence() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")

    for required in (
        "raw_answer",
        "evidence_refs",
        "check_result",
        "latency_ms",
        "runtime_profile",
        'st.button("Baseline 실행")',
        'st.button("Candidate 실행"',
        "supersedes_comparison_id",
    ):
        assert required in source


def test_comparison_exposes_issue_closure_after_verdict() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    comparison = source.split("def _render_comparison(", 1)[1].split(
        "def _asset_warnings(", 1
    )[0]

    assert "이슈 종결" in comparison
    assert "client.transition_issue(" in comparison
    assert "_available_issue_transitions(current_state)" in comparison
    assert "if target in _ISSUE_TERMINAL_TARGETS" in comparison
    assert "종결 사유를 입력하세요" in comparison


def test_run_action_registers_release_and_syncs_each_lifecycle_state() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    function = source.split("def _execute_new_run(", 1)[1].split(
        "def _release_label(", 1
    )[0]

    assert "include_release_manifest_ids=(release_manifest_id,)" in function
    assert function.index("include_release_manifest_ids") < function.index(
        ".execute_run("
    )
    assert "lifecycle_callback=synchronize_run_lifecycle" in function
    assert "progress_callback=render_run_progress" in function
    assert "runtime_profile=_default_release_runtime_profile()" in function
    assert 'st.status("실행 준비 중"' in function
    assert "st.progress(" in function
    assert "미완료 Run을 INTERRUPTED로 복구" in source


def test_run_projection_warning_keeps_local_terminal_result_visible() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    function = source.split("def _execute_new_run(", 1)[1].split(
        "def _release_label(", 1
    )[0]

    assert 'run.get("projection_sync_warnings")' in function
    assert "로컬 terminal 결과는 저장됐지만" in function


@pytest.mark.parametrize(
    ("execution_status", "validity", "expected_state", "label_fragment"),
    [
        ("FAILED", "INVALID", "error", "실행 실패"),
        ("SUCCEEDED", "INVALID", "complete", "판단 제외"),
        ("SUCCEEDED", "VALID", "complete", "실행 완료"),
    ],
)
def test_execute_new_run_keeps_terminal_outcome_visible(
    monkeypatch: pytest.MonkeyPatch,
    execution_status: str,
    validity: str,
    expected_state: str,
    label_fragment: str,
) -> None:
    status_updates: list[dict[str, object]] = []
    progress_updates: list[tuple[float, str]] = []

    class FakeStatus:
        def update(self, **kwargs: object) -> None:
            status_updates.append(dict(kwargs))

    class FakeProgress:
        def progress(self, value: float, *, text: str) -> None:
            progress_updates.append((value, text))

    run = {
        "run_id": "run-terminal-outcome",
        "execution_status": execution_status,
        "validity": validity,
    }

    class FakeService:
        @staticmethod
        def execute_run(**kwargs: object) -> dict[str, str]:
            callback = kwargs["progress_callback"]
            assert callable(callback)
            callback(
                {
                    "stage": execution_status,
                    "message": "fixture terminal outcome",
                    "step": 7,
                    "total_steps": 7,
                    **run,
                }
            )
            return run

    monkeypatch.setattr(
        monitoring_views,
        "_synchronize_control_projection",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(monitoring_views, "_service", lambda _registry: FakeService())
    monkeypatch.setattr(
        monitoring_views.st,
        "status",
        lambda *_args, **_kwargs: FakeStatus(),
    )
    monkeypatch.setattr(
        monitoring_views.st,
        "progress",
        lambda *_args, **_kwargs: FakeProgress(),
    )

    returned = monitoring_views._execute_new_run(
        object(),
        object(),
        remote_issue_id="remote-issue",
        issue={"issue_id": "local-issue"},
        case={"case_contract_id": "case-contract"},
        release_manifest_id="release-manifest",
        side="BASELINE",
    )

    assert returned == run
    assert progress_updates[-1][0] == 1.0
    assert status_updates[-1]["state"] == expected_state
    assert label_fragment in str(status_updates[-1]["label"])


def test_run_results_remain_visible_with_status_timestamps_and_errors() -> None:
    source = VIEW_PATH.read_text(encoding="utf-8")
    action = source.split("def _render_run_action(", 1)[1].split(
        "def _render_reproduction(", 1
    )[0]
    detail = source.split("def _render_run_detail(", 1)[1].split(
        "def _runtime_profile_diff(", 1
    )[0]

    assert 'side="BASELINE"' in action
    assert "최근 Baseline 실행 및 결과" in action
    assert "registry.get_run" in action
    for required in (
        "execution_status",
        "validity",
        "queued_at",
        "started_at",
        "completed_at",
        "error_type",
        "error_message",
        "raw_answer",
        "evidence_refs",
        "check_result",
        "latency_ms",
        "runtime_profile",
    ):
        assert required in detail


def test_execution_gate_blocks_unresolved_supabase_local_control_drift(
    monkeypatch,
) -> None:
    expected = [
        {
            "record_kind": "FIXTURE",
            "record_id": "fixture-1",
            "lifecycle_status": "READY",
            "content_digest": "a" * 64,
            "availability": None,
            "references": {},
            "attributes": {},
        }
    ]
    monkeypatch.setattr(
        monitoring_views,
        "_control_projection",
        lambda _registry, _issue, **_kwargs: expected,
    )

    class DriftedClient:
        @staticmethod
        def reconcile_control_projection(_issue_id, _expected):
            return {
                "missing": [],
                "unexpected": [{"record_kind": "RUN", "record_id": "run-1"}],
                "mismatched": [],
            }

    with pytest.raises(MonitoringServiceError, match="실행·종료를 중단"):
        monitoring_views._synchronize_control_projection(
            DriftedClient(),
            object(),
            remote_issue_id="remote-1",
            local_issue={"issue_id": "local-1"},
        )


def test_settings_warn_before_close_when_remote_control_has_no_local_issue(
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        monitoring_views.st.session_state,
        monitoring_views._ISSUE_KEY,
        "remote-1",
    )

    class RegistryWithoutIssue:
        @staticmethod
        def list_issues():
            return []

    class ClientWithRemoteControl:
        @staticmethod
        def list_control_records(issue_id):
            assert issue_id == "remote-1"
            return [{"record_kind": "RUN", "record_id": "run-1"}]

    warning = monitoring_views._control_projection_warning(
        ClientWithRemoteControl(), RegistryWithoutIssue()
    )

    assert warning is not None
    assert "로컬 Issue가 없습니다" in warning
    assert "exact 복원" in warning
