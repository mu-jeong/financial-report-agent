-- Minimal, metadata-only reconciliation records for the single-admin workflow.
-- Only bounded identities, digests, lifecycle state, and categorical attributes cross this boundary.

create table private.monitoring_control_records (
  issue_id uuid not null references private.monitoring_issues (issue_id),
  record_kind text not null check (record_kind in (
    'FIXTURE', 'CASE', 'FIXED_SNAPSHOT', 'RELEASE', 'RUN', 'COMPARISON'
  )),
  record_id text not null,
  lifecycle_status text not null check (lifecycle_status in (
    'DRAFT', 'READY', 'REGISTERED', 'QUEUED', 'RUNNING',
    'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'CREATED'
  )),
  content_digest text not null check (content_digest ~ '^[0-9a-f]{64}$'),
  availability text check (
    availability is null or availability in (
      'AVAILABLE', 'MISSING', 'CORRUPT', 'INCOMPATIBLE', 'UNKNOWN'
    )
  ),
  references_json jsonb not null default '{}'::jsonb,
  attributes_json jsonb not null default '{}'::jsonb,
  record_revision bigint not null default 1 check (record_revision > 0),
  created_at timestamptz not null default clock_timestamp(),
  updated_at timestamptz not null default clock_timestamp(),
  updated_by uuid not null references auth.users (id),
  primary key (issue_id, record_kind, record_id),
  constraint monitoring_control_record_id_bound check (
    char_length(record_id) between 1 and 160
    and record_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  constraint monitoring_control_references_object check (jsonb_typeof(references_json) = 'object'),
  constraint monitoring_control_attributes_object check (jsonb_typeof(attributes_json) = 'object')
);

create index monitoring_control_records_issue_updated_idx
  on private.monitoring_control_records (issue_id, updated_at, record_kind, record_id);

alter table private.monitoring_control_records enable row level security;
alter table private.monitoring_control_records force row level security;
revoke all on table private.monitoring_control_records from public, anon, authenticated;

create table private.monitoring_control_record_events (
  event_id uuid primary key default gen_random_uuid(),
  issue_id uuid not null,
  record_kind text not null,
  record_id text not null,
  actor_user_id uuid not null references auth.users (id),
  event_type text not null check (event_type in ('CREATED', 'UPDATED')),
  record_revision bigint not null check (record_revision > 0),
  content_digest text not null check (content_digest ~ '^[0-9a-f]{64}$'),
  lifecycle_status text not null check (lifecycle_status in (
    'DRAFT', 'READY', 'REGISTERED', 'QUEUED', 'RUNNING',
    'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'CREATED'
  )),
  availability text check (
    availability is null or availability in (
      'AVAILABLE', 'MISSING', 'CORRUPT', 'INCOMPATIBLE', 'UNKNOWN'
    )
  ),
  validity text check (
    validity is null or validity in ('VALID', 'INVALID', 'UNKNOWN')
  ),
  occurred_at timestamptz not null default clock_timestamp(),
  foreign key (issue_id, record_kind, record_id)
    references private.monitoring_control_records (issue_id, record_kind, record_id)
);

alter table private.monitoring_control_record_events enable row level security;
alter table private.monitoring_control_record_events force row level security;
revoke all on table private.monitoring_control_record_events from public, anon, authenticated;

create trigger prevent_monitoring_control_record_event_mutation_v1
before update or delete on private.monitoring_control_record_events
for each row execute function private.prevent_monitoring_issue_event_mutation_v1();

create or replace function private.validate_monitoring_control_payload_v1(
  p_kind text,
  p_references jsonb,
  p_attributes jsonb
)
returns void
language plpgsql
immutable
set search_path = ''
as $$
declare
  v_key text;
  v_value jsonb;
begin
  if p_kind not in ('FIXTURE', 'CASE', 'FIXED_SNAPSHOT', 'RELEASE', 'RUN', 'COMPARISON')
    or jsonb_typeof(p_references) <> 'object'
    or jsonb_typeof(p_attributes) <> 'object'
    or pg_column_size(p_references) > 4096
    or pg_column_size(p_attributes) > 2048 then
    raise exception 'invalid monitoring control payload' using errcode = '22023';
  end if;

  for v_key, v_value in select key, value from jsonb_each(p_references) loop
    if v_key not in (
      'fixture_revision_id', 'fixed_snapshot_revision_id', 'release_manifest_id',
      'case_contract_id', 'supersedes_comparison_id'
    ) then
      raise exception 'invalid monitoring control reference' using errcode = '22023';
    end if;
    if (p_kind in ('FIXTURE', 'FIXED_SNAPSHOT', 'RELEASE'))
      or (p_kind = 'CASE' and v_key not in (
        'fixture_revision_id', 'fixed_snapshot_revision_id', 'case_contract_id'
      ))
      or (p_kind = 'RUN' and v_key not in (
        'fixed_snapshot_revision_id', 'release_manifest_id', 'case_contract_id'
      ))
      or (p_kind = 'COMPARISON' and v_key not in (
        'case_contract_id', 'supersedes_comparison_id'
      )) then
      raise exception 'reference is not allowed for record kind' using errcode = '22023';
    end if;
    if jsonb_typeof(v_value) <> 'string'
      or length(v_value #>> '{}') not between 1 and 160
      or (v_value #>> '{}') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$' then
      raise exception 'invalid monitoring control reference type' using errcode = '22023';
    end if;
  end loop;

  for v_key, v_value in select key, value from jsonb_each(p_attributes) loop
    if v_key not in ('side', 'validity', 'verdict', 'evidence_qualifier')
      or jsonb_typeof(v_value) <> 'string'
      or length(v_value #>> '{}') not between 1 and 32
      or (v_value #>> '{}') !~ '^[A-Z_]+$' then
      raise exception 'invalid monitoring control attribute' using errcode = '22023';
    end if;
    if (p_kind in ('FIXTURE', 'FIXED_SNAPSHOT', 'RELEASE'))
      or (p_kind = 'CASE' and v_key <> 'evidence_qualifier')
      or (p_kind = 'RUN' and v_key not in ('side', 'validity', 'evidence_qualifier'))
      or (p_kind = 'COMPARISON' and v_key <> 'verdict') then
      raise exception 'attribute is not allowed for record kind' using errcode = '22023';
    end if;
    if (v_key = 'side' and (v_value #>> '{}') not in ('BASELINE', 'CANDIDATE'))
      or (v_key = 'validity' and (v_value #>> '{}') not in ('VALID', 'INVALID', 'UNKNOWN'))
      or (v_key = 'verdict' and (v_value #>> '{}') not in (
        'IMPROVED', 'NOT_IMPROVED', 'REGRESSED', 'INCONCLUSIVE'
      ))
      or (v_key = 'evidence_qualifier' and (v_value #>> '{}') not in (
        'EXACT', 'PARTIAL', 'SUBSTITUTE_INCLUDED', 'UNKNOWN'
      )) then
      raise exception 'invalid monitoring control attribute value' using errcode = '22023';
    end if;
  end loop;
end;
$$;

revoke all on function private.validate_monitoring_control_payload_v1(text, jsonb, jsonb)
  from public, anon, authenticated;

create or replace function public.monitoring_list_control_records_v1(
  p_actor_user_id uuid,
  p_issue_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_records jsonb;
begin
  perform private.assert_active_monitoring_admin_v1(p_actor_user_id);
  if not exists (select 1 from private.monitoring_issues where issue_id = p_issue_id) then
    return jsonb_build_object('disposition', 'not_found');
  end if;
  select coalesce(jsonb_agg(jsonb_build_object(
    'record_kind', record.record_kind,
    'record_id', record.record_id,
    'lifecycle_status', record.lifecycle_status,
    'content_digest', record.content_digest,
    'availability', record.availability,
    'references', record.references_json,
    'attributes', record.attributes_json,
    'record_revision', record.record_revision,
    'updated_at', record.updated_at
  ) order by record.record_kind, record.record_id), '[]'::jsonb)
  into v_records
  from private.monitoring_control_records as record
  where record.issue_id = p_issue_id;
  return jsonb_build_object('disposition', 'found', 'records', v_records);
end;
$$;

revoke all on function public.monitoring_list_control_records_v1(uuid, uuid)
  from public, anon, authenticated;
grant execute on function public.monitoring_list_control_records_v1(uuid, uuid)
  to service_role;

create or replace function public.monitoring_reconcile_control_record_v1(
  p_actor_user_id uuid,
  p_issue_id uuid,
  p_expected_record_revision bigint,
  p_record_kind text,
  p_record_id text,
  p_lifecycle_status text,
  p_content_digest text,
  p_availability text,
  p_references jsonb,
  p_attributes jsonb
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_before private.monitoring_control_records%rowtype;
  v_after private.monitoring_control_records%rowtype;
  v_event_type text;
begin
  perform private.assert_active_monitoring_admin_v1(p_actor_user_id);
  if p_expected_record_revision < 0
    or p_record_kind not in ('FIXTURE', 'CASE', 'FIXED_SNAPSHOT', 'RELEASE', 'RUN', 'COMPARISON')
    or p_lifecycle_status not in (
      'DRAFT', 'READY', 'REGISTERED', 'QUEUED', 'RUNNING',
      'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'CREATED'
    )
    or p_record_id is null or length(p_record_id) not between 1 and 160
    or p_record_id !~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
    or p_content_digest !~ '^[0-9a-f]{64}$'
    or (p_availability is not null and p_availability not in (
      'AVAILABLE', 'MISSING', 'CORRUPT', 'INCOMPATIBLE', 'UNKNOWN'
    )) then
    raise exception 'invalid monitoring control record' using errcode = '22023';
  end if;
  if (p_record_kind in ('FIXTURE', 'CASE') and p_lifecycle_status not in ('DRAFT', 'READY'))
    or (p_record_kind = 'FIXED_SNAPSHOT' and p_lifecycle_status <> 'READY')
    or (p_record_kind = 'RELEASE' and p_lifecycle_status <> 'REGISTERED')
    or (p_record_kind = 'RUN' and p_lifecycle_status not in (
      'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'
    ))
    or (p_record_kind = 'COMPARISON' and p_lifecycle_status <> 'CREATED') then
    raise exception 'invalid lifecycle for monitoring control kind' using errcode = '22023';
  end if;
  perform private.validate_monitoring_control_payload_v1(p_record_kind, p_references, p_attributes);
  if p_record_kind in ('FIXTURE', 'CASE', 'COMPARISON')
    and p_availability is not null then
    raise exception 'invalid non-artifact availability' using errcode = '22023';
  end if;
  if p_record_kind = 'CASE' and p_lifecycle_status = 'READY' and (
    not (p_references ? 'fixture_revision_id')
    or not (p_references ? 'fixed_snapshot_revision_id')
    or not (p_references ? 'case_contract_id')
    or not (p_attributes ? 'evidence_qualifier')
  ) then
    raise exception 'invalid Case control payload' using errcode = '22023';
  end if;
  if p_record_kind = 'COMPARISON' and (
    not (p_references ? 'case_contract_id')
    or not (p_attributes ? 'verdict')
  ) then
    raise exception 'invalid Comparison control payload' using errcode = '22023';
  end if;
  if p_record_kind = 'RUN' and (
    not (p_references ? 'fixed_snapshot_revision_id')
    or not (p_references ? 'release_manifest_id')
    or not (p_references ? 'case_contract_id')
    or not (p_attributes ? 'side')
    or not (p_attributes ? 'validity')
    or not (p_attributes ? 'evidence_qualifier')
    or (
      p_lifecycle_status in ('QUEUED', 'RUNNING')
      and (
        p_attributes ->> 'validity' <> 'UNKNOWN'
        or p_availability is not null
      )
    )
    or (
      p_lifecycle_status = 'SUCCEEDED'
      and (p_attributes ->> 'validity') not in ('VALID', 'INVALID')
    )
    or (
      p_lifecycle_status in ('FAILED', 'CANCELLED', 'INTERRUPTED')
      and p_attributes ->> 'validity' <> 'INVALID'
    )
  ) then
    raise exception 'invalid Run control payload' using errcode = '22023';
  end if;
  if not exists (select 1 from private.monitoring_issues where issue_id = p_issue_id) then
    return jsonb_build_object('disposition', 'not_found');
  end if;

  select * into v_before from private.monitoring_control_records
  where issue_id = p_issue_id and record_kind = p_record_kind and record_id = p_record_id
  for update;

  if not found then
    if p_expected_record_revision <> 0 then
      return jsonb_build_object('disposition', 'conflict', 'record_revision', 0);
    end if;
    if p_record_kind = 'RUN' and p_lifecycle_status <> 'QUEUED' then
      return jsonb_build_object(
        'disposition', 'immutable_conflict', 'record_revision', 0,
        'content_digest', null
      );
    end if;
    insert into private.monitoring_control_records (
      issue_id, record_kind, record_id, lifecycle_status, content_digest,
      availability, references_json, attributes_json, updated_by
    ) values (
      p_issue_id, p_record_kind, p_record_id, p_lifecycle_status, p_content_digest,
      p_availability, p_references, p_attributes, p_actor_user_id
    ) on conflict (issue_id, record_kind, record_id) do nothing
    returning * into v_after;
    if not found then
      select * into v_before from private.monitoring_control_records
      where issue_id = p_issue_id and record_kind = p_record_kind and record_id = p_record_id
      for update;
      if v_before.lifecycle_status = p_lifecycle_status
        and v_before.content_digest = p_content_digest
        and v_before.availability is not distinct from p_availability
        and v_before.references_json = p_references
        and v_before.attributes_json = p_attributes then
        return jsonb_build_object(
          'disposition', 'unchanged', 'record_revision', v_before.record_revision,
          'content_digest', v_before.content_digest
        );
      end if;
      return jsonb_build_object(
        'disposition', case when p_record_kind = 'COMPARISON'
          then 'immutable_conflict' else 'conflict' end,
        'record_revision', v_before.record_revision,
        'content_digest', v_before.content_digest
      );
    end if;
    v_event_type := 'CREATED';
  else
    if v_before.lifecycle_status = p_lifecycle_status
      and v_before.content_digest = p_content_digest
      and v_before.availability is not distinct from p_availability
      and v_before.references_json = p_references
      and v_before.attributes_json = p_attributes then
      return jsonb_build_object(
        'disposition', 'unchanged', 'record_revision', v_before.record_revision,
        'content_digest', v_before.content_digest
      );
    end if;
    if p_record_kind = 'COMPARISON' then
      return jsonb_build_object(
        'disposition', 'immutable_conflict', 'record_revision', v_before.record_revision,
        'content_digest', v_before.content_digest
      );
    end if;
    if p_record_kind in ('FIXTURE', 'CASE') and v_before.lifecycle_status = 'READY' then
      return jsonb_build_object(
        'disposition', 'immutable_conflict', 'record_revision', v_before.record_revision,
        'content_digest', v_before.content_digest
      );
    end if;
    if p_record_kind in ('FIXED_SNAPSHOT', 'RELEASE') and (
      v_before.content_digest <> p_content_digest
      or v_before.references_json <> p_references
      or v_before.attributes_json <> p_attributes
      or v_before.lifecycle_status <> p_lifecycle_status
    ) then
      return jsonb_build_object(
        'disposition', 'immutable_conflict', 'record_revision', v_before.record_revision,
        'content_digest', v_before.content_digest
      );
    end if;
    if p_record_kind = 'RUN' and not (
      (
        v_before.lifecycle_status = 'QUEUED'
        and p_lifecycle_status = 'RUNNING'
        and p_content_digest = v_before.content_digest
        and p_availability is not distinct from v_before.availability
        and p_references = v_before.references_json
        and p_attributes = v_before.attributes_json
      )
      or (
        v_before.lifecycle_status = 'RUNNING'
        and p_lifecycle_status in (
          'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'
        )
        and p_references = v_before.references_json
        and (p_attributes - 'validity') = (
          v_before.attributes_json - 'validity'
        )
      )
      or (
        v_before.lifecycle_status in (
          'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'
        )
        and p_lifecycle_status = v_before.lifecycle_status
        and p_content_digest = v_before.content_digest
        and p_references = v_before.references_json
        and p_attributes = v_before.attributes_json
      )
    ) then
      return jsonb_build_object(
        'disposition', 'immutable_conflict', 'record_revision', v_before.record_revision,
        'content_digest', v_before.content_digest
      );
    end if;
    if v_before.record_revision <> p_expected_record_revision then
      return jsonb_build_object(
        'disposition', 'conflict', 'record_revision', v_before.record_revision,
        'content_digest', v_before.content_digest
      );
    end if;
    update private.monitoring_control_records set
      lifecycle_status = p_lifecycle_status,
      content_digest = p_content_digest,
      availability = p_availability,
      references_json = p_references,
      attributes_json = p_attributes,
      record_revision = record_revision + 1,
      updated_at = clock_timestamp(),
      updated_by = p_actor_user_id
    where issue_id = p_issue_id and record_kind = p_record_kind
      and record_id = p_record_id and record_revision = p_expected_record_revision
    returning * into v_after;
    if not found then
      select * into v_before from private.monitoring_control_records
      where issue_id = p_issue_id and record_kind = p_record_kind and record_id = p_record_id;
      return jsonb_build_object(
        'disposition', 'conflict', 'record_revision', v_before.record_revision,
        'content_digest', v_before.content_digest
      );
    end if;
    v_event_type := 'UPDATED';
  end if;

  insert into private.monitoring_control_record_events (
    issue_id, record_kind, record_id, actor_user_id, event_type,
    record_revision, content_digest, lifecycle_status, availability, validity
  ) values (
    v_after.issue_id, v_after.record_kind, v_after.record_id, p_actor_user_id,
    v_event_type, v_after.record_revision, v_after.content_digest,
    v_after.lifecycle_status, v_after.availability,
    v_after.attributes_json ->> 'validity'
  );
  return jsonb_build_object(
    'disposition', lower(v_event_type),
    'record_kind', v_after.record_kind,
    'record_id', v_after.record_id,
    'record_revision', v_after.record_revision,
    'content_digest', v_after.content_digest
  );
end;
$$;

revoke all on function public.monitoring_reconcile_control_record_v1(
  uuid, uuid, bigint, text, text, text, text, text, jsonb, jsonb
) from public, anon, authenticated;
grant execute on function public.monitoring_reconcile_control_record_v1(
  uuid, uuid, bigint, text, text, text, text, text, jsonb, jsonb
) to service_role;

comment on table private.monitoring_control_records is
  'Bounded metadata-only reconciliation projection with no content or filesystem location fields.';
