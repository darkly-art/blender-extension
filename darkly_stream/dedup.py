"""Raw-frame de-duplication - numpy only, no `bpy`, no OpenImageIO.

Runs in the **Blender process** (the capture draw handler, pre-send), so a
duplicate ~15 MB frame is never serialized down the pipe to the helper: the
compare is ~1.7 ms and now saves the transfer *and* the encode. The captured
frame is unconditional (progressive refinement is invisible to any cheap
signature), so this raw compare is what filters redundant frames - an
incidental hover redraw, a converged scene's trailing harvest.
"""

import numpy as np


def frame_is_duplicate(rgba, view_settings, prev_rgba, prev_view_settings):
    """True when this raw captured frame would encode to the same PNG as the
    last published one, so the whole send + encode can be skipped.

    Compared on the *raw* buffer (pre display transform) plus its `ViewSettings`
    snapshot: raw-identical pixels under identical settings encode to identical
    bytes (the encode is deterministic), so this never drops a distinct-looking
    frame. The settings are part of the key because the raw buffer is
    scene-linear - a grading change (exposure / view transform / OCIO) leaves
    the pixels identical while the displayed frame differs, and must NOT be
    deduped. A shape change (viewport resize) is a mismatch, as is the first
    frame (`prev_rgba is None`).

    A plain `np.array_equal` is the compare: on a 720p float frame it is
    ~1.7 ms for the equal case (the case that matters - it saves the ~30 ms
    encode *and* the pipe transfer), memory-bandwidth-bound and needing no
    digest. A distinct frame gets sent regardless, so its compare cost is noise
    next to the encode; a strided fast-reject was measured to only *slow* the
    equal case, so there isn't one."""
    if prev_rgba is None or view_settings != prev_view_settings:
        return False
    if rgba.shape != prev_rgba.shape:
        return False
    return np.array_equal(rgba, prev_rgba)
