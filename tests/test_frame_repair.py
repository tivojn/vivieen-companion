"""Frame surgery: patch or drop ONE flagged frame of a packed clip.

Verified live 2026-07-31 on the user-reported artifact (Edge Idle frame 48,
a flash over the ankle): the temporal-median patch changed 118 alpha pixels
- the flash - and nothing else, then republished the runtime.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from studio import motion

ROOT = Path(__file__).resolve().parents[1]


def _make_clip(avatar_dir, frames):
    motion_dir = os.path.join(avatar_dir, "motion")
    os.makedirs(motion_dir)
    sheets = motion._pack_sheets(frames, motion_dir, "idle")
    metadata = {"idle": {
        "sheets": sheets, "frames": len(frames), "fps": 12,
        "frame_width": motion.TARGET_WIDTH,
        "frame_height": motion.TARGET_HEIGHT,
        "poster": "idle-poster.png", "alpha_video": "idle-alpha.mov",
        "bounds": [0, 0, motion.TARGET_WIDTH, motion.TARGET_HEIGHT],
    }}
    with open(os.path.join(motion_dir, "motion.json"), "w") as handle:
        json.dump(metadata, handle)
    return motion_dir


def _frames(count=10):
    frames = []
    for _ in range(count):
        frame = np.zeros(
            (motion.TARGET_HEIGHT, motion.TARGET_WIDTH, 4), np.uint8)
        frame[200:800, 250:470] = (40, 40, 200, 255)   # the body
        frames.append(frame)
    return frames


class FrameRepair(unittest.TestCase):
    def test_patch_removes_a_single_frame_flash(self):
        frames = _frames()
        frames[5] = frames[5].copy()
        frames[5][850:900, 300:360] = (255, 255, 255, 255)  # the flash
        with tempfile.TemporaryDirectory() as avatar_dir:
            _make_clip(avatar_dir, frames)
            metadata = motion.repair_frame(
                avatar_dir, "idle", 5, mode="patch", note="ankle flash")
            motion.commit_pending_build(avatar_dir)
            clip = metadata["idle"]
            repaired = motion._unpack_clip_frames(
                os.path.join(avatar_dir, "motion"), "idle", clip)
        self.assertEqual(10, clip["frames"])
        # The flash lost the temporal vote; the body survived untouched.
        self.assertEqual(0, int(repaired[5][870, 330, 3]))
        self.assertEqual(255, int(repaired[5][500, 350, 3]))
        self.assertEqual(
            [{"frame": 5, "mode": "patch"}],
            [{"frame": r["frame"], "mode": r["mode"]}
             for r in clip["repairs"]])

    def test_patch_repairs_a_multi_frame_run_against_clean_boundaries(self):
        # The real-world case (verified live 2026-07-31): a white flash on
        # the raised heel living across frames 1-6. Neighbours inside the
        # run share the defect, so each frame votes against the clean
        # frames just OUTSIDE the run instead.
        frames = _frames()
        for index in (3, 4, 5):
            frames[index] = frames[index].copy()
            frames[index][850:900, 300:360] = (255, 255, 255, 255)
        with tempfile.TemporaryDirectory() as avatar_dir:
            _make_clip(avatar_dir, frames)
            metadata = motion.repair_frame(
                avatar_dir, "idle", 3, frame_end=5, mode="patch")
            motion.commit_pending_build(avatar_dir)
            clip = metadata["idle"]
            repaired = motion._unpack_clip_frames(
                os.path.join(avatar_dir, "motion"), "idle", clip)
        for index in (3, 4, 5):
            self.assertEqual(0, int(repaired[index][870, 330, 3]),
                             f"flash survived in frame {index}")
            self.assertEqual(255, int(repaired[index][500, 350, 3]))
        receipt = clip["repairs"][-1]
        self.assertEqual((3, 5), (receipt["frame"], receipt["end"]))

    def test_drop_removes_the_frame_and_repacks(self):
        with tempfile.TemporaryDirectory() as avatar_dir:
            _make_clip(avatar_dir, _frames())
            metadata = motion.repair_frame(avatar_dir, "idle", 5, mode="drop")
            motion.commit_pending_build(avatar_dir)
            clip = metadata["idle"]
            repaired = motion._unpack_clip_frames(
                os.path.join(avatar_dir, "motion"), "idle", clip)
        self.assertEqual(9, clip["frames"])
        self.assertEqual(9, len(repaired))

    def test_repair_validates_inputs(self):
        with tempfile.TemporaryDirectory() as avatar_dir:
            with self.assertRaisesRegex(RuntimeError, "no motion"):
                motion.repair_frame(avatar_dir, "idle", 0)
            _make_clip(avatar_dir, _frames())
            with self.assertRaisesRegex(ValueError, "outside"):
                motion.repair_frame(avatar_dir, "idle", 99)
            with self.assertRaisesRegex(ValueError, "unknown repair mode"):
                motion.repair_frame(avatar_dir, "idle", 1, mode="regenerate")

    def test_server_and_inspector_wiring(self):
        app = (ROOT / "server" / "app.py").read_text()
        self.assertIn('@app.post("/api/avatar/motion/repair")', app)
        self.assertIn("def _repair_thread", app)
        marker = app.index("def _repair_thread")
        window = app[marker:marker + 2200]
        self.assertIn("_publish_runtime_atomic", window)
        self.assertIn("library.archive_motion", window)
        self.assertIn("frame_end: int | None", app)
        settings = (ROOT / "web" / "settings.html").read_text()
        self.assertIn('id="body-motion-fix"', settings)
        self.assertIn('id="body-motion-dropframe"', settings)
        self.assertIn("'/api/avatar/motion/repair'", settings)
        # The instruction is typed in on-screen numbers: "48" or "1-6".
        self.assertIn("or a run like 1-6", settings)
        self.assertIn("parseInt(match[1], 10) - 1", settings)


if __name__ == "__main__":
    unittest.main()
