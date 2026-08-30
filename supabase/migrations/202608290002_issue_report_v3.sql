-- Add bounded, optional case diagnostics without breaking pending v2 outbox rows.

alter table private.issue_reports
  drop constraint issue_reports_report_schema_version_check;

alter table private.issue_reports
  add constraint issue_reports_report_schema_version_check
  check (report_schema_version in (2, 3));

alter table private.issue_reports
  add constraint issue_reports_report_version_matches_payload
  check (
    report ->> 'schema_version' = report_schema_version::text
    and report ->> 'report_contract_version' = report_schema_version::text
  );

alter table private.issue_reports
  add constraint issue_reports_v3_has_release_identity
  check (
    report_schema_version = 2
    or (
      report ? 'reported_release_id'
      and report ->> 'reported_release_id' ~ '^release-[0-9A-Za-z][0-9A-Za-z.+_-]{0,71}$'
    )
  );

comment on constraint issue_reports_report_schema_version_check
  on private.issue_reports is
  'Accepts retrying v2 envelopes while current clients submit strict v3 reports.';
