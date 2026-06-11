"""Claude Code transcript parser for the session-log watcher.

Converts entries from ``~/.claude/projects/*/*.jsonl`` transcripts into
telemetry spans: ``chat <model>`` spans per assistant API turn (deduped by
message id, with token usage) and ``execute_tool <tool>`` spans built from
``tool_use`` blocks closed by the matching ``tool_result`` (correlated by
tool_use id). Unknown entry types are skipped; malformed lines never raise.

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


DEFAULT_GLOB = "~/.claude/projects/*/*.jsonl"
AGENT_NAME = "claude-code"
_MAX_OPEN_TOOLS = 1024
_MAX_SEEN_MESSAGES = 4096


class ClaudeCodeParser:
    """Stateful per-session converter from transcript lines to spans."""

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
        # (session_id, tool_use_id) -> {"name", "input", "start"}
        self._open_tools: dict[tuple[str, str], dict[str, Any]] = {}
        self._seen_messages: set[tuple[str, str]] = set()

    def feed(self, line: str, source_path: str | Path) -> list[TelemetrySpan]:
        """Convert one transcript line into zero or more spans. Never raises."""
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                return []
            session_id = entry.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                return []
            entry_type = entry.get("type")
            if entry_type == "assistant":
                return self._feed_assistant(entry, session_id, str(source_path))
            if entry_type == "user":
                return self._feed_user(entry, session_id, str(source_path))
            return []
        except Exception:
            return []

    def _feed_assistant(
        self, entry: dict[str, Any], session_id: str, source: str
    ) -> list[TelemetrySpan]:
        message = entry.get("message")
        if not isinstance(message, dict):
            return []
        timestamp = parse_timestamp_nano(entry.get("timestamp"))
        spans: list[TelemetrySpan] = []
        chat = self._maybe_chat_span(message, entry, session_id, source, timestamp)
        if chat is not None:
            spans.append(chat)
        record = None
        for index, block in enumerate(_content_blocks(message)):
            block_type = block.get("type")
            if block_type == "tool_use":
                tool_id = block.get("id")
                if not isinstance(tool_id, str) or not tool_id:
                    continue
                self._remember_open_tool(
                    (session_id, tool_id),
                    {
                        "name": block.get("name"),
                        "input": block.get("input"),
                        "start": timestamp,
                    },
                )
            elif self._capture_narrative and block_type in ("thinking", "text"):
                text = _narrative_text(block, block_type)
                if not text:
                    continue
                if record is None:
                    record = self._sessions.resolve(session_id)
                spans.append(
                    make_narrative_span(
                        record,
                        kind="reasoning" if block_type == "thinking" else "message",
                        text=text,
                        source_file=source,
                        time_unix_nano=timestamp,
                        redactor=self._redactor,
                        sequence=index,
                    )
                )
        return spans

    def _feed_user(
        self, entry: dict[str, Any], session_id: str, source: str
    ) -> list[TelemetrySpan]:
        message = entry.get("message")
        if not isinstance(message, dict):
            return []
        timestamp = parse_timestamp_nano(entry.get("timestamp"))
        spans: list[TelemetrySpan] = []
        for block in _content_blocks(message):
            if block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            if not isinstance(tool_id, str) or not tool_id:
                continue
            opened = self._open_tools.pop((session_id, tool_id), None) or {}
            tool_name = opened.get("name")
            record = self._sessions.resolve(session_id)
            spans.append(
                make_tool_span(
                    record,
                    tool_name=tool_name if isinstance(tool_name, str) else "unknown",
                    call_id=tool_id,
                    arguments=opened.get("input"),
                    source_file=source,
                    start_time_unix_nano=opened.get("start"),
                    end_time_unix_nano=timestamp,
                    is_error=bool(block.get("is_error")),
                    result=_result_text(block.get("content")),
                    redactor=self._redactor,
                )
            )
        return spans

    def _maybe_chat_span(
        self,
        message: dict[str, Any],
        entry: dict[str, Any],
        session_id: str,
        source: str,
        timestamp: int | None,
    ) -> TelemetrySpan | None:
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = coerce_int(usage.get("input_tokens"))
        output_tokens = coerce_int(usage.get("output_tokens"))
        if input_tokens is None and output_tokens is None:
            return None
        message_id = message.get("id") or entry.get("requestId") or entry.get("uuid")
        dedup_key = (session_id, str(message_id))
        if message_id is not None and dedup_key in self._seen_messages:
            return None
        if message_id is not None:
            self._remember_seen_message(dedup_key)
        model = message.get("model")
        record = self._sessions.resolve(session_id)
        return make_chat_span(
            record,
            model=model if isinstance(model, str) else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            source_file=source,
            time_unix_nano=timestamp,
            redactor=self._redactor,
        )

    def _remember_open_tool(self, key: tuple[str, str], payload: dict[str, Any]) -> None:
        if len(self._open_tools) >= _MAX_OPEN_TOOLS:
            self._open_tools.pop(next(iter(self._open_tools)), None)
        self._open_tools[key] = payload

    def _remember_seen_message(self, key: tuple[str, str]) -> None:
        if len(self._seen_messages) >= _MAX_SEEN_MESSAGES:
            self._seen_messages.clear()
        self._seen_messages.add(key)


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _narrative_text(block: dict[str, Any], block_type: str) -> str:
    """Extract assistant reasoning/message text from a content block."""
    if block_type == "thinking":
        value = block.get("thinking")
    else:
        value = block.get("text")
    return value.strip() if isinstance(value, str) else ""


def _result_text(content: Any) -> str | None:
    """Extract a best-effort text payload from a tool_result content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if parts:
            return "\n".join(part for part in parts if part)
    return None
