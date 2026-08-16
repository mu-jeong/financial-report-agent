import { validateEnvelope, ValidationError } from "./validation.ts";

const now = Date.parse("2026-08-09T00:00:00Z");
function valid() {
  return {
    ingest_contract_version: 1,
    event_id: "018f47a0-1111-7111-8111-111111111111",
    installation_id: "018f47a0-2222-7222-8222-222222222222",
    queued_at: "2026-08-08T23:59:00Z",
    report: {
      schema_version: 2,
      report_contract_version: 2,
      kind: "user_feedback",
      report_target_type: "response",
      source: "local_chat",
      app_version: "1.0.0",
      category: "오답/오류",
      comment: "The answer omitted a cited filing.",
      consent: {
        consent_version: 1,
        include_comment: true,
        include_selected_question: false,
        include_selected_answer: false,
        include_previous_turns: false,
      },
      observed: {
        route: "vectordb",
        status: "succeeded",
        latency_ms: 1200,
        result_count: 10,
        citation_count: 3,
        selected_question: null,
        selected_answer: null,
      },
      diagnostics: {
        stable_error_code: null,
        exception_type: null,
        stack_hash: null,
        debug_hints: [],
      },
      privacy: {
        redaction_version: 1,
        removed_fields: ["raw_context", "credentials"],
      },
    },
  };
}

function expectCode(value: unknown, code: string) {
  try {
    validateEnvelope(value, now);
    throw new Error(`expected ${code}`);
  } catch (error) {
    if (!(error instanceof ValidationError) || error.code !== code) throw error;
  }
}

Deno.test("accepts the minimized canonical version 2 report subset", () => {
  const result = validateEnvelope(valid(), now);
  if (result.report.category !== "오답/오류") {
    throw new Error("unexpected category");
  }
});

Deno.test("accepts queued legacy identifiers but removes them before storage", () => {
  const legacy = valid();
  Object.assign(legacy.report, {
    id: "report-1",
    created_at: "2026-08-08T23:58:00Z",
    thread_id: "thread-1",
    message_id: 42,
    job_id: "job-1",
  });

  const result = validateEnvelope(legacy, now);
  for (
    const field of [
      "id",
      "created_at",
      "thread_id",
      "message_id",
      "job_id",
    ]
  ) {
    if (field in result.report) {
      throw new Error(`${field} was not removed before storage`);
    }
  }
});

Deno.test("rejects unknown envelope and report fields", () => {
  expectCode({ ...valid(), surprise: true }, "unknown_field");
  const report = valid();
  (report.report as Record<string, unknown>).surprise = true;
  expectCode(report, "unknown_field");
});

Deno.test("rejects a missing canonical report field", () => {
  const report = valid();
  delete (report.report as Partial<typeof report.report>).privacy;
  expectCode(report, "missing_field");
});

Deno.test("rejects oversized comments", () => {
  const report = valid();
  report.report.comment = "x".repeat(4 * 1024 + 1);
  expectCode(report, "invalid_string");
});

Deno.test("rejects residual credentials and personal identifiers", () => {
  const unsafeValues = [
    "sb_secret_FAKE1234567890abcdefghijklmnop",
    "ISSUE_IP_HMAC_SECRET=abcdefghijklmnopqrstuvwxyz123456",
    "SUPABASE_SECRET_KEYS=sb_secret_FAKEabcdefghijklmnop",
    "ISSUE_NOTIFICATION_WEBHOOK_URL=https://hooks.example/secret-capability",
    "GITHUB_TOKEN=abcdefghijklmnopqrstuvwxyz123456",
    "HF_TOKEN: abcdefghijklmnopqrstuvwxyz123456",
    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature12345",
    "900101-1234567",
    "person@example.com",
    "C:\\Users\\alice\\private.txt",
    "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
  ];
  for (const unsafe of unsafeValues) {
    const report = valid();
    report.report.comment = `before ${unsafe} after`;
    expectCode(report, "sensitive_content");
  }
});

Deno.test("rejects selected content without matching consent", () => {
  const report = valid();
  (report.report.observed as Record<string, unknown>).selected_question =
    "Why?";
  expectCode(report, "consent_mismatch");
});

Deno.test("accepts a consented bounded turn trace", () => {
  const report = valid();
  report.report.consent.include_selected_question = true;
  report.report.consent.include_previous_turns = true;
  const observed = report.report.observed as Record<string, unknown>;
  observed.selected_question = "첫번째 리포트를 다시 확인해줘";
  observed.result_count_kind = "row";
  observed.turn_trace = [
    {
      turn_index: 1,
      question: "지난주 리포트를 정리해줘",
      rewritten_query: "지난주 리포트 정리",
      route: "vectordb",
      status: "succeeded",
      followup_scope_intent: false,
      scope_source: null,
      scope_reason: null,
      matched_document_rank: null,
      route_hint: null,
      has_vector_intent: true,
      search_filters: {
        report_date: "2026-08-01",
        report_type: "industry",
      },
      prior_search_filters: {},
      prior_file_names: [],
      selected_file_names: ["safe-report.pdf"],
      result_count: 1,
      result_count_kind: "document",
    },
  ];

  validateEnvelope(report, now);
});

Deno.test("rejects previous-turn consent without a turn trace", () => {
  const report = valid();
  report.report.consent.include_previous_turns = true;
  expectCode(report, "consent_mismatch");
});

Deno.test("rejects stale and future timestamps", () => {
  const stale = valid();
  stale.queued_at = "2025-01-01T00:00:00Z";
  expectCode(stale, "expired_timestamp");
  const future = valid();
  future.queued_at = "2026-08-09T00:11:00Z";
  expectCode(future, "future_timestamp");
});
