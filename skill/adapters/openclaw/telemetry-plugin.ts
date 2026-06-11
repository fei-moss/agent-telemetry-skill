/**
 * agent-telemetry plugin for OpenClaw — single file, zero dependency.
 *
 * Observes OpenClaw lifecycle hooks (session start/end, message in/out, tool
 * call before/after, model usage) and reports OTLP/HTTP JSON traces per
 * docs/PROTOCOL.md. Never throws into the host (errors are swallowed and sent
 * to console.debug); redaction is ON by default (ported from
 * agent_telemetry_skill/redaction.py); spans buffer per session and flush on
 * session end plus a 5s timer; every span carries
 * telemetry.collection_layer = "plugin".
 *
 * Config resolution: env var -> ~/.agent-telemetry/config.json -> default:
 *   AGENT_TELEMETRY_ENABLED/enabled, ENDPOINT/endpoint (absent => local JSONL),
 *   TOKEN/token, SERVICE/service, TENANT/tenant, ENVIRONMENT/environment,
 *   CAPTURE_CONTENT/capture_content, OUTPUT/output, HOME/home.
 */

// --- Ambient declarations + lazy "node:fs" loader (typechecks with plain
// typescript@5, no @types/node; OpenClaw runs on Node/Bun) ------------------

declare const process:
  | { env: Record<string, string | undefined>; getBuiltinModule?: (name: string) => unknown }
  | undefined;

interface NodeFsLike {
  readFileSync(path: string, encoding: "utf-8"): string;
  appendFileSync(
    path: string,
    data: string,
    options?: "utf-8" | { encoding?: "utf-8"; mode?: number },
  ): void;
  mkdirSync(path: string, options?: { recursive?: boolean; mode?: number }): void;
}

let cachedFs: NodeFsLike | null | undefined;

function nodeFs(): NodeFsLike | null {
  if (cachedFs !== undefined) return cachedFs;
  try {
    const requireFn = (globalThis as { require?: (name: string) => unknown }).require;
    if (typeof requireFn === "function") {
      cachedFs = requireFn("node:fs") as NodeFsLike;
      return cachedFs;
    }
  } catch { /* fall through */ }
  try {
    if (typeof process !== "undefined" && process && typeof process.getBuiltinModule === "function") {
      cachedFs = process.getBuiltinModule("node:fs") as NodeFsLike;
      return cachedFs;
    }
  } catch { /* fall through */ }
  cachedFs = null;
  return null;
}

/** Async fallback for pure-ESM hosts without require/getBuiltinModule. */
function bootstrapFs(onReady: () => void): void {
  if (nodeFs()) return;
  try {
    const dynamicImport = new Function("m", "return import(m)") as (n: string) => Promise<unknown>;
    void dynamicImport("node:fs")
      .then((mod) => { cachedFs = mod as NodeFsLike; onReady(); })
      .catch((error: unknown) => debugLog("bootstrapFs", error));
  } catch (error) {
    debugLog("bootstrapFs", error);
  }
}

const SCHEMA_VERSION = "0.1.0";
const SCOPE_NAME = "openclaw-telemetry-plugin";
const COLLECTION_LAYER = "plugin";
const AGENT_NAME = "openclaw";
const FLUSH_INTERVAL_MS = 5_000;
const EXPORT_TIMEOUT_MS = 5_000;
const MAX_STRING_LENGTH = 500;
const MAX_BUFFERED_SESSIONS = 256;

const STATUS_OK = "STATUS_CODE_OK";
const STATUS_ERROR = "STATUS_CODE_ERROR";
const SPAN_INTERNAL = "SPAN_KIND_INTERNAL";
const SPAN_CLIENT = "SPAN_KIND_CLIENT";

