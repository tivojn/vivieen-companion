"""The full-body brief is written from the portrait, not from a fixed paragraph.

These tests pin the things that actually matter: the brief adapts to the
subject, the rig-breaking garment families and carried props never survive,
glasses worn in the upload survive into every generated plate, and every failure
path lands on the static preset instead of breaking Full Body Studio.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import cv2

from studio import body, generate, wardrobe


ROOT = Path(__file__).resolve().parents[1]

FASHION = {
    "presentation": "feminine", "age_band": "young adult", "medium": "photograph",
    "register": "fashion-forward contemporary womenswear",
    "profession": "creative director", "palette": ["ivory", "scarlet", "warm gold"],
    "direction": (
        "Dress her in a precisely tailored scarlet single-breasted blazer worn "
        "over an ivory silk shell, with slim cropped cigarette trousers that hold "
        "a clean line from hip to ankle. Fabrics read as real wool crepe and "
        "washed silk with visible weave. Keep the palette to scarlet as the hero, "
        "warm gold as the single accent, and ivory as the neutral foundation. "
        "One statement detail only: slim gold pearl-drop earrings, with no "
        "competing necklace. Finish with pointed leather pumps at ninety "
        "millimetres. Everything stays opaque and impeccably fitted so the "
        "silhouette reads cleanly at full length."
    ),
}

HERO = {
    "presentation": "masculine", "age_band": "adult", "medium": "game art",
    "register": "mythic Chinese action-game hero", "profession": "warrior monk",
    "palette": ["lacquer black", "burnished bronze", "ember orange"],
    "direction": (
        "Render him in high-detail lacquered bronze scale armour over a fitted "
        "dark underlayer, with articulated shoulder plating, engraved bracers, "
        "and close-fitted greaves that keep the leg line readable. Surface the "
        "metal with edge wear, soot and micro-scratches at eight-K fidelity. "
        "Lacquer black carries the costume, burnished bronze is the hero metal, "
        "ember orange lights the trim. One statement element only: an engraved "
        "chest medallion. Light with a warm practical key and a cool rim to "
        "separate the armour silhouette from the background."
    ),
}


def _portrait(directory, name="head.png"):
    image = np.full((256, 256, 3), 180, dtype=np.uint8)
    cv2.circle(image, (128, 120), 60, (120, 90, 70), -1)
    path = os.path.join(directory, name)
    cv2.imwrite(path, image)
    return path


class WardrobeBanTests(unittest.TestCase):
    def test_banned_terms_catch_both_rig_breaking_families(self):
        for phrase in (
            "a heavy layered wool overcoat", "an oversized puffer jacket",
            "a long trench coat", "a flowing cape", "a draped shawl",
            "baggy cargo pants", "slouchy wide-leg trousers",
            "palazzo pants", "loose-fitting jeans", "a bulky parka",
        ):
            self.assertTrue(
                wardrobe.banned_terms(phrase),
                f"{phrase!r} should be rejected")

    def test_banned_terms_do_not_fire_on_legitimate_wardrobe(self):
        for phrase in (
            "a tailored scarlet blazer over an ivory silk shell",
            "slim cropped cigarette trousers and pointed leather pumps",
            "articulated bronze shoulder plating with engraved bracers",
            "a coated cotton pencil skirt with a fitted knit top",
        ):
            self.assertEqual(
                wardrobe.banned_terms(phrase), [],
                f"{phrase!r} should be allowed")

    def test_system_instruction_states_both_hard_bans(self):
        instruction = wardrobe.SYSTEM.lower()
        self.assertIn("hard ban", instruction)
        self.assertIn("heavy layering", instruction)
        self.assertIn("baggy", instruction)
        self.assertIn("wide-leg", instruction)

    def test_preset_prompt_carries_the_silhouette_rule(self):
        preset = wardrobe.preset_prompt()
        self.assertIn("never use heavy layering", preset.lower())
        self.assertIn("baggy", preset.lower())
        self.assertIn("opaque", preset.lower())


class WardrobeCompositionTests(unittest.TestCase):
    def _tailor(self, payload, directory, refresh=False):
        with mock.patch.object(wardrobe, "_llm_route",
                               return_value=("llm/features/x/chat", "m")), \
             mock.patch.object(wardrobe, "_chat",
                               return_value=json.dumps(payload)):
            return wardrobe.tailored_prompt(directory, refresh=refresh)

    def test_fashion_subject_gets_fashion_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            result = self._tailor(FASHION, directory)
        self.assertEqual(result["source"], "tailored")
        self.assertIn("scarlet", result["prompt"])
        self.assertIn("cigarette trousers", result["prompt"])
        self.assertEqual(result["traits"]["presentation"], "feminine")
        self.assertEqual(result["traits"]["medium"], "photograph")
        self.assertIn("scarlet", result["traits"]["palette"])
        self.assertNotIn(body.DEFAULT_BODY_PROMPT, result["prompt"])

    def test_game_hero_gets_costume_direction_not_office_separates(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            result = self._tailor(HERO, directory)
        self.assertEqual(result["source"], "tailored")
        self.assertIn("armour", result["prompt"])
        self.assertIn("rim", result["prompt"])
        self.assertEqual(result["traits"]["medium"], "game art")
        self.assertNotIn("blazer", result["prompt"])

    def test_every_tailored_prompt_appends_the_silhouette_rule(self):
        for payload in (FASHION, HERO):
            with tempfile.TemporaryDirectory() as directory:
                _portrait(directory)
                result = self._tailor(payload, directory)
            self.assertIn(wardrobe.SILHOUETTE_RULE, result["prompt"])

    def test_banned_garment_in_the_model_reply_falls_back_to_preset(self):
        rogue = dict(FASHION)
        rogue["direction"] = (
            "Wrap her in a heavy layered wool overcoat over baggy wide-leg "
            "trousers, styled with a long draped shawl and chunky boots for a "
            "relaxed oversized winter silhouette that hides the body line."
        )
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            result = self._tailor(rogue, directory)
        self.assertEqual(result["source"], "preset")
        self.assertEqual(result["prompt"], wardrobe.preset_prompt())
        self.assertIn("banned garment", result["error"])

    def test_markdown_fenced_json_is_still_parsed(self):
        fenced = "```json\n" + json.dumps(FASHION) + "\n```"
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat", return_value=fenced):
                result = wardrobe.tailored_prompt(directory)
        self.assertEqual(result["source"], "tailored")
        self.assertIn("scarlet", result["prompt"])


class WardrobeFallbackTests(unittest.TestCase):
    def test_missing_portrait_falls_back_without_calling_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(wardrobe, "_chat") as chat:
                result = wardrobe.tailored_prompt(directory)
        chat.assert_not_called()
        self.assertEqual(result["source"], "preset")
        self.assertEqual(result["prompt"], wardrobe.preset_prompt())

    def test_provider_failure_falls_back_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   side_effect=RuntimeError("no vision model")):
                result = wardrobe.tailored_prompt(directory)
        self.assertEqual(result["source"], "preset")
        self.assertIn("no vision model", result["error"])

    def test_unparseable_reply_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value="Sure! Here is a lovely outfit."):
                result = wardrobe.tailored_prompt(directory)
        self.assertEqual(result["source"], "preset")

    def test_short_brief_is_rejected(self):
        stub = dict(FASHION, direction="A red suit.")
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(stub)):
                result = wardrobe.tailored_prompt(directory)
        self.assertEqual(result["source"], "preset")


class WardrobeCacheTests(unittest.TestCase):
    def test_second_open_reuses_the_cache_without_a_second_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(FASHION)) as chat:
                first = wardrobe.tailored_prompt(directory)
                second = wardrobe.tailored_prompt(directory)
                self.assertEqual(chat.call_count, 1)
            self.assertFalse(first.get("cached"))
            self.assertTrue(second.get("cached"))
            self.assertEqual(first["prompt"], second["prompt"])
            self.assertTrue(os.path.isfile(
                os.path.join(directory, wardrobe.CACHE_NAME)))

    def test_refresh_rewrites_even_when_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(FASHION)):
                wardrobe.tailored_prompt(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(HERO)) as chat:
                refreshed = wardrobe.tailored_prompt(directory, refresh=True)
                self.assertEqual(chat.call_count, 1)
        self.assertIn("armour", refreshed["prompt"])

    def test_a_new_portrait_invalidates_the_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(FASHION)):
                wardrobe.tailored_prompt(directory)
            self.assertIsNotNone(wardrobe.cached_prompt(directory))
            image = np.full((256, 256, 3), 40, dtype=np.uint8)
            cv2.imwrite(os.path.join(directory, "head.png"), image)
            self.assertIsNone(wardrobe.cached_prompt(directory))

    def test_cached_prompt_is_none_without_a_portrait(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(wardrobe.cached_prompt(directory))

    def test_keyframe_is_used_when_there_is_no_head_plate(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory, name="keyframe.png")
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(FASHION)):
                result = wardrobe.tailored_prompt(directory)
        self.assertEqual(result["source"], "tailored")


class WardrobeRequestTests(unittest.TestCase):
    def test_reference_is_downscaled_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "head.png")
            cv2.imwrite(path, np.full((2048, 1536, 3), 200, dtype=np.uint8))
            encoded = wardrobe._encoded_reference(path)
        import base64
        decoded = cv2.imdecode(
            np.frombuffer(base64.b64decode(encoded), np.uint8),
            cv2.IMREAD_COLOR)
        self.assertEqual(max(decoded.shape[:2]), wardrobe.ANALYSIS_EDGE)

    def test_llm_route_rejects_an_injected_provider_name(self):
        with mock.patch.object(wardrobe, "_preference",
                               return_value={"selected": "llm|../../etc"}):
            with self.assertRaises(RuntimeError):
                wardrobe._llm_route()

    def test_preference_reader_refuses_paths_outside_its_root(self):
        self.assertEqual(wardrobe._preference("../../etc/passwd"), {})
        self.assertEqual(wardrobe._preference("llm/../../secret"), {})


class WardrobeIntegrationTests(unittest.TestCase):
    def test_body_prompt_accepts_a_tailored_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(HERO)):
                tailored = wardrobe.tailored_prompt(directory)["prompt"]
        plate = body._prompt({
            "style": "illustrated", "pose": "confident", "prompt": tailored})
        self.assertIn("armour", plate)
        self.assertIn("DECENCY FLOOR", plate)
        self.assertIn("IDENTITY LOCK", plate)

    def test_server_serves_the_cached_brief_and_exposes_the_rewrite_route(self):
        server = (ROOT / "server" / "app.py").read_text()
        self.assertIn("wardrobe.cached_prompt(directory)", server)
        self.assertIn('"/api/avatar/body/prompt"', server)
        self.assertIn("class BodyPromptRequest", server)
        self.assertIn("wardrobe.tailored_prompt", server)

    def test_settings_places_generate_directly_below_the_prompt(self):
        settings = (ROOT / "web" / "settings.html").read_text()
        prompt_at = settings.index('id="body-prompt"')
        progress_at = settings.index('id="body-progress"')
        generate_at = settings.index('id="body-generate"')
        identity_at = settings.index('class="body-identity"')
        motion_at = settings.index('id="body-walk-generate"')
        self.assertLess(prompt_at, progress_at)
        self.assertLess(progress_at, generate_at)
        self.assertLess(generate_at, identity_at)
        self.assertLess(generate_at, motion_at)
        self.assertIn("tailorBodyPrompt", settings)
        self.assertIn("setBodyPromptNote", settings)


class CarriedPropTests(unittest.TestCase):
    """No bag, and nothing in the hands - the third structural rule."""

    def test_banned_terms_catch_carried_props(self):
        for phrase in (
            "a structured leather handbag", "carrying a small clutch",
            "a quilted shoulder bag", "a slim leather tote",
            "a canvas backpack", "a black briefcase", "a leather satchel",
            "a crossbody strap across the chest", "a folded umbrella",
            "holding a takeaway coffee cup", "a hand-held paper fan",
            "a bouquet in her hands", "shopping bags",
        ):
            self.assertTrue(
                wardrobe.banned_terms(phrase),
                f"{phrase!r} should be rejected")

    def test_carry_ban_does_not_fire_on_legitimate_wardrobe(self):
        for phrase in (
            "a tailored scarlet blazer over an ivory silk shell",
            "slim cropped cigarette trousers that hold a clean line",
            "lacquer black carries the costume, bronze is the hero metal",
            "articulated bronze shoulder plating with engraved bracers",
        ):
            self.assertEqual(
                wardrobe.banned_terms(phrase), [],
                f"{phrase!r} should be allowed")

    def test_system_instruction_states_the_carry_ban(self):
        instruction = wardrobe.SYSTEM.lower()
        self.assertIn("carries nothing", instruction)
        self.assertIn("handbag", instruction)
        self.assertIn("both hands stay empty", instruction)

    def test_preset_and_tailored_prompts_both_carry_the_hands_rule(self):
        self.assertIn(wardrobe.HANDS_RULE, wardrobe.preset_prompt())
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(FASHION)):
                result = wardrobe.tailored_prompt(directory)
        self.assertEqual(result["source"], "tailored")
        self.assertIn(wardrobe.HANDS_RULE, result["prompt"])
        self.assertIn(wardrobe.SILHOUETTE_RULE, result["prompt"])

    def test_a_bag_in_the_model_reply_falls_back_to_preset(self):
        rogue = dict(FASHION)
        rogue["direction"] = (
            "Dress her in a precisely tailored scarlet blazer over an ivory silk "
            "shell with slim cigarette trousers, and finish the look with a "
            "structured black leather handbag carried in one hand, plus pointed "
            "leather pumps at ninety millimetres."
        )
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(rogue)):
                result = wardrobe.tailored_prompt(directory)
        self.assertEqual(result["source"], "preset")
        self.assertEqual(result["prompt"], wardrobe.preset_prompt())
        self.assertIn("handbag", result["error"])

    def test_the_appended_rules_are_never_read_as_a_violation(self):
        """The rules must name the props they forbid without self-rejecting."""
        self.assertTrue(wardrobe.banned_terms(wardrobe.STRUCTURAL_RULE))
        with tempfile.TemporaryDirectory() as directory:
            _portrait(directory)
            with mock.patch.object(wardrobe, "_llm_route",
                                   return_value=("llm/features/x/chat", "m")), \
                 mock.patch.object(wardrobe, "_chat",
                                   return_value=json.dumps(FASHION)):
                result = wardrobe.tailored_prompt(directory)
        self.assertEqual(result["source"], "tailored")

    def test_the_structural_rules_always_fit_inside_the_prompt_limit(self):
        longest = "x" * (wardrobe.PROMPT_LIMIT * 2)
        stub = dict(FASHION, direction=longest)
        direction, _traits = wardrobe._parse(json.dumps(stub))
        self.assertIn(wardrobe.STRUCTURAL_RULE, wardrobe._finalise(direction))

    def test_every_body_plate_bans_carried_objects(self):
        for view in body.BODY_VIEWS:
            plate = body._prompt({}, view=view)
            self.assertIn("CARRY NOTHING", plate)
            self.assertIn("handbag", plate)
            self.assertIn("both hands are completely empty", plate.lower())


class EyewearLockTests(unittest.TestCase):
    """Glasses worn in the upload survive into head.png and keyframe.png."""

    def test_head_prompt_preserves_existing_glasses(self):
        prompt = generate.HEAD_PROMPT
        self.assertIn("EYEWEAR", prompt)
        lowered = prompt.lower()
        self.assertIn("eyeglasses", lowered)
        self.assertIn("never remove them", lowered)
        self.assertIn("do not add any", lowered)

    def test_head_prompt_no_longer_strips_glasses_as_an_accessory(self):
        framing = generate.HEAD_PROMPT.split("FRAMING", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn(
            "accessories, props, or text anywhere in the image.", framing)
        self.assertIn("eyeglasses already worn", framing)

    def test_head_prompt_version_moved_so_cached_heads_rebuild(self):
        self.assertGreaterEqual(generate.HEAD_PROMPT_VERSION, 3)

    def test_keyframe_is_cropped_from_the_generated_head(self):
        source = (ROOT / "studio" / "build.py").read_text()
        # the call moved inside the frontality-retry loop (2026-08-01), but
        # the keyframe must still be cropped from the generated head
        self.assertIn(
            "prep.build_keyframe(\n                head_path, staged_keyframe", source)

    def test_body_plates_keep_the_reference_glasses(self):
        for view in body.BODY_VIEWS:
            plate = body._prompt({}, view=view)
            self.assertIn("eyeglasses", plate)
            self.assertIn("never remove them", plate)

    def test_body_plates_ban_green_everywhere(self):
        # Owner rule 2026-08-01: downstream alpha keying misreads green as
        # background, so no plate may contain green - clothing, props,
        # backdrop, or lighting cast - in any of the three views. An art
        # direction asking for green gets a substitute color instead.
        for view in body.BODY_VIEWS:
            plate = body._prompt({}, view=view)
            self.assertIn("NO GREEN", plate)
            self.assertIn("no green clothing", plate)
            self.assertIn("never green or green-tinted", plate)
            self.assertIn("substitute a different color", plate)
            # Second owner rule, same day: white wardrobe dissolves into
            # the light studio backdrop and shreds the cutout silhouette.
            self.assertIn("NO WHITE WARDROBE", plate)
            self.assertIn("no white shoes", plate)
            self.assertIn("clearly non-white, non-green color", plate)


if __name__ == "__main__":
    unittest.main()


class KeepFromThePortrait(unittest.TestCase):
    """An add-on the owner types before a build: what must survive it."""

    def test_a_note_rides_with_the_prompt_never_instead_of_it(self):
        # It used to be DROPPED the moment an expanded prompt existed -
        # which is the normal path - so "keep his bandana" never reached
        # the model, and a character came back with the right face and
        # none of what made him recognisable (owner, 2026-08-04).
        sys.path.insert(0, str(ROOT))
        from studio import body
        house = body._direction({"prompt": "A tailored navy dress."})
        self.assertEqual(house, "A tailored navy dress.")
        both = body._direction({"prompt": "A tailored navy dress.",
                                "notes": "keep his bandana"})
        self.assertIn("A tailored navy dress.", both)
        self.assertIn("keep his bandana", both)
        # a note on its own still gets the house prompt behind it
        alone = body._direction({"notes": "keep her earrings"})
        self.assertIn(body.DEFAULT_BODY_PROMPT, alone)
        self.assertIn("keep her earrings", alone)

    def test_the_head_plate_is_told_what_to_keep(self):
        # The bandana is a HEAD accessory, and normalising the face is
        # exactly what removed it.
        source = (ROOT / "studio" / "generate.py").read_text(encoding="utf-8")
        self.assertIn("keep=\"\"", source)
        self.assertIn("MUST KEEP from the source portrait:", source)
        builder = (ROOT / "studio" / "build.py").read_text(encoding="utf-8")
        self.assertIn("keep=notes", builder)
        # and it reaches the worker from the UI
        self.assertIn('b.add_argument("--keep"', builder)

    def test_a_note_forces_the_face_to_be_rebuilt(self):
        # One-click SKIPS the face when it is already built. With a note,
        # skipping means the one thing the owner asked to keep never comes
        # back - his bandana lives on the head plate (owner, 2026-08-04).
        source = (ROOT / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn('if manifest.get("status") != "ready" or notes:', source)

    def test_the_ui_asks_before_a_long_build(self):
        page = (ROOT / "web" / "settings.html").read_text(encoding="utf-8")
        self.assertIn("function askKeep(", page)
        self.assertIn("Add extra comments, e.g. keep the character", page)
        # both long builds ask
        self.assertIn("if (what === 'build' || what === 'pipeline')", page)
