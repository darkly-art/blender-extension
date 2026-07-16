"""Captured frame -> straight-alpha PNG via OpenImageIO. No `bpy`.

Runs in the **helper subprocess** (see `helper.py`), off the asyncio loop via
`run_in_executor`, so it must not touch `bpy` (which isn't present in the child
at all). OpenImageIO is bundled with Blender (`import OpenImageIO`, see
`addons_core/io_mesh_uv_layout/export_uv_png.py`) and works standalone under
Blender's Python.

Accepts both pixel semantics the capture sources produce (`capture.py`):

  - **uint8** (camera source): display-referred, associated alpha - already
    color-managed by `draw_view3d(do_color_management=True)`.
  - **float32** (viewport source): scene-linear, associated alpha - the raw
    render-texture contents; the display transform is applied here in the
    helper via `colormanage` (with the `ViewSettings` snapshot taken at
    capture time), keeping Blender's main thread free of it.

Both are un-premultiplied to straight alpha first (for float input this must
precede the display transform - transforming premultiplied colour would darken
edges), then flipped, quantized, and written. The inverse depends on the source's
edge convention (`_unpremultiply`): EEVEE / Cycles edges are premultiplied once
(`rgb / alpha`), while the workbench viewport (Solid / Wireframe) accumulates AA
edge RGB in log2 space, so a coverage-`c` edge is stored `rgb = (colour+1)**c - 1`
and needs the matching `(rgb + 1)**(1/alpha) - 1` inverse - carried by the
`ViewSettings.workbench_aa` flag - or a plain divide leaves a dark silhouette
fringe.

Why PNG, not WebP: OIIO's WebP writer does a slow high-effort/lossless encode
(measured ~2.5s per 720p frame), which pegs a core and caps the stream. libpng at
a low compression level encodes the same frame in tens of ms, is lossless, and
carries alpha. On localhost the larger byte size is a non-issue, and the browser's
`createImageBitmap` decodes PNG by content-sniffing regardless of the wire's
declared MIME type, so the frontend needs no change.

Alpha correctness (verified, not inferred):
  - Captures are **associated (premultiplied)** alpha - they blended over a
    cleared alpha=0 buffer. We un-premultiply with numpy
    (`rgb = where(a > 0, rgb / a, 0)`) -> straight alpha.
  - We tag the output `oiio:UnassociatedAlpha = 1` so the PNG writer stores
    straight alpha (no re-premultiply) - the semantics PNG itself specifies.
    Darkly's frontend decodes with
    `createImageBitmap(blob, { premultiplyAlpha: 'premultiply' })`, converting
    into the premultiplied convention its void frame texture stores (so GPU
    linear filtering doesn't darken alpha edges); that decode consumes the
    straight-alpha PNG correctly, so nothing changes on this side.

Orientation: `read_color` gives bottom-up rows (OpenGL origin); image files are
top-down, so we flip vertically before writing.
"""

import os
import tempfile

import numpy as np
import OpenImageIO as oiio

try:  # package context (Blender); the unit tests import modules top-level
    from . import colormanage  # because the package __init__ needs bpy
except ImportError:
    import colormanage


