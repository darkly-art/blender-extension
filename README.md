# Darkly Blender Extension

[![Discord](https://img.shields.io/discord/1495886270780539021?label=Discord&logo=discord&logoColor=white&style=for-the-badge&color=9500ff)](https://discord.gg/kFz2FGhbpu)
[![Darkly](https://img.shields.io/badge/GitHub-Darkly-orange?logo=github&logoColor=white&style=for-the-badge&color=6914ff)](https://github.com/darkly-art/darkly)

![Blender](https://img.shields.io/badge/Blender_5.1+-000000?style=for-the-badge&logo=blender&logoColor=9500ff)
![Python](https://img.shields.io/badge/Python-000000?style=for-the-badge&logo=python&logoColor=6914ff)
[![CI](https://img.shields.io/github/actions/workflow/status/darkly-art/blender-extension/ci.yml?branch=master&label=CI&logo=github&labelColor=black&logoColor=4400ff&style=for-the-badge&color=4400ff)](https://github.com/darkly-art/blender-extension/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/darkly-art/blender-extension?label=Release&logo=github&labelColor=black&logoColor=4400ff&style=for-the-badge&color=4400ff)](https://github.com/darkly-art/blender-extension/releases/latest)
[![License](https://img.shields.io/badge/AGPL--3.0-000000?style=for-the-badge&label=License&labelColor=black&color=4400ff)](LICENSE)

Stream a live view of Blender into [Darkly](https://darkly.art) over localhost. The stream has a **transparent background**, so you can paint behind and around your 3D scene and treat Blender as just another layer.

Two sources, chosen in the panel:

- **Viewport** (default) streams whatever the 3D viewport shows, reusing pixels Blender already rendered.
- **Camera** streams a camera's point of view regardless of where the viewport is looking, at the cost of one extra offscreen render per frame.

Both stream the **viewport shading** (Solid / Material Preview / Rendered), not a final render. Set the shading mode to what you want to see in Darkly. Rendered shading with Cycles will be slow; EEVEE stays real-time.

## Install

Grab the latest zip from [Releases](https://github.com/darkly-art/blender-extension/releases/latest) and install it via **Edit → Preferences → Get Extensions → Install from Disk…**

Or build and install from source (needs `blender` on your `PATH`):

```bash
mkdir -p dist && blender --command extension build --source-dir darkly_stream --output-dir dist
blender --command extension install-file --repo user_default --enable dist/darkly_stream-*.zip
```

## Use

1. In Blender, press `N` in the 3D viewport and open the **Darkly** tab.
2. Pick the **Source**, port (default `8765`), FPS, and PNG compression, then click **Start**. The panel shows the stream URL and connected client count.
3. In Darkly: **Add Void → Blender**. The layer connects to `http://localhost:8765/stream` and shows the view live. Edit the void's `url` param to point at a different port/host.

Good to know:

- **Keep at least one 3D viewport open.** With several open, the **Viewport** dropdown picks which one to stream (*Auto* = the first).
- It's safe to leave the stream running; nothing is captured or sent while the scene is unchanged or no client is connected.
- If streaming slows Blender down, shrink the viewport or lower the FPS. **Benchmark Capture** in the panel times the capture and encode on your machine.
- **Works offline.** No internet connections are made and Blender's **Allow Online Access** setting is not required; the stream is served on localhost only. To stream to a different machine, check **All Interfaces** and point the void at `http://<this machine's IP>:8765/stream`.
- If your browser blocks an `https://` Darkly page from reaching `http://localhost` (mixed content), run Darkly from `http://localhost`.

## How it works

The viewport source reads the previous completed frame from the viewport's own scene-render texture during a `'PRE_VIEW'` draw callback. That texture holds the scene with a transparent background and no overlays, and reading it adds no rendering (captures piggyback on redraws Blender performs anyway); the pixels are scene-linear, so a worker thread un-premultiplies to straight alpha and applies the viewport's OCIO display transform (via Blender's bundled `PyOpenColorIO`). The camera source draws into a `GPUOffScreen` once per frame with the display transform applied on the GPU. Either way only that capture touches Blender's main thread: the worker encodes a PNG with the bundled OpenImageIO and publishes it to a stdlib `ThreadingHTTPServer`. `GET /stream` is a single long-lived HTTP/1.1 chunked response; each frame is `[4-byte big-endian length][PNG bytes]`.

Frames are captured on the viewport's own redraws (Blender repaints for every progressive-render pass, shading switch, view move, and scene edit) and paced by a timer, then de-duplicated on the raw frame before encoding, so a progressive renderer like Cycles refines all the way to full quality instead of freezing at its first low-sample pass, and an idle scene costs nothing. Because frames only flow on a real change, liveness is signalled separately: after ~2 seconds without a write, each connection sends a heartbeat, a zero-length frame (just the 4-byte prefix). Clients skip heartbeats as frames but treat any bytes as proof the server is alive, and can declare a byte-silent connection dead. Stopping the stream ends every open response with the terminating HTTP chunk, so clients see a clean close immediately. If the add-on hits an internal error, it logs the traceback to the console, tears everything down (freeing the port), and shows the error in the panel; Start works again right away.

## Development

Blender 5.1+ is the only requirement. The add-on bundles nothing and uses only libraries Blender ships (numpy, OpenImageIO, PyOpenColorIO).

Unit tests need no Blender (the server, encode, readback, color management, and pacing/dedup logic are all `bpy`-free):

```bash
python3 -m unittest discover -s tests
```

Headless smoke test (needs Blender; installed extensions live under `bl_ext`):

```bash
blender --background your_scene.blend --python-expr \
  "from bl_ext.user_default import darkly_stream; darkly_stream.start_stream(__import__('bpy').context.scene)"
```

```
darkly_stream/
  blender_manifest.toml  extension metadata (id, version, license)
  __init__.py     register/unregister, Scene props, operators, panel wiring
  stream.py       StreamRuntime: redraw-observed capture, pacing timer, harvest, crash containment
  pacing.py       per-tick capture/dirtiness + trailing-harvest decision (bpy-free)
  lifecycle.py    exception-contained teardown steps (bpy-free)
  panel.py        View3D N-panel: Start/Stop, source, viewport, port, fps, camera, quality
  capture.py      viewport framebuffer readback (PRE_VIEW) + GPUOffScreen camera draw
  colormanage.py  scene-linear -> display transform via PyOpenColorIO (bpy-free)
  readback.py     gpu.Buffer -> numpy (bpy-free)
  encode.py       un-premultiply + display transform + PNG via OpenImageIO (bpy-free)
  server.py       stdlib ThreadingHTTPServer chunked stream + seq/Condition dedup + heartbeat
tests/            unit tests for all of the above (no Blender needed)
```

## Credits

The viewport-source mechanism (reading the previous frame's scene render from the framebuffer bound during `'PRE_VIEW'` callbacks) is based on reading Blender's draw-manager source: `draw_context.cc` (`drw_callbacks_pre_scene`), `gpu_viewport.cc`, and the overlay engine's background pass (`overlay_background_frag.glsl`). The offscreen camera draw follows Blender's official `doc/python_api/examples/gpu.9.py`, and the framebuffer readback pattern follows Blender's bundled `addons_core/io_mesh_uv_layout/export_uv_png.py`.

## License

AGPL-3.0-or-later; see [LICENSE](LICENSE).
