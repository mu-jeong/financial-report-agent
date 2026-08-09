# Issue report ingest deployment

## Security boundary

`issue-report-ingest` is deployed with `verify_jwt=false` because current Supabase publishable keys are not JWTs. The handler uses the official, pinned `@supabase/server@1.4.1` wrapper with `auth: "publishable:desktop_ingest"`; clients send only `apikey: sb_publishable_...`. Never put a publishable key in `Authorization`, and never distribute a secret key, legacy `service_role`, database URL/password, IP-hash salt, or notification URL.

The migrations create `private.issue_reports` and `private.issue_ingest_rate_counters`, enable and force RLS, and revoke table access from PostgreSQL `PUBLIC`, `anon`, and `authenticated`. Migration `202608090003_minimize_issue_report_payload.sql` removes local report/thread/message/job identifiers and the duplicate report timestamp from existing rows, then installs a trigger and check constraint so they cannot be stored again. Only `service_role` can execute the preflight and atomic ingestion functions or access these tables. The Edge handler charges IP/global quotas before its bounded body read, then validates and minimizes the report before using its server-only admin client for storage. There is no public Data API table path.

## Deploy

The target is a hosted Supabase project, including the Free tier. Running Supabase, PostgreSQL, or Docker locally is not required. The CLI is used only to apply the version-controlled migration and deploy the Edge Function to that hosted project. A full local stack is optional for integration testing.

Prerequisites: a hosted project, a linked Supabase CLI, a named publishable API key called `desktop_ingest`, and a randomly generated HMAC secret of at least 32 UTF-8 bytes. Supabase API key names allow lowercase letters, numbers, and underscores, so do not replace the underscore with a hyphen. Use separate projects and HMAC secrets per environment.

```powershell
supabase login
supabase link --project-ref <project-ref>
supabase db push
supabase secrets set ISSUE_IP_HMAC_SECRET=<at-least-32-random-bytes>
supabase functions deploy issue-report-ingest --use-api
```

Apply `supabase db push` and deploy the updated Edge Function before releasing a desktop client that omits the deprecated local-only report fields. The updated Function remains compatible with already queued legacy envelopes and drops those fields before storage.

The platform supplies `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEYS`, and `SUPABASE_SECRET_KEYS`; do not copy their values into the repository. Confirm `SUPABASE_PUBLISHABLE_KEYS` contains the `desktop_ingest` name in the hosted Function environment before traffic is enabled.

Phase 1 is intentionally rate/quota/idempotency-only. It neither accepts nor verifies a Turnstile token: tokens are single-use and expire in about five minutes, while the local outbox may retry for days, and arbitrary local Streamlit hostnames do not provide a stable hostname boundary. If proof-of-human is added later, use a separate online grant endpoint that verifies a fresh token immediately and returns a short-lived, single-purpose signed upload grant. Do not persist a Turnstile token in the outbox or add it back to this replayable envelope.

Optional best-effort notification:

```powershell
supabase secrets set ISSUE_NOTIFICATION_WEBHOOK_URL=https://notifications.example/internal-hook
```

The receiver must treat the webhook as untrusted, authenticate at its own boundary if required, and expect only bounded metadata. The current v1 setting does not add an authorization header, so the URL should be an unguessable managed webhook capability or fronted by an independently authenticated relay.

## Quotas and timestamp window

Defaults are deliberately conservative and can be changed through Function secrets without a database migration:

| Variable | Default | Allowed |
| --- | ---: | ---: |
| `ISSUE_RATE_IP_LIMIT` | 20 | 1..1,000,000 |
| `ISSUE_RATE_IP_WINDOW_SECONDS` | 3600 | 1..86,400 |
| `ISSUE_RATE_INSTALLATION_LIMIT` | 10 | 1..1,000,000 |
| `ISSUE_RATE_INSTALLATION_WINDOW_SECONDS` | 3600 | 1..86,400 |
| `ISSUE_RATE_GLOBAL_LIMIT` | 5000 | 1..10,000,000 |
| `ISSUE_RATE_GLOBAL_WINDOW_SECONDS` | 3600 | 1..86,400 |
| `ISSUE_MAX_QUEUE_AGE_SECONDS` | 2,592,000 | 60..7,776,000 |

