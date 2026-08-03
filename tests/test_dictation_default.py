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


class LiveDictationBridge(unittest.TestCase):
    """The realtime path: renderer -> local WebSocket bridge -> Soniox.

    Verified live 2026-07-31: a spoken webm streamed through /stt/stream
    produced 17 incremental updates and the exact final transcript.
    """

    def test_server_bridge_contract(self):
        app = (ROOT / "server" / "app.py").read_text()
        self.assertIn('@app.websocket("/stt/stream")', app)
        self.assertIn(
            'SONIOX_RT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"',
            app)
        # Measured live: Soniox only finalises on an empty TEXT frame; the
        # documented empty-binary alternative just times out. The bridge
        # must translate the client's end signal accordingly.
        self.assertIn('await upstream.send("")', app)
        # The http middleware skips websocket scopes, so the token gate
        # must be inside the endpoint, and the key stays server-side.
        marker = app.index('@app.websocket("/stt/stream")')
        window = app[marker:marker + 600]
        # _client_token reads the Electron header or the iOS pairing cookie.
        self.assertIn("_client_token(client)", window)
        self.assertIn("compare_digest", window)
        self.assertIn('"credentials|soniox"', app)
        # CSP: 'self' does not cover ws:, the local socket needs listing.
        self.assertIn("ws://127.0.0.1:*", app)

    def test_renderer_streams_and_falls_back(self):
        renderer = (ROOT / "web" / "index.html").read_text()
        self.assertIn("'/stt/stream'", renderer)
        self.assertIn("function openDictationStream", renderer)
        self.assertIn("function finishDictationStream", renderer)
        # The WebM header lives in chunk zero: the stream must always be
        # flushed from the start, never begun mid-take.
        self.assertIn("function flushDictationStream", renderer)
        self.assertIn(
            "while(dictationSent<chunks.length)dictationSocket.send(chunks[dictationSent++]);",
            renderer)
        # A dead bridge falls back to batch interim transcription.
        self.assertIn("else interimTranscribe();", renderer)

    def test_websockets_dependency_is_pinned(self):
        for name in ("requirements-backend.txt", "requirements-electron.txt"):
            self.assertIn("websockets", (ROOT / name).read_text())


class SonioxDirectProvider(unittest.TestCase):
    """Soniox as Vivieen's own STT provider, no EnConvo required.

    Verified live 2026-07-31: key validation passes, a wrong key is
    rejected with Soniox's own message, and a spoken take transcribes
    exactly through providers.hear.
    """

    def test_soniox_sits_second_in_the_stt_catalog(self):
        stt = providers.PROVIDERS["stt"]
        self.assertEqual("enconvo", stt[0]["id"])
        self.assertEqual("soniox", stt[1]["id"])
        self.assertTrue(stt[1]["key"])

    def test_config_normalises_to_a_realtime_model(self):
        config = providers._soniox_config(
            {"api_key": "k", "model": "stt-async-v5", "language": "ko"})
        self.assertEqual("stt-rt-v5", config["model"])
        self.assertEqual(["ko"], config["language_hints"])
        self.assertNotIn(
            "language_hints",
            providers._soniox_config({"api_key": "k", "language": "auto"}))

    def test_validation_passes_auth_but_not_other_errors(self):
        async def run(error):
            with mock.patch.object(
                    providers, "_soniox_stream", side_effect=error):
                return await providers._soniox_validate({"api_key": "k"})
        import asyncio
        # "No audio received." means auth already succeeded on an empty take.
        self.assertTrue(asyncio.run(run(RuntimeError("No audio received."))))
        with self.assertRaisesRegex(RuntimeError, "Incorrect API key"):
            asyncio.run(run(RuntimeError("Incorrect API key provided.")))

    def test_hear_and_model_listing_route_through_the_socket(self):
        source = (ROOT / "server" / "providers.py").read_text()
        # Batch transcription streams the take through the same socket
        # protocol as live dictation.
        self.assertIn("_soniox_stream(_soniox_config(c), frames)", source)
        # Listing doubles as the credentials check, so it validates first.
        self.assertIn("await _soniox_validate(c)", source)

    def test_live_dictation_bridge_prefers_the_direct_provider(self):
        app = (ROOT / "server" / "app.py").read_text()
        marker = app.index("def _soniox_stream_config")
        window = app[marker:marker + 700]
        self.assertIn('own.get("provider") == "soniox"', window)
        self.assertIn("P._soniox_config(own)", window)


if __name__ == "__main__":
    unittest.main()
