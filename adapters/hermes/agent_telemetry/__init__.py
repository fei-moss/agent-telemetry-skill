"""Hermes runtime plugin that exports session telemetry via agent-telemetry-skill.

Install layout (see adapters/hermes/install.py): the package contents live in
``~/.hermes/plugins/agent-telemetry/`` with the ``agent_telemetry_skill``
package vendored next to ``__init__.py``. The plugin also works straight from
the repo checkout under ``adapters/hermes/``.

Telemetry must never break the host: every hook entrypoint is wrapped in a
catch-all, exports never block (BackgroundExporter / SpoolExporter), and a
failed export falls back to the on-disk spool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import threading
from typing import Any

_SKILL_DIR = Path.home() / ".hermes" / "skills" / "observability" / "agent-telemetry"


def _bootstrap_sys_path() -> None:
    """Make agent_telemetry_skill importable from known vendoring locations."""
    package_dir = Path(__file__).resolve().parent
    candidates = [_SKILL_DIR, package_dir, package_dir.parent]
    parents = package_dir.parents
    if len(parents) >= 3:
        candidates.append(parents[2])  # repo root when running from adapters/hermes
    for candidate in candidates:
        try:
            if (candidate / "agent_telemetry_skill" / "__init__.py").is_file():
                path_str = str(candidate)
                if path_str not in sys.path:
                    sys.path.insert(0, path_str)
        except OSError:
            continue


_bootstrap_sys_path()

# A missing/corrupt vendored package must never raise out of plugin import:
# register() silently no-ops when the import failed.
_IMPORT_OK = False
try:
    from agent_telemetry_skill import (
        BackgroundExporter,
        JSONLFileExporter,
        NoopExporter,
        OTLPHTTPExporter,
        RedactionConfig,
        Redactor,
        Spool,
        SpoolExporter,
    )
    from agent_telemetry_skill.config import (
        DEFAULT_SERVICE,
        TelemetryConfig,
        load_config,
        spool_dir,
    )
    from agent_telemetry_skill.exporters import Exporter
    from agent_telemetry_skill.schema import (
        SPAN_CLIENT,
        SPAN_INTERNAL,
        TelemetrySpan,
        new_span_id,
        new_trace_id,
        now_unix_nano,
    )

    _IMPORT_OK = True
except Exception:  # pragma: no cover - depends on a broken install
    pass

COLLECTION_LAYER = "plugin"
HERMES_DEFAULT_SERVICE = "hermes-agent"
NANOS_PER_SECOND = 1_000_000_000
# Sessions whose on_session_end never arrives are evicted (and their spans
# exported) once the buffer reaches this size, so host memory stays bounded.
MAX_BUFFERED_RUNS = 256


@dataclass
class _Run:
    session_id: str
    trace_id: str
    root: TelemetrySpan
    spans: list[TelemetrySpan] = field(default_factory=list)
    open_tools: dict[str, TelemetrySpan] = field(default_factory=dict)


class HermesTelemetryPlugin:
    def __init__(
        self,
        config: TelemetryConfig | None = None,
        *,
        exporter: Exporter | None = None,
    ) -> None:
        self.config = config or load_config()
        self.enabled = self.config.enabled
        # Keep the historical hermes-agent identity unless the operator set
        # an explicit service name via env or config file.
        self.service_name = (
            self.config.service if self.config.service != DEFAULT_SERVICE else HERMES_DEFAULT_SERVICE
        )
        self.tenant_id = self.config.tenant
        self.environment = self.config.environment
        self.redactor = Redactor(RedactionConfig(capture_content=self.config.capture_content))
        self._spool = Spool(spool_dir(self.config))
        self.exporter = exporter or self._build_exporter()
        self._runs: dict[str, _Run] = {}
        self._lock = threading.RLock()

    def register(self, ctx: Any) -> None:
        if not self.enabled:
            return
        try:
            ctx.register_hook("pre_llm_call", self.pre_llm_call)
            ctx.register_hook("post_api_request", self.post_api_request)
            ctx.register_hook("pre_tool_call", self.pre_tool_call)
            ctx.register_hook("post_tool_call", self.post_tool_call)
            ctx.register_hook("on_session_end", self.on_session_end)
        except Exception:
            return

    # -- hook entrypoints (never raise into Hermes) ----------------------------

    def pre_llm_call(self, **kwargs: Any) -> None:
        try:
            self._pre_llm_call(kwargs)
        except Exception:
            return

    def post_api_request(self, **kwargs: Any) -> None:
        try:
            self._post_api_request(kwargs)
        except Exception:
            return

    def pre_tool_call(self, **kwargs: Any) -> None:
        try:
            self._pre_tool_call(kwargs)
        except Exception:
            return

    def post_tool_call(self, **kwargs: Any) -> None:
        try:
            self._post_tool_call(kwargs)
        except Exception:
            return

    def on_session_end(self, **kwargs: Any) -> None:
        try:
            self._on_session_end(kwargs)
        except Exception:
            return

    # -- hook implementations ---------------------------------------------------

    def _pre_llm_call(self, kwargs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        session_id = str(kwargs.get("session_id") or "unknown-session")
        with self._lock:
            if session_id in self._runs:
                return
            run = self._start_run(
                session_id,
                extra_attributes={
                    "messaging.platform": kwargs.get("platform") or "",
                    "enduser.id": kwargs.get("sender_id") or "",
                    "user.message": kwargs.get("user_message") or "",
                    "conversation.message_count": len(kwargs.get("conversation_history") or []),
                },
                model=kwargs.get("model"),
            )
            run.root.add_event(
                "hermes.turn.start", {"is_first_turn": bool(kwargs.get("is_first_turn"))}
            )

    def _post_api_request(self, kwargs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        session_id = str(kwargs.get("session_id") or "unknown-session")
        with self._lock:
            run = self._ensure_run(session_id, kwargs)
            duration = float(kwargs.get("api_duration") or 0)
            end = now_unix_nano()
            start = max(run.root.start_time_unix_nano, end - int(duration * NANOS_PER_SECOND))
            span = self._make_span(
                name=f"chat {kwargs.get('model') or 'unknown-model'}",
                trace_id=run.trace_id,
                parent_span_id=run.root.span_id,
                span_kind=SPAN_CLIENT,
                attributes={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": kwargs.get("provider") or "",
                    "gen_ai.request.model": kwargs.get("model") or "",
                    "gen_ai.response.model": kwargs.get("response_model") or "",
                    "gen_ai.response.finish_reasons": kwargs.get("finish_reason") or "",
                    "gen_ai.usage.input_tokens": _usage(kwargs, "input_tokens"),
                    "gen_ai.usage.output_tokens": _usage(kwargs, "output_tokens"),
                    "hermes.api_mode": kwargs.get("api_mode") or "",
                    "hermes.api_call_count": kwargs.get("api_call_count") or 0,
                    "hermes.message_count": kwargs.get("message_count") or 0,
                    "hermes.assistant_content_chars": kwargs.get("assistant_content_chars") or 0,
                    "hermes.assistant_tool_call_count": kwargs.get("assistant_tool_call_count")
                    or 0,
                    "duration.ms": int(duration * 1000),
                },
            )
            span.start_time_unix_nano = start
            span.end_time_unix_nano = end
            run.spans.append(span)

    def _pre_tool_call(self, kwargs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        session_id = str(kwargs.get("session_id") or "")
        tool_call_id = str(kwargs.get("tool_call_id") or "")
        if not session_id and not tool_call_id:
            return
        session_id = session_id or "unknown-session"
        key = self._tool_key(kwargs)
        with self._lock:
            run = self._ensure_run(session_id, kwargs)
            if key in run.open_tools:
                return
            run.open_tools[key] = self._make_tool_span(run, kwargs, tool_call_id=tool_call_id)

    def _post_tool_call(self, kwargs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        session_id = str(kwargs.get("session_id") or "unknown-session")
        key = self._tool_key(kwargs)
        with self._lock:
            run = self._ensure_run(session_id, kwargs)
            span = run.open_tools.pop(key, None)
            if span is None:
                span = self._make_tool_span(
                    run, kwargs, tool_call_id=str(kwargs.get("tool_call_id") or "")
                )
            span.add_event("tool.result", {"result": kwargs.get("result") or ""})
            span.finish()
            run.spans.append(span)

    def _on_session_end(self, kwargs: dict[str, Any]) -> None:
        if not self.enabled:
            return
        session_id = str(kwargs.get("session_id") or "unknown-session")
        with self._lock:
            run = self._runs.pop(session_id, None)
            if run is None:
                return
            spans = self._seal_run(
                run,
                end_attributes={
                    "completed": bool(kwargs.get("completed")),
                    "interrupted": bool(kwargs.get("interrupted")),
                    "gen_ai.request.model": kwargs.get("model") or "",
                    "messaging.platform": kwargs.get("platform") or "",
                },
            )
        self._export_spans(spans)

    # -- internals ----------------------------------------------------------------

    def _seal_run(self, run: _Run, *, end_attributes: dict[str, Any]) -> list[TelemetrySpan]:
        """Finish open tool spans and the root; caller must hold self._lock."""
        for span in list(run.open_tools.values()):
            span.add_event(
                "tool.result_missing", {"reason": "session ended before post_tool_call"}
            )
            span.finish()
            run.spans.append(span)
        run.open_tools.clear()
        run.root.add_event("hermes.turn.end", end_attributes)
        run.root.finish()
        return list(run.spans)

    def _export_spans(self, spans: list[TelemetrySpan]) -> None:
        try:
            self.exporter.export(spans)
        except Exception:
            self._spool.append(spans)  # Spool.append never raises

    def _evict_oldest_run(self) -> None:
        """Flush and drop the oldest buffered run; caller must hold self._lock."""
        oldest_key = next(iter(self._runs), None)
        if oldest_key is None:
            return
        evicted = self._runs.pop(oldest_key)
        spans = self._seal_run(
            evicted, end_attributes={"evicted": True, "reason": "session buffer full"}
        )
        self._export_spans(spans)

    def _start_run(
        self,
        session_id: str,
        *,
        extra_attributes: dict[str, Any] | None = None,
        model: Any = None,
    ) -> _Run:
        if len(self._runs) >= MAX_BUFFERED_RUNS:
            self._evict_oldest_run()
        trace_id = new_trace_id()
        root = self._make_span(
            name=f"agent.run hermes:{session_id}",
            trace_id=trace_id,
            parent_span_id=None,
            span_kind=SPAN_INTERNAL,
            attributes={
                "agent.telemetry.schema_version": "0.1.0",
                "deployment.environment": self.environment,
                "service.name": self.service_name,
                "tenant.id": self.tenant_id,
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "hermes",
                "session.id": session_id,
                "gen_ai.request.model": model or "",
                **(extra_attributes or {}),
            },
        )
        run = _Run(session_id=session_id, trace_id=trace_id, root=root, spans=[root])
        self._runs[session_id] = run
        return run

    def _ensure_run(self, session_id: str, kwargs: dict[str, Any]) -> _Run:
        run = self._runs.get(session_id)
        if run is not None:
            return run
        return self._start_run(session_id, model=kwargs.get("model"))

    def _make_tool_span(
        self,
        run: _Run,
        kwargs: dict[str, Any],
        *,
        tool_call_id: str,
    ) -> TelemetrySpan:
        return self._make_span(
            name=f"execute_tool {kwargs.get('tool_name') or 'unknown_tool'}",
            trace_id=run.trace_id,
            parent_span_id=run.root.span_id,
            span_kind=SPAN_INTERNAL,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": kwargs.get("tool_name") or "",
                "tool.call.id": tool_call_id,
                "task.id": kwargs.get("task_id") or "",
                **self.redactor.flatten(kwargs.get("args") or {}, "tool.arguments"),
            },
        )

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
                "telemetry.collection_layer": COLLECTION_LAYER,
                **self.redactor.redact(attributes),
            },
            _redact=self.redactor.redact,
        )

    def _build_exporter(self) -> Exporter:
        if not self.enabled:
            return NoopExporter()
        endpoint = self.config.endpoint or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        if endpoint:
            headers = (
                {"Authorization": f"Bearer {self.config.token}"} if self.config.token else {}
            )
            inner: Exporter = OTLPHTTPExporter(
                endpoint, headers=headers, service_name=self.service_name
            )
            return BackgroundExporter(inner, spool=self._spool)
        if self.config.output:
            return BackgroundExporter(JSONLFileExporter(self.config.output), spool=self._spool)
        return SpoolExporter(self._spool)

    @staticmethod
    def _tool_key(kwargs: dict[str, Any]) -> str:
        return "|".join(
            [
                str(kwargs.get("session_id") or ""),
                str(kwargs.get("task_id") or ""),
                str(kwargs.get("tool_call_id") or ""),
                str(kwargs.get("tool_name") or ""),
            ]
        )


def _usage(kwargs: dict[str, Any], key: str) -> int:
    usage = kwargs.get("usage")
    if isinstance(usage, dict):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return 0


_PLUGIN: HermesTelemetryPlugin | None = None


def register(ctx: Any) -> None:
    """Hermes plugin entrypoint. No-op when telemetry is disabled; never raises."""
    global _PLUGIN
    try:
        if not _IMPORT_OK:
            return  # vendored package missing/corrupt: stay silent
        if _PLUGIN is None:
            _PLUGIN = HermesTelemetryPlugin()
        _PLUGIN.register(ctx)
    except Exception:
        return
