# Supabase health check Edge Function

This function gives the scheduled GitHub Actions workflow a purpose-limited way to perform one real, read-only database query. It does not write application data and does not expose a Supabase secret or service-role key to GitHub.

## Contract

Send `GET /functions/v1/supabase-health-check` with `x-healthcheck-token` set to the value of the Edge Function secret `HEALTHCHECK_TOKEN`. Do not put the token in the URL, source, logs, or tracked environment files.

A successful request returns `200`, `Cache-Control: no-store`, and:

```json
{"ok":true,"database_time":"2026-08-09T00:17:00+00:00"}
```

Missing or invalid caller tokens return the same `401` response without accessing the database. A missing server secret or an RPC failure returns a sanitized `503`. Methods other than `GET` return `405` with `Allow: GET`.

The handler calls `public.project_healthcheck_v1()` exactly once after authentication. That RPC is granted only to `service_role` and returns PostgreSQL's current time without changing any table.

## Local verification

```sh
deno test --config supabase/functions/supabase-health-check/deno.json supabase/functions/supabase-health-check/index_test.ts
```

Deployment and secret registration are separate production operations. Configure the same random value of at least 32 bytes as the Supabase `HEALTHCHECK_TOKEN` secret and the GitHub `SUPABASE_HEALTHCHECK_TOKEN` secret; never commit the value.
