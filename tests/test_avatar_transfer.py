"""The .avtr interchange format: export, import, and its safety rails."""
import importlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

from studio import build

server_app = importlib.import_module("server.app")


def make_avatar(root, slug, name="Test Face", status="ready"):
    directory = Path(root) / slug
    (directory / "visemes").mkdir(parents=True)
    (directory / "raw").mkdir()
    (directory / "body").mkdir()
    (directory / "motion" / "raw").mkdir(parents=True)
    (directory / "library" / "motion" / "walk" / "set-1").mkdir(parents=True)
    (directory / "runtime").mkdir()
    (directory / ".motion-cache").mkdir()
    (directory / "manifest.json").write_text(json.dumps(
        {"slug": slug, "name": name, "status": status}))
    (directory / "keyframe.png").write_bytes(b"key")
    (directory / "visemes" / "v_closed.jpg").write_bytes(b"viseme")
    (directory / "raw" / "v_closed.png").write_bytes(b"raw-render")
    (directory / "body" / "body.json").write_text("{}")
    (directory / "body" / "body.png").write_bytes(b"plate")
    (directory / "motion" / "motion.json").write_text("{}")
    (directory / "motion" / "walk-0.png").write_bytes(b"sheet")
    (directory / "motion" / "raw" / "walk-source.mp4").write_bytes(b"take")
    (directory / "library" / "motion" / "walk" / "set-1" / "set.json").write_text("{}")
    (directory / "runtime" / "manifest.json").write_text("{}")
    (directory / ".motion-cache" / "sig").write_bytes(b"cache")
    return directory


class AvatarTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = build.AVATARS
        build.AVATARS = self.temp.name
        self.addCleanup(lambda: setattr(build, "AVATARS", self.original))

    def _export(self, slug):
        path = os.path.join(self.temp.name, f"{slug}.avtr")
        server_app._avatar_archive(slug, build.adir(slug), path)
        return path

    def test_round_trip_carries_everything_but_runtime_and_caches(self):
        make_avatar(self.temp.name, "alpha")
        archive_path = self._export("alpha")
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
        self.assertIn("avtr.json", names)
        self.assertIn("avatar/manifest.json", names)
        self.assertIn("avatar/visemes/v_closed.jpg", names)
        self.assertIn("avatar/raw/v_closed.png", names)
        self.assertIn("avatar/body/body.png", names)
        self.assertIn("avatar/motion/raw/walk-source.mp4", names)
        self.assertIn("avatar/library/motion/walk/set-1/set.json", names)
        self.assertFalse(any(n.startswith("avatar/runtime/") for n in names))
        self.assertFalse(any(".motion-cache" in n for n in names))

        result = server_app._import_avatar_archive(archive_path)
        # alpha exists, so the import lands beside it under a fresh slug.
        self.assertEqual(result["slug"], "alpha-2")
        self.assertEqual(result["status"], "ready")
        imported = Path(build.adir("alpha-2"))
        self.assertTrue((imported / "visemes" / "v_closed.jpg").is_file())
        self.assertTrue((imported / "library" / "motion" / "walk" /
                         "set-1" / "set.json").is_file())
        manifest = json.loads((imported / "manifest.json").read_text())
        self.assertEqual(manifest["slug"], "alpha-2")
        self.assertEqual(manifest["name"], "Test Face")

    def test_rejects_foreign_zip_and_newer_format(self):
        stray = os.path.join(self.temp.name, "stray.zip")
        with zipfile.ZipFile(stray, "w") as archive:
            archive.writestr("readme.txt", "hello")
        with self.assertRaises(ValueError):
            server_app._import_avatar_archive(stray)
        newer = os.path.join(self.temp.name, "newer.avtr")
        with zipfile.ZipFile(newer, "w") as archive:
            archive.writestr("avtr.json", json.dumps(
                {"format": server_app.AVTR_FORMAT, "version": 99}))
            archive.writestr("avatar/manifest.json", "{}")
        with self.assertRaises(ValueError):
            server_app._import_avatar_archive(newer)

    def test_rejects_zip_slip(self):
        evil = os.path.join(self.temp.name, "evil.avtr")
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("avtr.json", json.dumps(
                {"format": server_app.AVTR_FORMAT, "version": 1,
                 "slug": "evil"}))
            archive.writestr("avatar/manifest.json", "{}")
            archive.writestr("avatar/../../escape.txt", "boom")
        with self.assertRaises(ValueError):
            server_app._import_avatar_archive(evil)
        self.assertFalse(os.path.exists(
            os.path.join(self.temp.name, "..", "escape.txt")))

    def test_rejects_entries_outside_the_avatar_root(self):
        evil = os.path.join(self.temp.name, "outside.avtr")
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("avtr.json", json.dumps(
                {"format": server_app.AVTR_FORMAT, "version": 1}))
            archive.writestr("avatar/manifest.json", "{}")
            archive.writestr("elsewhere/file.txt", "boom")
        with self.assertRaises(ValueError):
            server_app._import_avatar_archive(evil)

    def test_draft_import_reports_draft_status(self):
        make_avatar(self.temp.name, "sketch", status="draft")
        result = server_app._import_avatar_archive(self._export("sketch"))
        self.assertEqual(result["status"], "draft")


class AvatarStoreTests(unittest.TestCase):
    """The in-app store: starter avatars pulled from GitHub releases."""

    def test_store_lists_both_starter_avatars_with_github_urls(self):
        import asyncio
        listing = asyncio.run(server_app.api_avatar_store())
        items = {item["id"]: item for item in listing["items"]}
        self.assertEqual(set(items), {"vvn", "vivieen"})
        for item in items.values():
            self.assertTrue(item["url"].startswith(
                "https://github.com/tivojn/vivieen-companion/releases/download/"))
            self.assertGreater(item["bytes"], 100 * 1024 * 1024)
            self.assertTrue(item["blurb"])
            # Cards show her before the download: face + full-body art.
            self.assertTrue(item["face"].endswith(".jpg"))
            self.assertTrue(item["body"].endswith(".png"))

    def test_store_art_rejects_unknown_id_and_kind(self):
        import asyncio
        from fastapi import HTTPException
        for bad in (("nobody", "face"), ("vvn", "keyframe")):
            with self.assertRaises(HTTPException):
                asyncio.run(server_app.api_avatar_store_art(*bad))

    def test_store_install_rejects_unknown_avatar(self):
        import asyncio
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(server_app.api_avatar_store_install(
                server_app.StoreInstall(id="nobody")))
        self.assertEqual(caught.exception.status_code, 404)

    def test_second_install_request_does_not_stack_downloads(self):
        with server_app._store_lock:
            server_app._store_jobs["vvn"] = {
                "phase": "downloading", "pct": 40, "error": "", "slug": ""}
        try:
            import asyncio
            result = asyncio.run(server_app.api_avatar_store_install(
                server_app.StoreInstall(id="vvn")))
            self.assertFalse(result["started"])
            self.assertEqual(result["job"]["pct"], 40)
        finally:
            with server_app._store_lock:
                server_app._store_jobs.pop("vvn", None)


if __name__ == "__main__":
    unittest.main()
