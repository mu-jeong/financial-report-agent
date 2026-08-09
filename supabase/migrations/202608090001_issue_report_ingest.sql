create schema if not exists private;

revoke all on schema private from public, anon, authenticated;
grant usage on schema private to service_role;

create table private.issue_reports (
  receipt_id uuid primary key default gen_random_uuid(),
  event_id uuid not null unique,
  installation_id uuid not null,
  queued_at timestamptz not null,
  received_at timestamptz not null default clock_timestamp(),
  report_schema_version smallint not null check (report_schema_version = 2),
  category text not null check (
    category in (
      '일반 답변 품질', '검색 정확도 이슈', '오답/오류',
      '속도', '버그/기능', '기타'
    )
  ),
  severity text not null check (severity = 'normal'),
  source_ip_hash text not null check (source_ip_hash ~ '^[0-9a-f]{64}$'),
  report jsonb not null,
  constraint issue_reports_report_is_object check (jsonb_typeof(report) = 'object')
);

comment on table private.issue_reports is
  'Private, server-validated issue reports. source_ip_hash is an HMAC-SHA-256 digest; raw IPs are never stored.';

create index issue_reports_received_at_idx
  on private.issue_reports (received_at desc);
create index issue_reports_installation_received_idx
  on private.issue_reports (installation_id, received_at desc);

alter table private.issue_reports enable row level security;
alter table private.issue_reports force row level security;

revoke all on table private.issue_reports from public, anon, authenticated;
grant select, insert, update, delete on table private.issue_reports to service_role;

create table private.issue_ingest_rate_counters (
  scope text not null check (scope in ('ip', 'installation', 'global')),
  subject_hash text not null,
  bucket_started_at timestamptz not null,
  window_seconds integer not null check (window_seconds between 1 and 86400),
  request_count integer not null check (request_count > 0),
  updated_at timestamptz not null default clock_timestamp(),
  primary key (scope, subject_hash, bucket_started_at, window_seconds)
);

alter table private.issue_ingest_rate_counters enable row level security;
alter table private.issue_ingest_rate_counters force row level security;
revoke all on table private.issue_ingest_rate_counters from public, anon, authenticated;
grant select, insert, update, delete on table private.issue_ingest_rate_counters to service_role;

create or replace function public.preflight_issue_ingest_v1(
  p_source_ip_hash text,
  p_ip_limit integer,
  p_ip_window_seconds integer,
  p_global_limit integer,
  p_global_window_seconds integer
)
returns table (
  disposition text,
  retry_after_seconds integer
)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_now timestamptz := clock_timestamp();
  v_scope text;
  v_subject text;
  v_limit integer;
  v_window integer;
  v_bucket timestamptz;
  v_count integer;
  v_retry integer := 0;
begin
  if p_ip_limit < 1 or p_global_limit < 1 then
    raise exception 'rate limits must be positive' using errcode = '22023';
  end if;
  if p_ip_window_seconds not between 1 and 86400
     or p_global_window_seconds not between 1 and 86400 then
    raise exception 'rate windows must be between 1 and 86400 seconds' using errcode = '22023';
  end if;
  if p_source_ip_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid subject hash' using errcode = '22023';
  end if;

  -- Charge every POST before reading or parsing its body. This prevents
  -- malformed and chunked requests from bypassing application quotas.
  for v_scope, v_subject, v_limit, v_window in
    select * from (values
      ('ip'::text, p_source_ip_hash, p_ip_limit, p_ip_window_seconds),
      ('global'::text, 'all', p_global_limit, p_global_window_seconds)
    ) as limits(scope, subject, max_requests, window_seconds)
  loop
    v_bucket := date_bin(make_interval(secs => v_window), v_now,
      timestamptz '1970-01-01 00:00:00+00');
    insert into private.issue_ingest_rate_counters (
      scope, subject_hash, bucket_started_at, window_seconds,
      request_count, updated_at
    ) values (v_scope, v_subject, v_bucket, v_window, 1, v_now)
    on conflict (scope, subject_hash, bucket_started_at, window_seconds)
    do update set
      request_count = private.issue_ingest_rate_counters.request_count + 1,
      updated_at = excluded.updated_at
    returning request_count into v_count;

    if v_count > v_limit then
      v_retry := greatest(v_retry,
        ceil(extract(epoch from (
          v_bucket + make_interval(secs => v_window) - v_now
        )))::integer);
    end if;
  end loop;

  if v_retry > 0 then
    return query select 'rate_limited'::text, greatest(v_retry, 1);
  else
    return query select 'allowed'::text, 0;
  end if;
end;
$$;

revoke all on function public.preflight_issue_ingest_v1(
  text, integer, integer, integer, integer
) from public, anon, authenticated;
grant execute on function public.preflight_issue_ingest_v1(
  text, integer, integer, integer, integer
) to service_role;

