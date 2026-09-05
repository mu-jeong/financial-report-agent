"""Authenticated one-administrator workflow for release-scoped monitoring."""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping

import streamlit as st

from src.configs import config as config_module
from src.configs import settings as settings_module
from src.core import fixed_snapshot, release_assets
from src.core.monitoring_admin_client import (
    MonitoringAdminClient,
    OperatorApiError,
    OperatorConflictError,
    OperatorForbiddenError,
    OperatorSession,
    OperatorUnauthorizedError,
    load_operator_api_config,
    production_monitoring_enabled,
    sign_in_with_password,
)
from src.core.operator_monitoring import (
    MonitoringRecordNotFound,
    MonitoringRegistry,
    MonitoringRegistryError,
)
from src.core.operator_monitoring_service import (
    MonitoringServiceError,
    ReleaseScopedMonitoringService,
    reproduction_seed_from_raw_report,
    snapshot_observed_report_uids,
)


_SESSION_KEY = "monitoring_operator_session"
_ISSUE_KEY = "monitoring_selected_issue_id"
_ISSUE_SELECTOR_KEY = "monitoring_selected_issue_selector"
_WORKSPACE_KEY = "monitoring_operator_workspace"
_REPRODUCTION_SEED_PREFIX = "monitoring_reproduction_seed_"
_RAW_REPORT_PREFIX = "monitoring_raw_report_"
_SNAPSHOT_STATE_PREFIX = "monitoring_snapshot_"
_MAX_SNAPSHOT_SEARCH_RESULTS = 100
_WORKSPACES = ("작업함", "재현 케이스", "버전 비교")
_ISSUE_STATE_LABELS = {
    "OPEN": "미확인",
    "IN_PROGRESS": "조치 중",
    "RESOLVED": "해결됨",
    "NOT_ISSUE": "이슈 아님",
    "CLOSED": "종료(미분류)",
}
_ISSUE_TRANSITIONS = {
    "OPEN": (
        ("IN_PROGRESS", "조치 시작"),
        ("RESOLVED", "해결됨으로 종료"),
        ("NOT_ISSUE", "이슈 아님으로 종료"),
    ),
    "IN_PROGRESS": (
        ("OPEN", "미확인으로 되돌리기"),
        ("RESOLVED", "해결됨으로 종료"),
        ("NOT_ISSUE", "이슈 아님으로 종료"),
    ),
    "RESOLVED": (
        ("OPEN", "다시 열기"),
        ("NOT_ISSUE", "이슈 아님으로 재분류"),
    ),
    "NOT_ISSUE": (
        ("OPEN", "다시 열기"),
        ("RESOLVED", "해결됨으로 재분류"),
    ),
    "CLOSED": (
        ("RESOLVED", "해결됨으로 분류"),
        ("NOT_ISSUE", "이슈 아님으로 분류"),
        ("OPEN", "다시 열기"),
    ),
}
_ISSUE_TERMINAL_TARGETS = frozenset({"RESOLVED", "NOT_ISSUE"})
_PROGRESS_LABELS = {
    "NOT_PREPARED": "재현 케이스 준비 전",
    "NOT_OBSERVED": "아직 증상을 확인하지 못함",
    "REPRODUCED": "운영 배포본에서 증상 재현됨",
    "NOT_COMPARED": "버전 비교 전",
    "IMPROVED": "개선됨",
    "NOT_IMPROVED": "개선되지 않음",
    "REGRESSED": "이전보다 나빠짐",
    "INCONCLUSIVE": "판단 보류",
    "PREPARE_CASE": "Fixture와 FixedSnapshot으로 재현 케이스를 준비하세요.",
    "RUN_BASELINE": "신고가 발생한 배포 버전으로 Baseline을 실행하세요.",
    "REVIEW_OR_REPEAT_BASELINE": "결과를 검토하고 필요할 때 Baseline을 다시 실행하세요.",
    "RUN_CANDIDATE": "개선 후보 버전으로 Candidate를 실행하세요.",
    "COMPARE_RUNS": "같은 재현 케이스의 Baseline과 Candidate를 비교하세요.",
    "CLOSE_ISSUE": "개선 근거를 확인한 뒤 해결됨 또는 이슈 아님으로 종결하세요.",
    "RUN_AGAIN_OR_REJUDGE": "필요한 만큼 다시 실행하거나 새 판단을 남기세요.",
    "IMPROVE_AND_RERUN": "추가 개선 후 같은 케이스로 Candidate를 다시 실행하세요.",
    "FIX_REGRESSION": "회귀 원인을 수정하고 같은 케이스로 다시 검증하세요.",
}
_RUN_STATUS_LABELS = {
    "QUEUED": "⏳ 대기",
    "RUNNING": "▶ 실행 중",
    "SUCCEEDED": "✓ 성공",
    "FAILED": "✕ 실패",
    "CANCELLED": "■ 취소",
    "INTERRUPTED": "⚠ 중단",
}
_RUN_VALIDITY_LABELS = {
    "VALID": "판단에 사용 가능",
    "INVALID": "판단에서 제외",
}


def operator_surface_enabled() -> bool:
    """Fail closed unless this is the fully configured production deployment."""

    return production_monitoring_enabled(
        deployment_environment=config_module.DEPLOYMENT_ENVIRONMENT,
        monitoring_mode=config_module.MONITORING_MODE,
        config=load_operator_api_config(),
    )


@st.cache_resource(show_spinner=False)
def _cached_registry(root_value: str) -> MonitoringRegistry:
    root = Path(root_value).resolve()
    return MonitoringRegistry(root / "registry.sqlite3", artifact_root=root)


def _registry() -> MonitoringRegistry:
    root = load_operator_api_config().artifact_root.resolve()
    return _cached_registry(str(root))


def _service(registry: MonitoringRegistry) -> ReleaseScopedMonitoringService:
    return ReleaseScopedMonitoringService(
        registry,
        managed_root=load_operator_api_config().artifact_root,
        project_root=settings_module.BASE_DIR,
    )


def _snapshot_source_cache_key(data_root: str | Path) -> tuple[Any, ...]:
    """Identify the active publication and invalidate metadata on file changes."""

    catalog, source_index = fixed_snapshot.resolve_active_snapshot_sources(data_root)

    def signature(
        path: Path,
    ) -> tuple[str, int | None, int | None, int | None, int | None]:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return str(path), None, None, None, None
        return (
            str(path),
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_size,
            metadata.st_ino,
        )

    return (
        signature(catalog),
        signature(Path(f"{catalog}-wal")),
        signature(source_index),
    )


@st.cache_data(show_spinner=False, max_entries=4)
def _cached_snapshot_documents(
    source_key: tuple[Any, ...],
    data_root_value: str,
    _loader: Callable[..., tuple[fixed_snapshot.ActiveReportDocument, ...]],
) -> tuple[fixed_snapshot.ActiveReportDocument, ...]:
    """Cache metadata only while the resolved active publication is unchanged."""

    del source_key
    return tuple(_loader(data_root=Path(data_root_value)))


def _load_snapshot_documents(
    service: ReleaseScopedMonitoringService,
    *,
    data_root: str | Path,
) -> tuple[fixed_snapshot.ActiveReportDocument, ...]:
    root = Path(data_root).expanduser().resolve()
    return _cached_snapshot_documents(
        _snapshot_source_cache_key(root),
        str(root),
        _loader=service.list_snapshot_documents,
    )


def _client(session: OperatorSession) -> MonitoringAdminClient:
    return MonitoringAdminClient(load_operator_api_config(), session)


def _clear_session() -> None:
    st.session_state.pop(_SESSION_KEY, None)
    st.session_state.pop(_ISSUE_KEY, None)
    st.session_state.pop(_ISSUE_SELECTOR_KEY, None)
    st.session_state.pop("monitoring_selected_issue_detail", None)
    for key in list(st.session_state):
        if str(key).startswith(
            (
                _REPRODUCTION_SEED_PREFIX,
                _RAW_REPORT_PREFIX,
                _SNAPSHOT_STATE_PREFIX,
            )
        ):
            st.session_state.pop(key, None)


def _seed_key(issue_id: str) -> str:
    return f"{_REPRODUCTION_SEED_PREFIX}{issue_id}"


def _raw_report_key(issue_id: str) -> str:
    return f"{_RAW_REPORT_PREFIX}{issue_id}"


def _load_raw_report(
    client: MonitoringAdminClient, issue_id: str
) -> dict[str, Any]:
    key = _raw_report_key(issue_id)
    cached = st.session_state.get(key)
    if isinstance(cached, Mapping):
        raw = dict(cached)
    else:
        raw = client.view_raw(issue_id)
        st.session_state[key] = raw
    seed_key = _seed_key(issue_id)
    if not isinstance(st.session_state.get(seed_key), Mapping):
        st.session_state[seed_key] = reproduction_seed_from_raw_report(raw)
    return raw


def _error_message(exc: BaseException) -> str:
    code = getattr(exc, "code", str(exc))
    return {
        "login failed": "이메일 또는 비밀번호를 확인하세요.",
        "session_expired": "로그인 시간이 만료되었습니다. 다시 로그인하세요.",
        "not_active_monitoring_admin": "활성 운영자 계정만 Monitoring을 열 수 있습니다.",
        "operator_api_unavailable": "운영자 API에 연결하지 못했습니다.",
        "auth_unavailable": "인증 서비스에 연결하지 못했습니다.",
        "method_not_allowed": (
            "운영자 API 배포 버전이 현재 앱보다 오래되었습니다. "
            "Supabase migration과 issue-report-operator Function 배포 상태를 확인하세요."
        ),
    }.get(str(code), f"요청을 처리하지 못했습니다: {code}")


def _render_login() -> None:
    st.title("운영 Monitoring 로그인")
    st.info(
        "이 화면은 운영환경의 신고 로그를 다루므로 활성 admin 계정으로만 열립니다. "
        "비밀번호와 refresh token은 저장하지 않으며, 짧게 유효한 access token만 현재 화면 세션의 메모리에 둡니다."
    )
    with st.form("monitoring_operator_login", clear_on_submit=True):
        email = st.text_input("이메일", key="monitoring_login_email")
        password = st.text_input(
            "비밀번호", type="password", key="monitoring_login_password"
        )
        submitted = st.form_submit_button("로그인", type="primary")
    if not submitted:
        return
    # Streamlit widget values normally live in session_state. Remove credentials
    # immediately after this single authentication attempt.
    st.session_state.pop("monitoring_login_email", None)
    st.session_state.pop("monitoring_login_password", None)
    try:
        session = sign_in_with_password(
            load_operator_api_config(), email=email, password=password
        )
    except OperatorApiError as exc:
        st.error(_error_message(exc))
        return
    st.session_state[_SESSION_KEY] = session
    st.rerun()


def _receipt_id(issue: Mapping[str, Any]) -> str:
    return str(issue.get("receipt_id") or issue.get("source_receipt_id") or issue["issue_id"])


def _find_local_issue(
    registry: MonitoringRegistry, remote_issue: Mapping[str, Any]
) -> dict[str, Any] | None:
    receipt_ids = {
        _receipt_id(remote_issue),
        f"supabase:{remote_issue['issue_id']}",
    }
    return next(
        (
            item
            for item in registry.list_issues()
            if item["source_receipt_id"] in receipt_ids
        ),
        None,
    )


def _ensure_local_issue(
    registry: MonitoringRegistry, remote_issue: Mapping[str, Any]
) -> dict[str, Any]:
    existing = _find_local_issue(registry, remote_issue)
    if existing:
        return existing
    return _service(registry).import_remote_issue(remote_issue)


