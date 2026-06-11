from agent_telemetry_skill import ConsoleExporter, TelemetryClient


def main() -> None:
    client = TelemetryClient(
        service_name="generic-agent",
        tenant_id="tenant-dev",
        exporter=ConsoleExporter(),
    )

    with client.run("answer-user", user_id="user-dev", agent_name="generic"):
        client.decision("use_calculator", rationale="The request is arithmetic", confidence=0.91)

        with client.tool_call("calculator", {"expression": "21 * 2"}) as span:
            result = 42
            span.add_event("tool.result", {"result": result})

        with client.retrieval(source="local-notes", query="arithmetic policy"):
            client.record_event("retrieval.output", {"document_count": 1})

        with client.llm_call(
            provider="openai",
            model="gpt-5-mini",
            prompt={"messages": [{"role": "user", "content": "What is 21 * 2?"}]},
        ) as span:
            span.set_result({"finish_reason": "stop"})


if __name__ == "__main__":
    main()
