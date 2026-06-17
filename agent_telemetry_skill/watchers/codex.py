"""Codex CLI rollout-file parser for the session-log watcher.

Converts entries from ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` into
telemetry spans: ``execute_tool`` spans from ``function_call`` /
``custom_tool_call`` response items closed by the matching ``*_output``
(correlated by call_id), and ``chat <model>`` spans from ``token_count``
events carrying token usage. The session id comes from the ``session_meta``
entry when present, else it is derived from the rollout file name so a
watcher started mid-session still joins the right trace. Unknown entry
types are skipped; malformed lines never raise.

Dedup guard: spans carry ``telemetry.source.file`` so the backend can dedup
if hooks AND this watcher both run. Installers should enable only ONE
collection layer per runtime.
"""

from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from ..config import load_config
from ..redaction import Redactor
from ..schema import TelemetrySpan
from ._common import (
    SessionResolver,
    coerce_int,
    default_redactor,
    make_chat_span,
    make_narrative_span,
    make_tool_span,
    parse_timestamp_nano,
)


DEFAULT_GLOB = "~/.codex/sessions/*/*/*/rollout-*.jsonl"
AGENT_NAME = "codex"
_ROLLOUT_PREFIX = "rollout-"
# "2026-05-18T15-18-22-" — datetime prefix length inside the rollout stem.
_ROLLOUT_DATETIME_CHARS = 20
_MAX_OPEN_CALLS = 1024

_TOOL_CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
_TOOL_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})
# Roles whose message text is worth showing a human; "developer"/"system"
# entries are sandbox/permission boilerplate, not conversation.
_NARRATIVE_ROLES = frozenset({"user", "assistant"})


class CodexParser:
    """Stateful per-session converter from rollout lines to spans."""

    def __init__(
        self,
        *,
        redactor: Redactor | None = None,
        state_dir: str | Path | None = None,
        capture_narrative: bool | None = None,
    ):
        self._redactor = redactor or default_redactor()
        self._sessions = SessionResolver(AGENT_NAME, state_dir=state_dir)
        self._file_sessions: dict[str, str] = {}
        self._file_models: dict[str, str] = {}
        # (session_id, call_id) -> {"name", "arguments", "start"}
        self._open_calls: dict[tuple[str, str], dict[str, Any]] = {}
        self._capture_narrative = (
            load_config().capture_narrative
            if capture_narrative is None
            else capture_narrative
        )
        # session_id -> monotonic sequence (rollout messages can share a stamp)
        self._sequence: dict[str, int] = {}

    def feed(self, line: str, source_path: str | Path) -> list[TelemetrySpan]:
        """Convert one rollout line into zero or more spans. Never raises."""
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                return []
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                return []
            source = str(source_path)
            entry_type = entry.get("type")
            timestamp = parse_timestamp_nano(entry.get("timestamp"))
            if entry_type == "session_meta":
                self._handle_session_meta(payload, source)
                return []
            if entry_type == "turn_context":
                self._handle_turn_context(payload, source)
                return []
            if entry_type == "response_item":
                return self._handle_response_item(payload, source, timestamp)
            if entry_type == "event_msg":
                return self._handle_event_msg(payload, source, timestamp)
            return []
        except Exception:
            return []

    def _handle_session_meta(self, payload: dict[str, Any], source: str) -> None:
        session_id = payload.get("id")
        if isinstance(session_id, str) and session_id:
            self._file_sessions[source] = session_id

    def _handle_turn_context(self, payload: dict[str, Any], source: str) -> None:
        model = payload.get("model")
        if isinstance(model, str) and model:
            self._file_models[source] = model

    def _handle_response_item(
        self, payload: dict[str, Any], source: str, timestamp: int | None
    ) -> list[TelemetrySpan]:
        item_type = payload.get("type")
        if item_type == "message":
            if payload.get("role") not in _NARRATIVE_ROLES:
                return []
            return self._handle_narrative(
                "message", _message_text(payload.get("content")), source, timestamp
            )
        if item_type == "reasoning":
            # Codex usually encrypts reasoning (encrypted_content) with an empty
            # summary; capture the summary text only when it is present.
            return self._handle_narrative(
                "reasoning", _reasoning_text(payload), source, timestamp
            )
        if item_type in _TOOL_CALL_TYPES:
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                session_id = self._session_id_for(source)
                self._remember_open_call(
                    (session_id, call_id),
                    {
                        "name": payload.get("name"),
                        "arguments": _parse_arguments(payload),
                        "start": timestamp,
                    },
                )
            return []
        if item_type in _TOOL_OUTPUT_TYPES:
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                return []
            session_id = self._session_id_for(source)
            opened = self._open_calls.pop((session_id, call_id), None) or {}
            tool_name = opened.get("name")
            record = self._sessions.resolve(session_id)
            return [
                make_tool_span(
                    record,
                    tool_name=tool_name if isinstance(tool_name, str) else "unknown",
                    call_id=call_id,
                    arguments=opened.get("arguments"),
                    source_file=source,
                    start_time_unix_nano=opened.get("start"),
                    end_time_unix_nano=timestamp,
                    result=_output_text(payload.get("output")),
                    redactor=self._redactor,
                )
            ]
        return []

    def _handle_narrative(
        self, kind: str, text: str, source: str, timestamp: int | None
    ) -> list[TelemetrySpan]:
        """Emit a narrative span (``message`` or ``reasoning``) for human display."""
        if not self._capture_narrative or not text:
            return []
        session_id = self._session_id_for(source)
        record = self._sessions.resolve(session_id)
        return [
            make_narrative_span(
                record,
                kind=kind,
                text=text,
                source_file=source,
                time_unix_nano=timestamp,
                redactor=self._redactor,
                sequence=self._next_sequence(session_id),
            )
        ]

    def _handle_event_msg(
        self, payload: dict[str, Any], source: str, timestamp: int | None
    ) -> list[TelemetrySpan]:
        if payload.get("type") != "token_count":
            return []
        info = payload.get("info")
        if not isinstance(info, dict):
            return []
        usage = info.get("last_token_usage") or info.get("total_token_usage")
        if not isinstance(usage, dict):
            return []
        input_tokens = coerce_int(usage.get("input_tokens"))
        output_tokens = coerce_int(usage.get("output_tokens"))
        if input_tokens is None and output_tokens is None:
            return []
        session_id = self._session_id_for(source)
        record = self._sessions.resolve(session_id)
        return [
            make_chat_span(
                record,
                model=self._file_models.get(source),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                source_file=source,
                time_unix_nano=timestamp,
                redactor=self._redactor,
            )
        ]

    def _session_id_for(self, source: str) -> str:
        known = self._file_sessions.get(source)
        if known:
            return known
        derived = _session_id_from_path(source)
        self._file_sessions[source] = derived
        return derived

    def _remember_open_call(self, key: tuple[str, str], payload: dict[str, Any]) -> None:
        if len(self._open_calls) >= _MAX_OPEN_CALLS:
            self._open_calls.pop(next(iter(self._open_calls)), None)
        self._open_calls[key] = payload

    def _next_sequence(self, session_id: str) -> int:
        value = self._sequence.get(session_id, 0)
        self._sequence[session_id] = value + 1
        return value


