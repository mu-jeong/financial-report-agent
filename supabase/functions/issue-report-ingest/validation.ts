export const MAX_BODY_BYTES = 128 * 1024;
const MAX_DEPTH = 7;
const MAX_ARRAY_ITEMS = 20;
const MAX_OBJECT_FIELDS = 24;
const MAX_STRING_BYTES = 32 * 1024;
const MAX_COMMENT_BYTES = 4 * 1024;
const MAX_PRIOR_TURNS = 8;
const MAX_RETRIEVAL_OBSERVATIONS = 20;
const encoder = new TextEncoder();

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const VERSION_RE = /^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$/;
const TOP_LEVEL_FIELDS = new Set([
  "ingest_contract_version",
  "event_id",
  "installation_id",
  "queued_at",
  "report",
]);
const REPORT_FIELDS_V2 = new Set([
  "schema_version",
  "report_contract_version",
  "kind",
  "report_target_type",
  "source",
  "app_version",
  "category",
  "comment",
  "consent",
  "observed",
  "diagnostics",
  "privacy",
]);
const REPORT_FIELDS_V3 = new Set([
  ...REPORT_FIELDS_V2,
  "reported_release_id",
  "case_diagnostics",
]);
const LEGACY_LOCAL_REPORT_FIELDS = new Set([
  "id",
  "created_at",
  "thread_id",
  "message_id",
  "job_id",
]);
const KINDS = new Set(["user_feedback", "system_error"]);
const TARGET_TYPES = new Set(["response", "ui_or_system"]);
const SOURCES = new Set(["local_chat", "chat_monitoring_trace", "system"]);
const CATEGORIES = new Set([
  "일반 답변 품질",
  "검색 정확도 이슈",
  "오답/오류",
  "속도",
  "버그/기능",
  "기타",
]);
const CONSENT_FIELDS = new Set([
  "consent_version",
  "include_comment",
  "include_selected_question",
  "include_selected_answer",
  "include_previous_turns",
]);
const REQUIRED_OBSERVED_FIELDS = [
  "route",
  "status",
  "latency_ms",
  "result_count",
  "citation_count",
  "selected_question",
  "selected_answer",
] as const;
const OBSERVED_FIELDS = new Set([
  ...REQUIRED_OBSERVED_FIELDS,
  "result_count_kind",
  "turn_trace",
]);
const RESULT_COUNT_KINDS = new Set(["document", "row", "source"]);
const TURN_TRACE_FIELDS = new Set([
  "turn_index",
  "question",
  "rewritten_query",
  "route",
  "status",
  "followup_scope_intent",
  "scope_source",
  "scope_reason",
  "matched_document_rank",
  "route_hint",
  "has_vector_intent",
  "search_filters",
  "prior_search_filters",
  "prior_file_names",
  "selected_file_names",
  "result_count",
  "result_count_kind",
]);
const FILTER_FIELDS = new Set([
  "broker",
  "brokers",
  "file_names",
  "report_date",
  "report_date_end",
  "report_date_start",
  "report_month",
  "report_type",
  "report_types",
  "target_name",
  "target_names",
]);
const DIAGNOSTIC_FIELDS = new Set([
  "stable_error_code",
  "exception_type",
  "stack_hash",
  "debug_hints",
]);
const PRIVACY_FIELDS = new Set(["redaction_version", "removed_fields"]);
const CASE_DIAGNOSTIC_FIELDS = new Set([
  "schema_version",
  "truncated",
  "prior_turns",
  "route_observations",
  "retrieval_observations",
  "evidence_refs",
]);
const PRIOR_TURN_FIELDS = new Set(["role", "content"]);
const ROUTE_OBSERVATION_FIELDS = new Set([
  "rewritten_query",
  "selected_route",
  "filters",
  "fallback_reason",
]);
const RETRIEVAL_OBSERVATION_FIELDS = new Set([
  "role",
  "source_uid",
  "source_sha256",
  "chunk_uid",
  "chunk_sha256",
  "rank",
]);
const EVIDENCE_ROLES = new Set(["OBSERVED_RESULT", "CONTEXT_USED", "CITED"]);
const TOKEN_RE = /^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}$/;
const DIAGNOSTIC_TOKEN_RE = /^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const UNSAFE_DIAGNOSTIC_FILE_RE = /(?:^|[\\/])[^\\/]+\.(?:db|sqlite|sqlite3|faiss|zip|tar|tgz|gz|7z|rar)$/i;
const ABSOLUTE_POSIX_PATH_RE = /^(?:\/[A-Za-z0-9._-]+){1,}(?:\/[^\s]*)?$/;
const BASE64_BINARY_RE = /^(?:[A-Za-z0-9+/]{4}){32,}={0,2}$/;
const SENSITIVE_PATTERNS = [
  /(?:^|[^A-Za-z0-9_])(?:[A-Za-z][A-Za-z0-9_-]*[_-])?(?:api[_-]?keys?|access[_-]?keys?|access[_-]?tokens?|auth[_-]?tokens?|tokens?|credentials?|client[_-]?secrets?|private[_-]?keys?|signing[_-]?keys?|encryption[_-]?keys?|passwords?|passwd|secrets?|cookies?|sessions?|webhook[_-]?urls?|database[_-]?urls?|connection[_-]?strings?|dsn)\s*[:=]\s*[^\s&#]+/i,
  /\bsb_secret_[A-Za-z0-9._-]{16,}\b/i,
  /\bbearer\s+[A-Za-z0-9._~+/=-]+/i,
  /\bsk-[A-Za-z0-9_-]{8,}\b/i,
  /\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\s*[:=]\s*[^\s&#]+/i,
  /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i,
  /(?:^|\D)(?:\+?82[-.\s]?)?0?1[016789](?:[-.\s]?\d){7,8}(?:\D|$)/,
  /(?:^|[^A-Za-z0-9])[A-Z]:[\\/][^\r\n,;]*/i,
  /(?:^|[^\\/])\\\\[^\r\n,;]*/,
  /\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b/i,
  /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/,
  /\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b/,
  /\b(?:xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{20,}|(?:sk|rk)_live_[A-Za-z0-9]{16,})\b/i,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/i,
  /(?:^|\D)\d{6}[- ]?[1-4]\d{6}(?:\D|$)/,
];

export type ValidEnvelope = {
  ingest_contract_version: 1;
  event_id: string;
  installation_id: string;
  queued_at: string;
  report: Record<string, unknown> & {
    schema_version: 2 | 3;
    report_contract_version: 2 | 3;
    category: string;
  };
};

export class ValidationError extends Error {
  constructor(public readonly code: string, message: string) {
    super(message);
  }
}

function fail(code: string, message: string): never {
  throw new ValidationError(code, message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactFields(
  value: Record<string, unknown>,
  allowed: Set<string>,
  required: string[],
  name: string,
) {
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      fail("unknown_field", `${name}.${key} is not allowed`);
    }
  }
  for (const key of required) {
    if (!(key in value)) fail("missing_field", `${name}.${key} is required`);
  }
}

function bytes(value: string): number {
  return encoder.encode(value).byteLength;
}

export function containsSensitiveContent(value: string): boolean {
  return SENSITIVE_PATTERNS.some((pattern) => pattern.test(value));
}

function boundedString(
  value: unknown,
  name: string,
  maximum: number,
  allowEmpty = false,
): string {
  if (
    typeof value !== "string" || (!allowEmpty && value.length === 0) ||
    bytes(value) > maximum
  ) {
    fail(
      "invalid_string",
      `${name} must be a${
        allowEmpty ? "" : " non-empty"
      } string of at most ${maximum} UTF-8 bytes`,
    );
  }
  return value;
}

function timestamp(
  value: unknown,
  name: string,
  nowMs: number,
  maxAgeSeconds: number,
): string {
  const raw = boundedString(value, name, 64);
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/
      .test(raw)
  ) {
    fail("invalid_timestamp", `${name} must be RFC3339 with a timezone`);
  }
  const parsed = Date.parse(raw);
  if (!Number.isFinite(parsed)) fail("invalid_timestamp", `${name} is invalid`);
  if (parsed > nowMs + 10 * 60 * 1000) {
    fail("future_timestamp", `${name} is too far in the future`);
  }
  if (parsed < nowMs - maxAgeSeconds * 1000) {
    fail("expired_timestamp", `${name} is outside the accepted queue window`);
  }
  return raw;
}

function inspectBounds(value: unknown, depth = 0): void {
  if (depth > MAX_DEPTH) fail("too_deep", `JSON nesting exceeds ${MAX_DEPTH}`);
  if (typeof value === "string") {
    if (bytes(value) > MAX_STRING_BYTES) {
      fail("string_too_large", "a string exceeds the global bound");
    }
    if (containsSensitiveContent(value)) {
      fail("sensitive_content", "a string contains sensitive content");
    }
  }
  if (Array.isArray(value)) {
    if (value.length > MAX_ARRAY_ITEMS) {
      fail("array_too_large", `an array exceeds ${MAX_ARRAY_ITEMS} items`);
    }
    for (const item of value) inspectBounds(item, depth + 1);
  } else if (isRecord(value)) {
    if (Object.keys(value).length > MAX_OBJECT_FIELDS) {
      fail("object_too_large", `an object exceeds ${MAX_OBJECT_FIELDS} fields`);
    }
    for (const item of Object.values(value)) inspectBounds(item, depth + 1);
  }
}

function identifier(value: unknown, name: string): void {
  if (value === null) return;
  if (typeof value === "string") {
    boundedString(value, name, 128, true);
    return;
  }
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) {
    return;
  }
  fail(
    "invalid_identifier",
    `${name} must be a string, non-negative integer, or null`,
  );
}

