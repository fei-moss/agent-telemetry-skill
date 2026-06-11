from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any


DEFAULT_SENSITIVE_KEYS = (
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "bearer_token",
    "cookie",
    "csrf_token",
    "id_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session_cookie",
    "session_token",
)

DEFAULT_CONTENT_KEYS = (
    "completion",
    "content",
    "input",
    "message",
    "output",
    "prompt",
    "query",
    "response",
    "result",
    "text",
)

DEFAULT_SECRET_PATTERNS = (
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    # AWS access key ids (long-lived, temporary, and service-specific).
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
    # GitHub tokens: classic PATs, OAuth, app/user/server/refresh tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # Slack tokens (app, bot, personal, refresh, signing).
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    # Google API keys.
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    # PEM private key blocks (or a bare BEGIN header on a truncated value).
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
        r"(?:[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----)?"
    ),
)

# Inline credentials embedded in command lines / connection strings. Unlike the
# token-shape patterns above, these match a flag (or URL prefix) that introduces
# a secret value of arbitrary shape. Each rule keeps the non-secret prefix via a
# backreference and replaces only the value, so the command stays legible while
# the credential is scrubbed. The value alternation matches a single-quoted,
# double-quoted, or bare (whitespace-delimited) argument.
_VALUE = r"""(?:'[^']*'|"[^"]*"|\S+)"""
DEFAULT_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # sshpass -p <password>
    (re.compile(r"(?i)(sshpass\s+-p\s*)" + _VALUE), r"\1[REDACTED]"),
    # Long credential flags: --password, --token, --api-key, --secret, --access-key, ...
    (
        re.compile(
            r"(?i)(--(?:password|passwd|pwd|token|api[-_]?key|apikey|secret"
            r"|access[-_]?key|secret[-_]?key|auth[-_]?token|client[-_]?secret)"
            r"(?:[=\s]+))" + _VALUE
        ),
        r"\1[REDACTED]",
    ),
    # Attached short password flags: mysql -pSECRET, psql -p'secret' (no space).
    (re.compile(r"(?i)(\s-p)(?:'[^']+'|\"[^\"]+\"|[^\s'\"]{1,128})(?=\s|$)"), r"\1[REDACTED]"),
    # Basic-auth flag: curl -u user:pass
    (re.compile(r"(?i)(\s-u\s+)" + _VALUE), r"\1[REDACTED]"),
    # URL userinfo: scheme://user:pass@host
    (
        re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+:[^/\s:@]+@"),
        r"\1[REDACTED]@",
    ),
)


@dataclass(frozen=True)
class RedactionConfig:
    capture_content: bool = False
    max_string_length: int = 500
    sensitive_keys: tuple[str, ...] = DEFAULT_SENSITIVE_KEYS
    content_keys: tuple[str, ...] = DEFAULT_CONTENT_KEYS
    secret_patterns: tuple[re.Pattern[str], ...] = field(default_factory=lambda: DEFAULT_SECRET_PATTERNS)
    credential_patterns: tuple[tuple[re.Pattern[str], str], ...] = field(
        default_factory=lambda: DEFAULT_CREDENTIAL_PATTERNS
    )


class Redactor:
    def __init__(self, config: RedactionConfig | None = None):
        self.config = config or RedactionConfig()
        self._sensitive_keys = tuple(key.lower() for key in self.config.sensitive_keys)
        self._content_keys = tuple(key.lower() for key in self.config.content_keys)

    def redact(self, value: Any, key_path: tuple[str, ...] = ()) -> Any:
        raw_key = key_path[-1].lower() if key_path else ""
        key = _normalize_key(raw_key)
        if key and self._is_sensitive_key(key):
            return "[REDACTED]"

        if isinstance(value, dict):
            return {
                str(item_key): self.redact(item_value, key_path + (str(item_key),))
                for item_key, item_value in value.items()
            }

        if isinstance(value, (list, tuple)):
            return [self.redact(item, key_path) for item in value]

        if isinstance(value, str):
            cleaned = self._redact_patterns(value)
            # Content gating considers every ancestor key, not just the leaf:
            # any string nested under e.g. "tool.result" (stdout, stderr, file
            # contents, ...) is content even when its own key name is not.
            if self._is_content_path(key_path) and not self.config.capture_content:
                return {
                    "content_omitted": True,
                    "char_count": len(cleaned),
                }
            return self._truncate(cleaned)

        return value

    def flatten(self, value: Any, prefix: str) -> dict[str, Any]:
        redacted = self.redact(value, (prefix,))
        return _flatten(redacted, prefix)

    def _is_content_path(self, key_path: tuple[str, ...]) -> bool:
        for raw_key in key_path:
            normalized = _normalize_key(str(raw_key).lower())
            leaf = normalized.split("_")[-1] if normalized else ""
            if normalized in self._content_keys or leaf in self._content_keys:
                return True
        return False

    def _is_sensitive_key(self, key: str) -> bool:
        normalized = _normalize_key(key)
        parts = [part for part in normalized.split("_") if part]

        if normalized in self._sensitive_keys:
            return True
        if "authorization" in parts or "password" in parts or "cookie" in parts:
            return True
        if "secret" in parts:
            return True
        if "private" in parts and "key" in parts:
            return True
        if "api" in parts and "key" in parts:
            return True
        if "apikey" in parts:
            return True
        if "token" in parts:
            token_prefixes = {"access", "auth", "bearer", "csrf", "id", "refresh", "session"}
            return any(prefix in parts for prefix in token_prefixes) or normalized == "token"
        return False

    def _redact_patterns(self, value: str) -> str:
        redacted = value
        for pattern, replacement in self.config.credential_patterns:
            redacted = pattern.sub(replacement, redacted)
        for pattern in self.config.secret_patterns:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    def _truncate(self, value: str) -> str:
        if len(value) <= self.config.max_string_length:
            return value
        return value[: self.config.max_string_length] + "...[TRUNCATED]"


def _flatten(value: Any, prefix: str) -> dict[str, Any]:
    if isinstance(value, dict):
        flattened: dict[str, Any] = {}
        for key, item in value.items():
            child_key = f"{prefix}.{key}"
            if isinstance(item, dict) and _is_primitive_dict(item):
                flattened[child_key] = item
            elif isinstance(item, dict):
                flattened.update(_flatten(item, child_key))
            elif isinstance(item, list):
                flattened[child_key] = json.dumps(item, ensure_ascii=False, sort_keys=True)
            else:
                flattened[child_key] = item
        return flattened

    return {prefix: value}


def _is_primitive_dict(value: dict[str, Any]) -> bool:
    return all(not isinstance(item, (dict, list, tuple)) for item in value.values())


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
