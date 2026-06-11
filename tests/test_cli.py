import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from agent_telemetry_skill import cli


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
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "HOME",
)


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        for name in ENV_VARS:
            if name != "HOME":
                os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._tmp.name
        os.environ["AGENT_TELEMETRY_HOME"] = str(Path(self._tmp.name) / "telemetry-home")
        self.output_path = Path(self._tmp.name) / "out.jsonl"
        os.environ["AGENT_TELEMETRY_OUTPUT"] = str(self.output_path)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def _run(self, argv: list[str], stdin_text: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        finally:
            sys.stdin = original_stdin
        return code, out.getvalue(), err.getvalue()

    def _read_output_spans(self) -> list[dict]:
        if not self.output_path.exists():
            return []
        lines = self.output_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


class SessionEndToEndTests(CliTestBase):
    def test_session_lifecycle_joins_one_trace_in_output_file(self):
        code, out, _ = self._run(
            ["session", "start", "--session-id", "s1", "--agent-name", "bot"]
        )
        self.assertEqual(code, 0)
        started = json.loads(out)
        self.assertEqual(started["session_id"], "s1")

        code, _, _ = self._run(
            ["emit-event", "tool.result", "--session-id", "s1"],
            stdin_text=json.dumps({"result_count": 2, "api_key": "sk-proj-super-secret"}),
        )
        self.assertEqual(code, 0)

        code, _, _ = self._run(
            [
                "decision",
                "pick_tool",
                "--rationale",
                "needs search",
                "--confidence",
                "0.8",
                "--session-id",
                "s1",
            ]
        )
        self.assertEqual(code, 0)

        code, _, _ = self._run(["session", "end", "--session-id", "s1"])
        self.assertEqual(code, 0)

        code, drain_out, _ = self._run(["drain"])
        self.assertEqual(code, 0)
        self.assertIn("exported", drain_out)

        spans = self._read_output_spans()
        self.assertGreaterEqual(len(spans), 3)
        trace_ids = {span["trace_id"] for span in spans}
        self.assertEqual(trace_ids, {started["trace_id"]})

        roots = [span for span in spans if span["name"] == "agent.run bot"]
        self.assertEqual(len(roots), 1)
        root = roots[0]
        self.assertEqual(root["span_id"], started["root_span_id"])
        self.assertEqual(root["attributes"]["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(root["attributes"]["session.id"], "s1")

        children = [span for span in spans if span is not root]
        for child in children:
            self.assertEqual(child["parent_span_id"], root["span_id"])
            self.assertIn("telemetry.collection_layer", child["attributes"])

        raw_text = self.output_path.read_text(encoding="utf-8")
        self.assertNotIn("sk-proj-super-secret", raw_text)
        self.assertIn("[REDACTED]", raw_text)

        decision_spans = [span for span in spans if span["name"] == "agent.decision"]
        self.assertEqual(len(decision_spans), 1)
        decision_event = decision_spans[0]["events"][0]
        self.assertEqual(decision_event["attributes"]["decision.name"], "pick_tool")
        self.assertEqual(decision_event["attributes"]["decision.confidence"], 0.8)

    def test_session_end_unknown_session_warns_but_exits_zero(self):
        code, _, err = self._run(["session", "end", "--session-id", "ghost"])

        self.assertEqual(code, 0)
        self.assertIn("warning", err)

    def test_emit_event_with_session_uses_layer_flag(self):
        self._run(["session", "start", "--session-id", "s2"])
        code, _, _ = self._run(
            ["--layer", "hook", "emit-event", "tool.result", "--session-id", "s2"],
            stdin_text="{}",
        )
        self.assertEqual(code, 0)
        self._run(["session", "end", "--session-id", "s2"])
        self._run(["drain"])

        spans = self._read_output_spans()
        event_spans = [span for span in spans if span["name"] == "tool.result"]
        self.assertEqual(
            event_spans[0]["attributes"]["telemetry.collection_layer"], "hook"
        )


class LegacyEmitEventTests(CliTestBase):
    def test_emit_event_without_session_runs_standalone(self):
        code, _, _ = self._run(
            ["emit-event", "telemetry.fallback", "--run-name", "fallback-run"],
            stdin_text=json.dumps({"reason": "sdk unavailable"}),
        )

        self.assertEqual(code, 0)
        spans = self._read_output_spans()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["name"], "agent.run fallback-run")
        self.assertEqual(spans[0]["events"][0]["name"], "telemetry.fallback")

    def test_emit_event_with_bad_stdin_still_exits_zero(self):
        code, _, err = self._run(
            ["emit-event", "x", "--session-id", "s3"], stdin_text="{not json"
        )

        self.assertEqual(code, 0)
        self.assertIn("warning", err)


class DrainAndStatusTests(CliTestBase):
    def test_drain_without_sink_prints_depth_message(self):
        os.environ.pop("AGENT_TELEMETRY_OUTPUT", None)

        code, out, _ = self._run(["drain"])

        self.assertEqual(code, 0)
        self.assertIn("no endpoint or output configured", out)

    def test_status_masks_token_and_reports_counts(self):
        os.environ["AGENT_TELEMETRY_TOKEN"] = "super-secret-token-value"
        self._run(["session", "start", "--session-id", "s4"])

        code, out, _ = self._run(["status"])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertNotIn("super-secret-token-value", out)
        self.assertEqual(payload["token"], "supe***")
        self.assertEqual(payload["service"], "local-agent")
        self.assertEqual(payload["session_count"], 1)
        self.assertIn("spool_depth", payload)

    def test_status_respects_service_name_flag(self):
        code, out, _ = self._run(["--service-name", "svc-x", "status"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["service"], "svc-x")


class DisabledAndFailureTests(CliTestBase):
    def test_disabled_emit_event_is_noop(self):
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"

        code, _, err = self._run(
            ["emit-event", "e", "--session-id", "s5"], stdin_text="{}"
        )

        self.assertEqual(code, 0)
        self.assertIn("disabled", err)
        self.assertEqual(self._read_output_spans(), [])

    def test_broken_output_sink_still_exits_zero(self):
        broken_dir = Path(self._tmp.name) / "broken-output"
        broken_dir.mkdir()
        os.environ["AGENT_TELEMETRY_OUTPUT"] = str(broken_dir)

        code, _, _ = self._run(
            ["emit-event", "e", "--session-id", "s6"], stdin_text="{}"
        )

        self.assertEqual(code, 0)

    def test_demo_prints_trace_to_stdout(self):
        code, out, _ = self._run(["demo"])

        self.assertEqual(code, 0)
        self.assertIn("agent.run demo", out)
        self.assertIn("telemetry.collection_layer", out)


if __name__ == "__main__":
    unittest.main()
