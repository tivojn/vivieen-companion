"""RobustVideoMatting as the preferred white-plate matte backend.

Verified live 2026-07-31 on a real take: temporally coherent alpha with the
model's own clean foreground colors (contour softness 1.33 vs the Vision
path's 1.08), ~18fps on Apple Silicon, and the color-fidelity gate passing
with protected deltas at zero. Never bundled: the packaged app ships no
torch, so it always falls back to the Vision path there.
"""
import os
import unittest
from pathlib import Path
from unittest import mock

from studio import motion

ROOT = Path(__file__).resolve().parents[1]


class RvmBackend(unittest.TestCase):
    def setUp(self):
        self._saved = dict(motion._RVM_STATE)
        self.addCleanup(lambda: motion._RVM_STATE.update(self._saved))

    def test_kill_switch_disables_the_backend(self):
        motion._RVM_STATE.update(loaded=None, failed=False)
        with mock.patch.dict(os.environ, {"VIVIEEN_NO_RVM": "1"}):
            self.assertIsNone(motion._rvm_runtime(lambda *a: None))
        self.assertTrue(motion._RVM_STATE["failed"])

    def test_matte_returns_none_when_backend_unavailable(self):
        motion._RVM_STATE.update(loaded=None, failed=True)
        self.assertIsNone(motion._rvm_matte([object()], lambda *a: None))

    def test_segment_frames_wiring(self):
        source = (ROOT / "studio" / "motion.py").read_text()
        # RVM runs over the WHOLE take before per-frame work so its
        # recurrent state carries; green takes keep the chroma key.
        self.assertIn(
            "rvm_frames = None if green_screen else _rvm_matte(frames, log)",
            source)
        self.assertIn('"robust-video-matting"', source)
        # Vision refinement and edge decontamination are fallback-only: the
        # model's clean foreground prediction must ship untouched.
        self.assertIn("if not green_screen and rvm_frames is None:", source)
        self.assertIn("elif rvm_frames is None:", source)
        # Green-spill neutralisation is a chroma-plate contract only.
        self.assertIn("check_green_spill=green_screen", source)

    def test_dev_requirements_carry_torchvision(self):
        text = (ROOT / "requirements-backend.txt").read_text()
        self.assertIn("torchvision", text)
        # And the PACKAGED set must not: no torch means no RVM (and no GPL
        # code) in the DMG.
        self.assertNotIn(
            "torchvision", (ROOT / "requirements-electron.txt").read_text())


if __name__ == "__main__":
    unittest.main()
