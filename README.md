# Vivieen Companion

![Vivieen icon](assets/icon.png)

A privacy-conscious, provider-agnostic talking avatar for Apple Silicon Macs. Vivieen combines a transparent Electron shell, a local FastAPI sidecar, user-selected language and speech providers, and a portrait-derived animation rig with timed visemes, eyelid travel, gaze lock, brow and cheek motion, and head-on-neck movement.

[![CI](https://github.com/tivojn/vivieen-companion/actions/workflows/ci.yml/badge.svg)](https://github.com/tivojn/vivieen-companion/actions/workflows/ci.yml)

## What is included

- **Electron desktop shell** — frameless floating avatar, tray controls, always-on-top mode, Settings window, microphone permission isolation, and a packaged Python runtime.
- **Provider choice** — EnConvo global defaults, Ollama, OpenAI, Anthropic, Gemini, xAI, Groq, DeepSeek, OpenRouter, LM Studio, local Kokoro, Edge TTS, ElevenLabs, macOS voices, and MLX Whisper.
- **Route proof** — the chat status reports the provider and model used by the completed request. A model's self-reported identity is deliberately not trusted.
- **Avatar Studio** — upload a portrait, review the crop, generate a viseme bank, run pose-locking and animation QA, then activate the result without restarting the app.
- **Privacy-first release** — no portrait, generated avatar, API key, user configuration, QA render, runtime cache, or historical media blob is part of this repository or application bundle.

## Platform

The packaged application currently targets **macOS 14 or newer on Apple Silicon (arm64)**. Local Kokoro and Whisper use MLX/Metal. The browser UI and direct cloud providers are portable in principle, but Windows and Linux packaging are not claimed or tested in this release.

## Quick start

Prerequisites:

- macOS 14 or newer on Apple Silicon
- Node.js 22 or newer
- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- EnConvo if you want inherited global providers or one-click portrait-to-viseme generation

Install and run:

```bash
git clone https://github.com/tivojn/vivieen-companion.git
cd vivieen-companion
npm install
./scripts/setup-electron-backend.sh
npm start
```

A fresh installation intentionally contains no avatar. Electron opens Settings, where you can add a portrait and build the animation assets. The face-landmark model and portable FFmpeg binary are fetched during setup rather than committed to Git.

For browser-only development:

```bash
./run.sh
```

## Providers

Vivieen starts with `EnConvo Global Default` for language, speech, and transcription. When EnConvo is installed, each request rereads that modality's current global selection and calls the exact selected endpoint and model. Credentials remain in EnConvo and are never copied into Vivieen.

You can instead configure direct providers in Settings. Direct API keys are stored only in the local `config.json` with mode `0600`, are never returned to the browser, and are not reused after switching providers. Ollama and LM Studio keep model inference local.

Changing an EnConvo global model takes effect on the next request. After a reply, the top-left status is transport evidence such as:

```text
READY · OLLAMA · QWEN3.5-35B-UNCENSORED:LATEST
```

## Build the Electron app

Create an unpacked application for smoke testing:

```bash
npm run pack
```

Build an unsigned DMG and ZIP:

```bash
npm run dist
```

Artifacts are written to `dist-electron/`. The build stages a relocatable Python 3.12 runtime, exact Python dependencies, a checksummed MediaPipe face-landmark model, the backend, and the two web surfaces. It never stages `avatars/`, `active.json`, `config.json`, logs, QA proofs, or local caches.

The distributed build is unsigned and unnotarized. On another Mac, use **Control-click → Open** the first time. Production distribution should add an Apple Developer ID and notarization rather than asking users to disable Gatekeeper.

## Test and audit

```bash
npm run check
```

That command validates Electron and browser JavaScript syntax, compiles the Python entry points, runs the unit and security suite, and executes the public-release privacy gate.

Run animation QA against your local active avatar separately:

```bash
npm run check:avatar
```

The avatar QA command is intentionally excluded from CI because portraits and generated face assets are private runtime data.

## Privacy and security

See [PRIVACY.md](PRIVACY.md) and [SECURITY.md](SECURITY.md). The key safeguards are:

- Electron binds the backend to `127.0.0.1` and uses a random 256-bit per-launch token on every browser-to-backend request.
- Electron verifies the backend application identity before reusing a port.
- Cross-origin mutations are rejected.
- File serving uses real path containment rather than string-prefix checks.
- Portrait uploads are capped at 20 MB and use secure temporary files.
- The Electron permission handler allows microphone capture only, never camera capture.
- Backend logs and window state use user-only file permissions.
- The release audit blocks credentials, personal home paths, user media, runtime configuration, caches, models, proofs, and oversized generated artifacts.

No desktop application can defend against a fully compromised local account. Treat third-party model endpoints as data processors: text, audio, or portrait-derived generation inputs leave the Mac when you choose a cloud provider.

## Project layout

```text
electron/   macOS shell, tray, permissions, authenticated sidecar lifecycle
server/     FastAPI routes, providers, alignment, viseme scheduling
studio/     portrait preparation, generation, compositing, export, animation rig
web/        avatar runtime and Settings UI
qa/         optional local animation QA tools
tests/      provider, routing, privacy, and API security regressions
scripts/    setup, model fetch, icon generation, packaging, release audit
```

Runtime data is outside the public source tree. In development it lives under ignored `avatars/`, `active.json`, and `config.json`. In the packaged app it lives in Electron's macOS `userData` directory.

## Known boundaries

- Portrait-to-viseme generation currently uses EnConvo's configured OpenAI image provider and may incur provider charges.
- Cloud LLM, TTS, STT, and image providers receive the inputs required for the operation you selected.
- Local ML models download weights on first use and can require substantial memory and disk space.
- The current packaged build is arm64-only and unsigned.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues should follow [SECURITY.md](SECURITY.md), not a public issue.

## License

Vivieen Companion source code is released under the [MIT License](LICENSE). Downloaded models, Python packages, Electron, Chromium, FFmpeg, and other dependencies retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
