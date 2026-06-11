from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import time
from typing import Any, Callable


STATUS_OK = "STATUS_CODE_OK"
STATUS_ERROR = "STATUS_CODE_ERROR"
SPAN_CLIENT = "SPAN_KIND_CLIENT"
SPAN_INTERNAL = "SPAN_KIND_INTERNAL"


def now_unix_nano() -> int:
    return time.time_ns()


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def identity(value: Any) -> Any:
    return value


def _coerce_nano(value: Any, default: int | None) -> int | None:
    """Best-effort int coercion for untrusted timestamp fields."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class TelemetryEvent:
    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    time_unix_nano: int = field(default_factory=now_unix_nano)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "time_unix_nano": self.time_unix_nano,
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TelemetryEvent":
        coerced_time = _coerce_nano(data.get("time_unix_nano"), now_unix_nano())
        return cls(
            name=str(data.get("name", "")),
            attributes=dict(data.get("attributes") or {}),
            time_unix_nano=coerced_time if coerced_time is not None else now_unix_nano(),
        )


@dataclass
class TelemetrySpan:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    span_kind: str = SPAN_INTERNAL
    start_time_unix_nano: int = field(default_factory=now_unix_nano)
    end_time_unix_nano: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[TelemetryEvent] = field(default_factory=list)
    status_code: str = STATUS_OK
    status_message: str = ""
    _redact: Callable[[Any], Any] = field(default=identity, repr=False, compare=False)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = self._redact(value)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append(TelemetryEvent(name=name, attributes=self._redact(attributes or {})))

    def set_result(self, result: Any) -> None:
        self.add_event("llm.result", {"result": result})

    def record_exception(self, exc: BaseException) -> None:
        self.status_code = STATUS_ERROR
        self.status_message = exc.__class__.__name__
        self.add_event(
            "exception",
            {
                "exception.type": exc.__class__.__name__,
                "exception.message": str(exc),
            },
        )

    def finish(self) -> None:
        if self.end_time_unix_nano is None:
            self.end_time_unix_nano = now_unix_nano()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "span_kind": self.span_kind,
            "start_time_unix_nano": self.start_time_unix_nano,
            "end_time_unix_nano": self.end_time_unix_nano,
            "attributes": self.attributes,
            "events": [event.to_dict() for event in self.events],
            "status": {
                "code": self.status_code,
                "message": self.status_message,
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TelemetrySpan":
        status = data.get("status") or {}
        if not isinstance(status, dict):
            status = {}
        start_value = _coerce_nano(data.get("start_time_unix_nano"), now_unix_nano())
        end_value = _coerce_nano(data.get("end_time_unix_nano"), None)
        raw_events = data.get("events") or []
        return cls(
            name=str(data.get("name", "")),
            trace_id=str(data.get("trace_id") or new_trace_id()),
            span_id=str(data.get("span_id") or new_span_id()),
            parent_span_id=data.get("parent_span_id"),
            span_kind=str(data.get("span_kind", SPAN_INTERNAL)),
            start_time_unix_nano=start_value if start_value is not None else now_unix_nano(),
            end_time_unix_nano=end_value,
            attributes=dict(data.get("attributes") or {}),
            events=[
                TelemetryEvent.from_dict(event)
                for event in raw_events
                if isinstance(event, dict)
            ],
            status_code=str(status.get("code", STATUS_OK)),
            status_message=str(status.get("message", "")),
        )
