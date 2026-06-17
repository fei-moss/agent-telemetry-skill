import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from agent_telemetry_skill import session_trace
from agent_telemetry_skill.config import load_config, spool_dir
from agent_telemetry_skill.redaction import RedactionConfig, Redactor
from agent_telemetry_skill.spool import Spool
from agent_telemetry_skill.watchers.codex import CodexParser, _session_id_from_path


def _rich_redactor() -> Redactor:
    """Content-capturing redactor so narrative text is emitted, not omitted."""
    return Redactor(RedactionConfig(capture_content=True, max_string_length=4000))


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

REPO_ROOT = Path(__file__).parent.parent
FIXTURES_DIR = Path(__file__).parent / "fixtures"
CODEX_FIXTURE = FIXTURES_DIR / "codex_rollout.jsonl"
SESSION_ID = "0fix0000-0000-7000-8000-codexfixture"
ROLLOUT_NAME = "rollout-2026-01-15T11-00-00-0fix0000-0000-7000-8000-codexfixture.jsonl"


class WatcherTestBase(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        for name in ENV_VARS:
            if name != "HOME":
                os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        os.environ["HOME"] = self._tmp.name
        os.environ["AGENT_TELEMETRY_HOME"] = str(self.tmp_path / "telemetry-home")

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()


class CodexParserTests(WatcherTestBase):
    def _feed_fixture(self, parser: CodexParser):
        spans = []
        source = str(self.tmp_path / ROLLOUT_NAME)
        for line in CODEX_FIXTURE.read_text(encoding="utf-8").splitlines():
            spans.extend(parser.feed(line, source))
        return spans, source

    def test_function_call_correlates_with_output(self):
        spans, source = self._feed_fixture(CodexParser())

        tool_spans = [span for span in spans if span.name.startswith("execute_tool")]
        self.assertEqual(len(tool_spans), 1)
        tool = tool_spans[0]
        self.assertEqual(tool.name, "execute_tool exec_command")
        self.assertEqual(tool.attributes["gen_ai.tool.name"], "exec_command")
        self.assertEqual(tool.attributes["tool.call.id"], "call_fixture_1")
        self.assertEqual(tool.attributes["telemetry.source.file"], source)
        self.assertEqual(tool.attributes["tool.arguments.cmd"], "echo fixture")
        self.assertLess(tool.start_time_unix_nano, tool.end_time_unix_nano)

    def test_token_count_produces_one_chat_span_with_usage(self):
        spans, _ = self._feed_fixture(CodexParser())

        chat_spans = [span for span in spans if span.name.startswith("chat")]
        # The second token_count entry has info=null and must be skipped.
        self.assertEqual(len(chat_spans), 1)
        chat = chat_spans[0]
        self.assertEqual(chat.name, "chat gpt-fixture-5")
        self.assertEqual(chat.attributes["gen_ai.request.model"], "gpt-fixture-5")
        self.assertEqual(chat.attributes["gen_ai.usage.input_tokens"], 350)
        self.assertEqual(chat.attributes["gen_ai.usage.output_tokens"], 42)

    def test_spans_join_session_trace_from_session_meta(self):
        spans, _ = self._feed_fixture(CodexParser())

        record = session_trace.get(SESSION_ID)
        self.assertIsNotNone(record)
        self.assertEqual(record.agent_name, "codex")
        for span in spans:
            self.assertEqual(span.trace_id, record.trace_id)
            self.assertEqual(span.parent_span_id, record.root_span_id)
            self.assertEqual(span.attributes["telemetry.collection_layer"], "log_watch")
            self.assertEqual(span.attributes["session.id"], SESSION_ID)

    def test_secrets_in_arguments_are_redacted(self):
        spans, _ = self._feed_fixture(CodexParser())

        tool = next(span for span in spans if span.name.startswith("execute_tool"))
        self.assertEqual(tool.attributes["tool.arguments.api_key"], "[REDACTED]")
        serialized = json.dumps([span.to_dict() for span in spans], ensure_ascii=False)
        self.assertNotIn("fixturesecretvalue9999", serialized)

    def test_unknown_and_malformed_lines_are_skipped(self):
        parser = CodexParser()
        source = ROLLOUT_NAME

        self.assertEqual(parser.feed("not json at all", source), [])
        self.assertEqual(parser.feed('{"type":"event_msg"}', source), [])  # no payload
        self.assertEqual(
            parser.feed('{"type":"some_future_entry","payload":{"type":"x"}}', source), []
        )
        self.assertEqual(parser.feed(json.dumps([1, 2, 3]), source), [])

    def test_session_id_falls_back_to_rollout_file_name(self):
        parser = CodexParser()
        source = "rollout-2026-02-01T09-30-00-feedface-0000-7000-8000-fallbackcase.jsonl"
        call = json.dumps(
            {
                "timestamp": "2026-02-01T09:30:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": "{\"cmd\":\"true\"}",
                    "call_id": "call_fallback_1",
                },
            }
        )
        output = json.dumps(
            {
                "timestamp": "2026-02-01T09:30:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_fallback_1",
                    "output": "ok",
                },
            }
        )

        self.assertEqual(parser.feed(call, source), [])
        spans = parser.feed(output, source)

        self.assertEqual(len(spans), 1)
        self.assertEqual(
            spans[0].attributes["session.id"],
            "feedface-0000-7000-8000-fallbackcase",
        )

    def test_session_id_from_path_handles_odd_names(self):
        self.assertEqual(_session_id_from_path("weird-name.jsonl"), "weird-name")
        self.assertEqual(_session_id_from_path("rollout-x.jsonl"), "x")

    def test_message_narrative_suppressed_when_disabled(self):
        spans, _ = self._feed_fixture(CodexParser(capture_narrative=False))

        self.assertEqual([s for s in spans if s.name == "message"], [])

    def test_message_narrative_captures_user_and_assistant_only(self):
        parser = CodexParser(redactor=_rich_redactor(), capture_narrative=True)
        spans, source = self._feed_fixture(parser)

        messages = [s for s in spans if s.name == "message"]
        # developer-role message must be filtered out; user + assistant kept
        self.assertEqual(len(messages), 2)
        texts = [m.events[0].attributes["text"] for m in messages]
        self.assertIn("run the demo command and report back", texts[0])
        self.assertIn("Done", texts[1])
        for message in messages:
            self.assertEqual(message.attributes["narrative.kind"], "message")
            self.assertEqual(message.attributes["telemetry.source.file"], source)
        # narrative.sequence preserves human-display order
        self.assertLess(
            messages[0].attributes["narrative.sequence"],
            messages[1].attributes["narrative.sequence"],
        )

    def test_message_secret_is_redacted(self):
        parser = CodexParser(redactor=_rich_redactor(), capture_narrative=True)
        spans, _ = self._feed_fixture(parser)

        assistant = [s for s in spans if s.name == "message"][1]
        text = assistant.events[0].attributes["text"]
        self.assertNotIn("fixturesecretvalue9999", text)
        self.assertIn("[REDACTED]", text)

    def test_reasoning_summary_captured_and_encrypted_skipped(self):
        parser = CodexParser(redactor=_rich_redactor(), capture_narrative=True)
        spans, _ = self._feed_fixture(parser)

        reasoning = [s for s in spans if s.name == "reasoning"]
        # one entry carries summary text; the encrypted-only entry is skipped
        self.assertEqual(len(reasoning), 1)
        text = reasoning[0].events[0].attributes["text"]
        self.assertIn("先运行 demo 命令", text)

    def test_reasoning_suppressed_when_disabled(self):
        spans, _ = self._feed_fixture(CodexParser(capture_narrative=False))

        self.assertEqual([s for s in spans if s.name == "reasoning"], [])

    def test_message_spans_join_session_trace(self):
        parser = CodexParser(redactor=_rich_redactor(), capture_narrative=True)
        spans, _ = self._feed_fixture(parser)

        record = session_trace.get(SESSION_ID)
        for message in (s for s in spans if s.name == "message"):
            self.assertEqual(message.trace_id, record.trace_id)
            self.assertEqual(message.attributes["session.id"], SESSION_ID)
            self.assertEqual(
                message.attributes["telemetry.collection_layer"], "log_watch"
            )


