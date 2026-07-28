import hashlib
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from studio import anatomy, build, compose, measure, rig, visemes


class RigProfileTests(unittest.TestCase):
    def test_all_controls_span_zero_to_one_hundred_with_safe_bands(self):
        schema = rig.public_schema()["controls"]
        expected_safe = {
            "lips": (80.0, 100.0),
            "jaw": (25.0, 80.0),
            "cheeks": (0.0, 70.0),
            "nasolabial": (0.0, 70.0),
            "nose": (0.0, 12.0),
        }
        for name, safe_limits in expected_safe.items():
            self.assertEqual(
                (schema[name]["minimum"], schema[name]["maximum"]),
                (0.0, 100.0),
            )
            self.assertEqual(
                (schema[name]["safe_minimum"], schema[name]["safe_maximum"]),
                safe_limits,
            )

    def test_profile_is_normalized_and_dental_lock_is_mandatory(self):
        profile = rig.normalize({
            "lips": 88,
            "jaw": 41,
            "cheeks": 15,
            "nasolabial": 22,
            "nose": 4,
            "teeth_lock": True,
            "preset": "subtle",
        })
        self.assertEqual(profile["nose"], 4.0)
        self.assertEqual(profile["preset"], "subtle")
        with self.assertRaisesRegex(ValueError, "identity lock"):
            rig.normalize({"teeth_lock": False})
        with self.assertRaisesRegex(ValueError, "identity lock"):
            rig.normalize({"lower_teeth_lock": False})
        locks = rig.public_schema()["locks"]
        self.assertTrue(locks["upper_teeth"])
        self.assertTrue(locks["lower_teeth"])
        self.assertEqual(rig.VERSION, 3)

    def test_red_experimental_values_are_allowed_for_anatomy_qa(self):
        profile = rig.normalize({
            "lips": 0, "jaw": 100, "cheeks": 100,
            "nasolabial": 100, "nose": 100,
        })
        self.assertEqual(profile["lips"], 0.0)
        self.assertEqual(profile["nose"], 100.0)

    def test_values_outside_zero_to_one_hundred_are_rejected(self):
        for name in rig.CONTROLS:
            for value in (-1, 101):
                with self.subTest(name=name, value=value), \
                     self.assertRaises(ValueError):
                    rig.normalize({name: value})

    def test_single_canonical_dental_pose_is_valid(self):
        self.assertEqual(
            anatomy._comparison_metrics([]),
            ((None, 0), (None, 1.0), (None, 0.0)),
        )

    def test_dental_rows_partition_at_the_lip_midline(self):
        cavity = np.full((10, 12), 255, np.uint8)
        landmarks = np.zeros((478, 2), np.float32)
        landmarks[13, 1] = 4
        landmarks[14, 1] = 6
        upper = compose._row_zone(cavity, landmarks, "upper") > 0
        lower = compose._row_zone(cavity, landmarks, "lower") > 0
        self.assertEqual(int(np.count_nonzero(upper)), 72)
        self.assertEqual(int(np.count_nonzero(lower)), 48)
        self.assertFalse(np.any(upper & lower))
        self.assertTrue(np.all(upper | lower))

    def test_degenerate_lower_row_transform_follows_the_jaw_translation(self):
        donor = np.zeros((478, 2), np.float32)
        target = donor.copy()
        target[compose.LOWER_MOUTH_ANCHORS] = [4.5, 8.0]
        transform = compose._lower_row_transform(donor, target)
        np.testing.assert_allclose(
            transform,
            np.array([[1.0, 0.0, 4.5], [0.0, 1.0, 8.0]], np.float32),
        )

    def test_connected_tooth_surface_extension_is_canonical(self):
        actual = np.zeros((8, 8), dtype=bool)
        actual[1:4, 1:4] = True
        anchor = np.zeros_like(actual)
        anchor[1:3, 1:3] = True
        self.assertEqual(anatomy._disconnected_fraction(actual, anchor), 0.0)

    def test_disconnected_tooth_component_is_measured(self):
        actual = np.zeros((8, 8), dtype=bool)
        actual[1:3, 1:3] = True
        actual[5:7, 5:7] = True
        anchor = np.zeros_like(actual)
        anchor[1:3, 1:3] = True
        self.assertEqual(anatomy._disconnected_fraction(actual, anchor), 0.5)
        self.assertEqual(
            anatomy._disconnected_fraction(
                actual, anchor, ignore_components_at_most=4),
            0.0,
        )

    def test_speech_articulation_excludes_eyelid_source_frame(self):
        self.assertEqual(len(visemes.SPEECH_ORDER), 15)
        self.assertNotIn("blink", visemes.SPEECH_ORDER)

    def test_articulation_failure_reports_measured_width(self):
        message = build._articulation_failure({
            "name": "oo", "ratio": 0.004, "max_ratio": 0.06,
            "width_ratio": 0.96, "want_width": 0.82,
        })
        self.assertEqual(
            message,
            "oo width 0.96x neutral is outside 0.70-0.94 (target 0.82)",
        )

    def test_aperture_tolerance_only_covers_landmark_jitter(self):
        self.assertTrue(measure._aperture_within_limit(0.091, 0.09))
        self.assertFalse(measure._aperture_within_limit(0.093, 0.09))


class PublishTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_avatars = build.AVATARS
        build.AVATARS = self.temp.name
        self.slug = "test-avatar"
        self.directory = build.adir(self.slug)
        os.makedirs(self.directory)
        self._write_artifacts(self.directory, b"live")
        build.write_manifest(self.slug, {"status": "ready", "version": 1})

    def tearDown(self):
        build.AVATARS = self.original_avatars
        self.temp.cleanup()

    @staticmethod
    def _write_artifacts(root, content):
        for artifact in build.RIG_ARTIFACTS:
            path = os.path.join(root, artifact)
            if "." in artifact:
                with open(path, "wb") as handle:
                    handle.write(content + artifact.encode())
            else:
                os.makedirs(path)
                with open(os.path.join(path, "payload.bin"), "wb") as handle:
                    handle.write(content + artifact.encode())

    @staticmethod
    def _digest(path):
        sha = hashlib.sha256()
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                sha.update(handle.read())
        else:
            for root, directories, files in os.walk(path):
                directories.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    sha.update(os.path.relpath(full, path).encode())
                    with open(full, "rb") as handle:
                        sha.update(handle.read())
        return sha.hexdigest()

    def _state(self):
        names = list(build.RIG_ARTIFACTS) + ["manifest.json"]
        return {
            name: self._digest(os.path.join(self.directory, name))
            for name in names
        }

    def test_mid_publish_failure_restores_exact_live_bytes(self):
        stage = tempfile.mkdtemp(prefix=".rig-stage-", dir=self.directory)
        self._write_artifacts(stage, b"stage")
        baseline = self._state()
        real_replace = os.replace
        staged_moves = 0

        def fail_third_staged_move(source, target):
            nonlocal staged_moves
            if stage in source:
                staged_moves += 1
                if staged_moves == 3:
                    raise OSError("injected publish failure")
            return real_replace(source, target)

        with mock.patch.object(
                build.os, "replace", side_effect=fail_third_staged_move):
            with self.assertRaisesRegex(OSError, "injected publish failure"):
                build._publish_stage(
                    self.slug, stage,
                    {"status": "ready", "version": 2},
                )
        self.assertEqual(self._state(), baseline)
        leftovers = [
            name for name in os.listdir(self.directory)
            if name.startswith(".rig-live-")
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
