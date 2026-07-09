"""Darkly Stream - serve a live view of Blender to Darkly over localhost.

Streams one of two sources (`capture.SOURCES`, chosen in the panel): the 3D
viewport's own view - captured by reusing the pixels Blender already rendered,
at zero extra render cost - or a camera's POV rendered into an offscreen each
frame. Frames are encoded as straight-alpha PNGs and streamed length-prefixed
over HTTP so Darkly's `blender` void can composite the live 3D view onto the
canvas - paint behind and around geometry thanks to the transparent background.

Threading / context model: GPU capture needs a live GPU context, which only a
viewport draw callback provides - a `bpy.app.timers` tick has none. So a timer
paces the stream (dedup gate) and, when a frame is due, tags the viewport for
redraw; a draw handler (registered under the event the active source needs -
`'PRE_VIEW'` or `'POST_PIXEL'`) then does the capture on the main thread inside
that live context and hands the raw pixels to a worker thread. The worker does
the (bpy-free) display transform + PNG encode and publishes to the server.
Keeping that work off the main thread is what makes the add-on cheap enough to
leave Blender responsive. The stdlib HTTP server serves the bytes from its own
handler threads. Handoff and dedup go through `server.FrameHub`
(worker -> clients) and a single-slot latest-frame handoff (main -> worker).

Duplicate-frame suppression is three layers: (1) origin - skip capture+encode
when the source's signature / frame / scene haven't changed (here);
(2) transport - the `FrameHub` seq/Condition only wakes clients on a real new
frame (`server.py`); (3) sink - the frontend `HttpStreamSource` decodes only on
a genuinely new frame.

Trailing harvest: the viewport source reads the *previous* completed frame
(see `capture.py`), so after the last change one extra redraw is owed to
harvest the final state - `_harvest_owed` below. During continuous interaction
the redraw tags coalesce with the redraws Blender performs anyway.
"""

import os

import bpy
import time
import threading

from bpy.app.handlers import persistent

from . import server, capture, encode

# This is a Blender *extension* (metadata lives in `blender_manifest.toml`), not a
# legacy add-on - there is deliberately no `bl_info` dict.

# --- Runtime state (module-level; a single stream per Blender process) ---
_hub = None
_server = None
_capture = None
_encoder = None
_running = False
_last_signature = None
_needs_render = True
_harvest_owed = False
_status = "Stopped"
_last_draw_ms = 0.0
_last_encode_ms = 0.0

# GPU capture must run in a live GPU context, which only a viewport draw
# callback provides (a timer tick has none). So the timer decides *when* a
# frame is needed and tags the viewport for redraw; this draw handler
# (event type per the active source) does the GPU work when `_capture_pending`
# is set.
_draw_handler = None
_capture_pending = False

# Main-thread -> worker handoff: a single latest-frame slot (stale frames are
# dropped, never queued) plus the worker thread that drains it.
_encode_cond = threading.Condition()
_pending_frame = None  # (width, height, rgba_ndarray, view_settings|None) | None
_worker_thread = None
_worker_stop = False


def is_running():
    return _running


def status_text():
    return _status


def _set_status(text):
    global _status
    if text == _status:
        return  # only repaint on an actual change, never every tick
    _status = text
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


@persistent
def _on_depsgraph_update(_scene, _depsgraph):
    # Any scene edit (transform, material, geometry) marks the next tick dirty so
    # dedup doesn't skip a genuine change the view signature alone can't see.
    global _needs_render
    _needs_render = True


def _ocio_config_path():
    """Blender's bundled OCIO config, for the worker-side display transform.
    `None` (-> sRGB fallback) if the layout ever changes."""
    path = os.path.join(
        bpy.utils.system_resource("DATAFILES"), "colormanagement", "config.ocio"
    )
    return path if os.path.exists(path) else None


def _timer_tick():
    """Paces the stream: runs the dedup gate and, when a fresh frame is warranted,
    requests one and forces a viewport redraw (the draw handler does the actual
    GPU capture). Returns the next interval, or `None` to unregister once streaming
    stops."""
    global _last_signature, _needs_render, _harvest_owed, _capture_pending
    if not _running:
        return None

    scene = bpy.context.scene
    props = scene.darkly_stream
    interval = 1.0 / max(1, props.fps)

    # Zero cost when nobody is connected: no capture, no readback, no encode.
    # The add-on must not touch Blender's frame budget while Darkly isn't looking.
    if _hub.client_count == 0:
        _set_status("Streaming - no client connected")
        return interval

    err = _capture.poll(scene, props)
    if err is not None:
        _set_status(err)
        return interval

    space, region = capture.find_view3d()
    if space is None:
        _set_status("Open a 3D viewport to stream")
        return interval

    # Dedup layer 1 (origin): nothing changed since the last publish - skip the
    # whole capture+encode. A static scene therefore drives zero GPU/CPU work
    # here, except the trailing harvest a stale-reading source is owed.
    signature = _capture.signature(scene, props, region)
    if signature is None:
        return interval
    dirty = _needs_render or signature != _last_signature

    if not dirty and not _harvest_owed:
        _set_status(f"Streaming - idle ({_hub.client_count} client(s))")
        return interval

    # A live capture needs an active GPU context, which a timer tick lacks. So
    # the timer only *requests* a frame and forces a viewport redraw; the draw
    # handler runs during that redraw (valid GPU context) and does the actual
    # capture + worker handoff.
    if dirty:
        _last_signature = signature
        _needs_render = False
        # A source that reads the previous frame will capture one step behind;
        # owe it a final redraw so the settled state is harvested too.
        _harvest_owed = _capture.needs_harvest
    else:
        _harvest_owed = False
    _capture_pending = True
    _request_redraw()
    _set_status(
        f"Streaming - capture {_last_draw_ms:.0f}ms/main, encode {_last_encode_ms:.0f}ms/worker"
    )
    return interval


