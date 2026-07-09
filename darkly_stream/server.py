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
"""

import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
        try:
            while not self.server.stopping:
                got = hub.wait_for(last_seq, timeout=1.0)
                if got is None:
                    continue  # timeout - loop to re-check `stopping`
                last_seq, frame = got
                if frame is None:
                    continue
                payload = struct.pack(">I", len(frame)) + frame
                self._write_chunk(payload)
        except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
            # Client departed (closed the tab, navigated away) - drop it quietly.
            pass
        finally:
            hub.add_client(-1)


class StreamServer(ThreadingHTTPServer):
    daemon_threads = True
    # Reuse the port immediately after a restart (avoids TIME_WAIT bind errors).
    allow_reuse_address = True

    def __init__(self, host, port, hub):
        super().__init__((host, port), _StreamHandler)
        self.hub = hub
        self.stopping = False


def bind_host(listen_all):
    """The address to bind: loopback by default, or every interface when the
    user opts in to serving other machines on the local network."""
    return "0.0.0.0" if listen_all else "127.0.0.1"


def start_server(host, port, hub):
    """Bind and serve on a background thread. Returns the running `StreamServer`
    (raises `OSError` if the port is taken)."""
    server = StreamServer(host, port, hub)
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
