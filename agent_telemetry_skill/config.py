from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any


DEFAULT_SERVICE = "local-agent"
DEFAULT_TENANT = "local-dev"
DEFAULT_ENVIRONMENT = "local"
HOME_DIR_NAME = ".agent-telemetry"
CONFIG_FILE_NAME = "config.json"

_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


def default_home() -> Path:
    return Path.home() / HOME_DIR_NAME


@dataclass(frozen=True)
class TelemetryConfig:
    endpoint: str | None = None
    token: str | None = None
    service: str = DEFAULT_SERVICE
    tenant: str = DEFAULT_TENANT
    environment: str = DEFAULT_ENVIRONMENT
    capture_content: bool = True
    output: str | None = None
    home: Path = field(default_factory=default_home)
    enabled: bool = True
    # Rich human-display capture is ON by default: the whole point of this skill
    # is to report assistant thinking + message/progress + tool content to a
    # frontend timeline. capture_narrative emits thinking/message spans;
    # max_content_chars caps any captured string (high, for full reasoning).
    # disable_redaction turns the redactor into a passthrough — RAW content, no
    # secret scrubbing (still OFF by default: secrets stay scrubbed).
    capture_narrative: bool = True
    max_content_chars: int = 4000
    disable_redaction: bool = False


def load_config() -> TelemetryConfig:
    """Resolve configuration: env var > ~/.agent-telemetry/config.json > default."""
    file_values = _read_config_file(default_home() / CONFIG_FILE_NAME)
    home_value = _resolve_str("AGENT_TELEMETRY_HOME", file_values, "home", None)
    return TelemetryConfig(
        endpoint=_resolve_str("AGENT_TELEMETRY_ENDPOINT", file_values, "endpoint", None),
        token=_resolve_str("AGENT_TELEMETRY_TOKEN", file_values, "token", None),
        service=_resolve_str("AGENT_TELEMETRY_SERVICE", file_values, "service", DEFAULT_SERVICE)
        or DEFAULT_SERVICE,
        tenant=_resolve_str("AGENT_TELEMETRY_TENANT", file_values, "tenant", DEFAULT_TENANT)
        or DEFAULT_TENANT,
        environment=_resolve_str(
            "AGENT_TELEMETRY_ENVIRONMENT", file_values, "environment", DEFAULT_ENVIRONMENT
        )
        or DEFAULT_ENVIRONMENT,
        capture_content=_resolve_bool(
            "AGENT_TELEMETRY_CAPTURE_CONTENT", file_values, "capture_content", True
        ),
        output=_resolve_str("AGENT_TELEMETRY_OUTPUT", file_values, "output", None),
        home=Path(home_value).expanduser() if home_value else default_home(),
        enabled=_resolve_bool("AGENT_TELEMETRY_ENABLED", file_values, "enabled", True),
        capture_narrative=_resolve_bool(
            "AGENT_TELEMETRY_CAPTURE_NARRATIVE", file_values, "capture_narrative", True
        ),
        max_content_chars=_resolve_int(
            "AGENT_TELEMETRY_MAX_CONTENT_CHARS", file_values, "max_content_chars", 4000
        ),
        disable_redaction=_resolve_bool(
            "AGENT_TELEMETRY_DISABLE_REDACTION", file_values, "disable_redaction", False
        ),
    )


def spool_dir(config: TelemetryConfig) -> Path:
    return config.home / "spool"


def state_dir(config: TelemetryConfig) -> Path:
    return config.home / "state"


def local_spans_path(config: TelemetryConfig) -> Path:
    """Default local-only sink when neither endpoint nor output is set."""
    return config.home / "local-spans.jsonl"


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_str(
    env_name: str,
    file_values: dict[str, Any],
    key: str,
    default: str | None,
) -> str | None:
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value
    file_value = file_values.get(key)
    if isinstance(file_value, str) and file_value:
        return file_value
    return default


def _resolve_bool(
    env_name: str,
    file_values: dict[str, Any],
    key: str,
    default: bool,
) -> bool:
    env_value = _parse_bool(os.environ.get(env_name))
    if env_value is not None:
        return env_value
    file_value = _parse_bool(file_values.get(key))
    if file_value is not None:
        return file_value
    return default


def _resolve_int(
    env_name: str,
    file_values: dict[str, Any],
    key: str,
    default: int,
) -> int:
    raw = os.environ.get(env_name)
    if raw is None:
        raw = file_values.get(key)
    try:
        if raw is not None and not isinstance(raw, bool):
            return int(raw)
    except (TypeError, ValueError):
        pass
    return default


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    return None
