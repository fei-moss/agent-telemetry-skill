# Agent Telemetry Wire Protocol

Language-agnostic ingest protocol for agent telemetry. Any framework, in any
language, can report agent runs, tool calls, LLM calls, and retrievals without
our SDK by POSTing the JSON documented here. The reference implementation is
`agent_telemetry_skill/exporters.py` (`OTLPHTTPExporter.build_payload` /
`_otlp_span`); this document is normative and matches that code exactly.

## 1. Transport

- **Method / URL**: `POST <endpoint>` where `<endpoint>` is the full OTLP/HTTP
  traces URL, including the `/v1/traces` path
  (example: `https://telemetry.example.com/v1/traces`).
- **Headers**:
  - `Content-Type: application/json`
  - `Authorization: Bearer <token>` (when a token is configured)
- **Body**: a single JSON document with the `resourceSpans` shape from
  section 3.
- **Success**: any HTTP status `< 400`. Clients MUST treat `>= 400` as failure.
- **Timeout**: clients SHOULD use a short request timeout (5 seconds in the
  reference implementation) and MUST NOT block the host agent on delivery.

## 2. Client Configuration Contract

Resolution order for every setting: environment variable, then the matching
key in `~/.agent-telemetry/config.json`, then the default.

| Env var | config.json key | Default | Meaning |
| --- | --- | --- | --- |
| `AGENT_TELEMETRY_ENDPOINT` | `endpoint` | _absent_ | OTLP HTTP traces URL. Absent means local-only mode (no network). |
| `AGENT_TELEMETRY_TOKEN` | `token` | _absent_ | Sent as `Authorization: Bearer <token>`. |
| `AGENT_TELEMETRY_SERVICE` | `service` | `local-agent` | `service.name` resource/span attribute. |
| `AGENT_TELEMETRY_TENANT` | `tenant` | `local-dev` | `tenant.id` attribute. |
| `AGENT_TELEMETRY_ENVIRONMENT` | `environment` | `local` | `deployment.environment` attribute. |
| `AGENT_TELEMETRY_CAPTURE_CONTENT` | `capture_content` | off | `"1"`/`true` enables full content capture (section 7). |
| `AGENT_TELEMETRY_OUTPUT` | `output` | client-defined | Local JSONL fallback path when the endpoint is absent or unreachable. |
| `AGENT_TELEMETRY_HOME` | `home` | `~/.agent-telemetry` | State root (spool dir = `<home>/spool`, state dir = `<home>/state`). |
| `AGENT_TELEMETRY_ENABLED` | `enabled` | on | `"0"`/`false` turns every telemetry entrypoint into a silent no-op. |

## 3. Payload Shape

Top-level document:

```json
{
  "resourceSpans": [
    {
      "resource": {
        "attributes": [
          {"key": "service.name", "value": {"stringValue": "<service>"}},
          {"key": "telemetry.sdk.name", "value": {"stringValue": "agent-telemetry-skill"}}
        ]
      },
      "scopeSpans": [
        {
          "scope": {"name": "agent-telemetry-skill", "version": "0.1.0"},
          "spans": []
        }
      ]
    }
  ]
}
```

- `resource.attributes` MUST contain `service.name`. Additional resource
  attributes (for example `tenant.id`, `deployment.environment`) are allowed.
- `scope.name` identifies the reporting client. Third-party clients SHOULD use
  their own scope name (for example `openclaw-telemetry-plugin`).

### 3.1 Span object

Exact field layout (from `_otlp_span`):

```json
{
  "traceId": "1f0c4a44a3f04d7c9a3b8d2f6e5a1c44",
  "spanId": "9a3b8d2f6e5a1c44",
  "parentSpanId": "7d2f6e5a1c449a3b",
  "name": "execute_tool search_web",
  "kind": "SPAN_KIND_INTERNAL",
  "startTimeUnixNano": "1760000000000000000",
  "endTimeUnixNano": "1760000001500000000",
  "attributes": [],
  "events": [],
  "status": {"code": "STATUS_CODE_OK", "message": ""}
}
```

Rules:

- `traceId`: 32 lowercase hex chars (16 random bytes). One trace per agent run.
- `spanId`: 16 lowercase hex chars (8 random bytes), unique per span.
- `parentSpanId`: OMIT the field entirely on root spans; set it to the parent
  `spanId` on child spans.
- `startTimeUnixNano` / `endTimeUnixNano`: Unix epoch nanoseconds encoded as
  **strings**. If the end time is unknown, repeat the start time.
- `kind`: enum string, one of `SPAN_KIND_INTERNAL` (agent runs, tool calls) or
  `SPAN_KIND_CLIENT` (LLM calls, retrievals).
