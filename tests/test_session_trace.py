import os
from pathlib import Path
import tempfile
import unittest

from agent_telemetry_skill import session_trace
from agent_telemetry_skill.exporters import InMemoryExporter
from agent_telemetry_skill.schema import STATUS_ERROR, STATUS_OK


ENV_VARS = (
    "AGENT_TELEMETRY_ENDPOINT",
    "AGENT_TELEMETRY_TOKEN",
    "AGENT_TELEMETRY_SERVICE",
    "AGENT_TELEMETRY_TENANT",
    "AGENT_TELEMETRY_ENVIRONMENT",
    "AGENT_TELEMETRY_CAPTURE_CONTENT",
    "AGENT_TELEMETRY_OUTPUT",
    "AGENT_TELEMETRY_HOME",
    "AGENT_TELEMETRY_ENABLED",
    "HOME",
)


class SessionTraceTestBase(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        for name in ENV_VARS:
            if name != "HOME":
                os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._tmp.name
        os.environ["AGENT_TELEMETRY_HOME"] = str(Path(self._tmp.name) / "telemetry-home")
        self.state_dir = Path(self._tmp.name) / "state"

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()


class BeginTests(SessionTraceTestBase):
    def test_begin_is_idempotent(self):
        first = session_trace.begin("session-1", agent_name="bot", state_dir=self.state_dir)
        second = session_trace.begin("session-1", state_dir=self.state_dir)

        self.assertEqual(first.trace_id, second.trace_id)
        self.assertEqual(first.root_span_id, second.root_span_id)
        self.assertEqual(first.start_time_unix_nano, second.start_time_unix_nano)
        self.assertEqual(second.agent_name, "bot")

    def test_cross_process_continuity_shares_trace_id(self):
        # Two independent begin calls with no shared in-memory state simulate
        # two separate short-lived processes joining the same session.
        process_a = session_trace.begin("shared-session", state_dir=self.state_dir)
        process_b = session_trace.begin("shared-session", state_dir=self.state_dir)

        self.assertEqual(process_a.trace_id, process_b.trace_id)
        self.assertEqual(process_a.root_span_id, process_b.root_span_id)

    def test_begin_uses_state_dir_from_config_by_default(self):
        record = session_trace.begin("env-session")

        fetched = session_trace.get("env-session")

        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.trace_id, record.trace_id)

    def test_begin_redacts_sensitive_attributes(self):
        record = session_trace.begin(
            "secret-session",
            attributes={"api_key": "sk-proj-super-secret-value"},
            state_dir=self.state_dir,
        )

        self.assertEqual(record.attributes["api_key"], "[REDACTED]")

    def test_weird_session_ids_are_sanitized_and_distinct(self):
        first = session_trace.begin("a/b:c d!", state_dir=self.state_dir)
        second = session_trace.begin("a/b:c_d!", state_dir=self.state_dir)

        self.assertNotEqual(first.trace_id, second.trace_id)
        self.assertEqual(
            session_trace.get("a/b:c d!", state_dir=self.state_dir).trace_id,
            first.trace_id,
        )

    def test_begin_never_raises_on_broken_state_dir(self):
        blocker = Path(self._tmp.name) / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")

        record = session_trace.begin("s", state_dir=blocker / "child")

        self.assertEqual(record.session_id, "s")
        self.assertTrue(record.trace_id)


class GetTests(SessionTraceTestBase):
    def test_get_unknown_session_returns_none(self):
        self.assertIsNone(session_trace.get("nope", state_dir=self.state_dir))


