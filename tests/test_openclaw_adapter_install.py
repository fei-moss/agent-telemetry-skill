from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "adapters" / "openclaw" / "install.py"
PLUGIN_TS = REPO_ROOT / "adapters" / "openclaw" / "telemetry-plugin.ts"


def load_install_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("openclaw_install", INSTALL_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpenClawInstallScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.install = load_install_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.openclaw_home = Path(self.tmp.name) / "openclaw-home"
        self.openclaw_home.mkdir()
        self._old_env = os.environ.get("OPENCLAW_HOME")
        os.environ["OPENCLAW_HOME"] = str(self.openclaw_home)
        self.addCleanup(self._restore_env)

    def _restore_env(self) -> None:
        if self._old_env is None:
            os.environ.pop("OPENCLAW_HOME", None)
        else:
            os.environ["OPENCLAW_HOME"] = self._old_env

    def test_detects_home_from_env(self) -> None:
        self.assertEqual(self.install.detect_openclaw_home(), self.openclaw_home)

    def test_install_writes_managed_files(self) -> None:
        exit_code = self.install.main(["install", "--yes"])

        self.assertEqual(exit_code, 0)
        target = self.openclaw_home / "extensions" / "agent-telemetry"
        for name in ("index.ts", "package.json", "openclaw.plugin.json"):
            self.assertTrue((target / name).is_file(), name)
        self.assertEqual(
            (target / "index.ts").read_text(encoding="utf-8"),
            PLUGIN_TS.read_text(encoding="utf-8"),
        )
        package = json.loads((target / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["type"], "module")
        self.assertEqual(package["openclaw"]["extensions"], ["./index.ts"])
        manifest = json.loads((target / "openclaw.plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["id"], "agent-telemetry")

    def test_install_is_idempotent(self) -> None:
        first = self.install.main(["install", "--yes"])
        second = self.install.main(["install", "--yes"])

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        target = self.openclaw_home / "extensions" / "agent-telemetry"
        self.assertTrue(self.install.is_installed(target))

    def test_uninstall_reverses_install(self) -> None:
        self.install.main(["install", "--yes"])

        exit_code = self.install.main(["uninstall", "--yes"])

        self.assertEqual(exit_code, 0)
        target = self.openclaw_home / "extensions" / "agent-telemetry"
        self.assertFalse(target.exists())

    def test_uninstall_without_install_is_noop(self) -> None:
        self.assertEqual(self.install.main(["uninstall", "--yes"]), 0)

    def test_status_runs_without_error(self) -> None:
        self.assertEqual(self.install.main(["status"]), 0)

    def test_install_without_openclaw_home_prints_manual_instructions(self) -> None:
        os.environ["OPENCLAW_HOME"] = str(Path(self.tmp.name) / "missing")
        original_home = Path.home
        Path.home = staticmethod(lambda: Path(self.tmp.name) / "no-such-user")  # type: ignore[method-assign]
        try:
            exit_code = self.install.main(["install", "--yes"])
        finally:
            Path.home = original_home  # type: ignore[method-assign]
        self.assertEqual(exit_code, 1)


class OpenClawPluginSourceContractTest(unittest.TestCase):
    """Sanity checks that the TS plugin keeps the semantic contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = PLUGIN_TS.read_text(encoding="utf-8")

    def test_collection_layer_is_plugin(self) -> None:
        self.assertIn('const COLLECTION_LAYER = "plugin";', self.source)
        self.assertIn('"telemetry.collection_layer": COLLECTION_LAYER', self.source)

    def test_span_naming_contract(self) -> None:
        self.assertIn("agent.run openclaw:", self.source)
        self.assertIn("execute_tool ${toolName}", self.source)
        self.assertIn("chat ${model}", self.source)

    def test_redaction_contract_present(self) -> None:
        self.assertIn("content_omitted: true", self.source)
        self.assertIn('"[REDACTED]"', self.source)
        self.assertIn("sk-(?:proj-)?[A-Za-z0-9_-]{8,}", self.source)
        self.assertIn("...[TRUNCATED]", self.source)

    def test_manual_wiring_exports_present(self) -> None:
        for export_name in ("onSessionStart", "onSessionEnd", "onToolStart", "onToolEnd"):
            self.assertIn(f"export const {export_name}", self.source)

    def test_config_contract_env_vars_present(self) -> None:
        for env_name in (
            "AGENT_TELEMETRY_ENDPOINT",
            "AGENT_TELEMETRY_TOKEN",
            "AGENT_TELEMETRY_SERVICE",
            "AGENT_TELEMETRY_TENANT",
            "AGENT_TELEMETRY_ENVIRONMENT",
            "AGENT_TELEMETRY_CAPTURE_CONTENT",
            "AGENT_TELEMETRY_OUTPUT",
            "AGENT_TELEMETRY_HOME",
            "AGENT_TELEMETRY_ENABLED",
        ):
            self.assertIn(env_name, self.source)


if __name__ == "__main__":
    unittest.main()
