import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_telemetry_skill.exporters import InMemoryExporter
from agent_telemetry_skill.schema import TelemetrySpan, new_span_id, new_trace_id
from agent_telemetry_skill.spool import Spool


REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_spans(count: int, prefix: str = "span") -> list[TelemetrySpan]:
    spans = []
    for index in range(count):
        span = TelemetrySpan(
            name=f"{prefix}-{index}",
            trace_id=new_trace_id(),
            span_id=new_span_id(),
            attributes={"telemetry.collection_layer": "sdk", "index": index},
        )
        span.add_event("tool.result", {"ok": True})
        span.finish()
        spans.append(span)
    return spans


class _FailingExporter:
    def __init__(self):
        self.calls = 0

    def export(self, spans):
        self.calls += 1
        raise RuntimeError("network down")


class _FailAfterFirstBatchExporter:
    def __init__(self):
        self.batches = 0
        self.spans = []

    def export(self, spans):
        if self.batches >= 1:
            raise RuntimeError("boom")
        self.batches += 1
        self.spans.extend(spans)


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_drain_round_trip_preserves_span_fields(self):
        original = _make_spans(3)
        self.spool.append(original)
        self.assertEqual(self.spool.depth(), 3)

        exporter = InMemoryExporter()
        exported = self.spool.drain(exporter)

        self.assertEqual(exported, 3)
        self.assertEqual(self.spool.depth(), 0)
        self.assertEqual(len(exporter.spans), 3)
        by_id = {span.span_id: span for span in exporter.spans}
        for span in original:
            restored = by_id[span.span_id]
            self.assertEqual(restored.name, span.name)
            self.assertEqual(restored.trace_id, span.trace_id)
            self.assertEqual(restored.attributes["telemetry.collection_layer"], "sdk")
            self.assertEqual(restored.events[0].name, "tool.result")
            self.assertEqual(restored.end_time_unix_nano, span.end_time_unix_nano)

    def test_append_accepts_plain_dicts(self):
        self.spool.append([{"name": "dict-span", "trace_id": "a" * 32, "span_id": "b" * 16}])

        exporter = InMemoryExporter()
        exported = self.spool.drain(exporter)

        self.assertEqual(exported, 1)
        self.assertEqual(exporter.spans[0].name, "dict-span")

    def test_failed_export_preserves_all_spans(self):
        self.spool.append(_make_spans(4))
        failing = _FailingExporter()

        exported = self.spool.drain(failing)

        self.assertEqual(exported, 0)
        self.assertEqual(failing.calls, 1)
        self.assertEqual(self.spool.depth(), 4)
        self.assertEqual(list(Path(self._tmp.name).glob("*.draining")), [])

        recovered = InMemoryExporter()
        self.assertEqual(self.spool.drain(recovered), 4)
        self.assertEqual(self.spool.depth(), 0)

    def test_partial_batch_failure_preserves_remainder(self):
        self.spool.append(_make_spans(5))
        exporter = _FailAfterFirstBatchExporter()

        exported = self.spool.drain(exporter, batch_size=2)

        self.assertEqual(exported, 2)
        self.assertEqual(self.spool.depth(), 3)

        recovered = InMemoryExporter()
        self.assertEqual(self.spool.drain(recovered), 3)
        total_ids = {span.span_id for span in exporter.spans} | {
            span.span_id for span in recovered.spans
        }
        self.assertEqual(len(total_ids), 5)

    def test_max_batches_limits_work_and_preserves_remainder(self):
        self.spool.append(_make_spans(5))
        exporter = InMemoryExporter()

        exported = self.spool.drain(exporter, batch_size=2, max_batches=1)

        self.assertEqual(exported, 2)
        self.assertEqual(self.spool.depth(), 3)

    def test_append_never_raises_when_directory_is_unwritable(self):
        blocker = Path(self._tmp.name) / "not-a-dir"
        blocker.write_text("occupied", encoding="utf-8")
        broken = Spool(blocker / "spool")

        broken.append(_make_spans(1))  # must not raise

        self.assertEqual(broken.depth(), 0)

    def test_concurrent_short_lived_processes_can_append_safely(self):
        script = (
            "import sys\n"
            "from agent_telemetry_skill.schema import TelemetrySpan, new_span_id, new_trace_id\n"
            "from agent_telemetry_skill.spool import Spool\n"
            "spool = Spool(sys.argv[1])\n"
            "spool.append([\n"
            "    TelemetrySpan(name=f'sub-{i}', trace_id=new_trace_id(), span_id=new_span_id())\n"
            "    for i in range(5)\n"
            "])\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
        procs = [
            subprocess.Popen([sys.executable, "-c", script, self._tmp.name], env=env)
            for _ in range(3)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=30), 0)
        self.spool.append(_make_spans(2, prefix="main"))

        self.assertEqual(self.spool.depth(), 17)

        exporter = InMemoryExporter()
        self.assertEqual(self.spool.drain(exporter), 17)
        self.assertEqual(len({span.span_id for span in exporter.spans}), 17)

    def test_competing_drainers_never_duplicate_spans(self):
        self.spool.append(_make_spans(6))
        first = InMemoryExporter()
        second = InMemoryExporter()
        other_view = Spool(self._tmp.name)

        total = self.spool.drain(first) + other_view.drain(second)

        self.assertEqual(total, 6)
        ids = [span.span_id for span in first.spans + second.spans]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 6)


if __name__ == "__main__":
    unittest.main()
