#!/usr/bin/env python3
"""Codex CLI notify hook for agent-telemetry.

Codex invokes the configured ``notify`` program after each agent turn with a
JSON payload appended as the final argv argument, e.g.::

    {"type": "agent-turn-complete", "thread-id": "...", "turn-id": "...", ...}

This hook spawns one detached ``scripts/watch_sessions.py --runtime codex
--once`` process (single rollout-log poll + opportunistic spool drain) and
exits 0 immediately. It must NEVER block, slow, or fail the Codex turn:
every step is wrapped in catch-all error handling, the child is fully
detached (``start_new_session``) with all stdio on devnull, and
``AGENT_TELEMETRY_ENABLED=0`` short-circuits to a silent no-op.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
WATCH_SCRIPT = REPO_ROOT / "scripts" / "watch_sessions.py"
TURN_COMPLETE_TYPE = "agent-turn-complete"

_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


def is_disabled() -> bool:
    """Cheap enabled check; full config resolution happens in the child."""
    value = os.environ.get("AGENT_TELEMETRY_ENABLED", "").strip().lower()
    return value in _FALSE_STRINGS


def should_trigger(argv: list[str]) -> bool:
    """True when the notify payload warrants a watcher poll.

    Codex appends the JSON payload as the last argument; earlier arguments
    are whatever was configured in ``notify``. Unknown or unparsable
    payloads trigger anyway — a spurious poll is harmless and cheap.
    """
    for arg in reversed(argv):
        try:
            payload = json.loads(arg)
        except ValueError:
            continue
        if isinstance(payload, dict):
            payload_type = payload.get("type")
            if isinstance(payload_type, str):
                return payload_type == TURN_COMPLETE_TYPE
        return True
    return True


def spawn_watcher() -> None:
    """Launch one detached watcher poll; stdio to devnull, never waits."""
    if not WATCH_SCRIPT.is_file():
        return
    subprocess.Popen(
        [
            sys.executable or "python3",
            str(WATCH_SCRIPT),
            "--runtime",
            "codex",
            "--once",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        cwd=str(REPO_ROOT),
    )


def main(argv: list[str] | None = None) -> int:
    """Always returns 0; telemetry must never fail the Codex turn."""
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        if is_disabled() or not should_trigger(args):
            return 0
        spawn_watcher()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
