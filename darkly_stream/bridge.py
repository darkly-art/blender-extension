"""Parent side of the helper subprocess - `bpy`-free, unit-testable.

The serve/encode pipeline runs in a helper subprocess (`helper.py`) launched
with Blender's bundled Python, so the Blender process itself imports no
`threading` and no `queue` (the reviewer's hard constraint). This module owns
that child: the `subprocess.Popen` handle and the two pipe fds, both set
non-blocking so the Blender main thread can pump them from the existing timer
tick without a reader thread and without ever parking on a full pipe.

Wire protocol (see `helper.py` for the child half):

  - **Parent -> child, over stdin** (frames): `[4-byte BE header length][JSON
    header][payload]`. Header carries width/height/dtype/view/size; the payload
    is a zero-copy `memoryview` of the contiguous capture array (never
    `tobytes()` - that would be a ~15 MB main-thread memcpy per send).
  - **Child -> parent, over stdout** (events): newline-delimited JSON, small and
    rare - `bound` / `bind_error` / `clients` / `encode_error` / `fatal`.

Handshake: `spawn` runs a short synchronous window so the common failure (port
taken) surfaces near-instantly, preserving today's synchronous bind-error UX;
if the child is merely slow (cold interpreter + numpy import on Windows) the
window expires with the handshake *pending* and the tick's `pump()` resolves it,
so Start never freezes Blender. A pending handshake past `HANDSHAKE_TIMEOUT`
with the child still alive reads as a wedged child (`stalled`).

Windows notes (all exercised by fake-fd tests, since CI is Linux):
`os.set_blocking` on pipes needs Python >= 3.12, guaranteed by
`blender_version_min = 5.1.0`; writes go in <= `WRITE_SLICE` slices to sidestep
`PIPE_NOWAIT` large-write ambiguity; a full pipe may return 0 from `os.write`
instead of raising, which counts as would-block.
"""

import dataclasses
import json
import os
import struct
import subprocess
import sys
import time

# Synchronous handshake budget in `spawn`: long enough to catch the near-instant
# port-taken failure, short enough not to freeze Start on a cold-spawning child.
HANDSHAKE_WINDOW = 1.5
# A handshake still unresolved this long after spawn, with the child alive, is a
# wedged child (surfaced through `stalled`).
HANDSHAKE_TIMEOUT = 15.0

# Writes are sliced: Windows `PIPE_NOWAIT` semantics for writes larger than the
# pipe buffer are unreliable, so never hand `os.write` more than this at once.
WRITE_SLICE = 65536
# Per-`pump` wall-clock budget for pushing frame bytes through a would-blocking
# pipe. Kernel pipe buffers are tiny next to a ~15 MB frame and the child drains
# at memcpy speed, so retrying within this budget lands a frame in one or two
# pumps instead of one buffer-worth per tick.
WRITE_BUDGET = 0.010
WOULD_BLOCK_SLEEP = 0.0002


def bind_host(listen_all):
    """The address to bind: loopback by default, or every interface when the
    user opts in to serving other machines on the local network."""
    return "0.0.0.0" if listen_all else "127.0.0.1"


def bind_error_message(port, raw):
    """The user-facing text for a failed HTTP bind - identical whether the
    failure is caught in `spawn`'s synchronous window or later by the tick."""
    return f"Could not bind port {port}: {raw}"


