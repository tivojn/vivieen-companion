"""Full gait-cycle gating: a walk loop must hold one complete period.

A half cycle cut mid-stance to mid-stance closes at the seam and shows two
zero crossings, so seam tests alone cannot reject it. These tests pin the
period and stride-coverage gates that make "one arm goes from behind to front
and back to behind" an enforced property of every shipped walk loop.
"""
import math
import unittest

import numpy as np

from studio import motion


def _joint(x, y, confidence=0.9):
    return {"x": float(x), "y": float(y), "confidence": confidence}


def gait_poses(count, period, amplitude=40.0):
    """Synthetic contralateral walker: arms and legs are anti-phase sinusoids."""
    poses = []
    for index in range(count):
        phase = 2 * math.pi * index / period
        swing = amplitude * math.sin(phase)
        poses.append({"joints": {
            "neck": _joint(200, 100),
            "root": _joint(200, 300),
            "left_shoulder": _joint(180, 120),
            "right_shoulder": _joint(220, 120),
            "left_elbow": _joint(178, 200),
            "right_elbow": _joint(222, 200),
            # Left arm forward exactly when the left leg is back.
            "left_wrist": _joint(180 + swing, 320),
            "right_wrist": _joint(220 - swing, 320),
            "left_hip": _joint(185, 300),
            "right_hip": _joint(215, 300),
            "left_knee": _joint(185, 450),
            "right_knee": _joint(215, 450),
            "left_ankle": _joint(185 - 1.5 * swing, 600),
            "right_ankle": _joint(215 + 1.5 * swing, 600),
        }})
    return poses


def blank_frames(count, width=48, height=72):
    frame = np.full((height, width, 4), 255, dtype=np.uint8)
    return [frame.copy() for _ in range(count)]


class SourceGaitProfileTests(unittest.TestCase):
    def test_detects_dominant_period(self):
        for period in (24, 48, 72):
            profile = motion._source_gait_profile(gait_poses(150, period))
            self.assertIsNotNone(profile)
            self.assertIsNotNone(profile["period"], f"period {period}")
            self.assertLessEqual(
                abs(profile["period"] - period), 3, f"period {period}")

    def test_single_cycle_take_reports_no_period(self):
        # An approved original that is itself one loop cannot show a repeat,
        # so the period gate must stand down rather than reject it.
        profile = motion._source_gait_profile(gait_poses(26, 26))
        self.assertIsNotNone(profile)
        self.assertIsNone(profile["period"])


class CycleGateTests(unittest.TestCase):
    def test_full_cycle_window_passes(self):
        poses = gait_poses(150, 48)
        quality = motion._pose_cycle_metrics(poses, 0, 48)
        self.assertTrue(quality["valid"], quality["reason"])
        self.assertGreaterEqual(quality["cycle_coverage"], 0.9)
        self.assertGreaterEqual(quality["stride_coverage"], 0.85)

    def test_half_cycle_window_is_rejected(self):
        # Starts and ends at mid-stance: seam closes, two crossings exist,
        # yet the arm never reaches behind. Only the new gates catch it.
        poses = gait_poses(150, 48)
        quality = motion._pose_cycle_metrics(poses, 0, 24)
        self.assertFalse(quality["valid"])
        self.assertIn("one full gait cycle", quality["reason"])

    def test_low_motion_sliver_is_rejected_by_coverage(self):
        # A window whose stride travel is a fraction of what the source
        # performs (the shipped 2026-07-29 failure covered 18%).
        poses = gait_poses(150, 48, amplitude=40.0)
        for pose in poses[:30]:
            for name in ("left_wrist", "right_wrist",
                         "left_ankle", "right_ankle"):
                joint = pose["joints"][name]
                base = 180 if name.startswith("left_w") else (
                    220 if name.startswith("right_w") else
                    185 if name.startswith("left_a") else 215)
                joint["x"] = base + (joint["x"] - base) * 0.15
        quality = motion._pose_cycle_metrics(poses, 2, 26)
        self.assertFalse(quality["valid"])
        self.assertIn("covers only part of the source", quality["reason"])


class LoopSelectionTests(unittest.TestCase):
    def test_selects_one_full_cycle_even_against_style_target(self):
        # Style target says ~1.05s (25 frames) but the walker's true cycle
        # is 48 frames: the selection must land on a whole cycle anyway.
        period = 48
        poses = gait_poses(150, period)
        frames = blank_frames(150)
        _selected, start, end = motion._select_loop(
            frames, 24, 1.05, 0.85, 3.4,
            poses=poses, require_pose_cycle=True)
        length = end - start
        self.assertGreaterEqual(length, round(0.85 * period))
        quality = motion._pose_cycle_metrics(poses, start, end)
        self.assertTrue(quality["valid"], quality["reason"])

    def test_too_slow_cadence_reports_cycle_duration(self):
        # One cycle takes 3.8s; the ceiling is 2.25s. Every window fails and
        # the error names the real cadence so the retry log is actionable.
        poses = gait_poses(150, 92)
        frames = blank_frames(150)
        with self.assertRaises(RuntimeError) as raised:
            motion._select_loop(
                frames, 24, 1.05, 0.85, 2.25,
                poses=poses, require_pose_cycle=True)
        self.assertIn("cadence is too slow", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
