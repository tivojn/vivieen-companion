# Contributing

Contributions are welcome, especially for provider compatibility, accessibility, animation quality, packaging, and privacy hardening.

## Setup

```bash
npm install
./scripts/setup-electron-backend.sh
npm run check
```

The packaged application currently targets Apple Silicon Macs. Keep changes portable where practical, but do not claim another platform without testing it.

## Pull requests

- Keep changes focused and preserve the existing provider contracts.
- Add or update tests for behavior changes.
- Run `npm run check` before opening a pull request.
- Run `npm run check:avatar` when changing animation or compositing code.
- Do not commit generated proof images, model weights, builds, local configuration, or runtime data.
- Never use a real portrait, recording, API key, or private provider response as a fixture.
- Describe any new outbound network request and document what data it transmits.
- Keep credentials out of browser state, API responses, logs, exceptions, and telemetry.

## Provider changes

For EnConvo inheritance, route the exact selected provider and model and refresh the selection at request time. Do not infer route identity from model-generated text. Direct provider changes must preserve key isolation when switching vendors.

## Animation changes

The face runtime is composited from private local avatar assets. Public tests should validate contracts and algorithms without shipping those assets. Local visual QA can write to ignored `qa/proof/`.

## Security

Follow [SECURITY.md](SECURITY.md) for vulnerabilities. Public issues are appropriate for ordinary bugs and feature requests, not privacy or credential exposure.
