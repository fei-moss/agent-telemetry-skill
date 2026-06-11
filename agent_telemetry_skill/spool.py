from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Iterable

from .config import load_config, spool_dir
from .schema import TelemetrySpan


MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_SPOOL_BYTES = 256 * 1024 * 1024
PENDING_GLOB = "pending-*.jsonl"
DRAINING_SUFFIX = ".draining"
DRAINING_GLOB = "*" + DRAINING_SUFFIX
# A .draining file older than this is treated as an orphan from a crashed
# drainer and re-queued; long enough that a live drainer is never robbed.
STALE_CLAIM_SECONDS = 60.0
_READ_CHUNK_BYTES = 65536


class Spool:
    """Durable on-disk JSONL buffer for telemetry spans.

    Safe for many concurrent short-lived processes: every process appends to
    its own pending-<pid>-<counter>.jsonl file with a single O_APPEND write
    per call, and drainers claim whole files atomically via os.rename.
    Claims orphaned by a crashed drainer are reclaimed after
    STALE_CLAIM_SECONDS, and the directory is capped at ``max_bytes``
    (oldest pending files dropped first). All operations are best-effort and
    never raise.
    """

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_SPOOL_BYTES,
    ):
        if directory is None:
            directory = spool_dir(load_config())
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self._counter = 0
        self._lock = threading.Lock()

    def append(self, spans: Iterable[TelemetrySpan] | Iterable[dict[str, Any]]) -> bool:
        """Append spans; returns False when nothing was durably written."""
        try:
            encoded = [_encode_span(span) for span in spans]
            lines = [line for line in encoded if line is not None]
            if not lines:
                return True
            data = ("\n".join(lines) + "\n").encode("utf-8")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._lock:
                path = self._writable_path()
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
            self._enforce_size_cap()
            return True
        except Exception:
            return False

    def depth(self) -> int:
        total = 0
        try:
            for pattern in (PENDING_GLOB, DRAINING_GLOB):
                for path in self.directory.glob(pattern):
                    total += _count_lines(path)
        except Exception:
            return total
        return total

    def size_bytes(self) -> int:
        total = 0
        try:
            for pattern in (PENDING_GLOB, DRAINING_GLOB):
                for path in self.directory.glob(pattern):
                    try:
                        total += path.stat().st_size
                    except OSError:
                        continue
        except Exception:
            return total
        return total

    def drain(
        self,
        exporter: Any,
        batch_size: int = 100,
        max_batches: int | None = None,
    ) -> int:
        exported_total = 0
        batches_done = 0
        self._reclaim_stale_claims()
        try:
            files = sorted(self.directory.glob(PENDING_GLOB))
        except Exception:
            return 0
        for path in files:
            if max_batches is not None and batches_done >= max_batches:
                break
            claimed = path.with_name(path.name + DRAINING_SUFFIX)
            try:
                os.rename(path, claimed)
            except OSError:
                continue  # another drainer won this file
            spans: list[TelemetrySpan] = []
            index = 0
            try:
                spans = _read_spans(claimed)
                while index < len(spans):
                    if max_batches is not None and batches_done >= max_batches:
                        break
                    batch = spans[index : index + max(1, batch_size)]
                    try:
                        exporter.export(batch)
                    except Exception:
                        break
                    exported_total += len(batch)
                    index += len(batch)
                    batches_done += 1
            finally:
                # Always release the claim, even if reading/export raised.
                self._finalize(claimed, spans[index:])
        return exported_total

    def _writable_path(self) -> Path:
        pid = os.getpid()
        while True:
            path = self.directory / f"pending-{pid}-{self._counter}.jsonl"
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    self._counter += 1
                    continue
            except OSError:
                pass
            return path

    def _finalize(self, claimed: Path, remainder: list[TelemetrySpan]) -> None:
        try:
            if not remainder:
                claimed.unlink(missing_ok=True)
                return
            # Requeue-before-delete: write the remainder to a fresh pending
            # file FIRST, so a crash in between duplicates instead of losing.
            encoded = [_encode_span(span) for span in remainder]
            payload = "\n".join(line for line in encoded if line is not None) + "\n"
            requeued = self.directory / f"pending-{os.getpid()}-{time.time_ns()}.jsonl"
            fd = os.open(str(requeued), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            claimed.unlink(missing_ok=True)
        except Exception:
            return

    def _reclaim_stale_claims(self) -> None:
        """Requeue .draining files orphaned by a crashed drainer."""
        try:
            now = time.time()
            for claimed in self.directory.glob(DRAINING_GLOB):
                try:
                    if now - claimed.stat().st_mtime < STALE_CLAIM_SECONDS:
                        continue  # plausibly owned by a live drainer
                    requeued = (
                        self.directory / f"pending-{os.getpid()}-{time.time_ns()}.jsonl"
                    )
                    os.rename(claimed, requeued)
                except OSError:
                    continue  # raced with another reclaimer
        except Exception:
            return

    def _enforce_size_cap(self) -> None:
        """Drop the oldest pending files when the spool exceeds max_bytes."""
        try:
            entries: list[tuple[float, int, Path]] = []
            for path in self.directory.glob(PENDING_GLOB):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((stat.st_mtime, stat.st_size, path))
            total = sum(size for _, size, _ in entries)
            if total <= self.max_bytes:
                return
            for _, size, path in sorted(entries):
                if total <= self.max_bytes:
                    break
                try:
                    path.unlink()
                    total -= size
                except OSError:
                    continue
        except Exception:
            return


def _encode_span(span: TelemetrySpan | dict[str, Any]) -> str | None:
    try:
        payload = span.to_dict() if isinstance(span, TelemetrySpan) else dict(span)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        return None


def _read_spans(path: Path) -> list[TelemetrySpan]:
    spans: list[TelemetrySpan] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        spans.append(TelemetrySpan.from_dict(data))
                except Exception:
                    continue  # skip poisoned records, keep healthy neighbors
    except Exception:
        return spans
    return spans


def _count_lines(path: Path) -> int:
    total = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_READ_CHUNK_BYTES), b""):
                total += chunk.count(b"\n")
    except OSError:
        return 0
    return total
