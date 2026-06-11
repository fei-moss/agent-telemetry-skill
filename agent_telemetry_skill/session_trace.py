"""File-backed session registry for cross-process trace continuity.

Maps a stable ``session_id`` to one trace so spans emitted by separate
short-lived processes (hooks, CLI invocations, log watchers) join a single
trace. State lives under ``<state_dir>/sessions`` (one JSON file per
sanitized session id) plus ``<state_dir>/open-spans`` for in-flight span
payloads awaiting correlation by another process.

Every public function is best-effort: it never raises out of telemetry
into the host agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any

from .config import load_config, state_dir as default_state_dir
from .exporters import Exporter, SpoolExporter
from .redaction import RedactionConfig, Redactor
from .schema import (
    SPAN_INTERNAL,
    STATUS_OK,
    TelemetrySpan,
    new_span_id,
    new_trace_id,
    now_unix_nano,
)


SESSIONS_DIR_NAME = "sessions"
OPEN_SPANS_DIR_NAME = "open-spans"
SCHEMA_VERSION = "0.1.0"
# Root spans built here close out hook-managed sessions by default.
DEFAULT_ROOT_COLLECTION_LAYER = "hook"

_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_SANITIZED_LENGTH = 64
_DIGEST_LENGTH = 8


@dataclass
class SessionTrace:
    session_id: str
    trace_id: str
    root_span_id: str
    start_time_unix_nano: int
    agent_name: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "trace_id": self.trace_id,
            "root_span_id": self.root_span_id,
            "start_time_unix_nano": self.start_time_unix_nano,
            "agent_name": self.agent_name,
            "attributes": self.attributes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionTrace":
        start_value = data.get("start_time_unix_nano")
        agent_name = data.get("agent_name")
        attributes = data.get("attributes")
        return cls(
            session_id=str(data.get("session_id", "")),
            trace_id=str(data.get("trace_id") or new_trace_id()),
            root_span_id=str(data.get("root_span_id") or new_span_id()),
            start_time_unix_nano=int(start_value) if start_value is not None else now_unix_nano(),
            agent_name=str(agent_name) if agent_name is not None else None,
            attributes=dict(attributes) if isinstance(attributes, dict) else {},
        )


def begin(
    session_id: str,
    *,
    agent_name: str | None = None,
    attributes: dict[str, Any] | None = None,
    state_dir: str | Path | None = None,
) -> SessionTrace:
    """Return the session record, creating and persisting it if absent.

    Idempotent and concurrent-safe: parallel callers converge on one record.
    """
    try:
        base = _resolve_state_dir(state_dir)
        existing = get(session_id, state_dir=base)
        if existing is not None:
            return existing
        record = SessionTrace(
            session_id=str(session_id),
            trace_id=new_trace_id(),
            root_span_id=new_span_id(),
            start_time_unix_nano=now_unix_nano(),
            agent_name=agent_name,
            attributes=_default_redactor().redact(dict(attributes or {})),
        )
        if _create_exclusive(_session_path(base, session_id), record.to_dict()):
            return record
        racer = get(session_id, state_dir=base)
        return racer if racer is not None else record
    except Exception:
        return _fallback_record(session_id, agent_name, attributes)


def get(session_id: str, state_dir: str | Path | None = None) -> SessionTrace | None:
    try:
        base = _resolve_state_dir(state_dir)
        return _read_record(_session_path(base, session_id))
    except Exception:
        return None


def end(
    session_id: str,
    *,
    attributes: dict[str, Any] | None = None,
    status_code: str | None = None,
    exporter: Exporter | None = None,
    state_dir: str | Path | None = None,
) -> TelemetrySpan | None:
    """Build and export the session root span, then delete session state.

    Idempotent: only the caller that claims the state file emits the root
    span; later calls return None.
    """
    try:
        base = _resolve_state_dir(state_dir)
        path = _session_path(base, session_id)
        claimed = path.with_name(f"{path.name}.ending-{os.getpid()}-{time.time_ns()}")
        try:
            os.rename(path, claimed)
        except OSError:
            return None  # unknown session, or another process already ended it
        record = _read_record(claimed)
        claimed.unlink(missing_ok=True)
        _discard_open_spans(base, session_id)
        if record is None:
            return None
        return _emit_root_span(record, attributes, status_code, exporter)
    except Exception:
        return None


def record_open(
    session_id: str,
    key: str,
    payload: dict[str, Any],
    *,
    state_dir: str | Path | None = None,
) -> bool:
    """Persist an open-span payload (e.g. tool start time + args) for later
    correlation by another process. Returns False on any failure."""
    try:
        base = _resolve_state_dir(state_dir)
        path = _open_span_path(base, session_id, key)
        redacted = _default_redactor().redact(dict(payload))
        _atomic_replace(path, json.dumps(redacted, ensure_ascii=False, sort_keys=True))
        return True
    except Exception:
        return False


def pop_open(
    session_id: str,
    key: str,
    *,
    state_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Atomically claim and return a previously recorded open-span payload."""
    try:
        base = _resolve_state_dir(state_dir)
        path = _open_span_path(base, session_id, key)
        claimed = path.with_name(f"{path.name}.pop-{os.getpid()}-{time.time_ns()}")
        try:
            os.rename(path, claimed)
        except OSError:
            return None
        try:
            data = json.loads(claimed.read_text(encoding="utf-8"))
        finally:
            claimed.unlink(missing_ok=True)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def count_sessions(state_dir: str | Path | None = None) -> int:
    try:
        sessions = _resolve_state_dir(state_dir) / SESSIONS_DIR_NAME
        return sum(1 for _ in sessions.glob("*.json"))
    except Exception:
        return 0


