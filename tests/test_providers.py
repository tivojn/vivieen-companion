import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import providers as P


class ProviderDefaultsTests(unittest.TestCase):
    def setUp(self):
        P._cache["globals"] = None
        P._cache["globals_at"] = 0.0
        P._routes.clear()

    def test_fresh_install_uses_enconvo_for_every_modality(self):
        self.assertEqual(
            {kind: P.DEFAULTS[kind]["provider"] for kind in ("llm", "tts", "stt")},
            {"llm": "enconvo", "tts": "enconvo", "stt": "enconvo"},
        )

    def test_global_metadata_is_whitelisted_and_never_returns_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            with open(os.path.join(directory, "llm.json"), "w") as handle:
                json.dump({"selected": "llm|open_ai"}, handle)

            calls = []

            def fake_cli(args, timeout=60):
                calls.append(args)
                return {
                    "modelName": "gpt-5.6-sol",
                    "temperature": 1,
                    "api_key": "must-not-escape",
                }

            with patch.object(P, "ENCONVO_PREFERENCES", directory), \
                 patch.object(P, "_run_enconvo_json", side_effect=fake_cli):
                mapped = P._global_one("llm")

        self.assertEqual(mapped["provider"], "open_ai")
        self.assertEqual(mapped["model"], "gpt-5.6-sol")
        self.assertNotIn("api_key", mapped)
        self.assertEqual(calls[0][:4], ["config", "get", "llm|open_ai", "--includes"])
        self.assertNotIn("api_key", calls[0])

    def test_global_chat_uses_scalar_model_override_and_validates_route(self):
        captured = []
        model = "qwen3.5-35b-uncensored:latest"

        async def fake_api(path, request, timeout=180):
            captured.append((path, request))
            return {
                "text": "mapped",
                "message": {"additional": {"metadata": {"llmUsage": {
                    "provider": "ollama", "model": model,
                }}}},
            }

        mapped = {
            "provider": "ollama",
            "model": model,
            "temperature": 1,
            "command_key": "llm|ollama",
            "display": f"Ollama · {model}",
        }
        with patch.object(P, "global_default", return_value=mapped) as global_mock, \
             patch.object(P, "_run_enconvo_api_async", side_effect=fake_api):
            result = asyncio.run(P._enconvo_chat(
                [{"role": "user", "content": "test"}], {}, "Be brief."))

        self.assertEqual(result, "mapped")
        global_mock.assert_called_once_with("llm", True)
        self.assertEqual(captured[0][0], "llm/features/ollama/chat")
        self.assertEqual(captured[0][1]["modelName"], model)
        self.assertNotIn("model", captured[0][1])
        self.assertNotIn("credentials", captured[0][1])
        self.assertEqual(captured[0][1]["modelParams"]["maxOutputTokens"], 160)
        self.assertEqual(
            captured[0][1]["modelName_preferences"][model]["reasoning_effort"],
            "disabled",
        )
        self.assertEqual(captured[0][1]["system"], "Be brief.")
        self.assertEqual(P.last_route("llm")["state"], "success")
        self.assertEqual(P.last_route("llm")["provider"], "ollama")
        self.assertEqual(P.last_route("llm")["actual_model"], model)

    def test_global_chat_rejects_a_model_mismatch(self):
        mapped = {
            "provider": "ollama",
            "model": "qwen3.5-35b-uncensored:latest",
            "display": "Ollama · qwen3.5-35b-uncensored:latest",
        }
        payload = {
            "text": "wrong route",
            "message": {"additional": {"metadata": {"llmUsage": {
                "provider": "ollama", "model": "glm-5.1:cloud",
            }}}},
        }
        with patch.object(P, "global_default", return_value=mapped), \
             patch.object(P, "_run_enconvo_api_async", new=AsyncMock(return_value=payload)):
            with self.assertRaisesRegex(RuntimeError, "glm-5.1:cloud"):
                asyncio.run(P._enconvo_chat(
                    [{"role": "user", "content": "test"}], {}, ""))

        route = P.last_route("llm")
        self.assertEqual(route["state"], "failed")
        self.assertEqual(route["actual_model"], "glm-5.1:cloud")
        self.assertIn("instead of", route["error"])

    def test_global_chat_rejects_missing_execution_evidence(self):
        mapped = {
            "provider": "ollama",
            "model": "qwen3.5-35b-uncensored:latest",
            "display": "Ollama · qwen3.5-35b-uncensored:latest",
        }
        with patch.object(P, "global_default", return_value=mapped), \
             patch.object(P, "_run_enconvo_api_async",
                          new=AsyncMock(return_value={"text": "unverified"})):
            with self.assertRaisesRegex(RuntimeError, "did not report"):
                asyncio.run(P._enconvo_chat(
                    [{"role": "user", "content": "test"}], {}, ""))

        route = P.last_route("llm")
        self.assertEqual(route["state"], "failed")
        self.assertNotIn("actual_model", route)

    def test_failed_global_chat_records_the_attempted_model(self):
        mapped = {
            "provider": "ollama",
            "model": "qwen3.5-35b-uncensored:latest",
            "display": "Ollama · qwen3.5-35b-uncensored:latest",
        }

        async def failed_api(path, request, timeout=180):
            raise RuntimeError("offline")

        with patch.object(P, "global_default", return_value=mapped), \
             patch.object(P, "_run_enconvo_api_async", side_effect=failed_api):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                asyncio.run(P._enconvo_chat(
                    [{"role": "user", "content": "test"}], {}, ""))

        self.assertEqual(P.last_route("llm")["state"], "failed")
        self.assertEqual(P.last_route("llm")["model"], "qwen3.5-35b-uncensored:latest")

    def test_direct_key_provider_does_not_list_models_before_key(self):
        with self.assertRaisesRegex(RuntimeError, "API key is required"):
            asyncio.run(P.list_models("llm", {"provider": "openai", "api_key": ""}))

    def test_stored_key_is_never_reused_after_provider_change(self):
        import app as A

        stored = {"llm": {"provider": "openai", "api_key": "example-stored-value"}}
        with patch.object(A.P, "load", return_value=stored):
            same = A._with_key("llm", {"provider": "openai"})
            changed = A._with_key("llm", {"provider": "anthropic"})
        self.assertEqual(same["api_key"], "example-stored-value")
        self.assertNotIn("api_key", changed)

    def test_chat_health_gate_is_provider_agnostic(self):
        index_path = os.path.join(ROOT, "web", "index.html")
        with open(index_path, encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("h.llm_ok??h.provider_ok??h.ollama", html)
        self.assertIn("routedStatus(h.last_llm,h.llm)", html)
        self.assertNotIn("classList.toggle('alert',!h.ollama)", html)

    def test_openai_lists_only_models_for_the_requested_modality(self):
        # Exclusion only, never a name allowlist: the old gpt-* prefix list
        # hid every newly-named family (a "luna-1" never appeared). Unknown
        # names survive; clearly-non-chat models are dropped.
        rows = [
            "gpt-5.6-sol", "luna-1", "gpt-4o-realtime-preview", "gpt-4o-mini-tts",
            "text-embedding-3-large", "whisper-1", "gpt-4o-mini-transcribe",
            "dall-e-3", "sora-2", "davinci-002",
        ]
        self.assertEqual(P._filter_models("llm", "openai", rows),
                         ["gpt-5.6-sol", "luna-1"])
        self.assertEqual(
            P._filter_models("stt", "openai", rows),
            ["gpt-4o-mini-transcribe", "whisper-1"],
        )
        self.assertEqual(
            P._filter_models("tts", "openai", rows),
            ["gpt-4o-mini-tts"],
        )
        self.assertEqual(
            P._filter_models("llm", "groq", ["llama-4-scout", "whisper-large-v3"]),
            ["llama-4-scout"],
        )
        self.assertEqual(
            P._filter_models("stt", "groq", ["llama-4-scout", "whisper-large-v3"]),
            ["whisper-large-v3"],
        )

    def test_failure_hint_names_the_reason_the_model_went_quiet(self):
        # The exact strings httpx produced live 2026-07-31 when Ollama Cloud
        # ran out of credit mid-session: Test had passed, chats then failed,
        # and the bubble's bare "not answering" hid the 402 for hours.
        gone = ("Client error '410 Gone' for url 'http://localhost:11434/api/chat' "
                "For more information check: https://developer.mozilla.org/...")
        paying = "Client error '402 Payment Required' for url 'http://localhost:11434/api/chat'"
        self.assertEqual(P.failure_hint(paying),
                         "the provider wants payment or sign-in for this model")
        self.assertEqual(P.failure_hint(gone),
                         "the provider no longer serves this model")
        self.assertEqual(P.failure_hint("Client error '401 Unauthorized' for url 'x'"),
                         "the provider rejected the API key")
        self.assertEqual(P.failure_hint("ReadTimeout: timed out"),
                         "the request timed out")
        self.assertEqual(P.failure_hint("All connection attempts failed: connect ECONNREFUSED"),
                         "the endpoint is unreachable")
        self.assertEqual(P.failure_hint("something novel"), "")
        # And the reply path speaks the hint into the bubble.
        app_source = open(os.path.join(ROOT, "server", "app.py")).read()
        self.assertIn("P.failure_hint(e)", app_source)
        self.assertIn("My model is not answering — {hint}.", app_source)

    def test_key_validation_failures_read_plainly_and_lists_are_refreshable(self):
        # A rejected key must say so - the raw httpx text buries the status
        # behind a docs URL and the tail-keeping redactor kept only the URL.
        source = open(os.path.join(ROOT, "server", "providers.py")).read()
        self.assertIn("except httpx.HTTPStatusError", source)
        self.assertIn("the provider rejected this API key", source)
        # Once validated, the load button becomes an explicit list refresh.
        settings = open(os.path.join(ROOT, "web", "settings.html")).read()
        self.assertIn("'↻ Refresh models'", settings)

    def test_direct_provider_records_actual_route(self):
        config = {"provider": "ollama", "model": "qwen-test:latest"}
        with patch.object(P, "_chat_direct", new=AsyncMock(return_value="direct")):
            result = asyncio.run(P.chat([{"role": "user", "content": "hello"}], config))
        self.assertEqual(result, "direct")
        self.assertEqual(P.last_route("llm")["state"], "success")
        self.assertEqual(P.last_route("llm")["provider"], "ollama")
        self.assertEqual(P.last_route("llm")["model"], "qwen-test:latest")

    def test_direct_provider_failure_records_attempt(self):
        config = {"provider": "ollama", "model": "offline-test:latest"}
        failing = AsyncMock(side_effect=RuntimeError("offline"))
        with patch.object(P, "_chat_direct", new=failing):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                asyncio.run(P.chat([{"role": "user", "content": "hello"}], config))
        self.assertEqual(P.last_route("llm")["state"], "failed")
        self.assertEqual(P.last_route("llm")["model"], "offline-test:latest")

    def test_global_tts_uses_enconvo_audio_contract(self):
        captured = []

        async def fake_cli(args, timeout=60):
            captured.append(args)
            output_dir = args[args.index("--output_dir") + 1]
            path = os.path.join(output_dir, "speech.wav")
            sf.write(path, np.zeros(2400, dtype=np.float32), P.SR)
            return {"path": path}

        mapped = {"provider": "mlx_kokoro", "model": "Kokoro-82M", "voice": "af_aoede"}
        with patch.object(P, "global_default", return_value=mapped) as global_mock, \
             patch.object(P, "_run_enconvo_async", side_effect=fake_cli), \
             patch.object(P, "_ff", return_value=np.zeros(2400, dtype=np.float32)):
            samples, alignment = asyncio.run(P._enconvo_speak("hello", {}))

        global_mock.assert_called_once_with("tts", True)
        self.assertGreater(len(samples), 0)
        self.assertIsNone(alignment)
        self.assertEqual(captured[0][:2], ["tts", "tts"])
        self.assertEqual(captured[0][captured[0].index("--speed") + 1], "1")

    def test_global_transcription_uses_temp_file_and_returns_content(self):
        observed = {}

        async def fake_cli(args, timeout=60):
            path = args[args.index("--filePaths") + 1]
            observed["exists_during_call"] = os.path.isfile(path)
            observed["args"] = args
            return {"content": "heard"}

        mapped = {"provider": "groq", "model": "whisper-large-v3", "language": "auto"}
        with patch.object(P, "global_default", return_value=mapped) as global_mock, \
             patch.object(P, "_run_enconvo_async", side_effect=fake_cli):
            result = asyncio.run(P._enconvo_hear(b"audio", "voice.wav", {}))

        global_mock.assert_called_once_with("stt", True)
        self.assertEqual(result, "heard")
        self.assertTrue(observed["exists_during_call"])
        self.assertIn("whisper-large-v3", observed["args"])
        self.assertNotIn("--credentials", observed["args"])

    def test_errors_redact_bearer_tokens_and_api_keys(self):
        text = P._safe_error(
            "Authorization: Bearer example-sensitive-value API key=example-private-value")
        self.assertNotIn("sensitive-value", text)
        self.assertNotIn("private-value", text)
        self.assertIn("[redacted]", text)


class OneClickPipelineTests(unittest.TestCase):
    def test_pipeline_chains_face_body_and_motion_in_one_job(self):
        # Owner request 2026-08-01: one button on the avatar card runs
        # face -> full body -> walk + edge idle + moves as a single
        # sequential background job. The standalone body/motion jobs and
        # the pipeline share the SAME stage functions (extracted, not
        # duplicated), so their behavior - including the motion rollback -
        # cannot drift apart.
        source = open(os.path.join(ROOT, "server", "app.py"),
                      encoding="utf-8").read()
        self.assertIn('@app.post("/api/avatar/pipeline")', source)
        self.assertIn("def _pipeline_thread", source)
        self.assertIn("def _body_stage", source)
        self.assertIn("def _motion_stage", source)
        # Takes run ONE AT A TIME with backoff retries: all three at once
        # burst past xAI's 2-rps team limit (eve, 2026-08-01) and a single
        # provider hiccup killed the whole stage. A re-click resumes -
        # finished body and takes are skipped, not regenerated.
        self.assertIn('for kind in ("walk", "idle", "move"):', source)
        self.assertIn("_motion_stage(\n                        slug, (kind,)",
                      source)
        self.assertIn("pause = 25 * (attempt + 1)", source)
        self.assertIn('writer(f"{kind} take already built - skipping")', source)
        self.assertIn("full body already built - skipping", source)
        self.assertIn("Ready with gaps", source)
        self.assertIn("_body_stage(slug, options, w,", source)
        # Owner defaults: office walk, high-heel-touch edge idle, K-pop
        # point dance - the pipeline builds these unprompted.
        from studio import motion
        self.assertEqual(motion.DEFAULT_WALK_STYLE, "office")
        self.assertEqual(motion.DEFAULT_IDLE_POSE, "back-heel")
        self.assertEqual(motion.DEFAULT_MOVE_STYLE, "kpop")
        self.assertEqual(
            motion.IDLE_POSE_PRESETS["back-heel"]["label"], "High heel touch")
        self.assertEqual(
            motion.MOVE_STYLES["kpop"]["label"], "K-pop point dance")
        self.assertIn("_body_stage(slug, BodyProfileInput().model_dump(), writer,",
                      source)
        # a still-running job refuses a second start instead of stacking
        self.assertIn('_reserve_job(request.slug, "pipeline"', source)
        settings = open(os.path.join(ROOT, "web", "settings.html"),
                        encoding="utf-8").read()
        self.assertIn('data-act="pipeline"', settings)
        self.assertIn("One-click everything", settings)
        self.assertIn("markAvatarBusy(slug, 'pipeline')", settings)


class LiveVoiceTests(unittest.TestCase):
    """Live talk: realtime speech-to-speech through a server-side bridge.
    Keys never reach the renderer; providers speak their own protocols
    upstream and the renderer gets one unified event stream."""

    @classmethod
    def setUpClass(cls):
        import app as application
        import providers
        cls.app_module = application
        cls.providers = providers

    def test_live_defaults_and_key_redaction(self):
        live = self.providers.DEFAULTS["live"]
        self.assertEqual(live["provider"], "xai")
        self.assertEqual(live["xai_model"], "grok-voice-think-fast-1.0")
        masked = self.providers.redacted(
            {"live": {"xai_api_key": "secret", "eleven_api_key": ""}})
        self.assertEqual(masked["live"]["xai_api_key"], "")
        self.assertTrue(masked["live"]["has_xai_api_key"])
        self.assertFalse(masked["live"]["has_eleven_api_key"])

    def test_xai_events_translate_to_the_unified_stream(self):
        translate = self.app_module._xai_event
        self.assertEqual(
            list(translate({"type": "response.output_audio.delta",
                            "delta": "QUJD"})),
            [{"type": "audio", "data": "QUJD", "rate": 24000}])
        self.assertEqual(
            list(translate({"type": "response.output_audio_transcript.delta",
                            "delta": "Hel"})),
            [{"type": "agent_text", "text": "Hel", "final": False}])
        self.assertEqual(
            list(translate({"type": "input_audio_buffer.speech_started"})),
            [{"type": "interrupt"}])
        self.assertEqual(list(translate({"type": "session.updated"})), [])

    def test_eleven_events_translate_to_the_unified_stream(self):
        translate = self.app_module._eleven_event
        self.assertEqual(
            list(translate({"type": "audio",
                            "audio_event": {"audio_base_64": "QUJD"}})),
            [{"type": "audio", "data": "QUJD", "rate": 16000}])
        self.assertEqual(
            list(translate({"type": "user_transcript",
                            "user_transcription_event":
                                {"user_transcript": "hi"}})),
            [{"type": "user_text", "text": "hi"}])
        self.assertEqual(
            list(translate({"type": "interruption"})),
            [{"type": "interrupt"}])

    def test_bridge_enforces_token_silence_hangup_and_key_custody(self):
        source = open(os.path.join(ROOT, "server", "app.py"),
                      encoding="utf-8").read()
        self.assertIn('@app.websocket("/live/voice")', source)
        self.assertIn("LIVE_SILENCE_HANGUP_S = 120", source)
        self.assertIn('{"type": "closed", "reason": "silence"}', source)
        self.assertIn('"type": "pong"', source)   # ElevenLabs keep-alive
        self.assertIn("def _ensure_eleven_agent", source)
        # blank keys from the UI mean "unchanged"; __clear__ erases and
        # also forgets the auto-created agent
        self.assertIn('live.pop("has_" + field, None)', source)
        self.assertIn('live["eleven_agent_id"] = ""', source)


class PublicReleaseSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as application
        cls.app_module = application

    def request(self, method, path, **kwargs):
        async def run():
            transport = httpx.ASGITransport(app=self.app_module.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(run())

    def test_backend_authentication_rejects_missing_token(self):
        with patch.object(self.app_module, "AUTH_TOKEN", "test-session-token"):
            rejected = self.request("GET", "/api/meta")
            accepted = self.request(
                "GET", "/api/meta", headers={"X-Vivieen-Token": "test-session-token"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(rejected.headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", rejected.headers["content-security-policy"])
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["app_id"], "com.vivieen.companion")

    def test_cross_origin_mutation_is_rejected(self):
        with patch.object(self.app_module, "AUTH_TOKEN", ""):
            response = self.request(
                "POST", "/api/avatar/delete", json={"slug": "anything"},
                headers={"Origin": "https://malicious.example"})
        self.assertEqual(response.status_code, 403)

    def test_file_containment_rejects_sibling_prefixes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, "avatars")
            sibling = os.path.join(directory, "avatars-private")
            os.makedirs(root)
            os.makedirs(sibling)
            private = os.path.join(sibling, "portrait.png")
            with open(private, "wb") as handle:
                handle.write(b"private")
            self.assertIsNone(
                self.app_module._safe_file(root, "../avatars-private/portrait.png"))

    def test_invalid_avatar_slug_is_rejected_before_file_access(self):
        with patch.object(self.app_module, "AUTH_TOKEN", ""):
            response = self.request(
                "POST", "/api/avatar/delete", json={"slug": "../../outside"})
        self.assertEqual(response.status_code, 422)

    def test_rig_request_accepts_red_experimental_percentages(self):
        profile = self.app_module.RigProfileInput(
            lips=0, jaw=100, cheeks=100, nasolabial=99, nose=100)
        self.assertEqual(profile.nasolabial, 99)
        self.assertEqual(profile.nose, 100)
        # Sliders run to 150 now (owner calibration 2026-08-01); past the
        # span is still rejected.
        self.assertEqual(
            self.app_module.RigProfileInput(nasolabial=150).nasolabial, 150)
        with self.assertRaises(ValueError):
            self.app_module.RigProfileInput(nasolabial=151)

    def test_audio_upload_limit_is_enforced(self):
        with patch.object(self.app_module, "AUTH_TOKEN", ""), \
             patch.object(self.app_module, "MAX_AUDIO_BYTES", 8):
            response = self.request(
                "POST", "/stt",
                files={"audio": ("speech.webm", b"x" * 9, "audio/webm")})
        self.assertEqual(response.status_code, 413)

    def test_portrait_upload_limit_is_enforced_before_processing(self):
        with patch.object(self.app_module, "AUTH_TOKEN", ""), \
             patch.object(self.app_module, "MAX_UPLOAD_BYTES", 16):
            response = self.request(
                "POST", "/api/avatar/upload",
                files={"photo": ("portrait.png", b"x" * 17, "image/png")},
                data={"name": "Test"})
        self.assertEqual(response.status_code, 413)

    def test_first_run_has_no_bundled_avatar_dependency(self):
        with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(ROOT, "package.json"), encoding="utf-8") as handle:
            package = json.load(handle)
        self.assertIn("Create your first avatar", html)
        resources = json.dumps(package["build"]["extraResources"])
        self.assertNotIn('"from": "avatars"', resources)
        self.assertNotIn('"from": "active.json"', resources)
        self.assertNotIn("default-data", resources)
        self.assertNotIn(".electron-models", resources)
        self.assertIn(".electron-ffmpeg", resources)

    def test_electron_never_reuses_an_existing_backend(self):
        with open(os.path.join(ROOT, "electron", "main.cjs"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("randomBytes(32)", source)
        self.assertIn("port = await freePort()", source)
        self.assertNotIn("return 'reuse'", source)
        self.assertNotIn("syncBundledAvatars", source)

    def test_browser_escapes_avatar_metadata(self):
        with open(os.path.join(ROOT, "web", "settings.html"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("${esc(a.name || a.slug)}", source)
        self.assertIn("${esc(a.error)}", source)

    def test_avatar_caption_folds_to_one_line_and_expands_safely(self):
        with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("--caption-lines:1", source)
        self.assertIn("#her.is-expanded", source)
        self.assertIn("CAPTION_FOLD_DELAY=2000", source)
        self.assertIn("CAPTION_ROW_HOT_ZONE=18", source)
        self.assertIn("elHer.addEventListener('pointerenter'", source)
        self.assertIn("overflow-y:auto", source)
        self.assertIn('role="log"', source)
        self.assertIn("renderMarkdown", source)
        self.assertIn("document.createElement('strong')", source)
        self.assertIn("elHer.replaceChildren()", source)
        self.assertNotIn("elHer.innerHTML", source)
        self.assertIn("elHer.scrollTop=0", source)

    def test_avatar_progress_updates_without_rerendering_the_grid(self):
        with open(os.path.join(ROOT, "web", "settings.html"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("setInterval(loadAvatars", source)
        self.assertIn("pollBusyAvatars", source)
        self.assertIn("data-busy=", source)
        self.assertIn("class=\"jobbox", source)
        self.assertIn("full control ${spec.minimum}–${spec.maximum}%", source)
        self.assertIn("--safe-start:", source)
        self.assertIn("red experimental", source)
        self.assertIn("formatRigError", source)

    def test_runtime_source_uses_no_predictable_tempfile_api(self):
        for relative in ("server/app.py", "server/providers.py"):
            with open(os.path.join(ROOT, relative), encoding="utf-8") as handle:
                self.assertNotIn("tempfile.mktemp", handle.read(), relative)

    def test_face_model_is_downloaded_with_a_pinned_checksum(self):
        with open(os.path.join(ROOT, "studio", "face.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("https://storage.googleapis.com/mediapipe-models/face_landmarker/", source)
        self.assertIn("64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff", source)
        self.assertIn("digest.hexdigest() != MODEL_SHA256", source)

    def test_ffmpeg_build_is_lgpl_only_and_reproducible(self):
        with open(os.path.join(ROOT, "scripts", "stage-electron-ffmpeg.sh"),
                  encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("--disable-everything", source)
        self.assertIn("--enable-zlib", source)
        self.assertNotIn("--enable-gpl", source)
        self.assertIn("de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f", source)


if __name__ == "__main__":
    unittest.main()
