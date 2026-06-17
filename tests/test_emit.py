import os
from pathlib import Path
import tempfile
import unittest

from agent_telemetry_skill import emit, session_trace
from agent_telemetry_skill.config import load_config, spool_dir
from agent_telemetry_skill.exporters import InMemoryExporter
from agent_telemetry_skill.schema import STATUS_ERROR, TelemetryEvent
from agent_telemetry_skill.spool import Spool


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


class _FailingExporter:
    def export(self, spans):
        raise RuntimeError("boom")


class EmitTestBase(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        for name in ENV_VARS:
            if name != "HOME":
                os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._tmp.name
        os.environ["AGENT_TELEMETRY_HOME"] = str(Path(self._tmp.name) / "telemetry-home")

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def _drain_spool(self) -> list:
        sink = InMemoryExporter()
        Spool(spool_dir(load_config())).drain(sink)
        return sink.spans


class BuildSpanTests(EmitTestBase):
    def test_build_span_stamps_resource_and_layer(self):
        span = emit.build_span(
            "execute_tool search",
            trace_id="a" * 32,
            parent_span_id="b" * 16,
            attributes={"gen_ai.tool.name": "search"},
        )

        self.assertEqual(span.trace_id, "a" * 32)
        self.assertEqual(span.parent_span_id, "b" * 16)
        self.assertEqual(span.attributes["telemetry.collection_layer"], "model_reported")
        self.assertEqual(span.attributes["service.name"], "local-agent")
        self.assertEqual(span.attributes["tenant.id"], "local-dev")
        self.assertEqual(span.attributes["deployment.environment"], "local")
        self.assertEqual(span.attributes["gen_ai.tool.name"], "search")
        self.assertEqual(span.end_time_unix_nano, span.start_time_unix_nano)

    def test_build_span_redacts_attributes_and_events(self):
        span = emit.build_span(
            "chat gpt-5-mini",
            trace_id="a" * 32,
            attributes={"api_key": "sk-proj-very-secret", "prompt": "tell me everything"},
            events=[
                {"name": "tool.result", "attributes": {"password": "hunter2"}},
                TelemetryEvent(name="note", attributes={"secret": "raw"}),
            ],
        )

        self.assertEqual(span.attributes["api_key"], "[REDACTED]")
        # non-secret content flows by default (rich capture); secrets still scrubbed
        self.assertEqual(span.attributes["prompt"], "tell me everything")
        self.assertEqual(span.events[0].attributes["password"], "[REDACTED]")
        self.assertEqual(span.events[1].attributes["secret"], "[REDACTED]")

    def test_build_span_honors_explicit_times_status_and_layer(self):
        span = emit.build_span(
            "execute_tool grep",
            trace_id="c" * 32,
            start_time_unix_nano=100,
            end_time_unix_nano=200,
            status_code=STATUS_ERROR,
            status_message="Timeout",
            collection_layer="log_watch",
        )

        self.assertEqual(span.start_time_unix_nano, 100)
        self.assertEqual(span.end_time_unix_nano, 200)
        self.assertEqual(span.status_code, STATUS_ERROR)
        self.assertEqual(span.status_message, "Timeout")
        self.assertEqual(span.attributes["telemetry.collection_layer"], "log_watch")


class EmitSpanTests(EmitTestBase):
    def test_emit_span_exports_prebuilt_span(self):
        exporter = InMemoryExporter()
        span = emit.build_span("execute_tool ls", trace_id="d" * 32)

        self.assertTrue(emit.emit_span(span, exporter=exporter))
        self.assertEqual(len(exporter.spans), 1)
        self.assertIs(exporter.spans[0], span)

    def test_emit_span_builds_from_kwargs_into_default_spool(self):
        ok = emit.emit_span(name="retrieve docs", trace_id="e" * 32)

        self.assertTrue(ok)
        spans = self._drain_spool()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].name, "retrieve docs")

    def test_emit_span_returns_false_on_exporter_failure(self):
        span = emit.build_span("x", trace_id="f" * 32)

        self.assertFalse(emit.emit_span(span, exporter=_FailingExporter()))

    def test_emit_span_noop_when_disabled(self):
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"
        exporter = InMemoryExporter()
        span = emit.build_span("x", trace_id="0" * 32)

        self.assertFalse(emit.emit_span(span, exporter=exporter))
        self.assertEqual(exporter.spans, [])


class EmitEventTests(EmitTestBase):
    def test_emit_event_joins_session_trace(self):
        record = session_trace.begin("sess-9", agent_name="bot")

        ok = emit.emit_event("tool.result", {"result_count": 2}, session_id="sess-9")

        self.assertTrue(ok)
        spans = self._drain_spool()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span.trace_id, record.trace_id)
        self.assertEqual(span.parent_span_id, record.root_span_id)
        self.assertEqual(span.attributes["session.id"], "sess-9")
        self.assertEqual(len(span.events), 1)
        self.assertEqual(span.events[0].name, "tool.result")
        self.assertEqual(span.events[0].attributes["result_count"], 2)

    def test_emit_event_auto_creates_session(self):
        ok = emit.emit_event("agent.decision", {"decision.name": "pick"}, session_id="early")

        self.assertTrue(ok)
        record = session_trace.get("early")
        self.assertIsNotNone(record)
        spans = self._drain_spool()
        self.assertEqual(spans[0].trace_id, record.trace_id)

    def test_emit_event_redacts_secret_attributes(self):
        emit.emit_event(
            "tool.result",
            {"api_key": "sk-proj-super-secret", "note": "Bearer abc.def.ghi"},
            session_id="sess-redact",
        )

        spans = self._drain_spool()
        event_attrs = spans[0].events[0].attributes
        self.assertEqual(event_attrs["api_key"], "[REDACTED]")
        self.assertNotIn("sk-proj-super-secret", str(event_attrs))
        self.assertNotIn("Bearer abc.def.ghi", str(event_attrs))

    def test_emit_event_with_explicit_trace_and_parent(self):
        exporter = InMemoryExporter()

        ok = emit.emit_event(
            "tool.result",
            {"k": "v"},
            trace_id="9" * 32,
            parent_span_id="8" * 16,
            exporter=exporter,
            collection_layer="hook",
        )

        self.assertTrue(ok)
        span = exporter.spans[0]
        self.assertEqual(span.trace_id, "9" * 32)
        self.assertEqual(span.parent_span_id, "8" * 16)
        self.assertEqual(span.attributes["telemetry.collection_layer"], "hook")
        self.assertNotIn("session.id", span.attributes)

    def test_emit_event_without_target_generates_trace(self):
        exporter = InMemoryExporter()

        self.assertTrue(emit.emit_event("standalone", {"a": 1}, exporter=exporter))
        self.assertEqual(len(exporter.spans[0].trace_id), 32)
        self.assertIsNone(exporter.spans[0].parent_span_id)

    def test_emit_event_noop_when_disabled(self):
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"

        self.assertFalse(emit.emit_event("e", {}, session_id="off"))
        self.assertEqual(self._drain_spool(), [])

    def test_emit_event_returns_false_on_failure(self):
        self.assertFalse(emit.emit_event("e", {}, exporter=_FailingExporter()))


if __name__ == "__main__":
    unittest.main()
