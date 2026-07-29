"""The generated-set library: archive, activate, delete, and reconcile."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from studio import library


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload, indent=1))


class SetLibraryFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.avatar = Path(self._tmp.name)
        self._make_body(b"side-plate-v1", b"front-plate-v1")
        self._make_motion("walk", b"walk-take-1", style_id="office",
                          style_label="Office walk")
        self._make_motion("idle", b"idle-take-1")

    def _make_body(self, side_bytes, front_bytes, style="photorealistic"):
        body = self.avatar / "body"
        _write(body / "source-side.png", side_bytes)
        _write(body / "source-front.png", front_bytes)
        _write(body / "body.png", front_bytes + b"-cut")
        _write(body / "body-front.png", front_bytes + b"-cut")
        _write(body / "body-side.png", side_bytes + b"-cut")
        _write(body / "body-back.png", b"back-cut")
        _write(body / "head-mask.png", b"mask")
        _write(body / "body.json", {
            "v": 3,
            "image": "body.png",
            "views": {"front": {"image": "body-front.png"},
                      "side": {"image": "body-side.png"},
                      "back": {"image": "body-back.png"}},
            "motion_reference": {"walk_source": "source-side.png",
                                 "idle_source": "source-front.png"},
            "options": {"style": style, "pose": "relaxed"},
        })

    def _make_motion(self, kind, take, style_id=None, style_label=None,
                     pose_id="back-heel", pose_label="High heel touch"):
        motion = self.avatar / "motion"
        _write(motion / f"{kind}-0.png", take + b"-sheet")
        _write(motion / f"{kind}-poster.png", take + b"-poster")
        _write(motion / f"{kind}-alpha.mov", take + b"-alpha")
        _write(motion / "raw" / f"{kind}-keyframe.png", take + b"-key")
        _write(motion / "raw" / f"{kind}-source.mp4", take + b"-src")
        metadata_path = motion / "motion.json"
        metadata = (json.loads(metadata_path.read_text())
                    if metadata_path.exists() else {
                        "v": 9, "signature": "sig",
                        "image_provider": {"name": "img"},
                        "video_provider": {"name": "vid"},
                        "identity_reference": {"file": "head.png"},
                    })
        source_name = ("source-side.png" if kind == "walk"
                       else "source-front.png")
        metadata[kind] = {
            "fps": 12, "frames": 3,
            "sheets": [{"image": f"{kind}-0.png", "first": 0, "count": 3}],
            "poster": f"{kind}-poster.png",
            "alpha_video": f"{kind}-alpha.mov",
        }
        if kind == "walk":
            metadata["walk_style"] = {
                "id": style_id or "office",
                "label": style_label or "Office walk",
                "description": "", "validation": "traversal"}
            metadata["walk_frame"] = {"id": "landscape"}
        else:
            metadata["idle_pose"] = {"id": pose_id, "label": pose_label,
                                     "validation": "back-heel"}
        references = metadata.get("body_references") or {}
        references[kind] = {
            "view": "side" if kind == "walk" else "front",
            "file": source_name,
            "sha256": library._sha256(
                str(self.avatar / "body" / source_name)),
            "use": "test",
        }
        metadata["body_references"] = references
        prompts = metadata.get("prompts") or {}
        prompts[f"{kind}_keyframe"] = f"{kind} keyframe prompt"
        prompts[f"{kind}_video"] = f"{kind} video prompt"
        metadata["prompts"] = prompts
        _write(metadata_path, metadata)


class MotionSetTests(SetLibraryFixture):
    def test_archive_lists_active_compatible_set(self):
        set_id = library.archive_motion(str(self.avatar), "walk")
        self.assertTrue(set_id)
        sets = library.list_motion_sets(str(self.avatar), "walk")
        self.assertEqual([record["id"] for record in sets], [set_id])
        self.assertTrue(sets[0]["active"])
        self.assertTrue(sets[0]["compatible"])
        self.assertEqual(sets[0]["label"], "Office walk")
        self.assertEqual(
            sets[0]["poster"],
            f"library/motion/walk/{set_id}/walk-poster.png")

    def test_archive_is_idempotent_per_content(self):
        first = library.archive_motion(str(self.avatar), "walk")
        second = library.archive_motion(str(self.avatar), "walk")
        self.assertEqual(first, second)
        self.assertEqual(
            len(library.list_motion_sets(str(self.avatar), "walk")), 1)

    def test_second_take_archives_as_new_active_set(self):
        first = library.archive_motion(str(self.avatar), "walk")
        self._make_motion("walk", b"walk-take-2", style_id="cartwheel",
                          style_label="Cartwheel")
        second = library.archive_motion(str(self.avatar), "walk")
        self.assertNotEqual(first, second)
        sets = {record["id"]: record
                for record in library.list_motion_sets(str(self.avatar), "walk")}
        self.assertEqual(len(sets), 2)
        self.assertTrue(sets[second]["active"])
        self.assertFalse(sets[first]["active"])

    def test_activate_restores_files_and_metadata(self):
        first = library.archive_motion(str(self.avatar), "walk")
        library.archive_motion(str(self.avatar), "idle")
        self._make_motion("walk", b"walk-take-2", style_id="cartwheel",
                          style_label="Cartwheel")
        library.archive_motion(str(self.avatar), "walk")
        metadata = library.activate_motion(str(self.avatar), "walk", first)
        self.assertEqual(metadata["walk_style"]["id"], "office")
        self.assertEqual(
            (self.avatar / "motion" / "walk-alpha.mov").read_bytes(),
            b"walk-take-1-alpha")
        self.assertEqual(
            (self.avatar / "motion" / "raw" / "walk-source.mp4").read_bytes(),
            b"walk-take-1-src")
        # Edge Idle survives the walk swap untouched.
        self.assertEqual(metadata["idle"]["poster"], "idle-poster.png")
        self.assertEqual(
            (self.avatar / "motion" / "idle-alpha.mov").read_bytes(),
            b"idle-take-1-alpha")
        on_disk = json.loads(
            (self.avatar / "motion" / "motion.json").read_text())
        self.assertEqual(on_disk["walk_style"]["id"], "office")

    def test_activate_rebuilds_bundle_after_canonical_removal(self):
        set_id = library.archive_motion(str(self.avatar), "walk")
        library.strip_canonical_motion(str(self.avatar), "walk")
        library.strip_canonical_motion(str(self.avatar), "idle")
        self.assertFalse((self.avatar / "motion").exists())
        metadata = library.activate_motion(str(self.avatar), "walk", set_id)
        self.assertIn("walk", metadata)
        self.assertEqual(metadata["signature"], "sig")
        self.assertTrue((self.avatar / "motion" / "walk-0.png").exists())

    def test_remove_reports_active_state(self):
        first = library.archive_motion(str(self.avatar), "walk")
        self._make_motion("walk", b"walk-take-2")
        second = library.archive_motion(str(self.avatar), "walk")
        self.assertFalse(
            library.remove_motion_set(str(self.avatar), "walk", first))
        self.assertTrue(
            library.remove_motion_set(str(self.avatar), "walk", second))
        self.assertEqual(
            library.list_motion_sets(str(self.avatar), "walk"), [])

    def test_strip_canonical_keeps_other_kind(self):
        metadata = library.strip_canonical_motion(str(self.avatar), "walk")
        self.assertNotIn("walk", metadata)
        self.assertIn("idle", metadata)
        self.assertFalse((self.avatar / "motion" / "walk-0.png").exists())
        self.assertTrue((self.avatar / "motion" / "idle-0.png").exists())
        self.assertIsNone(
            library.strip_canonical_motion(str(self.avatar), "idle"))
        self.assertFalse((self.avatar / "motion").exists())

    def test_rejects_bad_ids(self):
        with self.assertRaises(ValueError):
            library.activate_motion(str(self.avatar), "walk", "../escape")
        with self.assertRaises(ValueError):
            library.remove_motion_set(str(self.avatar), "walk", "no-such-set")


class BodySetTests(SetLibraryFixture):
    def test_archive_and_switch_bodies_reconciles_motion(self):
        first_body = library.archive_body(str(self.avatar))
        first_walk = library.archive_motion(str(self.avatar), "walk")
        library.archive_motion(str(self.avatar), "idle")

        # A regenerated body invalidates canonical motion (server behaviour),
        # and its own motion takes are archived against the new plates.
        self._make_body(b"side-plate-v2", b"front-plate-v2", style="anime")
        second_body = library.archive_body(str(self.avatar))
        self.assertNotEqual(first_body, second_body)
        self._make_motion("walk", b"walk-take-2", style_id="stroll",
                          style_label="Relaxed stroll")
        library.archive_motion(str(self.avatar), "walk")

        # Old walk is incompatible with the new body; new walk is compatible.
        sets = {record["id"]: record
                for record in library.list_motion_sets(str(self.avatar), "walk")}
        self.assertFalse(sets[first_walk]["compatible"])
        self.assertEqual(
            sum(1 for record in sets.values() if record["compatible"]), 1)

        # Switching back to the first body restores its archived walk.
        body_metadata = library.activate_body(str(self.avatar), first_body)
        self.assertEqual(body_metadata["options"]["style"], "photorealistic")
        metadata = library.reconcile_motion_with_body(str(self.avatar))
        self.assertEqual(metadata["walk_style"]["id"], "office")
        self.assertEqual(
            (self.avatar / "motion" / "walk-alpha.mov").read_bytes(),
            b"walk-take-1-alpha")
        sets = library.list_motion_sets(str(self.avatar), "walk")
        active = [record for record in sets if record["active"]]
        self.assertEqual([record["id"] for record in active], [first_walk])

    def test_reconcile_drops_motion_without_replacement(self):
        library.archive_body(str(self.avatar))
        library.archive_motion(str(self.avatar), "walk")
        library.archive_motion(str(self.avatar), "idle")
        self._make_body(b"side-plate-v2", b"front-plate-v2")
        library.archive_body(str(self.avatar))
        metadata = library.reconcile_motion_with_body(str(self.avatar))
        self.assertIsNone(metadata)
        self.assertFalse((self.avatar / "motion").exists())

    def test_remove_body_set_reports_active_state(self):
        first = library.archive_body(str(self.avatar))
        self._make_body(b"side-plate-v2", b"front-plate-v2")
        second = library.archive_body(str(self.avatar))
        self.assertFalse(library.remove_body_set(str(self.avatar), first))
        self.assertTrue(library.remove_body_set(str(self.avatar), second))
        self.assertEqual(library.list_body_sets(str(self.avatar)), [])

    def test_sync_canonical_adopts_existing_avatar(self):
        library.sync_canonical(str(self.avatar))
        self.assertEqual(len(library.list_body_sets(str(self.avatar))), 1)
        self.assertEqual(
            len(library.list_motion_sets(str(self.avatar), "walk")), 1)
        self.assertEqual(
            len(library.list_motion_sets(str(self.avatar), "idle")), 1)
        library.sync_canonical(str(self.avatar))
        self.assertEqual(
            len(library.list_motion_sets(str(self.avatar), "walk")), 1)


if __name__ == "__main__":
    unittest.main()
