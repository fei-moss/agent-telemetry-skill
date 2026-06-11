"""Detached span and event emission without a ``client.run()`` context.

Built for short-lived processes (hooks, CLI calls made by the model, log
watchers) that must contribute spans to an existing trace and exit fast.
Spans default to the on-disk spool so the host critical path never touches
the network. Emission helpers return False instead of raising.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import session_trace
from .config import load_config
from .exporters import Exporter, SpoolExporter
from .redaction import RedactionConfig, Redactor
from .schema import (
    SPAN_INTERNAL,
    STATUS_OK,
    TelemetryEvent,
    TelemetrySpan,
    new_span_id,
    new_trace_id,
    now_unix_nano,
)


DEFAULT_COLLECTION_LAYER = "model_reported"


def build_span(
    name: str,
    *,
    trace_id: str,
    parent_span_id: str | None = None,
    attributes: dict[str, Any] | None = None,
    events: Iterable[TelemetryEvent | dict[str, Any]] | None = None,
    start_time_unix_nano: int | None = None,
    end_time_unix_nano: int | None = None,
    span_kind: str = SPAN_INTERNAL,
    status_code: str = STATUS_OK,
    status_message: str = "",
    redactor: Redactor | None = None,
    collection_layer: str = DEFAULT_COLLECTION_LAYER,
) -> TelemetrySpan:
    """Build a standalone span stamped with resource and trust attributes.

    Attributes and event payloads pass through the redactor (default-on,
    driven by the resolved config's capture_content flag).
    """
    config = load_config()
    active = redactor or Redactor(RedactionConfig(capture_content=config.capture_content))
    start = start_time_unix_nano if start_time_unix_nano is not None else now_unix_nano()
    end = end_time_unix_nano if end_time_unix_nano is not None else start
    span = TelemetrySpan(
        name=name,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=parent_span_id,
        span_kind=span_kind,
        start_time_unix_nano=start,
        end_time_unix_nano=end,
        attributes={
            "telemetry.collection_layer": collection_layer,
            "service.name": config.service,
            "tenant.id": config.tenant,
            "deployment.environment": config.environment,
            **active.redact(dict(attributes or {})),
        },
        status_code=status_code,
        status_message=status_message,
        _redact=active.redact,
    )
    for event in events or ():
        _append_event(span, event, active)
    return span


def emit_span(
    span: TelemetrySpan | None = None,
    *,
    exporter: Exporter | None = None,
    **build_kwargs: Any,
) -> bool:
    """Export a prebuilt span, or build one from ``build_span`` kwargs.

    Returns True on success, False on any failure or when telemetry is
    disabled. Never raises.
    """
    try:
        if not load_config().enabled:
            return False
        target = span if span is not None else build_span(**build_kwargs)
        (exporter or SpoolExporter()).export([target])
        return True
    except Exception:
        return False


def emit_event(
    name: str,
    attributes: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    exporter: Exporter | None = None,
    collection_layer: str = DEFAULT_COLLECTION_LAYER,
) -> bool:
    """Emit one event as a zero-duration child span of the target trace.

    With ``session_id`` the session trace is resolved (auto-created via
    session_trace.begin so early events are never lost) and the span parents
    to the session root. Returns False instead of raising.
    """
    try:
        if not load_config().enabled:
            return False
        resolved_trace = trace_id
        resolved_parent = parent_span_id
        if session_id:
            record = session_trace.begin(session_id)
            resolved_trace = record.trace_id
            resolved_parent = record.root_span_id
        if not resolved_trace:
            resolved_trace = new_trace_id()
        span_attributes: dict[str, Any] = (
            {"session.id": session_id} if session_id else {}
        )
        span = build_span(
            name,
            trace_id=resolved_trace,
            parent_span_id=resolved_parent,
            attributes=span_attributes,
            events=[{"name": name, "attributes": dict(attributes or {})}],
            collection_layer=collection_layer,
        )
        return emit_span(span, exporter=exporter)
    except Exception:
        return False


def _append_event(
    span: TelemetrySpan,
    event: TelemetryEvent | dict[str, Any],
    redactor: Redactor,
) -> None:
    if isinstance(event, TelemetryEvent):
        span.events.append(
            TelemetryEvent(
                name=event.name,
                attributes=redactor.redact(dict(event.attributes)),
                time_unix_nano=event.time_unix_nano,
            )
        )
    elif isinstance(event, dict):
        stamp = event.get("time_unix_nano")
        if stamp is None:
            # Reconstructed spans (e.g. log watchers) carry historical times;
            # default an unstamped event to the span's end rather than wall-clock
            # now, so the backend orders events within the trace correctly.
            stamp = span.end_time_unix_nano or span.start_time_unix_nano
        span.events.append(
            TelemetryEvent(
                name=str(event.get("name", "event")),
                attributes=redactor.redact(dict(event.get("attributes") or {})),
                time_unix_nano=stamp,
            )
        )
