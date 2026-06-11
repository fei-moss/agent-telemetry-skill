"""Tests for scripts/setup.py (the universal installer).

Every test runs against a temp HOME, a stub adapters directory, and a
monkeypatched ``subprocess.run`` that records calls instead of spawning
real installer processes.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP_PY = REPO_ROOT / "scripts" / "setup.py"

ENV_VARS = (
    "HOME",
    "CLAUDECODE",
    "CODEX_HOME",
    "HERMES_HOME",
    "OPENCLAW_HOME",
    "PYTHONPATH",
    "AGENT_TELEMETRY_ENDPOINT",
    "AGENT_TELEMETRY_TOKEN",
    "AGENT_TELEMETRY_SERVICE",
    "AGENT_TELEMETRY_TENANT",
    "AGENT_TELEMETRY_ENVIRONMENT",
    "AGENT_TELEMETRY_CAPTURE_CONTENT",
    "AGENT_TELEMETRY_OUTPUT",
    "AGENT_TELEMETRY_HOME",
    "AGENT_TELEMETRY_ENABLED",
)


def load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses resolves cls.__module__ via sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SetupTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        for name in ENV_VARS:
            if name != "HOME":
                os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore_env)

        self.setup = load_module("setup_script_under_test", SETUP_PY)
        self.adapters_dir = Path(self.tmp.name) / "adapters"
        for dir_name in self.setup.ADAPTER_DIR_NAMES.values():
            adapter = self.adapters_dir / dir_name
            adapter.mkdir(parents=True)
            (adapter / "install.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        self.setup.ADAPTERS_DIR = self.adapters_dir

        self.calls: list[list[str]] = []
        self.adapter_returncode = 0
        patcher = mock.patch.object(
            self.setup.subprocess, "run", side_effect=self._fake_run
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_env(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _fake_run(self, cmd, **kwargs):
        self.calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(
            cmd, returncode=self.adapter_returncode, stdout="stub-output\n", stderr=""
        )

    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.setup.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def install_calls(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if "install.py" in " ".join(cmd)]

    def config_path(self) -> Path:
        return self.home / ".agent-telemetry" / "config.json"


class DetectionTest(SetupTestBase):
    def test_nothing_detected_in_empty_home(self) -> None:
        detections = self.setup.detect_runtimes()

        self.assertEqual(set(detections), set(self.setup.RUNTIMES))
        self.assertFalse(any(detections.values()))

    def test_detects_runtime_home_directories(self) -> None:
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        (self.home / ".hermes").mkdir()

        detections = self.setup.detect_runtimes()

        self.assertTrue(detections["claude-code"])
        self.assertTrue(detections["codex"])
        self.assertTrue(detections["hermes"])
        self.assertFalse(detections["openclaw"])

    def test_detects_claude_code_via_env(self) -> None:
        os.environ["CLAUDECODE"] = "1"

        self.assertTrue(self.setup.detect_claude_code())

    def test_detects_openclaw_via_xdg_config_dir(self) -> None:
        (self.home / ".config" / "openclaw").mkdir(parents=True)

        self.assertTrue(self.setup.detect_openclaw())

    def test_detects_codex_via_codex_home_env(self) -> None:
        custom = Path(self.tmp.name) / "custom-codex"
        custom.mkdir()
        os.environ["CODEX_HOME"] = str(custom)

        self.assertTrue(self.setup.detect_codex())


class InstallDispatchTest(SetupTestBase):
    def test_auto_installs_only_detected_runtimes(self) -> None:
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()

        code, output, _ = self.run_main(["--auto"])

        self.assertEqual(code, 0)
        commands = [" ".join(cmd) for cmd in self.install_calls()]
        self.assertEqual(len(commands), 2)
        self.assertIn("claude_code/install.py install --yes", commands[0])
        self.assertIn("codex/install.py install --yes", commands[1])
        self.assertNotIn("openclaw", " ".join(commands))
        self.assertNotIn("hermes", " ".join(commands))
        self.assertIn("summary:", output)

    def test_runtime_flag_forces_undetected_runtime(self) -> None:
        code, output, _ = self.run_main(["--runtime", "hermes"])

        self.assertEqual(code, 0)
        commands = [" ".join(cmd) for cmd in self.install_calls()]
        self.assertEqual(len(commands), 1)
        self.assertIn("hermes/install.py install --yes", commands[0])
        self.assertIn("no", output)  # detected column

    def test_uninstall_dispatches_uninstall_action(self) -> None:
        code, output, _ = self.run_main(["--runtime", "codex", "--uninstall"])

        self.assertEqual(code, 0)
        commands = [" ".join(cmd) for cmd in self.install_calls()]
        self.assertEqual(len(commands), 1)
        self.assertIn("codex/install.py uninstall --yes", commands[0])
        self.assertIn("uninstall", output)
        # No post-install info on uninstall.
        self.assertNotIn("spool dir:", output)

    def test_repeated_runtime_flag_deduplicates(self) -> None:
        code, _, _ = self.run_main(["--runtime", "codex", "--runtime", "codex"])

        self.assertEqual(code, 0)
        self.assertEqual(len(self.install_calls()), 1)

    def test_adapter_failure_still_exits_zero(self) -> None:
        self.adapter_returncode = 3

        code, output, _ = self.run_main(["--runtime", "openclaw"])

        self.assertEqual(code, 0)
        self.assertIn("failed (exit 3)", output)

    def test_missing_installer_reported_without_subprocess(self) -> None:
        (self.adapters_dir / "hermes" / "install.py").unlink()

        code, output, _ = self.run_main(["--runtime", "hermes"])

        self.assertEqual(code, 0)
        self.assertEqual(self.install_calls(), [])
        self.assertIn("installer missing", output)

    def test_invalid_runtime_is_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.setup.main(["--runtime", "not-a-runtime"])

        self.assertNotEqual(ctx.exception.code, 0)

    def test_interactive_decline_runs_nothing(self) -> None:
        (self.home / ".claude").mkdir()

        with mock.patch("builtins.input", side_effect=EOFError):
            code, output, _ = self.run_main([])

        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [])
        self.assertIn("detected runtimes: claude-code", output)

    def test_interactive_accept_installs_detected(self) -> None:
        (self.home / ".codex").mkdir()

        with mock.patch("builtins.input", return_value="y"):
            code, _, _ = self.run_main([])

        self.assertEqual(code, 0)
        commands = [" ".join(cmd) for cmd in self.install_calls()]
        self.assertEqual(len(commands), 1)
        self.assertIn("codex/install.py install --yes", commands[0])


class ConfigWriteTest(SetupTestBase):
    def test_writes_config_with_600_permissions(self) -> None:
        code, output, _ = self.run_main(
            ["--endpoint", "http://collector:4318/v1/traces", "--token", "secret-token-value"]
        )

        self.assertEqual(code, 0)
        path = self.config_path()
        self.assertTrue(path.is_file())
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["endpoint"], "http://collector:4318/v1/traces")
        self.assertEqual(data["token"], "secret-token-value")
        # The raw token must never be printed.
        self.assertNotIn("secret-token-value", output)
        self.assertIn("secr***", output)

    def test_merges_with_existing_config(self) -> None:
        path = self.config_path()
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"tenant": "keep-me", "service": "old-service"}), encoding="utf-8"
        )

        code, _, _ = self.run_main(["--service", "new-service", "--endpoint", "http://x"])

        self.assertEqual(code, 0)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["tenant"], "keep-me")
        self.assertEqual(data["service"], "new-service")
        self.assertEqual(data["endpoint"], "http://x")

    def test_print_env_echoes_exports_without_writing(self) -> None:
        code, output, _ = self.run_main(
            ["--print-env", "--endpoint", "http://collector:4318/v1/traces", "--token", "tok"]
        )

        self.assertEqual(code, 0)
        self.assertFalse(self.config_path().exists())
        self.assertIn(
            "export AGENT_TELEMETRY_ENDPOINT=http://collector:4318/v1/traces", output
        )
        self.assertIn("export AGENT_TELEMETRY_TOKEN=tok", output)


class PostInstallTest(SetupTestBase):
    def test_connectivity_ok_when_spool_empty(self) -> None:
        code, output, _ = self.run_main(
            ["--runtime", "codex", "--endpoint", "http://collector:4318/v1/traces"]
        )

        self.assertEqual(code, 0)
        self.assertIn("connectivity self-test: ok", output)
        emit_calls = [cmd for cmd in self.calls if "emit-event" in cmd]
        self.assertEqual(len(emit_calls), 1)
        self.assertIn("setup.connectivity-test", emit_calls[0])

    def test_connectivity_failed_spooled_when_spool_nonempty(self) -> None:
        spool = self.home / ".agent-telemetry" / "spool"
        spool.mkdir(parents=True)
        (spool / "pending-1-0.jsonl").write_text('{"name": "stub-span"}\n', encoding="utf-8")

        code, output, _ = self.run_main(
            ["--runtime", "codex", "--endpoint", "http://collector:4318/v1/traces"]
        )

        self.assertEqual(code, 0)
        self.assertIn("failed-spooled (1 span(s) remain in spool)", output)

    def test_local_only_install_prints_spool_state_and_watcher_hint(self) -> None:
        code, output, _ = self.run_main(["--runtime", "codex"])

        self.assertEqual(code, 0)
        self.assertIn("spool dir:", output)
        self.assertIn("state dir:", output)
        self.assertIn("local-only mode", output)
        self.assertIn("watch_sessions.py --runtime codex", output)
        # No endpoint => no connectivity subprocess call.
        self.assertEqual([cmd for cmd in self.calls if "emit-event" in cmd], [])


class StatusTest(SetupTestBase):
    def test_status_masks_token_and_shows_spool_depth(self) -> None:
        path = self.config_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"token": "super-secret-token"}), encoding="utf-8")
        (self.home / ".claude").mkdir()

        code, output, _ = self.run_main(["--status"])

        self.assertEqual(code, 0)
        self.assertNotIn("super-secret-token", output)
        self.assertIn('"supe***"', output)
        self.assertIn('"spool_depth": 0', output)
        self.assertIn("claude-code: detected=yes", output)
        self.assertIn("codex: detected=no", output)
        status_calls = [" ".join(cmd) for cmd in self.calls]
        self.assertEqual(len(status_calls), 1)
        self.assertIn("claude_code/install.py status", status_calls[0])

    def test_status_runs_no_installs(self) -> None:
        code, _, _ = self.run_main(["--status"])

        self.assertEqual(code, 0)
        self.assertEqual(self.install_calls(), [])


class SummaryRenderingTest(SetupTestBase):
    def test_render_summary_aligns_columns(self) -> None:
        rows = [
            self.setup.SummaryRow("claude-code", True, "install", "ok"),
            self.setup.SummaryRow("codex", False, "install", "failed (exit 1)"),
        ]

        rendered = self.setup.render_summary(rows)

        lines = rendered.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertRegex(lines[0], r"runtime\s+detected\s+action\s+result")
        self.assertRegex(lines[1], r"^-+\s+-+\s+-+\s+-+$")
        self.assertRegex(lines[2], r"claude-code\s+yes\s+install\s+ok")
        self.assertRegex(lines[3], r"codex\s+no\s+install\s+failed \(exit 1\)")


if __name__ == "__main__":
    unittest.main()
