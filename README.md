# Vivieen Companion

![Vivieen icon](assets/icon.png)

A privacy-conscious, provider-agnostic desktop companion for Apple Silicon Macs. Vivieen lives as a transparent, alpha-aware figure rather than an app-shaped panel: drag it across displays, let clear pixels pass clicks through to macOS, choose full-body through face-level framing, and tune opacity from fully present to a 50% ghost. Its local FastAPI sidecar and portrait-derived animation rig retain timed visemes, eyelid travel, gaze lock, brow and cheek motion, and head-on-neck movement.

[![CI](https://github.com/tivojn/vivieen-companion/actions/workflows/ci.yml/badge.svg)](https://github.com/tivojn/vivieen-companion/actions/workflows/ci.yml)

## What is included

- **Desktop Pet Mode** — frameless cut-out rendering, alpha-aware click-through, native right-click controls, persisted drag position, 0–100% opacity, full-body through face framing, generated Horizon Walk motion above the Dock, a global recovery shortcut, tray controls, and always-on-top behavior.
- **Provider choice** — EnConvo global defaults, Ollama, OpenAI, Anthropic, Gemini, xAI, Groq, DeepSeek, OpenRouter, LM Studio, local Kokoro, Edge TTS, ElevenLabs, macOS voices, and MLX Whisper.
- **Route proof** — the chat status reports the provider and model used by the completed request. A model's self-reported identity is deliberately not trusted.
- **Avatar Studio** — upload a portrait, review the crop, build a canonical HD head-only identity through EnConvo's current image provider, generate the viseme bank from that head, run pose-locking and animation QA, then activate the result without restarting the app.
- **Full Body Studio** — reference the canonical HD head to generate a matched front, right-side, and back full-body turn-around through EnConvo's current image provider, start from a styling prompt written for that specific portrait — the subject's medium, presentation, apparent age, and implied profession all steer the brief, so a photoreal fashion subject and a game hero are not dressed from the same paragraph — then edit it or ask for a rewrite before any generation, remove all three backgrounds locally with macOS Vision, and attach the front body beneath the original 1024px animated face rather than replacing its identity.
- **Desktop Motion Studio** — generate or remove Horizon Walk and Edge Idle independently, without changing the other behavior. Horizon Walk automatically uses the right-side body and offers Office walk, Runway catwalk, Relaxed stroll, Brisk power walk, Elegant promenade, and an experimental Cartwheel. Edge Idle uses the front body and offers clearly described supported poses or a custom geometry direction. Each selected clip is generated on chroma-key green, alpha-cut with spill suppression and macOS Vision pose tracking, reduced to a validated contiguous loop, and published as a deterministic PNG atlas instead of relying on browser alpha-video codecs. Native window travel follows the same per-frame trajectory; when Edge Idle is absent, the original standing body is the edge fallback. Hover, chat, recording, and speech always restore the original standing body and live face/viseme rig.
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
- Scroll or pinch over the figure to resize it continuously; after Horizon Walk is generated, double-click the figure to start roaming.
- Right-click the figure and choose **Size & Opacity…** for continuous 25–400% size and 0–100% opacity sliders. The native transparent window expands with the figure, up to the current display's full work area, so giant views remain inside the alpha canvas rather than being clipped by a fixed Electron frame.
- The same menu provides **Talk to Vivieen…**, view, click-through, position lock, audio-following, Settings, hide, and quit controls.
- Clear pixels pass pointer input to the app underneath when click-through is enabled.
- Press `Command+Shift+0` or choose **Recover Companion** from the tray to restore a hidden, fully transparent, off-screen, or click-through companion.
- Full Body remains a first-class view; Half Body is merely the default.
- After Horizon Walk is generated in Full Body Studio, double-click the figure or enable **Horizon Walk Along Dock** from the figure or tray menu. Vivieen traverses between display edges using the selected movement style. At an edge it plays the independently generated Edge Idle when available, otherwise it falls back to the original standing body; interaction always restores the standing/live-face rig. Use either menu to pause the walk.
- While **Follow EnConvo Audio** is enabled, the menu remains the status/control surface and the avatar window stays alpha-only; the bottom chat controls are suppressed.

The same controls are available from the command line. `--kind` accepts `walk`, `idle`, or the backward-compatible default `both`. Horizon Walk automatically uses the generated side body; Edge Idle uses the front body, and an optional pose image controls its geometry only without being retained in the motion directory or live runtime:

```bash
VIVIEEN_DATA_DIR="$HOME/Library/Application Support/Vivieen/backend-data" \
  python scripts/generate-pet-motion.py vivieen-front \
  --kind walk --walk-style runway

VIVIEEN_DATA_DIR="$HOME/Library/Application Support/Vivieen/backend-data" \
  python scripts/generate-pet-motion.py vivieen-front \
  --kind idle --idle-pose heel-up
```

Available walk styles are `office`, `runway`, `stroll`, `power`, `promenade`, and `cartwheel`. Add `--keyframes-only` to inspect only the selected stills before consuming video-generation credits; a subsequent full run reuses those cached keyframes.

To reprocess a previously approved original Walk without changing its timing, pose, travel, or RGB, use the current green-screen derivative only as a pose-aligned alpha matte:

```bash
VIVIEEN_DATA_DIR="$HOME/Library/Application Support/Vivieen/backend-data" \
  python scripts/generate-pet-motion.py vivieen-front \
  --approved-walk-original "/path/to/walk-original-source.mp4"
```

The command reuses the approved loop stored in `motion.json`, validates matte alignment and source-color fidelity, replaces only Walk assets, leaves Idle unchanged, and retains a timestamped rollback copy under `motion/backups/`. Use `--approved-walk-matte` only when the matte derivative is not the current `motion/raw/walk-source.mp4`.

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

- Desktop Motion is generated media, not a skeletal animation system. The current baseline substantially reduces conveyor-belt foot sliding with a continuous native-FPS loop, source-root travel, per-frame window offsets, planted-shoe calibration, and a pose-validated gait cycle, but it cannot repair every defect already baked into the provider video.
- Motion v7 requests chroma-key green for both keyframes and videos and records the exact front/side body plus canonical-head references used for each motion asset. Green-screen clips use adaptive hue/dominance alpha keying while preserving opaque source RGB; despill is restricted to genuinely green-dominant pixels, edge decontamination runs once on semi-transparent pixels only, and RGBA scaling is premultiplied to prevent transparent green from bleeding into hair, skin, or black shoes. Legacy non-green clips fall back to macOS Vision person segmentation. The edge-idle gate also requires the back/shoulder-blade contact and the raised stiletto heel to share one vertical wall line while the torso projects forward and the supporting foot remains planted ahead; physically unsupported wall leans are rejected. Motion alpha is repaired only after adjacent frames are aligned by locally detected torso motion; restored RGB is limited to pixels whose alpha was recovered. Wrist- and ankle-aligned repair, solid-interior recovery, lower-body hole inpainting, extremity integrity, and source-versus-output colour gates reject damaged clips. The full invariants and checkpoint order are documented in [`docs/MOTION_PIPELINE.md`](docs/MOTION_PIPELINE.md).
- Walk inherits the same hue-safe Idle pipeline, then adds fixed-camera registration, independent left/right pose coverage, closed per-limb pose and velocity seams, complete arm and leg excursions, contralateral motion, extremity integrity, and portrait-authoritative hairstyle locking. Office walk keeps the strict low-wrist and low-foot-lift gate; runway, stroll, power walk, and promenade use style-aware ranges without weakening identity, closure, anatomy, or trajectory checks. Cartwheel is validated as a complete left-to-right traversal rather than misclassified as an office gait cycle. For an already user-approved original Walk, reprocessing instead pose-aligns alpha from its green-screen derivative while preserving the original contiguous frames, timing, travel, and RGB exactly.
- These generated roaming frames are never used as the interactive face. Hover, chat, recording, speech, and lip-sync restore the original standing body and calibrated live face.
- Portrait-to-viseme and Desktop Motion generation use the selected EnConvo image/video providers and may incur provider charges.
- Cloud LLM, TTS, STT, image, and video providers receive the inputs required for the operation you selected. Full Body generation makes three image requests for the front, side, and back plates. Desktop Motion sends only the references required for the selected behavior: the side body and canonical HD head for Horizon Walk, or the front body, canonical HD head, and optional pose reference for Edge Idle. It then sends only the selected generated keyframe to the video provider; the server-staged pose copy is deleted after the job.
- Local ML models download weights on first use and can require substantial memory and disk space.
- The current packaged target is arm64-only. Developer ID signing requires a local or CI signing identity, and Gatekeeper-ready distribution additionally requires Apple notarization.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues should follow [SECURITY.md](SECURITY.md), not a public issue.

## License

Vivieen Companion source code is released under the [MIT License](LICENSE). Downloaded models, Python packages, Electron, Chromium, FFmpeg, and other dependencies retain their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
