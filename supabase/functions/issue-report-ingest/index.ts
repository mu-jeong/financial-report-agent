import { withSupabase } from "npm:@supabase/server@1.4.1";
import {
  MAX_BODY_BYTES,
  validateEnvelope,
  ValidationError,
} from "./validation.ts";

type Json = string | number | boolean | null | {
  [key: string]: Json | undefined;
} | Json[];
type Database = {
  public: {
    Tables: Record<string, never>;
    Views: Record<string, never>;
    Functions: {
      preflight_issue_ingest_v1: {
        Args: {
          p_source_ip_hash: string;
          p_ip_limit: number;
          p_ip_window_seconds: number;
          p_global_limit: number;
          p_global_window_seconds: number;
        };
        Returns: Array<{
          disposition: string;
          retry_after_seconds: number;
        }>;
      };
      ingest_issue_report_v1: {
        Args: {
          p_event_id: string;
          p_installation_id: string;
          p_queued_at: string;
          p_source_ip_hash: string;
          p_installation_hash: string;
          p_report: Json;
          p_report_schema_version: number;
          p_category: string;
          p_severity: string;
          p_installation_limit: number;
          p_installation_window_seconds: number;
        };
        Returns: Array<{
          disposition: string;
          receipt_id: string | null;
          received_at: string;
          retry_after_seconds: number;
        }>;
      };
    };
    Enums: Record<string, never>;
    CompositeTypes: Record<string, never>;
  };
};

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

type RpcResponse = { data: unknown; error: unknown };
export type IssueReportRpcClient = {
  rpc(
    name: string,
    args: Record<string, unknown>,
  ): PromiseLike<RpcResponse>;
};
export type EnvironmentReader = (name: string) => string | undefined;

export class BodyTooLargeError extends Error {
  constructor() {
    super("request body exceeds the configured limit");
  }
}

export async function readBoundedBody(
  req: Request,
  maximum = MAX_BODY_BYTES,
): Promise<Uint8Array> {
  if (req.body === null) return new Uint8Array();
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maximum) {
        try {
          await reader.cancel("body_too_large");
        } catch {
          // The size verdict is authoritative even if cancellation races.
        }
        throw new BodyTooLargeError();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

function json(
  status: number,
  body: Record<string, unknown>,
  extraHeaders: Record<string, string> = {},
) {
  return Response.json(body, {
    status,
    headers: { ...JSON_HEADERS, ...extraHeaders },
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function logFailure(
  requestId: string,
  stage: string,
  error: unknown,
  eventId?: string,
) {
  const rpcCode = isRecord(error) && typeof error.code === "string"
    ? error.code
    : null;
  console.error(JSON.stringify({
    type: "issue_report_ingest_error",
    request_id: requestId,
    event_id: eventId ?? null,
    stage,
    error_kind: error instanceof Error ? error.name : typeof error,
    rpc_code: rpcCode,
  }));
}

function rateLimited(result: Record<string, unknown>): Response {
  const retry = Math.max(
    1,
    Math.min(Number(result.retry_after_seconds) || 60, 86400),
  );
  return json(429, {
    ok: false,
    code: "rate_limited",
    retry_after_seconds: retry,
  }, { "retry-after": String(retry) });
}

function positiveInt(
  name: string,
  fallback: number,
  min: number,
  max: number,
  readEnvironment: EnvironmentReader = (key) => Deno.env.get(key),
): number {
  const raw = readEnvironment(name);
  const parsed = raw === undefined ? fallback : Number(raw);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`${name} must be an integer from ${min} through ${max}`);
  }
  return parsed;
}

async function hmacSha256Hex(secret: string, value: string): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, encoder.encode(value));
  return [...new Uint8Array(digest)].map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}

function clientIp(req: Request): string | null {
  const connecting = req.headers.get("cf-connecting-ip")?.trim();
  if (
    !connecting || connecting.length > 64 || !/^[0-9a-f:.]+$/i.test(connecting)
  ) return null;
  return connecting.toLowerCase();
}

async function notifyAccepted(
  payload: {
    event_id: string;
    receipt_id: string;
    received_at: string;
    category: string;
    severity: string;
  },
) {
  const url = Deno.env.get("ISSUE_NOTIFICATION_WEBHOOK_URL");
  if (!url) return;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ type: "issue_report_accepted", ...payload }),
      signal: controller.signal,
    });
  } catch {
    // Notification is best-effort and never changes an accepted receipt.
  } finally {
    clearTimeout(timeout);
  }
}

