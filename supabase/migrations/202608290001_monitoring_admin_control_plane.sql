-- Authenticated, single-administrator control plane for issue operations.
-- Anonymous ingest remains isolated in private.issue_reports and is unchanged.

create table private.monitoring_admins (
  user_id uuid primary key references auth.users (id),
  active boolean not null default true,
  created_at timestamptz not null default clock_timestamp(),
  deactivated_at timestamptz,
  constraint monitoring_admins_deactivation_consistent check (
    (active and deactivated_at is null)
    or (not active and deactivated_at is not null)
  )
);

comment on table private.monitoring_admins is
  'Invite-only operators allowed to use the production Monitoring API.';

alter table private.monitoring_admins enable row level security;
alter table private.monitoring_admins force row level security;
revoke all on table private.monitoring_admins from public, anon, authenticated;
grant select, insert, update on table private.monitoring_admins to service_role;

create table private.monitoring_issues (
  issue_id uuid primary key default gen_random_uuid(),
  receipt_id uuid not null unique,
  received_at timestamptz not null,
  summary jsonb not null,
  state text not null default 'OPEN' check (state in ('OPEN', 'CLOSED')),
  record_revision bigint not null default 1 check (record_revision > 0),
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  closed_at timestamptz,
  constraint monitoring_issues_closed_at_consistent check (
    (state = 'OPEN' and closed_at is null)
    or (state = 'CLOSED' and closed_at is not null)
  ),
  constraint monitoring_issues_summary_object check (jsonb_typeof(summary) = 'object')
);

create index monitoring_issues_state_updated_idx
  on private.monitoring_issues (state, updated_at desc, issue_id);

alter table private.monitoring_issues enable row level security;
alter table private.monitoring_issues force row level security;
revoke all on table private.monitoring_issues from public, anon, authenticated;

create table private.monitoring_issue_events (
  event_id uuid primary key default gen_random_uuid(),
  issue_id uuid not null references private.monitoring_issues (issue_id),
  event_type text not null check (
    event_type in ('CREATED', 'RAW_VIEWED', 'CLOSED', 'REOPENED')
  ),
  actor_user_id uuid references auth.users (id),
  reason text,
  from_state text check (from_state is null or from_state in ('OPEN', 'CLOSED')),
  to_state text check (to_state is null or to_state in ('OPEN', 'CLOSED')),
  record_revision bigint not null check (record_revision > 0),
  occurred_at timestamptz not null default clock_timestamp(),
  details jsonb not null default '{}'::jsonb,
  constraint monitoring_issue_events_reason_bound check (
    reason is null or (char_length(btrim(reason)) between 1 and 2000)
  ),
  constraint monitoring_issue_events_details_object check (
    jsonb_typeof(details) = 'object'
  )
);

create index monitoring_issue_events_issue_time_idx
  on private.monitoring_issue_events (issue_id, occurred_at, event_id);

alter table private.monitoring_issue_events enable row level security;
alter table private.monitoring_issue_events force row level security;
revoke all on table private.monitoring_issue_events from public, anon, authenticated;

create or replace function private.prevent_monitoring_issue_event_mutation_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  raise exception 'monitoring issue events are append-only' using errcode = '55000';
end;
$$;

revoke all on function private.prevent_monitoring_issue_event_mutation_v1()
  from public, anon, authenticated;

create trigger prevent_monitoring_issue_event_mutation_v1
before update or delete on private.monitoring_issue_events
for each row execute function private.prevent_monitoring_issue_event_mutation_v1();

