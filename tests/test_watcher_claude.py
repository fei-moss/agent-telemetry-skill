import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from agent_telemetry_skill import session_trace
from agent_telemetry_skill.schema import STATUS_OK
from agent_telemetry_skill.watchers.claude_code import ClaudeCodeParser
from agent_telemetry_skill.watchers.tailer import Tailer


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

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CLAUDE_FIXTURE = FIXTURES_DIR / "claude_code_session.jsonl"
SESSION_ID = "cc-fixture-session-0001"


def _nano(iso_text: str) -> int:
    parsed = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


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
        self.state_dir = self.tmp_path / "watch-state"

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()


class TailerTests(WatcherTestBase):
    def _make_log(self, name: str = "session.jsonl") -> Path:
        path = self.tmp_path / "logs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path

    def test_reads_only_complete_lines(self):
        path = self._make_log()
        path.write_text('{"a":1}\n{"partial', encoding="utf-8")
        tailer = Tailer([str(path)], state_dir=self.state_dir)

        batch = tailer.poll_once()

        self.assertEqual(batch, [(str(path), '{"a":1}')])
        # The partial tail line becomes readable once terminated.
        with path.open("a", encoding="utf-8") as handle:
            handle.write('"}\n')
        self.assertEqual(tailer.poll_once(), [(str(path), '{"partial"}')])

    def test_offsets_persist_across_tailer_restarts(self):
        path = self._make_log()
        path.write_text("line-one\n", encoding="utf-8")
        first = Tailer([str(path)], state_dir=self.state_dir)
        self.assertEqual(len(first.poll_once()), 1)

        restarted = Tailer([str(path)], state_dir=self.state_dir)
        self.assertEqual(restarted.poll_once(), [])  # nothing replayed

        with path.open("a", encoding="utf-8") as handle:
            handle.write("line-two\n")
        self.assertEqual(restarted.poll_once(), [(str(path), "line-two")])

        state_file = self.state_dir / "watch_offsets.json"
        offsets = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(offsets[str(path)], path.stat().st_size)

    def test_truncation_resets_offset(self):
        path = self._make_log()
        path.write_text("a-very-long-first-line\n", encoding="utf-8")
        tailer = Tailer([str(path)], state_dir=self.state_dir)
        tailer.poll_once()

        path.write_text("fresh\n", encoding="utf-8")  # truncated and rewritten

        self.assertEqual(tailer.poll_once(), [(str(path), "fresh")])

    def test_discover_matches_glob_and_ignores_missing(self):
        path = self._make_log("match-1.jsonl")
        self._make_log("match-2.jsonl")
        tailer = Tailer(
            [str(self.tmp_path / "logs" / "match-*.jsonl"), "/nonexistent/*.jsonl"],
            state_dir=self.state_dir,
        )

        discovered = tailer.discover()

        self.assertEqual(len(discovered), 2)
        self.assertIn(path, discovered)


class ClaudeCodeParserTests(WatcherTestBase):
    def _feed_fixture(self, parser: ClaudeCodeParser):
        spans = []
        source = str(self.tmp_path / "transcript.jsonl")
        for line in CLAUDE_FIXTURE.read_text(encoding="utf-8").splitlines():
            spans.extend(parser.feed(line, source))
        return spans, source

    def test_tool_use_correlates_with_tool_result(self):
        spans, source = self._feed_fixture(ClaudeCodeParser())

        tool_spans = [span for span in spans if span.name.startswith("execute_tool")]
        self.assertEqual(len(tool_spans), 1)
        tool = tool_spans[0]
        self.assertEqual(tool.name, "execute_tool Bash")
        self.assertEqual(tool.attributes["gen_ai.tool.name"], "Bash")
        self.assertEqual(tool.attributes["tool.call.id"], "toolu_fixture_001")
        self.assertEqual(tool.attributes["telemetry.source.file"], source)
        self.assertEqual(tool.status_code, STATUS_OK)
        self.assertEqual(tool.start_time_unix_nano, _nano("2026-01-15T10:00:05.000Z"))
        self.assertEqual(tool.end_time_unix_nano, _nano("2026-01-15T10:00:07.500Z"))

    def test_chat_spans_extract_usage_and_dedupe_streamed_turns(self):
        spans, _ = self._feed_fixture(ClaudeCodeParser())

        chat_spans = [span for span in spans if span.name.startswith("chat")]
        # msg_fixture_002 appears twice in the transcript but counts once.
        self.assertEqual(len(chat_spans), 2)
        first, second = chat_spans
        self.assertEqual(first.name, "chat claude-fixture-1")
        self.assertEqual(first.attributes["gen_ai.request.model"], "claude-fixture-1")
        self.assertEqual(first.attributes["gen_ai.usage.input_tokens"], 120)
        self.assertEqual(first.attributes["gen_ai.usage.output_tokens"], 45)
        self.assertEqual(second.attributes["gen_ai.usage.input_tokens"], 200)
        self.assertEqual(second.attributes["gen_ai.usage.output_tokens"], 30)

    def test_spans_join_the_session_trace_with_log_watch_layer(self):
        spans, _ = self._feed_fixture(ClaudeCodeParser())

        record = session_trace.get(SESSION_ID)
        self.assertIsNotNone(record)
        self.assertEqual(record.agent_name, "claude-code")
        for span in spans:
            self.assertEqual(span.trace_id, record.trace_id)
            self.assertEqual(span.parent_span_id, record.root_span_id)
            self.assertEqual(span.attributes["telemetry.collection_layer"], "log_watch")
            self.assertEqual(span.attributes["session.id"], SESSION_ID)

    def test_secrets_in_tool_arguments_are_redacted(self):
        spans, _ = self._feed_fixture(ClaudeCodeParser())

        tool = next(span for span in spans if span.name.startswith("execute_tool"))
        self.assertEqual(tool.attributes["tool.arguments.api_key"], "[REDACTED]")
        serialized = json.dumps([span.to_dict() for span in spans], ensure_ascii=False)
        # secrets are always scrubbed, even though content capture is ON by default
        self.assertNotIn("fixturesecretvalue1234", serialized)
        # non-secret tool content flows by default (rich capture)
        self.assertIn("alpha.txt", serialized)

    def test_unknown_and_malformed_lines_are_skipped(self):
        parser = ClaudeCodeParser()
        source = "transcript.jsonl"

        self.assertEqual(parser.feed("not json at all", source), [])
        self.assertEqual(parser.feed('{"type":"file-history-snapshot"}', source), [])
        self.assertEqual(parser.feed('{"type":"mystery","sessionId":"s1"}', source), [])
        self.assertEqual(parser.feed(json.dumps(["a", "list"]), source), [])
        self.assertEqual(parser.feed('{"type":"assistant"}', source), [])  # no sessionId

    def test_tool_result_without_seen_tool_use_is_best_effort(self):
        parser = ClaudeCodeParser()
        line = json.dumps(
            {
                "type": "user",
                "sessionId": SESSION_ID,
                "timestamp": "2026-01-15T10:05:00.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_orphan_001",
                            "content": "orphan output",
                            "is_error": False,
                        }
                    ],
                },
            }
        )

        spans = parser.feed(line, "transcript.jsonl")

        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].name, "execute_tool unknown")
        self.assertEqual(spans[0].start_time_unix_nano, spans[0].end_time_unix_nano)


if __name__ == "__main__":
    unittest.main()
