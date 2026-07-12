"""HTTP/1.1 chunked frame server - stdlib only, zero dependencies.

`GET /stream` returns a single long-lived chunked response; per frame the handler
writes `[4-byte big-endian length][WebP bytes]` (application-level framing, on top
of HTTP chunked transfer) and flushes. The frontend `HttpStreamSource` strips the
HTTP chunking (via `fetch`'s reader) and the 4-byte prefix to recover whole
frames independent of chunk boundaries.

Duplicate-frame suppression (transport layer): a monotonic `_seq` guarded by a
`threading.Condition`. The main-thread producer swaps the latest-frame buffer,
bumps `_seq`, and `notify_all()` only on a real new frame - encoding happens
*outside* the lock. Each client thread keeps its own `last_seq` and waits while
`_seq == last_seq`, so it never re-sends a frame, always gets the freshest one
(stale intermediates are dropped, not queued), and idles at zero CPU/bytes on a
static scene.

Transport liveness: change-driven frames alone make a dead-but-open socket
indistinguishable from an idle scene, so each handler emits a heartbeat - a
zero-length application frame, i.e. a chunk of exactly `\\x00\\x00\\x00\\x00` -
after `heartbeat_interval` seconds without a write. Clients skip it as a frame
but treat any bytes as proof of life; a failed heartbeat write is how the
server notices a departed client. Heartbeats come from the handler *threads*,
so a heavy render blocking Blender's main thread does not read as dead. On
`stop_server` each handler ends its response with the terminating HTTP chunk
(`0\\r\\n\\r\\n` - unambiguous vs. the 4-byte heartbeat), so clients see a clean
close instead of a parked socket.
"""

import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Seconds between transport-liveness writes on an idle connection. Overridable
# per-server (tests use a short interval). See `_StreamHandler.do_GET` for the
# actual cadence bound.
HEARTBEAT_INTERVAL = 2.0


class FrameHub:
    """Single-slot latest-frame handoff between the main-thread producer and the
    client handler threads, with a monotonic sequence for per-client dedup."""

    def __init__(self):
        self._cond = threading.Condition()
        self._latest = None  # bytes | None
        self._seq = 0
        self._clients = 0

    def publish(self, frame_bytes):
        """Swap in a new encoded frame and wake every waiting client. Cheap and
        held-lock-briefly: the (expensive) encode happens before this call."""
        with self._cond:
            self._latest = frame_bytes
            self._seq += 1
            self._cond.notify_all()

    def wait_for(self, last_seq, timeout):
        """Block until a frame newer than `last_seq` is available (or `timeout`
        seconds elapse). Returns `(seq, frame_bytes)` or `None` on timeout."""
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            if self._seq == last_seq:
                return None
            return self._seq, self._latest

    def latest_seq(self):
        with self._cond:
            return self._seq

    def add_client(self, delta):
        with self._cond:
            self._clients += delta
            return self._clients

    @property
    def client_count(self):
        with self._cond:
            return self._clients


class _StreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Silence the default per-request stderr logging - a live stream would spam it.
    def log_message(self, *_args):
        pass

    def _write_chunk(self, data):
        """Emit one HTTP/1.1 chunk: hex length, CRLF, data, CRLF."""
        self.wfile.write(b"%X\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path.rstrip("/") not in ("/stream", ""):
            self.send_error(404, "Not found")
            return

        hub = self.server.hub
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        # A Darkly page on https reaching http://localhost is cross-origin; allow
        # it explicitly so the browser's fetch isn't blocked by CORS.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        hub.add_client(1)
        # Start one behind the current seq so the freshest existing frame (if any)
        # is delivered immediately on connect rather than waiting for the next one.
        last_seq = max(0, hub.latest_seq() - 1)
        heartbeat_interval = self.server.heartbeat_interval
        last_write = time.monotonic()
        try:
            # Waking at half the heartbeat interval bounds the worst-case
            # inter-heartbeat gap at 1.5x the interval (a wait can expire just
            # before one is due, and only the next expiry sends it).
            while not self.server.stopping:
                got = hub.wait_for(last_seq, timeout=heartbeat_interval / 2)
                if got is None:
                    # Timeout - no new frame. Heartbeat if one is due: it keeps
                    # an idle scene distinguishable from a dead server, and a
                    # departed client raises out of the write (keeping
                    # `client_count` accurate, which gates the capture pipeline).
                    if time.monotonic() - last_write >= heartbeat_interval:
                        self._write_chunk(struct.pack(">I", 0))
                        last_write = time.monotonic()
                    continue
                last_seq, frame = got
                if frame is None:
                    continue
                payload = struct.pack(">I", len(frame)) + frame
                self._write_chunk(payload)
                last_write = time.monotonic()
            # Stopping: end the response with the terminating chunk (`0\r\n\r\n`)
            # so the client's reader sees a clean close immediately -
            # `stop_server` only closes the *listening* socket, and keep-alive
            # would otherwise park this handler waiting for a next request with
            # the response never ended.
            self._write_chunk(b"")
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            # Client departed (closed the tab, navigated away) - drop it quietly.
            pass
        finally:
            hub.add_client(-1)


class StreamServer(ThreadingHTTPServer):
    daemon_threads = True
    # Reuse the port immediately after a restart (avoids TIME_WAIT bind errors).
    allow_reuse_address = True

    def __init__(self, host, port, hub, heartbeat_interval=HEARTBEAT_INTERVAL):
        super().__init__((host, port), _StreamHandler)
        self.hub = hub
        self.heartbeat_interval = heartbeat_interval
        self.stopping = False


def bind_host(listen_all):
    """The address to bind: loopback by default, or every interface when the
    user opts in to serving other machines on the local network."""
    return "0.0.0.0" if listen_all else "127.0.0.1"


def start_server(host, port, hub, heartbeat_interval=HEARTBEAT_INTERVAL):
    """Bind and serve on a background thread. Returns the running `StreamServer`
    (raises `OSError` if the port is taken)."""
    server = StreamServer(host, port, hub, heartbeat_interval=heartbeat_interval)
    thread = threading.Thread(target=server.serve_forever, name="darkly-stream", daemon=True)
    thread.start()
    server._thread = thread
    return server


def stop_server(server):
    """Signal client loops to exit, then shut the server down and join its thread."""
    if server is None:
        return
    server.stopping = True
    server.shutdown()
    server.server_close()
    thread = getattr(server, "_thread", None)
    if thread is not None:
        thread.join(timeout=2.0)
