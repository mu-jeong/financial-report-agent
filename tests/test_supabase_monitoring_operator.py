from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/202608290001_monitoring_admin_control_plane.sql"
FUNCTION = ROOT / "supabase/functions/issue-report-operator/index.ts"
CONFIG = ROOT / "supabase/config.toml"
CONTROL_MIGRATION = (
    ROOT / "supabase/migrations/202608290003_monitoring_control_records.sql"
)
LIFECYCLE_MIGRATION = (
    ROOT / "supabase/migrations/202608300001_expand_monitoring_issue_lifecycle.sql"
)


def test_monitoring_control_plane_keeps_reports_private_and_events_append_only():
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "create table private.monitoring_admins" in sql
    assert "create table private.monitoring_issues" in sql
    assert "create table private.monitoring_issue_events" in sql
    assert "prevent_monitoring_issue_event_mutation_v1" in sql
    assert "before update or delete on private.monitoring_issue_events" in sql
    assert "revoke all on table private.issue_reports from public, anon, authenticated" in sql
    assert "grant select on table private.issue_reports to service_role" not in sql
    assert "grant execute on function public.monitoring_" in sql
    assert "to service_role" in sql
    assert "grant select, insert, update on table private.monitoring_issues" not in sql
    assert "grant select, insert on table private.monitoring_issue_events" not in sql
    assert "receipt_id uuid not null unique references private.issue_reports" not in sql
    assert "summary jsonb not null" in sql
    assert "'raw_available'" in sql
    assert "create trigger create_monitoring_issue_after_ingest_v1" in sql
    assert "after insert on private.issue_reports" in sql
    assert "pre_control_plane_backfill" in sql
    assert "select private.materialize_monitoring_issues_v1(null::uuid)" in sql


def test_summary_and_detail_rpcs_do_not_return_consented_raw_text():
    sql = MIGRATION.read_text(encoding="utf-8")
    summary = sql.split("create or replace function public.monitoring_list_issues_v1", 1)[1]
    summary = summary.split("create or replace function public.monitoring_get_issue_v1", 1)[0]
    detail = sql.split("create or replace function public.monitoring_get_issue_v1", 1)[1]
    detail = detail.split("create or replace function public.monitoring_view_issue_raw_v1", 1)[0]

    for forbidden in (
        "#> '{observed,selected_question}'",
        "#> '{observed,selected_answer}'",
        "#> '{observed,turn_trace}'",
        "report.report -> 'comment'",
    ):
        assert forbidden not in summary
        assert forbidden not in detail


def test_explicit_raw_view_is_consent_filtered_and_audited_atomically():
    sql = MIGRATION.read_text(encoding="utf-8")
    raw = sql.split("create or replace function public.monitoring_view_issue_raw_v1", 1)[1]
    raw = raw.split("create or replace function public.monitoring_transition_issue_v1", 1)[0]

    assert "include_comment" in raw
    assert "include_selected_question" in raw
    assert "include_selected_answer" in raw
    assert "include_previous_turns" in raw
    assert "then v_report -> 'case_diagnostics' else 'null'::jsonb end" in raw
    assert "RAW_VIEWED" in raw
    assert "insert into private.monitoring_issue_events" in raw
    assert "source_ip_hash" not in raw
    assert "installation_id" not in raw


def test_issue_transition_rpc_uses_compare_and_swap_and_preserves_history():
    sql = MIGRATION.read_text(encoding="utf-8")
    transition = sql.split("create or replace function public.monitoring_transition_issue_v1", 1)[1]

    assert "p_expected_record_revision" in transition
    assert "record_revision = p_expected_record_revision" in transition
    assert "'conflict'" in transition
    assert "event_type" in transition
    assert "CLOSED" in transition
    assert "REOPENED" in transition
    assert "delete from private.monitoring_issue_events" not in transition


