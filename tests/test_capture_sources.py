"""Tests for the capture sources' bpy-free surface: signatures and polling.

Regression test for a specific bug: the stream's dedup signature only tracked
the *camera pose*, so orbiting/panning/zooming the viewport never produced a
fresh frame - the viewport source's signature must move on any view change
(it hashes `perspective_matrix`, which folds view and projection together).

`capture.py` imports `bpy`/`gpu` at module scope (its capture methods are
GPU-bound), so those are stubbed here; the signature/poll logic under test is
plain attribute access.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "darkly_stream"))

sys.modules.setdefault("bpy", types.SimpleNamespace())
sys.modules.setdefault("gpu", types.SimpleNamespace())

import capture  # noqa: E402


class FakeMatrix(list):
    """4x4 nested-list stand-in for `mathutils.Matrix`."""

    @classmethod
    def diag(cls, d):
        return cls([[d if i == j else 0.0 for j in range(4)] for i in range(4)])


def fake_region(perspective=None, width=800, height=600):
    data = (
        None
        if perspective is None
        else types.SimpleNamespace(perspective_matrix=perspective)
    )
    return types.SimpleNamespace(data=data, width=width, height=height)


def fake_scene(frame=1, active_camera=None):
    return types.SimpleNamespace(frame_current=frame, camera=active_camera)


def fake_camera(matrix, kind="CAMERA"):
    return types.SimpleNamespace(matrix_world=matrix, type=kind)


def fake_props(camera=None):
    return types.SimpleNamespace(camera=camera)


class ViewportSignatureTest(unittest.TestCase):
    def setUp(self):
        self.src = capture.ViewportCapture()
        self.scene = fake_scene()
        self.props = fake_props()

    def test_navigation_changes_signature(self):
        # The bug this defends against: view orbit/pan/zoom must invalidate the
        # dedup signature (the old camera-pose signature never saw it).
        before = self.src.signature(
            self.scene, self.props, fake_region(FakeMatrix.diag(1.0))
        )
        after = self.src.signature(
            self.scene, self.props, fake_region(FakeMatrix.diag(2.0))
        )
        self.assertNotEqual(before, after)

    def test_static_view_is_stable(self):
        a = self.src.signature(self.scene, self.props, fake_region(FakeMatrix.diag(1.0)))
        b = self.src.signature(self.scene, self.props, fake_region(FakeMatrix.diag(1.0)))
        self.assertEqual(a, b)

    def test_frame_and_resize_change_signature(self):
        base = self.src.signature(self.scene, self.props, fake_region(FakeMatrix.diag(1.0)))
        frame = self.src.signature(
            fake_scene(frame=2), self.props, fake_region(FakeMatrix.diag(1.0))
        )
        resized = self.src.signature(
            self.scene, self.props, fake_region(FakeMatrix.diag(1.0), width=801)
        )
        self.assertNotEqual(base, frame)
        self.assertNotEqual(base, resized)

    def test_region_without_view_data_yields_none(self):
        self.assertIsNone(self.src.signature(self.scene, self.props, fake_region(None)))

    def test_poll_has_no_preconditions(self):
        self.assertIsNone(self.src.poll(self.scene, self.props))

    def test_source_traits(self):
        self.assertEqual(self.src.draw_handler_type, "PRE_VIEW")
        self.assertTrue(self.src.needs_harvest)


class CameraSignatureTest(unittest.TestCase):
    def setUp(self):
        self.src = capture.CameraCapture()
        self.region = fake_region(FakeMatrix.diag(1.0))

    def test_camera_move_changes_signature_but_view_does_not(self):
        cam_a = fake_camera(FakeMatrix.diag(1.0))
        cam_b = fake_camera(FakeMatrix.diag(3.0))
        scene = fake_scene(active_camera=cam_a)
        base = self.src.signature(scene, fake_props(), self.region)
        moved = self.src.signature(
            fake_scene(active_camera=cam_b), fake_props(), self.region
        )
        orbited = self.src.signature(
            scene, fake_props(), fake_region(FakeMatrix.diag(9.0))
        )
        self.assertNotEqual(base, moved)
        self.assertEqual(base, orbited)  # viewport navigation is irrelevant here

    def test_explicit_camera_overrides_scene_camera(self):
        scene = fake_scene(active_camera=fake_camera(FakeMatrix.diag(1.0)))
        override = fake_camera(FakeMatrix.diag(5.0))
        a = self.src.signature(scene, fake_props(), self.region)
        b = self.src.signature(scene, fake_props(camera=override), self.region)
        self.assertNotEqual(a, b)

    def test_poll_requires_a_camera(self):
        self.assertIsNotNone(self.src.poll(fake_scene(), fake_props()))
        self.assertIsNotNone(
            self.src.poll(
                fake_scene(active_camera=fake_camera(FakeMatrix.diag(1.0), kind="EMPTY")),
                fake_props(),
            )
        )
        self.assertIsNone(
            self.src.poll(
                fake_scene(active_camera=fake_camera(FakeMatrix.diag(1.0))), fake_props()
            )
        )

    def test_source_traits(self):
        self.assertEqual(self.src.draw_handler_type, "POST_PIXEL")
        self.assertFalse(self.src.needs_harvest)


class RegistryTest(unittest.TestCase):
    def test_sources_registry_matches_the_enum(self):
        self.assertEqual(set(capture.SOURCES), {"VIEWPORT", "CAMERA"})


def _fake_area(kind="VIEW_3D", width=800, height=600):
    space = types.SimpleNamespace()
    region = types.SimpleNamespace(type="WINDOW", width=width, height=height)
    return types.SimpleNamespace(type=kind, spaces=types.SimpleNamespace(active=space),
                                 regions=[region])


def _install_windows(*screens):
    """Point the stubbed bpy.context at fake windows: `screens` is a list of
    (name, [areas]) per window."""
    windows = [
        types.SimpleNamespace(screen=types.SimpleNamespace(name=name, areas=areas))
        for name, areas in screens
    ]
    sys.modules["bpy"].context = types.SimpleNamespace(
        window_manager=types.SimpleNamespace(windows=windows)
    )


class ViewportSelectionTest(unittest.TestCase):
    """The viewport dropdown's enumeration + resolution: keys are positional
    (screen name + index), and a stale selection falls back to the first open
    viewport instead of stopping the stream."""

    def test_enumerates_view3d_areas_across_windows(self):
        a, b, c = _fake_area(), _fake_area(width=400), _fake_area()
        _install_windows(
            ("Layout", [a, _fake_area(kind="PROPERTIES"), b]),
            ("Animation", [c]),
        )
        entries = capture.list_view3d()
        self.assertEqual([e[2] for e in entries], ["Layout:0", "Layout:1", "Animation:0"])
        # Labels disambiguate several viewports on one screen.
        self.assertIn("Layout", entries[0][3])
        self.assertIn("#2", entries[1][3])

    def test_selector_resolves_to_that_viewport(self):
        a, b = _fake_area(), _fake_area(width=400)
        _install_windows(("Layout", [a, b]))
        space, region = capture.find_view3d("Layout:1")
        self.assertIs(space, b.spaces.active)
        self.assertEqual(region.width, 400)

    def test_auto_and_stale_selection_fall_back_to_first(self):
        a = _fake_area()
        _install_windows(("Layout", [a]))
        self.assertIs(capture.find_view3d("AUTO")[0], a.spaces.active)
        self.assertIs(capture.find_view3d("Gone:3")[0], a.spaces.active)

    def test_no_viewports_yields_none(self):
        _install_windows(("Layout", [_fake_area(kind="PROPERTIES")]))
        self.assertEqual(capture.find_view3d(), (None, None))


if __name__ == "__main__":
    unittest.main()
