"""Scene-linear -> display-referred conversion for viewport-source frames.

The viewport source reads the viewport's render texture, which is scene-linear
(Blender applies the display transform only when blitting to screen). To make
the stream match what the user sees, this module reproduces that transform on
the worker thread.

Runs on a **worker thread**, so it must not touch `bpy`. The two entry points
that inspect Blender state (`view_settings_snapshot`) only read plain
attributes off duck-typed objects, so the module stays importable and testable
without Blender.

Which settings apply depends on the shading mode. Mirrors Blender's
`drw_color_management_type_for_v3d` (`draw_color_management.cc:47`):

  - Solid / Wireframe            -> display's default view transform only
  - Material Preview             -> scene view transform + look (no exposure)
  - Rendered, or scene lights /
    scene world enabled          -> full scene view settings

The transform itself uses Blender's bundled `PyOpenColorIO` and OCIO config
(exposure applied as a linear gain before, gamma after, matching Blender's
viewing pipeline). When PyOpenColorIO or the config is unavailable we fall
back to a plain linear->sRGB curve - exact for the Standard view transform,
approximate otherwise. Known gaps vs Blender: RENDERED-mode curve mapping and
dither are not replicated.
"""

import logging

from dataclasses import dataclass

log = logging.getLogger(__name__)

import numpy as np

try:
    import PyOpenColorIO as ocio
except ImportError:  # pragma: no cover - bundled with Blender, absent elsewhere
    ocio = None


@dataclass(frozen=True)
class ViewSettings:
    """Plain snapshot of the color management state a frame was rendered under.
    Captured on the main thread, hashable so the CPU processor can be cached.
    `view_transform is None` means "the display's default view transform"."""

    display: str
    view_transform: str | None
    look: str | None
    exposure: float
    gamma: float


def view_settings_snapshot(scene, shading):
    """Snapshot the settings the viewport is *displayed* with, per shading mode
    (`scene` / `shading` are `bpy` objects; attribute reads only)."""
    mode = shading.type  # 'WIREFRAME' | 'SOLID' | 'MATERIAL' | 'RENDERED'
    use_scene_lights = (mode == "MATERIAL" and shading.use_scene_lights) or (
        mode == "RENDERED" and shading.use_scene_lights_render
    )
    use_scene_world = (mode == "MATERIAL" and shading.use_scene_world) or (
        mode == "RENDERED" and shading.use_scene_world_render
    )
    use_workbench = scene.render.engine == "BLENDER_WORKBENCH"

    display = scene.display_settings.display_device
    view = scene.view_settings
    look = view.look if view.look and view.look != "None" else None

    if (use_workbench and mode == "RENDERED") or use_scene_lights or use_scene_world:
        return ViewSettings(
            display=display,
            view_transform=view.view_transform,
            look=look,
            exposure=float(view.exposure),
            gamma=float(view.gamma),
        )
    if mode in ("MATERIAL", "RENDERED"):
        # View transform + look only; exposure depends on scene light intensity,
        # which preview lighting doesn't use.
        return ViewSettings(
            display=display,
            view_transform=view.view_transform,
            look=look,
            exposure=0.0,
            gamma=1.0,
        )
    return ViewSettings(
        display=display, view_transform=None, look=None, exposure=0.0, gamma=1.0
    )


def linear_to_srgb(rgb):
    """Piecewise sRGB OETF - the fallback display transform."""
    rgb = np.clip(rgb, 0.0, None)
    return np.where(rgb < 0.0031308, rgb * 12.92, 1.055 * np.power(rgb, 1.0 / 2.4) - 0.055)


class DisplayTransform:
    """Applies a `ViewSettings` display transform to straight-alpha scene-linear
    float RGBA. Owns one cached OCIO `CPUProcessor`, rebuilt only when the
    settings change (they rarely do between frames). Single-threaded use."""

    def __init__(self, config_path=None):
        self._config = None
        self._warned = False
        if ocio is not None and config_path:
            try:
                self._config = ocio.Config.CreateFromFile(config_path)
            except Exception as exc:  # noqa: BLE001 - fall back, don't kill the stream
                log.warning("could not load OCIO config: %s", exc)
        self._key = None
        self._cpu = None

    def _processor(self, settings):
        if settings == self._key:
            return self._cpu
        self._key = settings
        self._cpu = None
        if self._config is not None:
            try:
                view = settings.view_transform or self._config.getDefaultView(
                    settings.display
                )
                transform = ocio.DisplayViewTransform(
                    src=ocio.ROLE_SCENE_LINEAR,
                    display=settings.display,
                    view=view,
                )
                if settings.look:
                    group = ocio.GroupTransform()
                    group.appendTransform(
                        ocio.LookTransform(
                            src=ocio.ROLE_SCENE_LINEAR,
                            dst=ocio.ROLE_SCENE_LINEAR,
                            looks=settings.look,
                        )
                    )
                    group.appendTransform(transform)
                    transform = group
                self._cpu = self._config.getProcessor(transform).getDefaultCPUProcessor()
            except Exception as exc:  # noqa: BLE001 - fall back, don't kill the stream
                log.warning("OCIO transform failed (%s); using sRGB", exc)
        if self._cpu is None and not self._warned:
            self._warned = True
            log.warning("no OCIO; approximating the display transform as sRGB")
        return self._cpu

    def apply(self, rgba, settings):
        """In place: straight-alpha scene-linear float32 -> display-referred.
        Alpha is untouched."""
        if settings.exposure != 0.0:
            rgba[..., :3] *= 2.0 ** settings.exposure
        cpu = self._processor(settings)
        if cpu is not None:
            height, width = rgba.shape[:2]
            cpu.apply(ocio.PackedImageDesc(rgba, width * height, 1, 4))
        else:
            rgba[..., :3] = linear_to_srgb(rgba[..., :3])
        if settings.gamma != 1.0:
            np.clip(rgba[..., :3], 0.0, None, out=rgba[..., :3])
            rgba[..., :3] **= 1.0 / settings.gamma
        return rgba
