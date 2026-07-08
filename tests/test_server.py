"""Stdlib-only tests for the frame server + dedup hub.

These need no Blender - `server.py` is pure stdlib - so they run under plain
`python3 -m pytest` (or `python3 -m unittest`). The capture/encode halves need a
live Blender GPU context and are validated by the headless smoke test in the
README.
"""

import os
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly_stream"))

import server  # noqa: E402


class FrameHubTest(unittest.TestCase):
    def test_publish_bumps_seq_and_returns_latest(self):
        hub = server.FrameHub()
        self.assertEqual(hub.latest_seq(), 0)
        hub.publish(b"frame-a")
        self.assertEqual(hub.latest_seq(), 1)
        got = hub.wait_for(0, timeout=1.0)
        self.assertEqual(got, (1, b"frame-a"))

    def test_wait_times_out_when_no_new_frame(self):
        hub = server.FrameHub()
        hub.publish(b"only")
        # Already caught up to seq 1 → no newer frame → times out (dedup: a client
        # never re-receives a frame it already has).
        start = time.monotonic()
        self.assertIsNone(hub.wait_for(1, timeout=0.2))
        self.assertGreaterEqual(time.monotonic() - start, 0.19)

    def test_stale_intermediate_frames_are_dropped(self):
        hub = server.FrameHub()
        hub.publish(b"one")
        hub.publish(b"two")
        hub.publish(b"three")
        # A client at seq 0 jumps straight to the freshest frame, not the queue.
        self.assertEqual(hub.wait_for(0, timeout=1.0), (3, b"three"))


class _StreamClient:
    """Reads the HTTP response headers once, then de-chunks the persistent body to
    yield successive application frames (`[4-byte length][bytes]`)."""

    def __init__(self, sock):
        self.sock = sock
        self.stream = b""
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += sock.recv(4096)
        header, self.stream = buf.split(b"\r\n\r\n", 1)
        assert b"200" in header.split(b"\r\n")[0], header

    def _pull_chunk(self):
        while b"\r\n" not in self.stream:
            self.stream += self.sock.recv(4096)
        size_line, self.stream = self.stream.split(b"\r\n", 1)
        size = int(size_line, 16)
        while len(self.stream) < size + 2:
            self.stream += self.sock.recv(4096)
        chunk = self.stream[:size]
        self.stream = self.stream[size + 2:]  # drop trailing CRLF
        return chunk

    def read_frame(self):
        decoded = b""
        while len(decoded) < 4:
            decoded += self._pull_chunk()
        length = struct.unpack(">I", decoded[:4])[0]
        while len(decoded) < 4 + length:
            decoded += self._pull_chunk()
        # Any bytes beyond this frame belong to the next one - but chunk-aligned
        # pulls never over-read here since the server writes one frame per chunk.
        return decoded[4:4 + length]


class StreamServerTest(unittest.TestCase):
    def test_serves_a_published_frame_over_http(self):
        hub = server.FrameHub()
        hub.publish(b"webp-bytes-here")  # publish before connecting
        srv = server.start_server("127.0.0.1", 0, hub)  # port 0 = ephemeral
        port = srv.server_address[1]
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            client = _StreamClient(sock)
            self.assertEqual(client.read_frame(), b"webp-bytes-here")

            # A newly published frame reaches the same open connection.
            hub.publish(b"second-frame")
            self.assertEqual(client.read_frame(), b"second-frame")
            sock.close()
        finally:
            server.stop_server(srv)

    def test_client_count_tracks_connections(self):
        hub = server.FrameHub()
        hub.publish(b"x")
        srv = server.start_server("127.0.0.1", 0, hub)
        port = srv.server_address[1]
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
            _StreamClient(sock).read_frame()  # ensure the handler has registered
            # Give the finally/register ordering a beat to settle.
            deadline = time.monotonic() + 2.0
            while hub.client_count != 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(hub.client_count, 1)
            sock.close()
        finally:
            server.stop_server(srv)


if __name__ == "__main__":
    unittest.main()