class WatchSessionsScriptTests(WatcherTestBase):
    def _load_script(self):
        spec = importlib.util.spec_from_file_location(
            "watch_sessions_under_test", REPO_ROOT / "scripts" / "watch_sessions.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def _stage_rollout(self) -> str:
        staged = self.tmp_path / "codex-logs" / ROLLOUT_NAME
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CODEX_FIXTURE, staged)
        return str(staged.parent / "rollout-*.jsonl")

    def test_once_spools_spans_from_codex_logs(self):
        module = self._load_script()
        pattern = self._stage_rollout()

        exit_code = module.main(["--runtime", "codex", "--once", "--codex-glob", pattern])

        self.assertEqual(exit_code, 0)
        depth = Spool(spool_dir(load_config())).depth()
        self.assertGreater(depth, 0)

    def test_once_is_noop_when_disabled(self):
        module = self._load_script()
        pattern = self._stage_rollout()
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"

        exit_code = module.main(["--runtime", "codex", "--once", "--codex-glob", pattern])

        self.assertEqual(exit_code, 0)
        self.assertEqual(Spool(spool_dir(load_config())).depth(), 0)

    def test_status_prints_offsets_and_spool_depth(self):
        module = self._load_script()
        pattern = self._stage_rollout()
        module.main(["--runtime", "codex", "--once", "--codex-glob", pattern])

        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = module.main(
                ["--runtime", "codex", "--status", "--codex-glob", pattern]
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertIn("offsets", payload)
        self.assertIn("spool_depth", payload)
        self.assertEqual(len(payload["offsets"]), 1)

    def test_restart_does_not_duplicate_spans(self):
        module = self._load_script()
        pattern = self._stage_rollout()
        module.main(["--runtime", "codex", "--once", "--codex-glob", pattern])
        depth_after_first = Spool(spool_dir(load_config())).depth()

        module.main(["--runtime", "codex", "--once", "--codex-glob", pattern])

        self.assertEqual(Spool(spool_dir(load_config())).depth(), depth_after_first)


if __name__ == "__main__":
    unittest.main()
