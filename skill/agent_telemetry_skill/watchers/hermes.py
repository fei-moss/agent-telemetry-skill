"""Hermes session-log parser for the session-log watcher.

Converts entries from ``~/.hermes/sessions/*.jsonl`` into telemetry spans.
Hermes writes one role-tagged JSON object per line:

- ``assistant``: ``reasoning`` (thinking text), ``content`` (message/progress
  text), and ``tool_calls`` (name + arguments).
- ``tool``: ``content`` (tool result) correlated by ``tool_call_id``.
- ``user`` / ``session_meta``: conversation framing.

It emits, in human-display order: ``reasoning`` and ``message`` narrative spans
(gated by capture_narrative + redaction) plus ``execute_tool`` spans built from
tool_calls closed by the matching tool result. The session id is the file stem
so spans join the same trace across polls. Unknown lines are skipped; malformed
lines never raise.

Dedup guard: spans carry ``telemetry.source.file`` (layer ``log_watch``).
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
    default_redactor,
    make_narrative_span,
    make_tool_span,
    parse_timestamp_nano,
)


DEFAULT_GLOB = "~/.hermes/sessions/*.jsonl"
AGENT_NAME = "hermes"
_MAX_OPEN_TOOLS = 1024


class HermesSessionParser:
    """Stateful per-session converter from Hermes session lines to spans."""

    def __init__(
        self,
        *,
        redactor: Redactor | None = None,
        state_dir: str | Path | None = None,
        capture_narrative: bool | None = None,
    ):
        self._redactor = redactor or default_redactor()
        self._sessions = SessionResolver(AGENT_NAME, state_dir=state_dir)
        self._capture_narrative = (
            load_config().capture_narrative if capture_narrative is None else capture_narrative
        )
        # (session_id, tool_call_id) -> {"name", "input", "start"}
        self._open_tools: dict[tuple[str, str], dict[str, Any]] = {}
        # session_id -> monotonic sequence (Hermes entries often share one timestamp)
        self._sequence: dict[str, int] = {}

    def feed(self, line: str, source_path: str | Path) -> list[TelemetrySpan]:
        """Convert one Hermes session line into zero or more spans. Never raises."""
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                return []
            role = entry.get("role") or entry.get("type")
            session_id = Path(str(source_path)).stem
            if not session_id:
                return []
            timestamp = parse_timestamp_nano(entry.get("timestamp"))
            if role == "assistant":
                return self._feed_assistant(entry, session_id, str(source_path), timestamp)
            if role == "tool":
                return self._feed_tool(entry, session_id, str(source_path), timestamp)
            return []
        except Exception:
            return []

    def _feed_assistant(
        self, entry: dict[str, Any], session_id: str, source: str, timestamp: int | None
    ) -> list[TelemetrySpan]:
        spans: list[TelemetrySpan] = []
        record = None
        if self._capture_narrative:
            reasoning = _clean_text(entry.get("reasoning"))
            if reasoning:
                record = self._sessions.resolve(session_id)
                spans.append(
                    make_narrative_span(
                        record,
                        kind="reasoning",
                        text=reasoning,
                        source_file=source,
                        time_unix_nano=timestamp,
                        redactor=self._redactor,
                        sequence=self._next_sequence(session_id),
                    )
                )
            message = _clean_text(entry.get("content"))
            if message:
                record = record or self._sessions.resolve(session_id)
                spans.append(
                    make_narrative_span(
                        record,
                        kind="message",
                        text=message,
                        source_file=source,
                        time_unix_nano=timestamp,
                        redactor=self._redactor,
                        sequence=self._next_sequence(session_id),
                    )
                )
        for call in _tool_calls(entry):
            call_id = call.get("id") or call.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            name, arguments = _tool_name_args(call)
            self._remember_open_tool(
                (session_id, call_id), {"name": name, "input": arguments, "start": timestamp}
            )
        return spans

    def _feed_tool(
        self, entry: dict[str, Any], session_id: str, source: str, timestamp: int | None
    ) -> list[TelemetrySpan]:
        call_id = entry.get("tool_call_id") or entry.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return []
        opened = self._open_tools.pop((session_id, call_id), None) or {}
        name = opened.get("name")
        record = self._sessions.resolve(session_id)
        return [
            make_tool_span(
                record,
                tool_name=name if isinstance(name, str) and name else "unknown",
                call_id=call_id,
                arguments=opened.get("input"),
                source_file=source,
                start_time_unix_nano=opened.get("start"),
                end_time_unix_nano=timestamp,
                is_error=_is_error(entry.get("content")),
                result=_result_text(entry.get("content")),
                redactor=self._redactor,
            )
        ]

    def _remember_open_tool(self, key: tuple[str, str], payload: dict[str, Any]) -> None:
        if len(self._open_tools) >= _MAX_OPEN_TOOLS:
            self._open_tools.pop(next(iter(self._open_tools)), None)
        self._open_tools[key] = payload

    def _next_sequence(self, session_id: str) -> int:
        value = self._sequence.get(session_id, 0)
        self._sequence[session_id] = value + 1
        return value


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _tool_calls(entry: dict[str, Any]) -> list[dict[str, Any]]:
    calls = entry.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _tool_name_args(call: dict[str, Any]) -> tuple[str, Any]:
    function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments")
    else:
        name = call.get("name")
        arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            arguments = {"raw": arguments}
    return (name if isinstance(name, str) else "unknown"), arguments


def _result_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if content is None:
        return None
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _is_error(content: Any) -> bool:
    if isinstance(content, str):
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(data, dict) and str(data.get("status", "")).lower() in ("error", "failed")
    if isinstance(content, dict):
        return str(content.get("status", "")).lower() in ("error", "failed")
    return False