def test_issue_lifecycle_migration_preserves_legacy_state_and_classifies_outcomes():
    sql = LIFECYCLE_MIGRATION.read_text(encoding="utf-8")
    transition = sql.split(
        "create or replace function public.monitoring_transition_issue_v1", 1
    )[1]

    for state in ("OPEN", "IN_PROGRESS", "CLOSED", "RESOLVED", "NOT_ISSUE"):
        assert state in sql
    assert "state in ('OPEN', 'IN_PROGRESS', 'CLOSED', 'RESOLVED', 'NOT_ISSUE')" in sql
    assert "state in ('OPEN', 'IN_PROGRESS') and closed_at is null" in sql
    assert (
        "state in ('CLOSED', 'RESOLVED', 'NOT_ISSUE') and closed_at is not null"
        in sql
    )
    assert "when p_target_state in ('OPEN', 'IN_PROGRESS') then null" in transition
    assert "else coalesce(v_before.closed_at, clock_timestamp())" in transition
    assert "record_revision = p_expected_record_revision" in transition
    assert "v_before.state = p_target_state" in transition
    assert "delete from private.monitoring_issue_events" not in sql
    assert "when 'OPEN' then 'REOPENED'" in transition
    assert "when 'IN_PROGRESS' then 'IN_PROGRESS'" in transition
    assert "when 'RESOLVED' then 'RESOLVED'" in transition
    assert "when 'NOT_ISSUE' then 'NOT_ISSUE'" in transition
    assert "else 'CLOSED'" not in transition


def test_issue_lifecycle_transition_matrix_is_explicit_and_bounded():
    sql = LIFECYCLE_MIGRATION.read_text(encoding="utf-8")
    transition = sql.split(
        "create or replace function public.monitoring_transition_issue_v1", 1
    )[1]

    assert "v_before.state = 'OPEN' and p_target_state in (" in transition
    assert "v_before.state = 'IN_PROGRESS' and p_target_state in (" in transition
    assert "v_before.state = 'CLOSED' and p_target_state in (" in transition
    assert "v_before.state = 'RESOLVED' and p_target_state in ('OPEN', 'NOT_ISSUE')" in transition
    assert "v_before.state = 'NOT_ISSUE' and p_target_state in ('OPEN', 'RESOLVED')" in transition
    assert "'OPEN', 'IN_PROGRESS', 'RESOLVED', 'NOT_ISSUE'" in transition
    assert "'IN_PROGRESS', 'CLOSED', 'RESOLVED', 'NOT_ISSUE'" not in transition
    assert "'OPEN', 'CLOSED', 'RESOLVED', 'NOT_ISSUE'" not in transition
    assert "'disposition', 'invalid_transition'" in transition
    assert "char_length(btrim(p_reason)) not between 1 and 2000" in transition


def test_operator_exposes_named_issue_lifecycle_actions_without_changing_body_contract():
    source = FUNCTION.read_text(encoding="utf-8")

    for action, state in {
        "start": "IN_PROGRESS",
        "resolve": "RESOLVED",
        "dismiss": "NOT_ISSUE",
        "reopen": "OPEN",
    }.items():
        assert f'{action}: "{state}"' in source
    assert 'close: "CLOSED"' not in source
    assert "ISSUE_STATES.has(state)" in source
    assert "fields.length !== 2" in source
    assert 'fields.includes("expected_record_revision")' in source
    assert 'fields.includes("reason")' in source
    assert "p_expected_record_revision: transition.expectedRevision" in source
    assert "p_reason: transition.reason" in source


def test_operator_edge_function_uses_user_auth_and_server_injected_actor():
    source = FUNCTION.read_text(encoding="utf-8")

    assert 'auth: "user"' in source
    assert "ctx.userClaims" in source
    assert "p_actor_user_id: actorUserId" in source
    assert "monitoring_check_admin_v1" in source
    assert "requestBody.actor" not in source
    logger = source.split("function logFailure", 1)[1].split("function errorResponse", 1)[0]
    assert "authorization" not in logger.lower()
    assert "raw_report" not in logger.lower()
    assert "code: \"unauthorized\"" in source
    assert "fetchWithAuthentication" in source
    assert 'code: "authentication_unavailable"' in source
    assert 'logFailure(requestId, "authentication"' in source
    assert "catch {\n    return json(401" not in source
    assert "code: \"forbidden\"" in source
    assert "code: \"revision_conflict\"" in source


