"""Regression test for the GPU readback Buffer -> numpy conversion.

Defends against a specific bug: converting the *multi-dimensional* gpu.Buffer
from `read_color` straight to numpy (`np.array(buffer).reshape(...)`) misread its
per-row strides and produced whole-frame striped garbage, even though the raw
Buffer held correct pixels. The fix (`readback.buffer_to_rgba`) flattens the
Buffer to 1D first, as Blender's own `export_uv_png.py` does.

The real `gpu.types.Buffer` is a C object that only exists inside Blender, so
`FakeBuffer` below models its *contract*: multi-dimensional -> numpy conversion is
untrustworthy; flattening via `.dimensions` makes it correct. If someone drops the
flatten step from `buffer_to_rgba`, this test fails.
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
    reproducing the multi-dimensional stride misread that `buffer_to_rgba` guards
    against. Starts multi-dimensional, exactly as `read_color` returns it."""

    def __init__(self, height, width, channels, true_bytes):
        self._true = np.frombuffer(true_bytes, dtype=np.uint8)
        self.dimensions = [height, width, channels]

    def _is_flat(self):
        return isinstance(self.dimensions, int) or len(np.atleast_1d(self.dimensions)) == 1

    def __array__(self, dtype=None, copy=None):
        if self._is_flat():
            data = self._true
        else:
            # Multi-dimensional: the untrustworthy path. Return the wrong bytes so
            # any code that skips the flatten step is caught (models the striped
            # garbage we actually observed - here, a constant fill).
            data = np.full(self._true.shape, 255, dtype=np.uint8)
        return data.astype(dtype) if dtype is not None else data


class ReadbackTest(unittest.TestCase):
    def _known_frame(self, height, width):
        """A frame where every pixel is distinct, so any stride misread shows."""
        frame = np.zeros((height, width, 4), dtype=np.uint8)
        frame[..., 0] = np.arange(width, dtype=np.uint8)[None, :]       # R across x
        frame[..., 1] = np.arange(height, dtype=np.uint8)[:, None]      # G down y
        frame[..., 2] = 128
        frame[..., 3] = 255
        return frame

    def test_buffer_to_rgba_round_trips_multidim_buffer(self):
        height, width = 17, 23
        frame = self._known_frame(height, width)
        buf = FakeBuffer(height, width, 4, frame.tobytes())

        rgba = readback.buffer_to_rgba(buf, width, height)

        self.assertEqual(rgba.shape, (height, width, 4))
        self.assertEqual(rgba.dtype, np.uint8)
        np.testing.assert_array_equal(rgba, frame)

    def test_naive_conversion_is_the_bug_this_guards(self):
        """Sanity-check the model: converting the multi-dim buffer *without*
        flattening yields garbage, so the round-trip test above has teeth."""
        height, width = 17, 23
        frame = self._known_frame(height, width)
        buf = FakeBuffer(height, width, 4, frame.tobytes())

        naive = np.array(buf, dtype=np.uint8).reshape(height, width, 4)

        self.assertFalse(np.array_equal(naive, frame))


if __name__ == "__main__":
    unittest.main()
