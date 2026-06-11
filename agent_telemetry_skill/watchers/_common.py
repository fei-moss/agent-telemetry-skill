"""Shared helpers for session-log watcher parsers.

Span construction goes through :func:`agent_telemetry_skill.emit.build_span`
so watcher spans carry the same resource attributes, redaction, and trust
metadata as every other emission path, with collection layer ``log_watch``
and a ``telemetry.source.file`` dedup attribute.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import session_trace
from ..config import load_config
from ..emit import build_span
from ..redaction import RedactionConfig, Redactor
from ..schema import (
    SPAN_CLIENT,
    STATUS_ERROR,
    STATUS_OK,
    TelemetrySpan,
    now_unix_nano,
)


COLLECTION_LAYER = "log_watch"

_NANOS_PER_SECOND = 1_000_000_000
# Plausible unix-seconds range: 2001-09-09 .. 2286-11-20.
_MIN_UNIX_SECONDS = 1_000_000_000
_MAX_UNIX_SECONDS = 10_000_000_000


def default_redactor() -> Redactor:
    config = load_config()
    if config.disable_redaction:
        # RAW passthrough: no content omission, no secret scrubbing, no truncation.
        return Redactor(
            RedactionConfig(
                capture_content=True,
                max_string_length=max(config.max_content_chars, 1_000_000),
                sensitive_keys=(),
                content_keys=(),
                secret_patterns=(),
                credential_patterns=(),
            )
        )
    return Redactor(
        RedactionConfig(
            capture_content=config.capture_content,
            max_string_length=config.max_content_chars,
        )
    )


def parse_timestamp_nano(value: Any) -> int | None:
    """Best-effort conversion of a transcript timestamp to unix nanoseconds.

    Accepts ISO-8601 strings (``2026-01-15T10:00:05.000Z``) and numeric unix
    seconds. Returns None when the value cannot be interpreted.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if _MIN_UNIX_SECONDS <= value <= _MAX_UNIX_SECONDS:
            return int(value * _NANOS_PER_SECOND)
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * _NANOS_PER_SECOND)


class SessionResolver:
    """Caches session_trace.begin lookups so each session resolves once.

    Resolution goes through :func:`session_trace.begin`, which is idempotent
    and concurrent-safe, so watcher spans join the SAME trace as hook- and
    CLI-emitted events for that session.
    """

    def __init__(self, agent_name: str, *, state_dir: str | Path | None = None):
        self._agent_name = agent_name
        self._state_dir = state_dir
        self._cache: dict[str, session_trace.SessionTrace] = {}

    def resolve(self, session_id: str) -> session_trace.SessionTrace:
        cached = self._cache.get(session_id)
        if cached is not None:
            return cached
        record = session_trace.begin(
            session_id, agent_name=self._agent_name, state_dir=self._state_dir
        )
        self._cache[session_id] = record
        return record


def make_tool_span(
    record: session_trace.SessionTrace,
    *,
    tool_name: str,
    call_id: str,
    arguments: Any,
    source_file: str,
    start_time_unix_nano: int | None,
    end_time_unix_nano: int | None,
    is_error: bool = False,
    result: Any = None,
    redactor: Redactor,
) -> TelemetrySpan:
    """Build one ``execute_tool`` span correlated to its session trace."""
    end = end_time_unix_nano if end_time_unix_nano is not None else now_unix_nano()
    start = start_time_unix_nano if start_time_unix_nano is not None else end
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool_name,
        "tool.call.id": call_id,
        "session.id": record.session_id,
        "telemetry.source.file": source_file,
    }
    if isinstance(arguments, dict) and arguments:
        attributes.update(redactor.flatten(arguments, "tool.arguments"))
    elif arguments not in (None, ""):
        attributes.update(redactor.flatten({"raw": arguments}, "tool.arguments"))
    events = None
    if result is not None:
        events = [
            {
                "name": "tool.result",
                "attributes": {"tool.call.id": call_id, "result": result},
                # Stamp the result event with the tool's end time from the log,
                # not the collection wall-clock, so the event sits inside the span.
                "time_unix_nano": end,
            }
        ]
    return build_span(
        f"execute_tool {tool_name}",
        trace_id=record.trace_id,
        parent_span_id=record.root_span_id,
        attributes=attributes,
        events=events,
        start_time_unix_nano=min(start, end),
        end_time_unix_nano=end,
        status_code=STATUS_ERROR if is_error else STATUS_OK,
        status_message="tool_error" if is_error else "",
        redactor=redactor,
        collection_layer=COLLECTION_LAYER,
    )


def make_chat_span(
    record: session_trace.SessionTrace,
    *,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    source_file: str,
    time_unix_nano: int | None,
    redactor: Redactor,
) -> TelemetrySpan:
    """Build one ``chat`` span carrying GenAI usage attributes."""
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "session.id": record.session_id,
        "telemetry.source.file": source_file,
    }
    if model:
        attributes["gen_ai.request.model"] = model
    if input_tokens is not None:
        attributes["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        attributes["gen_ai.usage.output_tokens"] = output_tokens
    stamp = time_unix_nano if time_unix_nano is not None else now_unix_nano()
    return build_span(
        f"chat {model or 'unknown'}",
        trace_id=record.trace_id,
        parent_span_id=record.root_span_id,
        attributes=attributes,
        start_time_unix_nano=stamp,
        end_time_unix_nano=stamp,
        span_kind=SPAN_CLIENT,
        redactor=redactor,
        collection_layer=COLLECTION_LAYER,
    )


# Narrative span kinds for human-facing display: the assistant's reasoning and
# its visible message/progress text. The text lives under the ``text`` key so it
# rides the redactor's content gating (omitted unless capture_content) and secret
# scrubbing; raise max_content_chars (or disable_redaction) for full reasoning.
NARRATIVE_KINDS = ("reasoning", "message")


def make_narrative_span(
    record: session_trace.SessionTrace,
    *,
    kind: str,
    text: str,
    source_file: str,
    time_unix_nano: int | None,
    redactor: Redactor,
    sequence: int | None = None,
) -> TelemetrySpan:
    """Build one human-display span carrying assistant reasoning or message text.

    ``kind`` is ``reasoning`` (thinking blocks) or ``message`` (visible text /
    progress). The text is redacted (content-gated + secret-scrubbed) before it
    leaves the machine.
    """
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": kind,
        "session.id": record.session_id,
        "telemetry.source.file": source_file,
        "narrative.kind": kind,
        "narrative.char_count": len(text),
    }
    if sequence is not None:
        attributes["narrative.sequence"] = sequence
    stamp = time_unix_nano if time_unix_nano is not None else now_unix_nano()
    return build_span(
        kind,
        trace_id=record.trace_id,
        parent_span_id=record.root_span_id,
        attributes=attributes,
        events=[{"name": kind, "attributes": {"text": text}, "time_unix_nano": stamp}],
        start_time_unix_nano=stamp,
        end_time_unix_nano=stamp,
        redactor=redactor,
        collection_layer=COLLECTION_LAYER,
    )


def coerce_int(value: Any) -> int | None:
    """Return ``value`` as an int when it is numeric, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None
