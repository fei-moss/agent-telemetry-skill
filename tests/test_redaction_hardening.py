"""Regression tests: content gating by ancestor key and secret-pattern scope.

Covers the findings that tool results/file contents leaked under non-content
key names (stdout, stderr, ...) and that AWS/GitHub/Slack/Google/PEM secrets
were not scrubbed by the value-level patterns.
"""

import unittest

from agent_telemetry_skill.redaction import RedactionConfig, Redactor


class ContentGatingByAncestorTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor()

    def test_tool_result_stdout_is_omitted_by_default(self):
        flattened = self.redactor.flatten(
            {"stdout": "-----file body-----", "stderr": "warning: x", "exit_code": 0},
            "tool.result",
        )

        self.assertEqual(flattened["tool.result.stdout"]["content_omitted"], True)
        self.assertEqual(flattened["tool.result.stderr"]["content_omitted"], True)
        self.assertEqual(flattened["tool.result.exit_code"], 0)
        self.assertNotIn("file body", str(flattened))

    def test_strings_nested_under_prompt_are_omitted(self):
        flattened = self.redactor.flatten(
            {"messages": [{"role": "user", "content": "secret plan"}]}, "prompt"
        )

        self.assertNotIn("secret plan", str(flattened))

    def test_tool_argument_scalars_still_pass_through(self):
        # tool.arguments is metadata (not content); values upload after
        # scrubbing + truncation. Pinned by existing hook/watcher tests too.
        flattened = self.redactor.flatten({"command": "ls -la"}, "tool.arguments")

        self.assertEqual(flattened["tool.arguments.command"], "ls -la")

    def test_capture_content_enables_tool_result_passthrough(self):
        redactor = Redactor(RedactionConfig(capture_content=True))

        flattened = redactor.flatten({"stdout": "visible output"}, "tool.result")

        self.assertEqual(flattened["tool.result.stdout"], "visible output")


class SecretPatternCoverageTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor()

    def _scrubbed(self, value: str) -> str:
        return str(self.redactor.flatten({"note": value}, "tool.arguments"))

    def test_aws_access_key_id_is_scrubbed(self):
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", self._scrubbed("key AKIAIOSFODNN7EXAMPLE end"))

    def test_github_pat_is_scrubbed(self):
        token = "ghp_" + "a1B2" * 10
        self.assertNotIn(token, self._scrubbed(f"auth with {token}"))

    def test_github_fine_grained_pat_is_scrubbed(self):
        token = "github_pat_" + "x9" * 20
        self.assertNotIn(token, self._scrubbed(token))

    def test_slack_token_is_scrubbed(self):
        token = "xoxb-1234567890-abcdefghijkl"
        self.assertNotIn(token, self._scrubbed(f"slack {token}"))

    def test_google_api_key_is_scrubbed(self):
        token = "AIza" + "Sy" * 18
        self.assertNotIn(token, self._scrubbed(token))

    def test_pem_private_key_block_is_scrubbed(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA1234567890\n"
            "-----END RSA PRIVATE KEY-----"
        )
        scrubbed = self._scrubbed(f"cert: {pem}")
        self.assertNotIn("MIIEowIBAAKCAQEA1234567890", scrubbed)

    def test_command_line_embedded_aws_key_is_scrubbed(self):
        scrubbed = self._scrubbed("aws s3 ls --key AKIAIOSFODNN7EXAMPLE")
        self.assertIn("[REDACTED]", scrubbed)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", scrubbed)


if __name__ == "__main__":
    unittest.main()
