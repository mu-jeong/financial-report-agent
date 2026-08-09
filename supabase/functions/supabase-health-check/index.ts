import { withSupabase } from "@supabase/server";

type Database = {
  public: {
    Tables: Record<string, never>;
    Views: Record<string, never>;
    Functions: {
      project_healthcheck_v1: {
        Args: Record<PropertyKey, never>;
        Returns: Array<{ database_time: string }>;
      };
    };
    Enums: Record<string, never>;
    CompositeTypes: Record<string, never>;
  };
};

type RpcResponse = { data: unknown; error: unknown };
export type HealthCheckRpcClient = {
  rpc(name: "project_healthcheck_v1"): PromiseLike<RpcResponse>;
};
export type EnvironmentReader = (name: string) => string | undefined;

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};
const MINIMUM_TOKEN_BYTES = 32;

function json(
  status: number,
  body: Record<string, unknown>,
  headers: HeadersInit = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...headers },
  });
}

function tokensMatch(provided: string | null, expected: string): boolean {
  if (provided === null) return false;
  const encoder = new TextEncoder();
  const providedBytes = encoder.encode(provided);
  const expectedBytes = encoder.encode(expected);
  let difference = providedBytes.length ^ expectedBytes.length;
  const length = Math.max(providedBytes.length, expectedBytes.length);
  for (let index = 0; index < length; index += 1) {
    difference |= (providedBytes[index] ?? 0) ^ (expectedBytes[index] ?? 0);
  }
  return difference === 0;
}

function errorType(error: unknown): string {
  if (error instanceof Error) return error.name;
  if (error === null) return "null";
  return typeof error;
}

function logFailure(requestId: string, stage: string, error: unknown): void {
  console.error(JSON.stringify({
    request_id: requestId,
    stage,
    error_type: errorType(error),
  }));
}

export async function handleHealthCheckRequest(
  req: Request,
  supabaseAdmin: HealthCheckRpcClient,
  readEnvironment: EnvironmentReader = (name) => Deno.env.get(name),
): Promise<Response> {
  const requestId = crypto.randomUUID();
  const expectedToken = readEnvironment("HEALTHCHECK_TOKEN");
  if (
    !expectedToken ||
    new TextEncoder().encode(expectedToken).byteLength < MINIMUM_TOKEN_BYTES
  ) {
    logFailure(requestId, "configuration", new Error("missing secret"));
    return json(503, {
      ok: false,
      code: "healthcheck_not_configured",
      request_id: requestId,
    });
  }

  if (!tokensMatch(req.headers.get("x-healthcheck-token"), expectedToken)) {
    return json(401, { ok: false, code: "unauthorized" });
  }

  try {
    const { data, error } = await supabaseAdmin.rpc("project_healthcheck_v1");
    if (error) throw error;
    const databaseTime = Array.isArray(data) &&
        data.length === 1 &&
        typeof data[0]?.database_time === "string" &&
        data[0].database_time.length > 0 &&
        !Number.isNaN(Date.parse(data[0].database_time))
      ? data[0].database_time
      : null;
    if (databaseTime === null) throw new TypeError("invalid RPC response");

    return json(200, { ok: true, database_time: databaseTime });
  } catch (error) {
    logFailure(requestId, "database_rpc", error);
    return json(503, {
      ok: false,
      code: "database_unavailable",
      request_id: requestId,
    });
  }
}

const authenticatedHealthCheckFetch = withSupabase<Database>(
  { auth: "none", cors: "disabled" },
  (req, ctx) =>
    handleHealthCheckRequest(
      req,
      ctx.supabaseAdmin as unknown as HealthCheckRpcClient,
    ),
);

export async function fetch(req: Request): Promise<Response> {
  if (req.method !== "GET") {
    return json(405, { ok: false, code: "method_not_allowed" }, {
      allow: "GET",
    });
  }
  return await authenticatedHealthCheckFetch(req);
}

export default {
  fetch,
};
