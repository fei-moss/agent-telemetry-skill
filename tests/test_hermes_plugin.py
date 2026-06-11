from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

from agent_telemetry_skill.exporters import InMemoryExporter
from agent_telemetry_skill.schema import TelemetrySpan

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_INIT = REPO_ROOT / "adapters" / "hermes" / "agent_telemetry" / "__init__.py"
INSTALL_PY = REPO_ROOT / "adapters" / "hermes" / "install.py"

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
    "HERMES_HOME",
    "HOME",
)


def load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # required for dataclass resolution during exec
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class RaisingExporter:
    def export(self, spans: list[TelemetrySpan]) -> None:
        raise RuntimeError("export backend down")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks: dict[str, object] = {}

    def register_hook(self, name: str, callback: object) -> None:
        self.hooks[name] = callback


class HermesPluginTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        for name in ENV_VARS:
            if name != "HOME":
                os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["HOME"] = self.tmp.name
        self.addCleanup(self._restore_env)
        self.module = load_module(PLUGIN_INIT, "hermes_agent_telemetry_plugin")

    def _restore_env(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def run_session(self, plugin, session_id: str = "s1") -> None:
        plugin.pre_llm_call(
            session_id=session_id,
            model="test-model",
            platform="telegram",
            sender_id="user-1",
            user_message="hello",
            conversation_history=[{"role": "user", "content": "hello"}],
            is_first_turn=True,
        )
        plugin.pre_tool_call(
            session_id=session_id,
            tool_call_id="tc-1",
            tool_name="search",
            args={"query": "find docs", "api_key": "sk-aaaabbbbcccc"},
        )
        plugin.post_tool_call(
            session_id=session_id,
            tool_call_id="tc-1",
            tool_name="search",
            args={"query": "find docs", "api_key": "sk-aaaabbbbcccc"},
            result="3 hits",
        )
        plugin.post_api_request(
            session_id=session_id,
            model="test-model",
            provider="anthropic",
            api_duration=0.25,
            usage={"input_tokens": 12, "output_tokens": 7},
        )
        plugin.on_session_end(session_id=session_id, completed=True, model="test-model")


class HermesPluginSpanTests(HermesPluginTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.exporter = InMemoryExporter()
        self.plugin = self.module.HermesTelemetryPlugin(exporter=self.exporter)

    def test_session_exports_spans_with_same_trace(self) -> None:
        self.run_session(self.plugin)

        spans = self.exporter.spans
        self.assertGreaterEqual(len(spans), 3)
        trace_ids = {span.trace_id for span in spans}
        self.assertEqual(len(trace_ids), 1)
        names = [span.name for span in spans]
        self.assertIn("agent.run hermes:s1", names)
        self.assertIn("execute_tool search", names)
        self.assertIn("chat test-model", names)

    def test_every_span_carries_plugin_collection_layer(self) -> None:
        self.run_session(self.plugin)

        for span in self.exporter.spans:
            self.assertEqual(span.attributes.get("telemetry.collection_layer"), "plugin")

    def test_child_spans_parent_to_root(self) -> None:
        self.run_session(self.plugin)

        root = next(s for s in self.exporter.spans if s.name == "agent.run hermes:s1")
        children = [s for s in self.exporter.spans if s is not root]
        self.assertIsNone(root.parent_span_id)
        for child in children:
            self.assertEqual(child.parent_span_id, root.span_id)

    def test_user_message_content_omitted_by_default(self) -> None:
        self.run_session(self.plugin)

        root = next(s for s in self.exporter.spans if s.name == "agent.run hermes:s1")
        message = root.attributes.get("user.message")
        self.assertIsInstance(message, dict)
        self.assertTrue(message["content_omitted"])
        self.assertNotIn("hello", json.dumps(root.attributes))

    def test_tool_arguments_are_redacted(self) -> None:
        self.run_session(self.plugin)

        tool = next(s for s in self.exporter.spans if s.name == "execute_tool search")
        self.assertEqual(tool.attributes.get("tool.arguments.api_key"), "[REDACTED]")
        query = tool.attributes.get("tool.arguments.query")
        self.assertIsInstance(query, dict)
        self.assertTrue(query["content_omitted"])
        self.assertNotIn("find docs", json.dumps(tool.attributes))

    def test_distinct_sessions_use_distinct_traces(self) -> None:
        self.run_session(self.plugin, session_id="s1")
        self.run_session(self.plugin, session_id="s2")

        trace_ids = {span.trace_id for span in self.exporter.spans}
        self.assertEqual(len(trace_ids), 2)

    def test_token_usage_attributes_recorded(self) -> None:
        self.run_session(self.plugin)

        chat = next(s for s in self.exporter.spans if s.name == "chat test-model")
        self.assertEqual(chat.attributes.get("gen_ai.usage.input_tokens"), 12)
        self.assertEqual(chat.attributes.get("gen_ai.usage.output_tokens"), 7)

    def test_hooks_swallow_garbage_input(self) -> None:
        self.plugin.pre_llm_call()
        self.plugin.pre_tool_call()
        self.plugin.post_tool_call()
        self.plugin.post_api_request(api_duration="not-a-number")
        self.plugin.on_session_end()


class HermesPluginRobustnessTests(HermesPluginTestBase):
    def test_exporter_exception_does_not_propagate_and_falls_back_to_spool(self) -> None:
        plugin = self.module.HermesTelemetryPlugin(exporter=RaisingExporter())

        self.run_session(plugin)  # must not raise

        self.assertGreater(plugin._spool.depth(), 0)

    def test_register_wires_all_hooks_when_enabled(self) -> None:
        plugin = self.module.HermesTelemetryPlugin(exporter=InMemoryExporter())
        ctx = FakeCtx()

        plugin.register(ctx)

        self.assertEqual(
            sorted(ctx.hooks),
            [
                "on_session_end",
                "post_api_request",
                "post_tool_call",
                "pre_llm_call",
                "pre_tool_call",
            ],
        )

    def test_disabled_register_is_noop(self) -> None:
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"
        plugin = self.module.HermesTelemetryPlugin()
        ctx = FakeCtx()

        plugin.register(ctx)
        self.run_session(plugin)

        self.assertEqual(ctx.hooks, {})
        self.assertIsInstance(plugin.exporter, self.module.NoopExporter)
        self.assertEqual(plugin._spool.depth(), 0)

    def test_module_register_never_raises(self) -> None:
        class BrokenCtx:
            def register_hook(self, name: str, callback: object) -> None:
                raise RuntimeError("hooks unavailable")

        self.module._PLUGIN = None
        self.module.register(BrokenCtx())


class HermesPluginExporterConfigTests(HermesPluginTestBase):
    def test_otlp_exporter_carries_bearer_token_header(self) -> None:
        os.environ["AGENT_TELEMETRY_ENDPOINT"] = "http://127.0.0.1:4318/v1/traces"
        os.environ["AGENT_TELEMETRY_TOKEN"] = "test-token-123"

        plugin = self.module.HermesTelemetryPlugin()

        self.assertIsInstance(plugin.exporter, self.module.BackgroundExporter)
        inner = plugin.exporter.inner
        self.assertIsInstance(inner, self.module.OTLPHTTPExporter)
        self.assertEqual(inner.headers.get("Authorization"), "Bearer test-token-123")

    def test_no_auth_header_without_token(self) -> None:
        os.environ["AGENT_TELEMETRY_ENDPOINT"] = "http://127.0.0.1:4318/v1/traces"

        plugin = self.module.HermesTelemetryPlugin()

        self.assertNotIn("Authorization", plugin.exporter.inner.headers)

    def test_local_only_mode_uses_spool_exporter(self) -> None:
        plugin = self.module.HermesTelemetryPlugin()

        self.assertIsInstance(plugin.exporter, self.module.SpoolExporter)

    def test_output_env_writes_jsonl_with_plugin_layer(self) -> None:
        output = Path(self.tmp.name) / "hermes-telemetry.jsonl"
        os.environ["AGENT_TELEMETRY_OUTPUT"] = str(output)
        plugin = self.module.HermesTelemetryPlugin()

        self.run_session(plugin)
        plugin.exporter.flush(5.0)

        lines = [
            json.loads(line)
            for line in output.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertGreaterEqual(len(lines), 3)
        for payload in lines:
            self.assertEqual(payload["attributes"]["telemetry.collection_layer"], "plugin")


class HermesInstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.install = load_module(INSTALL_PY, "hermes_install")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hermes_home = Path(self.tmp.name) / "hermes-home"
        self.hermes_home.mkdir()
        self._old_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.hermes_home)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._old_env is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_env

    def target(self) -> Path:
        return self.hermes_home / "plugins" / "agent-telemetry"

    def test_install_copies_plugin_and_vendors_skill_package(self) -> None:
        exit_code = self.install.main(["install", "--yes"])

        self.assertEqual(exit_code, 0)
        self.assertTrue((self.target() / "__init__.py").is_file())
        self.assertTrue((self.target() / "plugin.yaml").is_file())
        self.assertTrue((self.target() / "agent_telemetry_skill" / "__init__.py").is_file())
        self.assertTrue((self.target() / "agent_telemetry_skill" / "spool.py").is_file())

    def test_status_reports_installed_state(self) -> None:
        self.assertEqual(self.install.main(["status"]), 0)
        self.install.main(["install", "--yes"])
        self.assertEqual(self.install.main(["status"]), 0)
        self.assertTrue(self.install.is_installed(self.target()))

    def test_install_is_idempotent(self) -> None:
        self.assertEqual(self.install.main(["install", "--yes"]), 0)
        self.assertEqual(self.install.main(["install", "--yes"]), 0)
        self.assertTrue(self.install.is_installed(self.target()))

    def test_uninstall_reverses_install(self) -> None:
        self.install.main(["install", "--yes"])

        exit_code = self.install.main(["uninstall", "--yes"])

        self.assertEqual(exit_code, 0)
        self.assertFalse(self.target().exists())

    def test_uninstall_without_install_is_noop(self) -> None:
        self.assertEqual(self.install.main(["uninstall", "--yes"]), 0)

    def test_missing_hermes_home_prints_guidance_and_exits_zero(self) -> None:
        os.environ["HERMES_HOME"] = str(Path(self.tmp.name) / "missing")
        original_home = Path.home
        Path.home = staticmethod(lambda: Path(self.tmp.name) / "no-such-user")  # type: ignore[method-assign]
        try:
            self.assertEqual(self.install.main(["install", "--yes"]), 0)
            self.assertEqual(self.install.main(["status"]), 0)
            self.assertEqual(self.install.main(["uninstall", "--yes"]), 0)
        finally:
            Path.home = original_home  # type: ignore[method-assign]

    def test_installed_plugin_imports_vendored_package(self) -> None:
        self.install.main(["install", "--yes"])

        module = load_module(self.target() / "__init__.py", "hermes_installed_plugin")

        self.assertTrue(hasattr(module, "HermesTelemetryPlugin"))


if __name__ == "__main__":
    unittest.main()