The IP/global preflight runs before content-type checks, bounded streaming body reads, JSON parsing, and schema validation, so malformed and oversized POSTs cannot bypass application counters. The installation counter and insert run atomically in the storage RPC; a duplicate is detected before installation charging and returns the original receipt. Preflight still charges duplicate POSTs because their body is intentionally not trusted before parsing. Denied attempts remain counted. The migrations provide `private.purge_expired_issue_ingest_v1()`, which deletes report payloads after 90 days and rate counters after 7 days, enable hosted Supabase Cron, and schedule `purge-expired-issue-ingest-v1` daily at 03:15 UTC. Verify the job is active before enabling production traffic.

## Smoke test and permission audit

Send the exact example from the Function README with the named publishable key in `apikey`. Repeat it unchanged and verify the first response is `accepted`, the second is `duplicate`, and both receipts match. Then change only `installation_id` while retaining `event_id` and verify the request does not succeed. Exercise malformed JSON, unknown fields, 128 KiB + 1, stale/future timestamps, invalid enums, and quota exhaustion.

Run these database checks from the SQL editor as an administrator:

```sql
select has_table_privilege('anon', 'private.issue_reports', 'select,insert,update,delete');
select has_table_privilege('authenticated', 'private.issue_reports', 'select,insert,update,delete');
select has_function_privilege(
  'anon',
  'public.preflight_issue_ingest_v1(text,integer,integer,integer,integer)',
  'execute'
);
select has_function_privilege(
  'anon',
  'public.ingest_issue_report_v1(uuid,uuid,timestamptz,text,text,jsonb,smallint,text,text,integer,integer)',
  'execute'
);
```

All results must be `false`. Also query `pg_policies` and confirm there are no policies on the two private tables. With an actual `anon` or authenticated session, direct table access and direct RPC execution must fail, while the Edge endpoint remains usable with the named publishable key.

### Hosted verification record (2026-08-09)

- The deployed `issue-report-ingest` Function reported `ACTIVE` with `verify_jwt=false`.
- Remote migrations `202608090001` through `202608090003` matched the local migration history.
- A fresh synthetic event returned `accepted`; its unchanged retry returned `duplicate` with the same receipt. An invalid publishable key returned HTTP 401. The real Python retry-only SQLite outbox path progressed from `queued` to `delivered`, then contained no report row or payload. No `.txt`/`.json` report artifact was created.
- The hosted stored report contained only the minimized report allowlist; local report/thread/message/job identifiers and the duplicate report timestamp were absent.
- `anon` and `authenticated` had no CRUD privileges on either private table and no execute privilege on either ingest RPC. Both tables had enabled and forced RLS, with no policies.
- The daily `purge-expired-issue-ingest-v1` Cron job was active at `15 3 * * *`; a manual retention run completed with zero eligible rows. Accepted synthetic reports were then removed, leaving no smoke-test report stored.

## Rotation and rollback

Publishable-key rotation: create a second named publishable key, temporarily update the Function auth name and clients in a staged rollout, verify traffic, then revoke the old key. Never remove the only accepted key before deployed clients migrate. A key leak does not grant table access, but rotate it to restore endpoint-level traffic control.

IP HMAC-secret rotation breaks continuity of IP and installation quota buckets. Change it only during a planned low-traffic window, retain no old secret, monitor aggregate 429/5xx rates, and accept that all pseudonymous quota identities start fresh. Notification secrets can be rotated by setting the replacement and verifying the integration before revoking the provider-side predecessor.

To stop ingestion without exposing data, undeploy/disable the Function or set the global limit to a deliberately low positive value and return clients to bounded backoff. Do not roll back the privacy/grant migration while stored reports exist. Database rollback or report deletion is a separate destructive operation requiring retention and audit approval.

## Local validation limits

Run `deno test --config supabase/functions/issue-report-ingest/deno.json supabase/functions/issue-report-ingest/validation_test.ts supabase/functions/issue-report-ingest/index_test.ts` when Deno is installed. `supabase db reset` is an optional local integration test and is the only step here that requires Docker. Hosted verification requires project credentials and the named publishable key; the hosted gateway supplies `CF-Connecting-IP`. No production secret belongs in local test output or version control.
