import {
  default as healthCheckFunction,
  handleHealthCheckRequest,
  type HealthCheckRpcClient,
} from "./index.ts";

function assert(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(message);
}

function assertNoStore(response: Response): void {
  assert(
    response.headers.get("cache-control") === "no-store",
    `status ${response.status} response may be cached`,
  );
}

function rpcClient(
  response: { data: unknown; error: unknown },
  calls: string[],
): HealthCheckRpcClient {
  return {
    rpc(name) {
      calls.push(name);
      return Promise.resolve(response);
    },
  };
}

const token = "t".repeat(32);
const configuredEnvironment = (name: string) =>
  name === "HEALTHCHECK_TOKEN" ? token : undefined;

Deno.test("valid token performs one RPC and returns database time", async () => {
  const calls: string[] = [];
  const response = await handleHealthCheckRequest(
    new Request("https://example.test/health", {
      headers: { "x-healthcheck-token": token },
    }),
    rpcClient({
      data: [{ database_time: "2026-08-09T00:17:00+00:00" }],
      error: null,
    }, calls),
    configuredEnvironment,
  );

  assert(response.status === 200, "valid request did not succeed");
  assertNoStore(response);
  assert(
    JSON.stringify(await response.json()) ===
      JSON.stringify({
        ok: true,
        database_time: "2026-08-09T00:17:00+00:00",
      }),
    "success response contract changed",
  );
  assert(
    calls.join(",") === "project_healthcheck_v1",
    "RPC was not called exactly once",
  );
});

Deno.test("missing and invalid tokens return the same 401 without RPC", async () => {
  for (const providedToken of [undefined, "wrong-token"]) {
    const calls: string[] = [];
    const headers = providedToken === undefined
      ? undefined
      : { "x-healthcheck-token": providedToken };
    const response = await handleHealthCheckRequest(
      new Request("https://example.test/health", { headers }),
      rpcClient({ data: [], error: null }, calls),
      configuredEnvironment,
    );

    assert(response.status === 401, "unauthorized request was accepted");
    assertNoStore(response);
    assert(
      JSON.stringify(await response.json()) ===
        JSON.stringify({ ok: false, code: "unauthorized" }),
      "unauthorized responses are distinguishable",
    );
    assert(calls.length === 0, "unauthorized request reached the database");
  }
});

Deno.test("default fetch rejects OPTIONS and POST before wrapper context", async () => {
  for (const method of ["OPTIONS", "POST"]) {
    const response = await healthCheckFunction.fetch(
      new Request("https://example.test/health", { method }),
    );

    assert(
      response.status === 405,
      `${method} did not return method not allowed`,
    );
    assertNoStore(response);
    assert(
      response.headers.get("allow") === "GET",
      `${method} Allow header is incorrect`,
    );
    assert(
      JSON.stringify(await response.json()) ===
        JSON.stringify({ ok: false, code: "method_not_allowed" }),
      `${method} response contract changed`,
    );
  }
});

Deno.test("missing server token returns 503 without RPC", async () => {
  const calls: string[] = [];
  const response = await handleHealthCheckRequest(
    new Request("https://example.test/health"),
    rpcClient({ data: [], error: null }, calls),
    () => undefined,
  );

  assert(response.status === 503, "missing server token was not rejected");
  assertNoStore(response);
  assert(calls.length === 0, "misconfigured request reached the database");
});

Deno.test("server token shorter than 32 UTF-8 bytes returns 503 without RPC", async () => {
  const calls: string[] = [];
  const shortToken = "a".repeat(31);
  const response = await handleHealthCheckRequest(
    new Request("https://example.test/health", {
      headers: { "x-healthcheck-token": shortToken },
    }),
    rpcClient({ data: [], error: null }, calls),
    (name) => name === "HEALTHCHECK_TOKEN" ? shortToken : undefined,
  );

  assert(response.status === 503, "short server token was accepted");
  assertNoStore(response);
  assert(calls.length === 0, "short server token request reached the database");
});

Deno.test("RPC failure returns a sanitized 503", async () => {
  const calls: string[] = [];
  const sensitiveMessage = "database password and internal error details";
  const response = await handleHealthCheckRequest(
    new Request("https://example.test/health", {
      headers: { "x-healthcheck-token": token },
    }),
    rpcClient({ data: null, error: new Error(sensitiveMessage) }, calls),
    configuredEnvironment,
  );
  const body = await response.text();

  assert(response.status === 503, "RPC failure did not return unavailable");
  assertNoStore(response);
  assert(!body.includes(sensitiveMessage), "RPC error details leaked");
  assert(
    calls.join(",") === "project_healthcheck_v1",
    "RPC failure did not make exactly one call",
  );
});

Deno.test("empty or malformed RPC data returns 503", async () => {
  for (
    const data of [
      [],
      [{}],
      [{ database_time: "" }],
      [{ database_time: "not-a-time" }],
      [
        { database_time: "2026-08-09T00:17:00+00:00" },
        { database_time: "2026-08-09T00:17:01+00:00" },
      ],
      null,
    ]
  ) {
    const calls: string[] = [];
    const response = await handleHealthCheckRequest(
      new Request("https://example.test/health", {
        headers: { "x-healthcheck-token": token },
      }),
      rpcClient({ data, error: null }, calls),
      configuredEnvironment,
    );

    assert(response.status === 503, "malformed RPC data was accepted");
    assertNoStore(response);
    assert(calls.length === 1, "malformed response required extra RPC calls");
  }
});

Deno.test("rejected RPC call returns a sanitized 503", async () => {
  const calls: string[] = [];
  const sensitiveMessage = "internal connection string";
  const client: HealthCheckRpcClient = {
    rpc(name) {
      calls.push(name);
      return Promise.reject(new Error(sensitiveMessage));
    },
  };
  const response = await handleHealthCheckRequest(
    new Request("https://example.test/health", {
      headers: { "x-healthcheck-token": token },
    }),
    client,
    configuredEnvironment,
  );
  const body = await response.text();

  assert(response.status === 503, "rejected RPC did not return unavailable");
  assertNoStore(response);
  assert(!body.includes(sensitiveMessage), "rejected RPC details leaked");
  assert(calls.length === 1, "rejected RPC was retried unexpectedly");
});

Deno.test("RPC errors do not leak caller tokens or database details to logs", async () => {
  const calls: string[] = [];
  const sensitiveDetail = "postgres host and password";
  const logs: string[] = [];
  const originalConsoleError = console.error;
  console.error = (...args: unknown[]) => {
    logs.push(args.map(String).join(" "));
  };

  try {
    const response = await handleHealthCheckRequest(
      new Request("https://example.test/health", {
        headers: { "x-healthcheck-token": token },
      }),
      rpcClient({
        data: null,
        error: new Error(`caller=${token}; detail=${sensitiveDetail}`),
      }, calls),
      configuredEnvironment,
    );

    assert(response.status === 503, "RPC failure did not return unavailable");
  } finally {
    console.error = originalConsoleError;
  }

  const capturedLogs = logs.join("\n");
  assert(logs.length === 1, "RPC failure did not emit one structured log");
  assert(!capturedLogs.includes(token), "caller token leaked to logs");
  assert(
    !capturedLogs.includes(sensitiveDetail),
    "database error details leaked to logs",
  );
  assert(calls.length === 1, "logging test made an unexpected RPC call");
});
