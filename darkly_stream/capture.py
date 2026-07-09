"""Capture sources - the GPU half, main-thread only.

Two ways to produce a frame, behind one uniform surface (`SOURCES`):

  - **ViewportCapture** - reuses the viewport's *already-rendered* pixels.
    Blender keeps each 3D viewport's scene render in its own texture, separate
    from overlays and the theme background (both live in a second, overlay
    texture that is composited only at blit-to-screen time). During the
    on-screen render loop that texture's framebuffer is bound - with only
    depth cleared - *before* the engines draw, which is exactly when Python
    `'PRE_VIEW'` draw callbacks run (Blender `draw_context.cc`,
    `drw_callbacks_pre_scene`; the bind is in `DRW_draw_render_loop` right
    above it). So `active_framebuffer_get().read_color(...)` inside a
    `'PRE_VIEW'` handler returns the **previous completed frame's** scene:
    transparent background and no overlays by construction, one frame of
    latency, and zero extra rendering - captures piggyback on redraws Blender
    performs anyway. The pixels are scene-linear premultiplied RGBA16F, so a
    `ViewSettings` snapshot travels with each frame and the worker applies the
    display transform (`colormanage`).

  - **CameraCapture** - renders a camera's POV into a private `GPUOffScreen`
    via `draw_view3d` (one full extra scene render per frame). This is the
    idiomatic offscreen path - official example `doc/python_api/examples/gpu.9.py`
    (Blender source); readback pattern from the bundled
    `addons_core/io_mesh_uv_layout/export_uv_png.py`.
    `draw_background=False` keeps the cleared alpha=0 background and
    `do_color_management=True` applies the display transform on the GPU, so
    the output is display-referred uint8. It bakes in overlays/gizmos unless
    suppressed, so overlays are toggled off around the draw and restored.

Everything the rest of the add-on needs to know is on the class: which draw
handler event it must run under (`draw_handler_type`), whether the stream owes
a trailing harvest redraw because captures are one frame stale
(`needs_harvest`), the dedup `signature`, and the precondition `poll`.
Consumers never branch on which source they hold.

All capture methods must run on the main thread inside a live GPU context,
i.e. from a viewport draw callback - NOT from a `bpy.app.timers` tick or an
operator, which have no current GPU context. A `bpy.app.timers` callback also
has no view context, so `find_view3d` walks the window manager's screens for
an open `VIEW_3D` area.
"""

import gpu
import bpy
import numpy as np

try:  # package context (Blender); the unit tests import modules top-level
    from . import colormanage, readback  # because the package __init__ needs bpy
except ImportError:
    import colormanage
    import readback


def list_view3d():
    """Every open 3D viewport as `(space, region, key, label)`, across all
    windows. `key` is a re-resolvable identifier (`"<screen name>:<index of the
    VIEW_3D area within that screen>"` - screen datablock names are unique) and
    `label` is what the user sees in the viewport dropdown. Both are positional,
    so they go stale if the user rearranges areas - resolution falls back to the
    first viewport rather than stopping the stream (`find_view3d`)."""
    found = []
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        index = 0
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            suffix = f" #{index + 1}" if index else ""
            label = f"{screen.name}{suffix} ({region.width}x{region.height})"
            found.append((area.spaces.active, region, f"{screen.name}:{index}", label))
            index += 1
    return found


def find_view3d(selector="AUTO"):
    """The selected 3D viewport as `(space, region)`. `"AUTO"` - or a selection
    that no longer resolves (viewport closed, layout rearranged) - yields the
    first open viewport, so the stream degrades to "some viewport" instead of
    stopping. `(None, None)` if no 3D viewport is open (the timer then
    publishes nothing)."""
    viewports = list_view3d()
    if not viewports:
        return None, None
    if selector != "AUTO":
        for space, region, key, _label in viewports:
            if key == selector:
                return space, region
    return viewports[0][0], viewports[0][1]


def _flatten(matrix):
    """A 4x4 `mathutils.Matrix` (or any nested sequence) as a hashable tuple."""
    return tuple(matrix[i][j] for i in range(4) for j in range(4))


class ViewportCapture:
    """Streams whatever the 3D viewport shows, by reading the viewport's own
    render texture (see the module docstring for why this works and what it
    yields). Owns no GPU resources - the texture being read belongs to the
    viewport."""

    draw_handler_type = "PRE_VIEW"
    needs_harvest = True  # captures are one frame stale; the last one must be re-harvested

    def poll(self, scene, props):
        """No preconditions beyond an open viewport (checked by the caller)."""
        return None

    def signature(self, scene, props, region):
        """Moves on any orbit/pan/zoom or projection change
        (`perspective_matrix = window_matrix @ view_matrix`), frame change, or
        region resize. `None` when the region has no 3D view data yet."""
        rv3d = region.data
        if rv3d is None:
            return None
        return (
            _flatten(rv3d.perspective_matrix),
            scene.frame_current,
            region.width,
            region.height,
        )

    def capture(self, scene, props, space, region):
        """Read the bound framebuffer's color attachment - the viewport's scene
        render texture, holding the previous completed frame. MUST run inside a
        `'PRE_VIEW'` draw callback: that is the only moment the render
        framebuffer is the active one (later callbacks run on the overlay
        framebuffer). Returns `(width, height, rgba_float32, view_settings)` -
        scene-linear, premultiplied, bottom-up."""
        width, height = region.width, region.height
        framebuffer = gpu.state.active_framebuffer_get()
        buffer = framebuffer.read_color(0, 0, width, height, 4, 0, "FLOAT")
        rgba = readback.buffer_to_pixels(buffer, width, height, dtype=np.float32)
        view_settings = colormanage.view_settings_snapshot(scene, space.shading)
        return width, height, rgba, view_settings

    def free(self):
        pass


class CameraCapture:
    """Streams a camera's POV regardless of viewport orientation, by rendering
    it into a reused `GPUOffScreen` (reallocated only when the resolution
    changes)."""

    draw_handler_type = "POST_PIXEL"
    needs_harvest = False  # draw_view3d renders the current state directly

    def __init__(self):
        self._offscreen = None
        self._size = (0, 0)

    @staticmethod
    def _camera(scene, props):
        camera = props.camera or scene.camera
        if camera is None or camera.type != "CAMERA":
            return None
        return camera

    def poll(self, scene, props):
        if self._camera(scene, props) is None:
            return "No active camera in the scene"
        return None

    def signature(self, scene, props, region):
        # The region size is part of the signature so a viewport resize
        # re-captures (the stream's resolution *is* the viewport's own).
        camera = self._camera(scene, props)
        if camera is None:
            return None
        return (
            _flatten(camera.matrix_world),
            scene.frame_current,
            region.width,
            region.height,
        )

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

    def capture(self, scene, props, space, region):
        """Draw the camera's view at the viewport's own resolution and return
        `(width, height, rgba_uint8, None)` - display-referred, premultiplied,
        bottom-up - or `None` without a camera. The size is the 3D viewport
        region's size - we don't impose a resolution; the offscreen, the camera
        projection, and the readback all use that one size, so nothing can go
        out of agreement (a size mismatch corrupts `draw_view3d`'s output).

        MUST be called with a live GPU context, i.e. from a viewport draw
        callback - NOT from a `bpy.app.timers` tick or an operator, which have
        no current GPU context and make `draw_view3d` read back garbage."""
        camera = self._camera(scene, props)
        if camera is None:
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

        rgba = readback.buffer_to_pixels(buffer, width, height)

        return width, height, rgba, None


SOURCES = {
    "VIEWPORT": ViewportCapture,
    "CAMERA": CameraCapture,
}
