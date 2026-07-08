# Darkly Stream (Blender add-on)

Stream the **active Blender camera view** into [Darkly](https://darkly.art) over
localhost, so you can paint over and composite with the live 3D viewport. The
camera view is served with a **transparent background**, so paint shows through
empty areas of the scene - sketch behind and around geometry, try lighting or
camera angles without re-rendering, and treat Blender as just another layer.

The feed is the **real-time viewport** (`draw_view3d`) from the camera's point of
view, not a Cycles or final render, so it streams at interactive rates.

This folder is self-contained and destined to become its own repository; it has
no build dependency on the Darkly core.

## How it works

Each tick, on Blender's main thread, the add-on:

1. Draws the viewport **from the active camera's point of view** into a
   `GPUOffScreen` with `draw_view3d(..., do_color_management=True,
   draw_background=False)`. This is the **real-time viewport draw** - the same
   GPU engine that paints your 3D viewport (Workbench / EEVEE), *not* a Cycles or
   final `render()` - so it runs in milliseconds, at the camera's framing, with a
   transparent, color-managed result (the same offscreen draw the official
   `doc/python_api/examples/gpu.9.py` demonstrates).
2. Reads it back as RGBA8 and hands the raw pixels to a **worker thread**.
3. The worker un-premultiplies to **straight alpha** and encodes a **PNG** with
   **OpenImageIO** (bundled with Blender, `bpy`-free so it runs off the main
   thread), then publishes it to a stdlib `ThreadingHTTPServer`, which serves it
   on `GET /stream` as a single long-lived **HTTP/1.1 chunked** response. Each
   frame is length-prefixed on the wire: `[4-byte big-endian length][PNG bytes]`.

   (PNG, not WebP: OIIO's WebP writer does a slow lossless encode; libpng at a low
   compression level is an order of magnitude faster and still carries alpha. On
   localhost the larger byte size is irrelevant.)

Only step 1 (the GPU draw + readback) runs on Blender's main thread; the encode
and serving are entirely off it, so the add-on stays cheap. On a static scene it
does **nothing** (see duplicate-frame suppression), and it does nothing at all
while no client is connected.

In Darkly, the **Blender void** (`Add Void → Blender`) `fetch`es that stream,
decodes each PNG frame straight into the same GPU texture path the camera and
screenshare voids use, and composites it - masks, blend modes, transform gizmo,
and all.

### It streams viewport shading, not a final render

`draw_view3d` draws the **viewport** shading (Solid / Material Preview / Rendered,
whatever the 3D viewport is set to) from the camera's POV, *not* a full
`render()`. That is deliberate and real-time: a final render per frame would be
far too slow for a live feed. Set the viewport's shading mode to what you want to
see in Darkly.

> One caveat: if you set the viewport to **Rendered** shading with **Cycles** as
> the engine, the viewport itself runs progressive Cycles and the stream will be
> slow. Solid and Material Preview (EEVEE) stay real-time.

### A 3D viewport must be open

A `bpy.app.timers` callback has no view context, so the add-on walks the open
windows for a `VIEW_3D` area and draws through it. **Keep at least one 3D
viewport open** while streaming; otherwise the panel shows "Open a 3D viewport to
stream" and nothing is published. Overlays and gizmos are suppressed during the
draw.

## Install

This is a **Blender extension** (not a legacy add-on), so it installs through the
extensions system. Build a proper extension zip and install it (needs `blender`
on your `PATH`), from inside this folder:

```bash
blender --command extension build --source-dir darkly_stream --output-dir /tmp
blender --command extension install-file --repo user_default --enable \
  /tmp/darkly_stream-0.1.0.zip
```

Then open Blender, press `N` in the 3D viewport, and go to the **Darkly** tab.

Or install by hand: **Edit → Preferences → Get Extensions → Install from Disk…**,
and pick the built `darkly_stream-0.1.0.zip`.

> **Allow Online Access must be on.** The stream is served over a localhost HTTP
> socket, so the extension declares the `network` permission and honors Blender's
> policy: with **Preferences → System → Allow Online Access** disabled (or Blender
> launched with `--offline-mode`), **Start** refuses and binds no port. Enable it
> to stream.

## Use

1. Set the port (default `8765`), camera (defaults to the scene camera),
   resolution, FPS, and PNG compression.
2. Click **Start**. The panel shows the stream URL and connected client count.
3. In Darkly: **Add Void → Blender**. The layer connects to
   `http://localhost:8765/stream` and shows the camera view live.
4. Move/animate the camera → Darkly updates. A **static** scene sends nothing
   (three-layer duplicate-frame suppression), so it idles at zero CPU.
5. Edit the void's `url` param in Darkly to point at a different port/host.

## Performance

The add-on is designed to stay out of Blender's way:

- **Nothing runs when idle.** No client connected, or camera/scene unchanged since
  the last frame -> the timer returns immediately (no draw, no readback, no encode).
- **Only the GPU draw + readback is on the main thread.** Encoding (the expensive
  part) is on a worker thread, so it never enters Blender's frame budget.
- **Resolution is the main lever** for the remaining main-thread cost - the
  draw+readback scales with pixel count. **FPS** caps how often that cost is paid
  while the camera is actually moving. Drop either if movement feels heavy.

Measure the real cost on your machine with the **Benchmark Capture** button in the
panel: it times the draw+readback and encode stages directly (no server or client
needed) and reports to the status bar and system console, e.g.

```
[darkly_stream] benchmark 30x 1280x720: draw+readback avg 24.8ms (max 66.9),
  encode avg 22.0ms (max 40.5), total 31.1ms/frame -> ~32 fps ceiling
```

(The benchmark runs the encode inline to time it; in normal streaming that encode
is off the main thread, so only the draw+readback figure lands on Blender.)

## Duplicate-frame suppression

- **Origin** (`__init__.py`): skip the draw+encode entirely when the camera
  pose, current frame, and scene haven't changed since the last publish (a
  `depsgraph_update_post` handler marks real scene edits dirty).
