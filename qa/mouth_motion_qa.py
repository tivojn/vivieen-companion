#!/usr/bin/env python3
"""Hard anatomy gates for published or staged viseme banks."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from studio import anatomy, build, rig


def run(slug="vivieen-front", avatar_dir=None, profile=None):
    directory = avatar_dir or build.adir(slug)
    if profile is None:
        profile = rig.from_manifest(build.read_manifest(slug))
    result = anatomy.validate(
        os.path.join(directory, "keyframe.png"),
        os.path.join(directory, "visemes"),
        profile,
        diag_dir=os.path.join(directory, "diag"),
    )

    print(f"PASS · {slug}: {anatomy.summary(result)}")
    return result


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "vivieen-front")