function nullableToken(value: unknown, name: string): void {
  if (value !== null && (typeof value !== "string" || !TOKEN_RE.test(value))) {
    fail("invalid_token", `${name} must be a bounded token or null`);
  }
}

function nullableCount(value: unknown, name: string, maximum: number): void {
  if (
    value !== null &&
    (!Number.isSafeInteger(value) || (value as number) < 0 ||
      (value as number) > maximum)
  ) {
    fail(
      "invalid_count",
      `${name} must be a non-negative bounded integer or null`,
    );
  }
}

function nullableBoolean(value: unknown, name: string): void {
  if (value !== null && typeof value !== "boolean") {
    fail("invalid_boolean", `${name} must be a boolean or null`);
  }
}

function boundedStringList(value: unknown, name: string): void {
  if (!Array.isArray(value) || value.length > 8) {
    fail("invalid_list", `${name} must contain at most 8 strings`);
  }
  for (const [index, item] of value.entries()) {
    boundedString(item, `${name}[${index}]`, 256, true);
  }
}

function validateFilters(value: unknown, name: string): void {
  if (!isRecord(value)) fail("invalid_filters", `${name} must be an object`);
  exactFields(value, FILTER_FIELDS, [], name);
  for (const [key, item] of Object.entries(value)) {
    if (Array.isArray(item)) {
      boundedStringList(item, `${name}.${key}`);
    } else {
      boundedString(item, `${name}.${key}`, 256, true);
    }
  }
}

