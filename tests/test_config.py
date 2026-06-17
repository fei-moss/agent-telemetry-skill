import json
import os
from pathlib import Path
import tempfile
import unittest

from agent_telemetry_skill.config import (
    TelemetryConfig,
    load_config,
    spool_dir,
    state_dir,
)


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


class ConfigResolutionTests(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ENV_VARS}
        for name in ENV_VARS:
            if name != "HOME":
                os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._tmp.name

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def _write_config_file(self, text: str) -> None:
        config_dir = Path(self._tmp.name) / ".agent-telemetry"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(text, encoding="utf-8")

    def test_defaults_apply_when_no_env_and_no_file(self):
        cfg = load_config()

        self.assertIsNone(cfg.endpoint)
        self.assertIsNone(cfg.token)
        self.assertEqual(cfg.service, "local-agent")
        self.assertEqual(cfg.tenant, "local-dev")
        self.assertEqual(cfg.environment, "local")
        # rich human-display capture is ON by default
        self.assertTrue(cfg.capture_content)
        self.assertTrue(cfg.capture_narrative)
        self.assertEqual(cfg.max_content_chars, 4000)
        self.assertIsNone(cfg.output)
        self.assertEqual(cfg.home, Path(self._tmp.name) / ".agent-telemetry")
        self.assertTrue(cfg.enabled)

    def test_config_file_values_beat_defaults(self):
        self._write_config_file(
            json.dumps(
                {
                    "endpoint": "http://file-endpoint:4318/v1/traces",
                    "token": "file-token",
                    "service": "file-service",
                    "tenant": "file-tenant",
                    "environment": "staging",
                    "capture_content": True,
                    "output": "/tmp/file-output.jsonl",
                    "enabled": False,
                }
            )
        )

        cfg = load_config()

        self.assertEqual(cfg.endpoint, "http://file-endpoint:4318/v1/traces")
        self.assertEqual(cfg.token, "file-token")
        self.assertEqual(cfg.service, "file-service")
        self.assertEqual(cfg.tenant, "file-tenant")
        self.assertEqual(cfg.environment, "staging")
        self.assertTrue(cfg.capture_content)
        self.assertEqual(cfg.output, "/tmp/file-output.jsonl")
        self.assertFalse(cfg.enabled)

    def test_env_beats_config_file(self):
        self._write_config_file(
            json.dumps(
                {
                    "service": "file-service",
                    "endpoint": "http://file-endpoint",
                    "enabled": False,
                    "capture_content": True,
                }
            )
        )
        os.environ["AGENT_TELEMETRY_SERVICE"] = "env-service"
        os.environ["AGENT_TELEMETRY_ENDPOINT"] = "http://env-endpoint"
        os.environ["AGENT_TELEMETRY_ENABLED"] = "1"
        os.environ["AGENT_TELEMETRY_CAPTURE_CONTENT"] = "0"

        cfg = load_config()

        self.assertEqual(cfg.service, "env-service")
        self.assertEqual(cfg.endpoint, "http://env-endpoint")
        self.assertTrue(cfg.enabled)
        self.assertFalse(cfg.capture_content)

    def test_unreadable_config_file_is_ignored(self):
        self._write_config_file("{not valid json")
        os.environ["AGENT_TELEMETRY_SERVICE"] = "env-service"

        cfg = load_config()

        self.assertEqual(cfg.service, "env-service")
        self.assertEqual(cfg.tenant, "local-dev")
        self.assertTrue(cfg.enabled)

    def test_enabled_env_zero_disables(self):
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"

        cfg = load_config()

        self.assertFalse(cfg.enabled)

    def test_capture_content_env_one_enables(self):
        os.environ["AGENT_TELEMETRY_CAPTURE_CONTENT"] = "1"

        cfg = load_config()

        self.assertTrue(cfg.capture_content)

    def test_home_override_and_dir_helpers(self):
        custom_home = Path(self._tmp.name) / "custom-state"
        os.environ["AGENT_TELEMETRY_HOME"] = str(custom_home)

        cfg = load_config()

        self.assertEqual(cfg.home, custom_home)
        self.assertEqual(spool_dir(cfg), custom_home / "spool")
        self.assertEqual(state_dir(cfg), custom_home / "state")

    def test_dir_helpers_work_on_plain_config(self):
        cfg = TelemetryConfig(home=Path("/tmp/telemetry-home"))

        self.assertEqual(spool_dir(cfg), Path("/tmp/telemetry-home/spool"))
        self.assertEqual(state_dir(cfg), Path("/tmp/telemetry-home/state"))


if __name__ == "__main__":
    unittest.main()
