"""The second on-desk avatar: registry state and shell/server wiring.

The active avatar owns the RIGHT side of the desk; an optional second avatar
renders in its own window and mirrors to the LEFT. companion.json is the
source of truth for which avatar holds the left desk.
"""
import os
import tempfile
import unittest
from pathlib import Path

from studio import build

ROOT = Path(__file__).resolve().parents[1]


class CompanionRegistry(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._saved = {
            name: getattr(build, name)
            for name in ("ROOT", "AVATARS", "ACTIVE", "COMPANION")
        }
        self.addCleanup(lambda: [
            setattr(build, name, value) for name, value in self._saved.items()
        ])
        build.ROOT = str(root)
        build.AVATARS = str(root / "avatars")
        build.ACTIVE = str(root / "active.json")
        build.COMPANION = str(root / "companion.json")
        for slug in ("north", "south"):
            (root / "avatars" / slug).mkdir(parents=True)

    def test_companion_set_get_clear(self):
        self.assertIsNone(build.get_companion())
        self.assertEqual("south", build.set_companion("south"))
        self.assertEqual("south", build.get_companion())
        self.assertIsNone(build.set_companion(None))
        self.assertIsNone(build.get_companion())
        # Clearing an already-empty desk is not an error.
        self.assertIsNone(build.set_companion(""))

    def test_companion_rejects_unknown_avatar(self):
        with self.assertRaisesRegex(ValueError, "unknown avatar"):
            build.set_companion("nobody")

    def test_active_and_companion_are_independent_slots(self):
        build.set_active("north")
        build.set_companion("south")
        self.assertEqual("north", build.get_active())
        self.assertEqual("south", build.get_companion())

    def test_deleting_the_companion_vacates_the_desk(self):
        build.set_companion("south")
        build.delete_avatar("south")
        self.assertIsNone(build.get_companion())

    def test_deleting_another_avatar_keeps_the_companion(self):
        build.set_companion("south")
        build.delete_avatar("north")
        self.assertEqual("south", build.get_companion())


class CompanionServerContract(unittest.TestCase):
    """Source pins: the wiring these features depend on must stay present."""

    def setUp(self):
        self.app = (ROOT / "server" / "app.py").read_text()

    def test_companion_endpoint_and_meta_exist(self):
        self.assertIn('@app.post("/api/avatar/companion")', self.app)
        self.assertIn('"companion": r.get_companion()', self.app)
        self.assertIn('"companion": reg().get_companion()', self.app)

    def test_activation_vacates_a_promoted_companion(self):
        marker = self.app.index("r.set_active(b.slug)")
        window = self.app[marker:marker + 400]
        self.assertIn("r.set_companion(None)", window)

    def test_per_slug_page_and_assets_routes_exist(self):
        # The page is served UNDER /c/<slug>/ so the renderer's relative
        # "assets/..." references resolve per avatar with no URL rewriting.
        self.assertIn('@app.get("/c/{slug}/")', self.app)
        self.assertIn('@app.get("/c/{slug}/assets/{path:path}")', self.app)
        marker = self.app.index('@app.get("/c/{slug}/assets/{path:path}")')
        window = self.app[marker:marker + 400]
        self.assertIn("_safe_file(runtime_dir(slug), path)", window)


class CompanionShellContract(unittest.TestCase):
    def test_shell_routes_buddy_ipc_and_mirrors_left(self):
        main = (ROOT / "electron" / "main.cjs").read_text()
        self.assertIn("vivieen:companion-changed", main)
        # The second window loads the per-slug page flagged to the left side.
        self.assertIn("side=left", main)
        self.assertIn("/c/${slug}/?electron=1&companion=1", main)
        # Stillness docks the buddy flush on the LEFT edge (margin 0 mirrors
        # the primary's flush right dock).
        self.assertIn("dockedPetBounds(size, area, 0, 'left')", main)
        # Gestures route by sender so the buddy never drives the primary.
        self.assertIn("isBuddySender(event) ? applyBuddyRoam(value)", main)
        self.assertIn("isBuddySender(event) ? applyBuddyOpacity(value)", main)
        self.assertIn("isBuddySender(event) ? buddyShellState() : shellState()", main)
        # Boot restores the buddy from the server's companion slot.
        self.assertIn("createBuddyWindow(metadata.companion)", main)

    def test_bounds_helper_supports_a_left_dock(self):
        bounds = (ROOT / "electron" / "pet-window-bounds.cjs").read_text()
        self.assertIn("side = 'right'", bounds)
        self.assertIn("? area.x + margin", bounds)

    def test_preload_exposes_companion_changed(self):
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        self.assertIn("vivieen:companion-changed", preload)

    def test_renderer_leans_on_its_own_side(self):
        page = (ROOT / "web" / "index.html").read_text()
        self.assertIn("drawMotionClip('idle',now,PET_SIDE)", page)
        self.assertIn(
            "new URLSearchParams(location.search).get('side')==='left'", page)

    def test_settings_offers_the_left_desk(self):
        settings = (ROOT / "web" / "settings.html").read_text()
        self.assertIn('data-act="companion"', settings)
        self.assertIn("'/api/avatar/companion'", settings)


if __name__ == "__main__":
    unittest.main()
