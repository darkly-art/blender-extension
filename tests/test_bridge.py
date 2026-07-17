"""Tests for `bridge.HelperProcess` - the parent side of the subprocess.

No Blender needed. The handshake tests drive real subprocesses (tiny fake-child
scripts that emit events on stdout and idle on stdin). The single-slot, event
parsing, `stalled`, and pipe-semantics logic is unit-tested directly - including
the two Windows pipe behaviours CI's Linux can't reproduce natively (the write
retry budget, and a full `PIPE_NOWAIT` pipe returning 0 from `os.write`),
simulated with a real pipe + draining reader and a fake `os.write`.

Tests are excluded from the shipped zip, so threads here are fine.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly"))

import bridge  # noqa: E402


CHILD_BOUND = """
import sys, json
sys.stdout.write(json.dumps({"event": "bound"}) + "\\n")
sys.stdout.flush()
sys.stdin.buffer.read()  # idle until the parent closes stdin (EOF)
"""

CHILD_BIND_ERROR = """
import sys, json
sys.stdout.write(json.dumps({"event": "bind_error", "error": "address already in use"}) + "\\n")
sys.stdout.flush()
"""

CHILD_DIE = """
import sys
sys.exit(3)
"""

CHILD_SLOW = """
import sys, json, time
time.sleep(0.4)
sys.stdout.write(json.dumps({"event": "bound"}) + "\\n")
sys.stdout.flush()
sys.stdin.buffer.read()
"""


class _DummyProc:
    """Stands in for a live `Popen` for the pipe/logic unit tests (nothing
    actually spawned): alive, with no owned stdio for `close` to touch."""

    stdin = None
    stdout = None

    def poll(self):
        return None


class BridgeHelperTest(unittest.TestCase):
    def _write_script(self, source):
        fd, path = tempfile.mkstemp(suffix=".py", prefix="darkly_fake_child_")
        os.write(fd, source.encode("utf-8"))
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _spawn(self, source, **kwargs):
        path = self._write_script(source)
        hp = bridge.HelperProcess()
        self.addCleanup(hp.close)
        err = hp.spawn(path, "127.0.0.1", 0, 1, None, **kwargs)
        return hp, err

    # --- bind_host ---

    def test_bind_host(self):
        self.assertEqual(bridge.bind_host(False), "127.0.0.1")
        self.assertEqual(bridge.bind_host(True), "0.0.0.0")

    # --- handshake ---

    def test_handshake_bound(self):
        hp, err = self._spawn(CHILD_BOUND)
        self.assertIsNone(err)
        self.assertTrue(hp.bound)
        self.assertFalse(hp.handshake_pending)
        self.assertTrue(hp.alive())

    def test_handshake_bind_error(self):
        hp, err = self._spawn(CHILD_BIND_ERROR)
        self.assertIsNotNone(err)
        self.assertIn("Could not bind", err)
        self.assertIn("address already in use", err)
        self.assertFalse(hp.bound)

    def test_handshake_child_dies(self):
        hp, err = self._spawn(CHILD_DIE)
        self.assertIsNotNone(err)
        self.assertIn("exited", err)
        self.assertFalse(hp.bound)

    def test_slow_child_resolves_via_pump(self):
        # The child emits `bound` only after the synchronous window closes, so
        # spawn returns with the handshake pending and a later pump resolves it.
        with patch.object(bridge, "HANDSHAKE_WINDOW", 0.1):
            hp, err = self._spawn(CHILD_SLOW)
        self.assertIsNone(err)
        self.assertTrue(hp.handshake_pending)
        self.assertFalse(hp.bound)
        deadline = time.monotonic() + 3.0
        while not hp.bound and time.monotonic() < deadline:
            hp.pump()
            time.sleep(0.02)
        self.assertTrue(hp.bound)
        self.assertFalse(hp.handshake_pending)

    def test_close_is_idempotent(self):
        hp, err = self._spawn(CHILD_BOUND)
        self.assertIsNone(err)
        hp.close()
        hp.close()  # must not raise
        self.assertFalse(hp.alive())

    # --- event parsing ---

    def test_pump_parses_events_across_partial_reads(self):
        r, w = os.pipe()
        os.set_blocking(r, False)
        self.addCleanup(lambda: os.close(w))
        hp = bridge.HelperProcess()
        hp.proc = _DummyProc()
        hp._stdout_fd = r

        os.write(w, b'{"event":"cli')
        self.assertEqual(hp.pump(), [])  # incomplete line -> nothing yet

        os.write(w, b'ents","count":2}\n{"event":"bound"}\n')
        events = hp.pump()
        self.assertEqual(
            events, [{"event": "clients", "count": 2}, {"event": "bound"}]
        )
        self.assertTrue(hp.bound)
        os.close(r)

    # --- single-slot send semantics ---

    def test_unstarted_frame_is_replaced(self):
        hp = bridge.HelperProcess()
        a = np.zeros((2, 2, 4), np.float32)
        b = np.ones((2, 2, 4), np.float32)
        hp.send_frame(2, 2, a, None)
        hp.send_frame(2, 2, b, None)
        # The not-yet-started slot holds the newest frame; `a` was dropped.
        self.assertIsNone(hp._writing)
        self.assertEqual(bytes(hp._pending[1]), b.tobytes())

    def test_midwrite_frame_is_preserved(self):
        hp = bridge.HelperProcess()
        # Pretend a message is mid-transmission (protocol integrity in flight).
        in_flight = [memoryview(b"half-sent-header-and-payload")]
        hp._writing = in_flight
        b = np.ones((2, 2, 4), np.float32)
        hp.send_frame(2, 2, b, None)
        # The in-flight message is untouched; the new frame waits in `_pending`.
        self.assertIs(hp._writing, in_flight)
        self.assertEqual(bytes(hp._pending[1]), b.tobytes())

    # --- stalled watchdog ---

    def test_stalled_on_stuck_transfer(self):
        hp = bridge.HelperProcess()
        hp.proc = _DummyProc()
        hp._bound = True
        hp._writing = [memoryview(b"x")]
        hp._last_progress = time.monotonic() - 10.0
        self.assertTrue(hp.stalled(5.0))
        hp._last_progress = time.monotonic()
        self.assertFalse(hp.stalled(5.0))

    def test_stalled_on_unresolved_handshake(self):
        hp = bridge.HelperProcess()
        hp.proc = _DummyProc()
        hp._bound = False
        hp._spawn_time = time.monotonic() - (bridge.HANDSHAKE_TIMEOUT + 1.0)
        self.assertTrue(hp.stalled(5.0))

    # --- pipe semantics (Windows-shaped, simulated) ---

    def test_write_budget_completes_frame_in_bounded_pumps(self):
        # A frame many times the pipe buffer: with the retry budget (child drains
        # at memcpy speed during the sub-ms would-block sleeps) it lands in one
        # or two pumps; a first-would-block-bails pump would need buffer-many.
        r, w = os.pipe()
        os.set_blocking(w, False)
        received = bytearray()
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                try:
                    data = os.read(r, 65536)
                except BlockingIOError:
                    time.sleep(0.0001)
                    continue
                if data == b"":
                    break
                received.extend(data)

        reader = threading.Thread(target=drain)
        reader.start()
        try:
            hp = bridge.HelperProcess()
            hp.proc = _DummyProc()
            hp._stdin_fd = w
            frame = np.zeros((1024, 512, 4), np.float32)  # ~8 MB, >> 64 KiB buffer
            hp.send_frame(1024, 512, frame, None)
            pumps = 0
            while (hp._writing is not None or hp._pending is not None) and pumps < 40:
                hp.pump()
                pumps += 1
            self.assertIsNone(hp._writing)
            self.assertLessEqual(pumps, 5)
        finally:
            stop.set()
            os.close(w)
            reader.join(timeout=2.0)
            os.close(r)

    def test_zero_byte_write_is_would_block_not_spin(self):
        # Windows `PIPE_NOWAIT`: a full pipe may return 0 from WriteFile rather
        # than raising. `pump` must treat 0 as would-block - retry within the
        # budget, not spin forever, and leave the frame outstanding.
        hp = bridge.HelperProcess()
        hp.proc = _DummyProc()
        hp._stdin_fd = -1  # unused; os.write is faked
        frame = np.zeros((64, 64, 4), np.float32)
        hp.send_frame(64, 64, frame, None)

        calls = []

        def fake_write(_fd, data):
            calls.append(len(data))
            return 0  # always "full": would-block, never drains

        with patch.object(bridge.os, "write", fake_write):
            start = time.monotonic()
            hp.pump()
            elapsed = time.monotonic() - start

        self.assertGreater(len(calls), 1)  # retried, not one-and-done
        self.assertGreaterEqual(elapsed, bridge.WRITE_BUDGET * 0.5)  # respected budget
        self.assertLess(elapsed, 2.0)  # did not spin unbounded
        self.assertIsNotNone(hp._writing)  # frame still outstanding


if __name__ == "__main__":
    unittest.main()
