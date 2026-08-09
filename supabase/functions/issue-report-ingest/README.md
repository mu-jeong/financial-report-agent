# Issue report ingest Edge Function

This function is the only public write boundary for central issue reports. Callers send the named `desktop_ingest` publishable key in the `apikey` header. A publishable key identifies the public application component; it is not a user/device credential.

## Request and response

`POST /functions/v1/issue-report-ingest` with `Content-Type: application/json` and exactly these top-level keys:

```json
{
  "ingest_contract_version": 1,
  "event_id": "018f47a0-1111-7111-8111-111111111111",
  "installation_id": "018f47a0-2222-7222-8222-222222222222",
  "queued_at": "2026-08-09T00:00:00Z",
  "report": {
    "schema_version": 2,
    "report_contract_version": 2,
    "kind": "user_feedback",
    "report_target_type": "response",
    "source": "local_chat",
    "app_version": "1.0.0",
    "category": "오답/오류",
    "comment": "Bounded, user-approved comment",
    "consent": {
      "consent_version": 1,
      "include_comment": true,
      "include_selected_question": false,
      "include_selected_answer": false,
      "include_previous_turns": false
    },
    "observed": {
      "route": "vectordb",
      "status": "succeeded",
      "latency_ms": 1200,
      "result_count": 10,
      "citation_count": 3,
      "selected_question": null,
      "selected_answer": null
    },
    "diagnostics": {
      "stable_error_code": null,
      "exception_type": null,
      "stack_hash": null,
      "debug_hints": []
    },
    "privacy": {
      "redaction_version": 1,
      "removed_fields": ["raw_context", "credentials"]
    }
  }
}
```

New clients send exactly the minimized report keys shown above. Selected question and answer total at most 32 KiB and must match their explicit consent flags. Comment is at most 4 KiB and must match `include_comment`. Previous turns are rejected in Phase 1. Debug hints contain at most eight 512-byte strings (4 KiB total). Before parsing, every POST is charged against IP/global quotas and read through a 128 KiB bounded stream. The server also applies global object, array, string, depth, timestamp, identifier, category, source, target, version, and residual-sensitive-content checks before storage. Attachments, screenshots, raw documents, prompts, credentials, arbitrary fields, and challenge tokens are not part of v1.

For already queued legacy envelopes, the validator still accepts `id`, `created_at`, `thread_id`, `message_id`, and `job_id`, but removes them before storage. The database migration also deletes these local-only fields from existing rows and enforces the minimized stored shape. `app_version` remains because release-specific regressions require it; envelope-level `event_id`, `installation_id`, `queued_at`, and server `received_at` remain for idempotency, quota enforcement, retry freshness, and retention.

Success is exactly `{"ok":true,"disposition":"accepted|duplicate","receipt_id":"uuid","received_at":"RFC3339"}`. Contract failures return 4xx; transient storage/configuration failures return 503; quota failures return 429 plus `Retry-After`. Retrying must preserve `event_id`.

The handler never logs request bodies. The optional notification contains only receipt/event IDs, receipt time, category, and the server-assigned normal severity, and notification failure cannot fail an accepted report.
