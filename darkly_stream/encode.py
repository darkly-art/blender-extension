"""Captured frame -> straight-alpha PNG via OpenImageIO. Thread-safe, no `bpy`.

Runs on a **worker thread** (see `__init__._encode_worker`), so it must not touch
`bpy` (main-thread-only). OpenImageIO is bundled with Blender (`import OpenImageIO`,
see `addons_core/io_mesh_uv_layout/export_uv_png.py`) and is safe off-thread.

Accepts both pixel semantics the capture sources produce (`capture.py`):

  - **uint8** (camera source): display-referred, associated alpha - already
    color-managed by `draw_view3d(do_color_management=True)`.
  - **float32** (viewport source): scene-linear, associated alpha - the raw
    render-texture contents; the display transform is applied here on the
    worker via `colormanage` (with the `ViewSettings` snapshot taken at
    capture time), keeping the main thread free of it.

Both are un-premultiplied to straight alpha first (for float input this must
precede the display transform - transforming premultiplied colour would darken
edges), then flipped, quantized, and written.

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
    straight alpha (no re-premultiply), matching the Darkly void's
    `premultiplied_alpha: false` sampling and the frontend's
    `createImageBitmap(blob, { premultiplyAlpha: 'none' })` decode.

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
    (reused across frames); call from a single worker thread.

    `compression` is the libpng level (0 = none/fastest, 9 = smallest/slowest);
    the default trades a little size for a lot of speed since the encode runs
    continuously on the worker. `ocio_config_path` feeds the display transform
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
        produced (a CPU numpy array - safe to hand across threads); float32
        input is scene-linear and requires the `view_settings` snapshot taken
        with it."""
        if rgba.dtype == np.uint8:
            arr = rgba.reshape(height, width, 4).astype(np.float32) / 255.0
        else:
            arr = rgba.reshape(height, width, 4)

        alpha = arr[..., 3:4]
        # Un-premultiply: divide colour by alpha where alpha > 0, else 0.
        straight = np.empty_like(arr)
        np.divide(arr[..., :3], alpha, out=straight[..., :3], where=alpha > 0.0)
        straight[..., :3] = np.where(alpha > 0.0, straight[..., :3], 0.0)
        straight[..., 3:4] = alpha

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


_SRGB_FALLBACK = colormanage.ViewSettings(
    display="sRGB", view_transform=None, look=None, exposure=0.0, gamma=1.0
)
