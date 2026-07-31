"""The Character Studio window must survive every control inside it.

"Preview" shipped as a bare <a href="/files/<slug>/preview.mp4">.  Same origin,
so the Electron navigation guard waved it through, the settings window
navigated itself to the raw file, and Chromium's chrome-less video player
replaced the whole studio with no way back.  Two locks: no in-place navigation
links in the markup, and a shell that refuses them anyway.
"""
import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class SettingsMarkupTest(unittest.TestCase):
    def setUp(self):
        self.settings = read("web", "settings.html")

    def test_no_anchor_navigates_to_a_built_file(self):
        self.assertNotRegex(self.settings, r"<a[^>]+href=[\"'`]?/files/")

    def test_every_anchor_is_handled_or_in_page(self):
        for href in re.findall(r"<a\b[^>]*href=\"([^\"]+)\"", self.settings):
            self.assertTrue(href.startswith("#") or href == "/",
                            "unhandled navigation link: " + href)

    def test_two_designs_share_one_markup(self):
        # Quiet (calm paper minimalism) and Atelier (editorial couture) are
        # two complete designs over IDENTICAL markup: Atelier exists purely
        # as [data-design=atelier] overrides, so switching back restores
        # Quiet exactly and every feature behaves the same in both.
        self.assertIn(":root[data-design=atelier]", self.settings)
        self.assertIn('id="design-toggle"', self.settings)
        self.assertIn("localStorage.setItem('vivieen-design'", self.settings)
        self.assertIn("function applyDesign", self.settings)
        for page in ("index.html", "menu.html", "bubble.html"):
            source = read("web", page)
            self.assertIn("data-design=atelier", source)
            self.assertIn("vivieen-design", source)
        # Long-lived windows follow a switch live via the storage event.
        self.assertIn("addEventListener('storage'", read("web", "index.html"))
        self.assertIn("addEventListener('storage'", read("web", "bubble.html"))

    def test_upload_stages_a_naming_step_and_names_stay_editable(self):
        # A raw file name ("IMG_4032") is a bad avatar name: a picked
        # portrait is staged, the name field gets a cleaned suggestion
        # (selected, so typing replaces it), and Enter or Add portrait
        # commits. The name stays editable afterwards from the card.
        self.assertIn("function stageUpload", self.settings)
        self.assertIn("function suggestAvatarName", self.settings)
        self.assertIn('id="createAvatar"', self.settings)
        self.assertIn("stageUpload(dropped)", self.settings)
        self.assertIn("stageUpload(file.files[0])", self.settings)
        self.assertIn('data-act="rename"', self.settings)
        self.assertIn('class="av-name"', self.settings)
        self.assertIn("'/api/avatar/rename'", self.settings)
        # Export must read the clean name span, not the h3 (which now also
        # holds the rename button's glyph).
        self.assertIn("querySelector('.av-name')", self.settings)
        app = read("server", "app.py")
        self.assertIn('@app.post("/api/avatar/rename")', app)
        self.assertIn("class RenameRequest(BaseModel)", app)

    def test_preview_opens_the_modal_player(self):
        self.assertIn('data-act="preview"', self.settings)
        self.assertIn("function openPreview(", self.settings)
        self.assertIn('id="preview-video"', self.settings)

    def test_preview_stops_playing_when_closed(self):
        closer = self.settings.split("function closePreview()", 1)[1][:220]
        self.assertIn("video.pause()", closer)
        self.assertIn("removeAttribute('src')", closer)


class NavigationGuardTest(unittest.TestCase):
    def test_tool_windows_refuse_in_place_navigation(self):
        main = read("electron", "main.cjs")
        guard = main.split("function guardNavigation(", 1)[1].split("\n}", 1)[0]
        self.assertIn("targetUrl.pathname !== `/${kind}`", guard)
        self.assertIn("event.preventDefault()", guard)


class HeadFramingTest(unittest.TestCase):
    def test_head_views_are_pinned_to_the_silhouette_crown(self):
        runtime = read("web", "index.html")
        crop = runtime.split("function viewCrop(", 1)[1].split("\n}", 1)[0]
        self.assertIn("const crown=by-", crop)
        self.assertIn("crop.h+=crop.y-crown", crop)


if __name__ == "__main__":
    unittest.main()
