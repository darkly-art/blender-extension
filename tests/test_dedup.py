"""Tests for `dedup.frame_is_duplicate` - the pre-send raw-frame dedup.

The stream captures unconditionally on every redraw of the streamed viewport
(progressive refinement is invisible to any cheap proxy), so this compare is
what drops redundant frames before the ~15 MB pipe transfer and the PNG encode.
It keys on the raw scene-linear buffer plus its `ViewSettings`, so it never
drops a distinct-looking frame, yet correctly refuses to drop a grading-only
change (same pixels, different display transform).
"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly_stream"))

import colormanage  # noqa: E402
import dedup  # noqa: E402


def view_settings(display="sRGB", exposure=0.0):
    return colormanage.ViewSettings(
        display=display, view_transform=None, look=None, exposure=exposure, gamma=1.0
    )


def sample_frame():
    # Deterministic, not uniform, so a single-pixel change is detectable.
    rng = np.random.default_rng(0)
    return rng.random((4, 6, 4), dtype=np.float32)


class FrameDedupTest(unittest.TestCase):
    def setUp(self):
        self.raw = sample_frame()
        self.vs = view_settings()

    def test_identical_raw_and_settings_is_duplicate(self):
        self.assertTrue(
            dedup.frame_is_duplicate(self.raw.copy(), self.vs, self.raw, self.vs)
        )

    def test_changed_settings_is_not_duplicate(self):
        # Same pixels, different grading (the raw buffer is pre display transform,
        # so exposure/view-transform changes must still publish).
        self.assertFalse(
            dedup.frame_is_duplicate(
                self.raw.copy(), view_settings(exposure=1.5), self.raw, self.vs
            )
        )

    def test_changed_pixels_is_not_duplicate(self):
        changed = self.raw.copy()
        changed[0, 0, 0] = 1.0 - changed[0, 0, 0]
        self.assertFalse(dedup.frame_is_duplicate(changed, self.vs, self.raw, self.vs))

    def test_resize_is_not_duplicate(self):
        resized = np.zeros((5, 6, 4), np.float32)
        self.assertFalse(dedup.frame_is_duplicate(resized, self.vs, self.raw, self.vs))

    def test_first_frame_is_not_duplicate(self):
        self.assertFalse(dedup.frame_is_duplicate(self.raw, self.vs, None, None))

    def test_camera_source_none_settings_dedups_on_pixels(self):
        # The camera source carries `view_settings=None` (uint8, already
        # color-managed); dedup must still work with None on both sides.
        raw = (self.raw * 255).astype(np.uint8)
        self.assertTrue(dedup.frame_is_duplicate(raw.copy(), None, raw, None))
        changed = raw.copy()
        changed[0, 0, 0] ^= 0xFF
        self.assertFalse(dedup.frame_is_duplicate(changed, None, raw, None))

    def test_settings_presence_mismatch_is_not_duplicate(self):
        # One frame graded (viewport source), the other not (camera source): the
        # snapshots differ, so never a duplicate even with identical bytes.
        self.assertFalse(
            dedup.frame_is_duplicate(self.raw.copy(), self.vs, self.raw, None)
        )


if __name__ == "__main__":
    unittest.main()