- `status.code`: `STATUS_CODE_OK` or `STATUS_CODE_ERROR`. On error,
  `status.message` SHOULD be the exception/class name (not the full message —
  it may contain sensitive content).

### 3.2 Event object

```json
{
  "timeUnixNano": "1760000000800000000",
  "name": "tool.result",
  "attributes": [{"key": "result", "value": {"stringValue": "..."}}]
}
```

### 3.3 Attribute value encoding

Every attribute is `{"key": <string>, "value": <AnyValue>}` where `AnyValue`
is exactly one of:

| Source type | Encoding |
| --- | --- |
| boolean | `{"boolValue": true}` |
| integer | `{"intValue": "42"}` (string-encoded) |
| float | `{"doubleValue": 0.74}` |
| string | `{"stringValue": "..."}` |
| null / absent | `{"stringValue": ""}` |
| array | `{"arrayValue": {"values": [<AnyValue>, ...]}}` |
| object / anything else | `{"stringValue": "<JSON-serialized, sorted keys>"}` |

## 4. Span Naming Contract

| Operation | Span name | `gen_ai.operation.name` | Kind |
| --- | --- | --- | --- |
| Agent run (root) | `agent.run <name>` | `invoke_agent` | `SPAN_KIND_INTERNAL` |
| Tool call | `execute_tool <tool>` | `execute_tool` | `SPAN_KIND_INTERNAL` |
| LLM call | `chat <model>` | `chat` | `SPAN_KIND_CLIENT` |
| Retrieval | `retrieve <source>` | `retrieve` | `SPAN_KIND_CLIENT` |

Well-known event names:

- `agent.decision` — planner/branch decision. Attributes: `decision.name`,
  optional `decision.rationale`, `decision.confidence` (double).
- `tool.result` — tool output, attached to the `execute_tool` span. The
  `result` attribute MUST be redacted per section 7.
- `exception` — error detail. Attributes: `exception.type`,
  `exception.message`.

## 5. Required Attributes

### 5.1 On EVERY span

| Attribute | Type | Notes |
| --- | --- | --- |
| `telemetry.collection_layer` | string | REQUIRED. One of `hook`, `log_watch`, `model_reported`, `plugin`, `sdk`. Tells the backend how trustworthy the data is. |

### 5.2 On the root `agent.run` span (recommended on all spans)

| Attribute | Type | Notes |
| --- | --- | --- |
| `tenant.id` | string | Tenant/workspace identifier. |
| `service.name` | string | Same value as the resource attribute. |
| `deployment.environment` | string | e.g. `local`, `staging`, `prod`. |
| `agent.telemetry.schema_version` | string | Currently `0.1.0`. |
| `gen_ai.operation.name` | string | `invoke_agent`. |
| `gen_ai.agent.name` | string | Agent/framework name. |
| `session.id` | string | Host session identifier, if any. |
| `enduser.id` | string | Optional end-user identifier. |

### 5.3 OTel GenAI conventions per operation

| Attribute | Used on | Type |
| --- | --- | --- |
| `gen_ai.tool.name` | `execute_tool` spans | string |
| `tool.call.id` | `execute_tool` spans | string |
| `gen_ai.provider.name` | `chat` spans | string |
| `gen_ai.request.model` | `chat` spans | string |
| `gen_ai.response.model` | `chat` spans | string |
| `gen_ai.response.finish_reasons` | `chat` spans | string |
| `gen_ai.usage.input_tokens` | `chat` spans | int |
| `gen_ai.usage.output_tokens` | `chat` spans | int |
| `gen_ai.data_source.id` | `retrieve` spans | string |
| `retrieval.query` | `retrieve` spans | string (redacted) |
| `duration.ms` | any | int |

Nested payloads (tool arguments, prompts, metadata) are flattened to dotted
keys before encoding, e.g. `tool.arguments.query`, `prompt.messages`,
`metadata.region`.

## 6. Worked Example

A complete request body for one agent run with one tool call:

