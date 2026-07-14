"""Tests for the scene-linear -> display-referred conversion.

`colormanage` is deliberately `bpy`-free, so everything here runs in plain
Python: the shading-mode branch (`view_settings_snapshot`) against duck-typed
fakes of `scene` / `space.shading`, the sRGB fallback curve, and - when
PyOpenColorIO is importable (it ships with Blender and in scientific-Python
envs) - the OCIO processor cache and its agreement with the fallback on the
Standard view transform.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly_stream"))

import numpy as np  # noqa: E402

import colormanage  # noqa: E402

BLENDER_OCIO_CONFIG = "/usr/share/blender/5.1/datafiles/colormanagement/config.ocio"


def fake_scene(engine="BLENDER_EEVEE_NEXT", view_transform="AgX", look="None",
               exposure=1.5, gamma=1.2, display="sRGB"):
    return types.SimpleNamespace(
        render=types.SimpleNamespace(engine=engine),
        display_settings=types.SimpleNamespace(display_device=display),
        view_settings=types.SimpleNamespace(
            view_transform=view_transform, look=look, exposure=exposure, gamma=gamma
        ),
    )


def fake_shading(mode, **flags):
    return types.SimpleNamespace(
        type=mode,
        use_scene_lights=flags.get("use_scene_lights", False),
        use_scene_world=flags.get("use_scene_world", False),
        use_scene_lights_render=flags.get("use_scene_lights_render", False),
        use_scene_world_render=flags.get("use_scene_world_render", False),
    )


class SnapshotTest(unittest.TestCase):
    """Mirrors Blender's drw_color_management_type_for_v3d three-way branch."""

    def test_solid_uses_default_view_only(self):
        vs = colormanage.view_settings_snapshot(fake_scene(), fake_shading("SOLID"))
        self.assertIsNone(vs.view_transform)
        self.assertIsNone(vs.look)
        self.assertEqual((vs.exposure, vs.gamma), (0.0, 1.0))

    def test_material_preview_uses_view_and_look_without_exposure(self):
        vs = colormanage.view_settings_snapshot(
            fake_scene(look="AgX - Punchy"), fake_shading("MATERIAL")
        )
        self.assertEqual(vs.view_transform, "AgX")
        self.assertEqual(vs.look, "AgX - Punchy")
        self.assertEqual((vs.exposure, vs.gamma), (0.0, 1.0))

    def test_rendered_with_scene_lights_uses_full_settings(self):
        vs = colormanage.view_settings_snapshot(
            fake_scene(), fake_shading("RENDERED", use_scene_lights_render=True)
        )
        self.assertEqual(vs.view_transform, "AgX")
        self.assertEqual((vs.exposure, vs.gamma), (1.5, 1.2))

    def test_material_with_scene_world_uses_full_settings(self):
        vs = colormanage.view_settings_snapshot(
            fake_scene(), fake_shading("MATERIAL", use_scene_world=True)
        )
        self.assertEqual((vs.exposure, vs.gamma), (1.5, 1.2))

    def test_workbench_rendered_uses_full_settings(self):
        vs = colormanage.view_settings_snapshot(
            fake_scene(engine="BLENDER_WORKBENCH"), fake_shading("RENDERED")
        )
        self.assertEqual((vs.exposure, vs.gamma), (1.5, 1.2))

    def test_none_look_normalizes_to_none(self):
        vs = colormanage.view_settings_snapshot(
            fake_scene(look="None"), fake_shading("MATERIAL")
        )
        self.assertIsNone(vs.look)

    def test_workbench_aa_flag_tracks_workbench_engine(self):
        # Workbench (Solid / Wireframe, and Rendered under the workbench engine)
        # over-premultiplies viewport edges via its log2 AA, so the encoder must
        # apply the workbench inverse. EEVEE / Cycles edges are premultiplied
        # once (plain divide).
        def snap(mode, engine="BLENDER_EEVEE_NEXT", **flags):
            return colormanage.view_settings_snapshot(
                fake_scene(engine=engine), fake_shading(mode, **flags)
            )

        self.assertTrue(snap("SOLID").workbench_aa)
        self.assertTrue(snap("WIREFRAME").workbench_aa)
        self.assertTrue(snap("RENDERED", engine="BLENDER_WORKBENCH").workbench_aa)
        self.assertFalse(snap("MATERIAL").workbench_aa)
        self.assertFalse(snap("RENDERED", use_scene_lights_render=True).workbench_aa)


