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

import colormanage  # noqa: E402
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


class FloatEncodeTest(unittest.TestCase):
    """The viewport source hands the encoder scene-linear premultiplied float32
    (plus the ViewSettings snapshot); the encoder must un-premultiply *before*
    the display transform and produce the same PNG contract as the uint8 path."""

    def setUp(self):
        self.enc = encode.FrameEncoder(compression=1)  # no OCIO -> sRGB fallback
        self.settings = colormanage.ViewSettings(
            display="sRGB", view_transform=None, look=None, exposure=0.0, gamma=1.0
        )

    def tearDown(self):
        self.enc.free()

    def test_linear_float_roundtrip(self):
        # One opaque mid-grey, one semi-transparent, one empty pixel, bottom-up.
        H, W = 1, 3
        linear_straight = np.array(
            [[[0.5, 0.5, 0.5, 1.0], [0.5, 0.25, 0.125, 0.5], [0.0, 0.0, 0.0, 0.0]]],
            np.float32,
        )
        premul = linear_straight.copy()
        premul[..., :3] *= premul[..., 3:4]

        decoded = decode_png(self.enc.encode(W, H, premul, self.settings))

        expected_rgb = colormanage.linear_to_srgb(linear_straight[..., :3])
        expected = np.round(
            np.concatenate([expected_rgb, linear_straight[..., 3:4]], axis=-1) * 255.0
        ).astype(np.uint8)
        self.assertEqual(decoded.shape, (H, W, 4))
        np.testing.assert_array_equal(decoded[..., 3], expected[..., 3])
        diff = np.abs(decoded[..., :3].astype(int) - expected[..., :3].astype(int))
        self.assertLessEqual(int(diff.max()), 1)

    def test_unpremultiply_precedes_display_transform(self):
        # If the transform ran on premultiplied colour, a half-alpha mid-grey
        # would decode darker than the same colour at full alpha (the sRGB curve
        # is non-linear). Both must decode to the same straight RGB.
        H, W = 1, 1
        opaque = np.array([[[0.5, 0.5, 0.5, 1.0]]], np.float32)
        half = np.array([[[0.25, 0.25, 0.25, 0.5]]], np.float32)  # premul of 0.5

        rgb_opaque = decode_png(self.enc.encode(W, H, opaque, self.settings))[0, 0, :3]
        rgb_half = decode_png(self.enc.encode(W, H, half, self.settings))[0, 0, :3]

        np.testing.assert_allclose(rgb_half, rgb_opaque, atol=1)


class WorkbenchAAEdgeTest(unittest.TestCase):
    """Workbench (Solid / Wireframe) accumulates viewport AA edge RGB in log2
    space, so a coverage-`c` silhouette edge arrives as `rgb = (color+1)**c - 1`,
    `alpha = c`. With `workbench_aa` set the encoder applies the matching inverse
    `(rgb + 1)**(1/alpha) - 1` and recovers the true edge colour; a plain
    `rgb/alpha` divide leaves an over-premultiplied, darkening edge - the fringe."""

    def setUp(self):
        self.enc = encode.FrameEncoder(compression=1)  # no OCIO -> sRGB fallback

    def tearDown(self):
        self.enc.free()

    def _settings(self, workbench_aa):
        return colormanage.ViewSettings(
            display="sRGB", view_transform=None, look=None,
            exposure=0.0, gamma=1.0, workbench_aa=workbench_aa,
        )

    def _workbench_edge(self, color, cov):
        rgb = (color + 1.0) ** cov - 1.0
        return np.array([[[rgb, rgb, rgb, cov]]], np.float32)

    def test_workbench_edge_recovers_true_colour(self):
        color, cov = 0.8, 0.5
        decoded = decode_png(
            self.enc.encode(1, 1, self._workbench_edge(color, cov), self._settings(True))
        )
        expected = round(float(colormanage.linear_to_srgb(np.array([color]))[0]) * 255.0)
        self.assertEqual(int(decoded[0, 0, 3]), round(cov * 255))  # alpha = coverage, unchanged
        self.assertLessEqual(abs(int(decoded[0, 0, 0]) - expected), 2)

    def test_plain_divide_leaves_the_dark_fringe(self):
        # Pins the branch: the SAME edge on the plain (EEVEE) path stays
        # over-premultiplied and decodes well below the true colour.
        color, cov = 0.8, 0.5
        frame = self._workbench_edge(color, cov)
        decoded = decode_png(self.enc.encode(1, 1, frame, self._settings(False)))
        true_srgb = round(float(colormanage.linear_to_srgb(np.array([color]))[0]) * 255.0)
        self.assertLess(int(decoded[0, 0, 0]), true_srgb - 5)

    def test_opaque_pixel_is_untouched_by_workbench_path(self):
        # At alpha == 1 the inverse is the identity, so full-coverage interior
        # colour is unchanged whether or not the flag is set.
        opaque = np.array([[[0.5, 0.5, 0.5, 1.0]]], np.float32)
        on = decode_png(self.enc.encode(1, 1, opaque.copy(), self._settings(True)))
        off = decode_png(self.enc.encode(1, 1, opaque.copy(), self._settings(False)))
        np.testing.assert_array_equal(on, off)


if __name__ == "__main__":
    unittest.main()
