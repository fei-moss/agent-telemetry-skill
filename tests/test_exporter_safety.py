"""Regression tests: local-only sinks never touch stdout, JSONL files are
private, and flush() respects its time budget."""

import contextlib
import io
import json
from pathlib import Path
import stat
import tempfile
import time
import unittest

from agent_telemetry_skill import (
    BackgroundExporter,
    JSONLFileExporter,
    Spool,
    SpoolExporter,
    TelemetryClient,
    TelemetryConfig,
)
from agent_telemetry_skill.schema import TelemetrySpan, new_span_id, new_trace_id


def _make_spans(count: int, prefix: str = "safe") -> list[TelemetrySpan]:
    return [
        TelemetrySpan(name=f"{prefix}-{index}", trace_id=new_trace_id(), span_id=new_span_id())
        for index in range(count)
    ]


class LocalOnlyModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_from_env_local_only_uses_private_jsonl_not_console(self):
        client = TelemetryClient.from_env(config=TelemetryConfig(home=self.home))

        self.assertIsInstance(client.exporter, BackgroundExporter)
        inner = client.exporter.inner
        self.assertIsInstance(inner, JSONLFileExporter)
        self.assertEqual(inner.path, self.home / "local-spans.jsonl")

    def test_local_only_run_writes_nothing_to_stdout(self):
        client = TelemetryClient.from_env(config=TelemetryConfig(home=self.home))
        captured = io.StringIO()

        with contextlib.redirect_stdout(captured):
            with client.run("quiet-run"):
                pass
            client.exporter.flush(timeout=5.0)

        self.assertEqual(captured.getvalue(), "")
        lines = (self.home / "local-spans.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["name"], "agent.run quiet-run")

    def test_bare_client_default_exporter_is_spool_not_console(self):
        client = TelemetryClient("svc", "tenant")
        self.assertIsInstance(client.exporter, SpoolExporter)


class FilePermissionTests(unittest.TestCase):
    def test_jsonl_output_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "trace.jsonl"
            JSONLFileExporter(path).export(_make_spans(1))

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_spool_pending_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Spool(Path(tmp) / "spool")
            spool.append(_make_spans(1))

            pending = next((Path(tmp) / "spool").glob("pending-*.jsonl"))
            self.assertEqual(stat.S_IMODE(pending.stat().st_mode), 0o600)


class FlushDeadlineTests(unittest.TestCase):
    def test_flush_stops_near_its_budget_with_a_slow_inner(self):
        class _Slow:
            timeout_seconds = 5.0

            def __init__(self):
                self.batches = 0

            def export(self, spans):
                self.batches += 1
                time.sleep(0.4)

        with tempfile.TemporaryDirectory() as tmp:
            spool = Spool(tmp)
            # Several separate pending files so an unbounded drain would
            # pay the slow export once per file.
            for index in range(5):
                path = Path(tmp) / f"pending-1-{index}.jsonl"
                payload = TelemetrySpan(
                    name=f"slow-{index}", trace_id=new_trace_id(), span_id=new_span_id()
                ).to_dict()
                path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            inner = _Slow()
            exporter = BackgroundExporter(inner, spool=spool, flush_interval=60.0)

            started = time.monotonic()
            exporter.flush(timeout=0.5)
            elapsed = time.monotonic() - started

            # Old behavior drained every file (~2s); the budget now caps the
            # work at roughly one bounded batch past the deadline.
            self.assertLess(elapsed, 1.5)
            self.assertLessEqual(inner.batches, 2)


if __name__ == "__main__":
    unittest.main()
