# Privacy

Vivieen Companion is designed to keep user-controlled data local unless the user selects a network provider that requires it.

## Data stored on the Mac

Vivieen may store:

- uploaded portrait source images;
- generated viseme frames, cut-outs, full-body source images, masks, motion keyframes and source clips, alpha atlases, previews, and avatar manifests;
- the active-avatar selection;
- provider configuration and direct API keys;
- companion position, framing, zoom, opacity, click-through, lock, and always-on-top state;
- backend diagnostic logs;
- downloaded local model weights.

In development these files live in ignored runtime paths inside the working directory. In the packaged app they live under Electron's macOS `userData` directory. Configuration, window state, and backend logs are written with user-only permissions where the platform supports them.

None of this runtime data is committed to the public repository or bundled into release artifacts.

## Data that may leave the Mac

Data leaves the Mac only when a selected operation requires a remote service:

- a cloud LLM receives the conversation and system persona;
- a cloud TTS provider receives response text;
- a cloud STT provider receives recorded audio;
- portrait-to-viseme generation receives the prepared portrait and generation prompt through the configured EnConvo image provider;
- Full Body Studio sends the prepared identity portrait and the user's wardrobe, treatment, presence, and additional directions to EnConvo's currently configured image provider; the returned image is segmented locally with macOS Vision and the generated face is not used as the runtime identity;
- Desktop Motion sends the built full-body image and optional pose reference to EnConvo's configured image provider, then sends the generated walk and idle keyframes to the configured video provider. The pose reference is used only for geometry: the server's staged copy is deleted after the job, it is not stored in the motion directory, and it is never published to the live runtime. Returned clips are decoded and alpha-cut locally with macOS Vision;
- first portrait use downloads the public, checksum-pinned Face Landmarker model from Google's `storage.googleapis.com`; the portrait is not part of that download request;
- source setup downloads public runtime dependencies.

Provider privacy policies and retention terms apply to those requests. Use Ollama or LM Studio for local language models, Kokoro or macOS voices for local speech, and MLX Whisper for local transcription when data residency matters.

## EnConvo integration

When `EnConvo Global Default` is selected, Vivieen reads only whitelisted routing metadata: provider, model, voice, language, speed, temperature, format, and reasoning mode. Provider credentials stay inside EnConvo. Vivieen does not copy them into its configuration, API responses, browser state, route telemetry, or logs.

## Microphone and camera

The Electron shell grants media permission only for microphone audio requested by the local Vivieen page. It does not grant camera capture. Recording begins while the talk control is held and the browser media stream is stopped when recording ends.

Selecting an existing portrait uses the normal file upload control; it does not activate the camera.

## Local HTTP boundary

The backend listens only on `127.0.0.1`. In the packaged app, Electron creates a random 256-bit token for every launch and adds it to requests through the Electron session. The backend rejects requests without the matching token. State-changing cross-origin requests are also rejected.

Browser-only development does not enable a token unless `VIVIEEN_AUTH_TOKEN` is set. Do not expose the development server on a public interface.

## Deleting data

Generated body and Desktop Motion assets can be removed independently in Full Body Studio, and complete avatars can be deleted from Settings. Uninstalling the app does not automatically erase Electron's `userData` directory. To remove all local Vivieen data, quit the app and remove its application-support directory from your macOS user account.

## Repository privacy gate

`scripts/public-release-audit.py` rejects user media, avatar data, active configuration, credentials, personal home paths, generated models, caches, QA proofs, logs, and oversized artifacts before release. Public releases use fresh Git history so removed private blobs are not recoverable from earlier commits.