create or replace function private.assert_active_monitoring_admin_v1(
  p_actor_user_id uuid
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_actor_user_id is null or not exists (
    select 1
    from private.monitoring_admins as admin
    where admin.user_id = p_actor_user_id
      and admin.active
      and admin.deactivated_at is null
  ) then
    raise exception 'monitoring_forbidden' using errcode = '42501';
  end if;
end;
$$;

revoke all on function private.assert_active_monitoring_admin_v1(uuid)
  from public, anon, authenticated;

create or replace function private.monitoring_issue_summary_v1(
  p_report jsonb,
  p_category text
)
returns jsonb
language sql
immutable
strict
set search_path = ''
as $$
  select jsonb_build_object(
    'app_version', p_report ->> 'app_version',
    'reported_release_id', coalesce(
      p_report ->> 'reported_release_id',
      'release-v' || trim(leading 'v' from (p_report ->> 'app_version'))
    ),
    'category', p_category,
    'kind', p_report ->> 'kind',
    'report_target_type', p_report ->> 'report_target_type',
    'source', p_report ->> 'source',
    'route', p_report #>> '{observed,route}',
    'status', p_report #>> '{observed,status}',
    'latency_ms', p_report #> '{observed,latency_ms}',
    'result_count', p_report #> '{observed,result_count}',
    'citation_count', p_report #> '{observed,citation_count}',
    'diagnostics', p_report -> 'diagnostics',
    'case_diagnostics_status', case
      when p_report ? 'case_diagnostics' then 'AVAILABLE'
      else 'ABSENT'
    end,
    'privacy', p_report -> 'privacy',
    'consented_content', jsonb_build_object(
      'comment', coalesce((p_report #>> '{consent,include_comment}')::boolean, false),
      'question', coalesce((p_report #>> '{consent,include_selected_question}')::boolean, false),
      'answer', coalesce((p_report #>> '{consent,include_selected_answer}')::boolean, false),
      'previous_turns', coalesce((p_report #>> '{consent,include_previous_turns}')::boolean, false)
    )
  );
$$;

revoke all on function private.monitoring_issue_summary_v1(jsonb, text)
  from public, anon, authenticated;

create or replace function private.create_monitoring_issue_after_ingest_v1()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_issue private.monitoring_issues%rowtype;
begin
  insert into private.monitoring_issues (receipt_id, received_at, summary)
  values (
    new.receipt_id,
    new.received_at,
    private.monitoring_issue_summary_v1(new.report, new.category)
  )
  on conflict (receipt_id) do nothing
  returning * into v_issue;

  if found then
    insert into private.monitoring_issue_events (
      issue_id, event_type, actor_user_id, to_state, record_revision, details
    ) values (
      v_issue.issue_id, 'CREATED', null, 'OPEN', v_issue.record_revision,
      '{"source":"anonymous_ingest"}'::jsonb
    );
  end if;
  return new;
end;
$$;

revoke all on function private.create_monitoring_issue_after_ingest_v1()
  from public, anon, authenticated;

create trigger create_monitoring_issue_after_ingest_v1
after insert on private.issue_reports
for each row execute function private.create_monitoring_issue_after_ingest_v1();

create or replace function private.materialize_monitoring_issues_v1(
  p_actor_user_id uuid
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  with inserted as (
    insert into private.monitoring_issues (receipt_id, received_at, summary)
    select
      report.receipt_id,
      report.received_at,
      private.monitoring_issue_summary_v1(report.report, report.category)
    from private.issue_reports as report
    where true
    on conflict (receipt_id) do nothing
    returning issue_id, record_revision
  )
  insert into private.monitoring_issue_events (
    issue_id, event_type, actor_user_id, to_state, record_revision,
    details
  )
  select
    inserted.issue_id, 'CREATED', p_actor_user_id, 'OPEN',
    inserted.record_revision, '{"source":"pre_control_plane_backfill"}'::jsonb
  from inserted;
end;
$$;

revoke all on function private.materialize_monitoring_issues_v1(uuid)
  from public, anon, authenticated;

-- Preserve non-sensitive summaries for reports received before this control plane.
select private.materialize_monitoring_issues_v1(null::uuid);

create or replace function public.monitoring_check_admin_v1(
  p_actor_user_id uuid
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform private.assert_active_monitoring_admin_v1(p_actor_user_id);
  return true;
end;
$$;

revoke all on function public.monitoring_check_admin_v1(uuid)
  from public, anon, authenticated;
grant execute on function public.monitoring_check_admin_v1(uuid)
  to service_role;

create or replace function public.monitoring_list_issues_v1(
  p_actor_user_id uuid,
  p_state text default null,
  p_limit integer default 50
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_result jsonb;
begin
  perform private.assert_active_monitoring_admin_v1(p_actor_user_id);
  if p_state is not null and p_state not in ('OPEN', 'CLOSED') then
    raise exception 'invalid issue state' using errcode = '22023';
  end if;
  if p_limit not between 1 and 200 then
    raise exception 'limit must be between 1 and 200' using errcode = '22023';
  end if;

  perform private.materialize_monitoring_issues_v1(p_actor_user_id);

  select coalesce(jsonb_agg(row_payload order by received_at desc, issue_id), '[]'::jsonb)
  into v_result
  from (
    select
      issue.issue_id,
      issue.received_at,
      issue.summary || jsonb_build_object(
        'issue_id', issue.issue_id,
        'state', issue.state,
        'record_revision', issue.record_revision,
        'received_at', issue.received_at,
        'updated_at', issue.updated_at,
        'closed_at', issue.closed_at,
        'raw_available', exists (
          select 1 from private.issue_reports as raw_report
          where raw_report.receipt_id = issue.receipt_id
        )
      ) as row_payload
    from private.monitoring_issues as issue
    where p_state is null or issue.state = p_state
    order by issue.received_at desc, issue.issue_id
    limit p_limit
  ) as rows;

  return v_result;
end;
$$;

revoke all on function public.monitoring_list_issues_v1(uuid, text, integer)
  from public, anon, authenticated;
grant execute on function public.monitoring_list_issues_v1(uuid, text, integer)
  to service_role;

create or replace function public.monitoring_get_issue_v1(
  p_actor_user_id uuid,
  p_issue_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_result jsonb;
begin
  perform private.assert_active_monitoring_admin_v1(p_actor_user_id);
  perform private.materialize_monitoring_issues_v1(p_actor_user_id);

  select issue.summary || jsonb_build_object(
    'issue_id', issue.issue_id,
    'state', issue.state,
    'record_revision', issue.record_revision,
    'received_at', issue.received_at,
    'updated_at', issue.updated_at,
    'closed_at', issue.closed_at,
    'raw_available', exists (
      select 1 from private.issue_reports as raw_report
      where raw_report.receipt_id = issue.receipt_id
    ),
    'events', coalesce((
      select jsonb_agg(jsonb_build_object(
        'event_id', event.event_id,
        'event_type', event.event_type,
        'actor_user_id', event.actor_user_id,
        'reason', event.reason,
        'from_state', event.from_state,
        'to_state', event.to_state,
        'record_revision', event.record_revision,
        'occurred_at', event.occurred_at,
        'details', event.details
      ) order by event.occurred_at, event.event_id)
      from private.monitoring_issue_events as event
      where event.issue_id = issue.issue_id
    ), '[]'::jsonb)
  )
  into v_result
  from private.monitoring_issues as issue
  where issue.issue_id = p_issue_id;

  if v_result is null then
    return jsonb_build_object('disposition', 'not_found');
  end if;
  return jsonb_build_object('disposition', 'found', 'issue', v_result);
end;
$$;

revoke all on function public.monitoring_get_issue_v1(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.monitoring_get_issue_v1(uuid, uuid)
  to service_role;

create or replace function public.monitoring_view_issue_raw_v1(
  p_actor_user_id uuid,
  p_issue_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_issue private.monitoring_issues%rowtype;
  v_report jsonb;
  v_consent jsonb;
  v_raw jsonb;
begin
  perform private.assert_active_monitoring_admin_v1(p_actor_user_id);

  select issue.*
  into v_issue
  from private.monitoring_issues as issue
  where issue.issue_id = p_issue_id
  for update;

  if not found then
    return jsonb_build_object('disposition', 'not_found');
  end if;

  select report.report
  into v_report
  from private.issue_reports as report
  where report.receipt_id = v_issue.receipt_id;
  if not found then
    return jsonb_build_object(
      'disposition', 'raw_unavailable',
      'issue_id', v_issue.issue_id,
      'reason', 'retained_summary_only'
    );
  end if;

  v_consent := coalesce(v_report -> 'consent', '{}'::jsonb);
  v_raw := jsonb_build_object(
    'schema_version', v_report -> 'schema_version',
    'report_contract_version', v_report -> 'report_contract_version',
    'kind', v_report -> 'kind',
    'report_target_type', v_report -> 'report_target_type',
    'source', v_report -> 'source',
    'app_version', v_report -> 'app_version',
    'category', v_report -> 'category',
    'comment', case when coalesce((v_consent ->> 'include_comment')::boolean, false)
      then v_report -> 'comment' else 'null'::jsonb end,
    'consent', v_consent,
    'observed', jsonb_build_object(
      'route', v_report #> '{observed,route}',
      'status', v_report #> '{observed,status}',
      'latency_ms', v_report #> '{observed,latency_ms}',
      'result_count', v_report #> '{observed,result_count}',
      'result_count_kind', v_report #> '{observed,result_count_kind}',
      'citation_count', v_report #> '{observed,citation_count}',
      'selected_question', case when coalesce((v_consent ->> 'include_selected_question')::boolean, false)
        then v_report #> '{observed,selected_question}' else 'null'::jsonb end,
      'selected_answer', case when coalesce((v_consent ->> 'include_selected_answer')::boolean, false)
        then v_report #> '{observed,selected_answer}' else 'null'::jsonb end,
      'turn_trace', case when coalesce((v_consent ->> 'include_previous_turns')::boolean, false)
        then coalesce(v_report #> '{observed,turn_trace}', '[]'::jsonb) else '[]'::jsonb end
    ),
    'diagnostics', v_report -> 'diagnostics',
    'case_diagnostics', case when coalesce((v_consent ->> 'include_previous_turns')::boolean, false)
      then v_report -> 'case_diagnostics' else 'null'::jsonb end,
    'privacy', v_report -> 'privacy'
  );

  insert into private.monitoring_issue_events (
    issue_id, event_type, actor_user_id, from_state, to_state,
    record_revision, details
  ) values (
    v_issue.issue_id, 'RAW_VIEWED', p_actor_user_id, v_issue.state,
    v_issue.state, v_issue.record_revision,
    '{"access":"explicit_action","scope":"consented_report_only"}'::jsonb
  );

  return jsonb_build_object(
    'disposition', 'found',
    'issue_id', v_issue.issue_id,
    'record_revision', v_issue.record_revision,
    'raw_report', v_raw
  );
end;
$$;

revoke all on function public.monitoring_view_issue_raw_v1(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.monitoring_view_issue_raw_v1(uuid, uuid)
  to service_role;

create or replace function public.monitoring_transition_issue_v1(
  p_actor_user_id uuid,
  p_issue_id uuid,
  p_expected_record_revision bigint,
  p_target_state text,
  p_reason text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_before private.monitoring_issues%rowtype;
  v_after private.monitoring_issues%rowtype;
  v_event_type text;
begin
  perform private.assert_active_monitoring_admin_v1(p_actor_user_id);
  if p_target_state not in ('OPEN', 'CLOSED') then
    raise exception 'invalid target state' using errcode = '22023';
  end if;
  if p_reason is null or char_length(btrim(p_reason)) not between 1 and 2000 then
    raise exception 'reason must contain 1 to 2000 characters' using errcode = '22023';
  end if;

  select * into v_before
  from private.monitoring_issues
  where issue_id = p_issue_id;
  if not found then
    return jsonb_build_object('disposition', 'not_found');
  end if;
  if v_before.record_revision <> p_expected_record_revision then
    return jsonb_build_object(
      'disposition', 'conflict',
      'record_revision', v_before.record_revision,
      'state', v_before.state
    );
  end if;
  if v_before.state = p_target_state then
    return jsonb_build_object(
      'disposition', 'invalid_transition',
      'record_revision', v_before.record_revision,
      'state', v_before.state
    );
  end if;

  update private.monitoring_issues
  set
    state = p_target_state,
    record_revision = record_revision + 1,
    updated_at = clock_timestamp(),
    closed_at = case when p_target_state = 'CLOSED' then clock_timestamp() else null end
  where issue_id = p_issue_id
    and record_revision = p_expected_record_revision
    and state = v_before.state
  returning * into v_after;

  if not found then
    select * into v_before from private.monitoring_issues where issue_id = p_issue_id;
    return jsonb_build_object(
      'disposition', 'conflict',
      'record_revision', v_before.record_revision,
      'state', v_before.state
    );
  end if;

  v_event_type := case when p_target_state = 'CLOSED' then 'CLOSED' else 'REOPENED' end;
  insert into private.monitoring_issue_events (
    issue_id, event_type, actor_user_id, reason, from_state, to_state,
    record_revision
  ) values (
    v_after.issue_id, v_event_type, p_actor_user_id, btrim(p_reason),
    v_before.state, v_after.state, v_after.record_revision
  );

  return jsonb_build_object(
    'disposition', 'updated',
    'issue_id', v_after.issue_id,
    'state', v_after.state,
    'record_revision', v_after.record_revision,
    'updated_at', v_after.updated_at,
    'closed_at', v_after.closed_at
  );
end;
$$;

revoke all on function public.monitoring_transition_issue_v1(
  uuid, uuid, bigint, text, text
) from public, anon, authenticated;
grant execute on function public.monitoring_transition_issue_v1(
  uuid, uuid, bigint, text, text
) to service_role;

-- Reassert the anonymous report boundary after adding the operator control plane.
revoke all on table private.issue_reports from public, anon, authenticated;

comment on function public.monitoring_view_issue_raw_v1(uuid, uuid) is
  'Returns only consented report content and appends RAW_VIEWED in the same transaction.';
comment on function public.monitoring_transition_issue_v1(uuid, uuid, bigint, text, text) is
  'Compare-and-swap OPEN/CLOSED transition that preserves an append-only reason event.';
