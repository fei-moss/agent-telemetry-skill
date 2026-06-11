import os
from pathlib import Path
import tempfile
import time
import unittest

from agent_telemetry_skill import (
    BackgroundExporter,
    InMemoryExporter,
    JSONLFileExporter,
    NoopExporter,
    OTLPHTTPExporter,
    Spool,
    SpoolExporter,
    TelemetryClient,
    TelemetryConfig,
)
from agent_telemetry_skill.schema import TelemetrySpan, new_span_id, new_trace_id


def _make_spans(count: int, prefix: str = "bg") -> list[TelemetrySpan]:
    return [
        TelemetrySpan(name=f"{prefix}-{index}", trace_id=new_trace_id(), span_id=new_span_id())
        for index in range(count)
    ]


class _SlowExporter:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        self.spans = []

    def export(self, spans):
        time.sleep(self.delay_seconds)
        self.spans.extend(spans)


class BackgroundExporterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_thread_is_lazy_and_daemon(self):
        exporter = BackgroundExporter(InMemoryExporter(), spool=self.spool)
        self.assertIsNone(exporter._thread)

        exporter.export(_make_spans(1))

        self.assertIsNotNone(exporter._thread)
        self.assertTrue(exporter._thread.daemon)

    def test_flush_delivers_spooled_spans_to_inner(self):
        inner = InMemoryExporter()
        exporter = BackgroundExporter(inner, spool=self.spool, flush_interval=60.0)
        self.spool.append(_make_spans(3))

        flushed = exporter.flush(timeout=5.0)

        self.assertEqual(flushed, 3)
        self.assertEqual(len(inner.spans), 3)
        self.assertEqual(self.spool.depth(), 0)

    def test_export_does_not_block_on_slow_inner(self):
        inner = _SlowExporter(delay_seconds=0.3)
        exporter = BackgroundExporter(inner, spool=self.spool, flush_interval=0.05)

        started = time.monotonic()
        exporter.export(_make_spans(3))
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and len(inner.spans) < 3:
            time.sleep(0.02)
        self.assertEqual(len(inner.spans), 3)

    def test_export_never_raises_even_with_broken_spool(self):
        blocker = Path(self._tmp.name) / "file"
        blocker.write_text("occupied", encoding="utf-8")
        exporter = BackgroundExporter(InMemoryExporter(), spool=Spool(blocker / "x"))

        exporter.export(_make_spans(1))  # must not raise

    def test_failed_inner_export_keeps_spans_spooled(self):
        class _Failing:
            def export(self, spans):
                raise RuntimeError("offline")

        exporter = BackgroundExporter(_Failing(), spool=self.spool, flush_interval=60.0)
        self.spool.append(_make_spans(2))

        flushed = exporter.flush(timeout=1.0)

        self.assertEqual(flushed, 0)
        self.assertEqual(self.spool.depth(), 2)


class FromEnvWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self, **overrides) -> TelemetryConfig:
        values = {"home": self.home, **overrides}
        return TelemetryConfig(**values)

    def test_disabled_config_yields_noop_exporter_for_every_mode(self):
        for mode in ("background", "spool", "direct"):
            client = TelemetryClient.from_env(
                exporter_mode=mode,
                config=self._config(enabled=False, endpoint="http://example.invalid"),
            )
            self.assertIsInstance(client.exporter, NoopExporter)

    def test_disabled_env_var_yields_noop_exporter(self):
        saved = os.environ.get("AGENT_TELEMETRY_ENABLED")
        os.environ["AGENT_TELEMETRY_ENABLED"] = "0"
        try:
            client = TelemetryClient.from_env()
        finally:
            if saved is None:
                os.environ.pop("AGENT_TELEMETRY_ENABLED", None)
            else:
                os.environ["AGENT_TELEMETRY_ENABLED"] = saved
        self.assertIsInstance(client.exporter, NoopExporter)

    def test_spool_mode_uses_spool_exporter_under_home(self):
        client = TelemetryClient.from_env(exporter_mode="spool", config=self._config())

        self.assertIsInstance(client.exporter, SpoolExporter)
        self.assertEqual(client.exporter.spool.directory, self.home / "spool")

    def test_direct_mode_with_output_uses_jsonl_file_exporter(self):
        output = str(self.home / "trace-output.jsonl")
        client = TelemetryClient.from_env(
            exporter_mode="direct",
            config=self._config(output=output),
        )

        self.assertIsInstance(client.exporter, JSONLFileExporter)
        self.assertEqual(client.exporter.path, Path(output))

    def test_background_mode_with_endpoint_wraps_otlp_exporter(self):
        client = TelemetryClient.from_env(
            config=self._config(
                endpoint="http://collector.invalid/v1/traces",
                token="ingest-token",
                service="svc-a",
            ),
        )

        self.assertIsInstance(client.exporter, BackgroundExporter)
        inner = client.exporter.inner
        self.assertIsInstance(inner, OTLPHTTPExporter)
        self.assertEqual(inner.endpoint, "http://collector.invalid/v1/traces")
        self.assertEqual(inner.headers["Authorization"], "Bearer ingest-token")
        self.assertEqual(inner.service_name, "svc-a")
        self.assertEqual(client.service_name, "svc-a")

    def test_from_env_spool_mode_records_spans_with_collection_layer(self):
        client = TelemetryClient.from_env(
            collection_layer="hook",
            exporter_mode="spool",
            config=self._config(),
        )

        with client.run("hooked-run"):
            with client.tool_call("grep", {"query": "needle"}):
                pass

        spool = client.exporter.spool
        self.assertEqual(spool.depth(), 2)
        collected = InMemoryExporter()
        spool.drain(collected)
        for span in collected.spans:
            self.assertEqual(span.attributes["telemetry.collection_layer"], "hook")


class CollectionLayerTests(unittest.TestCase):
    def test_collection_layer_present_on_root_and_child_spans(self):
        exporter = InMemoryExporter()
        client = TelemetryClient(
            "local-agent",
            "tenant-1",
            exporter=exporter,
            collection_layer="log_watch",
        )

        with client.run("layered"):
            with client.tool_call("search", {"query": "hello"}):
                pass
            with client.llm_call(provider="anthropic", model="claude-sonnet-4-5"):
                pass

        self.assertEqual(len(exporter.spans), 3)
        for span in exporter.spans:
            self.assertEqual(span.attributes["telemetry.collection_layer"], "log_watch")

    def test_collection_layer_defaults_to_sdk(self):
        exporter = InMemoryExporter()
        client = TelemetryClient("local-agent", "tenant-1", exporter=exporter)

        with client.run("default-layer"):
            pass

        self.assertEqual(exporter.spans[0].attributes["telemetry.collection_layer"], "sdk")


if __name__ == "__main__":
    unittest.main()