def _emit_root_span(
    record: SessionTrace,
    attributes: dict[str, Any] | None,
    status_code: str | None,
    exporter: Exporter | None,
) -> TelemetrySpan:
    config = load_config()
    redactor = Redactor(RedactionConfig(capture_content=config.capture_content))
    merged: dict[str, Any] = {
        "telemetry.collection_layer": DEFAULT_ROOT_COLLECTION_LAYER,
        "agent.telemetry.schema_version": SCHEMA_VERSION,
        "service.name": config.service,
        "tenant.id": config.tenant,
        "deployment.environment": config.environment,
        "gen_ai.operation.name": "invoke_agent",
        "session.id": record.session_id,
        **({"gen_ai.agent.name": record.agent_name} if record.agent_name else {}),
        **redactor.redact(dict(record.attributes)),
        **redactor.redact(dict(attributes or {})),
    }
    root = TelemetrySpan(
        name=f"agent.run {record.agent_name or record.session_id}",
        trace_id=record.trace_id,
        span_id=record.root_span_id,
        parent_span_id=None,
        span_kind=SPAN_INTERNAL,
        start_time_unix_nano=record.start_time_unix_nano,
        end_time_unix_nano=now_unix_nano(),
        attributes=merged,
        status_code=status_code or STATUS_OK,
        _redact=redactor.redact,
    )
    if config.enabled:
        try:
            (exporter or SpoolExporter()).export([root])
        except Exception:
            pass
    return root


def _resolve_state_dir(state_dir: str | Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    return default_state_dir(load_config())


def _default_redactor() -> Redactor:
    return Redactor(RedactionConfig(capture_content=load_config().capture_content))


def _sanitize(value: str) -> str:
    """Filesystem-safe, collision-resistant name for an arbitrary id."""
    text = str(value)
    cleaned = _SANITIZE_PATTERN.sub("_", text).strip("._") or "session"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    return f"{cleaned[:_MAX_SANITIZED_LENGTH]}-{digest}"


def _session_path(base: Path, session_id: str) -> Path:
    return base / SESSIONS_DIR_NAME / f"{_sanitize(session_id)}.json"


def _open_span_path(base: Path, session_id: str, key: str) -> Path:
    return base / OPEN_SPANS_DIR_NAME / _sanitize(session_id) / f"{_sanitize(key)}.json"


def _read_record(path: Path) -> SessionTrace | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return SessionTrace.from_dict(data)


def _create_exclusive(path: Path, payload: dict[str, Any]) -> bool:
    """Atomically create `path` with `payload`; False if it already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    _write_private(tmp, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    try:
        os.link(tmp, path)  # atomic, fails if path exists (no overwrite)
        return True
    except FileExistsError:
        return False
    except OSError:
        os.replace(tmp, path)  # filesystems without hard-link support
        return True
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    _write_private(tmp, text)
    os.replace(tmp, path)


def _write_private(path: Path, text: str) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _discard_open_spans(base: Path, session_id: str) -> None:
    try:
        shutil.rmtree(base / OPEN_SPANS_DIR_NAME / _sanitize(session_id), ignore_errors=True)
    except Exception:
        return


def _fallback_record(
    session_id: str,
    agent_name: str | None,
    attributes: dict[str, Any] | None,
) -> SessionTrace:
    return SessionTrace(
        session_id=str(session_id),
        trace_id=new_trace_id(),
        root_span_id=new_span_id(),
        start_time_unix_nano=now_unix_nano(),
        agent_name=agent_name,
        attributes=dict(attributes or {}),
    )
