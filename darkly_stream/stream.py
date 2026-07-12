"""Stream runtime - lifecycle, pacing, capture handoff, and crash containment.

Threading / context model: GPU capture needs a live GPU context, which only a
viewport draw callback provides - a `bpy.app.timers` tick has none. So a timer
paces the stream and, when a frame is due, tags the viewport for redraw; a draw
handler (registered under the event the active source needs - `'PRE_VIEW'` or
`'POST_PIXEL'`) then does the capture on the main thread inside that live
context and hands the raw pixels to a worker thread. The worker does the
(bpy-free) display transform + PNG encode and publishes to the server. Keeping
that work off the main thread is what makes the add-on cheap enough to leave
Blender responsive. The stdlib HTTP server serves the bytes from its own handler
threads. Handoff goes through `server.FrameHub` (worker -> clients) and a
single-slot latest-frame handoff (main -> worker).

Change detection is redraw-observed, not signature-gated. Blender redraws the
streamed viewport for every event that changes the output - each Cycles
progressive-refinement pass, a shading switch, a view move, a depsgraph edit -
and that redraw is the honest "something might have changed" signal (a cheap
signature misses in-place refinement, which is what used to freeze the stream
at the first low-quality pass). The draw handler, when it isn't servicing a
requested capture, just sets `redraw_seen`; the tick turns that (type-owned via
`capture.is_dirty`) into a paced capture. See `pacing.plan_capture` for the
per-tick decision.

De-duplication is a single raw-buffer compare on the worker, *before* the
expensive encode (`encode.frame_is_duplicate`): raw-identical pixels under
identical `ViewSettings` encode to identical PNG bytes, so a redundant frame
(an incidental hover redraw, a converged scene's trailing harvest) is dropped
without an encode, while every distinct-looking frame is published. The
transport still coalesces on `FrameHub` seq and the frontend decodes only new
frames, but those are downstream of this compare, not the primary gate.

Trailing harvest: the viewport source reads the *previous* completed frame
(see `capture.py`), so after the last change one extra capture is owed to
harvest the final state - `harvest_owed` below, armed and spent in
`pacing.plan_capture` so a converged scene stops instead of redrawing forever.
That harvest is main-thread and dedup-independent: it is owed and spent by the
tick regardless of whether the worker later drops the frame as a duplicate.

Failure model: every Blender-invoked entry point is exception-contained, and
failure in any of them converges on `StreamRuntime.fail` - full teardown (the
port MUST come back; see `lifecycle.run_guarded`) plus an error status in the
panel. The teardown context matters:

- The timer tick is the only safe place to tear down from: it may unregister
  the draw handler, and stopping from inside it returns `None` - Blender's own
  unregistration path - instead of calling `timers.unregister` on the
  currently-executing callback.
- The draw handler and encode worker must NOT tear down from inside themselves
  (a draw handler can't remove itself mid-draw; the worker would join itself).
  They log immediately and record the exception in `pending_failure`; the next
  tick consumes it and fails properly.
- The tick also watchdogs the worker and server threads, so a thread that died
  without leaving a record still surfaces as a loud failure instead of a
  silently frozen stream.
"""

import logging
import os
import threading
import time

import bpy

from bpy.app.handlers import persistent

from . import server, capture, encode, pacing
from .lifecycle import run_guarded

log = logging.getLogger(__name__)


def request_redraw():
    """Tag every open 3D viewport for redraw - fires the capture draw handler
    and repaints the panel's status text."""
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def ocio_config_path():
    """Blender's bundled OCIO config, for the worker-side display transform.
    `None` (-> sRGB fallback) if the layout ever changes."""
    path = os.path.join(
        bpy.utils.system_resource("DATAFILES"), "colormanagement", "config.ocio"
    )
    return path if os.path.exists(path) else None


@persistent
def _on_depsgraph_update(_scene, _depsgraph):
    # Any scene edit (transform, material, geometry) marks the next tick dirty.
    # The viewport source would also redraw for such an edit, but the camera
    # source (which skips on its own signature) relies on this to re-render when
    # a depsgraph edit leaves the camera pose and shading unchanged.
    # Module-level (not a bound method): `@persistent` handlers are registered
    # once and survive across runtimes.
    if _runtime is not None:
        _runtime.needs_render = True


