#!/usr/bin/env python3
"""Create optional Horizon Walk and Edge Idle motion for one built avatar."""
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
        "--kind", choices=("walk", "idle", "both"), default="both",
        help="Motion behavior to generate; defaults to both for compatibility")
    parser.add_argument(
        "--walk-style", choices=tuple(motion.WALK_STYLE_PRESETS),
        default=motion.DEFAULT_WALK_STYLE,
        help="Horizon Walk movement style")
    parser.add_argument(
        "--idle-pose", choices=(*motion.IDLE_POSE_PRESETS, "custom"),
        default=motion.DEFAULT_IDLE_POSE,
        help="Edge Idle supported pose")
    parser.add_argument(
        "--idle-pose-prompt", default="",
        help="Custom Edge Idle direction when --idle-pose=custom")
    parser.add_argument(
        "--idle-pose-reference",
        help="Optional local image used only as edge-idle pose geometry")
    parser.add_argument(
        "--keyframes-only", action="store_true",
        help="Generate cached walk and idle stills for inspection, then stop")
    parser.add_argument(
        "--approved-walk-original",
        help="Reprocess an approved original walk while preserving its RGB and timing")
    parser.add_argument(
        "--approved-walk-matte",
        help="Green-screen derivative used only as the approved walk alpha matte")
    parser.add_argument(
        "--approved-walk-loop", metavar="START:END",
        help="Approved contiguous source loop; defaults to current motion metadata")
    arguments = parser.parse_args()
    avatar_dir = registry.adir(arguments.slug)
    if not os.path.isdir(avatar_dir):
        parser.error(f"avatar not found: {arguments.slug}")

    def progress(stage, value, label):
        print(f"[{round(value * 100):3d}%] {stage}: {label}", flush=True)

    def logger(message):
        print(message, flush=True)

    if arguments.approved_walk_original:
        if arguments.keyframes_only:
            parser.error(
                "--keyframes-only cannot be combined with approved walk reprocessing")
        source_loop = None
        if arguments.approved_walk_loop:
            try:
                source_loop = [
                    int(value)
                    for value in arguments.approved_walk_loop.split(":", 1)
                ]
            except ValueError:
                parser.error("--approved-walk-loop must be START:END")
            if len(source_loop) != 2:
                parser.error("--approved-walk-loop must be START:END")
        result = motion.reprocess_approved_walk(
            avatar_dir,
            arguments.approved_walk_original,
            matte_source=arguments.approved_walk_matte,
            source_loop=source_loop,
            progress=progress,
            log=logger,
        )
        print(f"backup: {result['backup']}", flush=True)
        return
    if arguments.approved_walk_matte or arguments.approved_walk_loop:
        parser.error(
            "--approved-walk-matte and --approved-walk-loop require "
            "--approved-walk-original")
    kinds = (
        ("walk", "idle") if arguments.kind == "both" else (arguments.kind,)
    )
    try:
        walk_style = (
            motion.resolve_walk_style(arguments.walk_style)
            if "walk" in kinds else None
        )
        idle_pose = (
            motion.resolve_idle_pose(
                arguments.idle_pose, arguments.idle_pose_prompt)
            if "idle" in kinds else None
        )
    except ValueError as error:
        parser.error(str(error))

    if arguments.keyframes_only:
        previews = motion.preview_keyframes(
            avatar_dir,
            pose_reference=arguments.idle_pose_reference,
            idle_pose=idle_pose,
            walk_style=walk_style,
            kinds=kinds,
            log=logger,
        )
        for kind, path in previews.items():
            print(f"{kind}: {path}", flush=True)
        return
    motion.build(
        avatar_dir,
        pose_reference=arguments.idle_pose_reference,
        idle_pose=idle_pose,
        kinds=kinds,
        walk_style=walk_style,
        progress=progress,
        log=logger,
    )


if __name__ == "__main__":
    main()