class FrameEncoder:
    """Encodes a captured RGBA buffer to PNG bytes. One temp file per encoder
    (reused across frames); call sequentially (the helper's single encoder task).

    `compression` is the libpng level (0 = none/fastest, 9 = smallest/slowest);
    the default trades a little size for a lot of speed since the encode runs
    continuously in the helper. `ocio_config_path` feeds the display transform
    for float (scene-linear) input; `None` falls back to sRGB."""

    def __init__(self, compression=1, ocio_config_path=None):
        self.compression = int(compression)
        self._display = colormanage.DisplayTransform(ocio_config_path)
        # PID-scoped temp path so concurrent Blender instances don't collide.
        self._temp_path = os.path.join(
            tempfile.gettempdir(), f"darkly_stream_{os.getpid()}.png"
        )

    def encode(self, width, height, rgba, view_settings=None):
        """Un-premultiply, color-manage (float input), flip, and encode to PNG
        bytes. `rgba` is the bottom-up, associated-alpha array a capture source
        produced (a CPU numpy array, reconstructed in the helper from the pipe);
        float32 input is scene-linear and requires the `view_settings` snapshot
        taken with it."""
        if rgba.dtype == np.uint8:
            arr = rgba.reshape(height, width, 4).astype(np.float32) / 255.0
            workbench_aa = False
        else:
            arr = rgba.reshape(height, width, 4)
            workbench_aa = view_settings is not None and view_settings.workbench_aa

        straight = _unpremultiply(arr, workbench_aa)

        if rgba.dtype != np.uint8:
            self._display.apply(straight, view_settings or _SRGB_FALLBACK)
        np.clip(straight, 0.0, 1.0, out=straight)

        # Flip bottom-up -> top-down and quantize back to uint8, contiguous for OIIO.
        pixels = np.ascontiguousarray((straight[::-1] * 255.0 + 0.5).astype(np.uint8))

        out = oiio.ImageOutput.create(self._temp_path)
        if out is None:
            raise RuntimeError(oiio.geterror() or "OpenImageIO: no PNG writer")
        spec = oiio.ImageSpec(width, height, 4, "uint8")
        spec.attribute("oiio:UnassociatedAlpha", 1)
        spec.attribute("png:compressionLevel", self.compression)
        out.open(self._temp_path, spec)
        out.write_image(pixels)
        out.close()

        with open(self._temp_path, "rb") as handle:
            return handle.read()

    def free(self):
        try:
            if os.path.exists(self._temp_path):
                os.remove(self._temp_path)
        except OSError:
            pass


def _unpremultiply(arr, workbench_aa):
    """Associated (premultiplied) -> straight alpha, colour set to 0 where alpha
    is 0. `arr` is `(H, W, 4)` float32; a new array is returned. Alpha is the
    linear coverage on both paths and is emitted unchanged.

    `workbench_aa` picks the inverse for the source's edge convention:

      - False (EEVEE / Cycles, and all uint8 camera frames): edges are
        premultiplied once, so `rgb / alpha` recovers the straight colour.
      - True (workbench: Solid / Wireframe / workbench-Rendered): workbench's
        viewport anti-aliasing accumulates edge RGB in log2 space - its TAA
        writes `color.rgb = log2(color.rgb + 1)`
        (`workbench_effect_taa_frag.glsl:22`) and its SMAA resolve divides by
        the accumulated weight in that space and reads back `exp2(rgb) - 1`
        (`workbench_effect_smaa_frag.glsl:39-41`), alpha never wrapped. So a
        coverage-`c` silhouette edge is stored `rgb = (colour+1)**c - 1`,
        `alpha = c`, and the exact inverse of the log2 blend is
        `colour = (rgb + 1)**(1/alpha) - 1`. It is stable as alpha -> 0
        (`rgb -> 0`, so the base -> 1, so `colour -> 0`) and is the identity at
        `alpha == 1`, so only partial-coverage edges are touched.
    """
    alpha = arr[..., 3:4]
    rgb = arr[..., :3]
    mask = alpha > 0.0
    straight = np.empty_like(arr)
    if workbench_aa:
        base = np.maximum(rgb + 1.0, np.finfo(np.float32).tiny)
        inv_alpha = np.divide(1.0, alpha, out=np.zeros_like(alpha), where=mask)
        recovered = np.power(base, inv_alpha) - 1.0
        straight[..., :3] = np.where(mask, recovered, 0.0)
    else:
        np.divide(rgb, alpha, out=straight[..., :3], where=mask)
        straight[..., :3] = np.where(mask, straight[..., :3], 0.0)
    straight[..., 3:4] = alpha
    return straight


_SRGB_FALLBACK = colormanage.ViewSettings(
    display="sRGB", view_transform=None, look=None, exposure=0.0, gamma=1.0
)
