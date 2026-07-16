"""HTTP/1.1 chunked frame server - asyncio, stdlib only, zero dependencies.

Runs inside the **helper subprocess** (`helper.py`) on a single-threaded asyncio
loop - no `threading`, no `queue`. `GET /stream` returns a single long-lived
chunked response; per frame the handler writes `[4-byte big-endian length][PNG
bytes]` (application-level framing, on top of HTTP chunked transfer) and drains.
The frontend `HttpStreamSource` strips the HTTP chunking (via `fetch`'s reader)
and the 4-byte prefix to recover whole frames independent of chunk boundaries.

Duplicate-frame suppression (transport layer): a monotonic `_seq` guarded by an
`asyncio.Condition`. The encoder task swaps the latest-frame buffer, bumps
`_seq`, and `notify_all()` only on a real new frame. Each client coroutine keeps
its own `last_seq` and waits while `_seq == last_seq`, so it never re-sends a
frame, always gets the freshest one (stale intermediates are dropped, not
queued), and idles at zero CPU/bytes on a static scene.

Transport liveness: change-driven frames alone make a dead-but-open socket
indistinguishable from an idle scene, so each handler emits a heartbeat - a
zero-length application frame, i.e. a chunk of exactly `\\x00\\x00\\x00\\x00` -
after `heartbeat_interval` seconds without a write. Clients skip it as a frame
but treat any bytes as proof of life; a failed heartbeat write is how the
server notices a departed client. On stop each handler ends its response with
the terminating HTTP chunk (`0\\r\\n\\r\\n` - unambiguous vs. the 4-byte
heartbeat), so clients see a clean close instead of a parked socket.

Client-count changes fire the `on_client_count` callback (each connect and
disconnect), which the helper turns into a `clients` event for the parent - it
gates Blender's capture pipeline (zero cost when nobody is watching).
"""

import asyncio
import struct

# Seconds between transport-liveness writes on an idle connection. Overridable
# per-server (tests use a short interval). See `_serve_client` for the actual
# cadence bound.
HEARTBEAT_INTERVAL = 2.0


class FrameHub:
    """Single-slot latest-frame handoff between the encoder task and the client
    handler coroutines, with a monotonic sequence for per-client dedup.

    `on_client_count(count)` is invoked (synchronously, outside the condition)
    on every connect/disconnect so the helper can report the count upstream."""

    def __init__(self, on_client_count=None):
        self._cond = asyncio.Condition()
        self._latest = None  # bytes | None
        self._seq = 0
        self._clients = 0
        self._on_client_count = on_client_count

    async def publish(self, frame_bytes):
        """Swap in a new encoded frame and wake every waiting client. Cheap and
        held-lock-briefly: the (expensive) encode happens before this call."""
        async with self._cond:
            self._latest = frame_bytes
            self._seq += 1
            self._cond.notify_all()

    async def wait_for(self, last_seq, timeout):
        """Wait until a frame newer than `last_seq` is available (or `timeout`
        seconds elapse). Returns `(seq, frame_bytes)` or `None` on timeout."""
        async with self._cond:
            if self._seq == last_seq:
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout)
                except asyncio.TimeoutError:
                    return None
            if self._seq == last_seq:
                return None
            return self._seq, self._latest

    def latest_seq(self):
        # No `await` between read and use: on a single-threaded loop this is a
        # plain read, no lock needed (the lock only guards the notify handoff).
        return self._seq

    def add_client(self, delta):
        """Adjust the connected-client count and fire the callback. Synchronous
        (no `await`) so it never races with itself on the one loop."""
        self._clients += delta
        count = self._clients
        if self._on_client_count is not None:
            self._on_client_count(count)
        return count

    @property
    def client_count(self):
        return self._clients


def _chunk(data):
    """One HTTP/1.1 chunk: hex length, CRLF, data, CRLF."""
    return b"%X\r\n" % len(data) + data + b"\r\n"


_RESPONSE_HEADERS = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/octet-stream\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"Cache-Control: no-store\r\n"
    # A Darkly page on https reaching http://localhost is cross-origin; allow
    # it explicitly so the browser's fetch isn't blocked by CORS.
    b"Access-Control-Allow-Origin: *\r\n"
    b"\r\n"
)

_NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class StreamServer:
    """Owns the running `asyncio.Server`, the hub, and the stop event the
    client handlers watch. `aclose()` is the clean shutdown (ends every response
    with the terminating chunk, then stops accepting)."""

    def __init__(self, server, hub, stop_event, heartbeat_interval):
        self._server = server
        self.hub = hub
        self._stop_event = stop_event
        self.heartbeat_interval = heartbeat_interval

    @property
    def sockets(self):
        return self._server.sockets

    @property
    def port(self):
        """The bound port (useful with an ephemeral port-0 bind in tests)."""
        return self._server.sockets[0].getsockname()[1]

    async def aclose(self):
        """Signal client loops to end their responses, stop accepting, and wait
        for the listening socket to close. Idempotent."""
        self._stop_event.set()
        self._server.close()
        await self._server.wait_closed()


async def _serve_client(reader, writer, hub, stop_event, heartbeat_interval):
    """One client connection: parse the request head, then stream frames +
    heartbeats until the client departs or the server stops."""
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = await reader.read(4096)
            if not chunk:
                writer.close()
                return
            head += chunk
            if len(head) > 65536:  # a well-formed GET head is tiny; cap abuse
                writer.close()
                return
    except (ConnectionError, OSError):
        writer.close()
        return

    request_line = head.split(b"\r\n", 1)[0].split(b" ")
    path = request_line[1].decode("latin-1") if len(request_line) >= 2 else "/"
    if path.rstrip("/") not in ("/stream", ""):
        try:
            writer.write(_NOT_FOUND)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        return

    writer.write(_RESPONSE_HEADERS)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        writer.close()
        return

    hub.add_client(1)
    # Start one behind the current seq so the freshest existing frame (if any)
    # is delivered immediately on connect rather than waiting for the next one.
    last_seq = max(0, hub.latest_seq() - 1)
    loop = asyncio.get_event_loop()
    last_write = loop.time()
    try:
        # Waking at half the heartbeat interval bounds the worst-case
        # inter-heartbeat gap at 1.5x the interval (a wait can expire just
        # before one is due, and only the next expiry sends it). The same
        # cadence bounds how promptly a stop is noticed.
        while not stop_event.is_set():
            got = await hub.wait_for(last_seq, timeout=heartbeat_interval / 2)
            if got is None:
                if stop_event.is_set():
                    break
                # Timeout - no new frame. Heartbeat if one is due: it keeps an
                # idle scene distinguishable from a dead server, and a departed
                # client raises out of the write (keeping `client_count`
                # accurate, which gates the capture pipeline).
                if loop.time() - last_write >= heartbeat_interval:
                    writer.write(_chunk(struct.pack(">I", 0)))
                    await writer.drain()
                    last_write = loop.time()
                continue
            last_seq, frame = got
            if frame is None:
                continue
            writer.write(_chunk(struct.pack(">I", len(frame)) + frame))
            await writer.drain()
            last_write = loop.time()
        # Stopping: end the response with the terminating chunk (`0\r\n\r\n`) so
        # the client's reader sees a clean close immediately instead of a parked
        # keep-alive socket.
        writer.write(_chunk(b""))
        await writer.drain()
    except (ConnectionError, OSError):
        # Client departed (closed the tab, navigated away) - drop it quietly.
        pass
    finally:
        hub.add_client(-1)
        try:
            writer.close()
        except (ConnectionError, OSError):
            pass


async def start_server(host, port, hub, heartbeat_interval=HEARTBEAT_INTERVAL):
    """Bind and start serving on the current asyncio loop. Returns a running
    `StreamServer` (raises `OSError` if the port is taken)."""
    stop_event = asyncio.Event()

    async def handler(reader, writer):
        await _serve_client(reader, writer, hub, stop_event, heartbeat_interval)

    server = await asyncio.start_server(handler, host, port)
    return StreamServer(server, hub, stop_event, heartbeat_interval)