function validateTurnTrace(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value) || value.length > 8) {
    fail("invalid_turn_trace", "report.observed.turn_trace must contain at most 8 turns");
  }
  for (const [index, rawTurn] of value.entries()) {
    if (!isRecord(rawTurn)) {
      fail("invalid_turn_trace", `report.observed.turn_trace[${index}] must be an object`);
    }
    const name = `report.observed.turn_trace[${index}]`;
    exactFields(rawTurn, TURN_TRACE_FIELDS, [...TURN_TRACE_FIELDS], name);
    nullableCount(rawTurn.turn_index, `${name}.turn_index`, 1_000_000);
    if (rawTurn.turn_index === 0 || rawTurn.turn_index === null) {
      fail("invalid_turn_trace", `${name}.turn_index must be positive`);
    }
    boundedString(rawTurn.question, `${name}.question`, 2048);
    if (rawTurn.rewritten_query !== null) {
      boundedString(rawTurn.rewritten_query, `${name}.rewritten_query`, 2048, true);
    }
    for (const field of ["route", "status", "scope_source", "scope_reason", "route_hint"]) {
      nullableToken(rawTurn[field], `${name}.${field}`);
    }
    nullableBoolean(rawTurn.followup_scope_intent, `${name}.followup_scope_intent`);
    nullableBoolean(rawTurn.has_vector_intent, `${name}.has_vector_intent`);
    nullableCount(rawTurn.matched_document_rank, `${name}.matched_document_rank`, 1_000_000);
    validateFilters(rawTurn.search_filters, `${name}.search_filters`);
    validateFilters(rawTurn.prior_search_filters, `${name}.prior_search_filters`);
    boundedStringList(rawTurn.prior_file_names, `${name}.prior_file_names`);
    boundedStringList(rawTurn.selected_file_names, `${name}.selected_file_names`);
    nullableCount(rawTurn.result_count, `${name}.result_count`, 1_000_000);
    if (
      rawTurn.result_count_kind !== null &&
      !RESULT_COUNT_KINDS.has(String(rawTurn.result_count_kind))
    ) {
      fail("invalid_result_count_kind", `${name}.result_count_kind is invalid`);
    }
  }
  return value as Array<Record<string, unknown>>;
}

