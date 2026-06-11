from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from .config import TelemetryConfig, load_config, local_spans_path, spool_dir
from .exporters import (
    BackgroundExporter,
    Exporter,
    JSONLFileExporter,
    NoopExporter,
    OTLPHTTPExporter,
    SpoolExporter,
)
from .redaction import RedactionConfig, Redactor
from .schema import (
    SPAN_CLIENT,
    SPAN_INTERNAL,
    TelemetrySpan,
    new_span_id,
    new_trace_id,
)
from .spool import Spool


DEFAULT_COLLECTION_LAYER = "sdk"

_current_run: ContextVar["_RunState | None"] = ContextVar("agent_telemetry_current_run", default=None)
# default=None (not []) so concurrent contexts never share one mutable list.
_span_stack: ContextVar[list[TelemetrySpan] | None] = ContextVar("agent_telemetry_span_stack", default=None)


def _current_stack() -> list[TelemetrySpan]:
    stack = _span_stack.get()
    return [] if stack is None else stack


@dataclass
class _RunState:
    trace_id: str
    spans: list[TelemetrySpan] = field(default_factory=list)


class TelemetryClient:
    def __init__(
        self,
        service_name: str,
        tenant_id: str,
        *,
        exporter: Exporter | None = None,
        redaction: RedactionConfig | Redactor | None = None,
        environment: str = "local",
        resource_attributes: dict[str, Any] | None = None,
        collection_layer: str = DEFAULT_COLLECTION_LAYER,
        spool: Spool | None = None,
    ):
        self.service_name = service_name
        self.tenant_id = tenant_id
        self.environment = environment
        # Default to the durable spool: a bare client must never write span
        # JSON to the host process stdout.
        self.exporter = exporter or SpoolExporter()
        self.resource_attributes = resource_attributes or {}
        self.redactor = redaction if isinstance(redaction, Redactor) else Redactor(redaction)
        self.pending_spans: list[TelemetrySpan] = []
        self.collection_layer = collection_layer
        self._spool = spool

    @classmethod
    def from_env(
        cls,
        *,
        collection_layer: str = DEFAULT_COLLECTION_LAYER,
        exporter_mode: str = "background",
        config: TelemetryConfig | None = None,
    ) -> "TelemetryClient":
        cfg = config or load_config()
        spool: Spool | None = None
        if not cfg.enabled:
            exporter: Exporter = NoopExporter()
        else:
            spool = Spool(spool_dir(cfg))
            inner = _build_inner_exporter(cfg)
            if exporter_mode == "spool":
                exporter = SpoolExporter(spool)
            elif exporter_mode == "direct":
                exporter = inner
            else:
                exporter = BackgroundExporter(inner, spool=spool)
        return cls(
            service_name=cfg.service,
            tenant_id=cfg.tenant,
            exporter=exporter,
            redaction=RedactionConfig(capture_content=cfg.capture_content),
            environment=cfg.environment,
            collection_layer=collection_layer,
            spool=spool,
        )

    @contextmanager
    def run(
        self,
        name: str,
        *,
        user_id: str | None = None,
        agent_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[TelemetrySpan]:
        trace_id = new_trace_id()
        root = self._make_span(
            name=f"agent.run {name}",
            trace_id=trace_id,
            parent_span_id=None,
            span_kind=SPAN_INTERNAL,
            attributes={
                "agent.telemetry.schema_version": "0.1.0",
                "deployment.environment": self.environment,
                "service.name": self.service_name,
                "tenant.id": self.tenant_id,
                "gen_ai.operation.name": "invoke_agent",
                **({"enduser.id": user_id} if user_id else {}),
                **({"gen_ai.agent.name": agent_name} if agent_name else {}),
                **self.redactor.flatten(metadata or {}, "metadata"),
            },
        )
        state = _RunState(trace_id=trace_id, spans=[root])
        run_token = _current_run.set(state)
        stack_token = _span_stack.set([root])
        try:
            yield root
        except BaseException as exc:
            root.record_exception(exc)
            raise
        finally:
            root.finish()
            _span_stack.reset(stack_token)
            _current_run.reset(run_token)
            self._export_or_buffer(state.spans)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attributes: dict[str, Any] | None = None,
        span_kind: str = SPAN_INTERNAL,
    ) -> Iterator[TelemetrySpan]:
        state = self._require_run()
        parent = self._require_parent_span()
        span = self._make_span(
            name=name,
            trace_id=state.trace_id,
            parent_span_id=parent.span_id,
            span_kind=span_kind,
            attributes=attributes or {},
        )
        state.spans.append(span)
        stack = [*_current_stack(), span]
        stack_token = _span_stack.set(stack)
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise
        finally:
            span.finish()
            _span_stack.reset(stack_token)

    @contextmanager
    def tool_call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[TelemetrySpan]:
        attrs = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            **self.redactor.flatten(arguments or {}, "tool.arguments"),
            **self.redactor.flatten(metadata or {}, "metadata"),
        }
        with self.span(f"execute_tool {name}", attributes=attrs, span_kind=SPAN_INTERNAL) as span:
            yield span

    @contextmanager
    def llm_call(
        self,
        *,
        provider: str,
        model: str,
        prompt: Any | None = None,
        operation: str = "chat",
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[TelemetrySpan]:
        attrs = {
            "gen_ai.operation.name": operation,
            "gen_ai.provider.name": provider,
            "gen_ai.request.model": model,
            **self.redactor.flatten(prompt or {}, "prompt"),
            **self.redactor.flatten(metadata or {}, "metadata"),
        }
        span_name = f"{operation} {model}"
        with self.span(span_name, attributes=attrs, span_kind=SPAN_CLIENT) as span:
            yield span

    @contextmanager
    def retrieval(
        self,
        *,
        source: str,
        query: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[TelemetrySpan]:
        attrs = {
            "gen_ai.operation.name": "retrieve",
            "gen_ai.data_source.id": source,
            **({"retrieval.query": query} if query is not None else {}),
            **self.redactor.flatten(metadata or {}, "metadata"),
        }
        with self.span(f"retrieve {source}", attributes=attrs, span_kind=SPAN_CLIENT) as span:
            yield span

    def decision(
        self,
        name: str,
        *,
        rationale: str | None = None,
        confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        attrs: dict[str, Any] = {"decision.name": name}
        if rationale is not None:
            attrs["decision.rationale"] = rationale
        if confidence is not None:
            attrs["decision.confidence"] = confidence
        attrs.update(self.redactor.flatten(metadata or {}, "metadata"))
        self._require_parent_span().add_event("agent.decision", attrs)

    def record_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self._require_parent_span().add_event(name, attributes or {})

    def flush_pending(self) -> int:
        if not self.pending_spans:
            return 0
        spans = list(self.pending_spans)
        self.exporter.export(spans)
        self.pending_spans.clear()
        return len(spans)

    def _make_span(
        self,
        *,
        name: str,
        trace_id: str,
        parent_span_id: str | None,
        span_kind: str,
        attributes: dict[str, Any],
    ) -> TelemetrySpan:
        return TelemetrySpan(
            name=name,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=parent_span_id,
            span_kind=span_kind,
            attributes={
                "telemetry.collection_layer": self.collection_layer,
                **self.redactor.redact(attributes),
            },
            _redact=self.redactor.redact,
        )

    def _export_or_buffer(self, spans: list[TelemetrySpan]) -> None:
        try:
            self.exporter.export(spans)
        except Exception:
            self.pending_spans.extend(spans)
            try:
                self._failover_spool().append(spans)
            except Exception:
                pass

    def _failover_spool(self) -> Spool:
        if self._spool is None:
            self._spool = Spool()
        return self._spool

    def _require_run(self) -> _RunState:
        state = _current_run.get()
        if state is None:
            raise RuntimeError("No active telemetry run. Wrap agent work in client.run(...).")
        return state

    def _require_parent_span(self) -> TelemetrySpan:
        stack = _current_stack()
        if not stack:
            raise RuntimeError("No active telemetry span. Wrap work in client.run(...).")
        return stack[-1]


def _build_inner_exporter(cfg: TelemetryConfig) -> Exporter:
    if cfg.endpoint:
        headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
        return OTLPHTTPExporter(cfg.endpoint, headers=headers, service_name=cfg.service)
    if cfg.output:
        return JSONLFileExporter(cfg.output)
    # Local-only mode: keep spans in a private JSONL file under the telemetry
    # home. Never the console — host stdout may be a parsed protocol stream.
    return JSONLFileExporter(local_spans_path(cfg))