def _session_id_from_path(source: str) -> str:
    """Derive the session id from a rollout file name.

    Rollout files are named ``rollout-<YYYY-MM-DDTHH-MM-SS>-<session-id>.jsonl``;
    the trailing id matches the ``session_meta`` payload id. Falls back to the
    full stem for unexpected names.
    """
    stem = Path(source).stem
    if stem.startswith(_ROLLOUT_PREFIX):
        rest = stem[len(_ROLLOUT_PREFIX):]
        if len(rest) > _ROLLOUT_DATETIME_CHARS:
            return rest[_ROLLOUT_DATETIME_CHARS:]
        return rest or stem
    return stem


def _parse_arguments(payload: dict[str, Any]) -> Any:
    """Return tool-call arguments as a dict when possible.

    ``function_call.arguments`` is a JSON-encoded string; ``custom_tool_call``
    may carry ``input`` instead. Unparseable values are returned as-is and
    redacted downstream.
    """
    raw = payload.get("arguments")
    if raw is None:
        raw = payload.get("input")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else raw
        except ValueError:
            return raw
    return raw


def _message_text(content: Any) -> str:
    """Join the text blocks of a Codex message ``content`` list into one string.

    Codex messages carry ``content`` as a list of blocks like
    ``{"type": "input_text"|"output_text", "text": "..."}``. A plain string is
    also accepted. Non-text blocks are ignored; the result is stripped.
    """
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n".join(parts).strip()


def _reasoning_text(payload: dict[str, Any]) -> str:
    """Extract plaintext reasoning from a Codex ``reasoning`` response item.

    Codex carries a ``summary`` list of ``{"type": "summary_text", "text": ...}``
    blocks plus an opaque ``encrypted_content`` blob. Only the summary text is
    human-readable; when it is absent (encrypted-only) there is nothing to show.
    """
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts = [
            block["text"]
            for block in summary
            if isinstance(block, dict)
            and isinstance(block.get("text"), str)
            and block["text"].strip()
        ]
        if parts:
            return "\n".join(parts).strip()
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _output_text(output: Any) -> str | None:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        content = output.get("content") or output.get("output")
        if isinstance(content, str):
            return content
    return None
