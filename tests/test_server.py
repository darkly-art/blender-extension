"""Tests for the asyncio frame server + dedup hub.

These need no Blender - `server.py` is pure stdlib asyncio - so they run under
plain `python3 -m unittest`. The capture/encode halves need a live Blender GPU
context and are validated by the headless smoke test in the README; the helper
subprocess end-to-end is covered by `test_helper.py`.
"""

import asyncio
import os
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly"))

import server  # noqa: E402

TIMEOUT = 3.0  # generous cap so a bug fails the test instead of hanging forever


async def _wait_until(predicate, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return predicate()


class _StreamClient:
    """Reads the HTTP response headers once, then de-chunks the persistent body
    to yield successive application frames (`[4-byte length][bytes]`)."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.stream = b""

    @classmethod
    async def connect(cls, port, path=b"/stream"):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET " + path + b" HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        self = cls(reader, writer)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                raise AssertionError("connection closed before headers")
            buf += chunk
        header, self.stream = buf.split(b"\r\n\r\n", 1)
        self.status_line = header.split(b"\r\n")[0]
        return self

    async def _pull_chunk(self):
        while b"\r\n" not in self.stream:
            self.stream += await self.reader.read(4096)
        size_line, self.stream = self.stream.split(b"\r\n", 1)
        size = int(size_line, 16)
        while len(self.stream) < size + 2:
            self.stream += await self.reader.read(4096)
        chunk = self.stream[:size]
        self.stream = self.stream[size + 2:]  # drop trailing CRLF
        return chunk

    async def read_frame(self):
        decoded = b""
        while len(decoded) < 4:
            decoded += await self._pull_chunk()
        length = struct.unpack(">I", decoded[:4])[0]
        while len(decoded) < 4 + length:
            decoded += await self._pull_chunk()
        return decoded[4:4 + length]

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class FrameHubTest(unittest.IsolatedAsyncioTestCase):
    async def test_publish_bumps_seq_and_returns_latest(self):
        hub = server.FrameHub()
        self.assertEqual(hub.latest_seq(), 0)
        await hub.publish(b"frame-a")
        self.assertEqual(hub.latest_seq(), 1)
        got = await hub.wait_for(0, timeout=1.0)
        self.assertEqual(got, (1, b"frame-a"))

    async def test_wait_times_out_when_no_new_frame(self):
        hub = server.FrameHub()
        await hub.publish(b"only")
        # Already caught up to seq 1 -> no newer frame -> times out (dedup: a
        # client never re-receives a frame it already has).
        start = time.monotonic()
        self.assertIsNone(await hub.wait_for(1, timeout=0.2))
        self.assertGreaterEqual(time.monotonic() - start, 0.19)

    async def test_stale_intermediate_frames_are_dropped(self):
        hub = server.FrameHub()
        await hub.publish(b"one")
        await hub.publish(b"two")
        await hub.publish(b"three")
        # A client at seq 0 jumps straight to the freshest frame, not the queue.
        self.assertEqual(await hub.wait_for(0, timeout=1.0), (3, b"three"))

    async def test_client_count_fires_callback(self):
        seen = []
        hub = server.FrameHub(on_client_count=seen.append)
        self.assertEqual(hub.add_client(1), 1)
        self.assertEqual(hub.add_client(1), 2)
        self.assertEqual(hub.add_client(-1), 1)
        self.assertEqual(seen, [1, 2, 1])


class StreamServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_serves_a_published_frame_over_http(self):
        hub = server.FrameHub()
        await hub.publish(b"png-bytes-here")  # publish before connecting
        srv = await server.start_server("127.0.0.1", 0, hub)
        try:
            client = await _StreamClient.connect(srv.port)
            self.assertIn(b"200", client.status_line)
            self.assertEqual(await asyncio.wait_for(client.read_frame(), TIMEOUT), b"png-bytes-here")

            # A newly published frame reaches the same open connection.
            await hub.publish(b"second-frame")
            self.assertEqual(await asyncio.wait_for(client.read_frame(), TIMEOUT), b"second-frame")
            await client.close()
        finally:
            await srv.aclose()

    async def test_unknown_path_gets_404(self):
        hub = server.FrameHub()
        srv = await server.start_server("127.0.0.1", 0, hub)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
            writer.write(b"GET /nope HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.read(4096), TIMEOUT)
            self.assertIn(b"404", head)
            writer.close()
        finally:
            await srv.aclose()

    async def test_listen_all_binds_every_interface(self):
        hub = server.FrameHub()
        srv = await server.start_server("0.0.0.0", 0, hub)
        try:
            self.assertEqual(srv.sockets[0].getsockname()[0], "0.0.0.0")
        finally:
            await srv.aclose()

    async def test_idle_connection_receives_heartbeat(self):
        # Regression: with no heartbeat, an idle scene and a dead server were
        # indistinguishable on the wire - this read blocked forever pre-fix.
        hub = server.FrameHub()
        await hub.publish(b"only-frame")
        srv = await server.start_server("127.0.0.1", 0, hub, heartbeat_interval=0.2)
        try:
            client = await _StreamClient.connect(srv.port)
            self.assertEqual(await asyncio.wait_for(client.read_frame(), TIMEOUT), b"only-frame")
            # Nothing else is published; the next payload must be a heartbeat
            # (zero-length frame) within ~1.5x the interval, not a hang.
            self.assertEqual(await asyncio.wait_for(client.read_frame(), TIMEOUT), b"")
            await client.close()
        finally:
            await srv.aclose()

    async def test_heartbeat_wire_format(self):
        # The heartbeat is a zero-length *application* frame: an HTTP chunk whose
        # body is exactly the 4-byte big-endian length prefix of 0 - unambiguous
        # vs. the empty chunk (`0\r\n\r\n`), which terminates the HTTP response.
        hub = server.FrameHub()
        srv = await server.start_server("127.0.0.1", 0, hub, heartbeat_interval=0.2)
        try:
            client = await _StreamClient.connect(srv.port)
            self.assertEqual(
                await asyncio.wait_for(client._pull_chunk(), TIMEOUT), b"\x00\x00\x00\x00"
            )
            await client.close()
        finally:
            await srv.aclose()

    async def test_dead_client_detected_via_heartbeat(self):
        # Regression: without periodic writes the handler blocked in `wait_for`
        # forever after the client vanished, so `client_count` never decayed
        # (defeating the "zero cost when nobody is connected" gate).
        hub = server.FrameHub()
        await hub.publish(b"x")
        srv = await server.start_server("127.0.0.1", 0, hub, heartbeat_interval=0.2)
        try:
            client = await _StreamClient.connect(srv.port)
            await asyncio.wait_for(client.read_frame(), TIMEOUT)
            self.assertTrue(await _wait_until(lambda: hub.client_count == 1))
            await client.close()
            # Heartbeat writes to the closed socket raise in the handler, which
            # then deregisters - the count must decay without any publish.
            self.assertTrue(await _wait_until(lambda: hub.client_count == 0, timeout=5.0))
        finally:
            await srv.aclose()

    async def test_stop_terminates_client_response(self):
        # Regression for the keep-alive park: without an explicit terminating
        # chunk the handler would leave the response un-ended and the client
        # would hang instead of seeing a clean close.
        hub = server.FrameHub()
        await hub.publish(b"frame")
        srv = await server.start_server("127.0.0.1", 0, hub, heartbeat_interval=0.2)
        try:
            client = await _StreamClient.connect(srv.port)
            self.assertEqual(await asyncio.wait_for(client.read_frame(), TIMEOUT), b"frame")
            stop_task = asyncio.ensure_future(srv.aclose())
            # The handler notices the stop event on its next wait timeout and
            # must end the response with the terminating chunk (empty body).
            self.assertEqual(await asyncio.wait_for(client._pull_chunk(), TIMEOUT), b"")
            await client.close()
            await stop_task
        finally:
            await srv.aclose()

    async def test_client_count_tracks_connections(self):
        hub = server.FrameHub()
        await hub.publish(b"x")
        srv = await server.start_server("127.0.0.1", 0, hub)
        try:
            client = await _StreamClient.connect(srv.port)
            await asyncio.wait_for(client.read_frame(), TIMEOUT)
            self.assertTrue(await _wait_until(lambda: hub.client_count == 1))
            await client.close()
        finally:
            await srv.aclose()


if __name__ == "__main__":
    unittest.main()
