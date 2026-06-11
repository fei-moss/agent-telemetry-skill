from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO_ROOT / "scripts" / "hooks" / "claude_code_hook.py"
INSTALL_PY = REPO_ROOT / "adapters" / "claude_code" / "install.py"

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
    "AGENT_TELEMETRY_DEBUG",
    "HOME",
)

SESSION_ID = "claude-sess-1"
TOOL_USE_ID = "toolu_01"
FAKE_SECRET = "sk-proj-not-a-real-key-12345"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaudeHookTestBase(unittest.TestCase):
    """Isolated AGENT_TELEMETRY_HOME + in-process hook invocation helpers."""

    def setUp(self) -> None:
        self.hook = _load_module("claude_code_hook_under_test", HOOK_SCRIPT)
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        for name in ENV_VARS:
            if name != "HOME":
                os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["HOME"] = self.tmp.name
        self.home = Path(self.tmp.name) / "telemetry-home"
        os.environ["AGENT_TELEMETRY_HOME"] = str(self.home)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def run_hook(self, event: str, payload: dict[str, Any] | str) -> tuple[int, str]:
        """Invoke main() in-process with payload on stdin; capture stdout."""
        stdin_text = payload if isinstance(payload, str) else json.dumps(payload)
        stdout = io.StringIO()
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with contextlib.redirect_stdout(stdout):
                code = self.hook.main(["--event", event])
        finally:
            sys.stdin = original_stdin
        return code, stdout.getvalue()

    def read_spool_spans(self) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        spool = self.home / "spool"
        if not spool.is_dir():
            return spans
        for path in sorted(spool.glob("pending-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    spans.append(json.loads(line))
        return spans


class FullSessionFlowTests(ClaudeHookTestBase):
    def _base_payload(self, event: str) -> dict[str, Any]:
        return {
            "session_id": SESSION_ID,
            "transcript_path": f"{self.tmp.name}/transcript.jsonl",
            "cwd": self.tmp.name,
            "hook_event_name": event,
        }

    def test_session_flow_lands_correlated_redacted_spans(self) -> None:
        # Arrange / Act: SessionStart -> PreToolUse -> PostToolUse -> SessionEnd.
        start_payload = {**self._base_payload("SessionStart"), "source": "startup"}
        pre_payload = {
            **self._base_payload("PreToolUse"),
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la", "api_key": FAKE_SECRET},
            "tool_use_id": TOOL_USE_ID,
        }
        post_payload = {
            **pre_payload,
            "hook_event_name": "PostToolUse",
            "tool_response": "total 0",
        }
        end_payload = {**self._base_payload("SessionEnd"), "reason": "other"}
        for event, payload in (
            ("SessionStart", start_payload),
            ("PreToolUse", pre_payload),
            ("PostToolUse", post_payload),
            ("SessionEnd", end_payload),
        ):
            code, stdout = self.run_hook(event, payload)
            self.assertEqual(code, 0, event)
            self.assertEqual(stdout, "", f"{event} hook wrote to stdout")

        # Assert: spool holds an execute_tool span plus the session root span.
        spans = self.read_spool_spans()
        tool_spans = [span for span in spans if span["name"] == "execute_tool Bash"]
        root_spans = [span for span in spans if span["name"].startswith("agent.run ")]
        self.assertEqual(len(tool_spans), 1)
        self.assertEqual(len(root_spans), 1)
        tool, root = tool_spans[0], root_spans[0]

        self.assertEqual(root["name"], "agent.run claude-code")
        self.assertEqual(tool["trace_id"], root["trace_id"])
        self.assertEqual(tool["parent_span_id"], root["span_id"])
        self.assertEqual(tool["attributes"]["telemetry.collection_layer"], "hook")
        self.assertEqual(root["attributes"]["telemetry.collection_layer"], "hook")
        self.assertEqual(tool["attributes"]["gen_ai.operation.name"], "execute_tool")
        self.assertEqual(tool["attributes"]["gen_ai.tool.name"], "Bash")
        self.assertEqual(tool["attributes"]["tool.call.id"], TOOL_USE_ID)
        self.assertEqual(tool["attributes"]["tool.arguments.api_key"], "[REDACTED]")
        self.assertEqual(tool["attributes"]["tool.arguments.command"], "ls -la")
        self.assertEqual(tool["attributes"]["session.id"], SESSION_ID)
        self.assertEqual(root["attributes"]["session.source"], "startup")
        self.assertEqual(root["attributes"]["session.end_reason"], "other")
        # Duration comes from the PreToolUse open-span record.
        self.assertLess(tool["start_time_unix_nano"], tool["end_time_unix_nano"])

        result_events = [event for event in tool["events"] if event["name"] == "tool.result"]
        self.assertEqual(len(result_events), 1)
        # Default-on redaction summarizes response content instead of storing it.
        self.assertTrue(result_events[0]["attributes"]["tool.result.content_omitted"])

        # Session state was cleaned up by SessionEnd.
        self.assertEqual(list((self.home / "state" / "sessions").glob("*.json")), [])

    def test_post_tool_use_failure_marks_error_status(self) -> None:
        payload = {
            **self._base_payload("PostToolUseFailure"),
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": "Error: permission denied",
            "tool_use_id": "toolu_02",
        }

        code, stdout = self.run_hook("PostToolUseFailure", payload)

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        spans = self.read_spool_spans()
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertEqual(span["name"], "execute_tool Bash")
        self.assertEqual(span["status"]["code"], "STATUS_CODE_ERROR")
        self.assertEqual(span["attributes"]["error.type"], "tool_error")
        self.assertEqual(span["attributes"]["telemetry.collection_layer"], "hook")

    def test_stop_spools_agent_turn_span(self) -> None:
        payload = {**self._base_payload("Stop"), "stop_hook_active": False}

        code, stdout = self.run_hook("Stop", payload)

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        spans = self.read_spool_spans()
        names = [span["name"] for span in spans]
        self.assertIn("agent.turn", names)
        turn = next(span for span in spans if span["name"] == "agent.turn")
        self.assertEqual(turn["attributes"]["telemetry.collection_layer"], "hook")
        self.assertEqual(turn["attributes"]["session.id"], SESSION_ID)


class HandlerRobustnessTests(ClaudeHookTestBase):
    def test_garbage_stdin_exits_zero_and_stays_silent(self) -> None:
        for event in self.hook.SUPPORTED_EVENTS:
            code, stdout = self.run_hook(event, "{{{ this is not json")
            self.assertEqual(code, 0, event)
            self.assertEqual(stdout, "", f"{event} hook wrote to stdout")

    def test_missing_or_unknown_event_is_silent_noop(self) -> None:
        for argv in ([], ["--event"], ["--event", "NotARealEvent"]):
            stdout = io.StringIO()
            original_stdin = sys.stdin
            sys.stdin = io.StringIO("{}")
            try:
                with contextlib.redirect_stdout(stdout):
                    code = self.hook.main(argv)
            finally:
                sys.stdin = original_stdin
            self.assertEqual(code, 0, argv)
            self.assertEqual(stdout.getvalue(), "", argv)
        self.assertEqual(self.read_spool_spans(), [])

    def test_disabled_config_is_instant_noop(self) -> None:
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"

        code, stdout = self.run_hook(
            "SessionStart", {"session_id": SESSION_ID, "source": "startup"}
        )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertFalse((self.home / "spool").exists())
        self.assertFalse((self.home / "state").exists())


class ClaudeInstallScriptTests(unittest.TestCase):
    USER_SETTINGS = {
        "model": "opus",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo user-hook"}],
                }
            ]
        },
    }

    def setUp(self) -> None:
        self.install = _load_module("claude_code_install_under_test", INSTALL_PY)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings_path = Path(self.tmp.name) / "claude" / "settings.json"
        self.settings_path.parent.mkdir(parents=True)
        self.settings_path.write_text(
            json.dumps(self.USER_SETTINGS, indent=2), encoding="utf-8"
        )

    def _run(self, *argv: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = self.install.main([*argv, "--settings-path", str(self.settings_path)])
        return code, stdout.getvalue()

    def _read_settings(self) -> dict[str, Any]:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def test_install_merges_preserving_user_hooks_and_backs_up(self) -> None:
        code, output = self._run("install", "--yes")

        self.assertEqual(code, 0)
        self.assertIn("added telemetry hook entry", output)
        settings = self._read_settings()
        self.assertEqual(settings["model"], "opus")
        expected_events = (
            "SessionStart",
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
            "SessionEnd",
        )
        for event in expected_events:
            self.assertIn(event, settings["hooks"], event)
        # User group untouched and first; ours appended after it.
        pre_groups = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre_groups), 2)
        self.assertEqual(pre_groups[0]["hooks"][0]["command"], "echo user-hook")
        our_command = pre_groups[1]["hooks"][0]["command"]
        self.assertIn("claude_code_hook.py", our_command)
        self.assertIn("--event PreToolUse", our_command)
        self.assertIn("PYTHONPATH=", our_command)
        self.assertEqual(pre_groups[1]["matcher"], "*")
        backup = self.settings_path.with_name(
            self.settings_path.name + ".bak-agent-telemetry"
        )
        self.assertEqual(
            json.loads(backup.read_text(encoding="utf-8")), self.USER_SETTINGS
        )

    def test_install_is_idempotent(self) -> None:
        self._run("install", "--yes")
        first = self._read_settings()

        code, output = self._run("install", "--yes")

        self.assertEqual(code, 0)
        self.assertIn("already installed", output)
        self.assertEqual(self._read_settings(), first)

    def test_status_reflects_install_state(self) -> None:
        code, before = self._run("status")
        self._run("install", "--yes")
        code_after, after = self._run("status")

        self.assertEqual((code, code_after), (0, 0))
        self.assertTrue(before.startswith("not-installed"))
        self.assertTrue(after.startswith("installed"))

    def test_uninstall_removes_only_our_entries(self) -> None:
        self._run("install", "--yes")

        code, output = self._run("uninstall", "--yes")

        self.assertEqual(code, 0)
        self.assertIn("uninstalled", output)
        settings = self._read_settings()
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(
            settings["hooks"],
            {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo user-hook"}],
                    }
                ]
            },
        )
        _, status = self._run("status")
        self.assertTrue(status.startswith("not-installed"))

    def test_uninstall_when_not_installed_is_noop(self) -> None:
        code, output = self._run("uninstall", "--yes")

        self.assertEqual(code, 0)
        self.assertIn("not installed", output)
        self.assertEqual(self._read_settings(), self.USER_SETTINGS)

    def test_install_creates_missing_settings_file(self) -> None:
        self.settings_path.unlink()

        code, _ = self._run("install", "--yes")

        self.assertEqual(code, 0)
        settings = self._read_settings()
        self.assertIn("SessionStart", settings["hooks"])
        backup = self.settings_path.with_name(
            self.settings_path.name + ".bak-agent-telemetry"
        )
        self.assertFalse(backup.exists())


if __name__ == "__main__":
    unittest.main()
