import {
  fetch,
  fetchWithAuthentication,
  handleOperatorRequest,
  type OperatorRpcClient,
} from "./index.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

const ACTOR = "018f47c2-a232-7c3e-8d91-8c4f3a2b1c0d";
const ISSUE = "018f47c2-a232-7c3e-8d91-8c4f3a2b1c0e";

function client(
  responder: (name: string, args: Record<string, unknown>) => RpcResponse,
  calls: Array<{ name: string; args: Record<string, unknown> }>,
): OperatorRpcClient {
  return {
    rpc(name, args) {
      calls.push({ name, args });
      return Promise.resolve(responder(name, args));
    },
  };
}

type RpcResponse = { data: unknown; error: unknown };

Deno.test("missing user JWT receives stable 401", async () => {
  const response = await fetch(
    new Request("https://example.test/issue-report-operator/issues"),
  );
  assert(response.status === 401, "missing JWT was accepted");
  assert(
    JSON.stringify(await response.json()) ===
      JSON.stringify({ ok: false, code: "unauthorized" }),
    "401 contract changed",
  );
});

Deno.test("expected authentication rejection receives stable 401", async () => {
  const response = await fetchWithAuthentication(
    new Request("https://example.test/issue-report-operator/issues", {
      headers: { authorization: "Bearer fixture-token" },
    }),
    () => Promise.reject({ status: 401, code: "invalid_jwt" }),
  );

  assert(response.status === 401, "authentication rejection was not normalized");
  assert(
    JSON.stringify(await response.json()) ===
      JSON.stringify({ ok: false, code: "unauthorized" }),
    "authentication rejection contract changed",
  );
});

Deno.test("unexpected authentication failure is logged and returned as 5xx", async () => {
  const messages: string[] = [];
  const original = console.error;
  console.error = (...values: unknown[]) => messages.push(values.join(" "));
  try {
    const response = await fetchWithAuthentication(
      new Request("https://example.test/issue-report-operator/issues", {
        headers: { authorization: "Bearer fixture-token" },
      }),
      () => Promise.reject(new Error("auth provider unavailable")),
    );
    const body = await response.json();

    assert(response.status >= 500, "unexpected authentication failure became 4xx");
    assert(body.code === "authentication_unavailable", "5xx code changed");
    assert(typeof body.request_id === "string", "request id is missing");
    assert(
      messages.some((message) => message.includes(body.request_id)),
      "failure was not logged",
    );
  } finally {
    console.error = original;
  }
});

Deno.test("active admin lists summary issues with the server actor id", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(
      "https://example.test/issue-report-operator/issues?state=OPEN&limit=20",
    ),
    client(
      (name) =>
        name === "monitoring_check_admin_v1"
          ? { data: true, error: null }
          : { data: [{ issue_id: ISSUE, state: "OPEN" }], error: null },
      calls,
    ),
    ACTOR,
  );

  assert(response.status === 200, "list request failed");
  assert(
    response.headers.get("cache-control") === "no-store",
    "response is cacheable",
  );
  assert(calls.length === 2, "unexpected RPC count");
  assert(
    calls[0].name === "monitoring_check_admin_v1",
    "membership was not rechecked",
  );
  assert(
    calls[1].name === "monitoring_list_issues_v1",
    "list RPC was not called",
  );
  assert(
    calls[1].args.p_actor_user_id === ACTOR,
    "actor was not server injected",
  );
});

Deno.test("list accepts every classified and legacy issue state", async () => {
  for (
    const state of ["OPEN", "IN_PROGRESS", "CLOSED", "RESOLVED", "NOT_ISSUE"]
  ) {
    const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
    const response = await handleOperatorRequest(
      new Request(
        `https://example.test/issue-report-operator/issues?state=${state}&limit=20`,
      ),
      client(
        (name) =>
          name === "monitoring_check_admin_v1"
            ? { data: true, error: null }
            : { data: [], error: null },
        calls,
      ),
      ACTOR,
    );

    assert(response.status === 200, `${state} list request failed`);
    assert(calls[1].args.p_state === state, `${state} filter was changed`);
  }
});

