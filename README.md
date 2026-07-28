# Vivieen Companion

![Vivieen icon](assets/icon.png)

A privacy-conscious, provider-agnostic desktop companion for Apple Silicon Macs. Vivieen lives as a transparent, alpha-aware figure rather than an app-shaped panel: drag it across displays, let clear pixels pass clicks through to macOS, choose full-body through face-level framing, and tune opacity from fully present to a 50% ghost. Its local FastAPI sidecar and portrait-derived animation rig retain timed visemes, eyelid travel, gaze lock, brow and cheek motion, and head-on-neck movement.

[![CI](https://github.com/tivojn/vivieen-companion/actions/workflows/ci.yml/badge.svg)](https://github.com/tivojn/vivieen-companion/actions/workflows/ci.yml)

## What is included

- **Desktop Pet Mode** — frameless cut-out rendering, alpha-aware click-through, native right-click controls, persisted drag position, 0–100% opacity, full-body through face framing, generated Horizon Walk motion above the Dock, a global recovery shortcut, tray controls, and always-on-top behavior.
- **Provider choice** — EnConvo global defaults, Ollama, OpenAI, Anthropic, Gemini, xAI, Groq, DeepSeek, OpenRouter, LM Studio, local Kokoro, Edge TTS, ElevenLabs, macOS voices, and MLX Whisper.
- **Route proof** — the chat status reports the provider and model used by the completed request. A model's self-reported identity is deliberately not trusted.
- **Avatar Studio** — upload a portrait, review the crop, generate a viseme bank, run pose-locking and animation QA, then activate the result without restarting the app.
- **Full Body Studio** — inherit EnConvo's current image provider, direct wardrobe and presence, remove the background locally with macOS Vision, and attach the generated body beneath the original 1024px animated face rather than replacing its identity.
- **Desktop Motion Studio** — turn the built body into a right-facing image-to-video walk and a pose-directed edge idle with EnConvo's current image/video defaults, alpha-cut every native frame locally with macOS Vision, preserve one contiguous gait sequence, and publish deterministic PNG atlases instead of relying on browser alpha-video codecs. Native window travel follows the same per-frame trajectory, calibrated against the planted shoes to reduce foot sliding. Hover, chat, recording, and speech always restore the original standing body and live face/viseme rig.
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

### Desktop Pet controls

- Drag the visible figure to move it unless position lock is enabled.
- Scroll or pinch over the figure to resize it continuously; double-click sends EnConvo's configured Right Option voice-command shortcut through the signed native helper. macOS may require Vivieen to be enabled in **Privacy & Security → Accessibility** before it can post that modifier event.
- Right-click the figure and choose **Size & Opacity…** for continuous 25–400% size and 0–100% opacity sliders. The native transparent window expands with the figure, up to the current display's full work area, so giant views remain inside the alpha canvas rather than being clipped by a fixed Electron frame.
- The same menu provides **Talk to Vivieen…**, view, click-through, position lock, audio-following, Settings, hide, and quit controls.
- Clear pixels pass pointer input to the app underneath when click-through is enabled.
- Press `Command+Shift+0` or choose **Recover Companion** from the tray to restore a hidden, fully transparent, off-screen, or click-through companion.
- Full Body remains a first-class view; Half Body is merely the default.
- After Desktop Motion is generated in Full Body Studio, enable **Horizon Walk Along Dock** from the figure or tray menu. Vivieen walks between display edges, holds the generated back-lean idle at each edge, and returns to the original standing/live-face rig while you interact.
- While **Follow EnConvo Audio** is enabled, the menu remains the status/control surface and the avatar window stays alpha-only; the bottom chat controls are suppressed.

The same motion build is available as a reusable command-line workflow. The pose image controls geometry only and is not retained in the motion directory or live runtime:

```bash
VIVIEEN_DATA_DIR="$HOME/Library/Application Support/Vivieen/backend-data" \
  python scripts/generate-pet-motion.py vivieen-front \
  --idle-pose-reference "/path/to/edge-pose.png"
```

Add `--keyframes-only` to inspect the walk and idle stills before consuming video-generation credits; a subsequent full run reuses those cached keyframes.

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

Build a DMG and ZIP (Developer ID-signed when a compatible identity is available):

```bash
npm run dist
```

Artifacts are written to `dist-electron/`. The build stages a relocatable Python 3.12 runtime, exact Python dependencies, a checksummed MediaPipe face-landmark model, the backend, and the two web surfaces. It never stages `avatars/`, `active.json`, `config.json`, logs, QA proofs, or local caches.

The macOS release configuration enables Hardened Runtime and explicit Electron/audio entitlements. Follow EnConvo's Swift capture engine is embedded as a signed executable inside `Vivieen.app`, not as a second application. A post-sign step gives that executable the same `com.vivieen.companion` code identity as its parent and reseals the app, so System Audio Recording belongs to Vivieen rather than a second helper entry. When a valid Developer ID Application identity is present in the login Keychain or CI environment, electron-builder signs the main app, frameworks, Electron helpers, and embedded capture executable. Notarization requires separate Apple credentials; without it, Gatekeeper reports an internet-downloaded artifact as unnotarized.

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

- Desktop Motion is generated media, not a skeletal animation system. The current baseline substantially reduces conveyor-belt foot sliding with a continuous native-FPS loop, source-root travel, per-frame window offsets, and planted-shoe calibration, but it cannot repair every defect already baked into the provider video.
- Generated motion frames can still show intermittent anatomy or matte instability: a forehead, hand, limb, or garment edge may briefly erode or flicker; narrow high-heel geometry can blur or absorb the studio-background color. Temporal alpha repair, solid-interior recovery, and lower-body hole inpainting address common dropouts but do not yet provide semantic body-part reconstruction.
- A visually matched contiguous loop can close before every secondary motion finishes. In particular, an arm swing may reverse midway rather than completing a perfectly closed contralateral cycle. A future motion-quality pass should add pose tracking, per-limb cycle validation, and rejection/regeneration gates before publish.
- These generated roaming frames are never used as the interactive face. Hover, chat, recording, speech, and lip-sync restore the original standing body and calibrated live face.
- Portrait-to-viseme and Desktop Motion generation use the selected EnConvo image/video providers and may incur provider charges.
- Cloud LLM, TTS, STT, image, and video providers receive the inputs required for the operation you selected. Desktop Motion sends the built body plus the optional pose reference to the selected image provider, then sends only generated keyframes to the selected video provider; the server-staged pose copy is deleted after the job.
- Local ML models download weights on first use and can require substantial memory and disk space.
- The current packaged target is arm64-only. Developer ID signing requires a local or CI signing identity, and Gatekeeper-ready distribution additionally requires Apple notarization.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues should follow [SECURITY.md](SECURITY.md), not a public issue.

## License

Vivieen Companion source code is released under the [MIT License](LICENSE). Downloaded models, Python packages, Electron, Chromium, FFmpeg, and other dependencies retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
