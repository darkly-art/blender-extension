"""Stream runtime - lifecycle, pacing, capture handoff, and crash containment.

Subprocess / context model: the serve/encode pipeline runs in a **helper
subprocess** (`helper.py`), launched by `bridge.HelperProcess` with Blender's
bundled Python and talking over its own stdin (frames in) and stdout (events
out). The Blender process itself is **main-thread only** - it imports no
`threading` and no `queue` (the reviewer's hard constraint). What stays here:

GPU capture needs a live GPU context, which only a viewport draw callback
provides - a `bpy.app.timers` tick has none. So a timer paces the stream and,
when a frame is due, tags the viewport for redraw; a draw handler (registered
under the event the active source needs - `'PRE_VIEW'` or `'POST_PIXEL'`) then
does the capture on the main thread inside that live context, drops raw
duplicates (`dedup`), and hands the raw pixels to the helper as a zero-copy
memoryview over its non-blocking stdin pipe (`bridge.send_frame`). The helper
does the (bpy-free) display transform + PNG encode and serves the bytes over
HTTP. Pipe I/O is non-blocking and pumped from the timer tick and the draw
handler (`bridge.pump`), so nothing parks Blender's main thread.

Change detection is redraw-observed, not signature-gated. Blender redraws the
streamed viewport for every event that changes the output - each Cycles
progressive-refinement pass, a shading switch, a view move, a depsgraph edit -
and that redraw is the honest "something might have changed" signal (a cheap
signature misses in-place refinement, which is what used to freeze the stream
at the first low-quality pass). The draw handler, when it isn't servicing a
requested capture, just sets `redraw_seen`; the tick turns that (type-owned via
`capture.is_dirty`) into a paced capture. See `pacing.plan_capture` for the
per-tick decision.

De-duplication is a single raw-buffer compare on the main thread, *before* the
frame is sent (`dedup.frame_is_duplicate`): raw-identical pixels under identical
`ViewSettings` encode to identical PNG bytes, so a redundant frame (an
incidental hover redraw, a converged scene's trailing harvest) is dropped
without a ~15 MB pipe transfer or an encode, while every distinct-looking frame
is sent. The transport still coalesces on the helper's `FrameHub` seq and the
frontend decodes only new frames, but those are downstream of this compare, not
the primary gate. `prev_rgba`/`prev_view_settings` hold the last sent frame; a
helper `encode_error` (the frame was never published) clears them so the next
identical capture isn't wrongly dropped.

Trailing harvest: the viewport source reads the *previous* completed frame
(see `capture.py`), so after the last change one extra capture is owed to
harvest the final state - `harvest_owed` below, armed and spent in
`pacing.plan_capture` so a converged scene stops instead of redrawing forever.
That harvest is main-thread and dedup-independent: it is owed and spent by the
tick regardless of whether the frame is later dropped as a duplicate.

Failure model: every Blender-invoked entry point is exception-contained, and
failure in any of them converges on `StreamRuntime.fail` - full teardown (the
port MUST come back; see `lifecycle.run_guarded`) plus an error status in the
panel. Closing the helper's stdin gives it EOF, so it ends its HTTP responses
and exits, freeing the port; a Blender crash produces the same EOF. The
teardown context matters:

- The timer tick is the only safe place to tear down from: it may unregister
  the draw handler, and stopping from inside it returns `None` - Blender's own
  unregistration path - instead of calling `timers.unregister` on the
  currently-executing callback.
- The draw handler must NOT tear down from inside itself (it can't remove
  itself mid-draw). It logs immediately and records the exception in
  `pending_failure`; the next tick consumes it and fails properly. A broken
  pipe there sets a dead flag rather than raising, so the draw never tears down.
- The tick watchdogs the helper (`alive`, `stalled`), so a child that died or
  wedged without leaving a record still surfaces as a loud failure instead of a
  silently frozen stream.
"""

import logging
import os

import bpy

from bpy.app.handlers import persistent

from . import capture, pacing, bridge, dedup
from .lifecycle import run_guarded

log = logging.getLogger(__name__)

