"""Darkly - serve a live view of Blender to Darkly over localhost.

Streams one of two sources (`capture.SOURCES`, chosen in the panel): the 3D
viewport's own view - captured by reusing the pixels Blender already rendered,
at zero extra render cost - or a camera's POV rendered into an offscreen each
frame. Frames are encoded as straight-alpha PNGs and streamed length-prefixed
over HTTP so Darkly's `blender` void can composite the live 3D view onto the
canvas - paint behind and around geometry thanks to the transparent background.

The runtime itself - pacing timer, capture draw handler, helper subprocess
lifecycle, and crash containment - lives in `stream.StreamRuntime` (see that
module's docstring for the subprocess / context / failure model). The
serve/encode pipeline runs in a helper subprocess (`helper.py`), so the Blender
process stays main-thread only. This module holds the Blender registration
surface: properties, operators, panel, and the re-exported module API.
"""

import bpy

from . import capture

# Public module API, used by `panel.py` and external drivers (the headless
# smoke test). The implementation lives on the `stream._runtime` singleton.
from .stream import (  # noqa: F401 - re-exported surface
    start_stream,
    stop_stream,
    is_running,
    has_live_resources,
    has_failed,
    status_text,
)

# This is a Blender *extension* (metadata lives in `blender_manifest.toml`), not a
# legacy add-on - there is deliberately no `bl_info` dict.


# Blender requires the strings returned by a dynamic enum callback to stay
# referenced from Python (a known API gotcha - they are otherwise garbage
# collected while the UI still points at them), hence this module-level cache.
_viewport_items = []


def _viewport_enum_items(_self, _context):
    global _viewport_items
    items = [
        (
            "AUTO",
            "Auto (First Viewport)",
            "Capture the first open 3D viewport; follows the layout if "
            "viewports come and go",
        )
    ]
    for _space, _region, key, label in capture.list_view3d():
        items.append((key, label, "Capture this 3D viewport"))
    _viewport_items = items
    return items


class DarklyProperties(bpy.types.PropertyGroup):
    source: bpy.props.EnumProperty(
        name="Source",
        items=(
            (
                "VIEWPORT",
                "Viewport",
                "Stream the 3D viewport's current view. Reuses the pixels "
                "Blender already rendered - no extra rendering, overlays and "
                "background excluded automatically (one frame of latency)",
            ),
            (
                "CAMERA",
                "Camera",
                "Stream a camera's point of view regardless of where the "
                "viewport is looking. Renders the scene once more per frame",
            ),
        ),
        default="VIEWPORT",
        description="What the stream shows",
    )
    viewport: bpy.props.EnumProperty(
        name="Viewport",
        items=_viewport_enum_items,
        description="Which 3D viewport to capture. Identified by position in "
        "the layout, so a closed or rearranged selection falls back to the "
        "first open viewport",
    )
    port: bpy.props.IntProperty(
        name="Port", default=8765, min=1, max=65535,
        description="Port the frame stream is served on",
    )
    listen_all: bpy.props.BoolProperty(
        name="All Interfaces",
        default=False,
        description="Serve on every network interface so other machines on "
        "your network can connect, instead of this machine only",
    )
    fps: bpy.props.IntProperty(
        name="FPS", default=15, min=1, max=60,
        description="Max capture rate. Gates the main-thread capture, so "
        "lower it if streaming feels heavy (a static scene sends nothing)",
    )
    compression: bpy.props.IntProperty(
        name="Compression", default=1, min=0, max=9,
        description="PNG compression level (0 = fastest/largest, 9 = slowest/smallest)",
    )
    camera: bpy.props.PointerProperty(
        name="Camera",
        type=bpy.types.Object,
        poll=lambda _self, obj: obj.type == "CAMERA",
        description="Camera to stream (defaults to the scene's active camera)",
    )


class DARKLY_OT_stream_start(bpy.types.Operator):
    bl_idname = "darkly.stream_start"
    bl_label = "Start Stream"
    bl_description = "Begin serving the selected view to Darkly"

    def execute(self, context):
        err = start_stream(context.scene)
        if err is not None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        return {"FINISHED"}


class DARKLY_OT_stream_stop(bpy.types.Operator):
    bl_idname = "darkly.stream_stop"
    bl_label = "Stop Stream"
    bl_description = "Stop serving the view"

    def execute(self, _context):
        stop_stream()
        return {"FINISHED"}


from .panel import DARKLY_PT_stream_panel  # noqa: E402 - after operator defs

_CLASSES = (
    DarklyProperties,
    DARKLY_OT_stream_start,
    DARKLY_OT_stream_stop,
    DARKLY_PT_stream_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.darkly = bpy.props.PointerProperty(type=DarklyProperties)


def unregister():
    stop_stream()
    del bpy.types.Scene.darkly
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
