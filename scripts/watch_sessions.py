#!/usr/bin/env python3
"""Universal session-log watcher entrypoint (``log_watch`` collection layer).

Tails Claude Code transcripts and/or Codex rollout files, converts new lines
into telemetry spans, and writes them to the on-disk spool (chosen over
BackgroundExporter: this process is already the background worker, so
spool-then-drain keeps the data durable with zero extra threads). File
offsets are committed only after the spool write succeeds.
After each cycle it opportunistically drains the spool to the configured
endpoint/output; on failure data simply stays spooled.

Safe as a long-lived background process: every cycle is wrapped in
catch-all error handling and AGENT_TELEMETRY_ENABLED=0 turns the script
into a no-op. Installers should enable only ONE collection layer per
runtime (hooks OR this watcher) — spans carry telemetry.source.file so the
backend can dedup if both run anyway.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import sys
import time

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agent_telemetry_skill.config import (  # noqa: E402
    TelemetryConfig,
    load_config,
    spool_dir,
    state_dir,
)
from agent_telemetry_skill.exporters import (  # noqa: E402
    Exporter,
    JSONLFileExporter,
    OTLPHTTPExporter,
)
from agent_telemetry_skill.spool import Spool  # noqa: E402
from agent_telemetry_skill.watchers import claude_code, codex, hermes  # noqa: E402
from agent_telemetry_skill.watchers.tailer import PollBatch, Tailer  # noqa: E402


RUNTIME_CLAUDE = "claude-code"
RUNTIME_CODEX = "codex"
RUNTIME_HERMES = "hermes"
RUNTIME_ALL = "all"
DRAIN_TIMEOUT_SECONDS = 5.0
DRAIN_MAX_BATCHES = 10

FeedTarget = tuple[str, object]  # (expanded glob pattern, parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watch-sessions",
        description="Tail agent session logs and emit telemetry spans.",
    )
    parser.add_argument(
        "--runtime",
        choices=(RUNTIME_CLAUDE, RUNTIME_CODEX, RUNTIME_HERMES, RUNTIME_ALL),
        default=RUNTIME_ALL,
    )
    parser.add_argument("--once", action="store_true", help="single poll then exit")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--claude-glob", default=claude_code.DEFAULT_GLOB)
    parser.add_argument("--codex-glob", default=codex.DEFAULT_GLOB)
    parser.add_argument("--hermes-glob", default=hermes.DEFAULT_GLOB)
    parser.add_argument(
        "--status", action="store_true", help="print offsets and spool depth, then exit"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # telemetry must never fail the caller
        print(f"watch-sessions: warning: {exc}", file=sys.stderr)
        return 0


def _run(args: argparse.Namespace) -> int:
    config = load_config()
    patterns = _patterns_for(args)
    tailer = Tailer(patterns, state_dir=state_dir(config))
    if args.status:
        return _print_status(config, tailer)
    if not config.enabled:
        print("watch-sessions: disabled via AGENT_TELEMETRY_ENABLED; no-op", file=sys.stderr)
        return 0
    targets = _build_targets(args)
    spool = Spool(spool_dir(config))

    def handle(batch: PollBatch) -> bool:
        # Returned to Tailer.poll_once: offsets are only committed after the
        # spans are durably spooled, so a failed write replays, never loses.
        spooled = _process_batch(batch, targets, spool)
        _opportunistic_drain(config)
        return spooled

    if args.once:
        tailer.poll_once(on_batch=handle)
        return 0
    while True:
        try:
            tailer.poll_once(on_batch=handle)
        except Exception as exc:
            print(f"watch-sessions: warning: cycle failed: {exc}", file=sys.stderr)
        time.sleep(max(0.1, args.interval))


def _patterns_for(args: argparse.Namespace) -> list[str]:
    patterns: list[str] = []
    if args.runtime in (RUNTIME_CLAUDE, RUNTIME_ALL):
        patterns.append(args.claude_glob)
    if args.runtime in (RUNTIME_CODEX, RUNTIME_ALL):
        patterns.append(args.codex_glob)
    if args.runtime in (RUNTIME_HERMES, RUNTIME_ALL):
        patterns.append(args.hermes_glob)
    return patterns


def _build_targets(args: argparse.Namespace) -> list[FeedTarget]:
    targets: list[FeedTarget] = []
    if args.runtime in (RUNTIME_CLAUDE, RUNTIME_ALL):
        targets.append(
            (os.path.expanduser(args.claude_glob), claude_code.ClaudeCodeParser())
        )
    if args.runtime in (RUNTIME_CODEX, RUNTIME_ALL):
        targets.append((os.path.expanduser(args.codex_glob), codex.CodexParser()))
    if args.runtime in (RUNTIME_HERMES, RUNTIME_ALL):
        targets.append((os.path.expanduser(args.hermes_glob), hermes.HermesSessionParser()))
    return targets


def _process_batch(
    batch: PollBatch,
    targets: list[FeedTarget],
    spool: Spool,
) -> bool:
    """Spool spans parsed from ``batch``; False when the write failed."""
    spans = []
    for source_path, line in batch:
        for pattern, parser in targets:
            if fnmatch.fnmatch(source_path, pattern):
                spans.extend(parser.feed(line, source_path))  # type: ignore[attr-defined]
                break
    if spans:
        return spool.append(spans)
    return True


def _print_status(config: TelemetryConfig, tailer: Tailer) -> int:
    payload = {
        "enabled": config.enabled,
        "state_file": str(tailer.state_path),
        "offsets": tailer.offsets(),
        "spool_depth": Spool(spool_dir(config)).depth(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _inner_exporter(config: TelemetryConfig) -> Exporter | None:
    if config.endpoint:
        headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
        return OTLPHTTPExporter(
            config.endpoint,
            headers=headers,
            service_name=config.service,
            timeout_seconds=DRAIN_TIMEOUT_SECONDS,
        )
    if config.output:
        return JSONLFileExporter(config.output)
    return None


def _opportunistic_drain(config: TelemetryConfig) -> int:
    """Best-effort drain after spooling; on failure data stays spooled."""
    try:
        inner = _inner_exporter(config)
        if inner is None:
            return 0
        return Spool(spool_dir(config)).drain(inner, max_batches=DRAIN_MAX_BATCHES)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
