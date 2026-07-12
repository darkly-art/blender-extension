"""Darkly Stream - serve a live view of Blender to Darkly over localhost.

Streams one of two sources (`capture.SOURCES`, chosen in the panel): the 3D
viewport's own view - captured by reusing the pixels Blender already rendered,
at zero extra render cost - or a camera's POV rendered into an offscreen each
frame. Frames are encoded as straight-alpha PNGs and streamed length-prefixed
over HTTP so Darkly's `blender` void can composite the live 3D view onto the
canvas - paint behind and around geometry thanks to the transparent background.

The runtime itself - pacing timer, capture draw handler, encode worker, HTTP
server lifecycle, and crash containment - lives in `stream.StreamRuntime`
(see that module's docstring for the threading / context / failure model).
This module holds the Blender registration surface: properties, operators,
panel, and the re-exported module API.
"""

import time

import bpy

from . import stream, capture, encode

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


class DarklyStreamProperties(bpy.types.PropertyGroup):
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
    profile: bpy.props.BoolProperty(
        name="Profile",
        default=False,
        description="Print per-stage timings (capture vs encode) to the console",
    )


class DARKLY_OT_stream_start(bpy.types.Operator):
    bl_idname = "darkly.stream_start"
    bl_label = "Start Darkly Stream"
    bl_description = "Begin serving the selected view to Darkly"

    def execute(self, context):
        err = start_stream(context.scene)
        if err is not None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        return {"FINISHED"}


class DARKLY_OT_stream_stop(bpy.types.Operator):
    bl_idname = "darkly.stream_stop"
    bl_label = "Stop Darkly Stream"
    bl_description = "Stop serving the view"

    def execute(self, _context):
        stop_stream()
        return {"FINISHED"}


class DARKLY_OT_stream_benchmark(bpy.types.Operator):
    """Measure the Blender-side cost with no server or client involved.

    Runs the exact capture and encode pipeline the stream runs - for the
    *selected source* - `iterations` times, and reports per-stage timings,
    fully decoupled from whether the HTTP stream works, so the main-thread cost
    can be profiled on its own.

    It is *modal*, not blocking: GPU capture needs a live GPU context, which
    only a viewport draw callback provides (see `StreamRuntime`), so each
    sample is taken inside a temporary draw handler (of the source's own
    event type) driven across viewport redraws by a window timer. A blocking
    loop can't work - there is no GPU context to draw into between redraws."""

    bl_idname = "darkly.stream_benchmark"
    bl_label = "Benchmark Capture"
    bl_description = "Time the capture + encode pipeline directly (no stream needed)"

    iterations: bpy.props.IntProperty(
        name="Iterations", default=30, min=1, max=500,
        description="How many frames to time",
    )

    def invoke(self, context, _event):
        scene = context.scene
        props = scene.darkly_stream

        self._cap = capture.SOURCES[props.source]()
        err = self._cap.poll(scene, props)
        if err is not None:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        target_space = capture.find_view3d(props.viewport)[0]
        if target_space is None:
            self.report({"ERROR"}, "Open a 3D viewport to benchmark")
            return {"CANCELLED"}
        self._target_space = target_space.as_pointer()

        self._scene = scene
        self._props = props
        self._enc = encode.FrameEncoder(
            compression=props.compression, ocio_config_path=stream.ocio_config_path()
        )
        self._draw_ms = []
        self._encode_ms = []
        self._size = (0, 0)
        self._pending = True

        self._handler = bpy.types.SpaceView3D.draw_handler_add(
            self._sample, (), "WINDOW", self._cap.draw_handler_type
        )
        self._timer = context.window_manager.event_timer_add(0.001, window=context.window)
        context.window_manager.modal_handler_add(self)
        stream.request_redraw()
        return {"RUNNING_MODAL"}

    def _sample(self):
        """Draw callback: take one timed capture+encode in a live GPU context."""
        if not self._pending:
            return
        ctx = bpy.context
        space = ctx.space_data
        region = ctx.region
        if space is None or getattr(space, "type", None) != "VIEW_3D" or region is None:
            return
        if space.as_pointer() != self._target_space:
            return
        self._pending = False
        t0 = time.perf_counter()
        result = self._cap.capture(self._scene, self._props, space, region)
        t1 = time.perf_counter()
        if result is None:
            return
        width, height, rgba, view_settings = result
        self._enc.encode(width, height, rgba, view_settings)
        t2 = time.perf_counter()
        self._draw_ms.append((t1 - t0) * 1000.0)
        self._encode_ms.append((t2 - t1) * 1000.0)
        self._size = (width, height)

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        if len(self._draw_ms) >= self.iterations:
            return self._finish(context)
        if not self._pending:
            self._pending = True
            stream.request_redraw()
        return {"PASS_THROUGH"}

    def _finish(self, context):
        context.window_manager.event_timer_remove(self._timer)
        bpy.types.SpaceView3D.draw_handler_remove(self._handler, "WINDOW")
        self._cap.free()
        self._enc.free()

        def avg(xs):
            return sum(xs) / len(xs)

        width, height = self._size
        d_avg, e_avg = avg(self._draw_ms), avg(self._encode_ms)
        total = d_avg + e_avg
        ceiling = 1000.0 / total if total > 0 else 0.0
        summary = (
            f"{len(self._draw_ms)}x {width}x{height}: "
            f"capture avg {d_avg:.1f}ms (max {max(self._draw_ms):.1f}), "
            f"encode avg {e_avg:.1f}ms (max {max(self._encode_ms):.1f}), "
            f"total {total:.1f}ms/frame -> ~{ceiling:.0f} fps ceiling"
        )
        print(f"[darkly_stream] benchmark {summary}")
        self.report({"INFO"}, summary)
        return {"FINISHED"}


from .panel import DARKLY_PT_stream_panel  # noqa: E402 - after operator defs

_CLASSES = (
    DarklyStreamProperties,
    DARKLY_OT_stream_start,
    DARKLY_OT_stream_stop,
    DARKLY_OT_stream_benchmark,
    DARKLY_PT_stream_panel,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.darkly_stream = bpy.props.PointerProperty(type=DarklyStreamProperties)


def unregister():
    stop_stream()
    del bpy.types.Scene.darkly_stream
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
