"""Hold-to-talk follows EnConvo's DICTATION default, not transcription.

EnConvo keeps two speech-to-text defaults: the transcription panel ("stt")
and Dictation. The avatar's hold-to-talk is dictation, and the Dictation
panel embeds its own per-route model override (e.g. Soniox "stt-rt-v5")
inside dictation.json - which must beat the route's standalone default.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import providers  # noqa: E402


class DictationDefault(unittest.TestCase):
    def test_stt_selection_reads_the_dictation_panel(self):
        selection_key, prefix = providers._GLOBAL_SELECTIONS["stt"]
        self.assertEqual("dictation", selection_key)
        self.assertEqual("transcribe|", prefix)

    def test_embedded_dictation_override_beats_route_default(self):
        with tempfile.TemporaryDirectory() as preferences:
            with open(os.path.join(preferences, "dictation.json"), "w") as f:
                json.dump({
                    "preferenceKey": "dictation",
                    "selected": "transcribe|soniox",
                    "transcribe|soniox": {"modelName": "stt-rt-v5"},
                }, f)
            # The route's standalone preference points at a DIFFERENT model;
            # the dictation panel's embedded override must win.
            with mock.patch.object(
                    providers, "ENCONVO_PREFERENCES", preferences), \
                 mock.patch.object(
                    providers, "_run_enconvo_json",
                    return_value={"modelName": "stt-async-v5"}):
                value = providers._global_one("stt")
        self.assertEqual("transcribe|soniox", value["command_key"])
        self.assertEqual("soniox", value["provider"])
        self.assertEqual("stt-rt-v5", value["model"])

    def test_settings_points_at_the_dictation_panel(self):
        settings = (ROOT / "web" / "settings.html").read_text()
        self.assertIn("'Dictation & Transcription → Dictation'", settings)


if __name__ == "__main__":
    unittest.main()
