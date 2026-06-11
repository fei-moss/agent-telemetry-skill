#!/usr/bin/env python3
"""Claude Code hook handler: spools telemetry spans for agent sessions.

Registered (by ``adapters/claude_code/install.py``) for SessionStart,
PreToolUse, PostToolUse, PostToolUseFailure, Stop, and SessionEnd. Reads the
hook payload from stdin, correlates events into one trace per Claude Code
session via ``agent_telemetry_skill.session_trace``, and writes spans to the
on-disk spool only — never the network. Stop and SessionEnd spawn a detached
drain process so shipping happens off the host critical path.

Safety contract:
- never writes to stdout (Claude Code parses hook stdout)
- always exits 0, even on internal errors
- honors AGENT_TELEMETRY_ENABLED=0 as an instant no-op
- diagnostics go to stderr only when AGENT_TELEMETRY_DEBUG=1
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

AGENT_NAME = "claude-code"
COLLECTION_LAYER = "hook"

SESSION_START = "SessionStart"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
POST_TOOL_USE_FAILURE = "PostToolUseFailure"
STOP = "Stop"
SESSION_END = "SessionEnd"
SUPPORTED_EVENTS = (
    SESSION_START,
    PRE_TOOL_USE,
    POST_TOOL_USE,
    POST_TOOL_USE_FAILURE,
    STOP,
    SESSION_END,
)

_KEY_DIGEST_LENGTH = 16
_STDIN_TIMEOUT_SECONDS = 2.0
_STDIN_MAX_BYTES = 8 * 1024 * 1024
_STDIN_CHUNK_BYTES = 65536


def main(argv: list[str] | None = None) -> int:
    """Always returns 0: telemetry must never break the host agent."""
    try:
        _run(list(sys.argv[1:] if argv is None else argv))
    except BaseException:
        _debug_traceback()
    return 0


def _run(argv: list[str]) -> None:
    event = _parse_event(argv)
    if event not in SUPPORTED_EVENTS:
        _debug(f"ignoring unsupported event: {event!r}")
        return
    _ensure_importable()
    from agent_telemetry_skill.config import load_config

    config = load_config()
    if not config.enabled:
        return
    payload = _read_stdin_payload()
    session_id = str(payload.get("session_id") or "unknown-session")
    if event == SESSION_START:
        _handle_session_start(session_id, payload)
    elif event == PRE_TOOL_USE:
        _handle_pre_tool_use(session_id, payload)
    elif event in (POST_TOOL_USE, POST_TOOL_USE_FAILURE):
        _handle_post_tool_use(session_id, payload, failed=event == POST_TOOL_USE_FAILURE)
    elif event == STOP:
        _handle_stop(session_id, payload)
        _spawn_detached_drain(config)
    elif event == SESSION_END:
        _handle_session_end(session_id, payload)
        _spawn_detached_drain(config)


def _handle_session_start(session_id: str, payload: dict[str, Any]) -> None:
    from agent_telemetry_skill import session_trace

    attributes = {
        key: value
        for key, value in (
            ("session.source", payload.get("source")),
            ("cwd", payload.get("cwd")),
            ("gen_ai.request.model", payload.get("model")),
        )
        if value
    }
    session_trace.begin(session_id, agent_name=AGENT_NAME, attributes=attributes)


def _handle_pre_tool_use(session_id: str, payload: dict[str, Any]) -> None:
    from agent_telemetry_skill import session_trace
    from agent_telemetry_skill.schema import now_unix_nano

    tool_name = payload.get("tool_name")
    if not tool_name:
        _debug("PreToolUse payload missing tool_name; skipping")
        return
    session_trace.begin(session_id, agent_name=AGENT_NAME)
    session_trace.record_open(
        session_id,
        _tool_key(payload),
        {
            "tool_name": str(tool_name),
            "tool_input": payload.get("tool_input"),
            "start_time_unix_nano": now_unix_nano(),
        },
    )


def _handle_post_tool_use(session_id: str, payload: dict[str, Any], *, failed: bool) -> None:
    from agent_telemetry_skill import emit, session_trace
    from agent_telemetry_skill.config import load_config
    from agent_telemetry_skill.redaction import RedactionConfig, Redactor
    from agent_telemetry_skill.schema import STATUS_ERROR, STATUS_OK, now_unix_nano

    # begin() auto-creates the session record, covering a missed SessionStart.
    record = session_trace.begin(session_id, agent_name=AGENT_NAME)
    key = _tool_key(payload)
    opened = session_trace.pop_open(session_id, key) or {}
    tool_name = payload.get("tool_name") or opened.get("tool_name")
    if not tool_name:
        _debug("PostToolUse payload missing tool_name; skipping")
        return
    redactor = Redactor(RedactionConfig(capture_content=load_config().capture_content))
    tool_input = payload.get("tool_input", opened.get("tool_input"))
    end_ns = now_unix_nano()
    start_ns = min(_coerce_nanos(opened.get("start_time_unix_nano"), default=end_ns), end_ns)
    attributes: dict[str, Any] = {
        "session.id": session_id,
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": str(tool_name),
        "tool.call.id": key,
    }
    if isinstance(tool_input, dict):
        attributes.update(redactor.flatten(tool_input, "tool.arguments"))
    if failed:
        attributes["error.type"] = "tool_error"
    events: list[dict[str, Any]] = []
    tool_response = payload.get("tool_response")
    if tool_response is not None:
        events.append(
            {"name": "tool.result", "attributes": redactor.flatten(tool_response, "tool.result")}
        )
    span = emit.build_span(
        f"execute_tool {tool_name}",
        trace_id=record.trace_id,
        parent_span_id=record.root_span_id,
        attributes=attributes,
        events=events,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=end_ns,
        status_code=STATUS_ERROR if failed else STATUS_OK,
        status_message="tool_error" if failed else "",
        redactor=redactor,
        collection_layer=COLLECTION_LAYER,
    )
    emit.emit_span(span)


def _handle_stop(session_id: str, payload: dict[str, Any]) -> None:
    from agent_telemetry_skill import emit, session_trace

    record = session_trace.begin(session_id, agent_name=AGENT_NAME)
    emit.emit_span(
        name="agent.turn",
        trace_id=record.trace_id,
        parent_span_id=record.root_span_id,
        attributes={
            "session.id": session_id,
            "gen_ai.agent.name": AGENT_NAME,
            "claude.stop_hook_active": bool(payload.get("stop_hook_active", False)),
        },
        collection_layer=COLLECTION_LAYER,
    )


def _handle_session_end(session_id: str, payload: dict[str, Any]) -> None:
    from agent_telemetry_skill import session_trace

    # Ensure a record exists so a lone SessionEnd still emits a root span.
    session_trace.begin(session_id, agent_name=AGENT_NAME)
    attributes: dict[str, Any] = {}
    reason = payload.get("reason")
    if reason:
        attributes["session.end_reason"] = str(reason)
    session_trace.end(session_id, attributes=attributes)


def _spawn_detached_drain(config: Any) -> None:
    """Ship spooled spans from a detached process, off the hook critical path."""
    try:
        if not (config.endpoint or config.output):
            return  # nowhere to ship; spans stay safely spooled
        import subprocess

        env = dict(os.environ)
        repo = str(_REPO_ROOT)
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = repo if not existing else repo + os.pathsep + existing
        subprocess.Popen(
            [sys.executable, "-m", "agent_telemetry_skill.cli", "drain"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            cwd=repo,
        )
    except Exception:
        _debug_traceback()


def _ensure_importable() -> None:
    """Make the vendored package importable when PYTHONPATH was not injected."""
    try:
        import agent_telemetry_skill  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(_REPO_ROOT))


def _parse_event(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--event" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--event="):
            return arg.split("=", 1)[1]
    return None


def _read_stdin_payload() -> dict[str, Any]:
    """Bounded stdin read: an idle pipe or oversized payload never stalls the
    host — after _STDIN_TIMEOUT_SECONDS or _STDIN_MAX_BYTES we give up."""
    try:
        if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
            return {}
        raw = _read_stdin_bounded()
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        _debug("failed to parse hook payload from stdin")
        return {}


def _read_stdin_bounded() -> str:
    try:
        import select

        fd = sys.stdin.fileno()
        deadline = time.monotonic() + _STDIN_TIMEOUT_SECONDS
        chunks: list[bytes] = []
        total = 0
        while total < _STDIN_MAX_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break  # open-but-idle pipe: treat as an empty payload
            chunk = os.read(fd, _STDIN_CHUNK_BYTES)
            if not chunk:
                break  # EOF
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:
        # select() is unavailable for pipes on some platforms (e.g. Windows)
        # and StringIO test doubles have no fileno; fall back to a capped read.
        return sys.stdin.read(_STDIN_MAX_BYTES)


def _tool_key(payload: dict[str, Any]) -> str:
    """Correlation key for Pre/PostToolUse: tool_use_id, else a stable hash."""
    tool_use_id = payload.get("tool_use_id")
    if tool_use_id:
        return str(tool_use_id)
    basis = json.dumps(
        {"tool_input": payload.get("tool_input"), "tool_name": payload.get("tool_name")},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_KEY_DIGEST_LENGTH]


def _coerce_nanos(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _debug(message: str) -> None:
    if os.environ.get("AGENT_TELEMETRY_DEBUG") == "1":
        sys.stderr.write(f"claude-code-hook: {message}\n")


def _debug_traceback() -> None:
    if os.environ.get("AGENT_TELEMETRY_DEBUG") == "1":
        import traceback

        traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
