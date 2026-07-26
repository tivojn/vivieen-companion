#!/usr/bin/env python3
"""Fail when private or generated material enters the public source tree."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
DENIED_PARTS = {
    ".electron-python-runtime", ".electron-models", ".electron-ffmpeg", ".learnings", ".venv",
    "avatars", "dist", "dist-electron", "models", "node_modules", "proof",
    "__pycache__",
}
DENIED_NAMES = {"active.json", "config.json", ".DS_Store", "backend.log"}
DENIED_SUFFIXES = {
    ".bak", ".heic", ".jpeg", ".jpg", ".log", ".mov", ".mp3", ".mp4",
    ".pyc", ".task", ".tmp", ".wav", ".webm",
}
ALLOWED_BINARY = {
    Path("assets/icon.icns"), Path("assets/icon.png"),
}
TEXT_SUFFIXES = {
    ".cjs", ".css", ".html", ".js", ".json", ".md", ".mjs", ".py",
    ".sh", ".toml", ".txt", ".yaml", ".yml",
}
PATTERNS = {
    "personal home path": re.compile(
        r"/" + r"Users/(?!example(?:/|$)|yourname(?:/|$))[^/\s\"']+"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    "provider credential": re.compile(r"\b(?:sk-|gsk_|xai_)[A-Za-z0-9_-]{12,}\b"),
    "agent workspace id": re.compile(r"agent\|[A-Za-z0-9_-]{8,}"),
}


def candidate_files() -> list[Path]:
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, capture_output=True, check=True,
        )
        return [Path(value.decode()) for value in result.stdout.split(b"\0") if value]
    return [path.relative_to(ROOT) for path in ROOT.rglob("*") if path.is_file()]


def audit() -> list[str]:
    errors: list[str] = []
    for relative in sorted(set(candidate_files())):
        path = ROOT / relative
        if not path.is_file():
            continue
        parts = set(relative.parts)
        if parts & DENIED_PARTS:
            errors.append(f"generated/private directory: {relative}")
            continue
        if relative.name in DENIED_NAMES:
            errors.append(f"runtime/private file: {relative}")
            continue
        suffix = relative.suffix.lower()
        if suffix in DENIED_SUFFIXES and relative not in ALLOWED_BINARY:
            errors.append(f"generated/private media: {relative}")
            continue
        if path.stat().st_size > 10 * 1024 * 1024:
            errors.append(f"oversized source artifact: {relative}")
            continue
        if suffix not in TEXT_SUFFIXES and relative not in ALLOWED_BINARY:
            continue
        if relative in ALLOWED_BINARY:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{label}: {relative}")
    return errors


def main() -> int:
    errors = audit()
    if errors:
        print("Public release audit failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Public release audit passed ({len(candidate_files())} files inspected).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
