"""agent-telemetry CLI.

Designed to be invoked by hooks or by the model itself: every command exits
0 even when telemetry fails (warnings go to stderr), honors enabled=False as
a no-op, and never hangs longer than ~5 seconds on the network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

from . import emit, session_trace
from .client import TelemetryClient
from .config import TelemetryConfig, load_config, local_spans_path, spool_dir, state_dir
from .exporters import (
    ConsoleExporter,
    Exporter,
    JSONLFileExporter,
    OTLPHTTPExporter,
)
from .redaction import RedactionConfig
from .schema import STATUS_ERROR, STATUS_OK
from .spool import Spool


DEFAULT_LAYER = "model_reported"
OPPORTUNISTIC_DRAIN_BUDGET_SECONDS = 3.0
DRAIN_TIMEOUT_SECONDS = 5.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-telemetry")
    parser.add_argument("--service-name", default=None)
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--capture-content", action="store_true")
    parser.add_argument("--layer", default=DEFAULT_LAYER)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="emit a demo trace")
    demo.add_argument("--otlp-endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))

    emit_event = sub.add_parser(
        "emit-event", help="read JSON attributes from stdin and emit one event"
    )
    emit_event.add_argument("event_name")
    target = emit_event.add_mutually_exclusive_group()
    target.add_argument("--session-id", default=None)
    target.add_argument("--trace-id", default=None)
    emit_event.add_argument("--parent-span-id", default=None)
    emit_event.add_argument("--run-name", default="manual-event")
    emit_event.add_argument(
        "--otlp-endpoint", default=os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    )

    decision = sub.add_parser("decision", help="emit an agent.decision event")
    decision.add_argument("decision_name")
    decision.add_argument("--rationale", default=None)
    decision.add_argument("--confidence", type=float, default=None)
    decision.add_argument("--session-id", default=None)

    session = sub.add_parser("session", help="manage cross-process session traces")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    start = session_sub.add_parser("start", help="register a session trace")
    start.add_argument("--session-id", required=True)
    start.add_argument("--agent-name", default=None)
    end = session_sub.add_parser("end", help="close a session and emit its root span")
    end.add_argument("--session-id", required=True)
    end.add_argument("--status", choices=("ok", "error"), default="ok")

    drain = sub.add_parser("drain", help="ship spooled spans to the configured sink")
    drain.add_argument("--batch-size", type=int, default=100)
    drain.add_argument("--max-batches", type=int, default=None)

    sub.add_parser("status", help="print resolved config, spool depth, session count")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except Exception as exc:  # telemetry must never fail the caller
        print(f"agent-telemetry: warning: {exc}", file=sys.stderr)
        return 0


def _dispatch(args: argparse.Namespace) -> int:
    _apply_global_overrides(args)
    config = load_config()
    if args.command == "status":
        return _cmd_status(config)
    if not config.enabled:
        print("agent-telemetry: disabled via AGENT_TELEMETRY_ENABLED; no-op", file=sys.stderr)
        return 0
    if args.command == "demo":
        return _cmd_demo(args, config)
    if args.command == "emit-event":
        return _cmd_emit_event(args, config)
    if args.command == "decision":
        return _cmd_decision(args, config)
    if args.command == "session":
        return _cmd_session(args, config)
    if args.command == "drain":
        return _cmd_drain(args, config)
    return 0


def _cmd_demo(args: argparse.Namespace, config: TelemetryConfig) -> int:
    exporter: Exporter = (
        OTLPHTTPExporter(endpoint=args.otlp_endpoint, service_name=config.service)
        if args.otlp_endpoint
        else ConsoleExporter()
    )
    client = _build_client(args, config, exporter)
    with client.run("demo", user_id="demo-user", agent_name="generic-agent"):
        client.decision("call_search", rationale="Need external context", confidence=0.7)
        with client.tool_call("search", {"query": "agent telemetry", "api_key": "sk-proj-demo-secret"}):
            client.record_event("tool.output", {"result_count": 2})
        with client.llm_call(
            provider="openai",
            model="gpt-5-mini",
            prompt={"messages": [{"role": "user", "content": "hello"}]},
        ):
            pass
    return 0


def _cmd_emit_event(args: argparse.Namespace, config: TelemetryConfig) -> int:
    attributes = _read_stdin_attributes()
    if args.session_id or args.trace_id:
        emitted = emit.emit_event(
            args.event_name,
            attributes,
            session_id=args.session_id,
            trace_id=args.trace_id,
            parent_span_id=args.parent_span_id,
            collection_layer=args.layer,
        )
        if not emitted:
            print("agent-telemetry: warning: event was not emitted", file=sys.stderr)
        _opportunistic_drain(config)
        return 0
    return _emit_legacy_run(args, config, attributes)


def _cmd_decision(args: argparse.Namespace, config: TelemetryConfig) -> int:
    attributes: dict[str, Any] = {"decision.name": args.decision_name}
    if args.rationale is not None:
        attributes["decision.rationale"] = args.rationale
    if args.confidence is not None:
        attributes["decision.confidence"] = args.confidence
    emitted = emit.emit_event(
        "agent.decision",
        attributes,
        session_id=args.session_id,
        collection_layer=args.layer,
    )
    if not emitted:
        print("agent-telemetry: warning: decision was not emitted", file=sys.stderr)
    _opportunistic_drain(config)
    return 0


def _cmd_session(args: argparse.Namespace, config: TelemetryConfig) -> int:
    if args.session_command == "start":
        record = session_trace.begin(args.session_id, agent_name=args.agent_name)
        print(
            json.dumps(
                {
                    "session_id": record.session_id,
                    "trace_id": record.trace_id,
                    "root_span_id": record.root_span_id,
                },
                ensure_ascii=False,
            )
        )
        return 0
    status_code = STATUS_ERROR if args.status == "error" else STATUS_OK
    root = session_trace.end(
        args.session_id,
        status_code=status_code,
        attributes={"telemetry.collection_layer": args.layer},
    )
    if root is None:
        print(
            f"agent-telemetry: warning: session {args.session_id!r} unknown or already ended",
            file=sys.stderr,
        )
        return 0
    _opportunistic_drain(config)
    return 0


def _cmd_drain(args: argparse.Namespace, config: TelemetryConfig) -> int:
    spool = Spool(spool_dir(config))
    inner = _inner_exporter(config, timeout=DRAIN_TIMEOUT_SECONDS)
    if inner is None:
        print(f"no endpoint or output configured; {spool.depth()} span(s) remain spooled")
        return 0
    exported = spool.drain(inner, batch_size=args.batch_size, max_batches=args.max_batches)
    print(f"exported {exported} span(s)")
    return 0


def _cmd_status(config: TelemetryConfig) -> int:
    spool = Spool(spool_dir(config))
    payload = {
        "endpoint": config.endpoint,
        "token": _mask_token(config.token),
        "service": config.service,
        "tenant": config.tenant,
        "environment": config.environment,
        "capture_content": config.capture_content,
        "output": config.output,
        "home": str(config.home),
        "enabled": config.enabled,
        "spool_depth": spool.depth(),
        "spool_bytes": spool.size_bytes(),
        "session_count": session_trace.count_sessions(state_dir(config)),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _emit_legacy_run(
    args: argparse.Namespace,
    config: TelemetryConfig,
    attributes: dict[str, Any],
) -> int:
    """Standalone one-shot run (pre-session behavior of emit-event)."""
    if args.otlp_endpoint:
        exporter: Exporter = OTLPHTTPExporter(
            endpoint=args.otlp_endpoint, service_name=config.service
        )
    elif config.output:
        exporter = JSONLFileExporter(config.output)
    else:
        # Local-only mode: keep the span durable instead of dumping it to
        # stdout, which callers (hooks, MCP-style hosts) may parse.
        local_path = local_spans_path(config)
        exporter = JSONLFileExporter(local_path)
        print(f"agent-telemetry: no endpoint/output; writing to {local_path}", file=sys.stderr)
    client = _build_client(args, config, exporter)
    with client.run(args.run_name):
        client.record_event(args.event_name, attributes)
    return 0


def _build_client(
    args: argparse.Namespace,
    config: TelemetryConfig,
    exporter: Exporter,
) -> TelemetryClient:
    return TelemetryClient(
        service_name=config.service,
        tenant_id=config.tenant,
        exporter=exporter,
        redaction=RedactionConfig(capture_content=config.capture_content),
        environment=config.environment,
        collection_layer=args.layer,
    )


def _apply_global_overrides(args: argparse.Namespace) -> None:
    """Project global CLI flags onto the env so load_config() sees them."""
    if args.service_name:
        os.environ["AGENT_TELEMETRY_SERVICE"] = args.service_name
    if args.tenant_id:
        os.environ["AGENT_TELEMETRY_TENANT"] = args.tenant_id
    if args.capture_content:
        os.environ["AGENT_TELEMETRY_CAPTURE_CONTENT"] = "1"


def _read_stdin_attributes() -> dict[str, Any]:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read().strip()
        if not raw:
            return {}
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        print("agent-telemetry: warning: stdin must be a JSON object; ignored", file=sys.stderr)
        return {}
    except Exception as exc:
        print(f"agent-telemetry: warning: bad stdin attributes: {exc}", file=sys.stderr)
        return {}


def _mask_token(token: str | None) -> str | None:
    if not token:
        return None
    if len(token) <= 4:
        return "***"
    return f"{token[:4]}***"


def _inner_exporter(config: TelemetryConfig, *, timeout: float) -> Exporter | None:
    if config.endpoint:
        headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
        return OTLPHTTPExporter(
            config.endpoint,
            headers=headers,
            service_name=config.service,
            timeout_seconds=timeout,
        )
    if config.output:
        return JSONLFileExporter(config.output)
    return None


def _opportunistic_drain(
    config: TelemetryConfig,
    budget_seconds: float = OPPORTUNISTIC_DRAIN_BUDGET_SECONDS,
) -> int:
    """Best-effort quick drain after spooling; on failure data stays spooled."""
    try:
        inner = _inner_exporter(config, timeout=budget_seconds)
        if inner is None:
            return 0
        spool = Spool(spool_dir(config))
        deadline = time.monotonic() + budget_seconds
        exported = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if isinstance(inner, OTLPHTTPExporter):
                inner.timeout_seconds = max(0.1, remaining)
            drained = spool.drain(inner, batch_size=100, max_batches=1)
            if drained == 0:
                break
            exported += drained
        return exported
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
