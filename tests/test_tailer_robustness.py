"""Regression tests for tailer oversized lines, rotation identity, locking,
and offset-commit-after-durable-write semantics."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from agent_telemetry_skill.watchers import tailer as tailer_module
from agent_telemetry_skill.watchers.tailer import Tailer


class TailerTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.state_dir = self.tmp_path / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, name: str, text: str) -> Path:
        path = self.tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path


class OversizedLineTests(TailerTestBase):
    def setUp(self):
        super().setUp()
        self._saved_max = tailer_module._MAX_READ_BYTES
        tailer_module._MAX_READ_BYTES = 64

    def tearDown(self):
        tailer_module._MAX_READ_BYTES = self._saved_max
        super().tearDown()

    def test_giant_line_is_skipped_and_file_keeps_flowing(self):
        path = self._file("log.jsonl", "g" * 200 + "\n" + "ok\n")
        watcher = Tailer([str(path)], state_dir=self.state_dir)

        first = watcher.poll_once()
        second = watcher.poll_once()

        self.assertEqual(first, [])  # oversized line dropped, not delivered
        self.assertEqual(second, [(str(path), "ok")])

    def test_unterminated_giant_line_advances_past_eof(self):
        path = self._file("log.jsonl", "g" * 200)  # no newline at all
        watcher = Tailer([str(path)], state_dir=self.state_dir)

        self.assertEqual(watcher.poll_once(), [])
        # The offset moved to EOF: the poll loop no longer rereads the window.
        self.assertEqual(watcher.offsets()[str(path)], 200)


class RotationIdentityTests(TailerTestBase):
    def test_same_size_replacement_resets_offset(self):
        path = self._file("rotated.log", "one\n")
        watcher = Tailer([str(path)], state_dir=self.state_dir)
        self.assertEqual(watcher.poll_once(), [(str(path), "one")])

        replacement = self._file("rotated.log.new", "two\n")  # same byte size
        os.replace(replacement, path)  # new inode, size >= old offset

        self.assertEqual(watcher.poll_once(), [(str(path), "two")])


class OffsetCommitTests(TailerTestBase):
    def test_failed_batch_handler_replays_lines_next_poll(self):
        path = self._file("spooled.log", "a\nb\n")
        watcher = Tailer([str(path)], state_dir=self.state_dir)

        first = watcher.poll_once(on_batch=lambda batch: False)  # durable write failed
        replay = watcher.poll_once(on_batch=lambda batch: True)
        done = watcher.poll_once()

        self.assertEqual(first, [(str(path), "a"), (str(path), "b")])
        self.assertEqual(replay, first)  # nothing was lost
        self.assertEqual(done, [])

    def test_raising_batch_handler_also_replays(self):
        path = self._file("spooled.log", "a\n")
        watcher = Tailer([str(path)], state_dir=self.state_dir)

        def boom(batch):
            raise RuntimeError("disk full")

        watcher.poll_once(on_batch=boom)  # must not raise

        self.assertEqual(watcher.poll_once(), [(str(path), "a")])


class SharedStateTests(TailerTestBase):
    def test_concurrent_watchers_do_not_clobber_each_others_progress(self):
        file_a = self._file("a.log", "a1\n")
        file_b = self._file("b.log", "b1\n")
        watcher_a = Tailer([str(file_a)], state_dir=self.state_dir)
        watcher_b = Tailer([str(file_b)], state_dir=self.state_dir)

        watcher_a.poll_once()
        watcher_b.poll_once()  # advances b.log after A persisted its state
        with file_a.open("a", encoding="utf-8") as handle:
            handle.write("a2\n")
        watcher_a.poll_once()  # A re-reads fresh state; must keep B's offset

        offsets = json.loads(
            (self.state_dir / "watch_offsets.json").read_text(encoding="utf-8")
        )
        self.assertEqual(offsets[str(file_b)], file_b.stat().st_size)
        self.assertEqual(offsets[str(file_a)], file_a.stat().st_size)
        self.assertEqual(watcher_b.poll_once(), [])  # nothing replayed

    def test_live_lock_skips_poll_and_stale_lock_is_broken(self):
        path = self._file("locked.log", "x\n")
        watcher = Tailer([str(path)], state_dir=self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock = watcher.lock_path
        lock.write_text("", encoding="utf-8")

        self.assertEqual(watcher.poll_once(), [])  # live lock: skip this poll

        stale = time.time() - 3600
        os.utime(lock, (stale, stale))
        self.assertEqual(watcher.poll_once(), [(str(path), "x")])
        self.assertFalse(lock.exists())  # released after the poll


if __name__ == "__main__":
    unittest.main()
