from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_PY = REPO_ROOT / "adapters" / "codex" / "install.py"
NOTIFY_HOOK_PY = REPO_ROOT / "adapters" / "codex" / "notify_hook.py"

EXISTING_CONFIG = (
    'model = "gpt-test"\n'
    "\n"
    '[projects."/tmp/example"]\n'
    'trust_level = "trusted"\n'
)


def load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CodexAdapterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.install = load_module("codex_install", INSTALL_PY)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.codex_home = Path(self.tmp.name) / "codex-home"
        self.codex_home.mkdir()
        self.config = self.codex_home / "config.toml"
        env = {
            "CODEX_HOME": str(self.codex_home),
            "AGENT_TELEMETRY_HOME": str(Path(self.tmp.name) / "telemetry-home"),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_install(self, argv: list[str]) -> tuple[int, str]:
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            code = self.install.main(argv)
        return code, stdout.getvalue()


class CodexInstallScriptTest(CodexAdapterTestBase):
    def test_detects_home_from_env(self) -> None:
        self.assertEqual(self.install.default_codex_home(), self.codex_home)

    def test_install_creates_config_with_managed_block(self) -> None:
        code, output = self.run_install(["install", "--yes"])

        self.assertEqual(code, 0)
        self.assertIn("watcher check:", output)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn(self.install.BLOCK_BEGIN, text)
        self.assertIn(self.install.BLOCK_END, text)
        self.assertIn("notify_hook.py", text)
        # No pre-existing file => no backup to create.
        self.assertFalse(self.config.with_name("config.toml.bak-agent-telemetry").exists())

    def test_install_preserves_existing_content_and_backs_up(self) -> None:
        self.config.write_text(EXISTING_CONFIG, encoding="utf-8")

        code, _ = self.run_install(["install", "--yes"])

        self.assertEqual(code, 0)
        text = self.config.read_text(encoding="utf-8")
        self.assertIn('model = "gpt-test"', text)
        self.assertIn('trust_level = "trusted"', text)
        # Managed block must come first so notify stays a top-level key.
        self.assertTrue(text.startswith(self.install.BLOCK_BEGIN))
        backup = self.config.with_name("config.toml.bak-agent-telemetry")
        self.assertEqual(backup.read_text(encoding="utf-8"), EXISTING_CONFIG)

    def test_install_result_is_valid_toml_with_top_level_notify(self) -> None:
        self.config.write_text(EXISTING_CONFIG, encoding="utf-8")
        self.run_install(["install", "--yes"])
        try:
            import tomllib
        except ModuleNotFoundError:
            self.skipTest("tomllib unavailable before Python 3.11")
        data = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertIn("notify", data)
        self.assertEqual(data["notify"][1], str(NOTIFY_HOOK_PY))
        self.assertEqual(data["model"], "gpt-test")

    def test_install_is_idempotent_and_keeps_first_backup(self) -> None:
        self.config.write_text(EXISTING_CONFIG, encoding="utf-8")

        first_code, _ = self.run_install(["install", "--yes"])
        after_first = self.config.read_text(encoding="utf-8")
        second_code, second_output = self.run_install(["install", "--yes"])

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertIn("already installed", second_output)
        self.assertEqual(self.config.read_text(encoding="utf-8"), after_first)
        backup = self.config.with_name("config.toml.bak-agent-telemetry")
        self.assertEqual(backup.read_text(encoding="utf-8"), EXISTING_CONFIG)

    def test_install_skips_foreign_notify(self) -> None:
        foreign = 'notify = ["/usr/bin/true"]\n' + EXISTING_CONFIG
        self.config.write_text(foreign, encoding="utf-8")

        code, output = self.run_install(["install", "--yes"])

        self.assertEqual(code, 0)
        self.assertIn("SKIPPED", output)
        self.assertEqual(self.config.read_text(encoding="utf-8"), foreign)

    def test_notify_inside_table_is_not_foreign(self) -> None:
        text = '[other]\nnotify = ["/usr/bin/true"]\n'
        self.assertIsNone(self.install.find_foreign_notify(text))
        self.assertEqual(
            self.install.find_foreign_notify('notify = ["x"]\n[other]\n'),
            'notify = ["x"]',
        )

    def test_uninstall_reverses_install(self) -> None:
        self.config.write_text(EXISTING_CONFIG, encoding="utf-8")
        self.run_install(["install", "--yes"])

        code, _ = self.run_install(["uninstall", "--yes"])

        self.assertEqual(code, 0)
        text = self.config.read_text(encoding="utf-8")
        self.assertNotIn(self.install.BLOCK_BEGIN, text)
        self.assertNotIn("notify", text)
        self.assertIn('model = "gpt-test"', text)
        self.assertIn('trust_level = "trusted"', text)

    def test_uninstall_without_install_is_noop(self) -> None:
        code, output = self.run_install(["uninstall", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn("not installed", output)

    def test_status_reports_not_installed_then_installed(self) -> None:
        code, output = self.run_install(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[0], "not-installed")

        self.run_install(["install", "--yes"])

        code, output = self.run_install(["status"])
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[0], "installed")

    def test_missing_codex_home_is_graceful(self) -> None:
        os.environ["CODEX_HOME"] = str(Path(self.tmp.name) / "missing")
        for argv in (["status"], ["install", "--yes"], ["uninstall", "--yes"]):
            code, output = self.run_install(argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("not", output.lower())


class CodexNotifyHookTest(CodexAdapterTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.hook = load_module("codex_notify_hook", NOTIFY_HOOK_PY)

    def test_main_spawns_detached_watcher_and_returns_zero(self) -> None:
        payload = '{"type": "agent-turn-complete", "turn-id": "t1"}'
        with mock.patch.object(self.hook.subprocess, "Popen") as popen:
            code = self.hook.main([payload])

        self.assertEqual(code, 0)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertIn("--once", args[0])
        self.assertIn("--runtime", args[0])
        self.assertIn("codex", args[0])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["stdout"], self.hook.subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], self.hook.subprocess.DEVNULL)

    def test_disabled_env_is_silent_noop(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_TELEMETRY_ENABLED": "0"}):
            with mock.patch.object(self.hook.subprocess, "Popen") as popen:
                code = self.hook.main(['{"type": "agent-turn-complete"}'])
        self.assertEqual(code, 0)
        popen.assert_not_called()

    def test_other_notification_types_do_not_trigger(self) -> None:
        with mock.patch.object(self.hook.subprocess, "Popen") as popen:
            code = self.hook.main(['{"type": "something-else"}'])
        self.assertEqual(code, 0)
        popen.assert_not_called()

    def test_unparsable_payload_still_triggers(self) -> None:
        self.assertTrue(self.hook.should_trigger(["not json"]))
        self.assertTrue(self.hook.should_trigger([]))

    def test_main_never_raises(self) -> None:
        with mock.patch.object(
            self.hook, "spawn_watcher", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(self.hook.main(['{"type": "agent-turn-complete"}']), 0)


if __name__ == "__main__":
    unittest.main()
