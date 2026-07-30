"""Re-cutting approved footage instead of generating a new performance."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from studio import motion


def _avatar(directory, manifest=None, kinds=()):
    """Build a throwaway avatar tree with optional manifest and raw takes."""
    raw = os.path.join(directory, "motion", "raw")
    os.makedirs(raw, exist_ok=True)
    if manifest is not None:
        with open(os.path.join(directory, "motion", "motion.json"), "w",
                  encoding="utf-8") as handle:
            json.dump(manifest, handle)
    for kind in kinds:
        with open(os.path.join(raw, f"{kind}-source.mp4"), "wb") as handle:
            handle.write(b"\x00" * 4096)
    return directory


BORED = (
    "Hold one single unchanging bored waiting posture for the entire shot, "
    "shoulder blade slumped against the wall."
)


class RecordedSettingsTest(unittest.TestCase):
    def test_reads_style_and_pose_from_the_shipped_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory, manifest={
                "walk_style": {"id": "cartwheel", "label": "Cartwheel"},
                "idle_pose": {"id": "custom", "label": "Custom pose",
                              "validation": "edge", "prompt": BORED},
            })
            settings = motion.recorded_motion_settings(directory)
        self.assertEqual(settings["walk_style"], "cartwheel")
        self.assertEqual(settings["idle_pose"]["prompt"], BORED)

    def test_custom_pose_survives_a_round_trip_through_resolve(self):
        # The whole point of inheriting is that the rebuilt manifest keeps
        # advertising the direction the footage actually performed.
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory, manifest={
                "idle_pose": {"id": "custom", "label": "Custom pose",
                              "validation": "edge", "prompt": BORED},
            })
            settings = motion.recorded_motion_settings(directory)
        resolved = motion.resolve_idle_pose(settings["idle_pose"], "")
        self.assertEqual(resolved["id"], "custom")
        self.assertEqual(resolved["validation"], "free")
        self.assertEqual(resolved["prompt"], BORED)

    def test_missing_manifest_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory)
            self.assertEqual(motion.recorded_motion_settings(directory), {})

    def test_unreadable_manifest_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory)
            with open(os.path.join(directory, "motion", "motion.json"), "w",
                      encoding="utf-8") as handle:
                handle.write("{ not json")
            self.assertEqual(motion.recorded_motion_settings(directory), {})

    def test_partial_manifest_only_reports_what_it_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory, manifest={"walk_style": {"id": "office"}})
            settings = motion.recorded_motion_settings(directory)
        self.assertEqual(settings, {"walk_style": "office"})


class SeedApprovedSourcesTest(unittest.TestCase):
    def test_copies_approved_takes_into_the_video_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory, kinds=("walk", "idle"))
            cache = os.path.join(directory, ".motion-cache", "signature")
            with mock.patch.object(
                    motion, "_build_context", return_value={"cache": cache}):
                seeded = motion.seed_approved_sources(
                    directory, ("walk", "idle"))
            self.assertEqual(seeded, ["walk", "idle"])
            for kind in ("walk", "idle"):
                cached = os.path.join(cache, "videos", f"{kind}.mp4")
                self.assertTrue(os.path.isfile(cached))
                # The build only reuses a cached file when it clears 8 KiB.
                self.assertGreater(os.path.getsize(cached), 8192 // 4)

    def test_seeds_only_the_requested_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory, kinds=("walk", "idle"))
            cache = os.path.join(directory, ".motion-cache", "signature")
            with mock.patch.object(
                    motion, "_build_context", return_value={"cache": cache}):
                motion.seed_approved_sources(directory, ("idle",))
            videos = os.path.join(cache, "videos")
            self.assertTrue(os.path.isfile(os.path.join(videos, "idle.mp4")))
            self.assertFalse(os.path.isfile(os.path.join(videos, "walk.mp4")))

    def test_missing_take_names_the_file_it_wanted(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory, kinds=("idle",))
            cache = os.path.join(directory, ".motion-cache", "signature")
            with mock.patch.object(
                    motion, "_build_context", return_value={"cache": cache}):
                with self.assertRaises(RuntimeError) as error:
                    motion.seed_approved_sources(directory, ("walk",))
        self.assertIn("motion/raw/walk-source.mp4", str(error.exception))

    def test_approved_source_reports_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory, kinds=("idle",))
            self.assertIsNone(motion.approved_source(directory, "walk"))
            self.assertTrue(motion.approved_source(directory, "idle"))


class DirectionConflictTest(unittest.TestCase):
    """Approved footage may only be re-cut under the direction it performed."""

    def _avatar(self, directory):
        return _avatar(directory, manifest={
            "walk_style": {"id": "cartwheel", "label": "Cartwheel"},
            "idle_pose": {"id": "custom", "label": "Custom pose",
                          "validation": "edge", "prompt": BORED},
        }, kinds=("walk", "idle"))

    def test_recorded_direction_is_safe_to_recut(self):
        with tempfile.TemporaryDirectory() as directory:
            self._avatar(directory)
            settings = motion.recorded_motion_settings(directory)
            self.assertIsNone(motion.approved_direction_conflict(
                directory, ("walk", "idle"),
                walk_style=settings["walk_style"],
                idle_pose=settings["idle_pose"]))

    def test_different_walk_style_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            self._avatar(directory)
            conflict = motion.approved_direction_conflict(
                directory, ("walk",), walk_style="office")
        self.assertIsNotNone(conflict)
        self.assertIn("cartwheel", conflict)
        self.assertIn("office", conflict)

    def test_different_idle_pose_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            self._avatar(directory)
            conflict = motion.approved_direction_conflict(
                directory, ("idle",),
                idle_pose=motion.resolve_idle_pose("back-heel"))
        self.assertIsNotNone(conflict)
        self.assertIn("custom", conflict)
        self.assertIn("back-heel", conflict)

    def test_same_custom_id_with_a_rewritten_prompt_is_refused(self):
        # Both sides say "custom", so only the prompt reveals that the footage
        # performed a different direction.
        with tempfile.TemporaryDirectory() as directory:
            self._avatar(directory)
            rewritten = motion.resolve_idle_pose(
                "custom", "Stand upright and wave both arms overhead slowly.")
            conflict = motion.approved_direction_conflict(
                directory, ("idle",), idle_pose=rewritten)
        self.assertIsNotNone(conflict)

    def test_kind_outside_the_build_is_not_policed(self):
        with tempfile.TemporaryDirectory() as directory:
            self._avatar(directory)
            # Rebuilding only idle must not trip over the walk style.
            self.assertIsNone(motion.approved_direction_conflict(
                directory, ("idle",),
                idle_pose=motion.recorded_motion_settings(
                    directory)["idle_pose"]))

    def test_avatar_without_recorded_motion_is_unrestricted(self):
        with tempfile.TemporaryDirectory() as directory:
            _avatar(directory, kinds=("walk", "idle"))
            self.assertIsNone(motion.approved_direction_conflict(
                directory, ("walk", "idle"), walk_style="office"))


class DefaultDirectionTest(unittest.TestCase):
    """The CLI now passes None when a flag is absent, so None must default."""

    def test_absent_walk_style_still_resolves_to_the_default(self):
        self.assertEqual(
            motion.resolve_walk_style(None)["id"], motion.DEFAULT_WALK_STYLE)

    def test_absent_idle_pose_still_resolves_to_the_default(self):
        self.assertEqual(
            motion.resolve_idle_pose(None, "")["id"], motion.DEFAULT_IDLE_POSE)


if __name__ == "__main__":
    unittest.main()
