#!/usr/bin/env python3
"""Reject copyleft or bundled-media dependencies from the Electron Python set."""
from __future__ import annotations

import importlib.metadata
import pathlib
import re
import sys


DENIED_PACKAGES = {
    "espeakng-loader",
    "imageio-ffmpeg",
    "kokoro",
    "misaki",
    "phonemizer-fork",
}
DENIED_FILENAMES = re.compile(r"(?:^|[-_.])(espeak|ffprobe)(?:[-_.]|$)", re.IGNORECASE)


def normalize(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: audit-electron-dependencies.py SITE_PACKAGES")
    root = pathlib.Path(sys.argv[1]).resolve()
    if not root.is_dir():
        raise SystemExit(f"site-packages directory not found: {root}")

    failures: list[str] = []
    distributions = list(importlib.metadata.distributions(path=[str(root)]))
    for distribution in distributions:
        name = normalize(distribution.metadata.get("Name") or "")
        if name in DENIED_PACKAGES:
            failures.append(f"denied package: {name}")
        metadata = "\n".join(
            [distribution.metadata.get("License-Expression") or "",
             *distribution.metadata.get_all("Classifier", [])]
        ).upper()
        metadata = metadata.replace("LESSER GENERAL PUBLIC LICENSE", "")
        metadata = metadata.replace("LGPL", "")
        if "AGPL" in metadata or "SSPL" in metadata or re.search(
                r"(?:^|[^A-Z])GPL(?:[^A-Z]|$)|GENERAL PUBLIC LICENSE", metadata):
            failures.append(f"copyleft license metadata: {name}")

    for path in root.rglob("*"):
        if path.is_file() and DENIED_FILENAMES.search(path.name):
            failures.append(f"denied bundled executable or library: {path.relative_to(root)}")

    if failures:
        raise SystemExit("\n".join(sorted(set(failures))))
    print(f"Electron dependency audit passed ({len(distributions)} distributions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
