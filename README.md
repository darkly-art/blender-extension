# Darkly Stream (Blender add-on)

Stream a live view of Blender into [Darkly](https://darkly.art) over localhost,
so you can paint over and composite with the live 3D scene. The view is served
with a **transparent background**, so paint shows through empty areas - sketch
behind and around geometry, try lighting or angles without re-rendering, and
treat Blender as just another layer.

Two sources, chosen in the panel:

- **Viewport** (default) - streams **whatever the 3D viewport shows**, by
  reusing the pixels Blender already rendered. Zero extra rendering; orbiting,
  panning, and zooming stream live. To stream a camera's framing, just look
  through the camera (numpad 0).
- **Camera** - streams a **camera's point of view** regardless of where the
  viewport is looking, by rendering the scene once more per frame into an
  offscreen buffer.

Both are the **real-time viewport draw** (Workbench / EEVEE viewport shading),
not a Cycles or final render, so they stream at interactive rates.

## How it works

A timer paces the stream and dedups; a viewport draw callback does the GPU work
on Blender's main thread; a worker thread does everything else.

**Viewport source.** Blender keeps each 3D viewport's scene render in its own
GPU texture, *separate* from overlays and the theme background (those live in a
second texture, composited only when blitting to the screen). During a viewport
redraw that render texture's framebuffer is bound - with only depth cleared -
right before the engines draw, which is exactly when Python `'PRE_VIEW'` draw
callbacks run. So the add-on reads the **previous completed frame** from the
active framebuffer there: transparent background and no overlays *by
construction*, one frame of latency, and **no extra rendering** - captures
piggyback on redraws Blender performs anyway (after the last change, one
trailing redraw is forced to harvest the settled frame). The pixels come back
scene-linear (the display transform is normally applied at blit time), so the
worker applies the same OCIO display transform the viewport uses - view
transform, look, exposure, gamma, per the shading mode - via Blender's bundled
`PyOpenColorIO` and OCIO config. (Not replicated: RENDERED-mode curve mapping
and dither.)

**Camera source.** Draws the camera's view into a `GPUOffScreen` with
`draw_view3d(..., do_color_management=True, draw_background=False)` - the same
offscreen draw the official `doc/python_api/examples/gpu.9.py` demonstrates -
then reads it back as display-referred RGBA8. This renders the scene once more
per streamed frame. Overlays and gizmos are suppressed during the draw and the
user's setting restored.

**Both sources** hand the raw pixels to a **worker thread**, which
un-premultiplies to **straight alpha** (and, for the viewport source, applies
the display transform), encodes a **PNG** with **OpenImageIO** (bundled with
Blender, `bpy`-free so it runs off the main thread), and publishes it to a
stdlib `ThreadingHTTPServer`, which serves it on `GET /stream` as a single
long-lived **HTTP/1.1 chunked** response. Each frame is length-prefixed on the
wire: `[4-byte big-endian length][PNG bytes]`.

(PNG, not WebP: OIIO's WebP writer does a slow lossless encode; libpng at a low
compression level is an order of magnitude faster and still carries alpha. On
localhost the larger byte size is irrelevant.)

Only the capture (a framebuffer readback, plus the offscreen draw for the
camera source) runs on Blender's main thread; the transform, encode, and
serving are entirely off it, so the add-on stays cheap. On a static scene it
does **nothing** (see duplicate-frame suppression), and it does nothing at all
while no client is connected.

In Darkly, the **Blender void** (`Add Void → Blender`) `fetch`es that stream,
decodes each PNG frame straight into the same GPU texture path the camera and
screenshare voids use, and composites it - masks, blend modes, transform gizmo,
and all.

### It streams viewport shading, not a final render

Both sources stream the **viewport** shading (Wireframe / Solid / Material
Preview / Rendered, whatever the 3D viewport is set to), *not* a full
`render()`. That is deliberate and real-time: a final render per frame would be
far too slow for a live feed. Set the viewport's shading mode to what you want
to see in Darkly.

> One caveat: if you set the viewport to **Rendered** shading with **Cycles** as
> the engine, the viewport itself runs progressive Cycles and the stream will be
> slow. Solid and Material Preview (EEVEE) stay real-time.

### A 3D viewport must be open

A `bpy.app.timers` callback has no view context, so the add-on walks the open
windows for a `VIEW_3D` area and captures through it. **Keep at least one 3D
viewport open** while streaming; otherwise the panel shows "Open a 3D viewport
to stream" and nothing is published.

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

> **Works offline.** The extension declares the `network` permission because it
> opens a (loopback-only) HTTP socket, but it does not gate on Blender's
> **Allow Online Access** setting: per the extensions platform guidelines, that
> setting means "not making connections to the internet when
> `bpy.app.online_access` is False" - and this add-on makes no internet
> connections; it only listens on `127.0.0.1`.

## Use

1. Pick the **Source** - *Viewport* to stream what you see, *Camera* for a
   fixed camera framing (the camera defaults to the scene camera). Set the
   port (default `8765`), FPS, and PNG compression.
2. Click **Start**. The panel shows the stream URL and connected client count.
3. In Darkly: **Add Void → Blender**. The layer connects to
   `http://localhost:8765/stream` and shows the view live.
4. Navigate the viewport / move the camera / edit the scene → Darkly updates.
   A **static** scene sends nothing (three-layer duplicate-frame suppression),
   so it idles at zero CPU.
5. Edit the void's `url` param in Darkly to point at a different port/host.

## Performance

The add-on is designed to stay out of Blender's way:

- **Nothing runs when idle.** No client connected, or view/scene unchanged since
  the last frame -> the timer returns immediately (no capture, no encode).
- **The viewport source never re-renders.** Its main-thread cost is one
  framebuffer readback per streamed frame; the scene renders exactly as often
  as Blender would redraw it anyway. The camera source renders the scene once
  more per frame on top of that.
- **Only the capture is on the main thread.** The display transform and encode
  (the expensive parts) are on a worker thread, so they never enter Blender's
  frame budget.
- **Resolution is the main lever** for the remaining main-thread cost - the
  readback scales with pixel count (and it is the viewport's own size). **FPS**
  caps how often that cost is paid while the view is actually changing.

Measure the real cost on your machine with the **Benchmark Capture** button in
the panel: it times the capture and encode stages for the *selected source*
directly (no server or client needed) and reports to the status bar and system
console, e.g.

```
[darkly_stream] benchmark 30x 1280x720: capture avg 3.1ms (max 6.9),
  encode avg 22.0ms (max 40.5), total 25.1ms/frame -> ~40 fps ceiling
```

(The benchmark runs the encode inline to time it; in normal streaming that
encode is off the main thread, so only the capture figure lands on Blender.)

## Duplicate-frame suppression

- **Origin** (`__init__.py`): skip the capture+encode entirely when the source's
  signature - the viewport's `perspective_matrix`, or the camera pose - plus the
  current frame and scene haven't changed since the last publish (a
  `depsgraph_update_post` handler marks real scene edits dirty). The viewport
  source reads one frame behind, so after the last change a single trailing
  redraw is forced to harvest the settled frame; then everything idles.
- **Transport** (`server.py`): a monotonic sequence + `threading.Condition`. The
  producer bumps the sequence and wakes clients only on a genuine new frame;
  each client waits while its sequence is current, so it never re-receives a
  frame and always gets the freshest one (stale intermediates dropped).
- **Sink** (Darkly's `HttpStreamSource`): decodes + uploads only on a new frame.

## Alpha

Straight alpha end-to-end. Captures are premultiplied (they blended over a
cleared `α=0` buffer); the worker un-premultiplies with numpy and tags the
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

- **Requirements:** Blender 5.1+ (bundled Python + numpy + OpenImageIO +
  PyOpenColorIO; no external deps).
- **Unit tests** (no Blender needed - the server, encode, readback, color
  management, and signature logic are all `bpy`-free):

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
  __init__.py     register/unregister, Scene props, pacing timer + dedup + harvest
  panel.py        View3D N-panel: Start/Stop, source, port, fps, camera, quality
  capture.py      the two capture sources: viewport framebuffer readback (PRE_VIEW)
                  and GPUOffScreen camera draw (POST_PIXEL)
  colormanage.py  scene-linear -> display transform via PyOpenColorIO (bpy-free)
  readback.py     gpu.Buffer -> numpy (bpy-free)
  encode.py       un-premultiply + display transform + PNG via OpenImageIO (bpy-free)
  server.py       stdlib ThreadingHTTPServer HTTP/1.1 chunked stream + seq/Condition dedup
tests/
  test_server.py           stdlib server + dedup tests (no Blender)
  test_capture_sources.py  source signatures / polling (bpy stubbed)
  test_colormanage.py      shading-mode branch, sRGB fallback, OCIO parity
  test_encode_roundtrip.py uint8 + float paths, alpha, orientation
  test_readback.py         Buffer flatten-first regression
```

## Credits

The viewport-source mechanism (reading the previous frame's scene render from
the framebuffer bound during `'PRE_VIEW'` callbacks) is based on reading
Blender's draw-manager source: `draw_context.cc` (`drw_callbacks_pre_scene`),
`gpu_viewport.cc`, and the overlay engine's background pass
(`overlay_background_frag.glsl`). The offscreen camera draw follows Blender's
official `doc/python_api/examples/gpu.9.py`, and the framebuffer readback
pattern follows Blender's bundled `addons_core/io_mesh_uv_layout/export_uv_png.py`.

## License

AGPL-3.0-or-later - see [LICENSE](LICENSE).
