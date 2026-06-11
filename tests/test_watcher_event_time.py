"""Regression test for the event-timestamp bug surfaced during e2e testing.

Log watchers reconstruct spans from historical session logs. The ``tool.result``
event used to be stamped with the collection wall-clock (``now()``) instead of
the tool's end time from the log, so events landed far outside their own span
(e.g. a March span carrying a June event), breaking trace ordering on the
backend. Events must inherit the historical span time.
"""

from __future__ import annotations

import time
import unittest

from agent_telemetry_skill import emit
from agent_telemetry_skill.redaction import Redactor
from agent_telemetry_skill.watchers import _common
from agent_telemetry_skill import session_trace


# A fixed historical instant: 2026-03-20T17:45:00Z, in nanoseconds.
HISTORICAL_START_NS = 1_774_028_700_000_000_000
HISTORICAL_END_NS = HISTORICAL_START_NS + 52_000_000  # +52ms


class WatcherEventTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = session_trace.SessionTrace(
            session_id="sess-historical",
            trace_id="0" * 32,
            root_span_id="1" * 16,
            start_time_unix_nano=HISTORICAL_START_NS,
            agent_name="codex",
            attributes={},
        )

    def test_tool_result_event_uses_log_time_not_now(self) -> None:
        span = _common.make_tool_span(
            self.record,
            tool_name="exec_command",
            call_id="call_1",
            arguments={"cmd": "ls -la"},
            source_file="/logs/rollout.jsonl",
            start_time_unix_nano=HISTORICAL_START_NS,
            end_time_unix_nano=HISTORICAL_END_NS,
            result={"content_omitted": True, "char_count": 12},
            redactor=Redactor(),
        )
        self.assertEqual(len(span.events), 1)
        event = span.events[0]
        self.assertEqual(event.name, "tool.result")
        # The event must carry the tool's end time, not wall-clock now.
        self.assertEqual(event.time_unix_nano, HISTORICAL_END_NS)
        # And it must sit within the span's [start, end] window.
        self.assertGreaterEqual(event.time_unix_nano, span.start_time_unix_nano)
        self.assertLessEqual(event.time_unix_nano, span.end_time_unix_nano)

    def test_dict_event_defaults_to_span_end_not_now(self) -> None:
        span = emit.build_span(
            "execute_tool demo",
            trace_id="0" * 32,
            start_time_unix_nano=HISTORICAL_START_NS,
            end_time_unix_nano=HISTORICAL_END_NS,
            events=[{"name": "tool.result", "attributes": {"ok": True}}],
            redactor=Redactor(),
        )
        self.assertEqual(span.events[0].time_unix_nano, HISTORICAL_END_NS)

    def test_explicit_event_time_is_honored(self) -> None:
        mid = HISTORICAL_START_NS + 10_000_000
        span = emit.build_span(
            "execute_tool demo",
            trace_id="0" * 32,
            start_time_unix_nano=HISTORICAL_START_NS,
            end_time_unix_nano=HISTORICAL_END_NS,
            events=[
                {"name": "x", "attributes": {}, "time_unix_nano": mid},
            ],
            redactor=Redactor(),
        )
        self.assertEqual(span.events[0].time_unix_nano, mid)

    def test_historical_event_is_not_near_present(self) -> None:
        span = _common.make_tool_span(
            self.record,
            tool_name="exec_command",
            call_id="call_2",
            arguments={"cmd": "pwd"},
            source_file="/logs/rollout.jsonl",
            start_time_unix_nano=HISTORICAL_START_NS,
            end_time_unix_nano=HISTORICAL_END_NS,
            result={"content_omitted": True, "char_count": 3},
            redactor=Redactor(),
        )
        now_ns = time.time_ns()
        # The reconstructed event must be far in the past, not ~now.
        self.assertLess(span.events[0].time_unix_nano, now_ns - 60 * 1_000_000_000)


if __name__ == "__main__":
    unittest.main()
