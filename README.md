# Darkly Blender Extension

[![Discord](https://img.shields.io/discord/1495886270780539021?label=Discord&logo=discord&logoColor=white&style=for-the-badge&color=9500ff)](https://discord.gg/kFz2FGhbpu)
[![Darkly](https://img.shields.io/badge/GitHub-Darkly-orange?logo=github&logoColor=white&style=for-the-badge&color=6914ff)](https://github.com/darkly-art/darkly)

![Blender](https://img.shields.io/badge/Blender_5.1+-000000?style=for-the-badge&logo=blender&logoColor=9500ff)
[![CI](https://img.shields.io/github/actions/workflow/status/darkly-art/blender-extension/ci.yml?branch=master&label=CI&logo=github&labelColor=black&logoColor=4400ff&style=for-the-badge&color=4400ff)](https://github.com/darkly-art/blender-extension/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/darkly-art/blender-extension?label=Release&logo=github&labelColor=black&logoColor=4400ff&style=for-the-badge&color=4400ff)](https://github.com/darkly-art/blender-extension/releases/latest)
[![License](https://img.shields.io/badge/GPL--3.0-000000?style=for-the-badge&label=License&labelColor=black&color=4400ff)](LICENSE)

Stream a live view of Blender into [Darkly](https://darkly.art) over localhost. The stream has a **transparent background**, so you can paint above and below your 3D scene and treat Blender as just another layer.

![Darkly](https://github.com/user-attachments/assets/30a589cb-dd53-464b-bcf3-b0d9433e31fe)

Darkly is an open source editor for digital artists and painters. You can download it from the [Darkly Github](https://github.com/darkly-art/darkly/releases) or run it in the browser at [demo.darkly.art](https://demo.darkly.art).

This Blender extension helps speed up hybrid workflows and lets you quickly try out different camera and lighting angles without having to render and paste over and over.

It reuses Blender's own viewport render and streams it continuously over a single HTTP request, without any extra CPU/GPU work or external dependencies.

https://github.com/user-attachments/assets/f92eec50-b2e7-4c4d-9967-1966d4df9037

You can pick from two sources:

- **Viewport** (default) streams whatever the 3D viewport shows, reusing pixels Blender already rendered.
- **Camera** streams a camera's point of view regardless of where the viewport is looking, at the cost of one extra offscreen render per frame.

Both stream the **viewport shading** (Solid / Material Preview / Rendered), not a final render. Set the shading mode to what you want to see in Darkly.

## Install

To install from the Blender marketplace, go to **Edit → Preferences → Get Extensions**, and search for "Darkly".

You can also grab the latest zip from [Releases](https://github.com/darkly-art/blender-extension/releases/latest) and install it via **Install from Disk…**

Or build and install from source (needs `blender` on your `PATH`):

```bash
mkdir -p dist && blender --command extension build --source-dir darkly --output-dir dist
blender --command extension install-file --repo user_default --enable dist/darkly-*.zip
```

## Use

1. In Blender, press `N` in the 3D viewport and open the **Darkly** tab.
2. Pick the **Source**, port (default `8765`), FPS, and PNG compression, then click **Start**. The panel shows the stream URL and connected client count.
3. In Darkly: **Add Void → Blender**. The layer connects to `http://localhost:8765/stream` and shows the view live. Edit the void's `url` param to point at a different port/host.

Good to know:

- **Keep at least one 3D viewport open.** With several open, the **Viewport** dropdown picks which one to stream (*Auto* = the first).
- **Streaming a Rendered view?** Tick **Film Transparency** in the panel so the world background drops out and only your geometry streams over Darkly's canvas. (Solid/Material shading is already excluded from the background automatically; this is the same setting as Render Properties > Film > Transparent, surfaced here for convenience.)
- **Streaming Solid shading?** Untick **Object Outline** in the panel for clean edges. The workbench outline is drawn in the theme colour (black by default) straight into the streamed buffer, so it bakes a dark fringe into anti-aliased silhouettes when composited over Darkly's canvas. This is the same setting as Viewport Shading > Options > Object Outline, surfaced here for convenience.
- It's safe to leave the stream running; nothing is captured or sent while the scene is unchanged or no client is connected.
- If streaming slows Blender down, try lowering the FPS.
- **Works offline.** No internet connections are made and Blender's **Allow Online Access** setting is not required; the stream is served on localhost only. To stream to a different machine, check **All Interfaces** and point the void at `http://<this machine's IP>:8765/stream`.
- If your browser blocks an `https://` Darkly page from reaching `http://localhost` (mixed content), run Darkly from `http://localhost`.

## How it works

The viewport source grabs the last rendered frame straight from the viewport's own texture (transparent background, no overlays), so it adds no extra rendering; the camera source draws into an offscreen buffer instead. A capture is triggered whenever Blender repaints (a render pass, a view move, a scene edit), paced by a timer, and dropped if identical to the last — so a progressive renderer like Cycles keeps refining to full quality while an idle scene costs nothing. Only that grab runs on Blender's main thread; a small helper subprocess (launched with Blender's own Python) receives the raw pixels over a pipe, does the color conversion, encodes a PNG, and serves it — so Blender's process stays single-threaded. It's served at `GET /stream` as one long-lived HTTP/1.1 chunked response, each frame framed as `[4-byte big-endian length][PNG bytes]`, with a zero-length heartbeat after ~2 seconds of silence so clients can tell a quiet stream from a dead one. If Blender quits or crashes, the helper sees the pipe close and exits, so the port always comes back.

## Development

Blender 5.1+ is the only requirement. The add-on bundles nothing and uses only libraries Blender ships (numpy, OpenImageIO, PyOpenColorIO).

Unit tests need no Blender (the server, helper bridge, encode, readback, color management, and pacing/dedup logic are all `bpy`-free):

```bash
python3 -m unittest discover -s tests
```

Headless smoke test (needs Blender; installed extensions live under `bl_ext`):

```bash
blender --background your_scene.blend --python-expr \
  "from bl_ext.user_default import darkly; darkly.start_stream(__import__('bpy').context.scene)"
```

```
darkly/
  blender_manifest.toml  extension metadata (id, version, license)
  __init__.py     register/unregister, Scene props, operators, panel wiring
  stream.py       StreamRuntime: redraw-observed capture, pacing timer, harvest, crash containment
  bridge.py       parent side of the helper subprocess: spawn, non-blocking pipe pump (bpy-free)
  helper.py       helper subprocess entry point: asyncio serve/encode pipeline (bpy-free)
  pacing.py       per-tick capture/dirtiness + trailing-harvest decision (bpy-free)
  dedup.py        pre-send raw-frame duplicate compare (bpy-free)
  lifecycle.py    exception-contained teardown steps (bpy-free)
  panel.py        View3D N-panel: Start/Stop, source, viewport, port, fps, camera, quality
  capture.py      viewport framebuffer readback (PRE_VIEW) + GPUOffScreen camera draw
  colormanage.py  scene-linear -> display transform via PyOpenColorIO (bpy-free)
  readback.py     gpu.Buffer -> numpy (bpy-free)
  encode.py       un-premultiply + display transform + PNG via OpenImageIO (bpy-free)
  server.py       asyncio chunked HTTP stream + seq/Condition dedup + heartbeat (bpy-free)
tests/            unit tests for all of the above (no Blender needed)
```

## Credits

The viewport-source mechanism (reading the previous frame's scene render from the framebuffer bound during `'PRE_VIEW'` callbacks) is based on reading Blender's draw-manager source: `draw_context.cc` (`drw_callbacks_pre_scene`), `gpu_viewport.cc`, and the overlay engine's background pass (`overlay_background_frag.glsl`). The offscreen camera draw follows Blender's official `doc/python_api/examples/gpu.9.py`, and the framebuffer readback pattern follows Blender's bundled `addons_core/io_mesh_uv_layout/export_uv_png.py`.

## Links

- **Darkly Blog** - [blog.darkly.art](https://blog.darkly.art)