def test_operator_function_requires_gateway_jwt_verification():
    config = CONFIG.read_text(encoding="utf-8")

    assert "[functions.issue-report-operator]" in config
    operator_config = config.split("[functions.issue-report-operator]", 1)[1]
    assert "verify_jwt = true" in operator_config


def test_control_projection_is_private_bounded_metadata_with_append_only_audit():
    sql = CONTROL_MIGRATION.read_text(encoding="utf-8")
    event_table = sql.split(
        "create table private.monitoring_control_record_events", 1
    )[1].split(
        "alter table private.monitoring_control_record_events", 1
    )[0]
    run_transition = sql.split("if p_record_kind = 'RUN' and not (", 1)[1].split(
        "if v_before.record_revision", 1
    )[0]

    assert "create table private.monitoring_control_records" in sql
    assert "create table private.monitoring_control_record_events" in sql
    assert "before update or delete on private.monitoring_control_record_events" in sql
    assert "validate_monitoring_control_payload_v1" in sql
    assert "pg_column_size(p_references) > 4096" in sql
    assert "pg_column_size(p_attributes) > 2048" in sql
    assert "p_expected_record_revision" in sql
    assert "record_revision = p_expected_record_revision" in sql
    assert "'unchanged'" in sql
    assert "'immutable_conflict'" in sql
    assert "p_record_kind = 'RUN' and p_lifecycle_status <> 'QUEUED'" in sql
    assert "if p_record_kind = 'COMPARISON'" in sql
    assert "'MIXED'" not in sql
    assert "'UNCHANGED'" not in sql
    assert "lifecycle_status text not null" in event_table
    assert "availability text check" in event_table
    assert "validity text check" in event_table
    assert "v_after.lifecycle_status, v_after.availability" in sql
    assert "v_after.attributes_json ->> 'validity'" in sql
    assert "p_lifecycle_status = 'RUNNING'" in run_transition
    assert "v_before.lifecycle_status = 'QUEUED'" in run_transition
    assert "'RUNNING', 'FAILED'" not in run_transition
    assert "and p_attributes = v_before.attributes_json\n      )\n    ) then" in run_transition
    assert "p_references = v_before.references_json" in run_transition
    assert "p_attributes - 'validity'" in run_transition
    assert "(p_attributes ->> 'validity') not in ('VALID', 'INVALID')" in sql
    assert "invalid Case control payload" in sql
    assert "invalid Comparison control payload" in sql
    assert "invalid non-artifact availability" in sql
    assert "jsonb_object_length" not in sql
    assert "revoke all on table private.monitoring_control_records from public, anon, authenticated" in sql
    assert "grant execute on function public.monitoring_reconcile_control_record_v1" in sql
    for forbidden in ("artifact_path", "bundle_relpath", "question", "answer", "raw_report"):
        assert forbidden not in sql


def test_control_edge_route_uses_exact_allowlist_and_server_injected_actor():
    source = FUNCTION.read_text(encoding="utf-8")

    assert "function exactControlBody" in source
    assert "CONTROL_STATUSES" in source
    assert "REFERENCE_KEYS" in source
    assert "ATTRIBUTE_KEYS" in source
    assert "requiredReferences" in source
    assert "requiredAttributes" in source
    assert "invalid_run_control_record" in source
    assert 'req.method === "GET" && path.length === 3 && path[2] === "control"' in source
    assert 'req.method === "PUT" && path.length === 3 && path[2] === "control"' in source
    route = source.split('req.method === "PUT" && path.length === 3', 1)[1]
    route = route.split('req.method === "POST" && path.length === 3', 1)[0]
    assert 'p_actor_user_id: actorUserId' in route
    assert "p_expected_record_revision: mutation.expectedRevision" in route
    assert '"immutable_conflict"' in route
    assert "requestBody.actor" not in route
