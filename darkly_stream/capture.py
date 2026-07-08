"""Offscreen viewport draw - the GPU half, main-thread only.

Draws the viewport shading from the active camera's POV into a `GPUOffScreen`
with a transparent background and reads it back as RGBA8. This is the real-time
viewport draw (`draw_view3d`), NOT a Cycles/final `render()` - it's the same GPU
engine that paints the 3D viewport, so it costs milliseconds per frame. This is
the idiomatic, prior-art-backed path for a live feed:

  - Official example: `doc/python_api/examples/gpu.9.py` (Blender source) shows
    the exact `GPUOffScreen.draw_view3d` offscreen camera draw used here.
  - Readback pattern: bundled `addons_core/io_mesh_uv_layout/export_uv_png.py`
    shows the `GPUOffScreen` → `framebuffer.clear` → `read_color(..., 'UBYTE')`
    sequence.
  - `draw_view3d(..., do_color_management=True, draw_background=False)`: the
    `draw_background=False` skips the world background so the cleared alpha=0
    survives - transparency without `film_transparent` - and color management is
    applied exactly once, here.

Realities (documented in the README):
  - `draw_view3d` draws *viewport shading* (Solid / Material Preview / Rendered as
    the viewport is set), not a full `render()`. That is correct and required for
    a live feed; a final render per frame is a non-starter. (The exception is
    Rendered shading with the Cycles engine, which makes the viewport itself slow.)
  - It bakes in overlays/gizmos unless `space.overlay.show_overlays = False` on
    the grabbed space - we toggle it off for the draw and restore it after.
  - A `bpy.app.timers` callback has no view context, so we walk the window
    manager's screens for an open `VIEW_3D` area and use its `SpaceView3D` +
    `'WINDOW'` region. If none is open we draw nothing and surface a status.
"""

import gpu
import bpy

from . import readback


def find_view3d():
    """First open 3D viewport as `(space, region)`, or `(None, None)` if the user
    has no 3D viewport open (the timer then publishes nothing)."""
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is not None:
                return area.spaces.active, region
    return None, None


class CameraCapture:
    """Owns a reused `GPUOffScreen`, reallocated only when the resolution changes.
    All methods must run on the main thread (GPU access is main-thread only)."""

    def __init__(self):
        self._offscreen = None
        self._size = (0, 0)

    def _ensure_offscreen(self, width, height):
        if self._offscreen is not None and self._size == (width, height):
            return
        if self._offscreen is not None:
            self._offscreen.free()
        self._offscreen = gpu.types.GPUOffScreen(width, height)
        self._size = (width, height)

    def free(self):
        if self._offscreen is not None:
            self._offscreen.free()
            self._offscreen = None
            self._size = (0, 0)

    def capture(self, scene, camera, space=None, region=None):
        """Draw `camera`'s view at the viewport's own resolution and return an
        `(width, height, rgba_uint8_ndarray)` tuple, or `None` if there is no open
        3D viewport. The size is the 3D viewport region's size - we don't impose a
        resolution; a viewport stream *is* whatever the viewport is. The offscreen,
        the camera projection, and the readback all use that one size, so nothing
        can go out of agreement (a size mismatch corrupts `draw_view3d`'s output).
        The pixels are display-referred, associated (premultiplied) alpha, bottom-up
        (OpenGL origin) - `encode` handles both facts.

        MUST be called with a live GPU context, i.e. from a viewport draw callback
        (`SpaceView3D.draw_handler_add(..., 'POST_PIXEL')`) - NOT from a
        `bpy.app.timers` tick or an operator, which have no current GPU context and
        make `draw_view3d` read back garbage. The draw handler passes its own
        `space`/`region`; when omitted we fall back to the first open 3D viewport."""
        if space is None or region is None:
            space, region = find_view3d()
        if space is None or region is None or camera is None:
            return None

        width, height = region.width, region.height
        self._ensure_offscreen(width, height)
        offscreen = self._offscreen
        view_layer = bpy.context.view_layer
        depsgraph = bpy.context.evaluated_depsgraph_get()

        view_matrix = camera.matrix_world.inverted()
        projection_matrix = camera.calc_matrix_camera(depsgraph, x=width, y=height)

        # Suppress overlays/gizmos for the draw, then restore the user's setting.
        prev_overlays = space.overlay.show_overlays
        space.overlay.show_overlays = False
        try:
            with offscreen.bind():
                framebuffer = gpu.state.active_framebuffer_get()
                framebuffer.clear(color=(0.0, 0.0, 0.0, 0.0))
                offscreen.draw_view3d(
                    scene,
                    view_layer,
                    space,
                    region,
                    view_matrix,
                    projection_matrix,
                    do_color_management=True,  # the ONLY color-management step
                    draw_background=False,     # transparent background
                )
                buffer = framebuffer.read_color(0, 0, width, height, 4, 0, "UBYTE")
        finally:
            space.overlay.show_overlays = prev_overlays

        # `read_color` yields a (height, width, 4) GPU Buffer, bottom-up.
        # `readback.buffer_to_rgba` flattens it before the numpy conversion - the
        # multi-dimensional conversion misreads the Buffer's strides on some
        # Blender/numpy builds. See `readback.py`.
        rgba = readback.buffer_to_rgba(buffer, width, height)

        return width, height, rgba