comment on function public.preflight_issue_ingest_v1 is
  'Charges IP and global quotas before Edge Function body reads. Service role only.';

create or replace function public.ingest_issue_report_v1(
  p_event_id uuid,
  p_installation_id uuid,
  p_queued_at timestamptz,
  p_source_ip_hash text,
  p_installation_hash text,
  p_report jsonb,
  p_report_schema_version smallint,
  p_category text,
  p_severity text,
  p_installation_limit integer,
  p_installation_window_seconds integer
)
returns table (
  disposition text,
  receipt_id uuid,
  received_at timestamptz,
  retry_after_seconds integer
)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_now timestamptz := clock_timestamp();
  v_existing private.issue_reports%rowtype;
  v_bucket timestamptz;
  v_count integer;
begin
  if p_installation_limit < 1 then
    raise exception 'rate limits must be positive' using errcode = '22023';
  end if;
  if p_installation_window_seconds not between 1 and 86400 then
    raise exception 'rate windows must be between 1 and 86400 seconds' using errcode = '22023';
  end if;
  if p_source_ip_hash !~ '^[0-9a-f]{64}$' or p_installation_hash !~ '^[0-9a-f]{64}$' then
    raise exception 'invalid subject hash' using errcode = '22023';
  end if;

  -- Serialize retries for one event before charging quotas or inserting.
  perform pg_advisory_xact_lock(hashtextextended(p_event_id::text, 424242));
  select * into v_existing
    from private.issue_reports r
    where r.event_id = p_event_id;

  if found then
    if v_existing.installation_id <> p_installation_id then
      raise exception 'event_id belongs to another installation' using errcode = '23505';
    end if;
    return query select 'duplicate'::text, v_existing.receipt_id,
      v_existing.received_at, 0;
    return;
  end if;

  v_bucket := date_bin(
    make_interval(secs => p_installation_window_seconds),
    v_now,
    timestamptz '1970-01-01 00:00:00+00'
  );
  insert into private.issue_ingest_rate_counters (
    scope, subject_hash, bucket_started_at, window_seconds,
    request_count, updated_at
  ) values (
    'installation', p_installation_hash, v_bucket,
    p_installation_window_seconds, 1, v_now
  )
  on conflict (scope, subject_hash, bucket_started_at, window_seconds)
  do update set
    request_count = private.issue_ingest_rate_counters.request_count + 1,
    updated_at = excluded.updated_at
  returning request_count into v_count;

  if v_count > p_installation_limit then
    return query select 'rate_limited'::text, null::uuid, v_now,
      greatest(ceil(extract(epoch from (
        v_bucket + make_interval(secs => p_installation_window_seconds) - v_now
      )))::integer, 1);
    return;
  end if;

  return query
    insert into private.issue_reports (
      event_id, installation_id, queued_at, received_at, report_schema_version,
      category, severity, source_ip_hash, report
    ) values (
      p_event_id, p_installation_id, p_queued_at, v_now, p_report_schema_version,
      p_category, p_severity, p_source_ip_hash, p_report
    )
    returning 'accepted'::text, private.issue_reports.receipt_id,
      private.issue_reports.received_at, 0;
end;
$$;

revoke all on function public.ingest_issue_report_v1(
  uuid, uuid, timestamptz, text, text, jsonb, smallint, text, text,
  integer, integer
) from public, anon, authenticated;
grant execute on function public.ingest_issue_report_v1(
  uuid, uuid, timestamptz, text, text, jsonb, smallint, text, text,
  integer, integer
) to service_role;

comment on function public.ingest_issue_report_v1 is
  'Atomic idempotency, installation quota, and private issue insert after IP/global preflight. Service role only.';

create or replace function private.purge_expired_issue_ingest_v1(
  p_now timestamptz default clock_timestamp()
)
returns table (
  issue_reports_deleted bigint,
  rate_counters_deleted bigint
)
language sql
security invoker
set search_path = ''
as $$
  with deleted_reports as (
    delete from private.issue_reports
    where received_at < p_now - interval '90 days'
    returning 1
  ), deleted_counters as (
    delete from private.issue_ingest_rate_counters
    where updated_at < p_now - interval '7 days'
    returning 1
  )
  select
    (select count(*) from deleted_reports),
    (select count(*) from deleted_counters);
$$;

revoke all on function private.purge_expired_issue_ingest_v1(timestamptz)
  from public, anon, authenticated;
grant execute on function private.purge_expired_issue_ingest_v1(timestamptz)
  to service_role;

comment on function private.purge_expired_issue_ingest_v1 is
  'Deletes issue payloads after 90 days and rate counters after 7 days. Schedule daily in hosted Supabase Cron.';