class StreamRuntime:
    """Everything one running stream owns: server, capture source, encoder,
    worker thread, Blender handler registrations, and pacing/dedup state.
    One per process (see the module-level singleton below)."""

    def __init__(self):
        # `bpy.app.timers` and `draw_handler_add`/`_remove` identify callbacks
        # by object identity, but a bound method is a fresh object on every
        # attribute access - bind each exactly once so register and unregister
        # always see the same object.
        self._timer_fn = self._timer_tick
        self._draw_fn = self._draw_handler_cb

        self.hub = None
        self.srv = None
        self.cap = None
        self.encoder = None
        self.running = False
        # Set by `fail` (and a failed bind); keeps the error status pinned in
        # the panel until the next successful start.
        self.failed = False
        self.status = "Stopped"

        # Pacing / dirtiness (see module docstring).
        # `redraw_seen` is the redraw-observer bit: the draw handler sets it
        # (main thread) on any external redraw of the streamed viewport; the
        # tick reads and clears it. `needs_render` is set by the depsgraph
        # handler for edits and cleared when the tick commits to a capture.
        self.redraw_seen = False
        self.needs_render = True
        self.harvest_owed = False
        self.last_draw_ms = 0.0
        self.last_encode_ms = 0.0

        # Timer -> draw-handler handoff: the tick requests a capture and the
        # handler for the selected viewport services it.
        self.draw_handler = None
        self.capture_pending = False
        # `as_pointer()` of the SpaceView3D the timer resolved for this frame.
        # The draw handler fires for *every* redrawing viewport; it only
        # services the request when running for this one, so the signature and
        # the captured pixels always come from the same viewport.
        self.target_space = None

        # Main-thread -> worker handoff: a single latest-frame slot (stale
        # frames are dropped, never queued) plus the worker that drains it.
        self.encode_cond = threading.Condition()
        self.pending_frame = None  # (width, height, rgba, view_settings) | None
        self.worker_thread = None
        self.worker_stop = False

        # An exception recorded by the draw handler or encode worker - contexts
        # that must not tear down from inside themselves - consumed by the next
        # timer tick, the one safe place to fail from.
        self.pending_failure = None

    # --- status ---

    def set_status(self, text):
        if text == self.status:
            return  # only repaint on an actual change, never every tick
        self.status = text
        request_redraw()

    def has_live_resources(self):
        """Whether any teardown-relevant resource is still held. The panel
        keeps Stop reachable while this is true, so a half-failed stop can
        always be retried instead of restarting Blender."""
        return (
            self.srv is not None
            or self.draw_handler is not None
            or self.worker_thread is not None
            or self.cap is not None
            or self.encoder is not None
        )

    # --- lifecycle ---

    def start(self, scene):
        """Bind the server and spin up the pipeline. Returns an error string,
        or `None` on success. The port bind comes first so its (common,
        user-fixable) failure allocates nothing else."""
        if self.running:
            return None

        # `bpy.app.online_access` is deliberately NOT checked: per the
        # extensions platform guidelines, "Allow Online Access" governs
        # *connections to the internet*, and this add-on makes none - it only
        # listens for incoming connections. The manifest declares no `network`
        # permission for the same reason (per the manifest spec it means
        # internet access).

        props = scene.darkly_stream
        hub = server.FrameHub()
        try:
            srv = server.start_server(server.bind_host(props.listen_all), props.port, hub)
        except OSError as exc:
            self.failed = True
            self.set_status(f"Could not bind port {props.port}: {exc}")
            return str(exc)

        self.hub = hub
        self.srv = srv
        self.cap = capture.SOURCES[props.source]()
        self.encoder = encode.FrameEncoder(
            compression=props.compression, ocio_config_path=ocio_config_path()
        )
        self.running = True
        self.failed = False
        self.pending_failure = None
        self.redraw_seen = False
        self.needs_render = True
        self.harvest_owed = False
        self.capture_pending = False
        self.target_space = None

        # Start the encode worker (drains the main-thread -> worker slot).
        self.worker_stop = False
        self.pending_frame = None
        self.worker_thread = threading.Thread(
            target=self._encode_worker, name="darkly-encode", daemon=True
        )
        self.worker_thread.start()

        if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
            bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
        if not bpy.app.timers.is_registered(self._timer_fn):
            bpy.app.timers.register(self._timer_fn)

        # The capture runs here (live GPU context); the timer only requests it.
        # The event type is the source's own - where in the draw the capture
        # must run.
        if self.draw_handler is None:
            self.draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                self._draw_fn, (), "WINDOW", self.cap.draw_handler_type
            )

        url = f"http://127.0.0.1:{props.port}/stream"
        self.set_status(
            f"Streaming on {url} (all interfaces)" if props.listen_all else f"Streaming on {url}"
        )
        return None

    def stop(self, unregister_timer=True):
        """Tear down the timer, handlers, worker, server, and GPU resources.
        Every step runs even if an earlier one raises (`run_guarded`) - in
        particular the server stop, so the port always comes back. Idempotent.

        `unregister_timer=False` is for stopping from inside the tick itself:
        the tick then returns `None`, Blender's own unregistration path, so
        `timers.unregister` is never called on the currently-executing
        callback."""
        self.running = False
        self.capture_pending = False
        self.target_space = None

        def unregister_tick_timer():
            if unregister_timer and bpy.app.timers.is_registered(self._timer_fn):
                bpy.app.timers.unregister(self._timer_fn)

        def remove_depsgraph_handler():
            if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
                bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)

        def remove_draw_handler():
            if self.draw_handler is not None:
                bpy.types.SpaceView3D.draw_handler_remove(self.draw_handler, "WINDOW")
                self.draw_handler = None

        def join_worker():
            # Wake the worker so it observes the stop flag and exits, then join.
            with self.encode_cond:
                self.worker_stop = True
                self.encode_cond.notify_all()
            if self.worker_thread is not None:
                self.worker_thread.join(timeout=2.0)
                self.worker_thread = None

        def stop_server():
            # The port release - the one step that must never be skippable.
            server.stop_server(self.srv)
            self.srv = None

        def free_capture():
            if self.cap is not None:
                self.cap.free()
                self.cap = None

        def free_encoder():
            if self.encoder is not None:
                self.encoder.free()
                self.encoder = None

        def clear_refs():
            self.hub = None
            self.pending_frame = None

        run_guarded(
            [
                ("unregister timer", unregister_tick_timer),
                ("remove depsgraph handler", remove_depsgraph_handler),
                ("remove draw handler", remove_draw_handler),
                ("join encode worker", join_worker),
                ("stop server", stop_server),
                ("free capture source", free_capture),
                ("free encoder", free_encoder),
                ("clear refs", clear_refs),
            ],
            log,
        )
        if not self.failed:
            self.set_status("Stopped")

    def fail(self, exc, from_tick=False):
        """Contain a pipeline failure: log it, tear everything down (the port
        must come back), and pin an error status in the panel. `from_tick`
        defers timer unregistration to the tick's own `return None`."""
        log.error("darkly_stream failed, stopping stream", exc_info=exc)
        self.failed = True
        self.stop(unregister_timer=not from_tick)
        self.set_status(f"Error: {str(exc) or type(exc).__name__} (see console)")

    # --- Blender-invoked callbacks (every one exception-contained) ---

    def _timer_tick(self):
        try:
            return self._tick_body()
        except Exception as exc:  # noqa: BLE001 - containment boundary
            self.fail(exc, from_tick=True)
            return None

    def _tick_body(self):
        """Paces the stream: runs the dedup gate and, when a fresh frame is
        warranted, requests one and forces a viewport redraw (the draw handler
        does the actual GPU capture). Returns the next interval, or `None` to
        unregister once streaming stops."""
        if not self.running:
            return None

        # A failure recorded by the draw handler or encode worker since the
        # last tick - this is the safe context to tear down from.
        if self.pending_failure is not None:
            exc = self.pending_failure
            self.pending_failure = None
            self.fail(exc, from_tick=True)
            return None

        # Watchdog: a helper thread that died without leaving a record must
        # surface loudly, not as a silently frozen stream.
        if self.worker_thread is not None and not self.worker_thread.is_alive():
            raise RuntimeError("encode worker thread died")
        srv_thread = getattr(self.srv, "_thread", None)
        if srv_thread is not None and not srv_thread.is_alive():
            raise RuntimeError("stream server thread died")

        scene = bpy.context.scene
        props = scene.darkly_stream
        interval = 1.0 / max(1, props.fps)

        # Zero cost when nobody is connected: no capture, no readback, no
        # encode. The add-on must not touch Blender's frame budget while
        # Darkly isn't looking.
        if self.hub.client_count == 0:
            self.set_status("Streaming - no client connected")
            return interval

        err = self.cap.poll(scene, props)
        if err is not None:
            self.set_status(err)
            return interval

        space, region = capture.find_view3d(props.viewport)
        if space is None:
            self.set_status("Open a 3D viewport to stream")
            return interval
        self.target_space = space.as_pointer()

        # Steady-state status must be *stable*: `set_status` forces a redraw on
        # any text change, and the draw handler turns any external redraw into a
        # capture, so a per-capture status (fluctuating ms timings) would spin a
        # self-sustaining capture loop on a static scene. The timings live in
        # the Profile print instead.
        self.set_status(f"Streaming - {self.hub.client_count} client(s)")

        # Type-owned dirtiness: the viewport source is dirty on a redraw of the
        # streamed viewport, the camera source on its own signature change.
        # Consume the redraw bit each tick, whether or not we capture.
        redraw_seen = self.redraw_seen
        self.redraw_seen = False
        source_dirty = self.cap.is_dirty(scene, props, space, region, redraw_seen)

        decision = pacing.plan_capture(
            self.needs_render, source_dirty, self.harvest_owed, self.cap.needs_harvest
        )
        if not decision.capture:
            return interval

        # A live capture needs an active GPU context, which a timer tick
        # lacks. So the timer only *requests* a frame and forces a viewport
        # redraw; the draw handler runs during that redraw (valid GPU context)
        # and does the actual capture + worker handoff. The observer sets no
        # per-redraw gate, so that requested redraw reliably lands the capture -
        # a depsgraph edit can't be swallowed.
        if decision.is_change:
            # Advance the source's own skip (the camera signature) to the
            # captured state; a trailing harvest re-reads the same settled
            # state and must not.
            self.cap.mark_captured(scene, props, space, region)
        self.needs_render = decision.needs_render
        self.harvest_owed = decision.harvest_owed
        self.capture_pending = True
        request_redraw()
        return interval

    def _draw_handler_cb(self):
        try:
            self._draw_body()
        except Exception as exc:  # noqa: BLE001 - containment boundary
            # Log here (Blender printing draw-handler exceptions is not
            # guaranteed) and defer teardown to the next tick - a draw handler
            # must never remove itself from inside its own draw.
            log.exception("capture draw handler failed")
            self.pending_failure = exc

    def _draw_body(self):
        """Viewport draw callback - the GPU half and the redraw observer. Runs
        on the main thread *inside* a live GPU context (registered under the
        event the active source requires). Fires on *every* redraw of *every*
        viewport, so it first filters to the streamed one, then splits:

        - servicing the timer's requested capture (`capture_pending`): do the
          GPU capture and hand the raw pixels to the encode worker. This redraw
          was ours, so it must NOT set `redraw_seen`, or the timer's own
          capture-servicing redraw would re-trigger the next tick forever.
        - otherwise: record that the streamed viewport redrew
          (`redraw_seen = True`) - the honest change signal the tick reads."""
        if not self.running:
            return

        ctx = bpy.context
        space = ctx.space_data
        region = ctx.region
        if space is None or getattr(space, "type", None) != "VIEW_3D" or region is None:
            return
        # Not the viewport the timer selected - ignore its redraws entirely.
        if self.target_space is not None and space.as_pointer() != self.target_space:
            return

        if not self.capture_pending:
            # An external redraw of the streamed viewport (a Cycles refinement
            # pass, a shading switch, a view move, a hover). The tick turns this
            # into a capture; a raw compare drops it if nothing actually changed.
            self.redraw_seen = True
            return

        scene = ctx.scene
        props = scene.darkly_stream

        # Claim the request before capturing so a redraw storm can't double-capture.
        self.capture_pending = False

        t0 = time.perf_counter()
        result = self.cap.capture(scene, props, space, region)
        if result is None:
            return
        self.last_draw_ms = (time.perf_counter() - t0) * 1000.0

        # Hand the raw pixels to the worker (latest wins; a slow encode never
        # backs up the main thread - stale frames are simply overwritten).
        with self.encode_cond:
            self.pending_frame = result
            self.encode_cond.notify()

        if props.profile:
            print(
                f"[darkly_stream] capture {self.last_draw_ms:.1f}ms "
                f"encode {self.last_encode_ms:.1f}ms (worker) ({result[0]}x{result[1]})"
            )

    def _encode_worker(self):
        """Worker thread: drain the latest captured frame, drop it if it's a
        raw duplicate of the last published one, else transform + encode it
        (numpy/OCIO/OpenImageIO, no `bpy`) and publish to the server. Idles on
        the condition when there's nothing to do, so it costs nothing on a
        static scene.

        The dedup lives here (off the main thread) because it holds one ~15 MB
        buffer and runs a raw-pixel compare; the redraw-driven capture path is
        deliberately unconditional (progressive refinement is invisible to any
        cheap proxy), so the raw compare is what filters redundant frames -
        e.g. an incidental hover redraw, or a converged scene's trailing
        harvest. Harvest/termination decisions stay main-thread and never
        consult this outcome."""
        prev_rgba = None
        prev_view_settings = None
        try:
            while True:
                with self.encode_cond:
                    while self.pending_frame is None and not self.worker_stop:
                        self.encode_cond.wait()
                    if self.worker_stop:
                        return
                    width, height, rgba, view_settings = self.pending_frame
                    self.pending_frame = None

                if encode.frame_is_duplicate(
                    rgba, view_settings, prev_rgba, prev_view_settings
                ):
                    continue

                t0 = time.perf_counter()
                try:
                    frame = self.encoder.encode(width, height, rgba, view_settings)
                    self.last_encode_ms = (time.perf_counter() - t0) * 1000.0
                    self.hub.publish(frame)
                    prev_rgba = rgba
                    prev_view_settings = view_settings
                except Exception:  # noqa: BLE001 - a bad frame must not kill the worker
                    log.exception("encode error")
                    continue
        except Exception as exc:  # noqa: BLE001 - anything past the per-frame guard
            # Record for the next tick (which also watchdogs a dead worker);
            # the worker can't tear the runtime down from inside itself.
            log.exception("encode worker died")
            self.pending_failure = exc


# One stream per Blender process. The instance persists after a failure so the
# error status stays visible in the panel; the next `start_stream` reclaims any
# leftover resources and restarts it.
_runtime = None


def start_stream(scene):
    """Bind the server and register the capture pipeline. Returns an error
    string, or `None` on success."""
    global _runtime
    if _runtime is None:
        _runtime = StreamRuntime()
    if _runtime.running:
        return None
    # Reclaim anything a previous half-failed stop left behind before rebinding.
    if _runtime.has_live_resources():
        _runtime.stop()
    return _runtime.start(scene)


def stop_stream():
    """Tear down the running stream. Idempotent."""
    if _runtime is not None:
        _runtime.stop()


def is_running():
    return _runtime is not None and _runtime.running


def has_live_resources():
    return _runtime is not None and _runtime.has_live_resources()


def has_failed():
    return _runtime is not None and _runtime.failed


def status_text():
    return _runtime.status if _runtime is not None else "Stopped"