export async function handleIssueReportRequest(
  req: Request,
  supabaseAdmin: IssueReportRpcClient,
  readEnvironment: EnvironmentReader = (name) => Deno.env.get(name),
): Promise<Response> {
  const requestId = crypto.randomUUID();
  let stage = "request";
  let eventId: string | undefined;
  if (req.method !== "POST") {
    return json(405, { ok: false, code: "method_not_allowed" }, {
      allow: "POST",
    });
  }

  try {
    stage = "configuration";
    const ip = clientIp(req);
    const hmacSecret = readEnvironment("ISSUE_IP_HMAC_SECRET");
    if (
      !ip || !hmacSecret ||
      new TextEncoder().encode(hmacSecret).byteLength < 32
    ) {
      const error = { code: "missing_ip_or_hmac_secret" };
      logFailure(requestId, stage, error);
      return json(503, {
        ok: false,
        code: "ingest_not_configured",
        request_id: requestId,
      });
    }

    const ipLimit = positiveInt(
      "ISSUE_RATE_IP_LIMIT",
      20,
      1,
      1000000,
      readEnvironment,
    );
    const ipWindow = positiveInt(
      "ISSUE_RATE_IP_WINDOW_SECONDS",
      3600,
      1,
      86400,
      readEnvironment,
    );
    const globalLimit = positiveInt(
      "ISSUE_RATE_GLOBAL_LIMIT",
      5000,
      1,
      10000000,
      readEnvironment,
    );
    const globalWindow = positiveInt(
      "ISSUE_RATE_GLOBAL_WINDOW_SECONDS",
      3600,
      1,
      86400,
      readEnvironment,
    );
    const ipHash = await hmacSha256Hex(hmacSecret, `ip:v1:${ip}`);

    stage = "preflight_rate_limit";
    const preflight = await supabaseAdmin.rpc(
      "preflight_issue_ingest_v1",
      {
        p_source_ip_hash: ipHash,
        p_ip_limit: ipLimit,
        p_ip_window_seconds: ipWindow,
        p_global_limit: globalLimit,
        p_global_window_seconds: globalWindow,
      },
    );
    if (
      preflight.error || !Array.isArray(preflight.data) ||
      preflight.data.length !== 1 || !isRecord(preflight.data[0])
    ) {
      logFailure(requestId, stage, preflight.error);
      return json(503, {
        ok: false,
        code: "storage_unavailable",
        request_id: requestId,
      });
    }
    const preflightResult = preflight.data[0];
    if (preflightResult.disposition === "rate_limited") {
      return rateLimited(preflightResult);
    }
    if (preflightResult.disposition !== "allowed") {
      logFailure(requestId, stage, { code: "invalid_preflight_result" });
      return json(503, {
        ok: false,
        code: "storage_unavailable",
        request_id: requestId,
      });
    }

    stage = "body";
    const contentType = req.headers.get("content-type")?.split(";", 1)[0]
      .trim().toLowerCase();
    if (contentType !== "application/json") {
      return json(415, { ok: false, code: "unsupported_media_type" });
    }
    const contentLength = req.headers.get("content-length");
    const declared = contentLength === null ? 0 : Number(contentLength);
    if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) {
      return json(413, { ok: false, code: "body_too_large" });
    }
    const raw = await readBoundedBody(req);
    let parsed: unknown;
    try {
      parsed = JSON.parse(
        new TextDecoder("utf-8", { fatal: true }).decode(raw),
      );
    } catch {
      return json(400, { ok: false, code: "invalid_json" });
    }

    stage = "validation";
    const maxAge = positiveInt(
      "ISSUE_MAX_QUEUE_AGE_SECONDS",
      2592000,
      60,
      7776000,
      readEnvironment,
    );
    const envelope = validateEnvelope(parsed, Date.now(), maxAge);
    eventId = envelope.event_id;
    const installationHash = await hmacSha256Hex(
      hmacSecret,
      `installation:v1:${envelope.installation_id}`,
    );

    stage = "storage";
    const storage = await supabaseAdmin.rpc("ingest_issue_report_v1", {
      p_event_id: envelope.event_id,
      p_installation_id: envelope.installation_id,
      p_queued_at: envelope.queued_at,
      p_source_ip_hash: ipHash,
      p_installation_hash: installationHash,
      p_report: envelope.report as Json,
      p_report_schema_version: envelope.report.schema_version,
      p_category: envelope.report.category,
      p_severity: "normal",
      p_installation_limit: positiveInt(
        "ISSUE_RATE_INSTALLATION_LIMIT",
        10,
        1,
        1000000,
        readEnvironment,
      ),
      p_installation_window_seconds: positiveInt(
        "ISSUE_RATE_INSTALLATION_WINDOW_SECONDS",
        3600,
        1,
        86400,
        readEnvironment,
      ),
    });
    if (
      storage.error || !Array.isArray(storage.data) ||
      storage.data.length !== 1 || !isRecord(storage.data[0])
    ) {
      logFailure(requestId, stage, storage.error, eventId);
      return json(503, {
        ok: false,
        code: "storage_unavailable",
        request_id: requestId,
      });
    }
    const result = storage.data[0];
    if (result.disposition === "rate_limited") return rateLimited(result);
    if (
      (result.disposition !== "accepted" &&
        result.disposition !== "duplicate") ||
      typeof result.receipt_id !== "string" ||
      typeof result.received_at !== "string"
    ) {
      logFailure(requestId, stage, { code: "invalid_storage_result" }, eventId);
      return json(503, {
        ok: false,
        code: "storage_unavailable",
        request_id: requestId,
      });
    }
    if (result.disposition === "accepted") {
      await notifyAccepted({
        event_id: envelope.event_id,
        receipt_id: result.receipt_id,
        received_at: result.received_at,
        category: envelope.report.category,
        severity: "normal",
      });
    }
    return json(200, {
      ok: true,
      disposition: result.disposition,
      receipt_id: result.receipt_id,
      received_at: result.received_at,
    });
  } catch (error) {
    if (error instanceof BodyTooLargeError) {
      return json(413, { ok: false, code: "body_too_large" });
    }
    if (error instanceof ValidationError) {
      return json(422, {
        ok: false,
        code: error.code,
        message: error.message,
      });
    }
    logFailure(requestId, stage, error, eventId);
    return json(503, {
      ok: false,
      code: stage === "configuration"
        ? "ingest_not_configured"
        : "storage_unavailable",
      request_id: requestId,
    });
  }
}

export default {
  fetch: withSupabase<Database>(
    { auth: "publishable:desktop_ingest" },
    (req, ctx) =>
      handleIssueReportRequest(
        req,
        ctx.supabaseAdmin as unknown as IssueReportRpcClient,
      ),
  ),
};