- **Transport** (`server.py`): a monotonic sequence + `threading.Condition`. The
  producer bumps the sequence and wakes clients only on a genuine new frame;
  each client waits while its sequence is current, so it never re-receives a
  frame and always gets the freshest one (stale intermediates dropped).
- **Sink** (Darkly's `HttpStreamSource`): decodes + uploads only on a new frame.

## Alpha

Straight alpha end-to-end. The offscreen readback is premultiplied (it blended
over a cleared `α=0` buffer); the add-on un-premultiplies with numpy and tags the
output `oiio:UnassociatedAlpha=1` so OpenImageIO's PNG writer stores straight
alpha, which matches Darkly's `premultiplied_alpha: false` sampling and the
frontend's `createImageBitmap(..., { premultiplyAlpha: 'none' })` decode.

> **Verify before trusting it:** getting straight-vs-premultiplied wrong is
> invisible over black. Composite a semi-transparent frame over a **non-black**
> background in Darkly and confirm there are no dark fringes on edges.

## Mixed content

A Darkly page served over `https://` reaching `http://localhost` is permitted in
Chromium (localhost is "potentially trustworthy") but inconsistent across
browsers. The server sends `Access-Control-Allow-Origin: *`. If your browser
blocks it, run Darkly from `http://localhost` or allow the localhost exception.

## Development

- **Requirements:** Blender 5.1+ (bundled Python + numpy + OpenImageIO; no
  external deps).
- **Server tests** (no Blender needed - `server.py` is pure stdlib):

  ```bash
  python3 -m unittest discover -s tests
  ```

- **Headless smoke test** (needs Blender): start the server, connect a client,
  and assert a decodable PNG frame arrives -

  Installed as an extension, the module lives under the `bl_ext` namespace:

  ```bash
  blender --background your_scene.blend --python-expr \
    "from bl_ext.user_default import darkly_stream; darkly_stream.start_stream(__import__('bpy').context.scene)"
  ```

  (Running straight from this source folder instead, it's just `import darkly_stream`.)

## Layout

```
darkly_stream/
  blender_manifest.toml  extension metadata (id, version, permissions, license)
  __init__.py   register/unregister, Scene props, capture timer + dedup, online-access gate
  panel.py      View3D N-panel: Start/Stop, port, resolution, fps, camera, quality
  capture.py    GPUOffScreen camera viewport draw (draw_background=False) + read_color
  encode.py     un-premultiply + PNG via OpenImageIO (bpy-free, runs off-thread)
  server.py     stdlib ThreadingHTTPServer HTTP/1.1 chunked stream + seq/Condition dedup
tests/
  test_server.py  stdlib server + dedup tests (no Blender)
```

## Credits

The offscreen camera draw follows Blender's official
`doc/python_api/examples/gpu.9.py`, and the framebuffer readback pattern follows
Blender's bundled `addons_core/io_mesh_uv_layout/export_uv_png.py`.

## License

AGPL-3.0-or-later - see [LICENSE](LICENSE).
