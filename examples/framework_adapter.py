from agent_telemetry_skill import ConsoleExporter, TelemetryClient
from agent_telemetry_skill.adapters import trace_agent_run, trace_tool


client = TelemetryClient(
    service_name="hermas-local",
    tenant_id="tenant-dev",
    exporter=ConsoleExporter(),
)


@trace_tool(client, "knowledge_search")
def search_knowledge_base(query: str) -> list[str]:
    return ["result-a", "result-b"]


@trace_agent_run(client, "support-agent", agent_name="hermas")
def support_agent(question: str) -> str:
    client.decision("search_first", rationale="The question needs private context", confidence=0.84)
    results = search_knowledge_base(question)
    with client.retrieval(source="knowledge_base", query=question):
        client.record_event("retrieval.output", {"document_count": len(results)})
    with client.llm_call(
        provider="openai",
        model="gpt-5-mini",
        prompt={"messages": [{"role": "user", "content": question}]},
        metadata={"retrieved_count": len(results)},
    ):
        return "answer"


if __name__ == "__main__":
    support_agent("How do I connect telemetry?")
