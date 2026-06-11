"""Tests for the Hermes session-log parser (rich narrative capture).

Synthetic Hermes session lines only — no real personal data. Covers reasoning
+ message + tool extraction, narrative gating, secret scrubbing, and the raw
(disable_redaction) passthrough mode.
"""

from __future__ import annotations

import json
import tempfile
import unittest

from agent_telemetry_skill.redaction import RedactionConfig, Redactor
from agent_telemetry_skill.watchers.hermes import HermesSessionParser


SOURCE = "/root/.hermes/sessions/20260611_010203_abcdef.jsonl"
SESSION_ID = "20260611_010203_abcdef"

ASSISTANT = json.dumps(
    {
        "role": "assistant",
        "content": "我先查 BTC 当前持仓再决定。",
        "reasoning": "用户问实盘状态。需要先调用 query 工具读取持仓，再判断是否开仓。",
        "tool_calls": [
            {
                "id": "call_1",
                "function": {"name": "terminal", "arguments": '{"command": "python3 q.py --symbol BTC"}'},
            }
        ],
        "timestamp": "2026-06-11T01:02:03.000Z",
    },
    ensure_ascii=False,
)
TOOL = json.dumps(
    {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"status": "success", "output": "持仓: 0, BTC=68200"}',
        "timestamp": "2026-06-11T01:02:05.000Z",
    },
    ensure_ascii=False,
)
ASSISTANT_SECRET = json.dumps(
    {
        "role": "assistant",
        "content": "执行 sshpass -p hunter2 ssh root@host 检查",
        "reasoning": "",
        "timestamp": "2026-06-11T01:02:06.000Z",
    },
    ensure_ascii=False,
)


def _rich_redactor() -> Redactor:
    return Redactor(RedactionConfig(capture_content=True, max_string_length=4000))


class HermesParserTests(unittest.TestCase):
    def _parse(self, lines, **kwargs):
        with tempfile.TemporaryDirectory() as state:
            parser = HermesSessionParser(state_dir=state, **kwargs)
            spans = []
            for line in lines:
                spans.extend(parser.feed(line, SOURCE))
            return spans

    def test_captures_reasoning_message_and_tool(self):
        spans = self._parse(
            [ASSISTANT, TOOL], redactor=_rich_redactor(), capture_narrative=True
        )
        names = [s.name for s in spans]
        self.assertIn("reasoning", names)
        self.assertIn("message", names)
        self.assertIn("execute_tool terminal", names)
        self.assertEqual(len({s.trace_id for s in spans}), 1)

    def test_reasoning_text_is_captured(self):
        spans = self._parse([ASSISTANT], redactor=_rich_redactor(), capture_narrative=True)
        reasoning = next(s for s in spans if s.name == "reasoning")
        text = reasoning.events[0].attributes["text"]
        self.assertIn("query 工具", text)

    def test_tool_result_content_captured(self):
        spans = self._parse([ASSISTANT, TOOL], redactor=_rich_redactor(), capture_narrative=True)
        tool = next(s for s in spans if s.name == "execute_tool terminal")
        result = next(e for e in tool.events if e.name == "tool.result").attributes["result"]
        self.assertIn("68200", str(result))

    def test_narrative_disabled_by_default(self):
        spans = self._parse([ASSISTANT], capture_narrative=False)
        self.assertNotIn("reasoning", [s.name for s in spans])
        self.assertNotIn("message", [s.name for s in spans])

    def test_secret_scrubbed_in_message(self):
        spans = self._parse(
            [ASSISTANT_SECRET], redactor=_rich_redactor(), capture_narrative=True
        )
        message = next(s for s in spans if s.name == "message")
        text = message.events[0].attributes["text"]
        self.assertNotIn("hunter2", text)
        self.assertIn("[REDACTED]", text)

    def test_raw_mode_passthrough_keeps_secret(self):
        raw = Redactor(
            RedactionConfig(
                capture_content=True,
                max_string_length=1_000_000,
                sensitive_keys=(),
                content_keys=(),
                secret_patterns=(),
                credential_patterns=(),
            )
        )
        spans = self._parse([ASSISTANT_SECRET], redactor=raw, capture_narrative=True)
        text = next(s for s in spans if s.name == "message").events[0].attributes["text"]
        self.assertIn("hunter2", text)

    def test_malformed_line_never_raises(self):
        self.assertEqual(self._parse(["not json", "{}"], capture_narrative=True), [])

    def test_session_id_from_filename(self):
        spans = self._parse([ASSISTANT], redactor=_rich_redactor(), capture_narrative=True)
        self.assertTrue(all(s.attributes.get("session.id") == SESSION_ID for s in spans))


if __name__ == "__main__":
    unittest.main()
