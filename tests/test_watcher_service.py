"""Tests for the persistent watcher-service renderers (pure, no side effects).

Only the unit text/plist generation is tested here — install()/uninstall()
drive launchctl/systemctl and are platform side-effecting, so they are not
exercised in unit tests.
"""
import plistlib
import unittest

from agent_telemetry_skill import watcher_service


class LaunchdPlistTests(unittest.TestCase):
    def _render(self):
        return watcher_service.render_launchd_plist(
            "codex",
            python_exe="/usr/bin/python3",
            watch_script="/opt/skill/scripts/watch_sessions.py",
            pythonpath="/opt/skill",
            log_path="/home/u/.agent-telemetry/watcher.log",
            interval=5,
        )

    def test_plist_is_valid_and_resident(self):
        data = plistlib.loads(self._render())
        self.assertEqual(data["Label"], "com.agent-telemetry.codex-watcher")
        # RunAtLoad + KeepAlive => starts at login and restarts on exit
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(data["KeepAlive"])

    def test_program_args_run_the_watcher_with_pythonpath(self):
        data = plistlib.loads(self._render())
        args = data["ProgramArguments"]
        self.assertEqual(args[0], "/usr/bin/python3")
        self.assertIn("watch_sessions.py", args[1])
        self.assertIn("--runtime", args)
        self.assertIn("codex", args)
        self.assertEqual(data["EnvironmentVariables"]["PYTHONPATH"], "/opt/skill")

    def test_no_secret_baked_into_plist(self):
        # config (endpoint/token) is read at runtime from config.json, not baked
        raw = self._render().decode("utf-8")
        self.assertNotIn("token", raw.lower())
        self.assertNotIn("endpoint", raw.lower())


class SystemdUnitTests(unittest.TestCase):
    def _render(self):
        return watcher_service.render_systemd_unit(
            "codex",
            python_exe="/usr/bin/python3",
            watch_script="/opt/skill/scripts/watch_sessions.py",
            pythonpath="/opt/skill",
            interval=5,
        )

    def test_unit_restarts_and_runs_watcher(self):
        unit = self._render()
        self.assertIn("Restart=always", unit)
        self.assertIn("--runtime codex", unit)
        self.assertIn("Environment=PYTHONPATH=/opt/skill", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_no_secret_baked_into_unit(self):
        unit = self._render().lower()
        self.assertNotIn("token", unit)
        self.assertNotIn("endpoint", unit)


class LabelTests(unittest.TestCase):
    def test_label_is_runtime_scoped(self):
        self.assertEqual(
            watcher_service.service_label("hermes"), "com.agent-telemetry.hermes-watcher"
        )


if __name__ == "__main__":
    unittest.main()
