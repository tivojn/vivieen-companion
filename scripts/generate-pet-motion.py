#!/usr/bin/env python3
"""Create Vivieen walk and edge-idle motion for one built avatar."""
import argparse
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from studio import build as registry
from studio import motion


def main():
    parser = argparse.ArgumentParser(
        description="Generate image-to-video Pet motion and local alpha atlases.")
    parser.add_argument("slug", help="Built avatar slug")
    parser.add_argument(
        "--idle-pose-reference",
        help="Optional local image used only as edge-idle pose geometry")
    parser.add_argument(
        "--keyframes-only", action="store_true",
        help="Generate cached walk and idle stills for inspection, then stop")
    arguments = parser.parse_args()
    avatar_dir = registry.adir(arguments.slug)
    if not os.path.isdir(avatar_dir):
        parser.error(f"avatar not found: {arguments.slug}")

    def progress(stage, value, label):
        print(f"[{round(value * 100):3d}%] {stage}: {label}", flush=True)

    def logger(message):
        print(message, flush=True)

    if arguments.keyframes_only:
        previews = motion.preview_keyframes(
            avatar_dir,
            pose_reference=arguments.idle_pose_reference,
            log=logger,
        )
        for kind, path in previews.items():
            print(f"{kind}: {path}", flush=True)
        return
    motion.build(
        avatar_dir,
        pose_reference=arguments.idle_pose_reference,
        progress=progress,
        log=logger,
    )


if __name__ == "__main__":
    main()
