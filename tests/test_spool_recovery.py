"""Regression tests for spool crash-recovery, poisoned records, and size cap."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from agent_telemetry_skill.exporters import InMemoryExporter
from agent_telemetry_skill.schema import TelemetrySpan, new_span_id, new_trace_id
from agent_telemetry_skill.spool import STALE_CLAIM_SECONDS, Spool


def _make_spans(count: int, prefix: str = "span") -> list[TelemetrySpan]:
    spans = []
    for index in range(count):
        span = TelemetrySpan(
            name=f"{prefix}-{index}",
            trace_id=new_trace_id(),
            span_id=new_span_id(),
            attributes={"telemetry.collection_layer": "sdk"},
        )
        span.finish()
        spans.append(span)
    return spans


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


class StaleDrainingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_draining_file_is_reclaimed_and_exported(self):
        self.spool.append(_make_spans(3))
        pending = next(Path(self._tmp.name).glob("pending-*.jsonl"))
        orphan = pending.with_name(pending.name + ".draining")
        os.rename(pending, orphan)  # simulate a drainer killed mid-drain
        _age(orphan, STALE_CLAIM_SECONDS + 5)

        exporter = InMemoryExporter()
        exported = self.spool.drain(exporter)

        self.assertEqual(exported, 3)
        self.assertEqual(list(Path(self._tmp.name).glob("*.draining")), [])
        self.assertEqual(self.spool.depth(), 0)

    def test_fresh_draining_file_is_left_for_its_live_owner(self):
        self.spool.append(_make_spans(2))
        pending = next(Path(self._tmp.name).glob("pending-*.jsonl"))
        claimed = pending.with_name(pending.name + ".draining")
        os.rename(pending, claimed)  # a live drainer owns this right now

        exported = self.spool.drain(InMemoryExporter())

        self.assertEqual(exported, 0)
        self.assertTrue(claimed.exists())

    def test_depth_counts_orphaned_draining_files(self):
        self.spool.append(_make_spans(2))
        pending = next(Path(self._tmp.name).glob("pending-*.jsonl"))
        os.rename(pending, pending.with_name(pending.name + ".draining"))

        self.assertEqual(self.spool.depth(), 2)


class PoisonedRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_drain_skips_poisoned_lines_and_keeps_healthy_spans(self):
        good = _make_spans(2, prefix="healthy")
        lines = [
            json.dumps(good[0].to_dict(), sort_keys=True),
            json.dumps({"name": "bad", "attributes": "not-a-dict"}),  # from_dict raises
            json.dumps(["not", "a", "span"]),
            "{truncated json",
            json.dumps(good[1].to_dict(), sort_keys=True),
        ]
        path = Path(self._tmp.name) / "pending-1-0.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        exporter = InMemoryExporter()
        exported = self.spool.drain(exporter)  # must not raise

        names = {span.name for span in exporter.spans}
        self.assertEqual(names, {"healthy-0", "healthy-1"})
        self.assertEqual(exported, 2)
        self.assertEqual(list(Path(self._tmp.name).glob("*.draining")), [])

    def test_non_numeric_timestamps_are_coerced_not_fatal(self):
        record = {
            "name": "weird-times",
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "start_time_unix_nano": "abc",
            "end_time_unix_nano": "def",
        }
        path = Path(self._tmp.name) / "pending-1-0.jsonl"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        exporter = InMemoryExporter()
        exported = self.spool.drain(exporter)

        self.assertEqual(exported, 1)
        self.assertEqual(exporter.spans[0].name, "weird-times")
        self.assertIsInstance(exporter.spans[0].start_time_unix_nano, int)


class SizeCapTests(unittest.TestCase):
    def test_oldest_pending_files_are_dropped_when_over_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Spool(tmp, max_bytes=512)
            old_file = Path(tmp) / "pending-1-0.jsonl"
            old_file.write_text("x" * 4096 + "\n", encoding="utf-8")
            _age(old_file, 120)

            spool.append(_make_spans(1))

            self.assertFalse(old_file.exists())  # oldest evicted
            self.assertEqual(len(list(Path(tmp).glob("pending-*.jsonl"))), 1)

    def test_append_under_cap_keeps_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Spool(tmp)  # default 256MB cap
            spool.append(_make_spans(3))
            self.assertEqual(spool.depth(), 3)


if __name__ == "__main__":
    unittest.main()