function diagnosticString(
  value: unknown,
  name: string,
  maximum: number,
  allowEmpty = false,
): string {
  const text = boundedString(value, name, maximum, allowEmpty);
  const stripped = text.trim();
  if (
    UNSAFE_DIAGNOSTIC_FILE_RE.test(stripped) ||
    ABSOLUTE_POSIX_PATH_RE.test(stripped) ||
    BASE64_BINARY_RE.test(stripped) ||
    stripped.toLowerCase().startsWith("data:")
  ) {
    fail(
      "unsafe_diagnostic_content",
      `${name} contains a forbidden database, .sqlite, .faiss, .zip, archive, absolute path, or base64 binary value`,
    );
  }
  return text;
}

function diagnosticToken(value: unknown, name: string): string {
  const token = diagnosticString(value, name, 128);
  if (!DIAGNOSTIC_TOKEN_RE.test(token)) {
    fail("invalid_diagnostic_token", `${name} must be a bounded opaque token`);
  }
  return token;
}

function validateCaseDiagnosticFilters(value: unknown, name: string): void {
  if (!isRecord(value)) fail("invalid_diagnostic_filters", `${name} must be an object`);
  if (Object.keys(value).length > 16) {
    fail("invalid_diagnostic_filters", `${name} must contain at most 16 fields`);
  }
  for (const [key, item] of Object.entries(value)) {
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(key)) {
      fail("unknown_field", `${name}.${key} is not an allowlisted filter key shape`);
    }
    if (item === null || typeof item === "boolean") continue;
    if (typeof item === "number" && Number.isFinite(item)) continue;
    if (typeof item === "string") {
      diagnosticString(item, `${name}.${key}`, 256, true);
      continue;
    }
    if (Array.isArray(item) && item.length <= 8) {
      for (const [index, member] of item.entries()) {
        if (member === null || typeof member === "boolean") continue;
        if (typeof member === "number" && Number.isFinite(member)) continue;
        if (typeof member === "string") {
          diagnosticString(member, `${name}.${key}[${index}]`, 256, true);
          continue;
        }
        fail("invalid_diagnostic_filters", `${name}.${key}[${index}] is invalid`);
      }
      continue;
    }
    fail("invalid_diagnostic_filters", `${name}.${key} is invalid`);
  }
}

