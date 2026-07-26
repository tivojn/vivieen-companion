# Security Policy

## Supported version

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose portraits, audio, credentials, local files, or provider requests.

Use GitHub's **Report a vulnerability** private advisory form for this repository. Include:

- affected version and macOS version;
- reproduction steps;
- expected and observed impact;
- whether local user data or credentials may have been exposed;
- any suggested mitigation.

Avoid attaching real portraits, recordings, API keys, access tokens, or private configuration. Use synthetic fixtures and redacted logs.

## Security model

Vivieen is a local desktop application, not a multi-user service. The packaged backend:

- binds to loopback only;
- authenticates Electron requests with a random per-launch token;
- verifies backend identity before port reuse;
- blocks cross-origin state changes;
- restricts file responses to contained runtime paths;
- caps portrait uploads;
- grants microphone-only media permission;
- redacts provider errors and never returns stored API keys to the browser.

A process running as the same macOS user may still read files that user can read, inspect process memory, or tamper with the installed application. Full protection against a compromised local account is outside the threat model.

## Release handling

Before publishing:

```bash
npm run check
npm audit --omit=dev
```

Release artifacts must be built from a clean export. Never publish `avatars/`, `active.json`, `config.json`, `.learnings/`, model caches, QA proofs, logs, `.venv/`, Electron runtime staging directories, or an old Git history containing generated face assets.
