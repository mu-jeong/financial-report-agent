-- Distinguish unreviewed, actively investigated, resolved, and dismissed issues.
-- Legacy CLOSED rows remain terminal and explicitly classified as legacy state.

alter table private.monitoring_issues
  drop constraint monitoring_issues_state_check;

alter table private.monitoring_issues
  add constraint monitoring_issues_state_check check (
    state in ('OPEN', 'IN_PROGRESS', 'CLOSED', 'RESOLVED', 'NOT_ISSUE')
  );

alter table private.monitoring_issues
  drop constraint monitoring_issues_closed_at_consistent;

alter table private.monitoring_issues
  add constraint monitoring_issues_closed_at_consistent check (
    (state in ('OPEN', 'IN_PROGRESS') and closed_at is null)
    or (state in ('CLOSED', 'RESOLVED', 'NOT_ISSUE') and closed_at is not null)
  );

alter table private.monitoring_issue_events
  drop constraint monitoring_issue_events_event_type_check;

alter table private.monitoring_issue_events
  add constraint monitoring_issue_events_event_type_check check (
    event_type in (
      'CREATED', 'RAW_VIEWED', 'IN_PROGRESS', 'CLOSED', 'RESOLVED',
      'NOT_ISSUE', 'REOPENED'
    )
  );

alter table private.monitoring_issue_events
  drop constraint monitoring_issue_events_from_state_check;

alter table private.monitoring_issue_events
  add constraint monitoring_issue_events_from_state_check check (
    from_state is null
    or from_state in ('OPEN', 'IN_PROGRESS', 'CLOSED', 'RESOLVED', 'NOT_ISSUE')
  );

alter table private.monitoring_issue_events
  drop constraint monitoring_issue_events_to_state_check;

alter table private.monitoring_issue_events
  add constraint monitoring_issue_events_to_state_check check (
    to_state is null
    or to_state in ('OPEN', 'IN_PROGRESS', 'CLOSED', 'RESOLVED', 'NOT_ISSUE')
  );

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
  if p_state is not null and p_state not in (
    'OPEN', 'IN_PROGRESS', 'CLOSED', 'RESOLVED', 'NOT_ISSUE'
  ) then
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
  if p_target_state not in (
    'OPEN', 'IN_PROGRESS', 'RESOLVED', 'NOT_ISSUE'
  ) then
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
  if not (
    (v_before.state = 'OPEN' and p_target_state in (
      'IN_PROGRESS', 'RESOLVED', 'NOT_ISSUE'
    ))
    or (v_before.state = 'IN_PROGRESS' and p_target_state in (
      'OPEN', 'RESOLVED', 'NOT_ISSUE'
    ))
    or (v_before.state = 'CLOSED' and p_target_state in (
      'OPEN', 'RESOLVED', 'NOT_ISSUE'
    ))
    or (v_before.state = 'RESOLVED' and p_target_state in ('OPEN', 'NOT_ISSUE'))
    or (v_before.state = 'NOT_ISSUE' and p_target_state in ('OPEN', 'RESOLVED'))
  ) then
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
    closed_at = case
      when p_target_state in ('OPEN', 'IN_PROGRESS') then null
      else coalesce(v_before.closed_at, clock_timestamp())
    end
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

  v_event_type := case p_target_state
    when 'OPEN' then 'REOPENED'
    when 'IN_PROGRESS' then 'IN_PROGRESS'
    when 'RESOLVED' then 'RESOLVED'
    when 'NOT_ISSUE' then 'NOT_ISSUE'
  end;
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

-- CREATE OR REPLACE preserves the v1 signatures and existing service-role grants.
comment on function public.monitoring_transition_issue_v1(uuid, uuid, bigint, text, text) is
  'CAS lifecycle transition across writable classified states, with legacy CLOSED accepted only as a source state and every reason appended as an event.';
