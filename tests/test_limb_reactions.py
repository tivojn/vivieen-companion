"""Limb reaction baking: local warps of the standing plate, no generation."""
import unittest

import numpy as np

from studio import limbs


def synthetic_plate(width=400, height=700):
    """An RGBA figure: torso block, one hanging arm, distinct hand color."""
    plate = np.zeros((height, width, 4), dtype=np.uint8)
    plate[80:520, 140:260] = (90, 110, 140, 255)      # torso
    plate[200:470, 262:300] = (60, 160, 60, 255)      # arm along the torso
    plate[470:520, 262:300] = (40, 40, 220, 255)      # hand (distinct)
    return plate


def synthetic_pose():
    joint = lambda x, y: {"x": float(x), "y": float(y), "confidence": 0.9}
    return {"joints": {
        "left_shoulder": joint(281, 210),
        "left_elbow": joint(281, 340),
        "left_wrist": joint(281, 470),
        "right_shoulder": joint(150, 210),
        "right_elbow": joint(120, 340),
        "right_wrist": joint(110, 470),
        "neck": joint(200, 150),
    }}


class LimbReactionTests(unittest.TestCase):
    def setUp(self):
        self.plate = synthetic_plate()
        self.reactions = limbs.build(
            self.plate, synthetic_pose(), log=lambda _m: None)

    def test_bakes_arms_and_shrug(self):
        self.assertEqual(
            {"arm_l", "arm_r", "shrug"}, set(self.reactions))
        for reaction in self.reactions.values():
            self.assertEqual(len(reaction["patches"]), limbs.STATES)
            x, y, w, h = reaction["box"]
            for patch in reaction["patches"]:
                self.assertEqual(patch.shape, (h, w, 4))

    def test_rest_state_is_the_plate(self):
        for reaction in self.reactions.values():
            x, y, w, h = reaction["box"]
            base = self.plate[y:y + h, x:x + w]
            difference = np.abs(
                reaction["patches"][0].astype(np.int16) - base.astype(np.int16))
            self.assertLessEqual(int(difference.max()), 2)

    def test_peak_state_moves_the_hand(self):
        reaction = self.reactions["arm_l"]
        x, y, w, h = reaction["box"]
        hand = (40, 40, 220)

        def hand_centroid(patch):
            mask = np.all(np.abs(
                patch[:, :, :3].astype(np.int16) - hand) < 60, axis=2)
            mask &= patch[:, :, 3] > 128
            ys, xs = np.nonzero(mask)
            self.assertTrue(len(xs), "hand pixels vanished")
            return float(xs.mean()), float(ys.mean())

        rest_x, rest_y = hand_centroid(reaction["patches"][0])
        peak_x, peak_y = hand_centroid(reaction["patches"][-1])
        travel = ((peak_x - rest_x) ** 2 + (peak_y - rest_y) ** 2) ** 0.5
        self.assertGreater(travel, 6.0, "peak state barely moved the hand")
        self.assertLess(peak_y, rest_y, "the hand must LIFT")

    def test_peak_state_keeps_silhouette_area(self):
        # The warp stretches, never tears: opaque area stays within a few
        # percent of rest, which would catch holes or dropped limbs.
        for name, reaction in self.reactions.items():
            rest = int((reaction["patches"][0][:, :, 3] > 128).sum())
            peak = int((reaction["patches"][-1][:, :, 3] > 128).sum())
            self.assertLess(abs(peak - rest) / max(rest, 1), 0.08, name)


class PublishRobustnessTests(unittest.TestCase):
    def test_reaction_bake_failure_never_blocks_publish(self):
        # Reactions are an enhancement: a Vision failure on some future body
        # must log and skip, never fail the runtime publish itself.
        import tempfile
        from unittest import mock
        from studio import export
        body_meta = {"pose": {"joints": {}}}
        lines = []
        with tempfile.TemporaryDirectory() as destination:
            with mock.patch.object(
                    export, "_body_pose",
                    side_effect=RuntimeError("vision down")):
                export._publish_body_extras(
                    "/nonexistent-body-dir", body_meta, destination,
                    log=lines.append)
        self.assertNotIn("reactions", body_meta)
        self.assertTrue(any("skipped" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