class EndTests(SessionTraceTestBase):
    def test_end_emits_root_span_to_exporter(self):
        record = session_trace.begin("run-1", agent_name="my-agent", state_dir=self.state_dir)
        exporter = InMemoryExporter()

        root = session_trace.end(
            "run-1",
            attributes={"extra": "value"},
            status_code=STATUS_ERROR,
            exporter=exporter,
            state_dir=self.state_dir,
        )

        self.assertIsNotNone(root)
        self.assertEqual(len(exporter.spans), 1)
        span = exporter.spans[0]
        self.assertEqual(span.name, "agent.run my-agent")
        self.assertEqual(span.trace_id, record.trace_id)
        self.assertEqual(span.span_id, record.root_span_id)
        self.assertEqual(span.start_time_unix_nano, record.start_time_unix_nano)
        self.assertIsNotNone(span.end_time_unix_nano)
        self.assertGreaterEqual(span.end_time_unix_nano, span.start_time_unix_nano)
        self.assertEqual(span.status_code, STATUS_ERROR)
        self.assertEqual(span.attributes["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(span.attributes["session.id"], "run-1")
        self.assertEqual(span.attributes["gen_ai.agent.name"], "my-agent")
        self.assertEqual(span.attributes["extra"], "value")
        self.assertIn("telemetry.collection_layer", span.attributes)

    def test_end_is_idempotent(self):
        session_trace.begin("run-2", state_dir=self.state_dir)
        exporter = InMemoryExporter()

        first = session_trace.end("run-2", exporter=exporter, state_dir=self.state_dir)
        second = session_trace.end("run-2", exporter=exporter, state_dir=self.state_dir)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(exporter.spans), 1)

    def test_end_unknown_session_returns_none(self):
        self.assertIsNone(session_trace.end("missing", state_dir=self.state_dir))

    def test_end_uses_session_id_in_name_when_agent_name_missing(self):
        session_trace.begin("anon-run", state_dir=self.state_dir)
        exporter = InMemoryExporter()

        root = session_trace.end("anon-run", exporter=exporter, state_dir=self.state_dir)

        self.assertEqual(root.name, "agent.run anon-run")
        self.assertEqual(root.status_code, STATUS_OK)

    def test_end_skips_export_when_disabled(self):
        session_trace.begin("disabled-run", state_dir=self.state_dir)
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"
        exporter = InMemoryExporter()

        root = session_trace.end("disabled-run", exporter=exporter, state_dir=self.state_dir)

        self.assertIsNotNone(root)
        self.assertEqual(exporter.spans, [])


class OpenSpanTests(SessionTraceTestBase):
    def test_record_open_pop_open_round_trip(self):
        payload = {"start_time_unix_nano": 123, "tool": "search"}

        recorded = session_trace.record_open(
            "sess", "tool-call-7", payload, state_dir=self.state_dir
        )
        popped = session_trace.pop_open("sess", "tool-call-7", state_dir=self.state_dir)

        self.assertTrue(recorded)
        self.assertEqual(popped["start_time_unix_nano"], 123)
        self.assertEqual(popped["tool"], "search")

    def test_pop_open_second_call_returns_none(self):
        session_trace.record_open("sess", "key", {"a": 1}, state_dir=self.state_dir)

        self.assertIsNotNone(session_trace.pop_open("sess", "key", state_dir=self.state_dir))
        self.assertIsNone(session_trace.pop_open("sess", "key", state_dir=self.state_dir))

    def test_pop_open_unknown_key_returns_none(self):
        self.assertIsNone(session_trace.pop_open("sess", "missing", state_dir=self.state_dir))

    def test_record_open_redacts_secrets(self):
        session_trace.record_open(
            "sess",
            "secret-key",
            {"password": "hunter2", "depth": 3},
            state_dir=self.state_dir,
        )

        popped = session_trace.pop_open("sess", "secret-key", state_dir=self.state_dir)

        self.assertEqual(popped["password"], "[REDACTED]")
        self.assertEqual(popped["depth"], 3)

    def test_end_discards_open_spans(self):
        session_trace.begin("run-3", state_dir=self.state_dir)
        session_trace.record_open("run-3", "k", {"a": 1}, state_dir=self.state_dir)

        session_trace.end("run-3", exporter=InMemoryExporter(), state_dir=self.state_dir)

        self.assertIsNone(session_trace.pop_open("run-3", "k", state_dir=self.state_dir))


class CountSessionsTests(SessionTraceTestBase):
    def test_count_sessions_tracks_lifecycle(self):
        self.assertEqual(session_trace.count_sessions(self.state_dir), 0)
        session_trace.begin("c1", state_dir=self.state_dir)
        session_trace.begin("c2", state_dir=self.state_dir)
        self.assertEqual(session_trace.count_sessions(self.state_dir), 2)

        session_trace.end("c1", exporter=InMemoryExporter(), state_dir=self.state_dir)

        self.assertEqual(session_trace.count_sessions(self.state_dir), 1)


if __name__ == "__main__":
    unittest.main()
