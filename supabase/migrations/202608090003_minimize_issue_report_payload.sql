create or replace function private.minimize_issue_report_payload_v1()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.report := new.report - array[
    'id',
    'created_at',
    'thread_id',
    'message_id',
    'job_id'
  ]::text[];
  return new;
end;
$$;

revoke all on function private.minimize_issue_report_payload_v1()
  from public, anon, authenticated;
grant execute on function private.minimize_issue_report_payload_v1()
  to service_role;

create trigger minimize_issue_report_payload_v1
before insert or update of report on private.issue_reports
for each row execute function private.minimize_issue_report_payload_v1();

update private.issue_reports
set report = report - array[
  'id',
  'created_at',
  'thread_id',
  'message_id',
  'job_id'
]::text[]
where report ?| array[
  'id',
  'created_at',
  'thread_id',
  'message_id',
  'job_id'
]::text[];

alter table private.issue_reports
  add constraint issue_reports_excludes_local_correlation_fields
  check (not (report ?| array[
    'id',
    'created_at',
    'thread_id',
    'message_id',
    'job_id'
  ]::text[]));

comment on function private.minimize_issue_report_payload_v1() is
  'Removes local-only report, thread, message, job, and duplicate timestamp fields before issue storage.';
