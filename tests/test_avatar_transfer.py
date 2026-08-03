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


class StoreProgressTests(unittest.TestCase):
    def setUp(self):
        # Importing an archive WRITES an avatar. Without this the test
        # installed itself into the owner's real collection - two stray
        # "Probe" faces on the bench (2026-08-03). Every test that imports
        # must own its own root.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = build.AVATARS
        build.AVATARS = self.temp.name
        self.addCleanup(lambda: setattr(build, "AVATARS", self.original))

    def test_unpacking_reports_bytes_not_a_frozen_number(self):
        # A 300MB avatar takes real time to unpack, and a bar parked on
        # one number reads as a hang (owner, 2026-08-03). Progress is by
        # BYTES written, because the sprite sheets are thousands of times
        # larger than the json beside them - counting files would race to
        # 90% and then sit through the only part that takes any time.
        import json as _json, tempfile, zipfile
        work = tempfile.mkdtemp()
        path = os.path.join(work, "tiny.avtr")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("avtr.json", _json.dumps(
                {"format": server_app.AVTR_FORMAT, "version": 1,
                 "slug": "progressprobe"}))
            archive.writestr("avatar/manifest.json",
                             _json.dumps({"name": "Probe", "status": "draft"}))
            for index, size in enumerate((200_000, 800_000, 40_000, 1_500_000)):
                archive.writestr(f"avatar/sheet-{index}.bin", b"\0" * size)
        seen = []
        server_app._import_avatar_archive(
            path, on_progress=lambda written, total: seen.append((written, total)))
        self.assertGreater(len(seen), 1)
        self.assertTrue(all(seen[i][0] <= seen[i + 1][0]
                            for i in range(len(seen) - 1)), "must not go backwards")
        self.assertEqual(seen[-1][0], seen[-1][1], "must finish at the total")

    def test_the_bar_spends_its_length_where_the_time_goes(self):
        source = (Path(__file__).resolve().parents[1] / "server" / "app.py").read_text(encoding="utf-8")
        # The download is most of the wait, so it owns most of the bar.
        self.assertIn("pct=min(70, int(got * 70 / expect))", source)
        self.assertIn("pct=70 + int(written * 25 / total)", source)
        # Byte counts ride along, so a slow line still shows movement
        # between whole percents.
        self.assertIn("done_bytes=got, total_bytes=expect", source)
        card = (Path(__file__).resolve().parents[1] / "web" / "settings.html").read_text(encoding="utf-8")
        self.assertIn("storeBytes(job)", card)


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
