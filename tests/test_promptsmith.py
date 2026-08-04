"""AI prompt drafting: a rough gist becomes a field-ready prompt."""
import unittest
from pathlib import Path
from unittest import mock

from studio import promptsmith, wardrobe

ROOT = Path(__file__).resolve().parents[1]


def _expand(kind, gist, reply):
    with mock.patch.object(wardrobe, "_llm_route",
                           return_value=("llm/features/test/chat", "model")), \
         mock.patch.object(promptsmith, "_chat", return_value=reply) as chat:
        result = promptsmith.expand(kind, gist)
    return result, chat


class PromptSmith(unittest.TestCase):
    def test_walk_direction_is_cleaned_and_clipped(self):
        reply = ('```\n"She skips in place with a light bounce, arms swinging '
                 'wide, chin high."\n```')
        result, chat = _expand("walk", "happy skipping", reply)
        self.assertEqual(
            "She skips in place with a light bounce, arms swinging wide, "
            "chin high.", result)
        # The brief demands an in-place, repeatable, body-only direction.
        brief = chat.call_args.args[2]
        self.assertIn("IN PLACE", brief)
        self.assertIn("repeat", brief)

    def test_idle_brief_demands_a_loop(self):
        result, chat = _expand(
            "idle", "stretch and yawn",
            "She stretches tall, yawns, and settles back to standing.")
        self.assertTrue(result.startswith("She stretches"))
        brief = chat.call_args.args[2]
        self.assertIn("returns to that exact pose", brief)

    def test_long_output_is_clipped_to_the_field_limit(self):
        reply = " ".join(["She hops on one foot with real energy."] * 40)
        result, _chat_mock = _expand("walk", "hopping", reply)
        self.assertLessEqual(len(result), promptsmith.ACT_LIMIT)
        self.assertTrue(result.endswith("."))

    def test_body_brief_gets_the_structural_rules_appended(self):
        direction = ("A sleek photoreal look: fitted charcoal blazer over a "
                     "silk shell, slim cropped trousers, pointed leather "
                     "flats, fine gold jewellery, palette of graphite and "
                     "champagne.")
        result, _chat_mock = _expand("body", "sleek office minimalism", direction)
        self.assertIn(direction[:40], result)
        self.assertIn(wardrobe.STRUCTURAL_RULE, result)

    def test_body_brief_refuses_banned_garments(self):
        with self.assertRaisesRegex(RuntimeError, "banned garment"):
            _expand("body", "cozy winter look",
                    "A giant puffer jacket over baggy cargo trousers, with a "
                    "large tote bag carried in one hand, styled for cold "
                    "weather walks in the park with warm wool accessories.")

    def test_gist_and_kind_are_validated(self):
        with self.assertRaisesRegex(ValueError, "few words"):
            promptsmith.expand("walk", "hi")
        with self.assertRaisesRegex(ValueError, "unknown prompt kind"):
            promptsmith.expand("poetry", "a nice sonnet about walking")


class PromptDraftWiring(unittest.TestCase):
    def test_endpoint_exists(self):
        app = (ROOT / "server" / "app.py").read_text()
        self.assertIn('@app.post("/api/avatar/prompt/expand")', app)
        self.assertIn('kind: str = Field(pattern=r"^(body|walk|idle|move)$")', app)

    def test_all_three_fields_have_a_draft_button(self):
        settings = (ROOT / "web" / "settings.html").read_text()
        for button in ("body-prompt-ai", "body-walk-prompt-ai",
                       "body-motion-prompt-ai"):
            self.assertIn(f'id="{button}"', settings)
        # The body field revises its brief rather than redrafting over it;
        # the motion fields still draft from a gist, which is right for
        # them - there is no long brief to preserve.
        self.assertIn("rewriteFromKeyPoints($('#body-prompt')", settings)
        self.assertIn("draftPromptFromGist('walk'", settings)
        self.assertIn("draftPromptFromGist('idle'", settings)
        self.assertIn("'/api/avatar/prompt/expand'", settings)


if __name__ == "__main__":
    unittest.main()


class RewriteFromKeyPoints(unittest.TestCase):
    """One button: keep the brief, say what to change."""

    def test_a_base_turns_expansion_into_a_revision(self):
        # Two buttons used to live here and each destroyed something: one
        # expanded whatever was in the field over a full prompt, the other
        # threw the owner's edits away (owner, 2026-08-04).
        seen = {}

        def fake_chat(route, model, brief, ask, encoded):
            seen["ask"] = ask
            return "A long enough rewritten brief. " * 6

        with mock.patch.object(promptsmith, "_chat", fake_chat), \
             mock.patch.object(promptsmith.wardrobe, "_llm_route",
                               lambda: ("route", "model")), \
             mock.patch.object(promptsmith.wardrobe, "_finalise",
                               lambda text: text):
            promptsmith.expand("body", "keep the red bandana",
                               base="A charcoal suit, sharp shoulders.")
        self.assertIn("CURRENT BRIEF:", seen["ask"])
        self.assertIn("A charcoal suit, sharp shoulders.", seen["ask"])
        self.assertIn("KEY POINTS:", seen["ask"])
        self.assertIn("keep the red bandana", seen["ask"])

        # ...and with no brief yet it is still a plain draft
        with mock.patch.object(promptsmith, "_chat", fake_chat), \
             mock.patch.object(promptsmith.wardrobe, "_llm_route",
                               lambda: ("route", "model")), \
             mock.patch.object(promptsmith.wardrobe, "_finalise",
                               lambda text: text):
            promptsmith.expand("body", "a pirate captain")
        self.assertTrue(seen["ask"].startswith("Gist:"))

    def test_the_page_offers_one_button_not_two(self):
        page = (ROOT / "web" / "settings.html").read_text(encoding="utf-8")
        self.assertIn("Rewrite the prompt from my key points", page)
        self.assertNotIn("Rewrite for this portrait", page)
        self.assertNotIn("body-prompt-reset", page)
        # and it sends the current brief along, or it cannot revise it
        self.assertIn("gist: points, base", page)
