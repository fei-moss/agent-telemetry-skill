import json
import tempfile
import unittest

from agent_telemetry_skill import (
    ConsoleExporter,
    InMemoryExporter,
    JSONLFileExporter,
    OTLPHTTPExporter,
    RedactionConfig,
    TelemetryClient,
)


class TelemetryClientTests(unittest.TestCase):
    def test_nested_run_tool_llm_and_decision_are_exported_as_spans(self):
        exporter = InMemoryExporter()
        client = TelemetryClient(
            service_name="local-agent",
            tenant_id="tenant-1",
            exporter=exporter,
        )

        with client.run("answer-user", user_id="user-1", agent_name="openclaw"):
            client.decision(
                "use-search",
                rationale="Need current market data",
                confidence=0.82,
            )
            with client.tool_call("search_web", {"query": "agent telemetry api"}):
                client.record_event("tool.output", {"result_count": 3})
            with client.retrieval(source="docs", query="telemetry sdk"):
                client.record_event("retrieval.output", {"document_count": 2})
            with client.llm_call(
                provider="openai",
                model="gpt-5-mini",
                prompt={"messages": [{"role": "user", "content": "hello"}]},
            ) as span:
                span.set_result({"finish_reason": "stop", "output": "hi"})

        spans = exporter.spans
        self.assertEqual(len(spans), 4)
        root, tool, retrieval, llm = spans
        self.assertEqual(root.name, "agent.run answer-user")
        self.assertEqual(tool.name, "execute_tool search_web")
        self.assertEqual(retrieval.name, "retrieve docs")
        self.assertEqual(llm.name, "chat gpt-5-mini")
        self.assertEqual(tool.parent_span_id, root.span_id)
        self.assertEqual(retrieval.parent_span_id, root.span_id)
        self.assertEqual(llm.parent_span_id, root.span_id)
        self.assertEqual(tool.trace_id, root.trace_id)
        self.assertEqual(retrieval.trace_id, root.trace_id)
        self.assertEqual(llm.trace_id, root.trace_id)
        self.assertEqual(root.attributes["gen_ai.agent.name"], "openclaw")
        self.assertEqual(tool.attributes["gen_ai.tool.name"], "search_web")
        self.assertEqual(tool.attributes["tool.arguments.query"]["char_count"], len("agent telemetry api"))
        self.assertEqual(retrieval.attributes["gen_ai.data_source.id"], "docs")
        self.assertEqual(retrieval.attributes["retrieval.query"]["char_count"], len("telemetry sdk"))
        self.assertEqual(llm.attributes["gen_ai.request.model"], "gpt-5-mini")
        self.assertEqual(root.events[0].name, "agent.decision")

    def test_sensitive_values_are_redacted_and_content_is_metadata_only_by_default(self):
        exporter = InMemoryExporter()
        client = TelemetryClient(
            service_name="local-agent",
            tenant_id="tenant-1",
            exporter=exporter,
        )

        with client.run("privacy-check"):
            with client.tool_call(
                "deploy",
                {
                    "api_key": "sk-proj-abc123",
                    "nested": {"authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"},
                    "plain": "keep me",
                },
            ):
                pass
            with client.llm_call(
                provider="anthropic",
                model="claude-sonnet-4-5",
                prompt={"messages": [{"role": "user", "content": "secret plan"}]},
            ):
                pass

        payload = json.dumps([span.to_dict() for span in exporter.spans], sort_keys=True)
        self.assertNotIn("sk-proj-abc123", payload)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9.payload.sig", payload)
        self.assertNotIn("secret plan", payload)
        self.assertIn("[REDACTED]", payload)
        self.assertIn("prompt.messages", payload)
        self.assertIn("content_omitted", payload)

    def test_non_secret_token_usage_and_session_ids_are_not_redacted(self):
        exporter = InMemoryExporter()
        client = TelemetryClient(
            service_name="local-agent",
            tenant_id="tenant-1",
            exporter=exporter,
        )

        with client.run(
            "usage",
            metadata={
                "input_tokens": 123,
                "output_tokens": 45,
                "session_id": "session-visible",
                "access_token": "secret-access-token",
            },
        ):
            pass

        payload = json.dumps([span.to_dict() for span in exporter.spans], sort_keys=True)
        self.assertIn("123", payload)
        self.assertIn("45", payload)
        self.assertIn("session-visible", payload)
        self.assertNotIn("secret-access-token", payload)

    def test_user_message_content_is_omitted_by_default(self):
        exporter = InMemoryExporter()
        client = TelemetryClient(
            service_name="local-agent",
            tenant_id="tenant-1",
            exporter=exporter,
        )

        with client.run(
            "message-privacy",
            metadata={
                "user": {"message": "private user question"},
                "dotted.message": "another private question",
            },
        ):
            pass

        payload = json.dumps([span.to_dict() for span in exporter.spans], sort_keys=True)
        self.assertNotIn("private user question", payload)
        self.assertNotIn("another private question", payload)
        self.assertIn("content_omitted", payload)

    def test_content_capture_can_be_enabled_explicitly_while_still_redacting_secrets(self):
        exporter = InMemoryExporter()
        client = TelemetryClient(
            service_name="local-agent",
            tenant_id="tenant-1",
            exporter=exporter,
            redaction=RedactionConfig(capture_content=True),
        )

        with client.run("debug-run"):
            client.record_event(
                "observation",
                {
                    "text": "public detail",
                    "password": "super-secret-password",
                },
            )

        payload = json.dumps([span.to_dict() for span in exporter.spans], sort_keys=True)
        self.assertIn("public detail", payload)
        self.assertNotIn("super-secret-password", payload)

    def test_export_failures_are_buffered_for_later_flush(self):
        class FailingExporter:
            def __init__(self):
                self.calls = 0

            def export(self, spans):
                self.calls += 1
                raise RuntimeError("network down")

        failing = FailingExporter()
        client = TelemetryClient(
            service_name="local-agent",
            tenant_id="tenant-1",
            exporter=failing,
        )

        with client.run("offline"):
            pass

        self.assertEqual(failing.calls, 1)
        self.assertEqual(len(client.pending_spans), 1)

        recovered = InMemoryExporter()
        client.exporter = recovered
        flushed = client.flush_pending()

        self.assertEqual(flushed, 1)
        self.assertEqual(len(client.pending_spans), 0)
        self.assertEqual(len(recovered.spans), 1)

    def test_otlp_exporter_builds_collector_compatible_json_payload(self):
        exporter = InMemoryExporter()
        client = TelemetryClient(
            service_name="local-agent",
            tenant_id="tenant-1",
            exporter=exporter,
        )
        with client.run("otlp"):
            client.record_event("agent.decision", {"selected_tool": "none"})

        payload = OTLPHTTPExporter.build_payload(
            exporter.spans,
            service_name="local-agent",
        )

        resource_span = payload["resourceSpans"][0]
        attrs = resource_span["resource"]["attributes"]
        self.assertIn({"key": "service.name", "value": {"stringValue": "local-agent"}}, attrs)
        span = resource_span["scopeSpans"][0]["spans"][0]
        self.assertEqual(span["traceId"], exporter.spans[0].trace_id)
        self.assertEqual(span["name"], "agent.run otlp")
        self.assertEqual(span["events"][0]["name"], "agent.decision")

    def test_console_exporter_writes_json_lines(self):
        exporter = InMemoryExporter()
        client = TelemetryClient("local-agent", "tenant-1", exporter=exporter)
        with client.run("console"):
            pass

        console = ConsoleExporter()
        lines = console.dumps(exporter.spans).strip().splitlines()

        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["name"], "agent.run console")

    def test_jsonl_file_exporter_appends_spans(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/trace.jsonl"
            exporter = JSONLFileExporter(path)
            client = TelemetryClient("local-agent", "tenant-1", exporter=exporter)

            with client.run("file-export"):
                pass

            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()

        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["name"], "agent.run file-export")


if __name__ == "__main__":
    unittest.main()
