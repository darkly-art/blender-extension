"""Tests for the capture sources' bpy-free surface: dirtiness and polling.

Dirtiness is type-owned (`is_dirty` / `mark_captured`), and the two sources
answer it differently:

  - `ViewportCapture` trusts the redraw: it is dirty exactly when the streamed
    viewport has redrawn since the last tick (`redraw_seen`), so progressive
    refinement passes - invisible to any cheap signature - are not frozen out.
  - `CameraCapture` keeps a private render-skip signature (camera pose + frame +
    region size + shading type) because its draw is a full extra render.

Regression tests pinned here: a shading switch (Solid->Rendered) must move the
camera signature so the camera stream refreshes without a view move; viewport
navigation must NOT move it (that is the viewport source's concern, via the
redraw). `capture.py` imports `bpy`/`gpu` at module scope (its capture methods
are GPU-bound), so those are stubbed here; the logic under test is plain
attribute access.
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


def fake_space(shading_type="SOLID"):
    return types.SimpleNamespace(shading=types.SimpleNamespace(type=shading_type))


class ViewportDirtyTest(unittest.TestCase):
    """The viewport source has no signature - it is dirty exactly when the
    streamed viewport redrew (`redraw_seen`), so in-place progressive refinement
    (same view, same frame) keeps streaming instead of freezing at pass one."""

    def setUp(self):
        self.src = capture.ViewportCapture()
        self.scene = fake_scene()
        self.props = fake_props()
        self.space = fake_space()
        self.region = fake_region(FakeMatrix.diag(1.0))

    def test_dirty_iff_redraw_seen(self):
        self.assertTrue(
            self.src.is_dirty(self.scene, self.props, self.space, self.region, True)
        )
        self.assertFalse(
            self.src.is_dirty(self.scene, self.props, self.space, self.region, False)
        )

    def test_dirtiness_ignores_view_and_frame_without_a_redraw(self):
        # No signature: a moved view or advanced frame is invisible here - the
        # redraw Blender issues for either is what drives dirtiness instead.
        moved = fake_region(FakeMatrix.diag(9.0))
        self.assertFalse(
            self.src.is_dirty(fake_scene(frame=5), self.props, self.space, moved, False)
        )

    def test_mark_captured_is_a_noop(self):
        self.src.mark_captured(self.scene, self.props, self.space, self.region)

    def test_poll_has_no_preconditions(self):
        self.assertIsNone(self.src.poll(self.scene, self.props))

    def test_source_traits(self):
        self.assertEqual(self.src.draw_handler_type, "PRE_VIEW")
        self.assertTrue(self.src.needs_harvest)


class CameraDirtyTest(unittest.TestCase):
    """The camera source keeps a private render-skip signature: dirty only when
    the render would differ (camera pose / frame / region size / shading type),
    ignoring redraws it didn't cause."""

    def setUp(self):
        self.src = capture.CameraCapture()
        self.region = fake_region(FakeMatrix.diag(1.0))
        self.space = fake_space("SOLID")

    def test_first_capture_is_dirty_then_stable(self):
        scene = fake_scene(active_camera=fake_camera(FakeMatrix.diag(1.0)))
        # Nothing captured yet -> dirty; after mark_captured, unchanged -> clean.
        self.assertTrue(self.src.is_dirty(scene, fake_props(), self.space, self.region, False))
        self.src.mark_captured(scene, fake_props(), self.space, self.region)
        self.assertFalse(self.src.is_dirty(scene, fake_props(), self.space, self.region, False))

    def test_camera_move_is_dirty_but_redraw_and_orbit_are_not(self):
        cam = fake_camera(FakeMatrix.diag(1.0))
        scene = fake_scene(active_camera=cam)
        self.src.mark_captured(scene, fake_props(), self.space, self.region)
        # A redraw alone (redraw_seen=True) and viewport orbit (different region
        # view data, same size) must NOT re-render the camera source.
        orbited = fake_region(FakeMatrix.diag(9.0))
        self.assertFalse(self.src.is_dirty(scene, fake_props(), self.space, orbited, True))
        # Moving the camera does.
        moved = fake_scene(active_camera=fake_camera(FakeMatrix.diag(3.0)))
        self.assertTrue(self.src.is_dirty(moved, fake_props(), self.space, self.region, False))

    def test_shading_switch_is_dirty_without_a_view_move(self):
        # Regression: a Solid->Rendered switch is not a depsgraph update and does
        # not move the camera pose, so without shading in the signature the
        # camera stream would not refresh until the view moved.
        scene = fake_scene(active_camera=fake_camera(FakeMatrix.diag(1.0)))
        self.src.mark_captured(scene, fake_props(), fake_space("SOLID"), self.region)
        self.assertTrue(
            self.src.is_dirty(scene, fake_props(), fake_space("RENDERED"), self.region, False)
        )
        self.src.mark_captured(scene, fake_props(), fake_space("RENDERED"), self.region)
        self.assertFalse(
            self.src.is_dirty(scene, fake_props(), fake_space("RENDERED"), self.region, True)
        )

    def test_resize_is_dirty(self):
        scene = fake_scene(active_camera=fake_camera(FakeMatrix.diag(1.0)))
        self.src.mark_captured(scene, fake_props(), self.space, self.region)
        resized = fake_region(FakeMatrix.diag(1.0), width=801)
        self.assertTrue(self.src.is_dirty(scene, fake_props(), self.space, resized, False))

    def test_explicit_camera_overrides_scene_camera(self):
        scene = fake_scene(active_camera=fake_camera(FakeMatrix.diag(1.0)))
        self.src.mark_captured(scene, fake_props(), self.space, self.region)
        override = fake_props(camera=fake_camera(FakeMatrix.diag(5.0)))
        self.assertTrue(self.src.is_dirty(scene, override, self.space, self.region, False))

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
