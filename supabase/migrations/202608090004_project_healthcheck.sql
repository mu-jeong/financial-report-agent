create or replace function public.project_healthcheck_v1()
returns table (
  database_time timestamptz
)
language sql
security invoker
set search_path = ''
as $$
  select clock_timestamp() as database_time;
$$;

revoke all on function public.project_healthcheck_v1()
  from public, anon, authenticated;
grant execute on function public.project_healthcheck_v1()
  to service_role;

comment on function public.project_healthcheck_v1 is
  'Returns the current database time without modifying application data. Service role only.';