Deno.test("named lifecycle actions map to explicit target states", async () => {
  const actions: Record<string, string> = {
    start: "IN_PROGRESS",
    resolve: "RESOLVED",
    dismiss: "NOT_ISSUE",
    reopen: "OPEN",
  };
  for (const [action, targetState] of Object.entries(actions)) {
    const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
    const response = await handleOperatorRequest(
      new Request(
        `https://example.test/issue-report-operator/issues/${ISSUE}/${action}`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            expected_record_revision: 3,
            reason: "operator decision",
          }),
        },
      ),
      client(
        (name) =>
          name === "monitoring_check_admin_v1" ? { data: true, error: null } : {
            data: {
              disposition: "updated",
              issue_id: ISSUE,
              state: targetState,
              record_revision: 4,
            },
            error: null,
          },
        calls,
      ),
      ACTOR,
    );

    assert(response.status === 200, `${action} action failed`);
    assert(
      calls[1].name === "monitoring_transition_issue_v1",
      `${action} used the wrong RPC`,
    );
    assert(
      calls[1].args.p_target_state === targetState,
      `${action} target changed`,
    );
    assert(
      calls[1].args.p_expected_record_revision === 3,
      `${action} omitted CAS`,
    );
    assert(
      calls[1].args.p_reason === "operator decision",
      `${action} reason changed`,
    );
  }
});

Deno.test("legacy close action is retired", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(
      `https://example.test/issue-report-operator/issues/${ISSUE}/close`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          expected_record_revision: 3,
          reason: "unclassified closure",
        }),
      },
    ),
    client(() => ({ data: true, error: null }), calls),
    ACTOR,
  );

  assert(response.status === 405, "legacy close action is still writable");
  assert(calls.length === 1, "retired close action reached the transition RPC");
  assert(
    calls[0].name === "monitoring_check_admin_v1",
    "membership was not rechecked",
  );
});

Deno.test("inactive admin receives stable 403 before issue access", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(`https://example.test/issue-report-operator/issues/${ISSUE}`),
    client(() => ({ data: null, error: { code: "42501" } }), calls),
    ACTOR,
  );

  assert(response.status === 403, "inactive admin was not forbidden");
  assert(
    JSON.stringify(await response.json()) ===
      JSON.stringify({ ok: false, code: "forbidden" }),
    "403 contract changed",
  );
  assert(calls.length === 1, "forbidden request reached issue data");
});

Deno.test("explicit raw action uses only the audited raw RPC", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(
      `https://example.test/issue-report-operator/issues/${ISSUE}/raw`,
      { method: "POST" },
    ),
    client(
      (name) =>
        name === "monitoring_check_admin_v1" ? { data: true, error: null } : {
          data: {
            disposition: "found",
            issue_id: ISSUE,
            record_revision: 1,
            raw_report: { comment: "consented" },
          },
          error: null,
        },
      calls,
    ),
    ACTOR,
  );

  assert(response.status === 200, "raw action failed");
  assert(
    calls[1].name === "monitoring_view_issue_raw_v1",
    "raw audit RPC was bypassed",
  );
  assert(
    calls[1].args.p_actor_user_id === ACTOR,
    "raw actor was client controlled",
  );
});

Deno.test("explicit raw action accepts a hosted zero-byte request stream", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const request = new Request(
    `https://example.test/issue-report-operator/issues/${ISSUE}/raw`,
    { method: "POST", body: new Uint8Array() },
  );
  assert(
    request.body !== null,
    "test request did not model the hosted body stream",
  );

  const response = await handleOperatorRequest(
    request,
    client(
      (name) =>
        name === "monitoring_check_admin_v1" ? { data: true, error: null } : {
          data: {
            disposition: "found",
            issue_id: ISSUE,
            record_revision: 1,
            raw_report: { comment: "consented" },
          },
          error: null,
        },
      calls,
    ),
    ACTOR,
  );

  assert(response.status === 200, "zero-byte raw action failed");
  assert(
    calls.length === 2,
    "zero-byte raw action used an unexpected RPC path",
  );
  assert(
    calls[1].name === "monitoring_view_issue_raw_v1",
    "raw audit RPC was bypassed",
  );
});

