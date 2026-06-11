"""Session-log watchers: the ``log_watch`` collection layer.

Tail runtime transcript files (Claude Code project transcripts, Codex CLI
rollout files) and convert their entries into telemetry spans that join the
same per-session trace used by hooks and CLI emissions.

Dedup guard: every span produced here carries ``telemetry.source.file`` so
the backend can deduplicate if hooks AND the watcher both run. Installers
should enable only ONE collection layer per runtime — if hook-based capture
is installed for a runtime, do not also run its log watcher.
"""

from .claude_code import ClaudeCodeParser
from .codex import CodexParser
from .hermes import HermesSessionParser
from .tailer import Tailer

__all__ = [
    "ClaudeCodeParser",
    "CodexParser",
    "HermesSessionParser",
    "Tailer",
]