def _control_projection(
    registry: MonitoringRegistry,
    local_issue: Mapping[str, Any],
    *,
    include_release_manifest_ids: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    return _service(registry).build_control_projection(
        str(local_issue["issue_id"]),
        include_release_manifest_ids=include_release_manifest_ids,
    )


def _control_drift_message(
    diff: Mapping[str, list[dict[str, Any]]],
) -> str | None:
    counts = {
        key: len(diff.get(key) or [])
        for key in ("missing", "unexpected", "mismatched")
    }
    if not any(counts.values()):
        return None
    return (
        "Supabase와 로컬 제어 기록이 다릅니다: "
        f"원격 누락 {counts['missing']} · 로컬 누락 {counts['unexpected']} · "
        f"내용 불일치 {counts['mismatched']}"
    )


def _synchronize_control_projection(
    client: MonitoringAdminClient,
    registry: MonitoringRegistry,
    *,
    remote_issue_id: str,
    local_issue: Mapping[str, Any],
    include_release_manifest_ids: tuple[str, ...] = (),
) -> None:
    expected = _control_projection(
        registry,
        local_issue,
        include_release_manifest_ids=include_release_manifest_ids,
    )
    diff = client.reconcile_control_projection(remote_issue_id, expected)
    message = _control_drift_message(diff)
    if message:
        raise MonitoringServiceError(
            f"{message}. 실행·종료를 중단하고 제어 기록 충돌을 확인하세요."
        )


def _issue_label(issue: Mapping[str, Any]) -> str:
    category = issue.get("category") or issue.get("kind") or "신고"
    version = issue.get("app_version") or "버전 미상"
    state = issue.get("state") or issue.get("status") or "OPEN"
    return (
        f"[{_issue_state_label(str(state))}] {category} · {version} · "
        f"{str(issue.get('issue_id', ''))[:8]}"
    )


def _issue_state_label(state: str) -> str:
    normalized = str(state or "OPEN").upper()
    return _ISSUE_STATE_LABELS.get(normalized, normalized)


def _available_issue_transitions(state: str) -> tuple[tuple[str, str], ...]:
    return _ISSUE_TRANSITIONS.get(str(state or "OPEN").upper(), ())


def _render_issue_state_counts(issues: list[dict[str, Any]]) -> None:
    counts = {state: 0 for state in _ISSUE_STATE_LABELS}
    for issue in issues:
        state = str(issue.get("state") or issue.get("status") or "OPEN").upper()
        if state in counts:
            counts[state] += 1
    for column, (state, label) in zip(
        st.columns(len(_ISSUE_STATE_LABELS)), _ISSUE_STATE_LABELS.items()
    ):
        column.metric(label, counts[state])


def _selected_issue(
    client: MonitoringAdminClient,
    *,
    state: str | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if issues is None:
        issues = client.list_issues(state=state, limit=200)
    if not issues:
        return issues, None
    ids = [str(item["issue_id"]) for item in issues]
    current = st.session_state.get(_ISSUE_KEY)
    if current not in ids:
        current = ids[0]
        st.session_state[_ISSUE_KEY] = current
    if st.session_state.get(_ISSUE_SELECTOR_KEY) not in ids:
        st.session_state[_ISSUE_SELECTOR_KEY] = current
    selected_id = st.selectbox(
        "신고 선택",
        ids,
        key=_ISSUE_SELECTOR_KEY,
        format_func=lambda value: _issue_label(
            next(item for item in issues if str(item["issue_id"]) == value)
        ),
    )
    st.session_state[_ISSUE_KEY] = str(selected_id)
    return issues, client.get_issue(str(selected_id))


def _render_summary(issue: Mapping[str, Any]) -> None:
    st.subheader("신고 요약")
    st.caption(
        "일상적인 확인에는 요약만 사용합니다. 사용자가 동의해 보낸 질문·답변 같은 원문은 아래의 명시적 열람 버튼을 눌렀을 때만 표시되고 열람 이력이 남습니다."
    )
    left, middle, right = st.columns(3)
    left.metric("상태", _issue_state_label(str(issue.get("state") or "OPEN")))
    middle.metric("신고 버전", str(issue.get("app_version") or "-"))
    right.metric("경로", str(issue.get("route") or "-"))
    rows = {
        "분류": issue.get("category") or issue.get("kind"),
        "응답 상태": issue.get("status"),
        "진단 자료": issue.get("case_diagnostics_status"),
        "응답 시간(ms)": issue.get("latency_ms"),
        "검색 결과 수": issue.get("result_count"),
        "인용 수": issue.get("citation_count"),
        "사용자 동의 범위": issue.get("consented_content"),
    }
    st.json({key: value for key, value in rows.items() if value is not None})


def _progress(registry: MonitoringRegistry, local_issue: dict[str, Any] | None) -> dict[str, str]:
    if local_issue is None:
        return {
            "reproduction": "NOT_PREPARED",
            "comparison": "NOT_COMPARED",
            "next_action": "PREPARE_CASE",
        }
    return registry.derive_issue_progress(local_issue["issue_id"])


def _render_workload_observations(
    issues: list[dict[str, Any]], registry: MonitoringRegistry
) -> None:
    """Show the two observation axes without turning them into release gates."""

    latencies = [
        float(item["latency_ms"])
        for item in issues
        if isinstance(item.get("latency_ms"), (int, float))
        and not isinstance(item.get("latency_ms"), bool)
        and float(item["latency_ms"]) >= 0
    ]
    comparisons = [
        comparison
        for local_issue in registry.list_issues()
        for comparison in registry.list_comparisons(local_issue["issue_id"])
    ]
    latest_by_issue: dict[str, dict[str, Any]] = {}
    for comparison in comparisons:
        latest_by_issue[comparison["issue_id"]] = comparison
    verdict_counts: dict[str, int] = {}
    for comparison in latest_by_issue.values():
        verdict = str(comparison["verdict"])
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1

    speed, quality = st.columns(2)
    speed.metric(
        "응답 속도 관찰",
        f"{statistics.median(latencies):,.0f} ms" if latencies else "측정 전",
    )
    speed.caption(
        f"현재 목록의 측정 표본 {len(latencies)}건 · 절대 통과 기준 없음"
    )
    quality.metric("답변 품질 판단", f"{len(latest_by_issue)}건")
    quality.caption(
        " · ".join(
            f"{_PROGRESS_LABELS.get(verdict, verdict)} {count}"
            for verdict, count in sorted(verdict_counts.items())
        )
        or "아직 저장된 정성 판단 없음 · 자동 점수 없음"
    )


def _render_progress(progress: Mapping[str, str]) -> None:
    st.subheader("현재 근거와 다음 할 일")
    first, second = st.columns(2)
    first.metric("재현 결과", _PROGRESS_LABELS.get(progress["reproduction"], progress["reproduction"]))
    second.metric("비교 판단", _PROGRESS_LABELS.get(progress["comparison"], progress["comparison"]))
    st.info(_PROGRESS_LABELS.get(progress["next_action"], progress["next_action"]))


def _render_work_inbox(client: MonitoringAdminClient, registry: MonitoringRegistry) -> None:
    st.header("작업함")
    st.write(
        "사용자가 신고한 문제의 요약과 동의된 원문을 함께 확인하고 재현 케이스로 넘기는 곳입니다."
    )
    all_issues = client.list_issues(state=None, limit=200)
    _render_issue_state_counts(all_issues)
    st.caption("상태별 건수는 최근 신고 최대 200건을 기준으로 합니다.")
    filter_labels = (*_ISSUE_STATE_LABELS.values(), "전체")
    state_label = st.segmented_control(
        "상태 필터", filter_labels, default=_ISSUE_STATE_LABELS["OPEN"]
    )
    state_label = state_label or _ISSUE_STATE_LABELS["OPEN"]
    state_by_label = {label: state for state, label in _ISSUE_STATE_LABELS.items()}
    state = state_by_label.get(str(state_label))
    filtered_issues = [
        item
        for item in all_issues
        if state is None
        or str(item.get("state") or item.get("status") or "OPEN").upper() == state
    ]
    issues, issue = _selected_issue(client, issues=filtered_issues)
    st.caption(f"현재 조건의 신고 {len(issues)}건")
    _render_workload_observations(issues, registry)
    if issue is None:
        st.info("현재 조건에 맞는 신고가 없습니다.")
        return
    _render_summary(issue)
    local_issue = _find_local_issue(registry, issue)
    _render_progress(_progress(registry, local_issue))

    st.subheader("신고 원문")
    st.caption(
        "선택한 신고의 동의된 원문을 자동으로 표시합니다. 최초 표시 시 감사기록 1건을 남기고 현재 로그인 세션에서는 같은 원문을 다시 요청하지 않습니다."
    )
    try:
        raw = _load_raw_report(client, str(issue["issue_id"]))
    except OperatorApiError as exc:
        st.error(f"신고 원문을 불러오지 못했습니다: {_error_message(exc)}")
    else:
        st.json(raw)

    current_state = str(issue.get("state") or "OPEN").upper()
    transitions = _available_issue_transitions(current_state)
    if not transitions:
        st.warning("이 상태에서 사용할 수 있는 상태 변경이 없습니다.")
        return
    transition_by_label = {label: target for target, label in transitions}
    with st.form(f"transition_{issue['issue_id']}"):
        transition_label = st.selectbox("다음 상태", tuple(transition_by_label))
        reason = st.text_area(
            "상태 변경 사유",
            placeholder="무엇을 확인했고 왜 상태를 바꾸는지 기록하세요.",
        )
        submitted = st.form_submit_button("상태 변경 저장")
    if submitted:
        target = transition_by_label[str(transition_label)]
        reason = str(reason or "").strip()
        if not reason:
            st.error("상태 변경 사유를 입력하세요.")
            return
        try:
            if local_issue is not None:
                _synchronize_control_projection(
                    client,
                    registry,
                    remote_issue_id=str(issue["issue_id"]),
                    local_issue=local_issue,
                )
            elif target in _ISSUE_TERMINAL_TARGETS and client.list_control_records(
                str(issue["issue_id"])
            ):
                raise MonitoringServiceError(
                    "Supabase에는 재현 제어 기록이 있지만 로컬 Issue가 없습니다. "
                    "로컬 registry를 exact 복원하기 전에는 이슈를 종결할 수 없습니다."
                )
            client.transition_issue(
                str(issue["issue_id"]),
                target_state=target,
                expected_record_revision=int(issue["record_revision"]),
                reason=reason,
            )
        except OperatorConflictError:
            st.warning("다른 화면에서 이슈가 변경되었습니다. 새로고침 후 다시 확인하세요.")
        except (OperatorApiError, MonitoringRegistryError, MonitoringServiceError, ValueError) as exc:
            st.error(_error_message(exc))
        else:
            st.success(f"{transition_label} 상태와 사유를 저장했습니다.")
            st.rerun()


def _fixture_state_key(issue_id: str) -> str:
    return f"monitoring_fixture_revision_{issue_id}"


def _case_state_key(issue_id: str) -> str:
    return f"monitoring_case_revision_{issue_id}"


def _snapshot_scope_state_key(issue_id: str) -> str:
    return f"{_SNAPSHOT_STATE_PREFIX}scope_{issue_id}"


def _snapshot_revision_state_key(issue_id: str) -> str:
    return f"{_SNAPSHOT_STATE_PREFIX}revision_{issue_id}"


def _snapshot_feedback_state_key(issue_id: str) -> str:
    return f"{_SNAPSHOT_STATE_PREFIX}feedback_{issue_id}"


def _snapshot_revision_for_scope(
    snapshots: list[Mapping[str, Any]], report_uids: tuple[str, ...]
) -> str | None:
    target = tuple(sorted(set(report_uids)))
    if not target:
        return None
    for snapshot in reversed(snapshots):
        manifest = snapshot.get("manifest")
        manifest_uids = (
            manifest.get("report_uids")
            if isinstance(manifest, Mapping)
            else None
        )
        if not isinstance(manifest_uids, list):
            continue
        candidate = tuple(
            sorted(
                {
                    value
                    for value in manifest_uids
                    if isinstance(value, str) and value
                }
            )
        )
        if candidate == target:
            revision_id = str(snapshot.get("fixed_snapshot_revision_id") or "").strip()
            if revision_id:
                return revision_id
    return None


def _snapshot_route_filters(
    reproduction_seed: Mapping[str, Any],
) -> dict[str, Any]:
    diagnostics = reproduction_seed.get("case_diagnostics")
    diagnostic_body = diagnostics if isinstance(diagnostics, Mapping) else {}
    filters: dict[str, Any] = {}
    for route in diagnostic_body.get("route_observations") or []:
        if isinstance(route, Mapping) and isinstance(
            route.get("filters"), Mapping
        ):
            filters.update(dict(route["filters"]))
    return filters


def _snapshot_filter_summary(filters: Mapping[str, Any]) -> str:
    labels = {
        "target_name": "대상",
        "target_names": "대상",
        "broker": "증권사",
        "brokers": "증권사",
        "report_date": "기준일",
        "report_date_start": "시작일",
        "report_date_end": "종료일",
        "report_type": "문서 유형",
        "report_types": "문서 유형",
        "file_name": "파일명",
        "file_names": "파일명",
    }
    parts: list[str] = []
    for key, value in filters.items():
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        text = ", ".join(str(item) for item in values if str(item))
        if text:
            parts.append(f"{labels.get(str(key), str(key))}: {text}")
    return " · ".join(parts)


def _snapshot_document_label(
    document: fixed_snapshot.ActiveReportDocument,
) -> str:
    identity = " · ".join(
        value
        for value in (
            document.report_date,
            document.target_name or "대상 없음",
            document.broker or "증권사 없음",
            document.report_type,
        )
        if value
    )
    title = document.title or document.file_name
    return f"{title} — {identity} · {document.file_name}"


def _parse_report_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _snapshot_date_filter(
    selected: Any,
) -> tuple[str | None, str | None]:
    if isinstance(selected, (tuple, list)) and selected:
        first = selected[0]
        second = selected[1] if len(selected) > 1 else first
        return (
            first.isoformat() if isinstance(first, date) else None,
            second.isoformat() if isinstance(second, date) else None,
        )
    if isinstance(selected, date):
        return selected.isoformat(), selected.isoformat()
    return None, None


def _search_snapshot_documents(
    documents: tuple[fixed_snapshot.ActiveReportDocument, ...],
    *,
    query: str = "",
    report_type: str | None = None,
    broker: str | None = None,
    report_date_start: str | None = None,
    report_date_end: str | None = None,
    limit: int = _MAX_SNAPSHOT_SEARCH_RESULTS,
) -> tuple[tuple[fixed_snapshot.ActiveReportDocument, ...], int]:
    """Search human-readable metadata without reading report content."""

    tokens = tuple(token for token in query.casefold().split() if token)
    matches: list[fixed_snapshot.ActiveReportDocument] = []
    for document in documents:
        if report_type and document.report_type != report_type:
            continue
        if broker and document.broker != broker:
            continue
        if report_date_start and (
            not document.report_date or document.report_date < report_date_start
        ):
            continue
        if report_date_end and (
            not document.report_date or document.report_date > report_date_end
        ):
            continue
        searchable = "\n".join(
            (
                document.title,
                document.target_name or "",
                document.broker,
                document.report_type,
                document.report_date,
                document.file_name,
                document.canonical_relative_path,
            )
        ).casefold()
        if all(token in searchable for token in tokens):
            matches.append(document)
    bounded_limit = max(1, int(limit))
    return tuple(matches[:bounded_limit]), len(matches)


def _reconcile_snapshot_selection(
    documents: tuple[fixed_snapshot.ActiveReportDocument, ...],
    *,
    current_uids: object,
    proposed_uids: tuple[str, ...],
    observed_uids: tuple[str, ...],
) -> tuple[str, ...]:
    """Keep valid operator choices while making observed evidence mandatory."""

    if isinstance(current_uids, (list, tuple, set)):
        desired = {str(value) for value in current_uids}
    else:
        desired = set(proposed_uids)
    desired.update(observed_uids)
    return tuple(
        document.report_uid
        for document in documents
        if document.report_uid in desired
    )


def _add_snapshot_documents(
    *,
    scope_key: str,
    add_key: str,
    feedback_key: str,
    ordered_uids: tuple[str, ...],
) -> None:
    """Apply an add selection before Streamlit renders the click rerun."""

    available = set(ordered_uids)
    current = {
        str(value)
        for value in (st.session_state.get(scope_key) or [])
        if str(value) in available
    }
    requested = {
        str(value)
        for value in (st.session_state.get(add_key) or [])
        if str(value) in available
    }
    added = requested - current
    current.update(requested)
    st.session_state[scope_key] = [
        report_uid for report_uid in ordered_uids if report_uid in current
    ]
    st.session_state[add_key] = []
    st.session_state[feedback_key] = {"action": "added", "count": len(added)}


def _remove_snapshot_documents(
    *,
    scope_key: str,
    remove_key: str,
    feedback_key: str,
    ordered_uids: tuple[str, ...],
    protected_uids: tuple[str, ...],
) -> None:
    """Apply a removable selection before Streamlit renders the click rerun."""

    available = set(ordered_uids)
    protected = set(protected_uids)
    current = {
        str(value)
        for value in (st.session_state.get(scope_key) or [])
        if str(value) in available
    }
    requested = {
        str(value)
        for value in (st.session_state.get(remove_key) or [])
        if str(value) in available and str(value) not in protected
    }
    removed = current & requested
    current.difference_update(removed)
    current.update(protected & available)
    st.session_state[scope_key] = [
        report_uid for report_uid in ordered_uids if report_uid in current
    ]
    st.session_state[remove_key] = []
    st.session_state[feedback_key] = {"action": "removed", "count": len(removed)}


def _reset_snapshot_documents(
    *,
    scope_key: str,
    remove_key: str,
    feedback_key: str,
    reset_uids: tuple[str, ...],
) -> None:
    """Restore the exact automatic proposal before the click rerun renders."""

    st.session_state[scope_key] = list(reset_uids)
    st.session_state[remove_key] = []
    st.session_state[feedback_key] = {"action": "reset", "count": len(reset_uids)}


def _snapshot_inclusion_reason(
    report_uid: str,
    *,
    observed_uids: set[str],
    filter_matched_uids: set[str],
) -> str:
    if report_uid in observed_uids:
        return "신고 당시 사용"
    if report_uid in filter_matched_uids:
        return "같은 조건 제안"
    return "운영자 추가"


def _snapshot_document_row(
    document: fixed_snapshot.ActiveReportDocument,
    *,
    reason: str,
) -> dict[str, str]:
    return {
        "포함 근거": reason,
        "날짜": document.report_date or "-",
        "대상": document.target_name or "-",
        "증권사": document.broker or "-",
        "유형": document.report_type or "-",
        "제목": document.title or "-",
        "파일": document.file_name or "-",
    }


def _render_fixture(registry: MonitoringRegistry, local_issue: Mapping[str, Any]) -> dict[str, Any] | None:
    st.subheader("1. Fixture — 같은 질문과 확인 기준 고정")
    st.write(
        "Fixture는 ‘무엇을 물었고 어떤 증상이 문제이며 무엇을 확인할지’를 고정합니다. READY가 된 뒤에는 고치지 않고 새 revision을 만들어야 과거 비교가 흔들리지 않습니다."
    )
    key = _fixture_state_key(str(local_issue["issue_id"]))
    revisions = registry.list_fixture_revisions(str(local_issue["issue_id"]))
    fixture_id = st.session_state.get(key)
    revision_ids = [str(item["fixture_revision_id"]) for item in revisions]
    if fixture_id not in revision_ids:
        fixture_id = None
        current_contract = local_issue.get("current_case_contract_id")
        if current_contract:
            try:
                fixture_id = registry.get_case_by_contract(
                    str(current_contract)
                )["fixture_revision_id"]
            except MonitoringRecordNotFound:
                fixture_id = None
        if fixture_id not in revision_ids:
            fixture_id = revision_ids[-1] if revision_ids else None
        if fixture_id:
            st.session_state[key] = fixture_id
    if revisions:
        selected_fixture_id = st.selectbox(
            "Fixture revision",
            revision_ids,
            index=revision_ids.index(str(fixture_id)),
            key=f"fixture_select_{local_issue['issue_id']}_{len(revisions)}",
            format_func=lambda value: next(
                f"{item['lifecycle_status']} · {value[:16]}"
                for item in revisions
                if item["fixture_revision_id"] == value
            ),
        )
        fixture_id = str(selected_fixture_id)
        st.session_state[key] = fixture_id
    fixture = registry.get_fixture_revision(str(fixture_id)) if fixture_id else None
    if fixture is None:
        seed = st.session_state.get(_seed_key(str(local_issue["source_receipt_id"]).removeprefix("supabase:")))
        seed_body = seed if isinstance(seed, Mapping) else {}
        with st.form(f"fixture_create_{local_issue['issue_id']}"):
            question = st.text_area(
                "재현할 질문", value=str(seed_body.get("question") or "")
            )
            symptom = st.text_area(
                "신고된 증상",
                value=str(seed_body.get("reported_symptom") or ""),
            )
            expected = st.text_area("기대하는 동작")
            check_type = st.selectbox(
                "자동 확인 기준", ("ANSWER_CONTAINS", "ANSWER_NOT_CONTAINS", "CITATION_PRESENT", "ROUTE_EQUALS")
            )
            check_value = st.text_input(
                "기준 값",
                help="CITATION_PRESENT는 비워도 됩니다. 나머지는 포함/제외 문구 또는 기대 route를 적으세요.",
            )
            manual = st.text_area("운영자가 직접 확인할 항목(선택)")
            create = st.form_submit_button("Fixture 초안 만들기")
        if create:
            typed_check: dict[str, Any] = {"type": check_type}
            if check_type != "CITATION_PRESENT":
                typed_check["expected"] = check_value
            try:
                fixture = registry.create_fixture_revision(
                    issue_id=str(local_issue["issue_id"]),
                    question=question,
                    reported_symptom=symptom,
                    expected_behavior=expected,
                    typed_checks=[typed_check],
                    manual_checks=[manual] if manual.strip() else [],
                )
            except MonitoringRegistryError as exc:
                st.error(str(exc))
            else:
                st.session_state[key] = fixture["fixture_revision_id"]
                st.rerun()
        return None
    st.code(fixture["fixture_revision_id"])
    st.json(fixture["body"])
    st.caption(f"상태: {fixture['lifecycle_status']}")
    if fixture["lifecycle_status"] == "DRAFT":
        with st.expander("READY 전에 초안 내용 수정", expanded=False):
            body = fixture["body"]
            with st.form(f"fixture_edit_{fixture['fixture_revision_id']}"):
                question = st.text_area("재현할 질문", value=body["question"])
                symptom = st.text_area("신고된 증상", value=body["reported_symptom"])
                expected = st.text_area("기대하는 동작", value=body["expected_behavior"])
                checks_text = st.text_area(
                    "typed checks (JSON 배열)",
                    value=json.dumps(body["typed_checks"], ensure_ascii=False, indent=2),
                )
                manual_text = st.text_area(
                    "수동 확인 항목 (한 줄에 하나)",
                    value="\n".join(body.get("manual_checks") or []),
                )
                update = st.form_submit_button("초안 수정 저장")
            if update:
                try:
                    parsed_checks = json.loads(checks_text)
                    if not isinstance(parsed_checks, list):
                        raise ValueError("typed checks는 JSON 배열이어야 합니다")
                    registry.update_fixture_revision(
                        fixture["fixture_revision_id"],
                        question=question,
                        reported_symptom=symptom,
                        expected_behavior=expected,
                        typed_checks=parsed_checks,
                        manual_checks=[
                            line.strip()
                            for line in manual_text.splitlines()
                            if line.strip()
                        ],
                    )
                except (json.JSONDecodeError, ValueError, MonitoringRegistryError) as exc:
                    st.error(f"Fixture 초안을 수정하지 못했습니다: {exc}")
                else:
                    st.rerun()
        if st.button(
            "Fixture READY로 고정", key=f"fixture_ready_{fixture['fixture_revision_id']}"
        ):
            try:
                registry.mark_fixture_ready(fixture["fixture_revision_id"])
            except MonitoringRegistryError as exc:
                st.error(str(exc))
            else:
                st.rerun()
    elif st.button(
        "내용을 바꿀 새 Fixture revision 만들기",
        key=f"fixture_revise_{fixture['fixture_revision_id']}",
    ):
        try:
            successor = registry.revise_fixture(fixture["fixture_revision_id"])
        except MonitoringRegistryError as exc:
            st.error(str(exc))
        else:
            st.session_state[key] = successor["fixture_revision_id"]
            st.rerun()
    return fixture


def _snapshot_record(
    registry: MonitoringRegistry, revision_id: str
) -> tuple[dict[str, Any] | None, str]:
    root = load_operator_api_config().artifact_root.resolve()
    snapshot_root = root / "fixed-snapshots"
    availability = fixed_snapshot.derive_fixed_snapshot_availability(snapshot_root, revision_id).value
    if availability != "AVAILABLE":
        return None, availability
    opened = fixed_snapshot.open_fixed_snapshot(snapshot_root, revision_id)
    try:
        record = registry.get_fixed_snapshot(revision_id)
    except MonitoringRecordNotFound:
        record = _service(registry).register_fixed_snapshot(opened)
    return record, availability


def _render_case(
    client: MonitoringAdminClient,
    registry: MonitoringRegistry,
    local_issue: Mapping[str, Any],
    fixture: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    st.subheader("2. FixedSnapshot과 ReconstructionLineage")
    st.write(
        "FixedSnapshot은 재현에 사용한 검색 DB와 벡터를 자체 포함합니다. ReconstructionLineage는 운영 자료가 로컬 근거와 정확히 같았는지 설명하며, 정확히 일치하는 항목은 자동 처리하고 차이·대체·누락만 운영자가 기록합니다."
    )
    issue_id = str(local_issue["issue_id"])
    case_key = _case_state_key(issue_id)
    snapshot_widget_key = f"snapshot_id_{issue_id}"
    snapshot_revision_key = _snapshot_revision_state_key(issue_id)
    snapshot_feedback_key = _snapshot_feedback_state_key(issue_id)
    revisions = registry.list_case_revisions(str(local_issue["issue_id"]))
    revision_ids = [str(item["case_revision_id"]) for item in revisions]
    case_id = st.session_state.get(case_key)
    if case_id not in revision_ids:
        current_contract = local_issue.get("current_case_contract_id")
        case_id = None
        if current_contract:
            try:
                case_id = registry.get_case_by_contract(
                    str(current_contract)
                )["case_revision_id"]
            except MonitoringRecordNotFound:
                case_id = None
        if case_id not in revision_ids:
            case_id = revision_ids[-1] if revision_ids else None
        if case_id:
            st.session_state[case_key] = case_id
    if revisions:
        selected_case_id = st.selectbox(
            "Case revision",
            revision_ids,
            index=revision_ids.index(str(case_id)),
            key=f"case_select_{local_issue['issue_id']}_{len(revisions)}",
            format_func=lambda value: next(
                f"{item['lifecycle_status']} · {value[:16]}"
                for item in revisions
                if item["case_revision_id"] == value
            ),
        )
        case_id = str(selected_case_id)
        st.session_state[case_key] = case_id
    case = registry.get_case_revision(str(case_id)) if case_id else None
    remote_issue_id = str(local_issue["source_receipt_id"]).removeprefix(
        "supabase:"
    )
    seed_key = _seed_key(remote_issue_id)
    seed = st.session_state.get(seed_key)
    if not isinstance(seed, Mapping):
        try:
            _load_raw_report(client, remote_issue_id)
        except OperatorApiError as exc:
            st.error(_error_message(exc))
        seed = st.session_state.get(seed_key)
    snapshot_feedback = st.session_state.get(snapshot_feedback_key)
    with st.expander(
        "신고 근거로 Snapshot 범위 준비",
        expanded=seed is None or isinstance(snapshot_feedback, Mapping),
    ):
        st.write(
            "신고 원문은 선택 시 자동으로 열람되며 감사기록이 남습니다. 전체 원문을 "
            "복제하지 않고 질문과 안전한 문서 ID·hash·필터만 현재 세션에서 사용합니다."
        )
        st.info(
            "자동 제안은 의미가 비슷한 문서를 임의로 고르는 기능이 아닙니다. "
            "신고 당시 실제 사용한 문서와, 당시 기록된 대상·증권사·날짜·유형·파일 "
            "필터에 정확히 맞는 활성 문서를 제안합니다. 아래에서 사람이 제목과 "
            "문서 정보를 확인해 범위를 보완할 수 있습니다."
        )
        if isinstance(snapshot_feedback, Mapping):
            feedback_action = str(snapshot_feedback.get("action") or "")
            feedback_count = int(snapshot_feedback.get("count") or 0)
            if feedback_action == "added":
                if feedback_count:
                    st.success(
                        f"문서 {feedback_count}건을 현재 Snapshot 범위에 추가했습니다."
                    )
                else:
                    st.info("선택한 문서는 이미 현재 Snapshot 범위에 포함되어 있습니다.")
            elif feedback_action == "removed":
                st.info(f"문서 {feedback_count}건을 현재 Snapshot 범위에서 제외했습니다.")
            elif feedback_action == "reset":
                st.info("현재 Snapshot 범위를 자동 제안 상태로 되돌렸습니다.")
            st.caption(
                "아직 FixedSnapshot으로 등록되지는 않았습니다. 범위를 확인한 뒤 "
                "아래의 ‘임시 생성·검증 후 FixedSnapshot READY 등록’을 실행하세요."
            )
            st.session_state.pop(snapshot_feedback_key, None)
        seed_body = seed if isinstance(seed, Mapping) else {}
        route_filters = _snapshot_route_filters(seed_body)
        filter_summary = _snapshot_filter_summary(route_filters)
        if filter_summary:
            st.caption(f"이번 신고에서 기록된 제안 조건 · {filter_summary}")
        else:
            st.caption(
                "이번 신고에는 문서 수준 필터가 없습니다. 신고 당시 사용 문서만 "
                "자동 포함하고, 나머지는 운영자가 직접 선택합니다."
            )
        service = _service(registry)
        proposal = None
        if seed_body:
            if seed_body.get("observed_answer"):
                st.caption("신고 당시 답변 — 기대 정답으로 자동 승인하지 않습니다.")
                st.write(seed_body["observed_answer"])

        documents: tuple[fixed_snapshot.ActiveReportDocument, ...] = ()
        try:
            documents = _load_snapshot_documents(
                service,
                data_root=config_module.DATA_ROOT
            )
        except (
            fixed_snapshot.FixedSnapshotError,
            sqlite3.Error,
            OSError,
            ValueError,
        ) as exc:
            st.warning(f"활성 문서 목록을 읽지 못했습니다: {exc}")

        observed_uids = snapshot_observed_report_uids(seed_body)
        if seed_body and documents:
            try:
                proposal = fixed_snapshot.propose_report_scope_from_documents(
                    documents,
                    observed_report_uids=observed_uids,
                    filters=route_filters,
                )
            except (fixed_snapshot.FixedSnapshotError, OSError, ValueError) as exc:
                st.warning(f"자동 범위를 만들지 못했습니다: {exc}")
        proposed_uids = proposal.report_uids if proposal else ()
        filter_matched_uids = (
            proposal.filter_matched_report_uids if proposal else ()
        )
        available_uids = {document.report_uid for document in documents}
        active_observed_uids = tuple(
            uid for uid in observed_uids if uid in available_uids
        )
        missing_observed_uids = tuple(
            uid for uid in observed_uids if uid not in available_uids
        )
        scope_key = _snapshot_scope_state_key(str(local_issue["issue_id"]))
        selected_uids = _reconcile_snapshot_selection(
            documents,
            current_uids=(
                st.session_state.get(scope_key)
                if scope_key in st.session_state
                else None
            ),
            proposed_uids=proposed_uids,
            observed_uids=active_observed_uids,
        )
        st.session_state[scope_key] = list(selected_uids)

        if proposal:
            st.success(
                f"신고 당시 사용 문서 {len(active_observed_uids)}건과 "
                f"같은 조건의 문서 {len(set(filter_matched_uids) - set(observed_uids))}건을 "
                "찾았습니다."
            )
            if proposal.unsupported_filters:
                st.warning(
                    "자동 해석하지 않은 필터: "
                    + ", ".join(proposal.unsupported_filters)
                )
        if missing_observed_uids:
            st.warning(
                f"신고 당시 사용한 문서 {len(missing_observed_uids)}건이 현재 활성 "
                "카탈로그에 없습니다. 아래 검색에서 대응 문서를 선택하고 Case의 "
                "ReconstructionLineage에 대체·누락 사유를 기록하세요."
            )

        observed_set = set(active_observed_uids)
        filter_matched_set = set(filter_matched_uids)
        selected_set = set(selected_uids)
        suggested_only = filter_matched_set - set(observed_uids)
        summary_columns = st.columns(3)
        summary_columns[0].metric("신고 당시 사용", len(active_observed_uids))
        summary_columns[1].metric("같은 조건 제안", len(suggested_only))
        summary_columns[2].metric("현재 선택", len(selected_uids))

        document_by_uid = {
            document.report_uid: document for document in documents
        }
        ordered_uids = tuple(document_by_uid)
        selected_documents = tuple(
            document_by_uid[uid]
            for uid in selected_uids
            if uid in document_by_uid
        )
        st.markdown("#### 현재 Snapshot 범위")
        if selected_documents:
            st.dataframe(
                [
                    _snapshot_document_row(
                        document,
                        reason=_snapshot_inclusion_reason(
                            document.report_uid,
                            observed_uids=observed_set,
                            filter_matched_uids=filter_matched_set,
                        ),
                    )
                    for document in selected_documents
                ],
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "‘신고 당시 사용’ 문서는 재현 근거이므로 제외할 수 없습니다. "
                "제안 문서와 운영자가 추가한 문서는 아래에서 제외할 수 있습니다."
            )
        else:
            st.info("선택된 문서가 없습니다. 아래에서 문서를 검색해 추가하세요.")

        removable_uids = tuple(
            uid for uid in selected_uids if uid not in observed_set
        )
        remove_key = (
            f"{_SNAPSHOT_STATE_PREFIX}remove_{local_issue['issue_id']}"
        )
        remove_uids = st.multiselect(
            "현재 범위에서 제외할 문서",
            removable_uids,
            key=remove_key,
            format_func=lambda uid: _snapshot_document_label(
                document_by_uid[uid]
            ),
            disabled=not removable_uids,
        )
        action_columns = st.columns(2)
        action_columns[0].button(
            "선택한 문서 제외",
            disabled=not remove_uids,
            on_click=_remove_snapshot_documents,
            kwargs={
                "scope_key": scope_key,
                "remove_key": remove_key,
                "feedback_key": snapshot_feedback_key,
                "ordered_uids": ordered_uids,
                "protected_uids": active_observed_uids,
            },
        )
        reset_uids = _reconcile_snapshot_selection(
            documents,
            current_uids=None,
            proposed_uids=proposed_uids,
            observed_uids=active_observed_uids,
        )
        action_columns[1].button(
            "자동 제안으로 되돌리기",
            disabled=selected_uids == reset_uids,
            on_click=_reset_snapshot_documents,
            kwargs={
                "scope_key": scope_key,
                "remove_key": remove_key,
                "feedback_key": snapshot_feedback_key,
                "reset_uids": reset_uids,
            },
        )

        st.markdown("#### 활성 문서에서 직접 찾기")
        st.caption(
            "제목, 대상, 증권사, 날짜, 유형 또는 파일명으로 검색합니다. "
            "문서 본문은 읽거나 복제하지 않습니다."
        )
        issue_id = str(local_issue["issue_id"])
        query = st.text_input(
            "문서 검색",
            placeholder="예: 삼성전자 2026-08 또는 파일명",
            key=f"{_SNAPSHOT_STATE_PREFIX}search_query_{issue_id}",
        )
        search_columns = st.columns(2)
        report_types = sorted(
            {
                document.report_type
                for document in documents
                if document.report_type
            }
        )
        brokers = sorted(
            {document.broker for document in documents if document.broker}
        )
        selected_report_type = search_columns[0].selectbox(
            "문서 유형",
            ("전체", *report_types),
            key=f"{_SNAPSHOT_STATE_PREFIX}search_type_{issue_id}",
        )
        selected_broker = search_columns[1].selectbox(
            "증권사",
            ("전체", *brokers),
            key=f"{_SNAPSHOT_STATE_PREFIX}search_broker_{issue_id}",
        )
        available_dates = [
            parsed
            for document in documents
            if (parsed := _parse_report_date(document.report_date)) is not None
        ]
        selected_date_range = st.date_input(
            "발간일 범위",
            value=(),
            min_value=min(available_dates) if available_dates else None,
            max_value=max(available_dates) if available_dates else None,
            key=f"{_SNAPSHOT_STATE_PREFIX}search_date_{issue_id}",
            help="시작일·종료일을 지정해 발간일 기준으로 좁힙니다. "
            "비워 두면 날짜 필터를 적용하지 않습니다.",
        )
        report_date_start, report_date_end = _snapshot_date_filter(
            selected_date_range
        )
        unselected_documents = tuple(
            document
            for document in documents
            if document.report_uid not in selected_set
        )
        search_results, total_matches = _search_snapshot_documents(
            unselected_documents,
            query=query,
            report_type=(
                None if selected_report_type == "전체" else selected_report_type
            ),
            broker=None if selected_broker == "전체" else selected_broker,
            report_date_start=report_date_start,
            report_date_end=report_date_end,
        )
        if total_matches > len(search_results):
            st.caption(
                f"조건에 맞는 {total_matches}건 중 최근 "
                f"{len(search_results)}건만 표시합니다. 검색어 또는 필터를 더 좁히세요."
            )
        else:
            st.caption(f"추가할 수 있는 문서 {total_matches}건")
        add_key = f"{_SNAPSHOT_STATE_PREFIX}add_{issue_id}"
        add_uids = st.multiselect(
            "검색 결과에서 추가할 문서",
            [document.report_uid for document in search_results],
            key=add_key,
            format_func=lambda uid: _snapshot_document_label(
                document_by_uid[uid]
            ),
            disabled=not search_results,
        )
        st.button(
            "선택한 문서 추가",
            disabled=not add_uids,
            on_click=_add_snapshot_documents,
            kwargs={
                "scope_key": scope_key,
                "add_key": add_key,
                "feedback_key": snapshot_feedback_key,
                "ordered_uids": ordered_uids,
            },
        )

        with st.expander("제안 기준과 문서 식별자", expanded=False):
            st.json(
                {
                    "제안 방식": (
                        "신고 당시 사용 문서 + 기록된 문서 수준 필터의 정확 일치"
                    ),
                    "신고 당시 필터": route_filters,
                    "선택한 report_uid": list(selected_uids),
                    "현재 카탈로그에 없는 신고 report_uid": list(
                        missing_observed_uids
                    ),
                }
            )

        if st.button(
            "임시 생성·검증 후 FixedSnapshot READY 등록",
            key=f"snapshot_create_{local_issue['issue_id']}",
            disabled=not selected_uids,
        ):
            try:
                created, _ = service.create_fixed_snapshot_for_case(
                    data_root=config_module.DATA_ROOT,
                    report_uids=list(selected_uids),
                )
            except (
                fixed_snapshot.FixedSnapshotError,
                MonitoringRegistryError,
                MonitoringServiceError,
                OSError,
                ValueError,
            ) as exc:
                st.error(f"FixedSnapshot을 등록하지 못했습니다: {exc}")
            else:
                st.session_state[snapshot_revision_key] = created.revision_id
                st.session_state[snapshot_widget_key] = created.revision_id
                st.success(f"READY Snapshot: {created.revision_id}")
    case_snapshot_id = str(
        (case.get("fixed_snapshot_revision_id") if case else "") or ""
    ).strip()
    remembered_snapshot_id = str(
        st.session_state.get(snapshot_revision_key) or ""
    ).strip()
    matching_snapshot_id = _snapshot_revision_for_scope(
        registry.list_fixed_snapshots(), selected_uids
    )
    recovered_snapshot_id = (
        case_snapshot_id or remembered_snapshot_id or matching_snapshot_id or ""
    )
    if recovered_snapshot_id and not remembered_snapshot_id:
        st.session_state[snapshot_revision_key] = recovered_snapshot_id
    if not str(st.session_state.get(snapshot_widget_key) or "").strip():
        st.session_state[snapshot_widget_key] = recovered_snapshot_id
    revision_id = st.text_input(
        "FixedSnapshot revision ID",
        key=f"snapshot_id_{local_issue['issue_id']}",
        help="관리 루트의 fixed-snapshots 아래에 이미 검증·게시된 64자리 revision ID입니다.",
    ).strip()
    if revision_id:
        st.session_state[snapshot_revision_key] = revision_id
        st.markdown("**현재 선택된 FixedSnapshot revision ID**")
        st.code(revision_id)
        st.caption(
            "Baseline과 Candidate를 비교할 때 이 동일한 Snapshot ID를 사용합니다."
        )
    snapshot_record = None
    availability = "선택 전"
    if revision_id:
        try:
            snapshot_record, availability = _snapshot_record(registry, revision_id)
        except (fixed_snapshot.FixedSnapshotError, ValueError, OSError, MonitoringRegistryError) as exc:
            availability = f"확인 실패: {exc}"
    st.caption(f"로컬 자산 상태: {availability}")

    lineage_template: dict[str, Any] | None = None
    if snapshot_record:
        try:
            lineage_template = _service(registry).build_reconstruction_lineage(
                seed if isinstance(seed, Mapping) else {},
                fixed_snapshot_revision_id=revision_id,
            )
        except (MonitoringRegistryError, MonitoringServiceError, OSError) as exc:
            st.warning(f"자료 대응을 계산하지 못했습니다: {exc}")
    if lineage_template:
        exact_count = int(lineage_template.get("exact_count") or 0)
        exception_count = len(lineage_template.get("exceptions") or [])
        st.caption(
            f"자동 hash 일치 {exact_count}건은 접었습니다 · 확인할 예외 {exception_count}건"
        )
        if exception_count:
            st.warning(
                "내용 차이·대체·누락 예외는 confirmed=true와 필요한 사유를 기록해야 Case를 READY로 고정할 수 있습니다."
            )
    if case is None and fixture and fixture["lifecycle_status"] == "READY" and snapshot_record:
        with st.form(f"case_create_{local_issue['issue_id']}"):
            fixed_clock = st.text_input("고정 시각(선택)", placeholder="2026-08-29T00:00:00Z")
            evaluator_note = st.text_input("평가 방식", value="운영자 정성 평가 + Fixture typed checks")
            exceptions_text = st.text_area(
                "Lineage 예외(JSON 배열, 선택)",
                value=json.dumps(
                    (lineage_template or {}).get("exceptions") or [],
                    ensure_ascii=False,
                    indent=2,
                ),
                help='차이가 있을 때만 예: [{"kind":"MISSING","reason":"운영 당시 자료 없음","confirmed":true}]',
            )
            manual_basis = (lineage_template or {}).get("basis") == "OPERATOR_DEFINED"
            operator_scope_confirmed = st.checkbox(
                "신고 진단 자료가 없어 선택한 문서 범위를 직접 확인했습니다",
                value=False,
                disabled=not manual_basis,
            )
            operator_scope_reason = st.text_input(
                "직접 범위 확인 사유",
                disabled=not manual_basis,
                placeholder="어떤 정보로 이 문서 범위를 선택했는지 기록하세요.",
            )
            create_case = st.form_submit_button("재현 케이스 초안 만들기")
        if create_case:
            try:
                exceptions = json.loads(exceptions_text)
                if not isinstance(exceptions, list):
                    raise ValueError("Lineage 예외는 JSON 배열이어야 합니다")
                lineage = dict(lineage_template or {})
                lineage["exceptions"] = exceptions
                if lineage.get("basis") == "OPERATOR_DEFINED":
                    lineage["operator_scope_confirmed"] = operator_scope_confirmed
                    lineage["operator_scope_reason"] = operator_scope_reason
                case = registry.create_case_revision(
                    issue_id=str(local_issue["issue_id"]),
                    fixture_revision_id=str(fixture["fixture_revision_id"]),
                    fixed_snapshot_revision_id=revision_id,
                    fixed_clock=fixed_clock or None,
                    evaluator={"method": evaluator_note},
                    reconstruction_lineage=lineage,
                )
            except (json.JSONDecodeError, ValueError, MonitoringRegistryError) as exc:
                st.error(f"케이스를 만들지 못했습니다: {exc}")
            else:
                st.session_state[case_key] = case["case_revision_id"]
                st.rerun()
    if case:
        st.code(case["case_revision_id"])
        st.caption(f"상태: {case['lifecycle_status']}")
        st.json(case["reconstruction_lineage"])
        if case["lifecycle_status"] == "DRAFT":
            case_lineage = dict(case["reconstruction_lineage"])
            operator_defined_scope = (
                str(case_lineage.get("basis") or "").upper()
                == "OPERATOR_DEFINED"
            )
            scope_confirmed = True
            scope_reason = ""
            if operator_defined_scope:
                st.warning(
                    "신고 진단에 재현 가능한 문서 식별자가 없어 운영자가 선택한 "
                    "Snapshot 범위를 사용합니다. READY로 고정하기 전에 아래 범위와 "
                    "확인 사유를 검토하세요."
                )
                scope_confirmed = st.checkbox(
                    "선택한 문서 범위를 직접 확인했습니다",
                    value=case_lineage.get("operator_scope_confirmed") is True,
                )
                scope_reason = st.text_input(
                    "직접 선택 범위 확인 사유",
                    value=str(
                        case_lineage.get("operator_scope_reason")
                        or "신고 진단에 문서 식별자가 없어 제목·대상·증권사·날짜를 "
                        "확인해 Snapshot 범위를 선택함"
                    ),
                ).strip()
                if not scope_confirmed:
                    st.caption(
                        "문서 범위를 확인하면 Case READY 버튼이 활성화됩니다."
                    )
                elif not scope_reason:
                    st.caption("확인 사유를 입력해야 Case를 READY로 고정할 수 있습니다.")
            with st.expander("READY 전에 Case 초안 수정", expanded=False):
                with st.form(f"case_edit_{case['case_revision_id']}"):
                    draft_clock = st.text_input(
                        "고정 시각",
                        value=str(case.get("fixed_clock") or ""),
                    )
                    evaluator_text = st.text_area(
                        "evaluator (JSON)",
                        value=json.dumps(
                            case["evaluator"], ensure_ascii=False, indent=2
                        ),
                    )
                    lineage_text = st.text_area(
                        "ReconstructionLineage (JSON)",
                        value=json.dumps(
                            case["reconstruction_lineage"],
                            ensure_ascii=False,
                            indent=2,
                        ),
                    )
                    update_case = st.form_submit_button("Case 초안 수정 저장")
                if update_case:
                    try:
                        evaluator_body = json.loads(evaluator_text)
                        lineage_body = json.loads(lineage_text)
                        if not isinstance(evaluator_body, dict) or not isinstance(
                            lineage_body, dict
                        ):
                            raise ValueError(
                                "evaluator와 Lineage는 JSON 객체여야 합니다"
                            )
                        registry.update_case_revision(
                            case["case_revision_id"],
                            fixture_revision_id=(
                                fixture["fixture_revision_id"]
                                if fixture
                                else case["fixture_revision_id"]
                            ),
                            fixed_snapshot_revision_id=(
                                revision_id
                                or case["fixed_snapshot_revision_id"]
                            ),
                            fixed_clock=draft_clock or None,
                            evaluator=evaluator_body,
                            reconstruction_lineage=lineage_body,
                        )
                    except (
                        json.JSONDecodeError,
                        ValueError,
                        MonitoringRegistryError,
                    ) as exc:
                        st.error(f"Case 초안을 수정하지 못했습니다: {exc}")
                    else:
                        st.rerun()
            ready_disabled = operator_defined_scope and not (
                scope_confirmed and scope_reason
            )
            if st.button(
                "Case READY로 고정",
                key=f"case_ready_{case['case_revision_id']}",
                disabled=ready_disabled,
            ):
                try:
                    if operator_defined_scope:
                        case_lineage["operator_scope_confirmed"] = True
                        case_lineage["operator_scope_reason"] = scope_reason
                        registry.update_case_revision(
                            case["case_revision_id"],
                            reconstruction_lineage=case_lineage,
                        )
                    _service(registry).mark_case_ready(case["case_revision_id"])
                except (MonitoringRegistryError, MonitoringServiceError) as exc:
                    st.error(str(exc))
                else:
                    st.rerun()
        elif (
            fixture
            and fixture["lifecycle_status"] == "READY"
            and snapshot_record
            and st.button(
                "조건을 바꿀 새 Case revision 만들기",
                key=f"case_revise_{case['case_revision_id']}",
            )
        ):
            try:
                successor = registry.revise_case(
                    case["case_revision_id"],
                    fixture_revision_id=fixture["fixture_revision_id"],
                    fixed_snapshot_revision_id=revision_id,
                    reconstruction_lineage=(
                        lineage_template
                        or case["reconstruction_lineage"]
                    ),
                )
            except MonitoringRegistryError as exc:
                st.error(str(exc))
            else:
                st.session_state[case_key] = successor["case_revision_id"]
                st.rerun()
    return case


def _descriptor_from_record(root: Path, record: Mapping[str, Any]) -> release_assets.ReleaseDescriptor:
    manifest = record["manifest"]
    manifest_version = int(record.get("manifest_version") or 1)
    return release_assets.ReleaseDescriptor(
        release_manifest_id=str(record["release_manifest_id"]),
        app_version=str(record["app_version"]),
        git_revision=str(manifest.get("git_revision") or "unknown"),
        build_digest=str(manifest.get("build_digest") or record["runtime_bundle_digest"]),
        runtime_bundle_digest=str(record["runtime_bundle_digest"]),
        runtime_profile_digest=str(
            manifest.get("runtime_profile_digest")
            or (record["runtime_bundle_digest"] if manifest_version == 1 else "")
        ),
        runner_contract_version=int(manifest.get("runner_contract_version") or 1),
        snapshot_reader_contract_version=int(manifest.get("snapshot_reader_contract_version") or 1),
        path=(root / str(record["bundle_relpath"])).resolve(),
        manifest_version=manifest_version,
    )


def _execute_new_run(
    client: MonitoringAdminClient,
    registry: MonitoringRegistry,
    *,
    remote_issue_id: str,
    issue: Mapping[str, Any],
    case: Mapping[str, Any],
    release_manifest_id: str,
    side: str,
) -> dict[str, Any]:
    runner_environment = (
        {"OPENROUTER_API_KEY": config_module.OPENROUTER_API_KEY}
        if config_module.OPENROUTER_API_KEY
        else None
    )

    status_box = st.status("실행 준비 중", expanded=True)
    progress_bar = st.progress(
        0,
        text="원격 실행 계약과 등록 Release를 동기화하고 있습니다.",
    )

    def synchronize_run_lifecycle(_run: Mapping[str, Any]) -> None:
        _synchronize_control_projection(
            client,
            registry,
            remote_issue_id=remote_issue_id,
            local_issue=registry.get_issue(str(issue["issue_id"])),
        )

    def render_run_progress(event: Mapping[str, Any]) -> None:
        step = int(event.get("step") or 0)
        total_steps = int(event.get("total_steps") or 1)
        stage = str(event.get("stage") or "RUNNING")
        message = str(event.get("message") or "실행 중입니다.")
        run_id = str(event.get("run_id") or "")
        suffix = f" · Run {run_id}" if run_id else ""
        progress_bar.progress(
            min(step / total_steps, 1.0),
            text=f"단계 {step}/{total_steps} · {message}",
        )
        state = "error" if stage in {"FAILED", "CANCELLED", "INTERRUPTED"} else "running"
        status_box.update(
            label=f"{message}{suffix}",
            state=state,
            expanded=state == "error",
        )

    try:
        _synchronize_control_projection(
            client,
            registry,
            remote_issue_id=remote_issue_id,
            local_issue=issue,
            include_release_manifest_ids=(release_manifest_id,),
        )
        run = _service(registry).execute_run(
            issue_id=str(issue["issue_id"]),
            case_contract_id=str(case["case_contract_id"]),
            release_manifest_id=release_manifest_id,
            side=side,
            runtime_profile=_default_release_runtime_profile(),
            extra_environment=runner_environment,
            lifecycle_callback=synchronize_run_lifecycle,
            progress_callback=render_run_progress,
        )
    except BaseException:
        status_box.update(
            label="실행을 완료하지 못했습니다. 아래 오류와 저장된 Run 결과를 확인하세요.",
            state="error",
            expanded=True,
        )
        raise
    if run.get("projection_sync_warnings"):
        st.warning(
            "로컬 terminal 결과는 저장됐지만 Supabase projection 동기화가 "
            "완료되지 않았습니다. 다음 작업 전에 설정의 drift를 확인하고 "
            "재동기화하세요."
        )
    execution_status = str(run.get("execution_status") or "UNKNOWN")
    validity = str(run.get("validity") or "")
    if execution_status != "SUCCEEDED":
        status_box.update(
            label=f"실행 실패 · Run {run['run_id']}",
            state="error",
            expanded=True,
        )
    elif validity != "VALID":
        status_box.update(
            label=f"실행 완료 · 판단 제외 · Run {run['run_id']}",
            state="complete",
            expanded=True,
        )
    else:
        status_box.update(
            label=f"실행 완료 · Run {run['run_id']}",
            state="complete",
            expanded=False,
        )
    return run


def _release_label(release: Mapping[str, Any]) -> str:
    manifest = release.get("manifest")
    manifest_body = manifest if isinstance(manifest, Mapping) else {}
    revision = str(manifest_body.get("git_revision") or "unknown")
    return (
        f"v{release['app_version']} · "
        f"{revision[:12]} · "
        f"{release['lifecycle_status']}"
    )


def _reported_release_id(
    registry: MonitoringRegistry, issue: Mapping[str, Any]
) -> str | None:
    releases = registry.list_release_manifests()
    reported = str(issue.get("reported_release_id") or "")
    if any(item["release_manifest_id"] == reported for item in releases):
        return reported
    version = reported.removeprefix("release-").removeprefix("v")
    if not version:
        version = str((issue.get("summary") or {}).get("app_version") or "")
    match = registry.find_release_by_version(version)
    return str(match["release_manifest_id"]) if match else None


def _render_run_action(
    client: MonitoringAdminClient,
    registry: MonitoringRegistry,
    local_issue: Mapping[str, Any],
    case: Mapping[str, Any] | None,
) -> None:
    st.subheader("3. 신고 버전 Baseline 실행")
    st.write(
        "READY 케이스가 되면 신고가 발생한 배포본의 등록된 bytes를 같은 Fixture·FixedSnapshot으로 실행합니다. 횟수는 고정하지 않고 결과가 애매할 때 운영자가 필요한 만큼 다시 실행합니다."
    )
    if not case or case["lifecycle_status"] != "READY":
        st.info("먼저 Fixture, FixedSnapshot, Lineage를 확인하고 Case를 READY로 고정하세요.")
        return
    releases = registry.list_release_manifests()
    if not releases:
        st.warning(
            "등록된 runnable release bundle이 없습니다. 설정의 ‘Release 등록’에서 "
            "먼저 신고 버전을 등록하세요."
        )
        return
    release_ids = [str(item["release_manifest_id"]) for item in releases]
    reported_id = _reported_release_id(registry, local_issue)
    if reported_id is None:
        st.error(
            f"신고 버전 {local_issue['reported_release_id']}에 대응하는 등록 Release가 없습니다."
        )
        return
    release_id = st.selectbox(
        "신고 버전 Release",
        release_ids,
        index=release_ids.index(reported_id),
        key=f"baseline_release_{local_issue['issue_id']}",
        format_func=lambda value: _release_label(
            next(
                item
                for item in releases
                if item["release_manifest_id"] == value
            )
        ),
        disabled=True,
    )
    if st.button("Baseline 새 Run 실행", type="primary"):
        try:
            run = _execute_new_run(
                client,
                registry,
                remote_issue_id=str(
                    local_issue["source_receipt_id"]
                ).removeprefix("supabase:"),
                issue=local_issue,
                case=case,
                release_manifest_id=release_id,
                side="BASELINE",
            )
        except (MonitoringRegistryError, MonitoringServiceError, release_assets.ReleaseAssetError, OSError, ValueError) as exc:
            st.error(f"Baseline 실행 실패: {exc}")
        else:
            if run["execution_status"] != "SUCCEEDED":
                st.error(
                    f"Baseline 실행이 실패 상태로 저장됐습니다: {run['run_id']}"
                )
            elif run["validity"] != "VALID":
                st.warning(
                    f"Baseline 실행은 끝났지만 판단에서 제외됐습니다: {run['run_id']}"
                )
            else:
                st.success(f"새 Run을 저장했습니다: {run['run_id']}")

    recent_runs = registry.list_runs(
        issue_id=str(local_issue["issue_id"]),
        case_contract_id=str(case["case_contract_id"]),
        side="BASELINE",
    )[-5:]
    if recent_runs:
        st.markdown("**최근 Baseline 실행 및 결과**")
        for index, row in enumerate(reversed(recent_runs)):
            try:
                detail = registry.get_run(str(row["run_id"]))
            except MonitoringRegistryError as exc:
                st.warning(f"Run 결과 확인 실패: {exc}")
                continue
            with st.expander(_run_option(detail), expanded=index == 0):
                _render_run_detail(detail, title="Baseline 실행 결과")
    else:
        st.caption("아직 이 재현 케이스의 Baseline 실행 기록이 없습니다.")


def _render_reproduction(client: MonitoringAdminClient, registry: MonitoringRegistry) -> None:
    st.header("재현 케이스")
    st.write(
        "한 번 만든 재현 조건을 버전이 바뀌어도 그대로 다시 쓰는 곳입니다. Fixture와 Case는 READY 이후 불변이며, 내용을 바꾸려면 새 revision을 만듭니다."
    )
    remote_issue = st.session_state.get("monitoring_selected_issue_detail")
    issue_id = st.session_state.get(_ISSUE_KEY)
    if (
        not issue_id
        and isinstance(remote_issue, Mapping)
        and remote_issue.get("issue_id")
    ):
        issue_id = str(remote_issue["issue_id"])
        st.session_state[_ISSUE_KEY] = issue_id
    if not issue_id:
        st.info("작업함에서 먼저 신고를 선택하세요.")
        return
    # Detail is fetched again by the caller's authenticated client in render page.
    if not isinstance(remote_issue, Mapping) or str(remote_issue.get("issue_id")) != str(issue_id):
        st.info("작업함에서 신고를 한 번 열어 요약을 확인하세요.")
        return
    local_issue = _ensure_local_issue(registry, remote_issue)
    _synchronize_control_projection(
        client,
        registry,
        remote_issue_id=str(remote_issue["issue_id"]),
        local_issue=local_issue,
    )
    _render_progress(_progress(registry, local_issue))
    fixture = _render_fixture(registry, local_issue)
    case = _render_case(client, registry, local_issue, fixture)
    _render_run_action(client, registry, local_issue, case)


def _run_option(run: Mapping[str, Any]) -> str:
    status = str(run["execution_status"])
    validity = str(run.get("validity") or "")
    return (
        f"{run['run_id']} · {run['release_manifest_id']} · "
        f"{_RUN_STATUS_LABELS.get(status, status)} · "
        f"{_RUN_VALIDITY_LABELS.get(validity, '판정 전')}"
    )


def _render_run_detail(run: Mapping[str, Any], *, title: str) -> None:
    st.subheader(title)
    artifact = run.get("artifact") or {}
    st.caption(f"Run: {run['run_id']} · Release: {run['release_manifest_id']}")
    status = str(run.get("execution_status") or "UNKNOWN")
    validity = str(run.get("validity") or "")
    status_col, validity_col = st.columns(2)
    status_col.metric("실행 상태", _RUN_STATUS_LABELS.get(status, status))
    validity_col.metric(
        "결과 판정", _RUN_VALIDITY_LABELS.get(validity, "판정 전")
    )
    st.caption(
        "대기 {queued} · 시작 {started} · 완료 {completed}".format(
            queued=run.get("queued_at") or "-",
            started=run.get("started_at") or "-",
            completed=run.get("completed_at") or "-",
        )
    )
    if status in {"QUEUED", "RUNNING"}:
        st.info(
            "실행이 진행 중입니다. 이 페이지를 다시 열어도 저장된 Run 상태에서 이어서 확인할 수 있습니다."
        )
        return
    if status != "SUCCEEDED":
        runner_result = artifact.get("runner_result")
        runner_error = runner_result if isinstance(runner_result, Mapping) else {}
        error_type = artifact.get("error_type") or runner_error.get("error_type")
        error_message = (
            artifact.get("error_message")
            or runner_error.get("error_message")
            or runner_error.get("stderr")
            or runner_error.get("runner_status")
            or "구체적인 오류 메시지가 저장되지 않았습니다."
        )
        st.error(
            f"{error_type or status}: {error_message}"
        )
        if artifact:
            with st.expander("저장된 오류 artifact", expanded=False):
                st.json(artifact)
        return
    if validity != "VALID" and artifact.get("invalid_reason"):
        st.warning(f"판단 제외 사유: {artifact['invalid_reason']}")
    qualifier = str(artifact.get("evidence_qualifier") or "EXACT")
    if qualifier == "EXACT":
        st.caption("근거 범위: exact 자료 대응")
    else:
        st.warning(f"근거 범위: {qualifier} — 완전한 exact 재현으로 해석하지 않습니다.")
    if artifact.get("cleanup_warning"):
        st.warning(f"실행 결과는 보존됐지만 정리가 필요합니다: {artifact['cleanup_warning']}")
    st.markdown("**답변**")
    st.write(artifact.get("raw_answer") or "답변 없음")
    st.markdown("**EvidenceRef**")
    st.json(artifact.get("evidence_refs") or [])
    first, second = st.columns(2)
    with first:
        st.markdown("**확인 결과**")
        st.json(artifact.get("check_result") or {})
        st.metric("응답 시간(ms)", artifact.get("latency_ms") or "-")
    with second:
        st.markdown("**실행 프로필**")
        st.json(artifact.get("runtime_profile") or {})
        st.markdown("**경로 요약**")
        st.json(artifact.get("route_summary") or {})


def _runtime_profile_diff(
    baseline_profile: Mapping[str, Any], candidate_profile: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "baseline": baseline_profile.get(key),
            "candidate": candidate_profile.get(key),
        }
        for key in sorted(set(baseline_profile) | set(candidate_profile))
        if baseline_profile.get(key) != candidate_profile.get(key)
    }


def _render_saved_comparison(
    registry: MonitoringRegistry, comparison: Mapping[str, Any]
) -> None:
    try:
        baseline = [
            registry.get_run(str(run_id))
            for run_id in comparison["baseline_run_ids"]
        ]
        candidate = [
            registry.get_run(str(run_id))
            for run_id in comparison["candidate_run_ids"]
        ]
        summary = _service(registry).comparison_view(
            baseline_run_ids=[run["run_id"] for run in baseline],
            candidate_run_ids=[run["run_id"] for run in candidate],
        )
        case = registry.get_case_by_contract(
            str(comparison["case_contract_id"])
        )
    except (MonitoringRegistryError, MonitoringServiceError) as exc:
        st.warning(f"저장 당시 Run을 다시 열지 못했습니다: {exc}")
        return

    st.caption(
        f"근거 범위 {case.get('evidence_qualifier') or 'UNKNOWN'} · "
        f"Baseline 지연 중앙값 {summary['baseline']['latency_median_ms']:,.1f} ms · "
        f"Candidate 지연 중앙값 {summary['candidate']['latency_median_ms']:,.1f} ms"
    )
    left, right = st.columns(2)
    with left:
        for run in baseline:
            _render_run_detail(run, title="저장된 Baseline Run")
    with right:
        for run in candidate:
            _render_run_detail(run, title="저장된 Candidate Run")
    profile_diff = _runtime_profile_diff(
        baseline[-1]["artifact"].get("runtime_profile") or {},
        candidate[-1]["artifact"].get("runtime_profile") or {},
    )
    st.markdown("**저장된 판단에서 선택한 대표 Run profile 차이**")
    st.json(profile_diff)


def _render_comparison(client: MonitoringAdminClient, registry: MonitoringRegistry) -> None:
    st.header("버전 비교")
    st.write(
        "동일한 Fixture와 FixedSnapshot, 즉 같은 case_contract_id에서 실행된 유효한 결과만 나란히 봅니다. 절대 점수로 자동 결론내리지 않고 답변·근거·검사·속도·프로필을 운영자가 정성적으로 판단합니다."
    )
    remote_issue = st.session_state.get("monitoring_selected_issue_detail")
    if not isinstance(remote_issue, Mapping):
        st.info("작업함에서 먼저 신고를 선택하세요.")
        return
    local_issue = _find_local_issue(registry, remote_issue)
    if local_issue is None or not local_issue.get("current_case_contract_id"):
        st.info("먼저 재현 케이스를 READY로 만들고 Baseline을 실행하세요.")
        return
    _synchronize_control_projection(
        client,
        registry,
        remote_issue_id=str(remote_issue["issue_id"]),
        local_issue=local_issue,
    )
    case = registry.get_case_by_contract(str(local_issue["current_case_contract_id"]))

    st.subheader("Candidate 실행")
    releases = registry.list_release_manifests()
    baseline_release_id = _reported_release_id(registry, local_issue)
    candidate_releases = [
        item
        for item in releases
        if item["release_manifest_id"] != baseline_release_id
    ]
    candidate_release: str | None = None
    if candidate_releases:
        candidate_release = st.selectbox(
            "개선 후보 Release",
            [str(item["release_manifest_id"]) for item in candidate_releases],
            key=f"candidate_release_{local_issue['issue_id']}",
            format_func=lambda value: _release_label(
                next(
                    item
                    for item in candidate_releases
                    if item["release_manifest_id"] == value
                )
            ),
        )
    else:
        st.warning("신고 버전과 다른 Candidate Release를 먼저 등록하세요.")
    if st.button("Candidate 새 Run 실행", disabled=candidate_release is None):
        assert candidate_release is not None
        try:
            run = _execute_new_run(
                client,
                registry,
                remote_issue_id=str(remote_issue["issue_id"]),
                issue=local_issue,
                case=case,
                release_manifest_id=candidate_release,
                side="CANDIDATE",
            )
        except (MonitoringRegistryError, MonitoringServiceError, release_assets.ReleaseAssetError, OSError, ValueError) as exc:
            st.error(f"Candidate 실행 실패: {exc}")
        else:
            if run["execution_status"] != "SUCCEEDED":
                st.error(
                    f"Candidate 실행이 실패 상태로 저장됐습니다: {run['run_id']}"
                )
            elif run["validity"] != "VALID":
                st.warning(
                    f"Candidate 실행은 끝났지만 판단에서 제외됐습니다: {run['run_id']}"
                )
            else:
                st.success(f"Candidate Run을 저장했습니다: {run['run_id']}")
            st.rerun()

    runs = registry.list_runs(
        issue_id=str(local_issue["issue_id"]),
        case_contract_id=str(case["case_contract_id"]),
    )
    valid = [
        registry.get_run(row["run_id"])
        for row in runs
        if row["execution_status"] == "SUCCEEDED" and row["validity"] == "VALID"
    ]
    invalid = [
        row
        for row in runs
        if row["execution_status"] != "SUCCEEDED" or row["validity"] != "VALID"
    ]
    baseline = [row for row in valid if row["side"] == "BASELINE"]
    candidate = [row for row in valid if row["side"] == "CANDIDATE"]

    if invalid:
        with st.expander(
            f"판단에서 제외된 대기·실패·무효 Run {len(invalid)}건",
            expanded=False,
        ):
            for row in invalid:
                try:
                    detail = registry.get_run(str(row["run_id"]))
                except MonitoringRegistryError as exc:
                    st.warning(f"Run 결과 확인 실패: {exc}")
                    continue
                with st.expander(_run_option(detail), expanded=False):
                    _render_run_detail(detail, title="판단 제외 Run")

    if not baseline or not candidate:
        st.info("SUCCEEDED + VALID Baseline과 Candidate가 각각 하나 이상 필요합니다.")
        if baseline:
            st.markdown("**확인 가능한 Baseline 결과**")
            for index, run in enumerate(reversed(baseline[-5:])):
                with st.expander(_run_option(run), expanded=index == 0):
                    _render_run_detail(run, title="Baseline Run")
        if candidate:
            st.markdown("**확인 가능한 Candidate 결과**")
            for index, run in enumerate(reversed(candidate[-5:])):
                with st.expander(_run_option(run), expanded=index == 0):
                    _render_run_detail(run, title="Candidate Run")
        return
    baseline_ids = st.multiselect(
        "비교할 Baseline Run",
        [row["run_id"] for row in baseline],
        default=[baseline[-1]["run_id"]],
        format_func=lambda value: _run_option(
            next(row for row in baseline if row["run_id"] == value)
        ),
    )
    candidate_ids = st.multiselect(
        "비교할 Candidate Run",
        [row["run_id"] for row in candidate],
        default=[candidate[-1]["run_id"]],
        format_func=lambda value: _run_option(
            next(row for row in candidate if row["run_id"] == value)
        ),
    )
    if not baseline_ids or not candidate_ids:
        st.info("양쪽에서 판단에 사용할 Run을 한 건 이상 선택하세요.")
        return
    selected_baseline = [
        next(row for row in baseline if row["run_id"] == run_id)
        for run_id in baseline_ids
    ]
    selected_candidate = [
        next(row for row in candidate if row["run_id"] == run_id)
        for run_id in candidate_ids
    ]
    view = _service(registry).comparison_view(
        baseline_run_ids=baseline_ids,
        candidate_run_ids=candidate_ids,
    )
    left, right = st.columns(2)
    with left:
        st.subheader("이전 버전 · Baseline")
        st.metric("선택 Run", view["baseline"]["valid_run_count"])
        st.caption(
            f"지연 중앙값 {view['baseline']['latency_median_ms']:,.1f} ms · "
            f"범위 {view['baseline']['latency_range_ms'][0]:,.1f}~"
            f"{view['baseline']['latency_range_ms'][1]:,.1f} ms"
        )
        for run in selected_baseline:
            with st.expander(_run_option(run), expanded=len(selected_baseline) == 1):
                _render_run_detail(run, title="Baseline Run")
        if st.button("Baseline 필요 시 다시 실행"):
            try:
                _execute_new_run(
                    client,
                    registry,
                    remote_issue_id=str(remote_issue["issue_id"]),
                    issue=local_issue,
                    case=case,
                    release_manifest_id=str(selected_baseline[-1]["release_manifest_id"]),
                    side="BASELINE",
                )
            except (MonitoringRegistryError, MonitoringServiceError, release_assets.ReleaseAssetError, OSError, ValueError) as exc:
                st.error(f"Baseline 반복 실행 실패: {exc}")
            else:
                st.rerun()
    with right:
        st.subheader("현재/후보 버전 · Candidate")
        st.metric("선택 Run", view["candidate"]["valid_run_count"])
        st.caption(
            f"지연 중앙값 {view['candidate']['latency_median_ms']:,.1f} ms · "
            f"범위 {view['candidate']['latency_range_ms'][0]:,.1f}~"
            f"{view['candidate']['latency_range_ms'][1]:,.1f} ms"
        )
        for run in selected_candidate:
            with st.expander(_run_option(run), expanded=len(selected_candidate) == 1):
                _render_run_detail(run, title="Candidate Run")
        if st.button("Candidate 필요 시 다시 실행"):
            try:
                _execute_new_run(
                    client,
                    registry,
                    remote_issue_id=str(remote_issue["issue_id"]),
                    issue=local_issue,
                    case=case,
                    release_manifest_id=str(selected_candidate[-1]["release_manifest_id"]),
                    side="CANDIDATE",
                )
            except (MonitoringRegistryError, MonitoringServiceError, release_assets.ReleaseAssetError, OSError, ValueError) as exc:
                st.error(f"Candidate 반복 실행 실패: {exc}")
            else:
                st.rerun()

    st.markdown("**선택한 대표 Run의 runtime profile 차이**")
    profile_diff = _runtime_profile_diff(
        selected_baseline[-1]["artifact"].get("runtime_profile") or {},
        selected_candidate[-1]["artifact"].get("runtime_profile") or {},
    )
    if profile_diff:
        st.json(profile_diff)
    else:
        st.caption("표시할 runtime profile 차이가 없습니다.")

    history = registry.list_comparisons(str(local_issue["issue_id"]))
    with st.form(f"comparison_{local_issue['issue_id']}"):
        verdict = st.selectbox(
            "정성 판단",
            ("IMPROVED", "NOT_IMPROVED", "REGRESSED", "INCONCLUSIVE"),
            format_func=lambda value: _PROGRESS_LABELS[value],
        )
        note = st.text_area("판단 근거", placeholder="답변과 EvidenceRef에서 무엇이 달라졌는지 기록하세요.")
        save = st.form_submit_button("불변 Comparison 저장", type="primary")
    if save:
        try:
            registry.create_comparison(
                issue_id=str(local_issue["issue_id"]),
                baseline_run_ids=[str(run_id) for run_id in baseline_ids],
                candidate_run_ids=[str(run_id) for run_id in candidate_ids],
                verdict=verdict,
                note=note,
                actor_user_id=str(st.session_state[_SESSION_KEY].user_id),
                supersedes_comparison_id=(history[-1]["comparison_id"] if history else None),
            )
            _synchronize_control_projection(
                client,
                registry,
                remote_issue_id=str(remote_issue["issue_id"]),
                local_issue=registry.get_issue(str(local_issue["issue_id"])),
            )
        except (MonitoringRegistryError, MonitoringServiceError) as exc:
            st.error(str(exc))
        else:
            st.success("판단을 저장했습니다. 이전 판단은 수정하지 않고 이력이 그대로 남습니다.")
            st.rerun()
    if history:
        with st.expander("판단 변경 이력", expanded=False):
            for item in reversed(history):
                st.markdown(f"**{item['verdict']}** · {item['created_at']}")
                st.write(item["note"])
                st.caption(
                    f"Baseline {len(item['baseline_run_ids'])}건 · "
                    f"Candidate {len(item['candidate_run_ids'])}건"
                )
                st.caption(
                    f"Comparison {item['comparison_id']} · supersedes {item.get('supersedes_comparison_id') or '-'}"
                )
                _render_saved_comparison(registry, item)
                st.divider()


def _asset_warnings(registry: MonitoringRegistry) -> list[str]:
    warnings: list[str] = []
    root = load_operator_api_config().artifact_root.resolve()
    snapshots = registry.list_fixed_snapshots()
    snapshot_ids = {
        str(snapshot["fixed_snapshot_revision_id"]) for snapshot in snapshots
    }
    for snapshot in snapshots:
        try:
            availability = fixed_snapshot.derive_fixed_snapshot_availability(
                root / Path(snapshot["bundle_relpath"]).parent,
                snapshot["fixed_snapshot_revision_id"],
            ).value
        except (MonitoringRegistryError, OSError, ValueError) as exc:
            availability = f"확인 실패: {exc}"
        if availability != "AVAILABLE":
            warnings.append(
                f"FixedSnapshot {snapshot['fixed_snapshot_revision_id']}: {availability}"
            )
    releases = registry.list_release_manifests()
    release_ids = {str(release["release_manifest_id"]) for release in releases}
    for release in releases:
        try:
            state = release_assets.inspect_release(
                _descriptor_from_record(root, release)
            ).value
        except (release_assets.ReleaseAssetError, OSError, ValueError) as exc:
            state = f"확인 실패: {exc}"
        if state != "AVAILABLE":
            label = _release_label(release)
            if state == "LOCAL_MISSING" and int(release["manifest_version"]) == 2:
                warnings.append(
                    f"Release {label}: LOCAL_MISSING — 다음 실행 시 등록된 "
                    "Git commit에서 cache를 자동 재생성합니다."
                )
            else:
                warnings.append(f"Release {label}: {state}")
    for issue in registry.list_issues():
        for run in registry.list_runs(issue_id=issue["issue_id"]):
            if run["execution_status"] in {"QUEUED", "RUNNING"}:
                warnings.append(
                    f"Run {run['run_id']}: 미완료 상태 {run['execution_status']} — "
                    "새 실행 전에 중단으로 복구해야 합니다."
                )
            if not run.get("artifact_relpath"):
                continue
            try:
                artifact = registry.get_run(run["run_id"]).get("artifact") or {}
            except MonitoringRegistryError as exc:
                warnings.append(f"Run {run['run_id']}: artifact 확인 실패: {exc}")
                continue
            if artifact.get("cleanup_warning"):
                warnings.append(
                    f"Run {run['run_id']}: {artifact['cleanup_warning']}"
                )
    for directory, registered_ids, label in (
        (root / "fixed-snapshots", snapshot_ids, "FixedSnapshot"),
        (root / "releases", release_ids, "Release"),
    ):
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            if child.is_dir() and child.name not in registered_ids:
                warnings.append(
                    f"{label} 로컬 bytes가 registry에 등록되지 않았습니다: {child.name}"
                )
    staging_root = root / "staging"
    if staging_root.is_dir():
        for stage in staging_root.iterdir():
            warnings.append(f"완료되지 않은 Release STAGED 정리 필요: {stage.name}")
    workspace_root = root / "workspaces"
    if workspace_root.is_dir():
        for workspace in workspace_root.iterdir():
            warnings.append(f"임시 workspace 정리 필요: {workspace.name}")
    return sorted(set(warnings))


def _incomplete_runs(registry: MonitoringRegistry) -> list[dict[str, Any]]:
    return [
        run
        for issue in registry.list_issues()
        for run in registry.list_runs(issue_id=str(issue["issue_id"]))
        if run["execution_status"] in {"QUEUED", "RUNNING"}
    ]


def _synchronize_recovered_runs(
    client: MonitoringAdminClient,
    registry: MonitoringRegistry,
    recovered_runs: list[Mapping[str, Any]],
) -> list[str]:
    """Mirror recovered terminal states after authentication is available."""

    errors: list[str] = []
    issue_ids = {str(run["issue_id"]) for run in recovered_runs}
    for issue_id in sorted(issue_ids):
        issue = registry.get_issue(issue_id)
        source_receipt_id = str(issue.get("source_receipt_id") or "")
        if not source_receipt_id.startswith("supabase:"):
            continue
        remote_issue_id = source_receipt_id.removeprefix("supabase:")
        try:
            _synchronize_control_projection(
                client,
                registry,
                remote_issue_id=remote_issue_id,
                local_issue=issue,
            )
        except (
            MonitoringRegistryError,
            MonitoringServiceError,
            OperatorApiError,
            OSError,
            ValueError,
        ) as exc:
            errors.append(f"Issue {remote_issue_id}: {exc}")
    return errors


def _control_projection_warning(
    client: MonitoringAdminClient, registry: MonitoringRegistry
) -> str | None:
    remote_issue_id = str(st.session_state.get(_ISSUE_KEY) or "")
    if not remote_issue_id:
        return None
    local_issue = next(
        (
            issue
            for issue in registry.list_issues()
            if issue["source_receipt_id"] == f"supabase:{remote_issue_id}"
        ),
        None,
    )
    if local_issue is None:
        try:
            remote_records = client.list_control_records(remote_issue_id)
        except OperatorApiError as exc:
            return f"Supabase 제어 기록을 확인하지 못했습니다: {exc.code}"
        if remote_records:
            return (
                "Supabase에는 재현 제어 기록이 있지만 로컬 Issue가 없습니다. "
                "로컬 registry와 자산의 exact 복원이 필요합니다."
            )
        return None
    try:
        diff = client.check_control_projection(
            remote_issue_id,
            _control_projection(registry, local_issue),
        )
    except OperatorApiError as exc:
        return f"Supabase 제어 기록을 확인하지 못했습니다: {exc.code}"
    except (MonitoringRegistryError, MonitoringServiceError, OSError, ValueError) as exc:
        return f"로컬 제어 기록을 확인하지 못했습니다: {exc}"
    return _control_drift_message(diff)


def _default_release_runtime_profile() -> dict[str, Any]:
    return {
        "environment": release_assets.snapshot_runtime_profile_environment(
            vars(config_module)
        ),
        "snapshot_reader": {
            "manifest_schema_version": fixed_snapshot.MANIFEST_SCHEMA_VERSION,
        },
    }


def _render_release_registration(registry: MonitoringRegistry) -> None:
    root = load_operator_api_config().artifact_root.resolve()
    st.markdown("**Release 등록**")
    st.caption("현재 Git commit의 실행 코드를 불변 Release로 등록합니다.")
    current_identity: release_assets.GitReleaseIdentity | None = None
    try:
        current_identity = release_assets.inspect_current_project_release(
            settings_module.BASE_DIR
        )
    except (OSError, ValueError, release_assets.ReleaseAssetError) as exc:
        st.warning(
            "Git Release를 준비할 수 없습니다. 추적 중인 Release 파일을 "
            f"commit한 뒤 다시 확인하세요: {exc}"
        )
    with st.form("register_release"):
        st.text_input(
            "app version",
            value=current_identity.app_version if current_identity else "",
            disabled=True,
        )
        st.text_input(
            "Git revision",
            value=current_identity.git_revision if current_identity else "",
            disabled=True,
        )
        register = st.form_submit_button(
            "Release 등록",
            disabled=current_identity is None,
        )
    if register and current_identity is not None:
        try:
            stage = release_assets.prepare_current_project_release_stage(
                root,
                project_root=settings_module.BASE_DIR,
            )
            release_tag = f"v{current_identity.app_version}"
            existing = registry.find_release_by_version(
                current_identity.app_version
            )
            descriptor = release_assets.register_release_stage(
                root,
                str(stage),
                expected_tag_version=release_tag,
                expected_git_revision=current_identity.git_revision,
                existing_release_manifest_id=(
                    str(existing["release_manifest_id"]) if existing else None
                ),
                existing_git_revision=(
                    str(existing["manifest"].get("git_revision") or "")
                    if existing
                    else None
                ),
                existing_manifest_version=(
                    int(existing["manifest_version"]) if existing else None
                ),
            )
            _service(registry).register_release(
                descriptor, release_tag=release_tag
            )
        except (
            MonitoringRegistryError,
            MonitoringServiceError,
            OSError,
            ValueError,
            release_assets.ReleaseAssetError,
        ) as exc:
            st.error(f"Release 등록에 실패했습니다: {exc}")
        else:
            st.success(
                f"등록 완료 v{descriptor.app_version} · "
                f"{descriptor.git_revision[:12]}"
            )
            st.rerun()

    releases = registry.list_release_manifests()
    if releases:
        with st.expander(f"등록된 Release {len(releases)}개", expanded=False):
            for release in releases:
                try:
                    availability = release_assets.inspect_release(
                        _descriptor_from_record(root, release)
                    ).value
                except (release_assets.ReleaseAssetError, OSError, ValueError) as exc:
                    availability = f"확인 실패: {exc}"
                if (
                    availability == "LOCAL_MISSING"
                    and int(release["manifest_version"]) == 2
                ):
                    availability += " · 실행 시 Git에서 자동 재생성"
                st.write(f"{_release_label(release)} · {availability}")


def _render_asset_settings(
    client: MonitoringAdminClient, registry: MonitoringRegistry
) -> None:
    with st.expander("설정 · 재현 자산 경고", expanded=False):
        st.write(
            "평소에는 세 작업공간만 사용합니다. 등록 기록은 있지만 로컬 bytes가 "
            "없거나 손상·비호환인 예외만 이곳에 모아 보여줍니다. Git Release의 "
            "누락 cache는 실행 시 등록 commit에서 자동 재생성하며, 손상된 Release와 "
            "FixedSnapshot은 기존 기록을 덮어쓰지 않습니다."
        )
        warnings = _asset_warnings(registry)
        control_warning = _control_projection_warning(client, registry)
        if control_warning:
            warnings.append(control_warning)
            warnings = sorted(set(warnings))
        if not warnings:
            st.success("현재 확인된 재현 자산 경고가 없습니다.")
        else:
            for warning in warnings:
                st.warning(warning)
        incomplete = _incomplete_runs(registry)
        if incomplete:
            st.caption(
                "미완료 Run은 덮어쓰지 않습니다. 명시적으로 복구하면 새 artifact에 "
                "중단 사유를 남기고 판단 대상에서 제외합니다."
            )
            recovery_confirmed = st.checkbox(
                "다른 Monitoring 프로세스와 runner가 실행 중이 아님을 확인했습니다.",
                key="monitoring_recovery_no_active_process",
            )
            if st.button(
                "미완료 Run을 INTERRUPTED로 복구",
                disabled=not recovery_confirmed,
            ):
                try:
                    recovered = _service(registry).recover_incomplete_runs(
                        operator_confirmed_no_active_process=True
                    )
                    sync_errors = _synchronize_recovered_runs(
                        client, registry, recovered
                    )
                except (
                    MonitoringRegistryError,
                    MonitoringServiceError,
                    OperatorApiError,
                    OSError,
                    ValueError,
                ) as exc:
                    st.error(f"미완료 Run을 복구하지 못했습니다: {exc}")
                else:
                    if sync_errors:
                        for message in sync_errors:
                            st.error(f"복구 후 제어 기록 동기화 실패: {message}")
                    else:
                        st.success(
                            f"Run {len(recovered)}건을 INTERRUPTED · INVALID로 보존했습니다."
                        )
                        st.rerun()
        _render_release_registration(registry)


def render_operator_monitoring_page() -> None:
    """Render the complete authenticated operator surface."""

    if not operator_surface_enabled():
        st.error("Monitoring은 설정이 완전한 운영환경에서만 사용할 수 있습니다.")
        return
    session = st.session_state.get(_SESSION_KEY)
    if not isinstance(session, OperatorSession) or session.expired:
        _clear_session()
        _render_login()
        return
    client = _client(session)
    try:
        # This first server round trip rechecks active administrator membership.
        issues = client.list_issues(limit=1)
    except (OperatorUnauthorizedError, OperatorForbiddenError) as exc:
        _clear_session()
        st.error(_error_message(exc))
        _render_login()
        return
    except OperatorApiError as exc:
        st.error(_error_message(exc))
        return

    top, action = st.columns([5, 1])
    with top:
        st.title("운영 Monitoring")
        st.caption(f"로그인: {session.email} · 신고 → 재현 → 개선 비교 → 종료")
    with action:
        if st.button("로그아웃"):
            _clear_session()
            st.rerun()
    registry = _registry()
    _render_asset_settings(client, registry)
    workspace = st.segmented_control(
        "운영 작업공간",
        _WORKSPACES,
        default="작업함",
        key=_WORKSPACE_KEY,
        label_visibility="collapsed",
    ) or "작업함"
    try:
        if workspace == "작업함":
            _render_work_inbox(client, registry)
            selected = st.session_state.get(_ISSUE_KEY)
            if selected:
                st.session_state["monitoring_selected_issue_detail"] = client.get_issue(str(selected))
        elif workspace == "재현 케이스":
            selected = st.session_state.get(_ISSUE_KEY)
            if selected:
                st.session_state["monitoring_selected_issue_detail"] = client.get_issue(str(selected))
            _render_reproduction(client, registry)
        else:
            selected = st.session_state.get(_ISSUE_KEY)
            if selected:
                st.session_state["monitoring_selected_issue_detail"] = client.get_issue(str(selected))
            _render_comparison(client, registry)
    except (OperatorUnauthorizedError, OperatorForbiddenError) as exc:
        _clear_session()
        st.error(_error_message(exc))
    except OperatorApiError as exc:
        st.error(_error_message(exc))
    except MonitoringRegistryError as exc:
        st.error(f"로컬 재현 기록을 확인하지 못했습니다: {exc}")
    except MonitoringServiceError as exc:
        st.error(f"재현 제어 기록을 확인하지 못했습니다: {exc}")


__all__ = ["operator_surface_enabled", "render_operator_monitoring_page"]
