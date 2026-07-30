"""HEIC portraits: iPhone photos must survive the upload pipeline.

OpenCV has no HEIC codec, so the decode goes through macOS sips and the
stored source of record becomes a PNG that every later stage can read.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from studio import prep

ROOT = Path(__file__).resolve().parents[1]
SIPS = "/usr/bin/sips"


def _make_heic(directory, name="portrait.heic"):
    png = os.path.join(directory, "seed.png")
    gradient = np.zeros((96, 128, 3), np.uint8)
    gradient[:, :, 0] = np.linspace(0, 255, 128, dtype=np.uint8)[None, :]
    gradient[:, :, 2] = np.linspace(255, 0, 96, dtype=np.uint8)[:, None]
    cv2.imwrite(png, gradient)
    heic = os.path.join(directory, name)
    subprocess.run(
        [SIPS, "-s", "format", "heic", png, "--out", heic],
        capture_output=True, check=True, timeout=60)
    return heic


@unittest.skipUnless(os.path.exists(SIPS), "macOS sips is required")
class HeicDecode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def test_read_image_bgr_decodes_heic(self):
        image = prep.read_image_bgr(_make_heic(self.dir))
        self.assertIsNotNone(image)
        self.assertEqual((96, 128, 3), image.shape)

    def test_read_image_bgr_decodes_heic_hiding_behind_jpg_name(self):
        # iPhone exports frequently keep a .jpg name on HEIC bytes; OpenCV
        # returns None on those, and the sips fallback must still decode.
        image = prep.read_image_bgr(_make_heic(self.dir, name="lying-name.jpg"))
        self.assertIsNotNone(image)
        self.assertEqual((96, 128, 3), image.shape)

    def test_read_image_bgr_still_reads_ordinary_images(self):
        png = os.path.join(self.dir, "plain.png")
        cv2.imwrite(png, np.full((10, 12, 3), 128, np.uint8))
        image = prep.read_image_bgr(png)
        self.assertEqual((10, 12, 3), image.shape)

    def test_unreadable_file_returns_none(self):
        garbage = os.path.join(self.dir, "garbage.heic")
        Path(garbage).write_bytes(b"not an image at all")
        self.assertIsNone(prep.read_image_bgr(garbage))

    def test_decode_heic_writes_a_png(self):
        out = os.path.join(self.dir, "decoded.png")
        prep.decode_heic(_make_heic(self.dir), out)
        decoded = cv2.imread(out, cv2.IMREAD_COLOR)
        self.assertEqual((96, 128, 3), decoded.shape)

    def test_decode_heic_raises_on_garbage(self):
        garbage = os.path.join(self.dir, "garbage.heic")
        Path(garbage).write_bytes(b"nope")
        with self.assertRaisesRegex(ValueError, "could not decode"):
            prep.decode_heic(garbage, os.path.join(self.dir, "out.png"))


class HeicPipelineContract(unittest.TestCase):
    def test_upload_endpoint_accepts_heic_and_heif(self):
        app = (ROOT / "server" / "app.py").read_text()
        self.assertIn('".heic"', app)
        self.assertIn('".heif"', app)

    def test_create_avatar_stores_heic_sources_as_png(self):
        # Everything downstream reads the stored source with OpenCV, so a
        # HEIC upload must be converted once at registration.
        build = (ROOT / "studio" / "build.py").read_text()
        self.assertIn("prep.HEIC_EXTENSIONS", build)
        marker = build.index("prep.HEIC_EXTENSIONS")
        window = build[marker:marker + 400]
        self.assertIn('"source.png"', window)
        self.assertIn("prep.decode_heic(image_path, src)", window)

    def test_keyframe_builder_uses_the_fallback_reader(self):
        source = (ROOT / "studio" / "prep.py").read_text()
        marker = source.index("def build_keyframe")
        window = source[marker:marker + 300]
        self.assertIn("read_image_bgr(src_path)", window)


if __name__ == "__main__":
    unittest.main()
