"""Crown headroom: a keyframe must never ship with the hair against the edge.

The first Vivieen plates cut the top of the hair because square_crop clamped
its window into the source image: when the wanted box ran off the top edge the
box slid DOWN instead, and the crown left the frame with no warning at all.
The runtime then had nothing above the hairline, so the desktop pet wore a flat
top - and the nod animation, which swings the crown ~1.5x the eye line, made it
worse on every idle breath.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from studio import face, prep  # noqa: E402


def landmarks(face_width=200.0, centre_x=500.0, eye_y=400.0):
    """Minimal landmark array carrying only what square_crop reads."""
    oval = np.asarray(face.FACE_OVAL, dtype=int)
    count = int(max(oval.max(), face.EYE_L_OUT, face.EYE_R_OUT)) + 1
    lm = np.zeros((count, 2), dtype=np.float64)
    lm[oval, 0] = np.linspace(centre_x - face_width / 2,
                              centre_x + face_width / 2, oval.size)
    lm[oval, 1] = eye_y
    lm[face.EYE_L_OUT] = (centre_x - face_width / 4, eye_y)
    lm[face.EYE_R_OUT] = (centre_x + face_width / 4, eye_y)
    return lm


class SquareCropTest(unittest.TestCase):
    def test_tall_hair_pushes_the_window_up(self):
        lm = landmarks()
        image = np.zeros((1200, 1000, 3), dtype=np.uint8)
        crown = 60.0                      # a bun, far above the default box
        _, y0, size = prep.square_crop(image, lm, crown)
        self.assertLessEqual(y0, crown - size * prep.CROWN_CLEARANCE)
        self.assertGreaterEqual(crown - y0, size * prep.CROWN_CLEARANCE)

    def test_window_is_not_clamped_into_the_source(self):
        """The regression itself: an overhanging box must stay overhanging."""
        lm = landmarks()
        image = np.zeros((1200, 1000, 3), dtype=np.uint8)
        _, y0, _ = prep.square_crop(image, lm, 20.0)
        self.assertLess(y0, 0, "box was clamped back into the image")

    def test_flat_crop_still_uses_the_eye_line(self):
        lm = landmarks()
        image = np.zeros((1200, 1000, 3), dtype=np.uint8)
        _, y0, size = prep.square_crop(image, lm, 300.0)
        self.assertEqual(y0, round(400.0 - size * prep.EYE_LINE))

    def test_fallback_estimate_clears_the_hair(self):
        lm = landmarks()
        image = np.zeros((1200, 1000, 3), dtype=np.uint8)
        _, y0, size = prep.square_crop(image, lm, None)
        assumed_crown = 400.0 - 200.0 * prep.HAIR_ABOVE_EYES
        self.assertGreaterEqual(assumed_crown - y0, size * prep.CROWN_CLEARANCE)


class TakeSquareTest(unittest.TestCase):
    def test_overhang_is_padded_not_shifted(self):
        image = np.zeros((1200, 1000, 3), dtype=np.uint8)
        image[100] = (7, 9, 11)                       # the crown row
        crop = prep.take_square(image, 200, -80, 600)
        self.assertEqual(crop.shape, (600, 600, 3))
        self.assertEqual(tuple(int(v) for v in crop[180, 0]), (7, 9, 11))

    def test_top_fill_replaces_replicated_hair(self):
        image = np.zeros((1200, 1000, 3), dtype=np.uint8)
        image[0] = (40, 50, 60)                       # hair against the edge
        crop = prep.take_square(image, 200, -50, 400, (200.0, 201.0, 202.0))
        self.assertEqual(tuple(int(v) for v in crop[10, 5]), (200, 201, 202))
        self.assertEqual(tuple(int(v) for v in crop[50, 5]), (40, 50, 60))

    def test_replicate_is_the_default(self):
        image = np.zeros((1200, 1000, 3), dtype=np.uint8)
        image[0] = (40, 50, 60)
        crop = prep.take_square(image, 200, -50, 400)
        self.assertEqual(tuple(int(v) for v in crop[10, 5]), (40, 50, 60))


class WarningTest(unittest.TestCase):
    def metrics(self, **extra):
        base = dict(yaw=0.0, pitch=0.0, roll=0.0, foreshortening=1.0,
                    mouth_width_px=170.0)
        base.update(extra)
        return base

    def test_photo_that_cuts_the_hair_is_called_out(self):
        warnings = prep.warnings_for(self.metrics(source_crown_y=0.0))
        self.assertTrue(any("cuts the top of the hair" in w for w in warnings))

    def test_thin_headroom_is_called_out(self):
        warnings = prep.warnings_for(self.metrics(crown_clearance=0.004))
        self.assertTrue(any("headroom" in w for w in warnings))

    def test_healthy_keyframe_is_silent(self):
        warnings = prep.warnings_for(
            self.metrics(source_crown_y=90.0, crown_clearance=0.06))
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
