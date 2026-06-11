"""Tailer glob discovery tests, incl. recursive ``**`` and exact-path patterns.

Regression for the production hardening where ``glob.glob`` lacked
``recursive=True`` and ``**`` patterns silently failed to recurse.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_telemetry_skill.watchers.tailer import Tailer


class TailerGlobTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        # nested layout: root/a/s1.jsonl, root/a/b/s2.jsonl, root/c/s3.jsonl
        for rel in ("a/s1.jsonl", "a/b/s2.jsonl", "c/s3.jsonl"):
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"x": 1}\n', encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _names(self, pattern):
        tailer = Tailer([pattern], state_dir=self.state)
        return sorted(p.name for p in tailer.discover())

    def test_recursive_double_star_matches_all_levels(self):
        self.assertEqual(
            self._names(str(self.root / "**" / "*.jsonl")),
            ["s1.jsonl", "s2.jsonl", "s3.jsonl"],
        )

    def test_single_level_glob_unaffected(self):
        self.assertEqual(self._names(str(self.root / "a" / "*.jsonl")), ["s1.jsonl"])

    def test_exact_path_matches(self):
        self.assertEqual(self._names(str(self.root / "c" / "s3.jsonl")), ["s3.jsonl"])

    def test_poll_reads_recursive_matches(self):
        tailer = Tailer([str(self.root / "**" / "*.jsonl")], state_dir=self.state)
        batch = tailer.poll_once()
        self.assertEqual(len(batch), 3)  # one line per file


if __name__ == "__main__":
    unittest.main()
