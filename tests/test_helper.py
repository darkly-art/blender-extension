"""End-to-end test of the helper subprocess - no Blender.

Spawns the real `helper.py` through `bridge.HelperProcess` (the real parent
path), streams one float32 frame down its stdin, and reads the PNG back out of
the HTTP server over a raw socket - the whole serve/encode pipeline, exercised
exactly as it runs in production minus the GPU capture. Decodes with Pillow (an
independent decoder) to prove a straight-alpha PNG comes out, checks the
`clients` event fires, and confirms closing stdin frees the port.

Needs numpy, OpenImageIO, and Pillow (all bundled with Blender). PyOpenColorIO
is optional - absent it, the display transform falls back to sRGB, which is the
norm here (no OCIO config path is passed).
"""

import io
import os
import socket
import struct
import sys
import time
import unittest

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly"))

import bridge  # noqa: E402
import colormanage  # noqa: E402

_HELPER = os.path.join(os.path.dirname(__file__), "..", "darkly", "helper.py")


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _BlockingClient:
    """Reads the HTTP headers once, then de-chunks the body into application
    frames (`[4-byte length][bytes]`) over a blocking socket."""

    def __init__(self, sock):
        self.sock = sock
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
        self.stream = self.stream[size + 2:]
        return chunk

    def read_frame(self):
        decoded = b""
        while len(decoded) < 4:
            decoded += self._pull_chunk()
        length = struct.unpack(">I", decoded[:4])[0]
        while len(decoded) < 4 + length:
            decoded += self._pull_chunk()
        return decoded[4:4 + length]


class HelperEndToEndTest(unittest.TestCase):
    def test_frame_round_trips_through_the_helper(self):
        port = _free_port()
        hp = bridge.HelperProcess()
        self.addCleanup(hp.close)
        err = hp.spawn(_HELPER, "127.0.0.1", port, 1, None)
        self.assertIsNone(err, err)
        self.assertTrue(hp.bound)

        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.settimeout(5)
        sock.sendall(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
        client = _BlockingClient(sock)

        # The connect fires a `clients` event back through stdout.
        clients = 0
        deadline = time.monotonic() + 5.0
        while clients < 1 and time.monotonic() < deadline:
            for evt in hp.pump():
                if evt.get("event") == "clients":
                    clients = evt["count"]
            time.sleep(0.02)
        self.assertEqual(clients, 1)

        # Send one opaque scene-linear float frame (premultiplied == straight at
        # alpha 1). Non-square so a width/height swap would show.
        height, width = 4, 6
        linear = np.zeros((height, width, 4), np.float32)
        linear[..., :3] = 0.5
        linear[..., 3] = 1.0
        settings = colormanage.ViewSettings(
            display="sRGB", view_transform=None, look=None, exposure=0.0, gamma=1.0
        )
        # Bottom-up (OpenGL origin) as a capture source produces; the encoder
        # flips it top-down. Opaque + uniform, so orientation doesn't change bytes.
        hp.send_frame(width, height, linear, settings)
        hp.pump()

        png = client.read_frame()
        decoded = np.array(Image.open(io.BytesIO(png)).convert("RGBA"))
        self.assertEqual(decoded.shape, (height, width, 4))
        np.testing.assert_array_equal(decoded[..., 3], 255)  # opaque
        expected = round(float(colormanage.linear_to_srgb(np.array([0.5]))[0]) * 255.0)
        self.assertLessEqual(abs(int(decoded[0, 0, 0]) - expected), 2)

        sock.close()

        # Closing stdin gives the helper EOF; it must exit and free the port.
        hp.close()
        self.assertFalse(hp.alive())
        rebind = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        rebind.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rebind.bind(("127.0.0.1", port))  # would raise if still held
        finally:
            rebind.close()


if __name__ == "__main__":
    unittest.main()