# The helper subprocess entry point, launched with `sys.executable`.
_HELPER_PATH = os.path.join(os.path.dirname(__file__), "helper.py")


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
    """Blender's bundled OCIO config, for the helper-side display transform.
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
    """Everything one running stream owns: the helper subprocess, capture
    source, Blender handler registrations, and pacing/dedup state. One per
    process (see the module-level singleton below)."""

    def __init__(self):
        # `bpy.app.timers` and `draw_handler_add`/`_remove` identify callbacks
        # by object identity, but a bound method is a fresh object on every
        # attribute access - bind each exactly once so register and unregister
        # always see the same object.
        self._timer_fn = self._timer_tick
        self._draw_fn = self._draw_handler_cb

        self.helper = None
        self.cap = None
        self.running = False
        # Set by `fail` (and a failed spawn/bind); keeps the error status pinned
        # in the panel until the next successful start.
        self.failed = False
        self.status = "Stopped"

        # Cached connected-client count, updated from the helper's `clients`
        # events. Gates the whole capture pipeline (zero cost with no client).
        self.client_count = 0

        # Pacing / dirtiness (see module docstring).
        # `redraw_seen` is the redraw-observer bit: the draw handler sets it
        # (main thread) on any external redraw of the streamed viewport; the
        # tick reads and clears it. `needs_render` is set by the depsgraph
        # handler for edits and cleared when the tick commits to a capture.
        self.redraw_seen = False
        self.needs_render = True
        self.harvest_owed = False

        # Timer -> draw-handler handoff: the tick requests a capture and the
        # handler for the selected viewport services it.
        self.draw_handler = None
        self.capture_pending = False
        # `as_pointer()` of the SpaceView3D the timer resolved for this frame.
        # The draw handler fires for *every* redrawing viewport; it only
        # services the request when running for this one, so the signature and
        # the captured pixels always come from the same viewport.
        self.target_space = None

        # Last sent frame, for the pre-send raw-duplicate compare. Cleared on a
        # helper `encode_error` (that frame was never published).
        self.prev_rgba = None
        self.prev_view_settings = None

        # An exception recorded by the draw handler - a context that must not
        # tear down from inside itself - consumed by the next timer tick, the
        # one safe place to fail from.
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
            self.helper is not None
            or self.draw_handler is not None
            or self.cap is not None
        )

    # --- lifecycle ---

    def start(self, scene):
        """Spawn the helper and register the capture pipeline. Returns an error
        string, or `None` on success. The helper binds the port first so its
        (common, user-fixable) failure surfaces synchronously and allocates
        nothing else here."""
        if self.running:
            return None

        # `bpy.app.online_access` is deliberately NOT checked: per the
        # extensions platform guidelines, "Allow Online Access" governs
        # *connections to the internet*, and this add-on makes none - it only
        # listens for incoming connections. The manifest declares no `network`
        # permission for the same reason (per the manifest spec it means
        # internet access).

        props = scene.darkly
        self.helper = bridge.HelperProcess()
        err = self.helper.spawn(
            _HELPER_PATH,
            bridge.bind_host(props.listen_all),
            props.port,
            props.compression,
            ocio_config_path(),
        )
        if err is not None:
            self.failed = True
            self.set_status(err)
            self.helper.close()
            self.helper = None
            return err

        self.cap = capture.SOURCES[props.source]()
        self.running = True
        self.failed = False
        self.pending_failure = None
        self.redraw_seen = False
        self.needs_render = True
        self.harvest_owed = False
        self.capture_pending = False
        self.target_space = None
        self.client_count = 0
        self.prev_rgba = None
        self.prev_view_settings = None

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
        if self.helper.bound:
            self.set_status(
                f"Streaming on {url} (all interfaces)"
                if props.listen_all else f"Streaming on {url}"
            )
        else:
            # Cold-spawning child (interpreter + numpy import); the tick flips to
            # the streaming status once the `bound` event arrives.
            self.set_status("Starting…")
        return None

    def stop(self, unregister_timer=True):
        """Tear down the timer, handlers, helper, and GPU resources. Every step
        runs even if an earlier one raises (`run_guarded`) - in particular the
        helper close, so the port always comes back. Idempotent.

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

        def close_helper():
            # The port release - the one step that must never be skippable.
            # Closing stdin gives the helper EOF; it ends its responses and
            # exits (terminate/kill fallback inside `close`).
            if self.helper is not None:
                self.helper.close()
                self.helper = None

        def free_capture():
            if self.cap is not None:
                self.cap.free()
                self.cap = None

        def clear_refs():
            self.prev_rgba = None
            self.prev_view_settings = None

        run_guarded(
            [
                ("unregister timer", unregister_tick_timer),
                ("remove depsgraph handler", remove_depsgraph_handler),
                ("remove draw handler", remove_draw_handler),
                ("close helper", close_helper),
                ("free capture source", free_capture),
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
        log.error("darkly failed, stopping stream", exc_info=exc)
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
        """Paces the stream: drains helper events, runs the dedup gate and, when
        a fresh frame is warranted, requests one and forces a viewport redraw
        (the draw handler does the actual GPU capture). Returns the next
        interval, or `None` to unregister once streaming stops."""
        if not self.running:
            return None

        # A failure recorded by the draw handler since the last tick - this is
        # the safe context to tear down from.
        if self.pending_failure is not None:
            exc = self.pending_failure
            self.pending_failure = None
            self.fail(exc, from_tick=True)
            return None

        scene = bpy.context.scene
        props = scene.darkly
        interval = 1.0 / max(1, props.fps)

        # Drain the helper: write progress on any in-flight frame + parse events.
        for evt in self.helper.pump():
            kind = evt.get("event")
            if kind == "bind_error":
                raise RuntimeError(
                    bridge.bind_error_message(props.port, evt.get("error", "bind failed"))
                )
            if kind == "clients":
                self.client_count = evt.get("count", 0)
            elif kind == "encode_error":
                # The frame was never published, so the dedup key must forget it
                # or the next identical capture would be dropped forever.
                self.prev_rgba = None
                self.prev_view_settings = None
            elif kind == "fatal":
                raise RuntimeError(f"helper: {evt.get('error', 'fatal')}")

        # Watchdogs: a child that died or wedged (mid-transfer, or a handshake
        # that never resolved) must surface loudly, not as a frozen stream.
        if not self.helper.alive():
            raise RuntimeError("helper process died")
        if self.helper.stalled(5.0):
            raise RuntimeError("helper process stalled")

        # Still waiting on the HTTP bind (cold child); capture stays gated.
        if self.helper.handshake_pending:
            self.set_status("Starting…")
            return interval

        # Zero cost when nobody is connected: no capture, no readback, no
        # encode. The add-on must not touch Blender's frame budget while
        # Darkly isn't looking.
        if self.client_count == 0:
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
        # self-sustaining capture loop on a static scene.
        self.set_status(f"Streaming - {self.client_count} client(s)")

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
        # and does the actual capture + helper handoff. The observer sets no
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
          GPU capture, drop a raw duplicate, else hand the raw pixels to the
          helper. This redraw was ours, so it must NOT set `redraw_seen`, or the
          timer's own capture-servicing redraw would re-trigger the next tick
          forever.
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
        props = scene.darkly

        # Claim the request before capturing so a redraw storm can't double-capture.
        self.capture_pending = False

        result = self.cap.capture(scene, props, space, region)
        if result is None:
            return

        width, height, rgba, view_settings = result

        # Pre-send raw duplicate gate: a redundant frame never touches the pipe
        # or the encoder. Runs here, on the main thread, before serialization.
        if dedup.frame_is_duplicate(
            rgba, view_settings, self.prev_rgba, self.prev_view_settings
        ):
            return

        # Hand the raw pixels to the helper (latest wins; a slow encode never
        # backs up the main thread - stale frames are simply overwritten). The
        # pump pushes the bytes now; a broken pipe sets a dead flag the tick
        # watchdog notices rather than raising mid-draw.
        self.helper.send_frame(width, height, rgba, view_settings)
        self.helper.pump()
        self.prev_rgba = rgba
        self.prev_view_settings = view_settings


# One stream per Blender process. The instance persists after a failure so the
# error status stays visible in the panel; the next `start_stream` reclaims any
# leftover resources and restarts it.
_runtime = None


def start_stream(scene):
    """Spawn the helper and register the capture pipeline. Returns an error
    string, or `None` on success."""
    global _runtime
    if _runtime is None:
        _runtime = StreamRuntime()
    if _runtime.running:
        return None
    # Reclaim anything a previous half-failed stop left behind before restarting.
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