class HelperProcess:
    """Owns the helper subprocess and its two non-blocking pipes. Every method
    is main-thread, never blocks Blender, and is safe to call after the child
    has died (idempotent teardown)."""

    def __init__(self):
        self.proc = None
        self._stdin_fd = None
        self._stdout_fd = None
        self._rbuf = b""            # stdout line-accumulation buffer
        self._prequeue = []         # events read during spawn, replayed by pump
        self._bound = False
        self._spawn_time = 0.0
        self._last_progress = 0.0
        self._eof = False           # stdout hit EOF (child closed / died)
        self._dead = False          # a pipe write/read errored (broken pipe)
        self._closed = False
        # Outbound single-slot: `_writing` is the message mid-transmission
        # (finished for protocol integrity before a newer one starts);
        # `_pending` is the not-yet-started slot, always latest-wins.
        self._writing = None        # list[memoryview] | None
        self._pending = None        # list[memoryview] | None

    # --- lifecycle ---

    def spawn(self, helper_path, host, port, compression, ocio_path,
              heartbeat=None, python=None):
        """Launch the helper and run the synchronous handshake window. Returns a
        user-facing error string on a definite failure (bind error, launch
        failure, child died), or `None` on success *or* a still-pending
        handshake (`bound` distinguishes them). `heartbeat=None` lets the child
        use its own default (keeping the interval's single source of truth in
        `server`)."""
        if python is None:
            python = sys.executable
        argv = [
            python, helper_path,
            "--host", host,
            "--port", str(port),
            "--compression", str(compression),
        ]
        if heartbeat is not None:
            argv += ["--heartbeat", str(heartbeat)]
        if ocio_path:
            argv += ["--ocio", ocio_path]

        # Sanitized child env: user site-packages must not shadow Blender's
        # numpy, and a stray PYTHONSTARTUP must not run in the helper. Not `-I`:
        # isolated mode also drops the script dir from `sys.path`, which the
        # helper's try-relative/except-top-level import pattern needs.
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONSTARTUP", None)
        env["PYTHONNOUSERSITE"] = "1"

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=None, env=env, bufsize=0, **kwargs
            )
        except OSError as exc:
            return f"Could not launch helper: {exc}"

        self._stdin_fd = self.proc.stdin.fileno()
        self._stdout_fd = self.proc.stdout.fileno()
        os.set_blocking(self._stdin_fd, False)
        os.set_blocking(self._stdout_fd, False)
        self._spawn_time = time.monotonic()
        self._last_progress = self._spawn_time

        deadline = self._spawn_time + HANDSHAKE_WINDOW
        while time.monotonic() < deadline:
            for evt in self._read_events():
                self._prequeue.append(evt)
                if evt.get("event") == "bind_error":
                    return bind_error_message(port, evt.get("error", "bind failed"))
            if self._bound:
                return None
            if self.proc.poll() is not None:
                return self._exit_error()
            time.sleep(0.02)
        return None  # pending; the tick resolves it via pump()

    def _exit_error(self):
        rc = self.proc.poll()
        return f"Helper process exited (code {rc}) before it was ready"

    def alive(self):
        """Whether the child is still running and its pipes are healthy."""
        return (
            self.proc is not None
            and self.proc.poll() is None
            and not self._eof
            and not self._dead
        )

    @property
    def bound(self):
        """The helper reported its HTTP server bound (stream is live)."""
        return self._bound

    @property
    def handshake_pending(self):
        return self.proc is not None and not self._bound and not self._dead

    def stalled(self, timeout):
        """A wedged-child watchdog: a frame stuck mid-transfer with no write
        progress for `timeout` seconds, or a handshake that never resolved
        within `HANDSHAKE_TIMEOUT`."""
        now = time.monotonic()
        if self._writing is not None and now - self._last_progress > timeout:
            return True
        if not self._bound and self.proc is not None:
            if now - self._spawn_time > HANDSHAKE_TIMEOUT:
                return True
        return False

    def close(self):
        """EOF-based shutdown: close the child's stdin so it sees EOF, ends its
        HTTP responses, and exits; fall back to terminate/kill. Idempotent -
        safe to call from `run_guarded` after a partial failure."""
        if self._closed:
            return
        self._closed = True
        self._writing = None
        self._pending = None
        if self.proc is None:
            return
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
        try:
            if self.proc.stdout is not None:
                self.proc.stdout.close()
        except OSError:
            pass

    # --- frame send (single-slot, latest-wins) ---

    def send_frame(self, width, height, rgba, view_settings):
        """Queue a captured frame for transmission. Held as a zero-copy
        `memoryview` of the contiguous array - no serialization memcpy. A frame
        that hasn't started transmitting is replaced (latest-wins); one already
        mid-write finishes first, preserving framing integrity."""
        view = dataclasses.asdict(view_settings) if view_settings is not None else None
        payload = memoryview(rgba).cast("B")
        header = json.dumps({
            "type": "frame",
            "width": int(width),
            "height": int(height),
            "dtype": "f4" if rgba.dtype == "float32" else "u1",
            "view": view,
            "size": payload.nbytes,
        }).encode("utf-8")
        prefix = struct.pack(">I", len(header)) + header
        # Latest-wins: overwrite the not-yet-started slot. A mid-write message
        # (`_writing`) is untouched and completes before this one starts.
        self._pending = [memoryview(prefix), payload]

    # --- pump (drive writes + read events); called each tick + after send ---

    def pump(self):
        """Push outstanding frame bytes (within `WRITE_BUDGET`) and return any
        complete events the child emitted since the last call. Never parks the
        main thread."""
        self._drive_writes()
        events = self._prequeue
        self._prequeue = []
        events.extend(self._read_events())
        return events

    def _drive_writes(self):
        if self.proc is None or self._dead:
            return
        budget_end = time.monotonic() + WRITE_BUDGET
        while True:
            if self._writing is None:
                if self._pending is None:
                    # Nothing outstanding: not stalled.
                    self._last_progress = time.monotonic()
                    return
                self._writing = self._pending
                self._pending = None
            seg = self._writing[0]
            try:
                n = os.write(self._stdin_fd, seg[:WRITE_SLICE])
            except BlockingIOError:
                n = 0
            except OSError:
                self._dead = True
                return
            if n > 0:
                self._last_progress = time.monotonic()
                if n == len(seg):
                    self._writing.pop(0)
                    if not self._writing:
                        self._writing = None  # message fully sent
                else:
                    self._writing[0] = seg[n:]
                continue
            # n == 0 -> would-block (BlockingIOError, or a full PIPE_NOWAIT pipe
            # whose WriteFile succeeds having written nothing). Retry briefly.
            if time.monotonic() >= budget_end:
                return  # resume next pump; `_writing` is preserved
            time.sleep(WOULD_BLOCK_SLEEP)

    def _read_events(self):
        if self.proc is None or self._stdout_fd is None:
            return []
        while True:
            try:
                data = os.read(self._stdout_fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                self._dead = True
                break
            if data == b"":
                self._eof = True
                break
            self._rbuf += data
        events = []
        while b"\n" in self._rbuf:
            line, self._rbuf = self._rbuf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            if isinstance(evt, dict) and evt.get("event") == "bound":
                self._bound = True
            events.append(evt)
        return events
