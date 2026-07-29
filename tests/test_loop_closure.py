"""Loop-closure gate.

These cases exist because two real clips shipped past every other quality gate:
a cartwheel that performed the move once and then stood still, and an edge idle
whose head kept lowering so the loop popped on every repeat. Contact, extremity,
colour and trajectory checks all passed both of them, because every individual
frame was fine. Only the endpoints disagreed.
"""

import unittest

import numpy as np

from studio import motion


HEIGHT, WIDTH = 384, 256


def _frame(parts):
    """Build an RGBA frame whose alpha is the union of (top, bottom, left, right)."""
    frame = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    for top, bottom, left, right in parts:
        frame[top:bottom, left:right, 3] = 255
    return frame


HEAD = (20, 80, 110, 146)
SHOULDERS = (80, 128, 95, 161)
TORSO = (128, 230, 95, 161)
LEGS_APART = [(230, 370, 100, 120), (230, 370, 136, 156)]
LEGS_CROSSED = [(230, 370, 110, 130), (230, 370, 126, 146)]
HEAD_LOWERED = (45, 105, 110, 146)


class LoopClosureTest(unittest.TestCase):
    def test_identical_endpoints_close(self):
        frame = _frame([HEAD, SHOULDERS, TORSO, *LEGS_APART])
        quality = motion._silhouette_closure_quality([frame, frame.copy(), frame.copy()])
        self.assertTrue(quality["available"])
        self.assertTrue(quality["valid"])
        self.assertEqual(quality["body_overlap"], 1.0)
        self.assertEqual(quality["upper_overlap"], 1.0)

    def test_swinging_legs_still_close(self):
        """A walk legitimately ends with its legs elsewhere; that is not a defect.

        This is the case that rules out judging closure on whole-body overlap
        alone: the approved production walk scores 0.79 there, lower than a
        genuinely broken idle at 0.83.
        """
        first = _frame([HEAD, SHOULDERS, TORSO, *LEGS_APART])
        last = _frame([HEAD, SHOULDERS, TORSO, *LEGS_CROSSED])
        quality = motion._silhouette_closure_quality([first, last])
        self.assertTrue(quality["valid"], quality["reason"])
        self.assertEqual(quality["upper_overlap"], 1.0)
        self.assertLess(quality["body_overlap"], 1.0)

    def test_drifting_head_is_rejected(self):
        first = _frame([HEAD, SHOULDERS, TORSO, *LEGS_APART])
        last = _frame([HEAD_LOWERED, SHOULDERS, TORSO, *LEGS_APART])
        quality = motion._silhouette_closure_quality([first, last])
        self.assertFalse(quality["valid"])
        self.assertIn("head and shoulders drift", quality["reason"])
        # The body barely notices a slipping head, which is exactly why the
        # upper-body floor has to carry this decision.
        self.assertGreater(quality["body_overlap"], motion.LOOP_CLOSURE_BODY_MINIMUM)
        self.assertLess(quality["upper_overlap"], motion.LOOP_CLOSURE_UPPER_MINIMUM)

    def test_one_shot_action_is_rejected(self):
        """Upright start, inverted-then-landed finish: the cartwheel failure."""
        first = _frame([HEAD, SHOULDERS, TORSO, *LEGS_APART])
        last = _frame([(200, 260, 40, 216)])
        quality = motion._silhouette_closure_quality([first, last])
        self.assertFalse(quality["valid"])
        self.assertIn("different pose", quality["reason"])
        self.assertLess(quality["body_overlap"], motion.LOOP_CLOSURE_BODY_MINIMUM)

    def test_short_or_alphaless_input_is_unavailable_not_valid(self):
        frame = _frame([HEAD, SHOULDERS, TORSO, *LEGS_APART])
        short = motion._silhouette_closure_quality([frame])
        self.assertFalse(short["available"])
        self.assertFalse(short["valid"])

        opaque = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
        alphaless = motion._silhouette_closure_quality([opaque, opaque.copy()])
        self.assertFalse(alphaless["available"])
        self.assertFalse(alphaless["valid"])

    def test_empty_endpoints_are_unavailable(self):
        blank = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
        quality = motion._silhouette_closure_quality([blank, blank.copy()])
        self.assertFalse(quality["available"])
        self.assertFalse(quality["valid"])

    def test_thresholds_separate_the_measured_clips(self):
        """Guard the calibration itself against a well-meaning tweak.

        Measured on real alpha: good clips scored 0.941 / 0.938 / 0.974 / 0.963
        upper-body, bad clips 0.884 and 0.000.
        """
        self.assertLess(motion.LOOP_CLOSURE_UPPER_MINIMUM, 0.938)
        self.assertGreater(motion.LOOP_CLOSURE_UPPER_MINIMUM, 0.884)
        self.assertLess(motion.LOOP_CLOSURE_BODY_MINIMUM, 0.794)
        self.assertGreater(motion.LOOP_CLOSURE_BODY_MINIMUM, 0.521)


class UpperBandEdgeCaseTest(unittest.TestCase):
    """The head band is only evidence when at least one endpoint occupies it."""

    @staticmethod
    def _frame(top_filled, bottom_filled, height=384, width=256):
        frame = np.zeros((height, width, 4), dtype=np.uint8)
        cut = height // 3
        if top_filled:
            frame[:cut, 80:176, 3] = 255
        if bottom_filled:
            frame[cut:, 40:216, 3] = 255
        return frame

    def test_band_empty_at_both_endpoints_is_agreement_not_drift(self):
        # A loop cut mid-inversion leaves the head band empty in both frames.
        # Scoring that as 0.0 rejected clips whose endpoints actually matched.
        low = self._frame(top_filled=False, bottom_filled=True)
        quality = motion._silhouette_closure_quality([low, low.copy()])
        self.assertTrue(quality["available"])
        self.assertTrue(quality["valid"])
        self.assertFalse(quality["upper_available"])
        self.assertIsNone(quality["upper_overlap"])
        self.assertIn("empty at both endpoints", quality["reason"])

    def test_band_occupied_at_one_endpoint_only_still_reads_as_drift(self):
        # Upright at one end and inverted at the other is the real failure the
        # band check exists for, and it must survive the both-empty exemption.
        upright = self._frame(top_filled=True, bottom_filled=True)
        inverted = self._frame(top_filled=False, bottom_filled=True)
        quality = motion._silhouette_closure_quality([upright, inverted])
        self.assertTrue(quality["upper_available"])
        self.assertEqual(quality["upper_overlap"], 0.0)
        self.assertFalse(quality["valid"])
        self.assertIn("drift", quality["reason"])


class TraversalPromptTest(unittest.TestCase):
    def test_traversal_styles_demand_a_repeating_loop(self):
        """Traversal styles used to receive no loop contract at all."""
        prompt = motion._walk_video_prompt("cartwheel")
        self.assertIn("REPEATING TRAVERSAL LOOP", prompt)
        self.assertIn("do not perform the move once", prompt)

    def test_gait_styles_keep_their_two_step_contract(self):
        prompt = motion._walk_video_prompt("office")
        self.assertIn("COMPLETE TWO-STEP GAIT CYCLE", prompt)
        self.assertNotIn("REPEATING TRAVERSAL LOOP", prompt)


if __name__ == "__main__":
    unittest.main()
