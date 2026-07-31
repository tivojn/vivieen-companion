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

    def test_lost_renders_explain_themselves_and_can_be_regenerated(self):
        # Live case (2026-08-01, gary33): the initial build lost 4 of 16
        # renders, so Validate & Rebuild was disabled - correctly, but
        # silently, because the first slider touch overwrote the reason.
        # The reason now survives every repaint, and a top-up button
        # regenerates ONLY the missing shapes, then re-opens the gate.
        self.assertIn('id="rig-fill-gaps"', self.settings)
        self.assertIn("Generate ${RIG_GAPS.length} missing renders", self.settings)
        self.assertIn("missing retained renders: '", self.settings)
        self.assertIn("JSON.stringify({ slug, shapes })", self.settings)
        self.assertIn("RIG_GAPS = [];", self.settings)

    def test_rebuild_room_has_preview_progress_and_a_full_panel(self):
        # 2026-08-01 usability pass: the preview plays inside the
        # calibration room (stacked above it), the rebuild's status card
        # pulses with a live bar and takes the spotlight, and every slider
        # is visible at once in a two-column panel - no accidental scroll
        # discovery.
        self.assertIn('id="rig-preview-play"', self.settings)
        self.assertIn("openPreview(RIG_SLUG)", self.settings)
        self.assertIn("#preview-modal{z-index:60}", self.settings)
        self.assertIn('id="rig-progress"', self.settings)
        self.assertIn("'busy', percent", self.settings)
        self.assertIn("rigPulse", self.settings)
        self.assertIn("#rig-controls{display:grid;grid-template-columns:1fr 1fr",
                      self.settings)
        self.assertIn("scrollIntoView({ block: 'nearest'", self.settings)

    def test_calibration_sliders_have_a_live_response_preview(self):
        # WYSIWYG expectation (2026-08-01): dragging a slider must visibly
        # change the face on the stage, not just a region's glow. The stage
        # blends the neutral keyframe with the selected pose per region at
        # the current slider weights, re-fetches the bank after every
        # publish (epoch-keyed bitmaps), and the success message says when
        # the rebuilt avatar is not the one on the desk.
        self.assertIn('id="rig-show-blend"', self.settings)
        self.assertIn("function rigBitmap", self.settings)
        self.assertIn("function resetRigBitmaps", self.settings)
        self.assertIn("ctx.clip();", self.settings)
        self.assertIn("ctx.globalAlpha = weight;", self.settings)
        self.assertIn("'rig-show-blend', 'rig-show-mesh'", self.settings)
        self.assertIn('press "Use this face" to see it live', self.settings)

    def test_two_designs_share_one_markup(self):
        # Quiet (calm paper minimalism) and Atelier (editorial couture) are
        # two complete designs over IDENTICAL markup: Atelier exists purely
        # as [data-design=atelier] overrides, so switching back restores
        # Quiet exactly and every feature behaves the same in both.
        self.assertIn(":root[data-design=atelier]", self.settings)
        self.assertIn('id="design-toggle"', self.settings)
        # Atelier is a different LAYOUT, not a reskin: a full-height
        # numbered contents rail replaces the top tabs, avatars become
        # editorial spreads instead of a card grid, and the studio tools
        # flip to controls-left / stage-right - all in CSS over the same
        # DOM, so Quiet's layout returns untouched on switch.
        self.assertIn("grid-template-columns:264px minmax(0,1fr);height:auto;min-height:100vh",
                      self.settings)
        # Verified on the live desktop 2026-07-31: without height:auto the
        # base html,body{height:100%} caps the grid at one viewport and the
        # sticky rail scrolls away, leaving an empty beige column.
        self.assertIn("counter(chapter,decimal-leading-zero)", self.settings)
        self.assertIn(".av{\n  display:grid;grid-template-columns:minmax(220px,300px)",
                      self.settings)
        self.assertIn(".rig-grid{direction:rtl}", self.settings)
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
