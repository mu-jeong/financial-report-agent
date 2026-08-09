create extension if not exists pg_cron;

select cron.schedule(
  'purge-expired-issue-ingest-v1',
  '15 3 * * *',
  $cron$select * from private.purge_expired_issue_ingest_v1();$cron$
);
