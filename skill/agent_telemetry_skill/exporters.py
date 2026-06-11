from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Iterable, Protocol, TextIO
from urllib import request
import weakref

from .schema import TelemetryEvent, TelemetrySpan
from .spool import Spool


# One shared atexit hook flushes every live BackgroundExporter within a single
# global budget, so short-lived clients can never stack up exit-time work.
_ATEXIT_FLUSH_BUDGET_SECONDS = 2.0
_active_background_exporters: "weakref.WeakSet[BackgroundExporter]" = weakref.WeakSet()
_atexit_lock = threading.Lock()
_atexit_registered = False


def _flush_background_exporters_at_exit() -> None:
    deadline = time.monotonic() + _ATEXIT_FLUSH_BUDGET_SECONDS
    for exporter in list(_active_background_exporters):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            exporter.flush(remaining)
        except Exception:
            continue


def _register_background_exporter(exporter: "BackgroundExporter") -> None:
    global _atexit_registered
    with _atexit_lock:
        _active_background_exporters.add(exporter)
        if not _atexit_registered:
            atexit.register(_flush_background_exporters_at_exit)
            _atexit_registered = True


class Exporter(Protocol):
    def export(self, spans: list[TelemetrySpan]) -> None:
        ...


class NoopExporter:
    def export(self, spans: list[TelemetrySpan]) -> None:
        return None


class SpoolExporter:
    """Writes spans to the on-disk spool only — zero network on the host path.

    Intended for short-lived hook processes; a separate drainer ships data.
    """

    def __init__(self, spool: Spool | None = None):
        self.spool = spool or Spool()

    def export(self, spans: list[TelemetrySpan]) -> None:
        self.spool.append(spans)


class BackgroundExporter:
    """Spools spans locally and ships them via `inner` on a daemon thread.

    export() never raises and never touches the network; a lazily-started
    daemon thread drains the spool. flush() drains synchronously.
    """

    def __init__(
        self,
        inner: Exporter,
        spool: Spool | None = None,
        flush_interval: float = 2.0,
    ):
        self.inner = inner
        self.spool = spool or Spool()
        self.flush_interval = flush_interval
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._drain_lock = threading.Lock()
        _register_background_exporter(self)

    def export(self, spans: list[TelemetrySpan]) -> None:
        try:
            self.spool.append(spans)
            self._ensure_thread()
            self._wake.set()
        except Exception:
            return

    def flush(self, timeout: float = 5.0) -> int:
        """Drain synchronously, never exceeding ``timeout`` by more than one
        bounded batch: the remaining budget caps both the wait for the drain
        lock and the inner exporter's network timeout."""
        exported = 0
        deadline = time.monotonic() + timeout
        try:
            while self.spool.depth() > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                drained = self._drain_once(budget_seconds=remaining)
                if drained == 0:
                    break
                exported += drained
        except Exception:
            return exported
        return exported

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            thread = threading.Thread(
                target=self._worker,
                name="agent-telemetry-background-exporter",
                daemon=True,
            )
            thread.start()
            self._thread = thread

    def _worker(self) -> None:
        while True:
            self._wake.wait(self.flush_interval)
            self._wake.clear()
            self._drain_once()

    def _drain_once(self, budget_seconds: float | None = None) -> int:
        lock_timeout = -1.0 if budget_seconds is None else max(0.05, budget_seconds)
        if not self._drain_lock.acquire(timeout=lock_timeout):
            return 0  # a slow in-flight drain must not block the caller
        try:
            max_batches = None
            if budget_seconds is not None:
                max_batches = 1  # one bounded batch per loop iteration
                current = getattr(self.inner, "timeout_seconds", None)
                if isinstance(current, (int, float)):
                    self.inner.timeout_seconds = max(0.1, min(float(current), budget_seconds))
            return self.spool.drain(self.inner, max_batches=max_batches)
        except Exception:
            return 0
        finally:
            self._drain_lock.release()


class InMemoryExporter:
    def __init__(self):
        self.spans: list[TelemetrySpan] = []

    def export(self, spans: list[TelemetrySpan]) -> None:
        self.spans.extend(spans)


class ConsoleExporter:
    def __init__(self, stream: TextIO | None = None):
        self.stream = stream or sys.stdout

    def dumps(self, spans: Iterable[TelemetrySpan]) -> str:
        return "".join(
            json.dumps(span.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for span in spans
        )

    def export(self, spans: list[TelemetrySpan]) -> None:
        self.stream.write(self.dumps(spans))
        self.stream.flush()


class JSONLFileExporter:
    """Appends spans as JSONL, owner-only (0o600 file in a 0o700 dir)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def export(self, spans: list[TelemetrySpan]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(span.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
            for span in spans
        )
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)


class OTLPHTTPExporter:
    def __init__(
        self,
        endpoint: str = "http://localhost:4318/v1/traces",
        *,
        headers: dict[str, str] | None = None,
        service_name: str = "agent-telemetry-skill",
        timeout_seconds: float = 5.0,
        resource_attributes: dict[str, Any] | None = None,
    ):
        self.endpoint = endpoint
        self.headers = headers or {}
        self.service_name = service_name
        self.timeout_seconds = timeout_seconds
        self.resource_attributes = resource_attributes or {}

    def export(self, spans: list[TelemetrySpan]) -> None:
        payload = self.build_payload(
            spans,
            service_name=self.service_name,
            resource_attributes=self.resource_attributes,
        )
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            **self.headers,
        }
        req = request.Request(self.endpoint, data=body, headers=headers, method="POST")
        with request.urlopen(req, timeout=self.timeout_seconds) as response:
            if response.status >= 400:
                raise RuntimeError(f"OTLP export failed with HTTP {response.status}")

    @staticmethod
    def build_payload(
        spans: list[TelemetrySpan],
        *,
        service_name: str,
        resource_attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resource_attrs = {
            "service.name": service_name,
            "telemetry.sdk.name": "agent-telemetry-skill",
            **(resource_attributes or {}),
        }
        return {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_otlp_attribute(k, v) for k, v in resource_attrs.items()]},
                    "scopeSpans": [
                        {
                            "scope": {"name": "agent-telemetry-skill", "version": "0.1.0"},
                            "spans": [_otlp_span(span) for span in spans],
                        }
                    ],
                }
            ]
        }


def _otlp_span(span: TelemetrySpan) -> dict[str, Any]:
    payload = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": span.span_kind,
        "startTimeUnixNano": str(span.start_time_unix_nano),
        "endTimeUnixNano": str(span.end_time_unix_nano or span.start_time_unix_nano),
        "attributes": [_otlp_attribute(k, v) for k, v in span.attributes.items()],
        "events": [_otlp_event(event) for event in span.events],
        "status": {
            "code": span.status_code,
            "message": span.status_message,
        },
    }
    if span.parent_span_id:
        payload["parentSpanId"] = span.parent_span_id
    return payload


def _otlp_event(event: TelemetryEvent) -> dict[str, Any]:
    return {
        "timeUnixNano": str(event.time_unix_nano),
        "name": event.name,
        "attributes": [_otlp_attribute(k, v) for k, v in event.attributes.items()],
    }


def _otlp_attribute(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": _otlp_value(value)}


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if value is None:
        return {"stringValue": ""}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_otlp_value(item) for item in value]}}
    return {"stringValue": json.dumps(value, ensure_ascii=False, sort_keys=True)}