class FallbackTransformTest(unittest.TestCase):
    """DisplayTransform without any OCIO config: sRGB curve + exposure/gamma."""

    def _settings(self, exposure=0.0, gamma=1.0):
        return colormanage.ViewSettings(
            display="sRGB", view_transform=None, look=None,
            exposure=exposure, gamma=gamma,
        )

    def test_srgb_curve(self):
        dt = colormanage.DisplayTransform(config_path=None)
        rgba = np.array([[[0.5, 0.0031308, 0.0, 0.7]]], dtype=np.float32)
        dt.apply(rgba, self._settings())
        self.assertAlmostEqual(float(rgba[0, 0, 0]), 0.735357, places=5)
        self.assertAlmostEqual(float(rgba[0, 0, 1]), 0.0031308 * 12.92, places=6)
        self.assertEqual(float(rgba[0, 0, 2]), 0.0)
        self.assertAlmostEqual(float(rgba[0, 0, 3]), 0.7, places=6)  # alpha untouched

    def test_exposure_is_linear_gain_before_transform(self):
        dt = colormanage.DisplayTransform(config_path=None)
        base = np.array([[[0.25, 0.25, 0.25, 1.0]]], dtype=np.float32)
        doubled = np.array([[[0.125, 0.125, 0.125, 1.0]]], dtype=np.float32)
        dt.apply(base, self._settings())
        dt.apply(doubled, self._settings(exposure=1.0))  # 2**1 * 0.125 = 0.25
        np.testing.assert_allclose(doubled, base, atol=1e-6)

    def test_gamma_applied_after_transform(self):
        dt = colormanage.DisplayTransform(config_path=None)
        rgba = np.array([[[0.5, 0.5, 0.5, 1.0]]], dtype=np.float32)
        dt.apply(rgba, self._settings(gamma=2.0))
        self.assertAlmostEqual(float(rgba[0, 0, 0]), 0.735357**0.5, places=5)


@unittest.skipUnless(
    colormanage.ocio is not None and os.path.exists(BLENDER_OCIO_CONFIG),
    "PyOpenColorIO or Blender OCIO config unavailable",
)
class OCIOTransformTest(unittest.TestCase):
    def _dt(self):
        return colormanage.DisplayTransform(config_path=BLENDER_OCIO_CONFIG)

    def test_standard_view_matches_srgb_fallback(self):
        settings = colormanage.ViewSettings(
            display="sRGB", view_transform="Standard", look=None,
            exposure=0.0, gamma=1.0,
        )
        rgba = np.array([[[0.5, 0.2, 0.05, 1.0]]], dtype=np.float32)
        expected = rgba.copy()
        expected[..., :3] = colormanage.linear_to_srgb(expected[..., :3])
        self._dt().apply(rgba, settings)
        np.testing.assert_allclose(rgba, expected, atol=2e-3)

    def test_agx_differs_from_srgb_and_look_differs_from_plain(self):
        plain = colormanage.ViewSettings(
            display="sRGB", view_transform="AgX", look=None, exposure=0.0, gamma=1.0
        )
        punchy = colormanage.ViewSettings(
            display="sRGB", view_transform="AgX", look="AgX - Punchy",
            exposure=0.0, gamma=1.0,
        )
        src = np.array([[[0.5, 0.2, 0.1, 1.0]]], dtype=np.float32)
        a, b, c = src.copy(), src.copy(), src.copy()
        dt = self._dt()
        dt.apply(a, plain)
        dt.apply(b, punchy)
        c[..., :3] = colormanage.linear_to_srgb(c[..., :3])
        self.assertFalse(np.allclose(a, c, atol=1e-2))
        self.assertFalse(np.allclose(a, b, atol=1e-2))

    def test_processor_is_cached_per_settings(self):
        dt = self._dt()
        settings = colormanage.ViewSettings(
            display="sRGB", view_transform="AgX", look=None, exposure=0.0, gamma=1.0
        )
        first = dt._processor(settings)
        self.assertIs(dt._processor(settings), first)
        other = colormanage.ViewSettings(
            display="sRGB", view_transform="Standard", look=None,
            exposure=0.0, gamma=1.0,
        )
        self.assertIsNot(dt._processor(other), first)


if __name__ == "__main__":
    unittest.main()