Deno.test("explicit raw action rejects request content before the audited RPC", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(
      `https://example.test/issue-report-operator/issues/${ISSUE}/raw`,
      {
        method: "POST",
        body: "{}",
      },
    ),
    client(() => ({ data: true, error: null }), calls),
    ACTOR,
  );

  assert(response.status === 422, "non-empty raw action body was accepted");
  const body = await response.json();
  assert(
    body.code === "invalid_request",
    "non-empty body error contract changed",
  );
  assert(calls.length === 1, "rejected raw action reached the audited RPC");
  assert(
    calls[0].name === "monitoring_check_admin_v1",
    "membership was not rechecked",
  );
});

Deno.test("expired raw report returns stable 410 while summary remains", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(
      `https://example.test/issue-report-operator/issues/${ISSUE}/raw`,
      { method: "POST" },
    ),
    client(
      (name) =>
        name === "monitoring_check_admin_v1" ? { data: true, error: null } : {
          data: {
            disposition: "raw_unavailable",
            issue_id: ISSUE,
            reason: "retained_summary_only",
          },
          error: null,
        },
      calls,
    ),
    ACTOR,
  );

  assert(response.status === 410, "expired raw report must return 410");
  const body = await response.json();
  assert(body.code === "raw_unavailable", "410 contract changed");
});

Deno.test("stale resolve revision returns stable 409", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(
      `https://example.test/issue-report-operator/issues/${ISSUE}/resolve`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          expected_record_revision: 1,
          reason: "verified",
        }),
      },
    ),
    client(
      (name) =>
        name === "monitoring_check_admin_v1" ? { data: true, error: null } : {
          data: { disposition: "conflict", record_revision: 2, state: "OPEN" },
          error: null,
        },
      calls,
    ),
    ACTOR,
  );

  assert(response.status === 409, "stale revision did not conflict");
  const body = await response.json();
  assert(body.code === "revision_conflict", "409 contract changed");
  assert(
    calls[1].args.p_expected_record_revision === 1,
    "CAS revision was omitted",
  );
});

Deno.test("invalid actor is rejected before any privileged RPC", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request("https://example.test/issue-report-operator/issues"),
    client(() => ({ data: true, error: null }), calls),
    "not-a-user-id",
  );

  assert(response.status === 401, "invalid actor was accepted");
  assert(calls.length === 0, "invalid actor reached privileged RPC");
});

Deno.test("control record reconciliation is strict, CAS-bound, and server attributed", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const record = {
    record_kind: "RUN",
    record_id: "run_0123456789abcdef0123456789abcdef",
    lifecycle_status: "RUNNING",
    content_digest: "a".repeat(64),
    availability: null,
    references: {
      fixed_snapshot_revision_id: "snapshot_0123456789abcdef0123456789abcdef",
      case_contract_id: "b".repeat(64),
      release_manifest_id: "release_0123456789abcdef0123456789abcdef",
    },
    attributes: {
      side: "CANDIDATE",
      validity: "UNKNOWN",
      evidence_qualifier: "EXACT",
    },
  };
  const response = await handleOperatorRequest(
    new Request(
      `https://example.test/issue-report-operator/issues/${ISSUE}/control`,
      {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ expected_record_revision: 1, record }),
      },
    ),
    client(
      (name) =>
        name === "monitoring_check_admin_v1" ? { data: true, error: null } : {
          data: {
            disposition: "updated",
            record_revision: 2,
            content_digest: "a".repeat(64),
          },
          error: null,
        },
      calls,
    ),
    ACTOR,
  );

  assert(response.status === 200, "control reconciliation failed");
  assert(
    calls[1].name === "monitoring_reconcile_control_record_v1",
    "wrong RPC called",
  );
  assert(
    calls[1].args.p_actor_user_id === ACTOR,
    "actor was client controlled",
  );
  assert(
    calls[1].args.p_expected_record_revision === 1,
    "CAS revision was omitted",
  );
  assert(calls[1].args.p_record_kind === "RUN", "record kind changed");
});