// Ported from agent_telemetry_skill/redaction.py defaults.
const SENSITIVE_KEYS: ReadonlySet<string> = new Set(
  ("access_token api_key apikey auth_token authorization bearer_token cookie csrf_token " +
    "id_token password private_key refresh_token secret session_cookie session_token").split(" "),
);
const CONTENT_KEYS: ReadonlySet<string> = new Set(
  "completion content input message output prompt query response result text".split(" "),
);
const SECRET_PATTERNS: readonly RegExp[] = [
  /sk-(?:proj-)?[A-Za-z0-9_-]{8,}/g,
  /sk-ant-[A-Za-z0-9_-]{8,}/g,
  /Bearer\s+[A-Za-z0-9._-]+/gi,
  /eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g,
  // Kept in lockstep with agent_telemetry_skill/redaction.py.
  /\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b/g,
  /\bgh[pousr]_[A-Za-z0-9]{16,}\b/g,
  /\bgithub_pat_[A-Za-z0-9_]{20,}\b/g,
  /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/g,
  /\bAIza[0-9A-Za-z_-]{30,}\b/g,
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----)?/g,
];
const TOKEN_PREFIXES: readonly string[] = "access auth bearer csrf id refresh session".split(" ");

// Inline command-line / connection-string credentials. Each rule keeps the
// non-secret prefix via a backreference and replaces only the value, so the
// command stays legible. Kept in lockstep with agent_telemetry_skill/redaction.py
// (DEFAULT_CREDENTIAL_PATTERNS).
const VALUE_RE = "(?:'[^']*'|\"[^\"]*\"|\\S+)";
const CREDENTIAL_PATTERNS: readonly { re: RegExp; repl: string }[] = [
  // sshpass -p <password>
  { re: new RegExp("(sshpass\\s+-p\\s*)" + VALUE_RE, "gi"), repl: "$1[REDACTED]" },
  // Long credential flags: --password, --token, --api-key, --secret, --access-key, ...
  {
    re: new RegExp(
      "(--(?:password|passwd|pwd|token|api[-_]?key|apikey|secret|access[-_]?key" +
        "|secret[-_]?key|auth[-_]?token|client[-_]?secret)(?:[=\\s]+))" + VALUE_RE,
      "gi",
    ),
    repl: "$1[REDACTED]",
  },
  // Attached short password flags: mysql -pSECRET, psql -p'secret' (no space).
  { re: /(\s-p)(?:'[^']+'|"[^"]+"|[^\s'"]{1,128})(?=\s|$)/gi, repl: "$1[REDACTED]" },
  // Basic-auth flag: curl -u user:pass
  { re: new RegExp("(\\s-u\\s+)" + VALUE_RE, "gi"), repl: "$1[REDACTED]" },
  // URL userinfo: scheme://user:pass@host
  { re: /([A-Za-z][A-Za-z0-9+.\-]*:\/\/)[^/\s:@]+:[^/\s:@]+@/g, repl: "$1[REDACTED]@" },
];

// --- Small utilities --------------------------------------------------------

function debugLog(context: string, error: unknown): void {
  try {
    if (typeof console !== "undefined" && typeof console.debug === "function") {
      console.debug(`[agent-telemetry][${context}]`, error);
    }
  } catch { /* Last resort: never propagate. */ }
}

function envVar(name: string): string | undefined {
  try {
    if (typeof process !== "undefined" && process && process.env) return process.env[name];
  } catch { /* ignore */ }
  return undefined;
}

function nowUnixNano(): string {
  return `${Date.now()}000000`;
}

function randomHex(byteCount: number): string {
  const bytes = new Uint8Array(byteCount);
  const cryptoObj = (globalThis as { crypto?: { getRandomValues?: (b: Uint8Array) => void } }).crypto;
  if (cryptoObj && typeof cryptoObj.getRandomValues === "function") {
    cryptoObj.getRandomValues(bytes);
  } else {
    for (let i = 0; i < byteCount; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  let hex = "";
  for (let i = 0; i < bytes.length; i += 1) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return '"[UNSERIALIZABLE]"';
  }
}

function asString(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return safeJson(value);
}

function asInt(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// --- Configuration ----------------------------------------------------------

interface TelemetryConfig {
  readonly enabled: boolean;
  readonly endpoint: string | null;
  readonly token: string | null;
  readonly service: string;
  readonly tenant: string;
  readonly environment: string;
  readonly captureContent: boolean;
  readonly output: string;
  readonly home: string;
}

function homeDir(): string { return envVar("HOME") ?? envVar("USERPROFILE") ?? "."; }

function readConfigFile(): Record<string, unknown> {
  try {
    const fs = nodeFs();
    if (!fs) {
      return {};
    }
    const parsed: unknown = JSON.parse(
      fs.readFileSync(`${homeDir()}/.agent-telemetry/config.json`, "utf-8"),
    );
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

function isTruthyFlag(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  const text = asString(value).trim().toLowerCase();
  return text === "1" || text === "true" || text === "yes" || text === "on";
}

function isFalsyFlag(value: unknown): boolean {
  if (typeof value === "boolean") return !value;
  const text = asString(value).trim().toLowerCase();
  return text === "0" || text === "false" || text === "no" || text === "off";
}

function resolveSetting(envName: string, fileKey: string, file: Record<string, unknown>): unknown {
  const fromEnv = envVar(envName);
  if (fromEnv !== undefined && fromEnv !== "") return fromEnv;
  return fileKey in file ? file[fileKey] : undefined;
}

function loadConfig(): TelemetryConfig {
  const file = readConfigFile();
  const enabledRaw = resolveSetting("AGENT_TELEMETRY_ENABLED", "enabled", file);
  const home =
    asString(resolveSetting("AGENT_TELEMETRY_HOME", "home", file)) || `${homeDir()}/.agent-telemetry`;
  return {
    enabled: enabledRaw === undefined ? true : !isFalsyFlag(enabledRaw),
    endpoint: asString(resolveSetting("AGENT_TELEMETRY_ENDPOINT", "endpoint", file)) || null,
    token: asString(resolveSetting("AGENT_TELEMETRY_TOKEN", "token", file)) || null,
    service: asString(resolveSetting("AGENT_TELEMETRY_SERVICE", "service", file)) || "local-agent",
    tenant: asString(resolveSetting("AGENT_TELEMETRY_TENANT", "tenant", file)) || "local-dev",
    environment:
      asString(resolveSetting("AGENT_TELEMETRY_ENVIRONMENT", "environment", file)) || "local",
    captureContent:
      isTruthyFlag(resolveSetting("AGENT_TELEMETRY_CAPTURE_CONTENT", "capture_content", file)),
    output:
      asString(resolveSetting("AGENT_TELEMETRY_OUTPUT", "output", file)) ||
      `${home}/openclaw-telemetry.jsonl`,
    home,
  };
}

// --- Redaction (port of agent_telemetry_skill/redaction.py) -----------------

function normalizeKey(key: string): string {
  return key.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

function isSensitiveKey(key: string): boolean {
  const normalized = normalizeKey(key);
  const parts = normalized.split("_").filter((part) => part.length > 0);
  const has = (part: string): boolean => parts.indexOf(part) !== -1;

  if (SENSITIVE_KEYS.has(normalized)) return true;
  if (has("authorization") || has("password") || has("cookie") || has("secret")) return true;
  if ((has("private") && has("key")) || (has("api") && has("key")) || has("apikey")) return true;
  if (has("token")) return TOKEN_PREFIXES.some((prefix) => has(prefix)) || normalized === "token";
  return false;
}

function scrubSecrets(value: string): string {
  let scrubbed = value;
  for (const { re, repl } of CREDENTIAL_PATTERNS) {
    scrubbed = scrubbed.replace(re, repl);
  }
  for (const pattern of SECRET_PATTERNS) {
    scrubbed = scrubbed.replace(pattern, "[REDACTED]");
  }
  return scrubbed;
}

function truncate(value: string): string {
  if (value.length <= MAX_STRING_LENGTH) return value;
  return `${value.slice(0, MAX_STRING_LENGTH)}...[TRUNCATED]`;
}

function isContentPath(keyPath: readonly string[]): boolean {
  // Mirrors Redactor._is_content_path: any ancestor key marks content, so
  // strings nested under e.g. "tool.result" (stdout, file bodies) are gated.
  for (const rawKey of keyPath) {
    const normalized = normalizeKey(rawKey);
    const parts = normalized.split("_");
    const leaf = parts.length > 0 ? parts[parts.length - 1] : "";
    if (CONTENT_KEYS.has(normalized) || CONTENT_KEYS.has(leaf)) return true;
  }
  return false;
}

function redactValue(value: unknown, keyPath: readonly string[], captureContent: boolean): unknown {
  const rawKey = keyPath.length > 0 ? keyPath[keyPath.length - 1] : "";
  const key = normalizeKey(rawKey);

  if (key !== "" && isSensitiveKey(key)) return "[REDACTED]";
  if (isRecord(value)) {
    const redacted: Record<string, unknown> = {};
    for (const itemKey of Object.keys(value)) {
      redacted[itemKey] = redactValue(value[itemKey], [...keyPath, itemKey], captureContent);
    }
    return redacted;
  }
  if (Array.isArray(value)) return value.map((item) => redactValue(item, keyPath, captureContent));
  if (typeof value === "string") {
    const cleaned = scrubSecrets(value);
    if (isContentPath(keyPath) && !captureContent) {
      return { content_omitted: true, char_count: cleaned.length };
    }
    return truncate(cleaned);
  }
  return value;
}

function isPrimitiveRecord(value: Record<string, unknown>): boolean {
  return Object.keys(value).every((key) => !isRecord(value[key]) && !Array.isArray(value[key]));
}

function flattenRedacted(value: unknown, prefix: string): Record<string, unknown> {
  if (!isRecord(value)) {
    return { [prefix]: value };
  }
  const flattened: Record<string, unknown> = {};
  for (const key of Object.keys(value)) {
    const childKey = `${prefix}.${key}`;
    const item = value[key];
    if (isRecord(item) && isPrimitiveRecord(item)) {
      flattened[childKey] = item;
    } else if (isRecord(item)) {
      Object.assign(flattened, flattenRedacted(item, childKey));
    } else if (Array.isArray(item)) {
      flattened[childKey] = safeJson(item);
    } else flattened[childKey] = item;
  }
  return flattened;
}

function redact(value: unknown, captureContent: boolean): unknown {
  return redactValue(value, [], captureContent);
}

function flatten(value: unknown, prefix: string, captureContent: boolean): Record<string, unknown> {
  return flattenRedacted(redactValue(value, [prefix], captureContent), prefix);
}

// --- Span model + OTLP JSON encoding (per docs/PROTOCOL.md) ------------------

interface SpanEvent {
  readonly name: string;
  readonly timeUnixNano: string;
  readonly attributes: Record<string, unknown>;
}

interface TelemetrySpan {
  readonly traceId: string;
  readonly spanId: string;
  readonly parentSpanId: string | null;
  readonly name: string;
  readonly kind: string;
  startTimeUnixNano: string;
  endTimeUnixNano: string | null;
  attributes: Record<string, unknown>;
  events: SpanEvent[];
  statusCode: string;
  statusMessage: string;
}

function otlpValue(value: unknown): Record<string, unknown> {
  if (typeof value === "boolean") return { boolValue: value };
  if (typeof value === "number") {
    return Number.isInteger(value) ? { intValue: String(value) } : { doubleValue: value };
  }
  if (value === null || value === undefined) return { stringValue: "" };
  if (typeof value === "string") return { stringValue: value };
  if (Array.isArray(value)) return { arrayValue: { values: value.map(otlpValue) } };
  return { stringValue: safeJson(value) };
}

function otlpAttributes(attributes: Record<string, unknown>): Array<Record<string, unknown>> {
  return Object.keys(attributes).map((key) => ({ key, value: otlpValue(attributes[key]) }));
}

function otlpSpan(span: TelemetrySpan): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    traceId: span.traceId,
    spanId: span.spanId,
    name: span.name,
    kind: span.kind,
    startTimeUnixNano: span.startTimeUnixNano,
    endTimeUnixNano: span.endTimeUnixNano ?? span.startTimeUnixNano,
    attributes: otlpAttributes(span.attributes),
    events: span.events.map((e) => ({
      timeUnixNano: e.timeUnixNano, name: e.name, attributes: otlpAttributes(e.attributes),
    })),
    status: { code: span.statusCode, message: span.statusMessage },
  };
  if (span.parentSpanId) payload.parentSpanId = span.parentSpanId;
  return payload;
}

function buildPayload(spans: readonly TelemetrySpan[], cfg: TelemetryConfig): Record<string, unknown> {
  const resourceAttributes: Record<string, unknown> = {
    "service.name": cfg.service, "telemetry.sdk.name": "agent-telemetry-skill",
    "tenant.id": cfg.tenant, "deployment.environment": cfg.environment,
  };
  return {
    resourceSpans: [
      {
        resource: { attributes: otlpAttributes(resourceAttributes) },
        scopeSpans: [
          { scope: { name: SCOPE_NAME, version: SCHEMA_VERSION }, spans: spans.map(otlpSpan) },
        ],
      },
    ],
  };
}

function spanToLocalDict(span: TelemetrySpan): Record<string, unknown> {
  return {
    name: span.name,
    trace_id: span.traceId,
    span_id: span.spanId,
    parent_span_id: span.parentSpanId,
    span_kind: span.kind,
    start_time_unix_nano: span.startTimeUnixNano,
    end_time_unix_nano: span.endTimeUnixNano,
    attributes: span.attributes,
    events: span.events.map((e) => ({
      name: e.name, time_unix_nano: e.timeUnixNano, attributes: e.attributes,
    })),
    status: { code: span.statusCode, message: span.statusMessage },
  };
}

// --- Export: OTLP POST with local JSONL fallback ------------------------------

function writeLocalJsonl(spans: readonly TelemetrySpan[], cfg: TelemetryConfig): void {
  try {
    const fs = nodeFs();
    if (!fs) {
      debugLog("writeLocalJsonl", "node:fs unavailable; spans dropped");
      return;
    }
    const slash = cfg.output.lastIndexOf("/");
    if (slash > 0) {
      fs.mkdirSync(cfg.output.slice(0, slash), { recursive: true, mode: 0o700 });
    }
    fs.appendFileSync(
      cfg.output,
      spans.map((s) => `${safeJson(spanToLocalDict(s))}\n`).join(""),
      { encoding: "utf-8", mode: 0o600 },
    );
  } catch (error) {
    debugLog("writeLocalJsonl", error);
  }
}

function postOtlp(spans: readonly TelemetrySpan[], cfg: TelemetryConfig): void {
  if (spans.length === 0) return;
  if (!cfg.endpoint || typeof fetch !== "function") {
    writeLocalJsonl(spans, cfg);
    return;
  }
  try {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (cfg.token) {
      headers.Authorization = `Bearer ${cfg.token}`;
    }
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    if (controller) setTimeout(() => controller.abort(), EXPORT_TIMEOUT_MS);
    void fetch(cfg.endpoint, {
      method: "POST",
      headers,
      body: safeJson(buildPayload(spans, cfg)),
      signal: controller ? controller.signal : undefined,
    })
      .then((response) => {
        if (!response.ok) writeLocalJsonl(spans, cfg);
      })
      .catch((error: unknown) => {
        debugLog("postOtlp.fetch", error);
        writeLocalJsonl(spans, cfg);
      });
  } catch (error) {
    debugLog("postOtlp", error);
    writeLocalJsonl(spans, cfg);
  }
}

// --- Per-session buffering ----------------------------------------------------

interface SessionRun {
  readonly sessionKey: string;
  readonly traceId: string;
  readonly root: TelemetrySpan;
  finishedSpans: TelemetrySpan[];
  openTools: Map<string, TelemetrySpan>;
  openModelCall: TelemetrySpan | null;
  lastModelCall: TelemetrySpan | null;
}

const runs: Map<string, SessionRun> = new Map();
let config: TelemetryConfig = loadConfig();
let flushTimer: unknown = null;

// On pure-ESM hosts node:fs resolves asynchronously; re-read the config file
// once available so ~/.agent-telemetry/config.json keys apply.
bootstrapFs(() => {
  config = loadConfig();
});

function makeSpan(
  name: string, traceId: string, parentSpanId: string | null, kind: string,
  attributes: Record<string, unknown>,
): TelemetrySpan {
  const redactedAttributes = redact(
    { "telemetry.collection_layer": COLLECTION_LAYER, ...attributes },
    config.captureContent,
  );
  return {
    traceId, spanId: randomHex(8), parentSpanId, name, kind,
    startTimeUnixNano: nowUnixNano(), endTimeUnixNano: null,
    attributes: isRecord(redactedAttributes) ? redactedAttributes : {},
    events: [], statusCode: STATUS_OK, statusMessage: "",
  };
}

function addEvent(span: TelemetrySpan, name: string, attributes: Record<string, unknown>): void {
  const redacted = redact(attributes, config.captureContent);
  span.events = [
    ...span.events,
    { name, timeUnixNano: nowUnixNano(), attributes: isRecord(redacted) ? redacted : {} },
  ];
}

function finishSpan(span: TelemetrySpan): void {
  if (span.endTimeUnixNano === null) span.endTimeUnixNano = nowUnixNano();
}

interface HookContext {
  readonly sessionKey: string;
  readonly sessionId: string;
  readonly runId: string;
  readonly senderId: string;
}

function extractContext(event: Record<string, unknown>): HookContext {
  const context = isRecord(event.context) ? event.context : {};
  const pick = (key: string): string => asString(event[key] ?? context[key] ?? "");
  const sessionKey = pick("sessionKey") || pick("sessionId") || "unknown-session";
  return {
    sessionKey, sessionId: pick("sessionId") || sessionKey,
    runId: pick("runId"), senderId: pick("senderId"),
  };
}

function ensureRun(ctx: HookContext): SessionRun {
  const existing = runs.get(ctx.sessionKey);
  if (existing) return existing;
  if (runs.size >= MAX_BUFFERED_SESSIONS) {
    const oldestKey = runs.keys().next().value as string | undefined;
    if (oldestKey !== undefined) closeRun(oldestKey, "evicted");
  }
  const traceId = randomHex(16);
  const root = makeSpan(`agent.run openclaw:${ctx.sessionKey}`, traceId, null, SPAN_INTERNAL, {
    "agent.telemetry.schema_version": SCHEMA_VERSION,
    "deployment.environment": config.environment,
    "service.name": config.service, "tenant.id": config.tenant,
    "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": AGENT_NAME,
    "session.id": ctx.sessionId,
    ...(ctx.senderId ? { "enduser.id": ctx.senderId } : {}),
  });
  const run: SessionRun = {
    sessionKey: ctx.sessionKey, traceId, root, finishedSpans: [],
    openTools: new Map(), openModelCall: null, lastModelCall: null,
  };
  runs.set(ctx.sessionKey, run);
  startFlushTimer();
  return run;
}

function closeRun(sessionKey: string, reason: string): void {
  const run = runs.get(sessionKey);
  if (!run) return;
  runs.delete(sessionKey);
  run.openTools.forEach((span) => {
    addEvent(span, "tool.result_missing", { reason: "session ended before after_tool_call" });
    finishSpan(span);
    run.finishedSpans = [...run.finishedSpans, span];
  });
  run.openTools = new Map();
  if (run.openModelCall) {
    finishSpan(run.openModelCall);
    run.finishedSpans = [...run.finishedSpans, run.openModelCall];
    run.openModelCall = null;
  }
  addEvent(run.root, "openclaw.session.end", { reason });
  finishSpan(run.root);
  postOtlp([run.root, ...run.finishedSpans], config);
}

function flushFinishedSpans(): void {
  runs.forEach((run) => {
    if (run.finishedSpans.length === 0) return;
    const toFlush = run.finishedSpans;
    run.finishedSpans = [];
    if (run.lastModelCall && toFlush.indexOf(run.lastModelCall) !== -1) run.lastModelCall = null;
    postOtlp(toFlush, config);
  });
}

function startFlushTimer(): void {
  if (flushTimer !== null || typeof setInterval !== "function") return;
  const timer = setInterval(() => {
    try { flushFinishedSpans(); } catch (error) { debugLog("flushTimer", error); }
  }, FLUSH_INTERVAL_MS);
  flushTimer = timer;
  const maybeUnref = timer as unknown as { unref?: () => void };
  if (typeof maybeUnref.unref === "function") maybeUnref.unref();
}

function toolKey(ctx: HookContext, event: Record<string, unknown>): string {
  return [ctx.sessionKey, ctx.runId, asString(event.toolCallId), asString(event.toolName)].join("|");
}

// --- Hook handlers -------------------------------------------------------------

function safe(name: string, fn: (event: Record<string, unknown>) => void): (event: unknown) => void {
  return (event: unknown): void => {
    try {
      if (!config.enabled) return;
      fn(isRecord(event) ? event : {});
    } catch (error) { debugLog(name, error); }
  };
}

function handleSessionStart(event: Record<string, unknown>): void {
  const run = ensureRun(extractContext(event));
  addEvent(run.root, "openclaw.session.start", { reason: asString(event.reason) });
}

function handleSessionEnd(event: Record<string, unknown>): void {
  closeRun(extractContext(event).sessionKey, asString(event.reason) || "session_end");
}

function handleMessageReceived(event: Record<string, unknown>): void {
  const ctx = extractContext(event);
  const run = ensureRun(ctx);
  addEvent(run.root, "message.received", {
    content: asString(event.content ?? event.body ?? event.text),
    "enduser.id": ctx.senderId, "message.id": asString(event.messageId),
  });
}

function handleMessageSent(event: Record<string, unknown>): void {
  const run = ensureRun(extractContext(event));
  addEvent(run.root, "message.sent", {
    content: asString(event.content ?? event.body ?? event.text),
    success: event.success === undefined ? true : isTruthyFlag(event.success),
  });
}

function handleToolStart(event: Record<string, unknown>): void {
  const ctx = extractContext(event);
  const run = ensureRun(ctx);
  const key = toolKey(ctx, event);
  if (run.openTools.has(key)) return;
  const toolName = asString(event.toolName) || "unknown_tool";
  const span = makeSpan(`execute_tool ${toolName}`, run.traceId, run.root.spanId, SPAN_INTERNAL, {
    "gen_ai.operation.name": "execute_tool",
    "gen_ai.tool.name": toolName,
    "tool.call.id": asString(event.toolCallId),
    ...flatten(isRecord(event.params) ? event.params : {}, "tool.arguments", config.captureContent),
  });
  run.openTools.set(key, span);
}

function handleToolEnd(event: Record<string, unknown>): void {
  const ctx = extractContext(event);
  const run = ensureRun(ctx);
  const key = toolKey(ctx, event);
  let span = run.openTools.get(key);
  if (span) {
    run.openTools.delete(key);
  } else {
    const toolName = asString(event.toolName) || "unknown_tool";
    span = makeSpan(`execute_tool ${toolName}`, run.traceId, run.root.spanId, SPAN_INTERNAL, {
      "gen_ai.operation.name": "execute_tool",
      "gen_ai.tool.name": toolName,
      "tool.call.id": asString(event.toolCallId),
    });
  }
  if (event.error !== undefined && event.error !== null) {
    span.statusCode = STATUS_ERROR;
    span.statusMessage = "ToolError";
    addEvent(span, "exception", {
      "exception.type": "ToolError", "exception.message": asString(event.error),
    });
  }
  if (event.durationMs !== undefined) {
    span.attributes = { ...span.attributes, "duration.ms": asInt(event.durationMs) };
  }
  addEvent(span, "tool.result", { result: asString(event.result ?? event.output ?? "") });
  finishSpan(span);
  run.finishedSpans = [...run.finishedSpans, span];
}

function newChatSpan(run: SessionRun, event: Record<string, unknown>): TelemetrySpan {
  const model = asString(event.model ?? event.modelId) || "unknown-model";
  return makeSpan(`chat ${model}`, run.traceId, run.root.spanId, SPAN_CLIENT, {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": asString(event.provider), "gen_ai.request.model": model,
  });
}

function handleModelCallStarted(event: Record<string, unknown>): void {
  const run = ensureRun(extractContext(event));
  if (run.openModelCall) {
    finishSpan(run.openModelCall);
    run.finishedSpans = [...run.finishedSpans, run.openModelCall];
  }
  run.openModelCall = newChatSpan(run, event);
}

function handleModelCallEnded(event: Record<string, unknown>): void {
  const run = ensureRun(extractContext(event));
  const span = run.openModelCall ?? newChatSpan(run, event);
  run.openModelCall = null;
  if (event.durationMs !== undefined) {
    span.attributes = { ...span.attributes, "duration.ms": asInt(event.durationMs) };
  }
  if (event.error !== undefined && event.error !== null) {
    span.statusCode = STATUS_ERROR;
    span.statusMessage = "ModelCallError";
  }
  finishSpan(span);
  run.lastModelCall = span;
  run.finishedSpans = [...run.finishedSpans, span];
}

function handleLlmOutput(event: Record<string, unknown>): void {
  const run = ensureRun(extractContext(event));
  const usage = isRecord(event.usage) ? event.usage : {};
  const usageAttributes: Record<string, unknown> = {
    "gen_ai.usage.input_tokens":
      asInt(usage.input_tokens ?? usage.inputTokens ?? usage.prompt_tokens),
    "gen_ai.usage.output_tokens":
      asInt(usage.output_tokens ?? usage.outputTokens ?? usage.completion_tokens),
  };
  const target = run.openModelCall ?? run.lastModelCall;
  if (target) {
    target.attributes = { ...target.attributes, ...usageAttributes };
  } else addEvent(run.root, "gen_ai.usage", usageAttributes);
}

function handleAgentEnd(event: Record<string, unknown>): void {
  const run = ensureRun(extractContext(event));
  addEvent(run.root, "agent.end", {
    success: event.success === undefined ? true : isTruthyFlag(event.success),
    "duration.ms": asInt(event.durationMs),
  });
  flushFinishedSpans();
}

// --- Public manual-wiring API (host-agnostic; never throws) ---------------------

export const onSessionStart = safe("onSessionStart", handleSessionStart);
export const onSessionEnd = safe("onSessionEnd", handleSessionEnd);
export const onMessageReceived = safe("onMessageReceived", handleMessageReceived);
export const onMessageSent = safe("onMessageSent", handleMessageSent);
export const onToolStart = safe("onToolStart", handleToolStart);
export const onToolEnd = safe("onToolEnd", handleToolEnd);
export const onModelCallStarted = safe("onModelCallStarted", handleModelCallStarted);
export const onModelCallEnded = safe("onModelCallEnded", handleModelCallEnded);
export const onLlmOutput = safe("onLlmOutput", handleLlmOutput);
export const onAgentEnd = safe("onAgentEnd", handleAgentEnd);

/** Force-flush every buffered session (e.g. on host shutdown). Never throws. */
export function flushAll(): void {
  try {
    const keys: string[] = [];
    runs.forEach((_run, key) => keys.push(key));
    keys.forEach((key) => closeRun(key, "flush_all"));
  } catch (error) { debugLog("flushAll", error); }
}

/** Re-read configuration (mainly for tests/config changes). Never throws. */
export function reloadConfig(): void {
  try { config = loadConfig(); } catch (error) { debugLog("reloadConfig", error); }
}

// --- REGISTRATION SHIM — OpenClaw plugin entry ----------------------------------
// Based on the OpenClaw plugin docs as of 2026-06:
//   https://docs.openclaw.ai/plugins/building-plugins (definePluginEntry shape:
//     default export { id, name, description, register(api) })
//   https://docs.openclaw.ai/plugins/hooks (api.on(hookName, handler, { priority }))
// We intentionally do NOT import "openclaw/plugin-sdk/plugin-entry" so this file
// stays zero-dependency; the default export mirrors definePluginEntry's object
// shape. If the registration API has drifted in your OpenClaw version, wire the
// exported onSessionStart/onToolStart/onToolEnd/onSessionEnd/... functions
// manually from your own plugin entry.

export interface OpenClawHookApi {
  on(
    hookName: string,
    handler: (event: unknown) => unknown | Promise<unknown>,
    opts?: { priority?: number; timeoutMs?: number },
  ): void;
}

const OBSERVE_ONLY = { priority: 0 } as const;

const pluginEntry = {
  id: "agent-telemetry",
  name: "Agent Telemetry",
  description:
    "Reports OpenClaw sessions, messages, tool calls, and model usage as OTLP traces with privacy-first redaction.",
  register(api: OpenClawHookApi): void {
    try {
      if (!config.enabled) return;
      api.on("session_start", onSessionStart, OBSERVE_ONLY);
      api.on("session_end", onSessionEnd, OBSERVE_ONLY);
      api.on("message_received", onMessageReceived, OBSERVE_ONLY);
      api.on("message_sent", onMessageSent, OBSERVE_ONLY);
      api.on("before_tool_call", onToolStart, OBSERVE_ONLY);
      api.on("after_tool_call", onToolEnd, OBSERVE_ONLY);
      api.on("model_call_started", onModelCallStarted, OBSERVE_ONLY);
      api.on("model_call_ended", onModelCallEnded, OBSERVE_ONLY);
      api.on("llm_output", onLlmOutput, OBSERVE_ONLY);
      api.on("agent_end", onAgentEnd, OBSERVE_ONLY);
    } catch (error) { debugLog("register", error); }
  },
};

export default pluginEntry;
