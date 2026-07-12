"""The per-tick capture decision - pure, stdlib only, no `bpy`.

The stream's timer paces capture; on each tick it must decide, from a few bits
of runtime state, whether to request a frame and how the dirtiness bookkeeping
advances. That decision is the subtle part of the redraw-observer model - in
particular the *trailing harvest*: a source that reads the previous frame
(`ViewportCapture`) captures one step behind, so after the last change one extra
capture is owed to harvest the settled state, and it must be owed exactly once
so a converged scene stops redrawing instead of looping forever.

Factoring it out of the (bpy-bound) tick keeps that state machine pure and
unit-testable. The tick supplies the inputs and applies the result.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureDecision:
    """The outcome of one tick's pacing decision.

    `capture` - request a capture this tick. `is_change` - the capture is for a
    genuine change (vs a trailing harvest of an already-captured state), so the
    source's own skip (e.g. the camera signature) should advance. `needs_render`
    / `harvest_owed` - the values those runtime flags take after this tick."""

    capture: bool
    is_change: bool
    needs_render: bool
    harvest_owed: bool


def plan_capture(needs_render, source_dirty, harvest_owed, needs_harvest):
    """Decide whether to capture this tick and how the dirtiness flags advance.

    `needs_render` - a depsgraph edit is pending (set by the depsgraph handler).
    `source_dirty` - the capture source reports itself dirty (a viewport redraw,
    or a camera-signature change). `harvest_owed` - a trailing harvest is owed
    from a prior change. `needs_harvest` - whether this source reads one frame
    stale and therefore owes a trailing harvest after each change.

    A genuine change clears `needs_render` and (re)arms the trailing harvest. A
    tick with no change but a harvest owed spends it - captures once and clears
    it - so a static scene converges to no capture instead of looping."""
    if needs_render or source_dirty:
        return CaptureDecision(
            capture=True, is_change=True, needs_render=False, harvest_owed=needs_harvest
        )
    if harvest_owed:
        return CaptureDecision(
            capture=True, is_change=False, needs_render=needs_render, harvest_owed=False
        )
    return CaptureDecision(
        capture=False, is_change=False, needs_render=needs_render, harvest_owed=harvest_owed
    )
