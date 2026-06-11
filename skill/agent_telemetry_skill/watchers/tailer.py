"""Incremental multi-file tailer with persisted byte offsets.

Discovers files via glob patterns, reads only new complete lines since the
last poll, and persists per-file byte offsets to
``<state_dir>/watch_offsets.json`` (atomic replace) so restarts never replay
already-seen lines. File identity (``st_dev:st_ino``) is persisted to
``<state_dir>/watch_identity.json``: truncation, or a same-name replacement
(rotation), resets that file's offset to zero. Each poll re-reads the state
from disk and holds a short-lived exclusive lock file, so concurrent watcher
processes sharing one state directory never clobber each other's progress.
All operations are best-effort: errors are logged and never propagate out of
the poll loop.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import IO, Callable, Iterable

from ..config import load_config, state_dir as default_state_dir


STATE_FILE_NAME = "watch_offsets.json"
IDENTITY_FILE_NAME = "watch_identity.json"
LOCK_FILE_NAME = "watch_offsets.lock"
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_MAX_READ_BYTES = 8 * 1024 * 1024
_MAX_CYCLE_BYTES = 64 * 1024 * 1024
_SKIP_SCAN_CHUNK_BYTES = 65536
# A lock file older than this belongs to a dead watcher and is broken.
_LOCK_STALE_SECONDS = 300.0

logger = logging.getLogger(__name__)

PollBatch = list[tuple[str, str]]


class Tailer:
    """Tails every file matched by ``patterns``, yielding new complete lines.

    A partial trailing line (no newline yet) is held back: the offset only
    advances past fully terminated lines, so a writer mid-append never
    produces a torn record. A single line larger than the read window is
    dropped (with a warning) so one oversized record can never stall the
    rest of the file.
    """

    def __init__(
        self,
        patterns: Iterable[str],
        *,
        state_dir: str | Path | None = None,
    ):
        self.patterns = [str(pattern) for pattern in patterns]
        self._state_dir = (
            Path(state_dir) if state_dir is not None else default_state_dir(load_config())
        )

    @property
    def state_path(self) -> Path:
        return self._state_dir / STATE_FILE_NAME

    @property
    def identity_path(self) -> Path:
        return self._state_dir / IDENTITY_FILE_NAME

    @property
    def lock_path(self) -> Path:
        return self._state_dir / LOCK_FILE_NAME

    def discover(self) -> list[Path]:
        """Return the sorted set of files currently matching the patterns.

        ``recursive=True`` so ``**`` patterns expand across directory levels;
        non-``**`` patterns are unaffected.
        """
        found: set[Path] = set()
        for pattern in self.patterns:
            try:
                expanded = os.path.expanduser(pattern)
                found.update(Path(match) for match in glob.glob(expanded, recursive=True))
            except Exception:
                logger.warning("tailer: discover failed for pattern %r", pattern, exc_info=True)
        return sorted(path for path in found if path.is_file())

    def offsets(self) -> dict[str, int]:
        """Return a copy of the persisted per-file byte offsets."""
        return dict(self._load_offsets())

    def poll_once(
        self,
        on_batch: Callable[[PollBatch], object] | None = None,
    ) -> PollBatch:
        """Read new complete lines from every discovered file.

        Returns ``(source_path, line)`` tuples in file order. Never raises.

        When ``on_batch`` is given it is called with the batch BEFORE the
        advanced offsets are persisted; offsets are only committed when it
        returns anything but False and does not raise, so a failed durable
        write replays the same lines on the next poll instead of losing them.
        """
        batch: PollBatch = []
        if not self._acquire_lock():
            return batch  # another watcher holds the state; retry next poll
        try:
            offsets = self._load_offsets()
            identities = self._load_identities()
            updated_offsets = dict(offsets)
            updated_identities = dict(identities)
            budget = _MAX_CYCLE_BYTES
            for path in self.discover():
                if budget <= 0:
                    break  # leave remaining files for the next cycle
                key = str(path)
                offset = offsets.get(key, 0)
                identity = _file_identity(path)
                if identity is not None and identities.get(key) not in (None, identity):
                    offset = 0  # same-name replacement: new file, fresh start
                lines, new_offset = self._read_new_lines(path, offset)
                budget -= max(0, new_offset - offset)
                batch.extend((key, line) for line in lines)
                updated_offsets[key] = new_offset
                if identity is not None:
                    updated_identities[key] = identity
            commit = True
            if on_batch is not None:
                commit = on_batch(batch) is not False
            if commit and (updated_offsets != offsets or updated_identities != identities):
                self._save_state(updated_offsets, updated_identities)
        except Exception:
            logger.warning("tailer: poll_once failed", exc_info=True)
        finally:
            self._release_lock()
        return batch

    def run(
        self,
        callback: Callable[[PollBatch], None],
        *,
        interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        stop: threading.Event | None = None,
    ) -> None:
        """Poll forever (or until ``stop`` is set), passing batches to ``callback``.

        Exceptions from polling or the callback are logged and never escape
        the loop.
        """
        while stop is None or not stop.is_set():
            try:
                callback(self.poll_once())
            except Exception:
                logger.warning("tailer: callback failed", exc_info=True)
            if stop is not None:
                stop.wait(interval)
            else:
                time.sleep(interval)

    def _read_new_lines(self, path: Path, offset: int) -> tuple[list[str], int]:
        try:
            size = path.stat().st_size
        except OSError:
            return [], offset
        if offset > size:
            offset = 0  # truncated or rotated in place
        if size == offset:
            return [], offset
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(min(size - offset, _MAX_READ_BYTES))
                last_newline = data.rfind(b"\n")
                if last_newline < 0 and len(data) >= _MAX_READ_BYTES:
                    # One line larger than the window: drop it instead of
                    # re-reading the same window forever and stalling the file.
                    new_offset = _skip_past_next_newline(handle, offset + len(data), size)
                    logger.warning(
                        "tailer: dropped oversized line (>%d bytes) in %s",
                        _MAX_READ_BYTES,
                        path,
                    )
                    return [], new_offset
        except OSError:
            return [], offset
        if last_newline < 0:
            return [], offset  # hold the partial line until it is terminated
        complete = data[: last_newline + 1]
        text = complete.decode("utf-8", errors="replace")
        lines = [line for line in text.splitlines() if line.strip()]
        return lines, offset + len(complete)

    def _load_offsets(self) -> dict[str, int]:
        loaded: dict[str, int] = {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                loaded = {
                    str(key): int(value)
                    for key, value in data.items()
                    if isinstance(value, (int, float))
                }
        except Exception:
            loaded = {}
        return loaded

    def _load_identities(self) -> dict[str, str]:
        loaded: dict[str, str] = {}
        try:
            data = json.loads(self.identity_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                loaded = {
                    str(key): str(value)
                    for key, value in data.items()
                    if isinstance(value, str)
                }
        except Exception:
            loaded = {}
        return loaded

    def _save_state(self, offsets: dict[str, int], identities: dict[str, str]) -> None:
        # Prune entries for files that vanished; a recreated file restarts at 0.
        pruned = {key: value for key, value in offsets.items() if os.path.isfile(key)}
        pruned_identities = {key: value for key, value in identities.items() if key in pruned}
        try:
            self._atomic_write(
                self.state_path, json.dumps(pruned, ensure_ascii=False, sort_keys=True)
            )
            self._atomic_write(
                self.identity_path,
                json.dumps(pruned_identities, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            logger.warning("tailer: failed to persist offsets", exc_info=True)

    def _acquire_lock(self) -> bool:
        """Take the shared state lock; never blocks and never raises."""
        try:
            self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            for _ in range(2):
                try:
                    fd = os.open(
                        str(self.lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    os.close(fd)
                    return True
                except FileExistsError:
                    try:
                        age = time.time() - self.lock_path.stat().st_mtime
                    except OSError:
                        continue  # holder just released; retry once
                    if age > _LOCK_STALE_SECONDS:
                        self.lock_path.unlink(missing_ok=True)
                        continue  # stale lock from a dead watcher: break it
                    return False
            return False
        except Exception:
            return True  # cannot lock at all: proceed rather than stall telemetry

    def _release_lock(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except Exception:
            return

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)


def _file_identity(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_dev}:{stat.st_ino}"


def _skip_past_next_newline(handle: IO[bytes], position: int, size: int) -> int:
    """Scan forward from ``position`` to just past the next newline (or EOF)."""
    handle.seek(position)
    current = position
    while current < size:
        chunk = handle.read(min(_SKIP_SCAN_CHUNK_BYTES, size - current))
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            return current + newline + 1
        current += len(chunk)
    return current