def _request_redraw():
    """Force every open 3D viewport to redraw, so the capture draw handler fires."""
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _capture_draw_handler():
    """Viewport draw callback - the GPU half. Runs on the main thread *inside* a
    live GPU context (registered under the event the active source requires),
    captures the frame the timer requested, and hands the raw pixels to the
    encode worker. Cheap early-out when no frame is pending, so it costs
    nothing on the many redraws it isn't servicing."""
    global _capture_pending, _last_draw_ms, _pending_frame
    if not _running or not _capture_pending:
        return

    ctx = bpy.context
    space = ctx.space_data
    region = ctx.region
    if space is None or getattr(space, "type", None) != "VIEW_3D" or region is None:
        return

    scene = ctx.scene
    props = scene.darkly_stream

    # Claim the request before capturing so a redraw storm can't double-capture.
    _capture_pending = False

    t0 = time.perf_counter()
    result = _capture.capture(scene, props, space, region)
    if result is None:
        return
    _last_draw_ms = (time.perf_counter() - t0) * 1000.0

    # Hand the raw pixels to the worker (latest wins; a slow encode never backs up
    # the main thread - stale frames are simply overwritten).
    with _encode_cond:
        _pending_frame = result
        _encode_cond.notify()

    if props.profile:
        print(
            f"[darkly_stream] capture {_last_draw_ms:.1f}ms "
            f"encode {_last_encode_ms:.1f}ms (worker) ({result[0]}x{result[1]})"
        )


def _encode_worker():
    """Worker thread: drain the latest captured frame, transform + encode it
    (numpy/OCIO/OpenImageIO, no `bpy`), and publish to the server. Idles on the
    condition when there's nothing to do, so it costs nothing on a static
    scene."""
    global _pending_frame, _last_encode_ms
    while True:
        with _encode_cond:
            while _pending_frame is None and not _worker_stop:
                _encode_cond.wait()
            if _worker_stop:
                return
            width, height, rgba, view_settings = _pending_frame
            _pending_frame = None

        t0 = time.perf_counter()
        try:
            frame = _encoder.encode(width, height, rgba, view_settings)
        except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the worker
            print(f"[darkly_stream] encode error: {exc}")
            continue
        _last_encode_ms = (time.perf_counter() - t0) * 1000.0
        _hub.publish(frame)


def start_stream(scene):
    """Bind the server and register the capture timer. Returns an error string, or
    `None` on success."""
    global _hub, _server, _capture, _encoder, _running, _last_signature, _needs_render
    global _worker_thread, _worker_stop, _pending_frame, _draw_handler, _capture_pending
    global _harvest_owed
    if _running:
        return None

    # `bpy.app.online_access` is deliberately NOT checked: per the extensions
    # platform guidelines, "Allow Online Access" governs *connections to the
    # internet*, and this add-on makes none - it only listens on loopback.
    # The `network` permission stays declared in the manifest as disclosure
    # that a (localhost) socket is opened.

    props = scene.darkly_stream
    hub = server.FrameHub()
    try:
        srv = server.start_server("127.0.0.1", props.port, hub)
    except OSError as exc:
        _set_status(f"Could not bind port {props.port}: {exc}")
        return str(exc)

    _hub = hub
    _server = srv
    _capture = capture.SOURCES[props.source]()
    _encoder = encode.FrameEncoder(
        compression=props.compression, ocio_config_path=_ocio_config_path()
    )
    _running = True
    _last_signature = None
    _needs_render = True
    _harvest_owed = False

    # Start the encode worker (drains the main-thread -> worker handoff slot).
    _worker_stop = False
    _pending_frame = None
    _worker_thread = threading.Thread(
        target=_encode_worker, name="darkly-encode", daemon=True
    )
    _worker_thread.start()

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if not bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.register(_timer_tick)

    # The capture runs here (live GPU context); the timer only requests it. The
    # event type is the source's own - where in the draw the capture must run.
    _capture_pending = False
    if _draw_handler is None:
        _draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _capture_draw_handler, (), "WINDOW", _capture.draw_handler_type
        )

    _set_status(f"Streaming on http://127.0.0.1:{props.port}/stream")
    return None


def stop_stream():
    """Tear down the timer, worker, server, and GPU resources. Idempotent."""
    global _hub, _server, _capture, _encoder, _running, _worker_thread, _worker_stop
    global _draw_handler, _capture_pending
    _running = False
    _capture_pending = False

    if bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.unregister(_timer_tick)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    if _draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_draw_handler, "WINDOW")
        _draw_handler = None

    # Wake the worker so it observes the stop flag and exits, then join it.
    with _encode_cond:
        _worker_stop = True
        _encode_cond.notify_all()
    if _worker_thread is not None:
        _worker_thread.join(timeout=2.0)
        _worker_thread = None

    server.stop_server(_server)
    _server = None
    if _capture is not None:
        _capture.free()
        _capture = None
    if _encoder is not None:
        _encoder.free()
        _encoder = None
    _hub = None
    _set_status("Stopped")


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
    port: bpy.props.IntProperty(
        name="Port", default=8765, min=1, max=65535,
        description="localhost port the frame stream is served on",
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
    only a viewport draw callback provides (see `_capture_draw_handler`), so
    each sample is taken inside a temporary draw handler (of the source's own
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
        if capture.find_view3d()[0] is None:
            self.report({"ERROR"}, "Open a 3D viewport to benchmark")
            return {"CANCELLED"}

        self._scene = scene
        self._props = props
        self._enc = encode.FrameEncoder(
            compression=props.compression, ocio_config_path=_ocio_config_path()
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
        _request_redraw()
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
            _request_redraw()
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
