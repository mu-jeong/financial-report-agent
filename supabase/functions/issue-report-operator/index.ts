import { withSupabase } from "@supabase/server";

type Json = string | number | boolean | null | {
  [key: string]: Json | undefined;
} | Json[];

type Database = {
  public: {
    Tables: Record<string, never>;
    Views: Record<string, never>;
    Functions: {
      monitoring_check_admin_v1: {
        Args: { p_actor_user_id: string };
        Returns: boolean;
      };
      monitoring_list_issues_v1: {
        Args: {
          p_actor_user_id: string;
          p_state: string | null;
          p_limit: number;
        };
        Returns: Json;
      };
      monitoring_get_issue_v1: {
        Args: { p_actor_user_id: string; p_issue_id: string };
        Returns: Json;
      };
      monitoring_view_issue_raw_v1: {
        Args: { p_actor_user_id: string; p_issue_id: string };
        Returns: Json;
      };
      monitoring_transition_issue_v1: {
        Args: {
          p_actor_user_id: string;
          p_issue_id: string;
          p_expected_record_revision: number;
          p_target_state: string;
          p_reason: string;
        };
        Returns: Json;
      };
      monitoring_list_control_records_v1: {
        Args: { p_actor_user_id: string; p_issue_id: string };
        Returns: Json;
      };
      monitoring_reconcile_control_record_v1: {
        Args: {
          p_actor_user_id: string;
          p_issue_id: string;
          p_expected_record_revision: number;
          p_record_kind: string;
          p_record_id: string;
          p_lifecycle_status: string;
          p_content_digest: string;
          p_availability: string | null;
          p_references: Json;
          p_attributes: Json;
        };
        Returns: Json;
      };
    };
    Enums: Record<string, never>;
    CompositeTypes: Record<string, never>;
  };
};

type RpcResponse = { data: unknown; error: unknown };
export type OperatorRpcClient = {
  rpc(name: string, args: Record<string, unknown>): PromiseLike<RpcResponse>;
};
type AuthenticatedRequestHandler = (
  req: Request,
) => Response | Promise<Response>;

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};
const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MAX_MUTATION_BODY_BYTES = 4 * 1024;
const ISSUE_STATES = new Set([
  "OPEN",
  "IN_PROGRESS",
  "CLOSED",
  "RESOLVED",
  "NOT_ISSUE",
]);
const ISSUE_TRANSITION_TARGETS: Record<string, string> = {
  start: "IN_PROGRESS",
  resolve: "RESOLVED",
  dismiss: "NOT_ISSUE",
  reopen: "OPEN",
};
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const DIGEST_RE = /^[0-9a-f]{64}$/;
const CONTROL_KINDS = new Set([
  "FIXTURE",
  "CASE",
  "FIXED_SNAPSHOT",
  "RELEASE",
  "RUN",
  "COMPARISON",
]);
const CONTROL_STATUSES: Record<string, Set<string>> = {
  FIXTURE: new Set(["DRAFT", "READY"]),
  CASE: new Set(["DRAFT", "READY"]),
  FIXED_SNAPSHOT: new Set(["READY"]),
  RELEASE: new Set(["REGISTERED"]),
  RUN: new Set([
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "INTERRUPTED",
  ]),
  COMPARISON: new Set(["CREATED"]),
};
const AVAILABILITY = new Set([
  "AVAILABLE",
  "MISSING",
  "CORRUPT",
  "INCOMPATIBLE",
  "UNKNOWN",
]);
const REFERENCE_KEYS = new Set([
  "fixture_revision_id",
  "fixed_snapshot_revision_id",
  "release_manifest_id",
  "case_contract_id",
  "supersedes_comparison_id",
]);
const REFERENCE_KEYS_BY_KIND: Record<string, Set<string>> = {
  FIXTURE: new Set(),
  CASE: new Set([
    "fixture_revision_id",
    "fixed_snapshot_revision_id",
    "case_contract_id",
  ]),
  FIXED_SNAPSHOT: new Set(),
  RELEASE: new Set(),
  RUN: new Set([
    "fixed_snapshot_revision_id",
    "release_manifest_id",
    "case_contract_id",
  ]),
  COMPARISON: new Set([
    "case_contract_id",
    "supersedes_comparison_id",
  ]),
};
const ATTRIBUTE_KEYS = new Set([
  "side",
  "validity",
  "verdict",
  "evidence_qualifier",
]);
const ATTRIBUTE_VALUE_RE = /^[A-Z_]{1,32}$/;
const ATTRIBUTE_VALUES: Record<string, Set<string>> = {
  side: new Set(["BASELINE", "CANDIDATE"]),
  validity: new Set(["VALID", "INVALID", "UNKNOWN"]),
  verdict: new Set([
    "IMPROVED",
    "NOT_IMPROVED",
    "REGRESSED",
    "INCONCLUSIVE",
  ]),
  evidence_qualifier: new Set([
    "EXACT",
    "PARTIAL",
    "SUBSTITUTE_INCLUDED",
    "UNKNOWN",
  ]),
};
const ATTRIBUTE_KEYS_BY_KIND: Record<string, Set<string>> = {
  FIXTURE: new Set(),
  CASE: new Set(["evidence_qualifier"]),
  FIXED_SNAPSHOT: new Set(),
  RELEASE: new Set(),
  RUN: new Set(["side", "validity", "evidence_qualifier"]),
  COMPARISON: new Set(["verdict"]),
};

