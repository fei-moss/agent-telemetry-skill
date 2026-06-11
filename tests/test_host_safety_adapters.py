"""Regression tests for host-safety in the Hermes plugin and Claude hook:
bounded session buffering, import-failure no-op, and bounded stdin reads."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest

from agent_telemetry_skill.exporters import InMemoryExporter

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_PLUGIN_PATH = REPO_ROOT / "adapters" / "hermes" / "agent_telemetry" / "__init__.py"
HOOK_PATH = REPO_ROOT / "scripts" / "hooks" / "claude_code_hook.py"

ENV_VARS = ("AGENT_TELEMETRY_HOME", "AGENT_TELEMETRY_ENABLED", "AGENT_TELEMETRY_ENDPOINT")


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class _FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}

    def register_hook(self, name: str, callback: object) -> None:
        self.hooks[name] = callback


class HermesHostSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["AGENT_TELEMETRY_HOME"] = str(Path(self._tmp.name) / "telemetry-home")
        os.environ.pop("AGENT_TELEMETRY_ENABLED", None)
        os.environ.pop("AGENT_TELEMETRY_ENDPOINT", None)
        self.module = _load_module(HERMES_PLUGIN_PATH, "hermes_host_safety_under_test")

    def tearDown(self) -> None:
        sys.modules.pop("hermes_host_safety_under_test", None)
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def test_runs_buffer_is_capped_and_evicted_runs_are_exported(self):
        saved_cap = self.module.MAX_BUFFERED_RUNS
        self.module.MAX_BUFFERED_RUNS = 2
        try:
            exporter = InMemoryExporter()
            plugin = self.module.HermesTelemetryPlugin(exporter=exporter)
            for index in range(4):
                plugin.pre_llm_call(session_id=f"s{index}", user_message="hi")

            self.assertLessEqual(len(plugin._runs), 2)
            evicted_roots = [
                span for span in exporter.spans if span.name == "agent.run hermes:s0"
            ]
            self.assertEqual(len(evicted_roots), 1)
            self.assertIsNotNone(evicted_roots[0].end_time_unix_nano)
        finally:
            self.module.MAX_BUFFERED_RUNS = saved_cap

    def test_register_is_noop_when_vendored_import_failed(self):
        saved_flag = self.module._IMPORT_OK
        self.module._IMPORT_OK = False
        try:
            ctx = _FakeCtx()
            self.module.register(ctx)  # must not raise
            self.assertEqual(ctx.hooks, {})
        finally:
            self.module._IMPORT_OK = saved_flag


class HookStdinBoundedReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = _load_module(HOOK_PATH, "claude_hook_stdin_under_test")
        self._saved_timeout = self.module._STDIN_TIMEOUT_SECONDS
        self.module._STDIN_TIMEOUT_SECONDS = 0.2
        self._saved_stdin = sys.stdin

    def tearDown(self) -> None:
        sys.stdin = self._saved_stdin
        self.module._STDIN_TIMEOUT_SECONDS = self._saved_timeout
        sys.modules.pop("claude_hook_stdin_under_test", None)

    def test_open_but_idle_pipe_times_out_with_empty_payload(self):
        read_fd, write_fd = os.pipe()
        try:
            sys.stdin = os.fdopen(read_fd, "r")
            started = time.monotonic()
            payload = self.module._read_stdin_payload()
            elapsed = time.monotonic() - started
        finally:
            os.close(write_fd)
            sys.stdin.close()

        self.assertEqual(payload, {})
        self.assertLess(elapsed, 1.5)  # never burns the host's 10s hook budget

    def test_payload_written_then_closed_is_parsed(self):
        read_fd, write_fd = os.pipe()
        sys.stdin = os.fdopen(read_fd, "r")
        try:
            os.write(write_fd, json.dumps({"session_id": "abc"}).encode("utf-8"))
            os.close(write_fd)
            payload = self.module._read_stdin_payload()
        finally:
            sys.stdin.close()

        self.assertEqual(payload, {"session_id": "abc"})


if __name__ == "__main__":
    unittest.main()