Deno.test("control record rejects unknown fields before storage", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(
      `https://example.test/issue-report-operator/issues/${ISSUE}/control`,
      {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          expected_record_revision: 0,
          record: {
            record_kind: "FIXTURE",
            record_id: "fixture_0123456789abcdef0123456789abcdef",
            lifecycle_status: "READY",
            content_digest: "a".repeat(64),
            availability: null,
            references: {},
            attributes: {},
            local_path: "C:/secret/fixture.json",
          },
        }),
      },
    ),
    client(() => ({ data: true, error: null }), calls),
    ACTOR,
  );

  assert(response.status === 422, "unknown field was accepted");
  assert(calls.length === 1, "invalid body reached storage RPC");
});

Deno.test("control record rejects incomplete audit metadata before storage", async () => {
  const invalidRecords = [
    {
      record_kind: "CASE",
      record_id: "case_0123456789abcdef0123456789abcdef",
      lifecycle_status: "READY",
      content_digest: "b".repeat(64),
      availability: null,
      references: {
        fixture_revision_id: "fixture_0123456789abcdef0123456789abcdef",
        fixed_snapshot_revision_id:
          "snapshot_0123456789abcdef0123456789abcdef",
      },
      attributes: {},
    },
    {
      record_kind: "COMPARISON",
      record_id: "comparison_0123456789abcdef0123456789abcdef",
      lifecycle_status: "CREATED",
      content_digest: "c".repeat(64),
      availability: null,
      references: {},
      attributes: { verdict: "INCONCLUSIVE" },
    },
    {
      record_kind: "FIXTURE",
      record_id: "fixture_0123456789abcdef0123456789abcdef",
      lifecycle_status: "READY",
      content_digest: "d".repeat(64),
      availability: "AVAILABLE",
      references: {},
      attributes: {},
    },
  ];

  for (const record of invalidRecords) {
    const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
    const response = await handleOperatorRequest(
      new Request(
        `https://example.test/issue-report-operator/issues/${ISSUE}/control`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ expected_record_revision: 0, record }),
        },
      ),
      client(() => ({ data: true, error: null }), calls),
      ACTOR,
    );

    assert(response.status === 422, `${record.record_kind} metadata was accepted`);
    assert(calls.length === 1, "invalid metadata reached storage RPC");
  }
});

Deno.test("immutable comparison mismatch returns stable conflict", async () => {
  const calls: Array<{ name: string; args: Record<string, unknown> }> = [];
  const response = await handleOperatorRequest(
    new Request(
      `https://example.test/issue-report-operator/issues/${ISSUE}/control`,
      {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          expected_record_revision: 1,
          record: {
            record_kind: "COMPARISON",
            record_id: "comparison_0123456789abcdef0123456789abcdef",
            lifecycle_status: "CREATED",
            content_digest: "c".repeat(64),
            availability: null,
            references: { case_contract_id: "b".repeat(64) },
            attributes: { verdict: "INCONCLUSIVE" },
          },
        }),
      },
    ),
    client(
      (name) =>
        name === "monitoring_check_admin_v1" ? { data: true, error: null } : {
          data: {
            disposition: "immutable_conflict",
            record_revision: 1,
            content_digest: "d".repeat(64),
          },
          error: null,
        },
      calls,
    ),
    ACTOR,
  );

  assert(response.status === 409, "immutable mismatch was accepted");
  const body = await response.json();
  assert(
    body.code === "immutable_conflict",
    "immutable conflict contract changed",
  );
});
