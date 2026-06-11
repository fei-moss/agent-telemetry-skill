"""Regression tests for inline command-line credential redaction.

These cover the e2e finding where a real Codex `exec_command` span carried
`sshpass -p '<password>' ssh ...` in tool.arguments.cmd and the password was
uploaded verbatim, because it matched no known token shape and `cmd` is neither
a sensitive nor a content key.
"""

from __future__ import annotations

import unittest

from agent_telemetry_skill.redaction import Redactor


SECRETS = ("Fake_TestPw_001", "SuperSecret123", "hunter2", "p4ssw0rd", "abc123def456")


def _has_secret(value: object) -> bool:
    text = str(value)
    return any(secret in text for secret in SECRETS)


class CredentialRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.redactor = Redactor()

    def _redact_cmd(self, command: str) -> str:
        return str(self.redactor.redact(command, ("tool", "arguments", "cmd")))

    def test_sshpass_password_is_redacted(self) -> None:
        out = self._redact_cmd("sshpass -p 'Fake_TestPw_001' ssh root@host whoami")
        self.assertFalse(_has_secret(out))
        self.assertIn("[REDACTED]", out)
        self.assertIn("ssh root@host", out)  # rest of command stays legible

    def test_attached_short_password_flag(self) -> None:
        out = self._redact_cmd("mysql -uroot -pSuperSecret123 -e 'show tables'")
        self.assertFalse(_has_secret(out))

    def test_basic_auth_flag(self) -> None:
        out = self._redact_cmd("curl -u admin:hunter2 https://api.example.com")
        self.assertFalse(_has_secret(out))

    def test_url_userinfo(self) -> None:
        out = self._redact_cmd("psql postgres://user:p4ssw0rd@db.host:5432/mydb")
        self.assertFalse(_has_secret(out))
        self.assertIn("db.host", out)

    def test_long_credential_flags(self) -> None:
        out = self._redact_cmd("cmd --token abc123def456 --api-key abc123def456")
        self.assertFalse(_has_secret(out))

    def test_benign_commands_untouched(self) -> None:
        for command in ("ls -la /tmp", "ssh -p 22 root@host", "grep -p foo file.txt"):
            self.assertEqual(self._redact_cmd(command), command)

    def test_redaction_applies_inside_flattened_attributes(self) -> None:
        flattened = self.redactor.flatten(
            {"cmd": "sshpass -p 'Fake_TestPw_001' ssh root@host"}, "tool.arguments"
        )
        self.assertFalse(_has_secret(flattened["tool.arguments.cmd"]))


if __name__ == "__main__":
    unittest.main()
