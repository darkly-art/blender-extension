"""Regression test for the GPU readback Buffer -> numpy conversion.

Defends against a specific bug: converting the *multi-dimensional* gpu.Buffer
from `read_color` straight to numpy (`np.array(buffer).reshape(...)`) misread its
per-row strides and produced whole-frame striped garbage, even though the raw
Buffer held correct pixels. The fix (`readback.buffer_to_pixels`) flattens the
Buffer to 1D first, as Blender's own `export_uv_png.py` does.

The real `gpu.types.Buffer` is a C object that only exists inside Blender, so
`FakeBuffer` below models its *contract*: multi-dimensional -> numpy conversion is
untrustworthy; flattening via `.dimensions` makes it correct. If someone drops the
flatten step from `buffer_to_pixels`, this test fails.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly_stream"))

import numpy as np  # noqa: E402

import readback  # noqa: E402


class FakeBuffer:
    """Stand-in for `gpu.types.Buffer`. Holds the true row-major bytes but only
    yields them correctly to numpy once flattened to 1D via `.dimensions` -
    reproducing the multi-dimensional stride misread that `buffer_to_pixels`
    guards against. Starts multi-dimensional, exactly as `read_color` returns
    it."""

    def __init__(self, height, width, channels, true_array):
        self._true = true_array.reshape(-1)
        self.dimensions = [height, width, channels]

    def _is_flat(self):
        return isinstance(self.dimensions, int) or len(np.atleast_1d(self.dimensions)) == 1

    def __array__(self, dtype=None, copy=None):
        if self._is_flat():
            data = self._true
        else:
            # Multi-dimensional: the untrustworthy path. Return the wrong values
            # so any code that skips the flatten step is caught (models the
            # striped garbage we actually observed - here, a constant fill).
            data = np.full(self._true.shape, 255, dtype=self._true.dtype)
        return data.astype(dtype) if dtype is not None else data


class ReadbackTest(unittest.TestCase):
    def _known_frame(self, height, width, dtype):
        """A frame where every pixel is distinct, so any stride misread shows."""
        frame = np.zeros((height, width, 4), dtype=dtype)
        frame[..., 0] = np.arange(width, dtype=dtype)[None, :]       # R across x
        frame[..., 1] = np.arange(height, dtype=dtype)[:, None]      # G down y
        frame[..., 2] = 128
        frame[..., 3] = 200
        return frame

    def test_uint8_round_trips_multidim_buffer(self):
        height, width = 17, 23
        frame = self._known_frame(height, width, np.uint8)
        buf = FakeBuffer(height, width, 4, frame)

        rgba = readback.buffer_to_pixels(buf, width, height)

        self.assertEqual(rgba.shape, (height, width, 4))
        self.assertEqual(rgba.dtype, np.uint8)
        np.testing.assert_array_equal(rgba, frame)

    def test_float32_round_trips_multidim_buffer(self):
        # The viewport source reads 'FLOAT' (scene-linear RGBA16F framebuffer);
        # the flatten-first contract must hold for float buffers too.
        height, width = 9, 13
        frame = self._known_frame(height, width, np.float32) / 255.0
        buf = FakeBuffer(height, width, 4, frame)

        rgba = readback.buffer_to_pixels(buf, width, height, dtype=np.float32)

        self.assertEqual(rgba.shape, (height, width, 4))
        self.assertEqual(rgba.dtype, np.float32)
        np.testing.assert_array_equal(rgba, frame)

    def test_naive_conversion_is_the_bug_this_guards(self):
        """Sanity-check the model: converting the multi-dim buffer *without*
        flattening yields garbage, so the round-trip tests above have teeth."""
        height, width = 17, 23
        frame = self._known_frame(height, width, np.uint8)
        buf = FakeBuffer(height, width, 4, frame)

        naive = np.array(buf, dtype=np.uint8).reshape(height, width, 4)

        self.assertFalse(np.array_equal(naive, frame))


if __name__ == "__main__":
    unittest.main()
