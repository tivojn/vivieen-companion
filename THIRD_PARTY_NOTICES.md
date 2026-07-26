# Third-Party Notices

Vivieen Companion uses third-party software and model assets. This file is informational; each component remains governed by its own license.

## Electron and Chromium

Electron, Chromium, Node.js, and their bundled components are distributed under their respective open-source licenses. Electron's generated license bundle is included inside the packaged application.

- Project: https://www.electronjs.org/
- Source: https://github.com/electron/electron

## FFmpeg 7.1.5

The macOS package builds FFmpeg from official source with `--disable-everything`, no GPL components, no network protocols, and only the decoders, muxers, filters, and Apple VideoToolbox encoder required by Vivieen. The staging script rejects a binary unless FFmpeg reports `LGPL version 2.1 or later` and links only to Apple system libraries.

- Project: https://ffmpeg.org/
- Exact source: https://ffmpeg.org/releases/ffmpeg-7.1.5.tar.xz
- Source SHA-256: `de668509caf9e35e3cd162473441fdb29538c6d96ed080292b3cf9e6fc5d558f`
- License: GNU Lesser General Public License, version 2.1 or later

`LICENSE.LGPLv2.1.txt` is staged beside the FFmpeg binary in the packaged app. The complete corresponding source is the exact archive above; the full reproducible configure invocation is in `scripts/stage-electron-ffmpeg.sh`.

## MediaPipe and Face Landmarker

Vivieen packages the Apache-2.0-licensed MediaPipe Python library. Google's separately hosted Face Landmarker model is not stored in this Git repository or redistributed in the application. On first portrait use, Vivieen downloads the official model over HTTPS into private application data and accepts it only when its SHA-256 matches the pinned value.

- Project: https://github.com/google-ai-edge/mediapipe
- Model: https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
- Model SHA-256: `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff`
- MediaPipe library license: Apache License 2.0

Use of the separately downloaded model remains subject to Google's applicable terms.

## Python and JavaScript dependencies

Runtime dependencies are declared in `requirements-backend.txt` and fully hash-locked for macOS arm64 in `requirements-backend.lock`; JavaScript dependencies are locked in `package-lock.json`. Their package metadata and license files are preserved in the Electron bundle where supplied by the publisher.

Notable projects include FastAPI, Uvicorn, NumPy, OpenCV, Pillow, SoundFile, MediaPipe, HTTPX, MLX, MLX Whisper, Kokoro, Misaki, Edge TTS, PyTorch, spaCy, the `en_core_web_sm` English model, electron-builder, and their transitive dependencies. spaCy and `en_core_web_sm` are distributed under the MIT License.

Review the dependency lockfiles and package metadata before redistributing a modified build. Adding an optional codec, native library, model, font, voice, or image can change the resulting license obligations.