function validateCaseDiagnostics(value: unknown): void {
  const name = "report.case_diagnostics";
  if (!isRecord(value)) fail("invalid_case_diagnostics", `${name} must be an object`);
  exactFields(value, CASE_DIAGNOSTIC_FIELDS, [...CASE_DIAGNOSTIC_FIELDS], name);
  if (value.schema_version !== 1) {
    fail("unsupported_case_diagnostics", `${name}.schema_version must be 1`);
  }
  if (typeof value.truncated !== "boolean") {
    fail("invalid_case_diagnostics", `${name}.truncated must be boolean`);
  }

  if (!Array.isArray(value.prior_turns) || value.prior_turns.length > MAX_PRIOR_TURNS) {
    fail("invalid_case_diagnostics", `${name}.prior_turns must contain at most ${MAX_PRIOR_TURNS} turns`);
  }
  for (const [index, rawTurn] of value.prior_turns.entries()) {
    const turnName = `${name}.prior_turns[${index}]`;
    if (!isRecord(rawTurn)) fail("invalid_case_diagnostics", `${turnName} must be an object`);
    exactFields(rawTurn, PRIOR_TURN_FIELDS, [...PRIOR_TURN_FIELDS], turnName);
    if (rawTurn.role !== "user") {
      fail("invalid_case_diagnostics", `${turnName}.role is invalid`);
    }
    diagnosticString(rawTurn.content, `${turnName}.content`, 4096);
  }

  if (!Array.isArray(value.route_observations) || value.route_observations.length > MAX_PRIOR_TURNS) {
    fail("invalid_case_diagnostics", `${name}.route_observations must contain at most ${MAX_PRIOR_TURNS} items`);
  }
  for (const [index, rawObservation] of value.route_observations.entries()) {
    const observationName = `${name}.route_observations[${index}]`;
    if (!isRecord(rawObservation)) {
      fail("invalid_case_diagnostics", `${observationName} must be an object`);
    }
    exactFields(
      rawObservation,
      ROUTE_OBSERVATION_FIELDS,
      [...ROUTE_OBSERVATION_FIELDS],
      observationName,
    );
    if (rawObservation.rewritten_query !== null) {
      diagnosticString(rawObservation.rewritten_query, `${observationName}.rewritten_query`, 2048, true);
    }
    if (rawObservation.selected_route !== null) {
      diagnosticToken(rawObservation.selected_route, `${observationName}.selected_route`);
    }
    validateCaseDiagnosticFilters(rawObservation.filters, `${observationName}.filters`);
    if (rawObservation.fallback_reason !== null) {
      diagnosticString(rawObservation.fallback_reason, `${observationName}.fallback_reason`, 512, true);
    }
  }

  if (
    !Array.isArray(value.retrieval_observations) ||
    value.retrieval_observations.length > MAX_RETRIEVAL_OBSERVATIONS
  ) {
    fail(
      "invalid_case_diagnostics",
      `${name}.retrieval_observations must contain at most ${MAX_RETRIEVAL_OBSERVATIONS} items`,
    );
  }
  for (const [index, rawObservation] of value.retrieval_observations.entries()) {
    const observationName = `${name}.retrieval_observations[${index}]`;
    if (!isRecord(rawObservation)) {
      fail("invalid_case_diagnostics", `${observationName} must be an object`);
    }
    exactFields(
      rawObservation,
      RETRIEVAL_OBSERVATION_FIELDS,
      ["role", "source_uid", "source_sha256", "rank"],
      observationName,
    );
    if (!EVIDENCE_ROLES.has(String(rawObservation.role))) {
      fail("invalid_case_diagnostics", `${observationName}.role is invalid`);
    }
    diagnosticToken(rawObservation.source_uid, `${observationName}.source_uid`);
    if (typeof rawObservation.source_sha256 !== "string" || !SHA256_RE.test(rawObservation.source_sha256)) {
      fail("invalid_case_diagnostics", `${observationName}.source_sha256 is invalid`);
    }
    const hasChunkUid = "chunk_uid" in rawObservation;
    const hasChunkHash = "chunk_sha256" in rawObservation;
    if (hasChunkUid !== hasChunkHash) {
      fail("invalid_case_diagnostics", `${observationName} must provide both chunk fields or neither`);
    }
    if (hasChunkUid) {
      diagnosticToken(rawObservation.chunk_uid, `${observationName}.chunk_uid`);
      if (typeof rawObservation.chunk_sha256 !== "string" || !SHA256_RE.test(rawObservation.chunk_sha256)) {
        fail("invalid_case_diagnostics", `${observationName}.chunk_sha256 is invalid`);
      }
    }
    if (!Number.isSafeInteger(rawObservation.rank) || Number(rawObservation.rank) < 1 || Number(rawObservation.rank) > 1_000_000) {
      fail("invalid_case_diagnostics", `${observationName}.rank is invalid`);
    }
  }

  if (!Array.isArray(value.evidence_refs) || value.evidence_refs.length > MAX_RETRIEVAL_OBSERVATIONS) {
    fail("invalid_case_diagnostics", `${name}.evidence_refs must contain at most ${MAX_RETRIEVAL_OBSERVATIONS} strings`);
  }
  for (const [index, evidenceRef] of value.evidence_refs.entries()) {
    diagnosticString(evidenceRef, `${name}.evidence_refs[${index}]`, 256);
  }
}

