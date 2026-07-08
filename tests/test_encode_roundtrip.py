"""Round-trip tests for the frame encoder.

Encodes a *known* RGBA8 image through the real `FrameEncoder` (the exact code the
worker runs) and decodes it back with an **independent** decoder (Pillow, not
OpenImageIO), asserting the pixels survive. This is the regression net that was
missing: it pins down channel order (R/G/B not swapped), vertical orientation
(top-down, not flipped), and straight-alpha correctness - the classes of bug that
show up as "wrong colours" or "glitchy stripes" in Darkly.

Needs `OpenImageIO`, `numpy`, and `PIL` (all bundled with Blender; also present in
a normal scientific-Python env). The capture half (`draw_view3d` + `read_color`)
can only run in a Blender GUI session, so it is validated separately - see the
"Save Test Frame" button the add-on adds for that.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly_stream"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import encode  # noqa: E402


def premultiply(straight):
    """Simulate the GPU's associated-alpha output from a straight reference."""
    out = straight.astype(np.float32)
    a = out[..., 3:4] / 255.0
    out[..., :3] = np.round(out[..., :3] * a)
    return out.astype(np.uint8)


def capture_buffer(straight_topdown):
    """Turn a straight, top-down reference into what `CameraCapture` hands the
    encoder: premultiplied alpha, bottom-up (OpenGL origin)."""
    return np.flipud(premultiply(straight_topdown)).copy()


def decode_png(png_bytes):
    return np.array(Image.open(io.BytesIO(png_bytes)).convert("RGBA"))


class EncodeRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.enc = encode.FrameEncoder(compression=1)

    def tearDown(self):
        self.enc.free()

    def test_channel_order_and_orientation(self):
        # Non-square (W != H) so a width/height swap is caught; each pixel encodes
        # its (col, row) in R/G with a constant B, so a channel swap or a vertical
        # flip changes specific pixels detectably. Fully opaque -> exact match.
        H, W = 4, 6
        straight = np.zeros((H, W, 4), np.uint8)
        for r in range(H):
            for c in range(W):
                straight[r, c] = [c * 40, r * 60, 200, 255]

        png = self.enc.encode(W, H, capture_buffer(straight))
        decoded = decode_png(png)

        self.assertEqual(decoded.shape, (H, W, 4))
        # Top-left is (col0,row0) = [0,0,200]; a vertical flip would put row3 here.
        np.testing.assert_array_equal(decoded[0, 0], [0, 0, 200, 255])
        # A red-dominant pixel must stay in channel 0, not leak into blue.
        np.testing.assert_array_equal(decoded[0, 5], [200, 0, 200, 255])
        np.testing.assert_array_equal(decoded, straight)

    def test_straight_alpha_survives(self):
        # Semi-transparent colour: capture is premultiplied, the encoder must
        # un-premultiply so the decoded PNG carries the original straight colour.
        H, W = 2, 2
        straight = np.array(
            [
                [[200, 100, 50, 128], [255, 255, 255, 255]],
                [[10, 220, 60, 128], [0, 0, 0, 0]],  # last: fully transparent
            ],
            np.uint8,
        )
        png = self.enc.encode(W, H, capture_buffer(straight))
        decoded = decode_png(png)

        # Alpha is exact; colour survives the premultiply/un-premultiply integer
        # round-trip within a couple of levels.
        np.testing.assert_array_equal(decoded[..., 3], straight[..., 3])
        opaque_or_semi = straight[..., 3] > 0
        diff = np.abs(decoded[..., :3].astype(int) - straight[..., :3].astype(int))
        self.assertLessEqual(int(diff[opaque_or_semi].max()), 2)

    def test_fully_transparent_pixel_has_zero_colour(self):
        # Over-black invisibility trap: a transparent pixel must decode to straight
        # alpha 0 (colour irrelevant, but must not carry a premultiplied fringe).
        H, W = 1, 1
        straight = np.array([[[0, 0, 0, 0]]], np.uint8)
        decoded = decode_png(self.enc.encode(W, H, capture_buffer(straight)))
        self.assertEqual(int(decoded[0, 0, 3]), 0)


if __name__ == "__main__":
    unittest.main()
