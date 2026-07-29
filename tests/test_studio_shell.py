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
