"""Tests for `pacing.plan_capture` - the per-tick capture/dirtiness decision.

The subtle invariant here is the *trailing harvest*: a source that reads the
previous frame (`ViewportCapture`, `needs_harvest=True`) captures one step
behind, so after the last change one extra capture is owed - but exactly once,
so a converged, untouched scene stops capturing instead of redrawing forever.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly_stream"))

import pacing  # noqa: E402


class PlanCaptureTest(unittest.TestCase):
    def test_change_captures_and_arms_harvest_for_a_stale_source(self):
        d = pacing.plan_capture(
            needs_render=False, source_dirty=True, harvest_owed=False, needs_harvest=True
        )
        self.assertTrue(d.capture)
        self.assertTrue(d.is_change)
        self.assertFalse(d.needs_render)  # a change clears the depsgraph flag
        self.assertTrue(d.harvest_owed)  # stale source owes a trailing harvest

    def test_change_does_not_arm_harvest_for_a_direct_source(self):
        d = pacing.plan_capture(False, True, False, needs_harvest=False)
        self.assertTrue(d.capture)
        self.assertTrue(d.is_change)
        self.assertFalse(d.harvest_owed)

    def test_depsgraph_edit_forces_a_change_capture(self):
        d = pacing.plan_capture(
            needs_render=True, source_dirty=False, harvest_owed=False, needs_harvest=True
        )
        self.assertTrue(d.capture)
        self.assertTrue(d.is_change)
        self.assertFalse(d.needs_render)

    def test_idle_scene_does_not_capture(self):
        d = pacing.plan_capture(False, False, harvest_owed=False, needs_harvest=True)
        self.assertFalse(d.capture)
        self.assertFalse(d.harvest_owed)

    def test_trailing_harvest_captures_once_then_terminates(self):
        # A change on a stale source arms the harvest.
        changed = pacing.plan_capture(False, True, False, needs_harvest=True)
        self.assertTrue(changed.harvest_owed)

        # Next tick, nothing changed but a harvest is owed: capture once (not a
        # change), and clear it.
        harvest = pacing.plan_capture(
            changed.needs_render, False, changed.harvest_owed, needs_harvest=True
        )
        self.assertTrue(harvest.capture)
        self.assertFalse(harvest.is_change)
        self.assertFalse(harvest.harvest_owed)

        # Following tick, still nothing changed and the harvest is spent: no
        # capture. The scene has converged - this is what stops the redraw loop.
        converged = pacing.plan_capture(
            harvest.needs_render, False, harvest.harvest_owed, needs_harvest=True
        )
        self.assertFalse(converged.capture)

    def test_change_while_a_harvest_is_owed_rearms_it(self):
        d = pacing.plan_capture(
            needs_render=False, source_dirty=True, harvest_owed=True, needs_harvest=True
        )
        self.assertTrue(d.capture)
        self.assertTrue(d.is_change)
        self.assertTrue(d.harvest_owed)


if __name__ == "__main__":
    unittest.main()