export function validateEnvelope(
  value: unknown,
  nowMs = Date.now(),
  maxAgeSeconds = 30 * 24 * 60 * 60,
): ValidEnvelope {
  inspectBounds(value);
  if (!isRecord(value)) fail("invalid_body", "body must be a JSON object");
  exactFields(value, TOP_LEVEL_FIELDS, [
    "ingest_contract_version",
    "event_id",
    "installation_id",
    "queued_at",
    "report",
  ], "body");
  if (value.ingest_contract_version !== 1) {
    fail("unsupported_contract", "ingest_contract_version must be 1");
  }
  if (typeof value.event_id !== "string" || !UUID_RE.test(value.event_id)) {
    fail("invalid_event_id", "event_id must be a UUID");
  }
  if (
    typeof value.installation_id !== "string" ||
    !UUID_RE.test(value.installation_id)
  ) fail("invalid_installation_id", "installation_id must be a UUID");
  timestamp(value.queued_at, "queued_at", nowMs, maxAgeSeconds);

  if (!isRecord(value.report)) {
    fail("invalid_report", "report must be an object");
  }
  const report = value.report;
  const supportedReportV2 = report.schema_version === 2 &&
    report.report_contract_version === 2;
  const supportedReportV3 = report.schema_version === 3 &&
    report.report_contract_version === 3;
  if (!supportedReportV2 && !supportedReportV3) {
    fail(
      "unsupported_report",
      "schema_version and report_contract_version must be matching version 2 or 3",
    );
  }
  const reportFields = supportedReportV3 ? REPORT_FIELDS_V3 : REPORT_FIELDS_V2;
  const acceptedFields = new Set([
    ...reportFields,
    ...LEGACY_LOCAL_REPORT_FIELDS,
  ]);
  exactFields(report, acceptedFields, [...REPORT_FIELDS_V2], "report");
  if (supportedReportV3 && !("reported_release_id" in report)) {
    fail("missing_field", "report.reported_release_id is required for version 3");
  }
  if ("id" in report) boundedString(report.id, "report.id", 128);
  if (typeof report.kind !== "string" || !KINDS.has(report.kind)) {
    fail("invalid_kind", "kind is not allowlisted");
  }
  if (
    typeof report.report_target_type !== "string" ||
    !TARGET_TYPES.has(report.report_target_type)
  ) fail("invalid_target_type", "report_target_type is not allowlisted");
  if (typeof report.source !== "string" || !SOURCES.has(report.source)) {
    fail("invalid_source", "source is not allowlisted");
  }
  if ("created_at" in report) {
    timestamp(report.created_at, "report.created_at", nowMs, maxAgeSeconds);
  }
  if (
    typeof report.app_version !== "string" ||
    !VERSION_RE.test(report.app_version)
  ) fail("invalid_app_version", "app_version is invalid");
  if (supportedReportV3) {
    const releaseId = boundedString(
      report.reported_release_id,
      "report.reported_release_id",
      80,
    );
    if (!/^release-[0-9A-Za-z][0-9A-Za-z.+_-]{0,71}$/.test(releaseId)) {
      fail("invalid_reported_release_id", "reported_release_id is invalid");
    }
    if ("case_diagnostics" in report) {
      validateCaseDiagnostics(report.case_diagnostics);
    }
  }
  if ("thread_id" in report) identifier(report.thread_id, "report.thread_id");
  if ("message_id" in report) {
    identifier(report.message_id, "report.message_id");
  }
  if ("job_id" in report) identifier(report.job_id, "report.job_id");
  if (typeof report.category !== "string" || !CATEGORIES.has(report.category)) {
    fail("invalid_category", "category is not allowlisted");
  }
  boundedString(report.comment, "report.comment", MAX_COMMENT_BYTES, true);
  for (const name of ["consent", "observed", "diagnostics", "privacy"]) {
    if (!isRecord(report[name])) {
      fail("invalid_report_section", `report.${name} must be an object`);
    }
  }

  const consent = report.consent as Record<string, unknown>;
  exactFields(consent, CONSENT_FIELDS, [...CONSENT_FIELDS], "report.consent");
  if (consent.consent_version !== 1) {
    fail("unsupported_consent", "consent_version must be 1");
  }
  for (
    const name of [
      "include_comment",
      "include_selected_question",
      "include_selected_answer",
      "include_previous_turns",
    ]
  ) {
    if (typeof consent[name] !== "boolean") {
      fail("invalid_consent", `report.consent.${name} must be boolean`);
    }
  }
  if (consent.include_comment !== (report.comment !== "")) {
    fail(
      "consent_mismatch",
      "include_comment must match non-empty comment presence",
    );
  }

  const observed = report.observed as Record<string, unknown>;
  exactFields(
    observed,
    OBSERVED_FIELDS,
    [...REQUIRED_OBSERVED_FIELDS],
    "report.observed",
  );
  nullableToken(observed.route, "report.observed.route");
  nullableToken(observed.status, "report.observed.status");
  nullableCount(observed.latency_ms, "report.observed.latency_ms", 86_400_000);
  nullableCount(
    observed.result_count,
    "report.observed.result_count",
    1_000_000,
  );
  if (
    "result_count_kind" in observed && observed.result_count_kind !== null &&
    !RESULT_COUNT_KINDS.has(String(observed.result_count_kind))
  ) {
    fail(
      "invalid_result_count_kind",
      "report.observed.result_count_kind is invalid",
    );
  }
  const turnTrace = "turn_trace" in observed
    ? validateTurnTrace(observed.turn_trace)
    : [];
  nullableCount(
    observed.citation_count,
    "report.observed.citation_count",
    1_000_000,
  );
  const question = observed.selected_question === null ? "" : boundedString(
    observed.selected_question,
    "report.observed.selected_question",
    32 * 1024,
    true,
  );
  const answer = observed.selected_answer === null ? "" : boundedString(
    observed.selected_answer,
    "report.observed.selected_answer",
    32 * 1024,
    true,
  );
  if (bytes(question) + bytes(answer) > 32 * 1024) {
    fail(
      "selected_content_too_large",
      "selected question and answer exceed 32768 UTF-8 bytes",
    );
  }
  if (
    consent.include_selected_question !== (observed.selected_question !== null)
  ) {
    fail(
      "consent_mismatch",
      "include_selected_question must match selected_question presence",
    );
  }
  if (consent.include_selected_answer !== (observed.selected_answer !== null)) {
    fail(
      "consent_mismatch",
      "include_selected_answer must match selected_answer presence",
    );
  }
  const hasCaseDiagnostics = report.schema_version === 3 &&
    "case_diagnostics" in report;
  if (
    consent.include_previous_turns !==
      (turnTrace.length > 0 || hasCaseDiagnostics)
  ) {
    fail(
      "consent_mismatch",
      "include_previous_turns must match turn_trace or case_diagnostics presence",
    );
  }
  if (consent.include_previous_turns && !consent.include_selected_question) {
    fail(
      "consent_mismatch",
      "previous turn trace requires selected-question consent",
    );
  }

  const diagnostics = report.diagnostics as Record<string, unknown>;
  exactFields(
    diagnostics,
    DIAGNOSTIC_FIELDS,
    [...DIAGNOSTIC_FIELDS],
    "report.diagnostics",
  );
  nullableToken(
    diagnostics.stable_error_code,
    "report.diagnostics.stable_error_code",
  );
  nullableToken(
    diagnostics.exception_type,
    "report.diagnostics.exception_type",
  );
  if (
    diagnostics.stack_hash !== null &&
    (typeof diagnostics.stack_hash !== "string" ||
      !SHA256_RE.test(diagnostics.stack_hash))
  ) {
    fail(
      "invalid_stack_hash",
      "stack_hash must be a lowercase SHA-256 hex digest or null",
    );
  }
  if (
    !Array.isArray(diagnostics.debug_hints) ||
    diagnostics.debug_hints.length > 8
  ) fail("invalid_debug_hints", "debug_hints must contain at most 8 strings");
  let hintBytes = 0;
  for (const [index, hint] of diagnostics.debug_hints.entries()) {
    hintBytes += bytes(
      boundedString(
        hint,
        `report.diagnostics.debug_hints[${index}]`,
        512,
        true,
      ),
    );
  }
  if (hintBytes > 4096) {
    fail("invalid_debug_hints", "debug_hints exceed 4096 UTF-8 bytes");
  }

  const privacy = report.privacy as Record<string, unknown>;
  exactFields(privacy, PRIVACY_FIELDS, [...PRIVACY_FIELDS], "report.privacy");
  if (privacy.redaction_version !== 1) {
    fail("unsupported_redaction", "redaction_version must be 1");
  }
  if (
    !Array.isArray(privacy.removed_fields) || privacy.removed_fields.length > 16
  ) {
    fail(
      "invalid_removed_fields",
      "removed_fields must contain at most 16 strings",
    );
  }
  const seen = new Set<string>();
  for (const [index, field] of privacy.removed_fields.entries()) {
    const normalized = boundedString(
      field,
      `report.privacy.removed_fields[${index}]`,
      64,
    );
    if (!/^[a-z][a-z0-9_.]{0,63}$/.test(normalized) || seen.has(normalized)) {
      fail(
        "invalid_removed_fields",
        "removed_fields values must be unique bounded field paths",
      );
    }
    seen.add(normalized);
  }
  const minimizedReport = Object.fromEntries(
    Object.entries(report).filter(([field]) => reportFields.has(field)),
  );
  return { ...value, report: minimizedReport } as ValidEnvelope;
}
