"""Hardened telemetry ingest server for public deployment.

Unlike ``minimal_ingest.py`` (a dependency-free dev sink), this server is meant
to sit on a public interface, so it enforces the safety floor a public endpoint
needs: mandatory Bearer-token auth, a request-body size cap, a health endpoint,
and per-day output rotation. It never stores tokens in plaintext (only a sha256
fingerprint prefix) and never crashes the listener on a single bad request.

It is still a reference sink, not a full backend: for production you should put
it behind TLS (a reverse proxy) and map tokens to tenants server-side. See
docs/PROTOCOL.md and 使用说明.md.

Config via env (systemd EnvironmentFile) or flags (flags win):
    INGEST_TOKENS           comma-separated accepted Bearer tokens (required
                            unless --allow-anonymous)
    INGEST_HOST             default 0.0.0.0
    INGEST_PORT             default 4318
    INGEST_OUTPUT_DIR       default ./ingest-data
    INGEST_MAX_BODY_BYTES   default 5242880 (5 MiB)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import time


TRACE_PATHS = ("/v1/traces", "/ingest")
DEFAULT_MAX_BODY_BYTES = 5 * 1024 * 1024


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class IngestConfig:
    tokens: tuple[str, ...] = ()
    allow_anonymous: bool = False
    output_dir: Path = Path("ingest-data")
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES


class IngestHandler(BaseHTTPRequestHandler):
    config: IngestConfig

    server_version = "agent-telemetry-ingest/1.0"

    def _reply(self, code: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> tuple[bool, str]:
        """Return (ok, token_fingerprint)."""
        if self.config.allow_anonymous:
            return True, "anonymous"
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False, ""
        presented = header[7:].strip()
        for accepted in self.config.tokens:
            if hmac.compare_digest(presented, accepted):
                return True, _token_fingerprint(presented)
        return False, ""

    def do_GET(self) -> None:
        if self.path in ("/healthz", "/health"):
            self._reply(200, b'{"status":"ok"}', "application/json")
            return
        self._reply(404, b"not found")

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except Exception:  # never let one request kill the listener
            try:
                self._reply(500, b"internal error")
            except Exception:
                pass

    def _handle_post(self) -> None:
        if self.path not in TRACE_PATHS:
            self._reply(404, b"not found")
            return

        ok, fingerprint = self._authorized()
        if not ok:
            self._reply(401, b"unauthorized")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400, b"bad content-length")
            return
        if length > self.config.max_body_bytes:
            self._reply(413, b"payload too large")
            return

        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._reply(400, b"invalid json")
            return

        record = {
            "received_at_unix_ms": int(time.time() * 1000),
            "path": self.path,
            "client_ip": self.client_address[0] if self.client_address else "",
            "auth_token_fp": fingerprint,
            "payload": payload,
        }
        out = self.config.output_dir / f"traces-{time.strftime('%Y-%m-%d')}.jsonl"
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

        self._reply(200, b"ok")

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def build_config(args: argparse.Namespace) -> IngestConfig:
    config = IngestConfig()
    raw_tokens = args.tokens if args.tokens is not None else _env("INGEST_TOKENS", "")
    config.tokens = tuple(t.strip() for t in raw_tokens.split(",") if t.strip())
    config.allow_anonymous = args.allow_anonymous
    config.output_dir = Path(args.output_dir or _env("INGEST_OUTPUT_DIR", "ingest-data")).expanduser()
    config.max_body_bytes = args.max_body_bytes or int(_env("INGEST_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES)))
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hardened telemetry ingest server.")
    parser.add_argument("--host", default=_env("INGEST_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(_env("INGEST_PORT", "4318")))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--tokens", default=None, help="comma-separated accepted Bearer tokens")
    parser.add_argument("--max-body-bytes", type=int, default=0)
    parser.add_argument("--allow-anonymous", action="store_true", help="DANGER: disable auth")
    args = parser.parse_args(argv)

    config = build_config(args)
    if not config.tokens and not config.allow_anonymous:
        print(
            "refusing to start: no INGEST_TOKENS configured and --allow-anonymous not set.\n"
            "Set INGEST_TOKENS=<token[,token2]> (public endpoint must be authenticated).",
            file=sys.stderr,
        )
        return 2
    config.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        config.output_dir.chmod(0o700)
    except OSError:
        pass

    IngestHandler.config = config
    server = ThreadingHTTPServer((args.host, args.port), IngestHandler)
    mode = "ANONYMOUS (no auth)" if config.allow_anonymous else f"{len(config.tokens)} token(s)"
    print(
        f"ingest listening on http://{args.host}:{args.port}{TRACE_PATHS[0]} "
        f"| auth: {mode} | output: {config.output_dir} | max body: {config.max_body_bytes} bytes",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
