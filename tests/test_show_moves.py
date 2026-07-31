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


class WhitePlateMatte(unittest.TestCase):
    def test_refinement_cuts_plate_pockets_and_keeps_cream_wardrobe(self):
        """Verified against the real defect 2026-07-31: a plate pocket at a
        hair-shoulder gap shipped opaque and flashed white; cream shoes
        measured whiteness <=0.68 vs plate 1.0."""
        import numpy as np
        from studio import motion
        height, width = 200, 200
        source = np.full((height, width, 3), 255, np.uint8)     # pure plate
        source[40:160, 60:140] = (30, 30, 190)                  # red dress
        source[150:196, 90:112] = (225, 236, 245)               # cream shoe
        source[70:100, 120:138] = 255                           # plate pocket
        source[70:80, 138:200] = 255                            # gap to plate
        alpha = np.zeros((height, width), np.uint8)
        alpha[38:198, 55:145] = 255      # Vision mask: includes the pocket
        rgba = np.dstack([source, alpha])
        refined = motion._refine_white_matte(source, rgba)
        out = refined[:, :, 3]
        self.assertLess(int(out[85, 130]), 40)      # pocket removed
        self.assertEqual(255, int(out[100, 100]))   # dress kept
        self.assertGreater(int(out[170, 100]), 200)  # cream shoe kept

    def test_refinement_runs_before_temporal_repair_on_white_takes(self):
        source = (ROOT / "studio" / "motion.py").read_text()
        marker = source.index("def _segment_frames")
        window = source[marker:marker + 4600]
        self.assertIn("_refine_white_matte(frames[index], segmented[index])",
                      window)
        # It is the VISION-fallback sharpener; RVM output ships untouched.
        self.assertIn("if not green_screen and rvm_frames is None:", window)


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

    def test_left_preview_panel_has_a_moves_tab(self):
        settings = (ROOT / "web" / "settings.html").read_text()
        self.assertIn('data-body-mode="move"', settings)
        self.assertIn('id="body-move-file-count"', settings)
        self.assertIn("['body', 'walk', 'idle', 'move'].includes(mode)", settings)
        self.assertIn("kind === 'move' ? 'Show Me Some Moves'", settings)

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
