---
name: agent-telemetry
description: Report local agent decisions, tool calls, LLM calls, retrievals, errors, and timing to an OpenTelemetry-compatible telemetry endpoint with privacy-first defaults. Reliable capture (hooks/plugins/log watchers) is installed once; the model's only ongoing duty is reporting decisions the runtime cannot see.
---

# Agent Telemetry

Telemetry is collected in three layers. Layers 1 and 2 run automatically once
installed — do NOT re-report what they already capture:

1. **Reliable layer** (`hook` / `plugin`): runtime hooks and plugins capture
   tool calls, model calls, and session lifecycle.
2. **Log-watch layer** (`log_watch`): a watcher tails session logs for
   runtimes without hooks.
3. **Model-reported layer** (`model_reported`): YOU report only what the
   runtime cannot see — decisions, rationale, retries, anomalies.

All commands below also work as
`PYTHONPATH=<skill_dir> python3 -m agent_telemetry_skill.cli ...`
if the `agent-telemetry` entrypoint is not on PATH. Every command always
exits 0 and never blocks or breaks the host agent.

## First Use (once per machine)

```bash
python3 scripts/setup.py --auto
```

This detects installed runtimes (Claude Code, Codex, OpenClaw, Hermes),
installs the matching hook/plugin/log-watcher adapter, and writes
`~/.agent-telemetry/config.json`. Configure the backend via env vars or that
file (env wins): `AGENT_TELEMETRY_ENDPOINT`, `AGENT_TELEMETRY_TOKEN`,
`AGENT_TELEMETRY_SERVICE`, `AGENT_TELEMETRY_TENANT`. No endpoint configured
means local-only mode: spans persist in `~/.agent-telemetry/spool/` until
`agent-telemetry drain` ships them.

Verify with:

```bash
agent-telemetry status
```

## Every Session: Report Your Decisions

Your ONLY ongoing duty. Before committing to a tool, plan branch, or model
choice, record why:

```bash
agent-telemetry decision use_search \
  --rationale "Need external context before answering" \
  --confidence 0.8 \
  --session-id <SESSION_ID>
```

Report retries, fallbacks, and anomalies as events (attributes are a JSON
object on stdin):

```bash
echo '{"attempt": 2, "reason": "timeout"}' | \
  agent-telemetry emit-event retry --session-id <SESSION_ID>
```

### Choosing `<SESSION_ID>` per runtime

Always pass the same session id for the whole conversation so your events
join the trace the reliable layer already opened:

- **Claude Code**: use `$CLAUDE_SESSION_ID` if the environment exposes it.
  Otherwise reuse the session id you already know from context. The hook
  layer tracks the same id, so your decisions attach to the same trace.
- **Codex**: use the thread/session id if you know it; otherwise generate one
  stable id at the start of the conversation and reuse it throughout.
- **OpenClaw / Hermes**: use the session id provided in your context.
- **Unknown runtime**: generate a stable id once (e.g. `session-$(date +%s)`),
  remember it, and reuse it for every command in this conversation.

If no session exists yet (no adapter installed), open and close one yourself:

```bash
agent-telemetry session start --session-id <SESSION_ID> --agent-name <name>
# ... work, decisions, events ...
agent-telemetry session end --session-id <SESSION_ID> --status ok
```

## Fallback When Nothing Is Installed

A single shell command, no Python imports, no setup:

```bash
echo '{"reason": "sdk unavailable"}' | agent-telemetry emit-event telemetry.fallback
```

Or without the entrypoint installed:

```bash
echo '{"reason": "sdk unavailable"}' | \
  PYTHONPATH=<skill_dir> python3 -m agent_telemetry_skill.cli emit-event telemetry.fallback
```

## Privacy Rules

- NEVER bypass the redactor. Default behavior omits full prompt, query,
  completion, text, input, output, response, and result content — only
  structure and lengths are reported.
- Full content capture requires the end user's explicit opt-in via
  `AGENT_TELEMETRY_CAPTURE_CONTENT=1`. Never enable it yourself.
- NEVER put raw secrets in event names, span names, or attribute keys.
  Secret values in attributes are redacted; keys and names are preserved
  verbatim for debugging.
- `AGENT_TELEMETRY_ENABLED=0` makes every command a silent no-op. Respect it;
  do not work around it.
