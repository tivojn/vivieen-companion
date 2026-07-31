"""Show Me Some Moves: a third motion kind beside Horizon Walk and Edge Idle.

Every move is a free act - performed in place on the 9:16 plate, first
frame equals last - so the idle free-act pipeline carries it end to end;
only the choreography prompt and the runtime trigger are new.
"""
import unittest
from pathlib import Path

from studio import library, motion

ROOT = Path(__file__).resolve().parents[1]


class MoveStyles(unittest.TestCase):
    def test_presets_and_default(self):
        self.assertEqual(
            {"viral", "hiphop", "kpop", "ballet", "salsa"},
            set(motion.MOVE_STYLES))
        self.assertEqual("viral", motion.DEFAULT_MOVE_STYLE)
        for style_id in motion.MOVE_STYLES:
            style = motion.resolve_move_style(style_id)
            self.assertEqual("free", style["validation"])
            self.assertGreaterEqual(len(style["prompt"]), 60)

    def test_the_signature_viral_prompt_is_verbatim(self):
        prompt = motion.resolve_move_style("viral")["prompt"]
        self.assertIn("seductive TikTok star captivating a live crowd", prompt)
        self.assertIn("standout signature move", prompt)
        self.assertIn("bold, confident pose", prompt)

    def test_custom_move_round_trips_through_its_receipt(self):
        style = motion.resolve_move_style(
            "custom", "vogue with sharp arm frames and a dramatic pose")
        receipt = motion._move_style_receipt(style)
        self.assertEqual(style["prompt"], receipt["prompt"])
        self.assertEqual(style, motion.resolve_move_style(receipt))
        self.assertNotIn("prompt", motion._move_style_receipt("viral"))
        with self.assertRaisesRegex(ValueError, "at least 12 characters"):
            motion.resolve_move_style("custom", "spin")
        with self.assertRaisesRegex(ValueError, "unknown move style"):
            motion.resolve_move_style("moonwalk")

    def test_move_prompts_ride_the_free_act_contracts(self):
        keyframe = motion._move_keyframe_prompt("existing outfit", "viral")
        video = motion._move_video_prompt("viral")
        self.assertIn("seductive TikTok star", keyframe)
        self.assertIn("seductive TikTok star", video)
        # The proven free-act mechanics: loopable opening pose, first frame
        # equals last, white plate, in-place performance.
        self.assertIn("performance loop can begin and end on", keyframe)
        self.assertIn("EXACT first frame and the EXACT final frame", video)
        self.assertIn("pure white", video)

    def test_move_is_a_first_class_kind(self):
        self.assertIn("move", library.MOTION_KINDS)
        self.assertEqual(("move_style",), library._CLIP_KEYS["move"])
        app = (ROOT / "server" / "app.py").read_text()
        self.assertIn('pattern=r"^(walk|idle|move|both)$"', app)
        self.assertIn('"has_move": has_move,', app)
        export = (ROOT / "studio" / "export.py").read_text()
        self.assertIn('("walk", "idle", "move")', export)


class MoveRuntime(unittest.TestCase):
    def test_hair_double_tap_and_menu_trigger_the_show(self):
        renderer = (ROOT / "web" / "index.html").read_text()
        self.assertIn("function toggleMoveShow", renderer)
        self.assertIn("if(part==='hair'&&toggleMoveShow())return true;", renderer)
        self.assertIn("loadMotion('move')", renderer)
        self.assertIn("moveShowActive(now)&&drawMotionClip('move',now)", renderer)
        # A tap off the hair ends the show; the show counts as engagement so
        # Edge Idle never docks mid-performance.
        self.assertIn("moveShowActive(now)&&part!=='hair'", renderer)
        self.assertIn("now<moveShowUntil||", renderer)
        self.assertIn("SHELL.onPetMoves", renderer)
        preload = (ROOT / "electron" / "preload.cjs").read_text()
        self.assertIn("vivieen:pet-moves", preload)
        main = (ROOT / "electron" / "main.cjs").read_text()
        self.assertIn("'Moves · 2×tap hair'", main)
        self.assertIn("send('vivieen:pet-moves')", main)

    def test_settings_offers_the_move_studio(self):
        settings = (ROOT / "web" / "settings.html").read_text()
        for style in ("viral", "hiphop", "kpop", "ballet", "salsa", "custom"):
            self.assertIn(f'data-move-style="{style}"', settings)
        self.assertIn('id="body-move-generate"', settings)
        self.assertIn('id="body-move-sets"', settings)
        self.assertIn("draftPromptFromGist('move'", settings)
        self.assertIn("renderMotionSets('move')", settings)


if __name__ == "__main__":
    unittest.main()