function json(
  status: number,
  body: Record<string, unknown>,
  headers: HeadersInit = {},
) {
  return Response.json(body, {
    status,
    headers: { ...JSON_HEADERS, ...headers },
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function rpcCode(error: unknown): string | null {
  return isRecord(error) && typeof error.code === "string" ? error.code : null;
}

function isExpectedAuthenticationFailure(error: unknown): boolean {
  if (error instanceof Response) return error.status === 401;
  if (!isRecord(error)) return false;
  const status = error.status ?? error.statusCode;
  if (status === 401) return true;
  const code = rpcCode(error)?.toLowerCase();
  return code === "invalid_jwt" || code === "jwt_expired" ||
    code === "unauthorized";
}

function logFailure(
  requestId: string,
  stage: string,
  action: string,
  error: unknown,
) {
  console.error(JSON.stringify({
    type: "issue_report_operator_error",
    request_id: requestId,
    stage,
    action,
    error_kind: error instanceof Error ? error.name : typeof error,
    rpc_code: rpcCode(error),
  }));
}

function errorResponse(error: unknown, requestId: string): Response {
  const code = rpcCode(error);
  if (code === "42501") return json(403, { ok: false, code: "forbidden" });
  if (code === "22023") {
    return json(422, { ok: false, code: "invalid_request" });
  }
  return json(503, {
    ok: false,
    code: "storage_unavailable",
    request_id: requestId,
  });
}

function operatorPath(req: Request): string[] {
  const parts = new URL(req.url).pathname.split("/").filter(Boolean);
  const marker = parts.lastIndexOf("issue-report-operator");
  return marker < 0 ? parts : parts.slice(marker + 1);
}

async function requestBodyHasBytes(req: Request): Promise<boolean> {
  if (req.body === null) return false;
  const reader = req.body.getReader();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return false;
      if (value.byteLength > 0) {
        try {
          await reader.cancel("body_not_allowed");
        } catch {
          // The request is already rejected even if cancellation races.
        }
        return true;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

async function readMutationBody(
  req: Request,
): Promise<Record<string, unknown>> {
  const contentType = req.headers.get("content-type")?.split(";", 1)[0]
    .trim().toLowerCase();
  if (contentType !== "application/json") {
    throw new TypeError("invalid_content_type");
  }
  const declared = Number(req.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > MAX_MUTATION_BODY_BYTES) {
    throw new RangeError("body_too_large");
  }
  if (req.body === null) throw new TypeError("invalid_body");
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_MUTATION_BODY_BYTES) {
        try {
          await reader.cancel("body_too_large");
        } catch {
          // The bounded verdict is final even if cancellation races.
        }
        throw new RangeError("body_too_large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(bytes),
    );
  } catch {
    throw new SyntaxError("invalid_json");
  }
  if (!isRecord(parsed)) throw new TypeError("invalid_body");
  return parsed;
}

function exactMutationBody(body: Record<string, unknown>) {
  const fields = Object.keys(body);
  if (
    fields.length !== 2 || !fields.includes("expected_record_revision") ||
    !fields.includes("reason") ||
    !Number.isSafeInteger(body.expected_record_revision) ||
    Number(body.expected_record_revision) < 1 ||
    typeof body.reason !== "string" || body.reason.trim().length < 1 ||
    body.reason.trim().length > 2000 ||
    new TextEncoder().encode(body.reason).byteLength > 8000
  ) {
    throw new TypeError("invalid_transition_body");
  }
  return {
    expectedRevision: Number(body.expected_record_revision),
    reason: body.reason.trim(),
  };
}

type ControlMutation = {
  expectedRevision: number;
  record: {
    kind: string;
    id: string;
    status: string;
    digest: string;
    availability: string | null;
    references: Record<string, string>;
    attributes: Record<string, string>;
  };
};

function exactControlBody(body: Record<string, unknown>): ControlMutation {
  if (
    Object.keys(body).length !== 2 || !("expected_record_revision" in body) ||
    !("record" in body) ||
    !Number.isSafeInteger(body.expected_record_revision) ||
    Number(body.expected_record_revision) < 0 || !isRecord(body.record)
  ) throw new TypeError("invalid_control_body");
  const record = body.record;
  const expected = [
    "record_kind",
    "record_id",
    "lifecycle_status",
    "content_digest",
    "availability",
    "references",
    "attributes",
  ];
  if (
    Object.keys(record).length !== expected.length ||
    expected.some((key) => !(key in record))
  ) {
    throw new TypeError("invalid_control_record");
  }
  const kind = record.record_kind;
  const id = record.record_id;
  const status = record.lifecycle_status;
  const digest = record.content_digest;
  const availability = record.availability;
  if (
    typeof kind !== "string" || !CONTROL_KINDS.has(kind) ||
    typeof id !== "string" || !ID_RE.test(id) ||
    typeof status !== "string" || !CONTROL_STATUSES[kind]?.has(status) ||
    typeof digest !== "string" || !DIGEST_RE.test(digest) ||
    (availability !== null &&
      (typeof availability !== "string" || !AVAILABILITY.has(availability))) ||
    !isRecord(record.references) || !isRecord(record.attributes)
  ) throw new TypeError("invalid_control_record");

  const references: Record<string, string> = {};
  for (const [key, value] of Object.entries(record.references)) {
    if (!REFERENCE_KEYS.has(key) || !REFERENCE_KEYS_BY_KIND[kind]?.has(key)) {
      throw new TypeError("invalid_control_reference");
    }
    if (typeof value === "string" && ID_RE.test(value)) {
      references[key] = value;
    } else {
      throw new TypeError("invalid_control_reference");
    }
  }
  const attributes: Record<string, string> = {};
  for (const [key, value] of Object.entries(record.attributes)) {
    if (
      !ATTRIBUTE_KEYS.has(key) || !ATTRIBUTE_KEYS_BY_KIND[kind]?.has(key) ||
      typeof value !== "string" ||
      !ATTRIBUTE_VALUE_RE.test(value) || !ATTRIBUTE_VALUES[key]?.has(value)
    ) {
      throw new TypeError("invalid_control_attribute");
    }
    attributes[key] = value;
  }
  if (["FIXTURE", "CASE", "COMPARISON"].includes(kind) && availability !== null) {
    throw new TypeError("invalid_non_artifact_availability");
  }
  if (kind === "CASE" && status === "READY") {
    const requiredReferences = [
      "fixture_revision_id",
      "fixed_snapshot_revision_id",
      "case_contract_id",
    ];
    if (
      Object.keys(references).length !== requiredReferences.length ||
      requiredReferences.some((key) => !(key in references)) ||
      Object.keys(attributes).length !== 1 ||
      !("evidence_qualifier" in attributes)
    ) {
      throw new TypeError("invalid_case_control_record");
    }
  }
  if (
    kind === "COMPARISON" &&
    (!("case_contract_id" in references) ||
      Object.keys(attributes).length !== 1 || !("verdict" in attributes))
  ) {
    throw new TypeError("invalid_comparison_control_record");
  }
  if (kind === "RUN") {
    const requiredReferences = [
      "fixed_snapshot_revision_id",
      "release_manifest_id",
      "case_contract_id",
    ];
    const requiredAttributes = ["side", "validity", "evidence_qualifier"];
    const validity = attributes.validity;
    if (
      Object.keys(references).length !== requiredReferences.length ||
      requiredReferences.some((key) => !(key in references)) ||
      Object.keys(attributes).length !== requiredAttributes.length ||
      requiredAttributes.some((key) => !(key in attributes)) ||
      (["QUEUED", "RUNNING"].includes(status) &&
        (validity !== "UNKNOWN" || availability !== null)) ||
      (status === "SUCCEEDED" && !["VALID", "INVALID"].includes(validity)) ||
      (["FAILED", "CANCELLED", "INTERRUPTED"].includes(status) &&
        validity !== "INVALID")
    ) {
      throw new TypeError("invalid_run_control_record");
    }
  }
  return {
    expectedRevision: Number(body.expected_record_revision),
    record: { kind, id, status, digest, availability, references, attributes },
  };
}

function disposition(data: unknown): string | null {
  return isRecord(data) && typeof data.disposition === "string"
    ? data.disposition
    : null;
}

export async function handleOperatorRequest(
  req: Request,
  supabaseAdmin: OperatorRpcClient,
  actorUserId: string,
): Promise<Response> {
  const requestId = crypto.randomUUID();
  const path = operatorPath(req);
  const action = `${req.method}:${path.join("/") || "issues"}`;

  if (!UUID_RE.test(actorUserId)) {
    return json(401, { ok: false, code: "unauthorized" });
  }

  let membership: RpcResponse;
  try {
    membership = await supabaseAdmin.rpc("monitoring_check_admin_v1", {
      p_actor_user_id: actorUserId,
    });
  } catch (error) {
    logFailure(requestId, "membership", action, error);
    return errorResponse(error, requestId);
  }
  if (membership.error || membership.data !== true) {
    if (rpcCode(membership.error) !== "42501") {
      logFailure(requestId, "membership", action, membership.error);
    }
    return rpcCode(membership.error) === "42501" || membership.data === false
      ? json(403, { ok: false, code: "forbidden" })
      : errorResponse(membership.error, requestId);
  }

  try {
    if (
      req.method === "GET" && (path.length === 0 || path.join("/") === "issues")
    ) {
      const url = new URL(req.url);
      const state = url.searchParams.get("state");
      const limitRaw = url.searchParams.get("limit") ?? "50";
      if (
        (state !== null && !ISSUE_STATES.has(state)) ||
        !/^\d{1,3}$/.test(limitRaw)
      ) {
        return json(422, { ok: false, code: "invalid_request" });
      }
      const limit = Number(limitRaw);
      if (limit < 1 || limit > 200) {
        return json(422, { ok: false, code: "invalid_request" });
      }
      const result = await supabaseAdmin.rpc("monitoring_list_issues_v1", {
        p_actor_user_id: actorUserId,
        p_state: state,
        p_limit: limit,
      });
      if (result.error) throw result.error;
      if (!Array.isArray(result.data)) {
        throw new TypeError("invalid_rpc_result");
      }
      return json(200, { ok: true, issues: result.data });
    }

    const issueId = path[0] === "issues" ? path[1] : undefined;
    if (!issueId || !UUID_RE.test(issueId)) {
      return json(404, { ok: false, code: "not_found" });
    }

    if (req.method === "GET" && path.length === 2) {
      const result = await supabaseAdmin.rpc("monitoring_get_issue_v1", {
        p_actor_user_id: actorUserId,
        p_issue_id: issueId,
      });
      if (result.error) throw result.error;
      if (!isRecord(result.data)) throw new TypeError("invalid_rpc_result");
      if (disposition(result.data) === "not_found") {
        return json(404, { ok: false, code: "not_found" });
      }
      return json(200, { ok: true, issue: result.data.issue });
    }

    if (req.method === "GET" && path.length === 3 && path[2] === "control") {
      const result = await supabaseAdmin.rpc(
        "monitoring_list_control_records_v1",
        {
          p_actor_user_id: actorUserId,
          p_issue_id: issueId,
        },
      );
      if (result.error) throw result.error;
      if (!isRecord(result.data)) throw new TypeError("invalid_rpc_result");
      if (disposition(result.data) === "not_found") {
        return json(404, { ok: false, code: "not_found" });
      }
      if (!Array.isArray(result.data.records)) {
        throw new TypeError("invalid_rpc_result");
      }
      return json(200, { ok: true, records: result.data.records });
    }

    if (req.method === "PUT" && path.length === 3 && path[2] === "control") {
      let mutation: ControlMutation;
      try {
        mutation = exactControlBody(await readMutationBody(req));
      } catch (error) {
        return json(error instanceof RangeError ? 413 : 422, {
          ok: false,
          code: error instanceof RangeError
            ? "body_too_large"
            : "invalid_request",
        });
      }
      const record = mutation.record;
      const result = await supabaseAdmin.rpc(
        "monitoring_reconcile_control_record_v1",
        {
          p_actor_user_id: actorUserId,
          p_issue_id: issueId,
          p_expected_record_revision: mutation.expectedRevision,
          p_record_kind: record.kind,
          p_record_id: record.id,
          p_lifecycle_status: record.status,
          p_content_digest: record.digest,
          p_availability: record.availability,
          p_references: record.references,
          p_attributes: record.attributes,
        },
      );
      if (result.error) throw result.error;
      if (!isRecord(result.data)) throw new TypeError("invalid_rpc_result");
      const resultDisposition = disposition(result.data);
      if (resultDisposition === "not_found") {
        return json(404, { ok: false, code: "not_found" });
      }
      if (
        resultDisposition === "conflict" ||
        resultDisposition === "immutable_conflict"
      ) {
        return json(409, {
          ok: false,
          code: resultDisposition === "immutable_conflict"
            ? "immutable_conflict"
            : "revision_conflict",
          record_revision: result.data.record_revision,
          content_digest: result.data.content_digest,
        });
      }
      if (
        !["created", "updated", "unchanged"].includes(String(resultDisposition))
      ) {
        throw new TypeError("invalid_rpc_result");
      }
      return json(resultDisposition === "created" ? 201 : 200, {
        ok: true,
        result: result.data,
      });
    }

    if (req.method === "POST" && path.length === 3 && path[2] === "raw") {
      if (await requestBodyHasBytes(req)) {
        return json(422, { ok: false, code: "invalid_request" });
      }
      const result = await supabaseAdmin.rpc("monitoring_view_issue_raw_v1", {
        p_actor_user_id: actorUserId,
        p_issue_id: issueId,
      });
      if (result.error) throw result.error;
      if (!isRecord(result.data)) throw new TypeError("invalid_rpc_result");
      if (disposition(result.data) === "not_found") {
        return json(404, { ok: false, code: "not_found" });
      }
      if (disposition(result.data) === "raw_unavailable") {
        return json(410, { ok: false, code: "raw_unavailable" });
      }
      return json(200, {
        ok: true,
        issue_id: result.data.issue_id,
        record_revision: result.data.record_revision,
        raw_report: result.data.raw_report,
      });
    }

    if (
      req.method === "POST" && path.length === 3 &&
      Object.hasOwn(ISSUE_TRANSITION_TARGETS, path[2])
    ) {
      let transition;
      try {
        transition = exactMutationBody(await readMutationBody(req));
      } catch (error) {
        return json(error instanceof RangeError ? 413 : 422, {
          ok: false,
          code: error instanceof RangeError
            ? "body_too_large"
            : "invalid_request",
        });
      }
      const result = await supabaseAdmin.rpc("monitoring_transition_issue_v1", {
        p_actor_user_id: actorUserId,
        p_issue_id: issueId,
        p_expected_record_revision: transition.expectedRevision,
        p_target_state: ISSUE_TRANSITION_TARGETS[path[2]],
        p_reason: transition.reason,
      });
      if (result.error) throw result.error;
      if (!isRecord(result.data)) throw new TypeError("invalid_rpc_result");
      const resultDisposition = disposition(result.data);
      if (resultDisposition === "not_found") {
        return json(404, { ok: false, code: "not_found" });
      }
      if (resultDisposition === "conflict") {
        return json(409, {
          ok: false,
          code: "revision_conflict",
          record_revision: result.data.record_revision,
          state: result.data.state,
        });
      }
      if (resultDisposition === "invalid_transition") {
        return json(409, {
          ok: false,
          code: "invalid_transition",
          record_revision: result.data.record_revision,
          state: result.data.state,
        });
      }
      if (resultDisposition !== "updated") {
        throw new TypeError("invalid_rpc_result");
      }
      return json(200, { ok: true, issue: result.data });
    }

    return json(405, { ok: false, code: "method_not_allowed" }, {
      allow: "GET, POST, PUT",
    });
  } catch (error) {
    if (rpcCode(error) !== "42501") {
      logFailure(requestId, "operation", action, error);
    }
    return errorResponse(error, requestId);
  }
}

const authenticatedFetch = withSupabase<Database>(
  { auth: "user", cors: "disabled" },
  (req, ctx) => {
    const claims = ctx.userClaims as unknown as Record<string, unknown>;
    const actorUserId = typeof claims.id === "string" ? claims.id : "";
    return handleOperatorRequest(
      req,
      ctx.supabaseAdmin as unknown as OperatorRpcClient,
      actorUserId,
    );
  },
);

export async function fetchWithAuthentication(
  req: Request,
  authenticate: AuthenticatedRequestHandler,
): Promise<Response> {
  const authorization = req.headers.get("authorization");
  if (!authorization || !/^Bearer\s+\S+$/i.test(authorization)) {
    return json(401, { ok: false, code: "unauthorized" });
  }
  try {
    const response = await authenticate(req);
    return response.status === 401
      ? json(401, { ok: false, code: "unauthorized" })
      : response;
  } catch (error) {
    if (isExpectedAuthenticationFailure(error)) {
      return json(401, { ok: false, code: "unauthorized" });
    }
    const requestId = crypto.randomUUID();
    logFailure(requestId, "authentication", "AUTH:operator", error);
    return json(503, {
      ok: false,
      code: "authentication_unavailable",
      request_id: requestId,
    });
  }
}

export async function fetch(req: Request): Promise<Response> {
  return await fetchWithAuthentication(req, authenticatedFetch);
}

export default { fetch };