```json
{
  "resourceSpans": [
    {
      "resource": {
        "attributes": [
          {"key": "service.name", "value": {"stringValue": "openclaw-local"}},
          {"key": "telemetry.sdk.name", "value": {"stringValue": "agent-telemetry-skill"}}
        ]
      },
      "scopeSpans": [
        {
          "scope": {"name": "agent-telemetry-skill", "version": "0.1.0"},
          "spans": [
            {
              "traceId": "1f0c4a44a3f04d7c9a3b8d2f6e5a1c44",
              "spanId": "7d2f6e5a1c449a3b",
              "name": "agent.run answer-user",
              "kind": "SPAN_KIND_INTERNAL",
              "startTimeUnixNano": "1760000000000000000",
              "endTimeUnixNano": "1760000002000000000",
              "attributes": [
                {"key": "agent.telemetry.schema_version", "value": {"stringValue": "0.1.0"}},
                {"key": "deployment.environment", "value": {"stringValue": "local"}},
                {"key": "service.name", "value": {"stringValue": "openclaw-local"}},
                {"key": "tenant.id", "value": {"stringValue": "tenant_123"}},
                {"key": "telemetry.collection_layer", "value": {"stringValue": "plugin"}},
                {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                {"key": "gen_ai.agent.name", "value": {"stringValue": "openclaw"}},
                {"key": "session.id", "value": {"stringValue": "sess-42"}}
              ],
              "events": [
                {
                  "timeUnixNano": "1760000000100000000",
                  "name": "agent.decision",
                  "attributes": [
                    {"key": "decision.name", "value": {"stringValue": "call_search"}},
                    {"key": "decision.confidence", "value": {"doubleValue": 0.74}}
                  ]
                }
              ],
              "status": {"code": "STATUS_CODE_OK", "message": ""}
            },
            {
              "traceId": "1f0c4a44a3f04d7c9a3b8d2f6e5a1c44",
              "spanId": "9a3b8d2f6e5a1c44",
              "parentSpanId": "7d2f6e5a1c449a3b",
              "name": "execute_tool search_web",
              "kind": "SPAN_KIND_INTERNAL",
              "startTimeUnixNano": "1760000000200000000",
              "endTimeUnixNano": "1760000001500000000",
              "attributes": [
                {"key": "telemetry.collection_layer", "value": {"stringValue": "plugin"}},
                {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                {"key": "gen_ai.tool.name", "value": {"stringValue": "search_web"}},
                {
                  "key": "tool.arguments.query",
                  "value": {"stringValue": "{\"char_count\": 15, \"content_omitted\": true}"}
                }
              ],
              "events": [
                {
                  "timeUnixNano": "1760000001400000000",
                  "name": "tool.result",
                  "attributes": [
                    {
                      "key": "result",
                      "value": {"stringValue": "{\"char_count\": 2048, \"content_omitted\": true}"}
                    }
                  ]
                }
              ],
              "status": {"code": "STATUS_CODE_OK", "message": ""}
            }
          ]
        }
      ]
    }
  ]
}
```

## 7. Redaction Requirements (MANDATORY for all clients)

Redaction is performed CLIENT-SIDE, before serialization. The backend assumes
incoming data is already safe. Reference: `agent_telemetry_skill/redaction.py`.

### 7.1 Sensitive keys → `"[REDACTED]"`

Normalize each key: lowercase, replace every non-alphanumeric run with `_`,
strip leading/trailing `_` (`X-Api-Key` → `x_api_key`). Replace the whole
value with the string `"[REDACTED]"` when the normalized key matches:

Exact key list:

```
access_token, api_key, apikey, auth_token, authorization, bearer_token,
cookie, csrf_token, id_token, password, private_key, refresh_token,
secret, session_cookie, session_token
```

Part-based rules (split the normalized key on `_`):

- contains part `authorization`, `password`, `cookie`, or `secret`
- contains parts `private` AND `key`
- contains parts `api` AND `key`, or part `apikey`
- contains part `token` AND any of the prefixes
  `access, auth, bearer, csrf, id, refresh, session` — or the key is exactly
  `token`

### 7.2 Secret patterns inside string values → `"[REDACTED]"`

Apply these regexes to every string value and substitute matches:

```
sk-(?:proj-)?[A-Za-z0-9_-]{8,}
sk-ant-[A-Za-z0-9_-]{8,}
Bearer\s+[A-Za-z0-9._-]+        (case-insensitive)
eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+
```

### 7.3 Content omission (default ON)

Unless full content capture is explicitly enabled
(`AGENT_TELEMETRY_CAPTURE_CONTENT=1` / `capture_content: true`), string values
whose normalized key — or the last `_`-separated part of it — is one of:

```
completion, content, input, message, output, prompt, query, response,
result, text
```

MUST be replaced by the exact object shape:

```json
{"content_omitted": true, "char_count": <length of the secret-scrubbed string>}
```

`char_count` is computed AFTER applying the secret patterns of 7.2.

### 7.4 Truncation

Surviving string values longer than 500 characters MUST be truncated to 500
characters with the suffix `...[TRUNCATED]`.

## 8. Delivery Semantics

- Telemetry MUST NEVER break, block, or slow the host agent. Catch and
  swallow every error at the boundary.
- Buffer spans locally and flush asynchronously (on run/session end and/or a
  short timer).
- If the endpoint is absent or unreachable, write spans to the local JSONL
  fallback path (`AGENT_TELEMETRY_OUTPUT`) instead of dropping them, or spool
  them under `<home>/spool` for a later retry.
- Batching: any number of spans per request is accepted; group spans of one
  trace in one request when practical.
