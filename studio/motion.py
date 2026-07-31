"""Generate and publish identity-locked desktop-companion motion loops."""
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from . import body, cutout


ENCONVO = body.ENCONVO
MOTION_VERSION = 9
# Full provider resolution. These were 512x768 - a decoded-atlas memory
# budget from the traversal era - which threw away almost half the subject
# pixels the in-place loop pipeline now buys (the portrait plate spends its
# whole 720p short side on her). Decoded sheets cost ~3x more RAM at native
# resolution, a fair price for a companion that survives being zoomed.
TARGET_WIDTH = 720
TARGET_HEIGHT = 1088
WALK_FPS = 24
IDLE_FPS = 12
MAX_SHEET_FRAMES = 32
# Gate rejections are dominated by near-misses, so a third candidate
# meaningfully raises the odds a run ships instead of failing outright.
MAX_CANDIDATE_ATTEMPTS = 3
# TEMPORARY reliability mode, by explicit taste (2026-07-30): quality gates
# reject too many honest takes, and a shipped clip with a visible loop seam
# beats a failed generation. While True: the idle uses the FULL raw take
# (its authored first-equals-last frame IS the seam), the in-place walk uses
# the first half of its take with no loop search, and every quality gate on
# those paths is demoted to a logged observation. Flip to False to restore
# strict selection and gating; traversal styles (cartwheel) are unaffected.
RELAXED_LOOP_SHIPPING = True
DEFAULT_WALK_STYLE = "office"
DEFAULT_IDLE_POSE = "back-heel"
WALK_STYLE_PRESETS = {
    "office": {
        "loop_video": (
            "Animate the exact selected person performing a NORMAL charming office "
            "walk IN PLACE, as if on an invisible treadmill, at an ordinary 108\u2013114 "
            "steps per minute. Use ordinary shoe-length steps that glide back under "
            "the body, low toe clearance, soft heel-to-toe contact, almost level "
            "hips, and steady head height. Elbows stay softly bent and close to the "
            "ribs; each wrist travels only a small distance just ahead of and behind "
            "its hip with correct contralateral coordination: right leg forward with "
            "LEFT arm forward, left leg forward with RIGHT arm forward."
        ),
        "label": "Office walk",
        "description": "Natural workplace pace with compact steps and arm swing.",
        "validation": "office-gait",
        "loop": {"target": 1.05, "minimum": 0.85, "maximum": 3.4},
        "keyframe": (
            "right-facing 25–30 degree three-quarter view in one frozen frame of a "
            "NORMAL charming office-floor walk, moving purposefully in a straight line "
            "from one workplace to another. This is not a runway performance. Use one "
            "ordinary shoe-length step: front heel softly touching the floor, rear toe "
            "still skimming it, rear heel lifted only a few centimetres and never above "
            "the opposite ankle, both knees low and relaxed. Keep upper arms close to "
            "the ribs and within 15 degrees of vertical, elbows softly bent, and both "
            "wrists between the hip seam and mid-thigh with only a compact contralateral "
            "counter-swing. BOTH complete arms, elbows, wrists, and hands remain "
            "naturally visible because of the three-quarter torso angle, with a narrow "
            "background gap around each wrist; never spread or raise the arms to expose "
            "them. Catwalk influence is limited to tall posture and a slightly narrow "
            "footpath. Do NOT use a flat side profile, high knee, high front foot, heel "
            "kicked toward the calf or knee, split stride, marching, power walk, long "
            "runway lunge, crossed legs, chest-height hand, airplane arms, or theatrical "
            "arm swing. Spine tall, gaze level toward the destination. Both complete "
            "shoes and the full original hair silhouette must be visible."
        ),
        "video": (
            "Animate the exact selected person taking a NORMAL, charming office walk in "
            "a straight line from one place on an office floor to another, moving "
            "camera-left to camera-right across this locked white-studio frame.\n\n"
            "PRIORITY 2 — ORDINARY OFFICE GAIT: this must look like a real office "
            "professional walking to a meeting, not performing. Use an ordinary 108–114 "
            "steps per minute, one normal shoe-length step, low toe clearance, soft "
            "heel-to-toe contact, almost level hips, and steady head height. The swing "
            "shoe skims only a few centimetres above the floor; the rear heel never kicks "
            "toward the knee or rises above the lower calf. Knees stay low and relaxed. "
            "Catwalk influence is limited to confident posture and a slightly narrow "
            "footpath—never longer steps, higher legs, extra hip rotation, or theatrical "
            "motion.\n\n"
            "PRIORITY 3 — SMALL NATURAL ARM SWING: elbows remain softly bent and close "
            "to the ribs; upper arms stay within 15 degrees of vertical. Each wrist stays "
            "continuously between the hip seam and mid-thigh and travels only a small "
            "distance just ahead of and behind its hip. Hands never rise above the waist "
            "and arms never open sideways. Use correct contralateral coordination: right "
            "leg forward with LEFT arm forward; left leg forward with RIGHT arm forward.\n\n"
            "PRIORITY 4 — BILATERAL COMPLETENESS WITHOUT EXAGGERATION: keep the torso at "
            "a stable right-facing 25–30 degree three-quarter angle so both shoulders, "
            "sleeves, elbows, wrists, and hands remain naturally readable. Preserve a "
            "small separation around both wrists by orientation alone; do NOT increase "
            "arm height or swing width to expose them. Never rotate into a perfectly flat "
            "side profile. Each left and right arm independently completes forward → "
            "back → return to the same pose and velocity at least once."
        ),
        "reject": (
            "Reject high knees, high front kicks, heel-to-knee kicks, split strides, "
            "marching, power walking, runway lunges, shoulder-height hands, airplane "
            "arms, giant arm swing, arms fused into the torso, a missing far arm, "
            "one-sided partial cycles, same-side arm-and-leg motion, or foot sliding. "
            "Natural office walking outranks every other instruction."
        ),
    },
    "runway": {
        "loop_video": (
            "Animate a polished luxury-runway catwalk performed IN PLACE, as if on an "
            "invisible treadmill, at a confident 104\u2013112 steps per minute. Use a "
            "narrow controlled crossover footpath, smooth heel-to-toe placement, "
            "restrained hip rotation, level shoulders, a steady editorial gaze, and "
            "elegant compact contralateral arm swing below the waist."
        ),
        "label": "Runway catwalk",
        "description": "Confident, sensual runway rhythm with controlled crossover steps.",
        "validation": "stylized-gait",
        "loop": {"target": 1.2, "minimum": 0.9, "maximum": 3.5},
        "keyframe": (
            "right-facing 25–30 degree three-quarter view in one poised frame of a "
            "sophisticated runway catwalk. Use a clean moderate stride on a narrow "
            "crossover track, long upright spine, quiet shoulders, controlled hip shift, "
            "soft elbows, and relaxed hands below the waist. The sensuality comes from "
            "posture, rhythm, and confidence—not an extreme lunge, exaggerated sway, "
            "high kick, or costume pose. Keep both complete arms, hands, legs, and shoes "
            "visible and anatomically correct."
        ),
        "video": (
            "Animate a polished luxury-runway catwalk from camera-left to camera-right. "
            "Use a confident 104–112 step-per-minute cadence, a narrow controlled "
            "crossover footpath, smooth heel-to-toe placement, restrained hip rotation, "
            "level shoulders, and a steady editorial gaze. Keep arm swing elegant and "
            "compact below the waist. Complete at least one seamless left-and-right gait "
            "cycle with correct contralateral coordination."
        ),
        "reject": (
            "Reject parody strutting, extreme hip whipping, pageant waving, high kicks, "
            "split strides, stumbling, frozen arms, or a perfectly flat side profile."
        ),
    },
    "stroll": {
        "loop_video": (
            "Animate an unhurried relaxed stroll performed IN PLACE, as if on an "
            "invisible treadmill, at 88\u201398 steps per minute. Use short comfortable "
            "heel-to-toe steps gliding back under the body, gentle contralateral arm "
            "swing, soft shoulders, low foot lift, and a calm level head."
        ),
        "label": "Relaxed stroll",
        "description": "Easy unhurried steps with a soft, casual rhythm.",
        "validation": "stylized-gait",
        "loop": {"target": 1.35, "minimum": 1.0, "maximum": 3.6},
        "keyframe": (
            "right-facing three-quarter view in one natural frame of an easy relaxed "
            "stroll. Use a short comfortable step, loose shoulders, gentle contralateral "
            "arm swing, low toe clearance, level head height, and an unhurried friendly "
            "presence. Keep both complete hands and shoes visible."
        ),
        "video": (
            "Animate an unhurried relaxed stroll from camera-left to camera-right at "
            "88–98 steps per minute. Use short comfortable heel-to-toe steps, gentle "
            "contralateral arm swing, soft shoulders, low foot lift, and a smooth steady "
            "root trajectory. Complete one clean left-and-right gait cycle."
        ),
        "reject": "Reject shuffling, dragging feet, slouching, waving, bouncing, or stopping.",
    },
    "power": {
        "loop_video": (
            "Animate a brisk purposeful power walk performed IN PLACE, as if on an "
            "invisible treadmill, at 124\u2013134 steps per minute. Use grounded moderate "
            "strides gliding back under a stable torso, compact stronger contralateral "
            "arm drive with both hands below the lower ribs, and low swing-foot "
            "clearance."
        ),
        "label": "Brisk power walk",
        "description": "Fast, purposeful cadence with stronger grounded momentum.",
        "validation": "stylized-gait",
        "loop": {"target": 0.95, "minimum": 0.72, "maximum": 3.2},
        "keyframe": (
            "right-facing three-quarter view in one grounded frame of a brisk purposeful "
            "power walk. Use a moderately longer step, slight forward intent from the "
            "ankles, compact athletic contralateral arm drive, low knees, and firm "
            "heel-to-toe contact. Keep both hands below the lower ribs and every limb and "
            "shoe fully visible."
        ),
        "video": (
            "Animate a brisk purposeful power walk from camera-left to camera-right at "
            "124–134 steps per minute. Use grounded moderate strides, a stable torso, "
            "compact stronger contralateral arm drive, low swing-foot clearance, and "
            "constant forward speed. Complete one clean bilateral gait cycle."
        ),
        "reject": "Reject jogging, running, high knees, pumping fists at chest height, or camera shake.",
    },
    "promenade": {
        "loop_video": (
            "Animate an elegant measured promenade performed IN PLACE, as if on an "
            "invisible treadmill, at 92\u2013102 steps per minute. Use fluid medium "
            "heel-to-toe steps gliding back under the body, composed upright carriage, "
            "restrained hip motion, and small symmetrical contralateral arm swing."
        ),
        "label": "Elegant promenade",
        "description": "Graceful measured steps with composed formal carriage.",
        "validation": "stylized-gait",
        "loop": {"target": 1.45, "minimum": 1.05, "maximum": 3.7},
        "keyframe": (
            "right-facing three-quarter view in one graceful frame of an elegant formal "
            "promenade. Use a measured medium step, elongated posture, softly narrow "
            "footpath, restrained hip movement, relaxed fingers, and small balanced arm "
            "swing. Keep both complete arms, hands, legs, and shoes visible."
        ),
        "video": (
            "Animate an elegant measured promenade from camera-left to camera-right at "
            "92–102 steps per minute. Use fluid medium heel-to-toe steps, composed upright "
            "carriage, restrained hip motion, and small symmetrical contralateral arm "
            "swing. Complete one seamless bilateral gait cycle."
        ),
        "reject": "Reject a bridal glide, frozen feet, curtseying, waving, or exaggerated runway sway.",
    },
    "cartwheel": {
        "label": "Cartwheel",
        "description": "Repeating lateral cartwheels; experimental with tailored wardrobes.",
        "validation": "traversal",
        "loop": {"target": 2.8, "minimum": 1.7, "maximum": 5.2},
        # The keyframe seeds frame 0, so for a traversal style it must BE the loop
        # anchor, not the launch pose. This used to ask for both arms lengthened
        # diagonally overhead, which also contradicted the video contract's "begin
        # standing fully upright, arms lowered". The model obeyed the keyframe, so
        # every clip opened in a pose it never returned to and no window could close.
        "keyframe": (
            "right-facing neutral upright standing rest pose, the resting beat between "
            "two lateral cartwheels traveling to the right. Stand tall and balanced with "
            "both feet flat and together on the floor, both arms hanging relaxed at the "
            "sides, shoulders level, torso quiet, and both complete hands and shoes "
            "visible. This is a still upright stance, not a launch, lunge, windup, or "
            "reach: do not raise the arms overhead and do not stride the feet apart. "
            "Preserve the outfit's coverage and tailoring exactly; do not redesign it as "
            "sportswear or expose underwear."
        ),
        "video": (
            "Animate repeated clean lateral cartwheels traveling from camera-left to "
            "camera-right, each one separated by a clear upright standing beat. Each "
            "cartwheel begins from that upright stance, places each hand on the floor in "
            "sequence, passes through a clear inverted split-leg position, lands one foot "
            "then the other, and rises back to the identical upright stance before the "
            "next one begins. Keep forward travel smooth and evenly paced. Preserve full "
            "wardrobe coverage, both hands, both shoes, and the exact person throughout "
            "the inversion."
        ),
        "reject": (
            "Reject flips, aerials, handspring rebounds, floor sliding, wardrobe exposure, "
            "missing hands or shoes, a finish facing the wrong way, a single cartwheel "
            "followed by standing still, or nonstop tumbling that never returns to an "
            "upright stance."
        ),
    },
}
IDLE_POSE_PRESETS = {
    "back-heel": {
        "label": "High heel touch",
        "validation": "back-heel",
        "prompt": (
            "Use an unmistakable profile-to-three-quarter backward lean. Press the "
            "screen-left shoulder blade and upper back against the wall, fold the arms "
            "calmly, shift the pelvis into frame, and keep one supporting foot planted "
            "forward. One knee lifts to hip height and bends sharply behind the body so "
            "the rear tip of that raised shoe's heel touches the same wall line as the "
            "upper-back contact."
        ),
    },
    "tailored-cross": {
        "label": "Tailored cross",
        "validation": "edge",
        "prompt": (
            "Stand in a polished three-quarter pose with the screen-left shoulder and "
            "upper back lightly against the wall. Keep the torso tall, one hand relaxed "
            "near a trouser pocket and the other resting naturally, with the ankles "
            "crossed and both shoes touching the floor."
        ),
    },
    "pocket-lean": {
        "label": "Pocket lean",
        "validation": "edge",
        "prompt": (
            "Lean the upper back against the screen-left wall in a relaxed three-quarter "
            "stance. Rest both hands naturally in the trouser pockets, project the hips "
            "slightly into frame, and cross the ankles with both shoes on the floor."
        ),
    },
    "heel-up": {
        "label": "Low heel touch",
        "validation": "back-heel",
        "prompt": (
            "Rest the screen-left shoulder and upper back against the wall, keep the "
            "torso poised, and let both arms hang naturally. Plant one foot forward, bend "
            "the wall-side knee behind the body, and bring the rear tip of the raised "
            "shoe's heel back to the same wall line as the upper-back contact."
        ),
    },
    "folded-cross": {
        "label": "Folded cross",
        "validation": "edge",
        "prompt": (
            "Stand upright with the screen-left shoulder and upper back against the wall, "
            "arms folded calmly, hips subtly shifted into frame, and ankles crossed with "
            "both shoe soles resting on the floor."
        ),
    },
    "side-cross": {
        "label": "Side cross",
        "validation": "edge",
        "prompt": (
            "Create a relaxed side lean with the screen-left shoulder and hip touching "
            "the wall. Angle the torso into frame, keep the arms loose along the body, "
            "and extend the legs diagonally with crossed ankles and both feet on the floor."
        ),
    },
}


def _clean(value, maximum=800):
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()[:maximum]


def resolve_walk_style(style_id=None, custom_prompt=""):
    if isinstance(style_id, dict):
        custom_prompt = style_id.get("prompt", custom_prompt)
        style_id = style_id.get("id")
    style_id = _clean(style_id, 40) or DEFAULT_WALK_STYLE
    if style_id == "custom":
        prompt = _clean(custom_prompt, 600)
        if len(prompt) < 12:
            raise ValueError(
                "describe the custom gait in at least 12 characters")
        # The user's text IS the gait; the synthesized fields slot into the
        # standard keyframe and loop templates so only mechanics (in place,
        # first equals final frame, identity, plate) are contract-imposed.
        return {
            "id": "custom",
            "label": "Custom gait",
            "description": "Your own described movement, walked in place.",
            "validation": "free",
            "prompt": prompt,
            "loop": {"target": 1.2, "minimum": 0.85, "maximum": 3.6},
            "keyframe": (
                "one frozen, readable mid-movement frame of this custom "
                f"gait: {prompt}. Keep both complete arms, hands, legs, and "
                "shoes naturally visible and anatomically correct."
            ),
            "loop_video": (
                "Animate the exact selected person performing this custom "
                "gait IN PLACE, as if on an invisible treadmill, with real "
                f"energy and full movement: {prompt}."
            ),
            "reject": (
                "Reject freezing in place instead of performing, leaving the "
                "frame, or drifting into an ordinary generic walk that "
                "ignores the described movement."
            ),
        }
    preset = WALK_STYLE_PRESETS.get(style_id)
    if not preset:
        raise ValueError(f"unknown Horizon Walk style: {style_id}")
    return {"id": style_id, **preset}


def _walk_style_receipt(style):
    style = resolve_walk_style(style)
    receipt = {
        key: style[key]
        for key in ("id", "label", "description", "validation")
    }
    if style.get("prompt"):
        # Custom gaits differ only by their text: without it in the receipt,
        # two different prompts would share one cache signature and one
        # approved-reuse identity.
        receipt["prompt"] = style["prompt"]
    return receipt


def walk_mode(style):
    """How this style's footage is shot and cut.

    Gait styles are generated as authored in-place loops: the keyframe is the
    exact first and final frame, the subject walks on an invisible treadmill,
    and the whole portrait frame budget goes to the subject. Traversal styles
    (cartwheel) still cross the runway, because a lateral pass cannot loop in
    place and the crossing itself is the move.
    """
    style = resolve_walk_style(style)
    return "traversal" if style["validation"] == "traversal" else "loop"


# The walk plate is a traversal runway: the subject starts near the left edge and
# crosses to the right so the source trajectory can be measured. The provider caps
# output at 720p, and 720p means the SHORT side, so a landscape plate only ever
# has 720 rows to spend on the body while a portrait plate has 1088. Portrait
# therefore buys real subject detail, but it pays in runway, so each frame carries
# its own geometry and its own crossing contract rather than sharing constants.
WALK_FRAME_PRESETS = {
    "landscape": {
        "label": "Landscape runway",
        "aspect_ratio": "16:9",
        "width": 1280,
        "height": 720,
        "floor": 679,
        "guides": (320, 640, 960),
        "subject_height": 620,
        "subject_width": 250,
        "start_x": 190,
        "crossing": (15, 85),
    },
    "portrait": {
        "label": "Portrait runway",
        "aspect_ratio": "2:3",
        "width": 720,
        "height": 1088,
        "floor": 1040,
        "guides": (180, 360, 540),
        "subject_height": 940,
        "subject_width": 340,
        "start_x": 190,
        "crossing": (26, 74),
    },
}
# Portrait would spend the provider's 720p short-side budget on the subject
# (940px of body instead of 620px), but measured portrait runs (2026-07-29)
# show Grok walking in place on the narrow runway: net root travel ~19px per
# cycle, trajectory r2 0.47 vs the required 0.72, so no desktop speed can be
# derived and the clip is unusable. Landscape trades subject detail for
# reliable traversal, which is the harder requirement.
DEFAULT_WALK_FRAME = "landscape"


def resolve_walk_frame(frame_id=None):
    if isinstance(frame_id, dict):
        frame_id = frame_id.get("id")
    frame_id = _clean(frame_id, 40) or DEFAULT_WALK_FRAME
    preset = WALK_FRAME_PRESETS.get(frame_id)
    if not preset:
        raise ValueError(f"unknown Horizon Walk frame: {frame_id}")
    return {"id": frame_id, **preset}


def _walk_frame_receipt(frame):
    frame = resolve_walk_frame(frame)
    return {
        key: frame[key]
        for key in ("id", "label", "aspect_ratio", "width", "height")
    }


# ---------------------------------------------------------------- moves
# "Show Me Some Moves": short performance loops at the same level as Horizon
# Walk and Edge Idle. Every move is a free act - performed in place on the
# 9:16 plate, first frame equals last frame - so the whole idle free-act
# pipeline carries it; only the choreography prompt changes.
MOVE_STYLES = {
    "viral": {
        "label": "Viral TikTok",
        "description": "High-energy trend choreography for the camera.",
        "prompt": (
            "Move like a seductive TikTok star captivating a live crowd. "
            "Execute high-energy, trend-driven choreography with crisp "
            "transitions, animated facial expressions, and flirtatious "
            "gestures. Keep perfect rhythm, lock eyes with the audience, and "
            "perform as though you're shooting a viral dance trend. Start "
            "with a powerful opening stance, incorporate a standout "
            "signature move, and finish with a bold, confident pose."),
    },
    "hiphop": {
        "label": "Hip-hop freestyle",
        "description": "Grounded grooves, pops, and freestyle bounce.",
        "prompt": (
            "Freestyle like a confident hip-hop dancer owning a cypher: deep "
            "rhythmic grooves, chest pops, shoulder bounces, sharp arm hits, "
            "and quick in-place footwork. Stay loose and musical, ride one "
            "steady beat, drop in a playful freeze, and finish back in a "
            "relaxed stance with a knowing grin."),
    },
    "kpop": {
        "label": "K-pop point dance",
        "description": "Sharp, camera-ready point choreography.",
        "prompt": (
            "Perform razor-sharp K-pop point choreography like the center "
            "position in a music video: precise synchronized arm points, "
            "clean angles, quick head accents, and a signature point move "
            "aimed straight at the camera. Keep every hit crisp and on the "
            "beat, expressions bright and idol-confident, and end striking "
            "an iconic final pose."),
    },
    "ballet": {
        "label": "Ballet grace",
        "description": "Elegant lines and one gentle turn, in place.",
        "prompt": (
            "Dance like a principal ballerina in a spotlight: rise through "
            "demi-pointe, sweep elegant port de bras, unfold a controlled "
            "arabesque, and turn one gentle pirouette. Keep the lines long, "
            "the carriage regal, and the tempo serene, then settle softly "
            "back to a poised fifth position with a graceful bow of the "
            "head."),
    },
    "salsa": {
        "label": "Salsa heat",
        "description": "Latin rhythm, hip action, and a playful spin.",
        "prompt": (
            "Dance salsa like the star of a Havana club: quick basic steps "
            "in place, rolling hip action, styled arms, flirtatious shoulder "
            "shimmies, and one playful spin. Ride the rhythm with infectious "
            "joy, flash a dazzling smile, and finish with a sassy "
            "hand-on-hip pose."),
    },
}
DEFAULT_MOVE_STYLE = "viral"


def resolve_move_style(style_id=None, custom_prompt=""):
    if isinstance(style_id, dict):
        custom_prompt = style_id.get("prompt", custom_prompt)
        style_id = style_id.get("id")
    style_id = _clean(style_id, 40) or DEFAULT_MOVE_STYLE
    if style_id == "custom":
        prompt = _clean(custom_prompt, 600)
        if len(prompt) < 12:
            raise ValueError("describe the custom move in at least 12 characters")
        return {"id": "custom", "label": "Custom move",
                "description": "Your own described performance.",
                "validation": "free", "prompt": prompt}
    preset = MOVE_STYLES.get(style_id)
    if not preset:
        raise ValueError(f"unknown move style: {style_id}")
    return {"id": style_id, "validation": "free", **preset}


def _move_style_receipt(style):
    style = resolve_move_style(style)
    receipt = {key: style[key]
               for key in ("id", "label", "description", "validation")}
    if style["id"] == "custom":
        # Two custom moves differ only by their text; the receipt is the
        # cache signature and the reuse identity.
        receipt["prompt"] = style["prompt"]
    return receipt


def resolve_idle_pose(pose_id=None, custom_prompt=""):
    if isinstance(pose_id, dict):
        custom_prompt = pose_id.get("prompt", custom_prompt)
        pose_id = pose_id.get("id")
    pose_id = _clean(pose_id, 40) or DEFAULT_IDLE_POSE
    if pose_id == "custom":
        prompt = _clean(custom_prompt, 600)
        if len(prompt) < 12:
            raise ValueError("describe the custom edge act in at least 12 characters")
        # A custom act is FREE: positioned at the screen edge by the window,
        # but the pose and movement are whatever the user describes - dance,
        # stretch, anything - never forced into the wall-lean contract that
        # drowned custom instructions ("dance" came out as a lean, 2026-07-30).
        return {
            "id": "custom",
            "label": "Custom act",
            "validation": "free",
            "prompt": prompt,
        }
    preset = IDLE_POSE_PRESETS.get(pose_id)
    if not preset:
        raise ValueError(f"unknown edge-idle pose: {pose_id}")
    return {"id": pose_id, **preset}


def recorded_motion_settings(avatar_dir):
    """Walk style and idle pose that produced the motion currently shipped.

    Re-cutting an approved take has to reuse the direction that take was
    generated from, otherwise the rebuilt manifest would advertise a pose or
    style the footage never performed.
    """
    manifest = os.path.join(avatar_dir, "motion", "motion.json")
    if not os.path.isfile(manifest):
        return {}
    try:
        with open(manifest, encoding="utf-8") as handle:
            metadata = json.load(handle)
    except (OSError, ValueError):
        return {}
    settings = {}
    walk_style = metadata.get("walk_style")
    if isinstance(walk_style, dict) and walk_style.get("id"):
        settings["walk_style"] = walk_style["id"]
    walk_clip = metadata.get("walk")
    if isinstance(walk_clip, dict):
        # Takes shot before loop mode existed carry no receipt; they are
        # traversal footage by construction.
        settings["walk_mode"] = walk_clip.get("walk_mode") or "traversal"
    walk_frame = metadata.get("walk_frame")
    if isinstance(walk_frame, dict) and walk_frame.get("id"):
        settings["walk_frame"] = walk_frame["id"]
    idle_pose = metadata.get("idle_pose")
    if isinstance(idle_pose, dict) and idle_pose.get("id"):
        settings["idle_pose"] = idle_pose
    return settings


def approved_source(avatar_dir, kind):
    """Archived provider footage that produced the clip currently shipped."""
    path = os.path.join(avatar_dir, "motion", "raw", f"{kind}-source.mp4")
    return path if os.path.isfile(path) else None


def approved_direction_conflict(
        avatar_dir, kinds, walk_style=None, idle_pose=None, walk_frame=None):
    """Why approved footage cannot be re-cut under a different direction.

    The archived takes performed the direction recorded in the manifest. Re-
    cutting them under another style or pose would ship metadata describing
    motion the footage never performed, so the caller has to either keep the
    recorded direction or generate fresh footage. Returns an explanation, or
    None when re-cutting is safe.
    """
    recorded = recorded_motion_settings(avatar_dir)
    if "walk" in kinds and recorded.get("walk_mode"):
        wanted_mode = walk_mode(walk_style)
        if wanted_mode != recorded["walk_mode"]:
            return (
                f"approved walk footage is a {recorded['walk_mode']} take, "
                f"but this style now shoots {wanted_mode} footage; drop "
                "--reuse-approved to generate fresh footage"
            )
    if "walk" in kinds and recorded.get("walk_frame"):
        # The archived clip physically IS landscape or portrait, so a frame
        # switch cannot be honoured by re-cutting; it needs new footage.
        wanted_frame = resolve_walk_frame(walk_frame)["id"]
        if wanted_frame != recorded["walk_frame"]:
            return (
                f"approved walk footage was shot on the "
                f"'{recorded['walk_frame']}' runway, not '{wanted_frame}'; "
                f"drop --reuse-approved to shoot a '{wanted_frame}' walk"
            )
    if "walk" in kinds and recorded.get("walk_style"):
        wanted = resolve_walk_style(walk_style)["id"]
        if wanted != recorded["walk_style"]:
            return (
                f"approved walk footage performs '{recorded['walk_style']}', "
                f"not '{wanted}'; drop --reuse-approved to generate a "
                f"'{wanted}' walk")
    if "idle" in kinds and recorded.get("idle_pose"):
        wanted = resolve_idle_pose(idle_pose)
        current = recorded["idle_pose"]
        if (wanted["id"] != current.get("id")
                or wanted.get("prompt") != current.get("prompt")):
            return (
                f"approved idle footage performs '{current.get('id')}', not "
                f"'{wanted['id']}'; drop --reuse-approved to generate a "
                f"'{wanted['id']}' idle")
    return None


def seed_approved_sources(
        avatar_dir, kinds, pose_reference=None, idle_pose=None,
        walk_style=None, log=None, walk_frame=None):
    """Prime the video cache with footage that was already approved.

    A successful build deletes its own cache, so a later rebuild would ask the
    provider for fresh footage and land a different performance. The takes
    under motion/raw are the ones that actually passed review, so copying them
    back into the cache lets a rebuild re-cut that same performance onto the
    current canvas without spending a generation.
    """
    context = _build_context(
        avatar_dir, pose_reference, idle_pose, walk_style, walk_frame)
    video_dir = os.path.join(context["cache"], "videos")
    os.makedirs(video_dir, mode=0o700, exist_ok=True)
    seeded = []
    for kind in kinds:
        source = approved_source(avatar_dir, kind)
        if not source:
            raise RuntimeError(
                f"no approved {kind} footage to reuse; "
                f"expected motion/raw/{kind}-source.mp4")
        shutil.copy2(source, os.path.join(video_dir, f"{kind}.mp4"))
        seeded.append(kind)
        if log:
            log(f"reusing approved {kind} footage: {os.path.basename(source)}")
    return seeded


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _emit(progress, stage, value, label):
    if progress:
        progress(stage, value, label)


def _walk_keyframe_prompt(outfit, walk_style=None):
    walk_style = resolve_walk_style(walk_style)
    return f"""Edit the references into a full-body side-profile motion keyframe of the exact same adult person.

REFERENCE AUTHORITY — Reference 1 is the generated canonical RIGHT-SIDE full-body plate and is the absolute authority for body proportions, side geometry, hair silhouette, wardrobe, materials, colors, accessories, and footwear. Reference 2 is the canonical HD head and is the absolute authority for facial identity, skull proportions, skin tone, hairline, hairstyle, and apparent age. Do not average the references or invent a new person. Preserve every visible identity and styling detail exactly.

MOVEMENT STYLE — {walk_style['label']}. {walk_style['keyframe']}

COMPOSITION — one person only, complete figure centered on a vertical 2:3 canvas, locked camera at waist height, long lens, generous clean margin, no crop, no props, no text, no furniture, no floor shadow. Shoot against a seamless pure white studio background and floor: bright, even white only, with no cast shadow, gray, scenery, reflections, or colored light spill on the subject. If any garment or shoe is white or near-white, render it one clearly visible tone deeper than the backdrop so the silhouette never blends into it.

Editable wardrobe receipt, subordinate to the visual references: {outfit}"""


def _idle_keyframe_prompt(outfit, has_pose_reference, idle_pose=None):
    idle_pose = resolve_idle_pose(idle_pose)
    if idle_pose["validation"] == "free":
        return f"""Create a full-body performance keyframe of the exact same adult person. Reference 1 is the generated canonical FRONT full-body plate and is the body, wardrobe, and proportion authority. Reference 2 is the canonical HD head and is the facial-identity authority.

IDENTITY AND WARDROBE LOCK — preserve the exact face, apparent age, hair, body proportions, outfit, materials, colors, accessories, and footwear shown by References 1 and 2. Do not average identities, beautify, de-age, redesign, or change clothes.

OPENING POSE — one natural, balanced standing frame that a performance loop can begin and end on: weight settled, both shoes on the floor, arms relaxed and readable. This frame is the resting beat of the following act, so keep it poised rather than mid-move: {idle_pose['prompt']}

COMPOSITION — one person only, full figure centered on a vertical 2:3 canvas with margin for the movement, locked camera, no crop, no props, no text, no furniture, no cast shadow. Shoot against a seamless pure white studio background and floor: bright, even white only, with no cast shadow, gray, scenery, reflections, or colored light spill on the subject. If any garment or shoe is white or near-white, render it one clearly visible tone deeper than the backdrop so the silhouette never blends into it.

Editable wardrobe receipt, subordinate to the visual references: {outfit}"""
    reference_note = (
        "Reference 1 is the generated canonical FRONT full-body plate and is the body, wardrobe, and proportion authority. "
        "Reference 2 is the canonical HD head and is the facial-identity authority. Reference 3 is pose geometry only: "
        "do not copy its person, face, hair, clothes, accessories, or styling."
        if has_pose_reference else
        "Reference 1 is the generated canonical FRONT full-body plate and is the body, wardrobe, and proportion authority. "
        "Reference 2 is the canonical HD head and is the facial-identity authority."
    )
    return f"""Create a full-body edge-idle keyframe of the exact same adult person. {reference_note}

IDENTITY AND WARDROBE LOCK — preserve the exact face, apparent age, hair, body proportions, outfit, materials, colors, accessories, and footwear shown by References 1 and 2. Do not average identities, beautify, de-age, redesign, or change clothes.

POSE GEOMETRY — author the selected canonical LEFT-EDGE pose. The selected direction controls geometry only and never identity, wardrobe, styling, age, or gender: {idle_pose['prompt']} Keep at least one intentional screen-left shoulder, upper-back, or side-body contact visibly pressed to one invisible vertical wall. Preserve every selected arm, hand, hip, leg, foot, and contact arrangement exactly. The body must project naturally into the screen toward camera-right rather than lean against empty air. Never substitute an upright tree pose, ballet balance, floating lean, or unrelated fashion pose. Keep it poised and self-possessed. Both complete shoes and all limbs must remain anatomically correct.

COMPOSITION — one person only, full figure centered on a vertical 2:3 canvas with enough margin for the selected edge pose, locked camera, no crop, no props, no weapons, no garter, no text, no furniture, no cast shadow. Shoot against a seamless pure white studio background and floor: bright, even white only, with no cast shadow, gray, scenery, reflections, or colored light spill on the subject. If any garment or shoe is white or near-white, render it one clearly visible tone deeper than the backdrop so the silhouette never blends into it.

Editable wardrobe receipt, subordinate to the visual references: {outfit}"""


def _loop_walk_video_prompt(walk_style):
    """In-place treadmill loop contract, seeded by the proven first-equals-
    last-frame approach: the authored seam replaces the traversal pipeline's
    loop-window search."""
    # A custom gait may not be a two-footed walk at all (a hop, a shuffle),
    # so the contralateral two-step contract only wraps the preset gaits;
    # a custom act just demands identical repeated cycles of the described
    # movement.
    if walk_style["id"] == "custom":
        cycle_contract = (
            "PRIORITY 0.5 — REPEATED CYCLES: repeat identical cycles of "
            "exactly the described movement continuously so the whole clip "
            "is performing; never pause, stand still, or change speed."
        )
    else:
        cycle_contract = (
            "PRIORITY 0.5 — COMPLETE TWO-STEP GAIT CYCLES: one step is not "
            "a cycle. In every cycle the left foot passes the right foot, "
            "then the right foot passes the left foot, and each hand passes "
            "IN FRONT OF its hip and then BEHIND its hip. Hold one steady "
            "cadence and repeat identical cycles continuously so the whole "
            "clip is walking; never pause, stand still, or change speed."
        )
    return f"""{walk_style['loop_video']}

PRIORITY 0 — SEAMLESS IN-PLACE LOOP: the supplied image is the EXACT first frame and the EXACT final frame. The character stays fixed at the same screen position for the entire clip: no forward travel, no sideways drift, no scale change. Motion eases smoothly away from the supplied starting pose and returns precisely to that identical supplied pose at the end.

{cycle_contract}

PRIORITY 1 — IDENTITY, HAIR, AND WARDROBE: preserve the exact selected person's face, apparent age, body proportions, skin tone, hairline, hairstyle, outfit, materials, colors, accessories, and both complete shoes from the input keyframe in every frame. Never restyle, beautify, de-age, change clothes, change footwear, or invent a different person.

CAMERA AND PLATE: locked camera with constant scale, exposure, and color; no camera motion, zoom, reframing, or cuts. The entire background and floor stay seamless pure white with no scenery, shadows, reflections, text, props, gray, or colored spill; white or near-white wardrobe stays one clearly visible tone deeper than the backdrop. The subject's complete full body and both shoes stay inside the frame at all times.

STYLE-SPECIFIC REJECTIONS — {walk_style['reject']}

GLOBAL REJECTIONS — reject forward travel across the frame, root drift, treadmill speed changes, bounce, camera movement, cuts, body-part disappearance, extra fingers, warped shoes, color flicker, hairstyle drift, identity drift, or wardrobe drift."""


def _walk_video_prompt(walk_style=None, walk_frame=None):
    walk_style = resolve_walk_style(walk_style)
    if walk_mode(walk_style) == "loop":
        return _loop_walk_video_prompt(walk_style)
    walk_frame = resolve_walk_frame(walk_frame)
    enters, exits = walk_frame["crossing"]
    # The loop gate requires two hip-line crossings per arm and per leg, i.e. a full
    # two-step cycle. "One gait cycle" reads as "one step" to the video model, which
    # renders the forward arm swing only and can never close the loop. Say two steps.
    # Traversal styles first had no loop contract at all, so the model performed the
    # move once and stood still; no window could close because the opening pose never
    # returned. Demanding a continuous back-to-back chain then overcorrected into an
    # unbroken tumble with no upright frame anywhere, which also cannot close. The
    # loop needs a repeated ANCHOR pose, so ask for the upright beat explicitly.
    cycle_contract = (
        "\n\nPRIORITY 0 — REPEATING TRAVERSAL LOOP WITH AN UPRIGHT ANCHOR: perform the "
        "move at least twice, separated by a clearly upright standing beat. Begin the "
        "clip standing fully upright, feet together on the floor, arms lowered. Perform "
        "the move once, return to that exact same upright standing beat and hold it "
        "briefly, then perform the identical move again and return to it once more. The "
        "upright beat must be unmistakable and identical every time, because it is the "
        "frame the loop cuts on. Keep every repetition the same speed, the same shape, "
        "and the same facing. Do not tumble continuously without ever standing up, and "
        "do not perform the move once and then stand still for the rest of the clip."
        if walk_style["validation"] == "traversal" else
        "\n\nPRIORITY 0 — COMPLETE TWO-STEP GAIT CYCLE: one step is not a loop. Walk TWO "
        "full steps — the left foot passes the right foot, then the right foot passes the "
        "left foot, and the body returns to the identical starting pose. Both arms must "
        "complete the matching swing: each hand passes IN FRONT OF the hip and then "
        "BEHIND the hip. A clip that ends while the near hand is still in front of the "
        "hip is unusable. Hold one steady cadence so the two steps fill the clip evenly."
    )
    return f"""{walk_style['video']}{cycle_contract}

PRIORITY 1 — IDENTITY, HAIR, AND WARDROBE: preserve the exact selected person's face, apparent age, body proportions, skin tone, hairline, hairstyle, outfit, materials, colors, accessories, and both complete shoes from the input keyframe in every frame. Never restyle, beautify, de-age, change clothes, change footwear, or invent a different person.

CAMERA AND PLATE: the three light-gray vertical registration lines and floor line remain exactly stationary. Camera scale, exposure, and color remain constant. The entire background and floor stay seamless pure white with no scenery, shadows, reflections, text, props, gray, or colored spill; white or near-white wardrobe stays one clearly visible tone deeper than the backdrop. Keep the subject's complete full body and both shoes inside frame while the subject crosses from roughly {enters}% to {exits}% at constant speed.

STYLE-SPECIFIC REJECTIONS — {walk_style['reject']}

GLOBAL REJECTIONS — reject bounce, camera movement, cuts, body-part disappearance, extra fingers, warped shoes, color flicker, hairstyle drift, identity drift, or wardrobe drift."""


def _idle_video_prompt(idle_pose=None):
    idle_pose = resolve_idle_pose(idle_pose)
    if idle_pose["validation"] == "free":
        return f"""Create a seamless character performance loop. The supplied image is the EXACT first frame and the EXACT final frame.

THE ACT — the person performs exactly this, with real energy and full movement: {idle_pose['prompt']}

PRIORITY — SEAMLESS IN-PLACE LOOP: the character stays at the same screen position throughout: no walking away, no sideways travel, no scale change. Motion eases out of the supplied opening pose into the act and returns precisely to that identical supplied pose at the end.

IDENTITY AND WARDROBE — preserve the exact person's face, apparent age, hair, body proportions, outfit, materials, colors, accessories, and both complete shoes from the input keyframe in every frame. Never restyle or invent a different person.

CAMERA AND PLATE — locked camera, constant scale, exposure, and color; no cuts or zoom. The entire background and floor stay seamless pure white with no scenery, shadows, reflections, text, props, gray, or colored spill; white or near-white wardrobe stays one clearly visible tone deeper than the backdrop. The complete body and both shoes stay inside the frame at all times.

Reject: identity drift, wardrobe changes, camera motion, leaving the frame, or freezing in place instead of performing."""
    contact_lock = (
        "Keep the screen-left upper-back contact and the rear tip of the raised shoe's "
        "heel pressed to the same wall on one plumb vertical line. Never let the raised "
        "heel drift forward away from the wall or move the supporting floor foot back "
        "to the wall."
        if idle_pose["validation"] == "back-heel" else
        "Keep every selected screen-left shoulder, upper-back, or side-body wall contact "
        "fixed in place, and keep every floor-contacting shoe planted exactly as shown."
    )
    return f"""Animate a subtle living hold of this exact supported edge pose with a locked camera. Preserve the exact identity, hair, outfit, materials, colors, accessories, arm arrangement, leg arrangement, contact points, and both complete shoes from the input keyframe. The selected pose direction is: {idle_pose['prompt']} Preserve a seamless pure white background and floor throughout every frame, with no gray, scenery, reflections, cast shadow, or colored spill on the subject. {contact_lock} Add only natural breathing, one soft blink, a tiny chin adjustment, and restrained fabric and hair settling. Never straighten away from the wall, become a tree pose, float without support, change which leg bears weight, uncross or cross the legs, change the arm arrangement, walk, talk, move the camera, zoom, cut, add objects, or add text. Begin and end with the exact same silhouette, limb geometry, wall contacts, and floor contacts for a seamless idle loop."""


def _move_keyframe_prompt(outfit, move_style=None):
    # A move IS a custom free act: the idle free-branch contracts (opening
    # pose, loopability, white plate) already say everything a performance
    # keyframe needs, with the choreography text in the driver's seat.
    move_style = resolve_move_style(move_style)
    return _idle_keyframe_prompt(
        outfit, False, {"id": "custom", "prompt": move_style["prompt"]})


def _move_video_prompt(move_style=None):
    move_style = resolve_move_style(move_style)
    return _idle_video_prompt({"id": "custom", "prompt": move_style["prompt"]})


def _image_command(provider, references, output_dir, file_name, prompt):
    route = provider["route"]
    command = [
        ENCONVO, "image_create", "features", route,
        "--prompt", prompt,
        "--reference_images", *references,
        "--output_dir", output_dir,
        "--file_name", file_name,
        "--download",
    ]
    if provider.get("model"):
        command += ["--model", str(provider["model"])]
    if route == "open_ai/create":
        command += [
            "--mode", "edit", "--size", "1024x1536", "--quality", "high",
            "--background", "opaque", "--input_fidelity", "high",
        ]
    elif route == "gemini/create":
        image_size = "1K" if "flash-lite" in str(provider.get("model", "")) else "2K"
        command += ["--mode", "edit", "--aspectRatio", "2:3", "--imageSize", image_size]
    elif route == "x_ai/create":
        command += ["--aspect_ratio", "2:3", "--resolution", "2k"]
    elif route == "kie_ai/create":
        command += ["--mode", "edit", "--aspect_ratio", "2:3", "--resolution", "2k"]
    elif route == "azure/create":
        command += ["--mode", "edit", "--size", "1024x1792"]
    elif route in {"together/create", "straico/create"}:
        command += ["--mode", "edit"]
    return command


def _video_command(
        provider, keyframe, output_dir, file_name, prompt, aspect_ratio=None):
    normalized = provider["name"].replace("-enconvo", "")
    if normalized != "x_ai":
        raise RuntimeError(
            f"EnConvo's selected video provider is not supported for Pet motion yet: {provider['name']}")
    if not aspect_ratio:
        # Both fallbacks are native provider grids; "2:3" is not one, and a
        # non-native request resamples the subject through the model's
        # internal grid (measured as the idle figure drifting wide).
        aspect_ratio = "16:9" if file_name.startswith("walk") else "9:16"
    command = [
        ENCONVO, "video_create", "features", "x_ai/create",
        "--prompt", prompt,
        "--mode", "image-to-video",
        "--image", keyframe,
        "--duration", "6",
        "--aspect_ratio", aspect_ratio,
        # The provider only offers 720p and 480p, and 720p sizes the SHORT side.
        # Subject detail is therefore bought with aspect ratio, not this flag.
        "--resolution", "720p",
        "--output_dir", output_dir,
        "--file_name", file_name,
        "--download",
    ]
    if provider.get("model"):
        command += ["--model", str(provider["model"])]
    return command


def _paths_from_json(value):
    paths = []
    if isinstance(value, dict):
        for child in value.values():
            paths.extend(_paths_from_json(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_paths_from_json(child))
    elif isinstance(value, str) and os.path.isfile(value):
        paths.append(value)
    return paths


def _generated_file(directory, extensions, started, stdout=""):
    try:
        payload = json.loads((stdout or "").strip() or "{}")
        candidates = [path for path in _paths_from_json(payload)
                      if os.path.splitext(path)[1].lower() in extensions]
        if candidates:
            return max(candidates, key=os.path.getmtime)
    except Exception:
        pass
    for _attempt in range(5):
        candidates = []
        for root, _, files in os.walk(directory):
            for name in files:
                path = os.path.join(root, name)
                if (os.path.splitext(name)[1].lower() in extensions
                        and os.path.getmtime(path) >= started - 2
                        and os.path.getsize(path) > 4096):
                    candidates.append(path)
        if candidates:
            return max(candidates, key=os.path.getmtime)
        time.sleep(1)
    detail = (stdout or "").strip()[-1000:]
    raise RuntimeError("the provider returned no downloadable media" +
                       (f": {detail}" if detail else ""))


_TRANSIENT_PROVIDER_MARKERS = (
    "polling failed", "socket disconnected", "tls", "econnreset",
    "connection reset", "network", "temporarily unavailable",
    "bad gateway", "502", "503", "timed out")


def _run(command, output_dir, extensions):
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    for attempt in (1, 2):
        started = time.time()
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1200,
            stdin=subprocess.DEVNULL,
        )
        try:
            if result.returncode:
                detail = (result.stderr or result.stdout
                          or "generation failed").strip()[-1600:]
                raise RuntimeError(detail)
            return _generated_file(output_dir, extensions, started, result.stdout)
        except RuntimeError as error:
            # A dropped socket mid-poll is the provider's network hiccup,
            # not a bad request: one quiet retry before surfacing it.
            message = str(error).lower()
            transient = any(
                marker in message for marker in _TRANSIENT_PROVIDER_MARKERS)
            if attempt == 1 and transient:
                time.sleep(8)
                continue
            raise


def _standard_image(source, destination):
    image = cv2.imread(source, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"could not decode generated image: {os.path.basename(source)}")
    if not cv2.imwrite(destination, image, [cv2.IMWRITE_PNG_COMPRESSION, 5]):
        raise RuntimeError("could not save generated motion keyframe")
    return destination


def _keyframe_person(source, destination, log, label):
    """The keyframe's subject as a tightly-cropped RGBA cutout."""
    source_image = cv2.imread(source, cv2.IMREAD_COLOR)
    if source_image is None:
        raise RuntimeError(f"could not decode the {label} keyframe")
    if _green_screen_purity(source_image) >= 0.62:
        rgba = _chroma_key_frame(source_image)
    else:
        alpha_path = os.path.splitext(destination)[0] + "-alpha.png"
        if not cutout.render(source, alpha_path, log=log, tight=True):
            raise RuntimeError(f"could not alpha-cut the {label} keyframe")
        rgba = cv2.imread(alpha_path, cv2.IMREAD_UNCHANGED)
        os.remove(alpha_path)
    points = cv2.findNonZero((rgba[:, :, 3] > 16).astype(np.uint8))
    if points is None:
        raise RuntimeError(f"{label} keyframe has no person")
    x, y, width, height = cv2.boundingRect(points)
    return rgba[y:y + height, x:x + width]


# The in-place loop spends the provider's 720p short side entirely on the
# subject: a 2:3 portrait plate with the figure at ~86% of frame height,
# leaving margin for arm swing and hair. No registration lines: the loop
# contract keeps the camera and root locked, so there is nothing to measure
# against them, and a pure plate matches the proven reference clip.
LOOP_WALK_PLATE = {
    "width": 720, "height": 1088,
    "subject_height": 940, "subject_width": 560, "floor": 1054,
    "aspect_ratio": "2:3",
}


# The idle i2v runs on the provider's NATIVE 9:16 grid. "2:3" is not one of
# xAI's video aspect ratios, so a 2:3 request is resampled through the model's
# internal grid and the subject drifts wide. Composing the keyframe onto a
# 720x1280 plate and requesting 9:16 keeps request, plate, and output on one
# grid. The figure at ~78% of frame height matches the subject's pixel scale
# under the old 2:3 plate, so the 720x1088 bake canvas never has to shrink it.
IDLE_PLATE = {
    "width": 720, "height": 1280,
    "subject_height": 1000, "subject_width": 620, "floor": 1250,
    "aspect_ratio": "9:16",
}


def _idle_loop_keyframe(source, destination, log):
    person = _keyframe_person(source, destination, log, "idle loop")
    plate = IDLE_PLATE
    scale = min(
        plate["subject_height"] / person.shape[0],
        plate["subject_width"] / person.shape[1],
    )
    person = cv2.resize(
        person,
        (round(person.shape[1] * scale), round(person.shape[0] * scale)),
        interpolation=cv2.INTER_AREA,
    )
    canvas = np.full(
        (plate["height"], plate["width"], 3), (255, 255, 255), dtype=np.uint8)
    left = max(0, (plate["width"] - person.shape[1]) // 2)
    top = max(0, plate["floor"] - person.shape[0])
    region = canvas[top:top + person.shape[0], left:left + person.shape[1]]
    alpha = person[:, :, 3:4].astype(np.float32) / 255
    region[:] = (person[:, :, :3] * alpha + region * (1 - alpha)).astype(np.uint8)
    if not cv2.imwrite(destination, canvas, [cv2.IMWRITE_PNG_COMPRESSION, 5]):
        raise RuntimeError("could not save the idle loop keyframe")
    return destination


def _loop_walk_keyframe(source, destination, log):
    person = _keyframe_person(source, destination, log, "walk loop")
    plate = LOOP_WALK_PLATE
    scale = min(
        plate["subject_height"] / person.shape[0],
        plate["subject_width"] / person.shape[1],
    )
    person = cv2.resize(
        person,
        (round(person.shape[1] * scale), round(person.shape[0] * scale)),
        interpolation=cv2.INTER_AREA,
    )
    canvas = np.full(
        (plate["height"], plate["width"], 3), (255, 255, 255), dtype=np.uint8)
    left = max(0, (plate["width"] - person.shape[1]) // 2)
    top = max(0, plate["floor"] - person.shape[0])
    region = canvas[top:top + person.shape[0], left:left + person.shape[1]]
    alpha = person[:, :, 3:4].astype(np.float32) / 255
    region[:] = (person[:, :, :3] * alpha + region * (1 - alpha)).astype(np.uint8)
    if not cv2.imwrite(destination, canvas, [cv2.IMWRITE_PNG_COMPRESSION, 5]):
        raise RuntimeError("could not save the walk loop keyframe")
    return destination


def _wide_walk_keyframe(source, destination, log, walk_frame=None):
    walk_frame = resolve_walk_frame(walk_frame)
    person = _keyframe_person(source, destination, log, "walk traversal")
    height, width = person.shape[:2]
    scale = min(
        walk_frame["subject_height"] / height,
        walk_frame["subject_width"] / width,
    )
    person = cv2.resize(
        person,
        (round(width * scale), round(height * scale)),
        interpolation=cv2.INTER_AREA,
    )
    plate_width = walk_frame["width"]
    plate_height = walk_frame["height"]
    floor = walk_frame["floor"]
    canvas = np.full(
        (plate_height, plate_width, 3), (255, 255, 255), dtype=np.uint8)
    for panel_x in walk_frame["guides"]:
        cv2.line(canvas, (panel_x, 0), (panel_x, floor - 1), (205, 205, 205), 2)
    cv2.line(canvas, (0, floor), (plate_width, floor), (196, 196, 196), 2)
    # A narrow plate can start the subject far enough left that half the body
    # falls outside the frame, so keep the placement inside the runway.
    left = min(
        max(0, walk_frame["start_x"] - person.shape[1] // 2),
        max(0, plate_width - person.shape[1]),
    )
    top = max(0, floor - 1 - person.shape[0])
    region = canvas[top:top + person.shape[0], left:left + person.shape[1]]
    alpha = person[:, :, 3:4].astype(np.float32) / 255
    region[:] = (person[:, :, :3] * alpha + region * (1 - alpha)).astype(np.uint8)
    if not cv2.imwrite(destination, canvas, [cv2.IMWRITE_PNG_COMPRESSION, 5]):
        raise RuntimeError("could not save the wide walk traversal keyframe")
    return destination


def _generate_keyframes(
        cache, image_provider, body_sources, identity_reference,
        pose_reference, prompts, log, kinds=("walk", "idle")):
    keyframe_dir = os.path.join(cache, "keyframes")
    os.makedirs(keyframe_dir, mode=0o700, exist_ok=True)

    def generate(kind):
        destination = os.path.join(keyframe_dir, f"{kind}.png")
        if os.path.getsize(destination) > 4096 if os.path.isfile(destination) else False:
            return destination
        body_source = (
            body_sources[kind]
            if isinstance(body_sources, dict) else body_sources
        )
        references = [body_source]
        if identity_reference:
            references.append(identity_reference)
        if kind == "idle" and pose_reference:
            references.append(pose_reference)
        output_dir = os.path.join(keyframe_dir, f"{kind}-provider")
        generated = _run(
            _image_command(
                image_provider, references, output_dir, f"{kind}-keyframe", prompts[kind]),
            output_dir,
            {".png", ".jpg", ".jpeg", ".webp"},
        )
        return _standard_image(generated, destination)

    kinds = tuple(dict.fromkeys(kinds))
    log(f"using EnConvo image default: {image_provider['title']} / {image_provider.get('model') or 'provider default'}")
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(kinds))) as executor:
        futures = {kind: executor.submit(generate, kind) for kind in kinds}
        return {kind: future.result() for kind, future in futures.items()}


def _generate_videos(
        cache, video_provider, keyframes, prompts, log,
        kinds=("walk", "idle"), walk_frame=None, walk_style=None):
    walk_frame = resolve_walk_frame(walk_frame)
    loop_walk = (
        walk_mode(walk_style) == "loop" if walk_style is not None else False)
    video_dir = os.path.join(cache, "videos")
    os.makedirs(video_dir, mode=0o700, exist_ok=True)

    def generate(kind):
        destination = os.path.join(video_dir, f"{kind}.mp4")
        if os.path.getsize(destination) > 8192 if os.path.isfile(destination) else False:
            return destination
        output_dir = os.path.join(video_dir, f"{kind}-provider")
        source_keyframe = keyframes[kind]
        aspect_ratio = None
        if kind == "walk" and loop_walk:
            source_keyframe = _loop_walk_keyframe(
                source_keyframe,
                os.path.join(video_dir, "walk-loop-keyframe.png"),
                log,
            )
            aspect_ratio = LOOP_WALK_PLATE["aspect_ratio"]
        elif kind == "walk":
            source_keyframe = _wide_walk_keyframe(
                source_keyframe,
                os.path.join(video_dir, "walk-traversal-keyframe.png"),
                log,
                walk_frame,
            )
            aspect_ratio = walk_frame["aspect_ratio"]
        elif kind in ("idle", "move"):
            source_keyframe = _idle_loop_keyframe(
                source_keyframe,
                os.path.join(video_dir, f"{kind}-loop-keyframe.png"),
                log,
            )
            aspect_ratio = IDLE_PLATE["aspect_ratio"]
        generated = _run(
            _video_command(
                video_provider, source_keyframe, output_dir, f"{kind}-source",
                prompts[kind], aspect_ratio),
            output_dir,
            {".mp4", ".mov", ".webm", ".m4v"},
        )
        shutil.copy2(generated, destination)
        return destination

    kinds = tuple(dict.fromkeys(kinds))
    log(f"using EnConvo video default: {video_provider['title']} / {video_provider.get('model') or 'provider default'}")
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(kinds))) as executor:
        futures = {kind: executor.submit(generate, kind) for kind in kinds}
        return {kind: future.result() for kind, future in futures.items()}


def _decode_video(path, target_fps):
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        raise RuntimeError(f"could not decode generated video: {os.path.basename(path)}")
    source_fps = float(capture.get(cv2.CAP_PROP_FPS)) or 24.0
    frames = []
    source_index = 0
    next_sample = 0.0
    while True:
        available, frame = capture.read()
        if not available:
            break
        if source_index + 0.001 >= next_sample:
            frames.append(frame)
            next_sample += source_fps / target_fps
        source_index += 1
        if len(frames) >= target_fps * 10:
            break
    capture.release()
    if len(frames) < target_fps:
        raise RuntimeError("generated motion clip is too short")
    return frames


def _green_screen_confidence(frame):
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV).astype(np.float32)
    blue = frame[:, :, 0].astype(np.float32)
    green = frame[:, :, 1].astype(np.float32)
    red = frame[:, :, 2].astype(np.float32)
    dominance = green - np.maximum(red, blue)
    candidates = (
        (hsv[:, :, 0] >= 30)
        & (hsv[:, :, 0] <= 95)
        & (hsv[:, :, 1] >= 35)
        & (dominance >= 6)
        & (green >= 28)
    )
    height, width = frame.shape[:2]
    border = np.zeros((height, width), dtype=bool)
    border_height = max(2, round(height * 0.08))
    border_width = max(2, round(width * 0.08))
    border[:border_height] = True
    border[-border_height:] = True
    border[:, :border_width] = True
    border[:, -border_width:] = True
    sample = hsv[:, :, 0][candidates & border]
    center = float(np.median(sample)) if sample.size else 60.0
    hue_distance = np.abs(hsv[:, :, 0] - center)
    hue_distance = np.minimum(hue_distance, 180 - hue_distance)
    hue_score = np.clip((25 - hue_distance) / 15, 0, 1)
    dominance_score = np.clip((dominance - 3) / 35, 0, 1)
    saturation_score = np.clip((hsv[:, :, 1] - 25) / 90, 0, 1)
    brightness_score = np.clip((hsv[:, :, 2] - 18) / 50, 0, 1)
    return hue_score * np.sqrt(dominance_score * saturation_score) * brightness_score


def _green_screen_purity(frame):
    confidence = _green_screen_confidence(frame)
    height, width = frame.shape[:2]
    border = np.zeros((height, width), dtype=bool)
    border_height = max(2, round(height * 0.08))
    border_width = max(2, round(width * 0.08))
    border[:border_height] = True
    border[-border_height:] = True
    border[:, :border_width] = True
    border[:, -border_width:] = True
    return float(np.mean(confidence[border] >= 0.42))


def _is_green_screen(frames):
    if not frames:
        return False
    indices = np.linspace(0, len(frames) - 1, min(7, len(frames))).astype(int)
    purities = [_green_screen_purity(frames[index]) for index in indices]
    return float(np.median(purities)) >= 0.62 and float(np.min(purities)) >= 0.48


def _despill_green(rgba):
    output = rgba.copy()
    color = output[:, :, :3].astype(np.float32)
    alpha = output[:, :, 3].astype(np.float32) / 255
    blue, green, red = color[:, :, 0], color[:, :, 1], color[:, :, 2]
    neutral = np.maximum(red, blue)
    # The contour band is where green physically mixes into the camera pixel,
    # so it gets zero allowance and full-strength correction; opaque interior
    # keeps a small allowance so naturally green-ish wardrobe isn't flattened.
    edge_band = alpha < 250 / 255  # exactly the gate's non-opaque class
    spill = np.maximum(0, green - neutral - np.where(edge_band, 0.0, 2.0))
    correction = spill * np.where(edge_band, 1.0, 0.94)
    green -= correction
    red += correction * 0.12
    blue += correction * 0.12
    color[:, :, 0] = np.clip(blue, 0, 255)
    color[:, :, 1] = np.clip(green, 0, 255)
    color[:, :, 2] = np.clip(red, 0, 255)
    output[:, :, :3] = color.astype(np.uint8)
    return output


def _chroma_key_frame(frame):
    confidence = _green_screen_confidence(frame)
    foreground = (confidence < 0.42).astype(np.uint8)
    foreground = cv2.morphologyEx(
        foreground,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    count, labels, statistics, _centroids = cv2.connectedComponentsWithStats(
        foreground, connectivity=8)
    if count <= 1:
        raise RuntimeError("green-screen key found no foreground subject")
    largest = 1 + int(np.argmax(statistics[1:, cv2.CC_STAT_AREA]))
    subject = (labels == largest).astype(np.uint8) * 255
    # The outermost matte ring is a camera pixel physically blended with the
    # green plate; one erosion drops that ring so the soft edge is fed by
    # subject-only pixels instead of green-contaminated ones.
    subject = cv2.erode(
        subject,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    alpha = cv2.GaussianBlur(subject, (0, 0), 0.75)
    alpha[alpha < 8] = 0
    alpha[alpha > 247] = 255
    rgba = np.dstack((frame, alpha)).astype(np.uint8)
    rgba[:, :, :3][rgba[:, :, 3] == 0] = 0
    return rgba


def _color_fidelity_quality(
        source_frames, processed_frames, check_green_spill=True):
    if not source_frames or len(source_frames) != len(processed_frames):
        return {
            "available": False,
            "valid": False,
            "reason": "source and processed frame counts differ",
        }
    protected_deltas = []
    spill_residuals = []
    spill_pixels = 0
    for source, processed in zip(source_frames, processed_frames):
        shared_opaque = (
            (source[:, :, 3] >= 250) &
            (processed[:, :, 3] >= 250)
        )
        source_color = source[:, :, :3].astype(np.int16)
        source_blue, source_green, source_red = cv2.split(source_color)
        green_spill = (
            source_green > np.maximum(source_red, source_blue) + 2
            if check_green_spill else
            np.zeros(shared_opaque.shape, dtype=bool)
        )
        protected = shared_opaque & ~green_spill
        if np.any(protected):
            frame_delta = np.max(np.abs(
                processed[:, :, :3].astype(np.int16) -
                source_color), axis=2)
            protected_deltas.append(frame_delta[protected])
        corrected = shared_opaque & green_spill
        if np.any(corrected):
            output_color = processed[:, :, :3].astype(np.int16)
            output_blue, output_green, output_red = cv2.split(output_color)
            spill_residuals.append(
                output_green[corrected] -
                np.maximum(output_red[corrected], output_blue[corrected])
            )
            spill_pixels += int(np.sum(corrected))
    if not protected_deltas:
        return {
            "available": False,
            "valid": False,
            "reason": "no protected opaque subject pixels",
        }
    protected_deltas = np.concatenate(protected_deltas)
    residuals = (
        np.concatenate(spill_residuals)
        if spill_residuals else
        np.zeros(1, dtype=np.int16)
    )
    protected_p99 = float(np.percentile(protected_deltas, 99))
    protected_over_twelve = float(np.mean(protected_deltas > 12))
    residual_p99 = float(np.percentile(residuals, 99))
    valid = (
        protected_p99 <= 2 and
        protected_over_twelve <= 0.0005 and
        (not check_green_spill or residual_p99 <= 6)
    )
    return {
        "available": True,
        "valid": valid,
        "reason": (
            "approved original RGB preserved"
            if valid and not check_green_spill else
            "skin and wardrobe RGB protected; green spill neutralised"
            if valid else
            "processing changed protected colors or left green spill"
        ),
        "green_spill_checked": check_green_spill,
        "protected_opaque_pixels": int(protected_deltas.size),
        "corrected_green_pixels": spill_pixels,
        "protected_channel_delta_p99": round(protected_p99, 2),
        "protected_channel_delta_max": int(protected_deltas.max()),
        "protected_pixels_over_12_ratio": round(
            protected_over_twelve, 6),
        "green_residual_p99": round(residual_p99, 2),
    }


def _pose_point(pose, name, minimum_confidence=0.15):
    joint = ((pose or {}).get("joints") or {}).get(name) or {}
    try:
        if float(joint.get("confidence", 0)) < minimum_confidence:
            return None
        return np.array([float(joint["x"]), float(joint["y"])], dtype=np.float64)
    except (KeyError, TypeError, ValueError):
        return None


_POSE_ALIGNMENT_JOINTS = (
    "neck", "root", "left_shoulder", "right_shoulder",
    "left_hip", "right_hip", "left_knee", "right_knee",
    "left_ankle", "right_ankle", "left_wrist", "right_wrist",
)


def _pose_similarity_transform(source_pose, target_pose):
    source_points = []
    target_points = []
    for joint in _POSE_ALIGNMENT_JOINTS:
        source = _pose_point(source_pose, joint, 0.20)
        target = _pose_point(target_pose, joint, 0.20)
        if source is not None and target is not None:
            source_points.append(source)
            target_points.append(target)
    if len(source_points) < 4:
        return None, len(source_points)
    matrix, _inliers = cv2.estimateAffinePartial2D(
        np.asarray(source_points),
        np.asarray(target_points),
        method=cv2.LMEDS,
    )
    return matrix, len(source_points)


def _pose_aligned_color_authority(
        matte_frames, matte_poses, color_frames, color_poses,
        validation_frames=None):
    frame_count = len(matte_frames)
    if not frame_count or any(len(values) != frame_count for values in (
            matte_poses, color_frames, color_poses)):
        raise RuntimeError(
            "matte and approved color-authority frame counts differ")
    if validation_frames is not None and len(validation_frames) != frame_count:
        raise RuntimeError(
            "color-authority validation frame counts differ")
    processed = []
    source_color_frames = []
    alignment_ious = []
    shared_joint_counts = []
    for index, (matte, matte_pose, color, color_pose) in enumerate(zip(
            matte_frames, matte_poses, color_frames, color_poses)):
        if matte.shape[:2] != color.shape[:2]:
            raise RuntimeError(
                f"matte and color-authority dimensions differ on frame {index + 1}")
        matrix, shared_joints = _pose_similarity_transform(
            matte_pose, color_pose)
        if matrix is None:
            raise RuntimeError(
                f"could not align matte to approved source on frame {index + 1}; "
                f"only {shared_joints} shared joints")
        alpha = cv2.warpAffine(
            matte[:, :, 3],
            matrix,
            (color.shape[1], color.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        validation_alpha = None
        if validation_frames is not None:
            validation_alpha = validation_frames[index][:, :, 3]
            support = cv2.dilate(
                validation_alpha,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                iterations=1,
            )
            alpha = np.minimum(alpha, support)
            untrusted_edge = validation_alpha < 246
            alpha[untrusted_edge] = np.minimum(alpha[untrusted_edge], 245)
        source_rgba = np.dstack((color, alpha)).astype(np.uint8)
        if validation_alpha is not None:
            original_edge = (validation_alpha > 8) & (validation_alpha < 246)
            source_rgba[:, :, :3][original_edge] = (
                validation_frames[index][:, :, :3][original_edge])
        source_color_frames.append(source_rgba.copy())
        output = cutout._decontaminate_edges(source_rgba)
        output[:, :, :3][output[:, :, 3] == 0] = 0
        processed.append(output)
        shared_joint_counts.append(shared_joints)
        if validation_frames is not None:
            reference_mask = validation_alpha >= 32
            aligned_mask = alpha >= 32
            intersection = int(np.sum(reference_mask & aligned_mask))
            union = int(np.sum(reference_mask | aligned_mask))
            alignment_ious.append(intersection / max(1, union))
    alignment_quality = {
        "available": bool(alignment_ious),
        "valid": True,
        "reason": "green matte follows approved original source",
        "shared_joints_min": min(shared_joint_counts),
    }
    if alignment_ious:
        values = np.asarray(alignment_ious, dtype=np.float64)
        alignment_quality.update({
            "iou_median": round(float(np.median(values)), 4),
            "iou_p10": round(float(np.percentile(values, 10)), 4),
            "iou_min": round(float(np.min(values)), 4),
        })
        alignment_quality["valid"] = (
            alignment_quality["iou_p10"] >= 0.88 and
            alignment_quality["iou_min"] >= 0.85
        )
        if not alignment_quality["valid"]:
            alignment_quality["reason"] = (
                "green matte does not align with approved original source")
            raise RuntimeError(alignment_quality["reason"])
    color_quality = _color_fidelity_quality(
        source_color_frames, processed, check_green_spill=False)
    color_quality["authority"] = "approved-original-source-rgb"
    return processed, alignment_quality, color_quality


def _pose_height(pose):
    points = [
        _pose_point(pose, name)
        for name in ((pose or {}).get("joints") or {})
    ]
    points = [point for point in points if point is not None]
    if len(points) < 6:
        return None
    height = max(point[1] for point in points) - min(point[1] for point in points)
    return float(height) if height >= 20 else None


def _pose_torso_anchor(pose):
    points = [
        _pose_point(pose, name, 0.20)
        for name in (
            "neck", "root", "left_shoulder", "right_shoulder",
            "left_hip", "right_hip",
        )
    ]
    points = [point for point in points if point is not None]
    return float(np.median([point[0] for point in points])) if len(points) >= 2 else None


def _warp_rgba(image, shift_x, shift_y=0):
    matrix = np.array([[1, 0, shift_x], [0, 1, shift_y]], dtype=np.float32)
    return cv2.warpAffine(
        image,
        matrix,
        (image.shape[1], image.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )


def _restore_temporal_color(current, neighbours, mask):
    if not np.any(mask):
        return
    alphas = [neighbour[:, :, 3].astype(np.float32) / 255 for neighbour in neighbours]
    weight = np.maximum(1e-6, np.sum(alphas, axis=0))
    color = np.zeros_like(current[:, :, :3], dtype=np.float32)
    for neighbour, alpha in zip(neighbours, alphas):
        color += neighbour[:, :, :3].astype(np.float32) * alpha[:, :, None]
    color /= weight[:, :, None]
    usable = mask & (weight > 0.20)
    current[:, :, :3][usable] = np.clip(color[usable], 0, 255).astype(np.uint8)


def _repair_pose_extremities(index, current, segmented, poses, stable_alpha):
    if not poses or not 0 < index < len(segmented) - 1:
        return stable_alpha
    current_pose = poses[index]
    body_height = _pose_height(current_pose)
    if body_height is None:
        return stable_alpha
    radii = {
        "left_wrist": 0.045,
        "right_wrist": 0.045,
        "left_ankle": 0.070,
        "right_ankle": 0.070,
    }
    rows, columns = np.ogrid[:current.shape[0], :current.shape[1]]
    for joint, radius_fraction in radii.items():
        point = _pose_point(current_pose, joint, 0.20)
        if point is None:
            continue
        aligned = []
        for neighbour_index in (index - 1, index + 1):
            neighbour_point = _pose_point(poses[neighbour_index], joint, 0.20)
            if neighbour_point is None:
                aligned = []
                break
            shift = point - neighbour_point
            aligned.append(_warp_rgba(
                segmented[neighbour_index], float(shift[0]), float(shift[1])))
        if len(aligned) != 2:
            continue
        consensus = np.median(np.stack([
            aligned[0][:, :, 3], stable_alpha, aligned[1][:, :, 3],
        ], axis=0), axis=0)
        radius = max(4, round(body_height * radius_fraction))
        region = ((columns - point[0]) ** 2 + (rows - point[1]) ** 2) <= radius ** 2
        recovered = region & (consensus > stable_alpha.astype(np.float32) + 8)
        _restore_temporal_color(current, aligned, recovered)
        stable_alpha[region] = np.maximum(
            stable_alpha[region], consensus[region]).astype(np.uint8)
    return stable_alpha


def _segment_frames(frames, workspace, log):
    source_dir = os.path.join(workspace, "source-frames")
    alpha_dir = os.path.join(workspace, "alpha-frames")
    pose_dir = os.path.join(workspace, "pose-frames")
    os.makedirs(source_dir)
    os.makedirs(alpha_dir)
    os.makedirs(pose_dir)
    sources = []
    green_screen = _is_green_screen(frames)
    # White-plate takes: prefer RVM's temporally-coherent matte with clean
    # predicted foreground colors. Vision still runs per frame regardless -
    # the pose skeleton comes from it - and remains the matte fallback.
    rvm_frames = None if green_screen else _rvm_matte(frames, log)
    for index, frame in enumerate(frames):
        source = os.path.join(source_dir, f"{index:04d}.jpg")
        cv2.imwrite(source, frame, [cv2.IMWRITE_JPEG_QUALITY, 96])
        sources.append(source)

    def segment(index):
        destination = os.path.join(alpha_dir, f"{index:04d}.png")
        pose_destination = os.path.join(pose_dir, f"{index:04d}.json")
        rendered = cutout.render(
            sources[index], destination, log=lambda _message: None, tight=True,
            pose_destination=pose_destination)
        # On a green-screen clip the Vision mask is discarded in favour of the
        # chroma key below, so an empty mask must not fail the build. Vision
        # returns nothing for inverted or non-human subjects (cartwheels,
        # creature avatars) that the chroma key mattes perfectly well. The pose
        # metadata check further down still guards what we actually consume.
        if not rendered and not green_screen and rvm_frames is None:
            raise RuntimeError(f"local person segmentation failed on frame {index + 1}")
        if green_screen:
            image = _chroma_key_frame(frames[index])
        elif rvm_frames is not None:
            image = rvm_frames[index]
        else:
            image = cv2.imread(destination, cv2.IMREAD_UNCHANGED)
        if image is None or image.ndim != 3 or image.shape[2] != 4:
            raise RuntimeError(f"frame {index + 1} did not produce RGBA output")
        try:
            with open(pose_destination) as handle:
                pose = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"frame {index + 1} did not produce body-pose metadata") from error
        return image, pose

    matte_method = (
        "chroma-key-green-screen"
        if green_screen else
        "robust-video-matting"
        if rvm_frames is not None else
        "macos-vision-person-segmentation"
    )
    log(
        f"alpha-cutting {len(frames)} frames with {matte_method}; "
        "tracking body pose with macOS Vision")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(segment, range(len(frames))))
    segmented = [result[0] for result in results]
    poses = [result[1] for result in results]
    if not green_screen and rvm_frames is None:
        # Vision fallback on white-plate takes: color sharpens the coarse
        # semantic boundary BEFORE temporal repair, so the fine matte is
        # deterministic and the included-rim flicker never ships. RVM output
        # needs neither - its matte is temporally coherent and its colors
        # are the model's own clean foreground prediction.
        segmented = [
            _refine_white_matte(frames[index], segmented[index])
            for index in range(len(segmented))
        ]
    repaired = _stabilise_segmented(segmented, poses)
    if green_screen:
        repaired = [
            cutout._decontaminate_edges(_despill_green(frame))
            for frame in repaired
        ]
    elif rvm_frames is None:
        repaired = [cutout._decontaminate_edges(frame) for frame in repaired]
    # RVM frames skip edge decontamination on purpose: repainting the soft
    # contour from core colors would erase the model's own clean foreground
    # prediction, which is the whole point of using it.
    # Green-spill neutralisation is a CHROMA-plate contract: on white-plate
    # takes it would demand naturally greenish pixels (a shadow, a jewel) be
    # "corrected", failing takes that have no green plate at all.
    color_quality = _color_fidelity_quality(
        segmented, repaired, check_green_spill=green_screen)
    return repaired, poses, matte_method, color_quality


def _fill_lower_body_alpha_holes(alpha):
    mask = (alpha >= 24).astype(np.uint8)
    points = cv2.findNonZero(mask)
    if points is None:
        return alpha
    x, y, width, height = cv2.boundingRect(points)
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return alpha
    maximum_area = max(64, float(mask.sum()) * 0.035)
    output = alpha.copy()
    for index, contour in enumerate(contours):
        if hierarchy[0][index][3] < 0:
            continue
        area = cv2.contourArea(contour)
        moments = cv2.moments(contour)
        center_y = moments["m01"] / moments["m00"] if moments["m00"] else 0
        if 4 <= area <= maximum_area and center_y >= y + height * 0.40:
            cv2.drawContours(output, [contour], -1, 255, thickness=cv2.FILLED)
    return output


# ------------------------------------------------------- robust video matting
# RobustVideoMatting (PeterL1n/RobustVideoMatting) is the preferred matte for
# white-plate takes when PyTorch is present: its recurrent memory keeps the
# alpha temporally coherent across the take, and it predicts the CLEAN
# foreground colors, so no plate contamination survives at the contour.
# Measured against the Vision path on a real take: softer true matte edges
# (1.33 vs 1.08 contour softness) at equal temporal stability, ~18fps on
# Apple Silicon. The model is fetched at runtime via torch.hub on the local
# machine only - it is never bundled (the packaged app ships no torch, so it
# falls back to the Vision path there; RVM itself is GPL-3.0 and stays out
# of the distributed payload).
_RVM_STATE = {"loaded": None, "failed": False}


def _rvm_runtime(log=print):
    if _RVM_STATE["failed"]:
        return None
    if _RVM_STATE["loaded"] is not None:
        return _RVM_STATE["loaded"]
    if os.environ.get("VIVIEEN_NO_RVM"):
        _RVM_STATE["failed"] = True
        return None
    try:
        import torch
        model = torch.hub.load(
            "PeterL1n/RobustVideoMatting", "mobilenetv3", trust_repo=True)
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        _RVM_STATE["loaded"] = (torch, model.eval().to(device), device)
        log(f"robust video matting ready on {device}")
    except Exception as error:
        _RVM_STATE["failed"] = True
        log(f"robust video matting unavailable ({error}); using Vision matte")
        return None
    return _RVM_STATE["loaded"]


def _rvm_matte(frames, log=print):
    """The whole take through RVM in order, so the recurrent state carries.

    Returns one RGBA frame per input using the model's own foreground-color
    prediction, or None when the backend is unavailable.
    """
    runtime = _rvm_runtime(log)
    if runtime is None or not frames:
        return None
    torch, model, device = runtime
    try:
        recurrent = [None] * 4
        output = []
        with torch.no_grad():
            for frame in frames:
                source = torch.from_numpy(
                    cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                ).float().div(255).permute(2, 0, 1).unsqueeze(0).to(device)
                foreground, alpha, *recurrent = model(
                    source, *recurrent, downsample_ratio=0.6)
                color = (foreground[0].permute(1, 2, 0).cpu().numpy()
                         * 255).astype(np.uint8)
                matte = (alpha[0, 0].cpu().numpy() * 255).astype(np.uint8)
                rgba = np.dstack(
                    (cv2.cvtColor(color, cv2.COLOR_RGB2BGR), matte))
                rgba[:, :, :3][matte == 0] = 0
                output.append(rgba)
        log(f"robust video matting: {len(output)} frames matted")
        return output
    except Exception as error:
        _RVM_STATE["failed"] = True
        log(f"robust video matting failed mid-take ({error}); using Vision matte")
        return None


def _refine_white_matte(source, rgba):
    """Sharpen Vision's person matte against the KNOWN white plate.

    The semantic mask is right about WHERE the person is but coarse about
    the boundary: it rides a few pixels outside the true silhouette (an
    included white rim, at full alpha, that flickers frame to frame) and it
    drops thin structures like stiletto heel sticks. Within a band around
    the mask, color decides instead: near-white is plate, clearly non-white
    connected to the body is subject. The soft contour is then un-mixed
    against white (C = aF + (1-a)W) so no plate tint survives compositing.
    """
    alpha = rgba[:, :, 3]
    mask = (alpha > 127).astype(np.uint8)
    if not mask.any():
        return rgba
    hsv = cv2.cvtColor(source, cv2.COLOR_BGR2HSV).astype(np.float32)
    whiteness = (np.clip((hsv[:, :, 2] - 205) / 40, 0, 1)
                 * np.clip((70 - hsv[:, :, 1]) / 55, 0, 1))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    core = cv2.erode(mask, kernel, iterations=2).astype(bool)
    reach = cv2.dilate(mask, kernel, iterations=5).astype(bool)
    inside = mask.astype(bool)
    refined = alpha.astype(np.float32)
    # Outside the semantic boundary color decides alone: near-white is
    # plate, however far Vision's coarse edge overshot.
    outer = reach & ~inside
    refined[outer] = np.minimum(refined[outer], (1 - whiteness[outer]) * 255)
    # Plate pockets: PURE-white regions (measured plate 1.0 vs cream shoes
    # <=0.68) connected to the plate outside the figure - white showing
    # through a hair-shoulder gap, a smudge hugging a moving hand - are
    # background even where Vision marked them opaque at full confidence,
    # at any depth inside the mask. The strict threshold is what protects
    # off-white wardrobe; garments that are literally plate-white are
    # unmattable on a white plate and the prompts now forbid them.
    plate = (whiteness > 0.93).astype(np.uint8)
    count, labels = cv2.connectedComponents(plate, connectivity=8)
    exterior_labels = np.unique(labels[(~reach) & (plate > 0)])
    exterior_labels = exterior_labels[exterior_labels > 0]
    if exterior_labels.size:
        pocket = np.isin(labels, exterior_labels)
        refined[pocket] = np.minimum(
            refined[pocket], (1 - whiteness[pocket]) * 255)
    # Clearly non-white pixels near the body are subject (heel sticks,
    # straps, hair wisps) - but only when connected to the body, so plate
    # smudges and shadows far from her stay out.
    solid = reach & ~core & (whiteness < 0.18)
    count, labels = cv2.connectedComponents(
        (solid | core).astype(np.uint8), connectivity=8)
    core_labels = np.unique(labels[core])
    core_labels = core_labels[core_labels > 0]
    if core_labels.size:
        refined[np.isin(labels, core_labels) & solid] = 255
    refined = cv2.GaussianBlur(
        np.clip(refined, 0, 255).astype(np.uint8), (0, 0), 0.6)
    output = rgba.copy()
    output[:, :, 3] = refined
    scale = refined.astype(np.float32)[..., None] / 255
    edge = (refined > 0) & (refined < 250)
    if edge.any():
        colors = output[:, :, :3].astype(np.float32)
        unmixed = np.clip(
            (colors - (1 - scale) * 255) / np.maximum(scale, 0.04), 0, 255)
        output[:, :, :3][edge] = unmixed[edge].astype(np.uint8)
    output[:, :, :3][refined == 0] = 0
    return output


def _stabilise_segmented(segmented, poses=None):
    repaired = []
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    anchors = []
    for index, frame in enumerate(segmented):
        anchor = _pose_torso_anchor(poses[index]) if poses else None
        if anchor is None and poses:
            try:
                anchor = _torso_anchor(frame)
            except RuntimeError:
                anchor = frame.shape[1] / 2
        anchors.append(anchor if anchor is not None else 0.0)
    for index, image in enumerate(segmented):
        current = image.copy()
        current_alpha = image[:, :, 3].astype(np.float32)
        stable_alpha = current_alpha.copy()
        if 0 < index < len(segmented) - 1:
            aligned = [
                _warp_rgba(
                    segmented[neighbour_index],
                    anchors[index] - anchors[neighbour_index],
                )
                for neighbour_index in (index - 1, index + 1)
            ]
            consensus = np.median(np.stack([
                aligned[0][:, :, 3], current_alpha, aligned[1][:, :, 3],
            ], axis=0), axis=0)
            recovered = consensus > current_alpha + 8
            _restore_temporal_color(current, aligned, recovered)
            stable_alpha = np.maximum(stable_alpha, consensus)
        stable_alpha = np.clip(stable_alpha, 0, 255).astype(np.uint8)
        stable_alpha = _repair_pose_extremities(
            index, current, segmented, poses, stable_alpha)
        stable_alpha = cv2.morphologyEx(
            stable_alpha,
            cv2.MORPH_CLOSE,
            close_kernel,
        )
        alpha_before_hole_fill = stable_alpha.copy()
        stable_alpha = _fill_lower_body_alpha_holes(stable_alpha)
        filled_holes = ((alpha_before_hole_fill < 24) & (stable_alpha >= 24)).astype(np.uint8)
        presence = (stable_alpha >= 24).astype(np.uint8)
        interior = cv2.distanceTransform(presence, cv2.DIST_L2, 3) >= 1.5
        stable_alpha[interior] = 255
        stable_alpha[stable_alpha < 16] = 0
        if filled_holes.any():
            current[:, :, :3] = cv2.inpaint(
                current[:, :, :3], filled_holes * 255, 5, cv2.INPAINT_TELEA)
        current[:, :, 3] = stable_alpha
        current[:, :, :3][stable_alpha == 0] = 0
        repaired.append(current)
    return repaired


def _torso_anchor(frame):
    alpha = frame[:, :, 3]
    points = cv2.findNonZero((alpha > 32).astype(np.uint8))
    if points is None:
        raise RuntimeError("walk frame has no person alpha")
    x, y, width, height = cv2.boundingRect(points)
    band = alpha[
        y + round(height * 0.20):y + round(height * 0.60),
        x:x + width,
    ] > 32
    _rows, columns = np.where(band)
    return float(x + (np.median(columns) if columns.size else width / 2))


def _recenter_walk_frames(frames):
    anchors = np.array([_torso_anchor(frame) for frame in frames], dtype=np.float64)
    target = frames[0].shape[1] / 2
    recentered = []
    for frame, anchor in zip(frames, anchors):
        matrix = np.array([[1, 0, target - anchor], [0, 1, 0]], dtype=np.float32)
        recentered.append(cv2.warpAffine(
            frame,
            matrix,
            (frame.shape[1], frame.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0, 0),
        ))
    return recentered, anchors


def _trajectory_profile(anchors, start, end, fps, scale):
    selected = np.asarray(anchors[start:end], dtype=np.float64)
    if selected.size < 8:
        return None
    times = np.arange(selected.size, dtype=np.float64) / max(1, fps)
    slope, intercept = np.polyfit(times, selected, 1)
    predicted = slope * times + intercept
    residual = float(np.sum((selected - predicted) ** 2))
    total = float(np.sum((selected - selected.mean()) ** 2))
    r_squared = 1.0 - residual / max(total, 1e-6)
    cycle_seconds = selected.size / max(1, fps)
    cycle_distance = float(slope * cycle_seconds * scale)
    if slope <= 0 or r_squared < 0.72 or cycle_distance < 12:
        return None

    offsets = (selected - selected[0]) * scale
    if offsets.size > 2:
        smooth = offsets.copy()
        for index in range(1, offsets.size - 1):
            smooth[index] = np.median(offsets[index - 1:index + 2])
        offsets = smooth
    offsets = np.maximum.accumulate(np.maximum(0, offsets))
    target_last = cycle_distance * (selected.size - 1) / selected.size
    if offsets[-1] > 1e-6:
        offsets *= target_last / offsets[-1]
    else:
        offsets = np.linspace(0, target_last, selected.size)
    return {
        "speed_method": "source-root-trajectory",
        "trajectory_r2": round(r_squared, 4),
        "cycle_distance": round(cycle_distance, 2),
        "ground_speed": round(cycle_distance / cycle_seconds, 2),
        "travel_offsets": [round(float(value), 2) for value in offsets],
        "continuous_source_frames": True,
    }


def _inplace_drift(frames, start, end):
    """Horizontal root travel across the selected loop, in source pixels."""
    anchors = [
        _torso_anchor(frame) for frame in frames[start:end]
    ]
    anchors = [anchor for anchor in anchors if anchor is not None]
    if len(anchors) < max(8, (end - start) * 0.8):
        return None
    return float(max(anchors) - min(anchors))


def _inplace_trajectory(poses, start, end, fps, scale):
    """Ground speed of an in-place loop, measured from stance-foot slide.

    On an invisible treadmill the planted foot slides backward under the body
    at exactly the ground speed the walk represents. Per frame the more
    negative of the two ankle velocities (relative to the root) is the stance
    foot's slide; the median of that backward cluster is the speed. Returns
    None when the take reads as marching rather than treadmill walking, so
    the caller can fall back to the stride heuristic.
    """
    series = {}
    for side in ("left", "right"):
        values = []
        for pose in poses[start:end]:
            ankle = _pose_point(pose, f"{side}_ankle")
            root = _pose_point(pose, "root")
            values.append(
                ankle[0] - root[0]
                if ankle is not None and root is not None else np.nan)
        values = np.asarray(values, dtype=np.float64)
        valid = np.isfinite(values)
        if valid.sum() < max(8, math.ceil(len(values) * 0.85)):
            return None
        indices = np.arange(len(values))
        values[~valid] = np.interp(
            indices[~valid], indices[valid], values[valid])
        series[side] = _smooth_signal(values, 3)
    stance_velocity = np.minimum(
        np.diff(series["left"]), np.diff(series["right"]))
    backward = stance_velocity[stance_velocity < 0]
    if backward.size < stance_velocity.size * 0.5:
        return None
    slide = float(-np.median(backward))
    cycle_frames = end - start
    cycle_seconds = cycle_frames / max(1, fps)
    cycle_distance = slide * cycle_frames * scale
    if cycle_distance < 12:
        return None
    offsets = np.linspace(
        0.0, cycle_distance * (cycle_frames - 1) / cycle_frames, cycle_frames)
    return {
        "speed_method": "in-place-stance-slide",
        "stance_slide_px_per_frame": round(slide, 3),
        "cycle_distance": round(cycle_distance, 2),
        "ground_speed": round(cycle_distance / cycle_seconds, 2),
        "travel_offsets": [round(float(value), 2) for value in offsets],
        "continuous_source_frames": True,
    }


def _loop_feature(frame):
    resized = cv2.resize(frame, (48, 72), interpolation=cv2.INTER_AREA).astype(np.float32)
    alpha = resized[:, :, 3:4] / 255.0
    premultiplied = resized[:, :, :3] * alpha / 255.0
    return np.concatenate((alpha, premultiplied), axis=2)


def _thumbnail_closure_shortfall(mask_a, mask_b, upper_minimum=None):
    """How far an endpoint pair falls short of the silhouette closure gate.

    _silhouette_closure_quality later judges the finished loop by alpha
    overlap on the head-and-shoulders band; this is the same measure taken on
    selection thumbnails, so the selector can rank endpoint pairs by the
    criterion the gate will actually enforce. Zero once the pair clears the
    floor - closure stops competing with duration and pose the moment it is
    good enough."""
    if upper_minimum is None:
        upper_minimum = LOOP_CLOSURE_UPPER_MINIMUM
    union = mask_a | mask_b
    rows = np.flatnonzero(union.any(axis=1))
    if not rows.size:
        return 0.0
    top = rows[0]
    cut = top + max(1, (rows[-1] - top + 1) // 3)
    band_union = int((mask_a[top:cut] | mask_b[top:cut]).sum())
    if not band_union:
        return 0.0
    overlap = float((mask_a[top:cut] & mask_b[top:cut]).sum()) / band_union
    return max(0.0, upper_minimum - overlap)


def _normalised_pose_point(pose, name):
    point = _pose_point(pose, name)
    height = _pose_height(pose)
    if point is None or height is None:
        return None
    origin = _pose_point(pose, "root")
    if origin is None:
        hips = [
            _pose_point(pose, side)
            for side in ("left_hip", "right_hip")
        ]
        hips = [hip for hip in hips if hip is not None]
        origin = np.mean(hips, axis=0) if hips else _pose_point(pose, "neck")
    return (point - origin) / height if origin is not None else None


def _pose_signal(poses, start, end, distal, proximal):
    values = []
    for pose in poses[start:end]:
        distal_point = _pose_point(pose, distal)
        proximal_point = _pose_point(pose, proximal)
        height = _pose_height(pose)
        values.append(
            (distal_point[0] - proximal_point[0]) / height
            if distal_point is not None and proximal_point is not None and height else np.nan)
    values = np.asarray(values, dtype=np.float64)
    valid = np.isfinite(values)
    if valid.sum() < max(8, math.ceil(len(values) * 0.90)):
        return None
    missing_runs = np.diff(np.flatnonzero(np.concatenate((
        [True], valid, [True],
    )))) - 1
    if missing_runs.size and int(missing_runs.max()) > 2:
        return None
    indices = np.arange(len(values))
    values[~valid] = np.interp(indices[~valid], indices[valid], values[valid])
    return values


def _wrist_height_metrics(poses, start, end, side):
    values = []
    for pose in poses[start:end]:
        wrist = _pose_point(pose, f"{side}_wrist")
        hip = _pose_point(pose, f"{side}_hip")
        height = _pose_height(pose)
        values.append(
            (hip[1] - wrist[1]) / height
            if wrist is not None and hip is not None and height else np.nan)
    values = np.asarray(values, dtype=np.float64)
    valid = np.isfinite(values)
    if valid.sum() < max(8, math.ceil(len(values) * 0.90)):
        return {
            "available": False,
            "elevation_p90": None,
            "elevation_max": None,
        }
    return {
        "available": True,
        "elevation_p90": round(float(np.percentile(values[valid], 90)), 4),
        "elevation_max": round(float(np.max(values[valid])), 4),
    }


def _foot_lift_metrics(poses, start, end):
    values = []
    for pose in poses[start:end]:
        left = _pose_point(pose, "left_ankle")
        right = _pose_point(pose, "right_ankle")
        height = _pose_height(pose)
        values.append(
            abs(left[1] - right[1]) / height
            if left is not None and right is not None and height else np.nan)
    values = np.asarray(values, dtype=np.float64)
    valid = np.isfinite(values)
    if valid.sum() < max(8, math.ceil(len(values) * 0.90)):
        return {
            "available": False,
            "lift_p90": None,
            "lift_max": None,
        }
    return {
        "available": True,
        "lift_p90": round(float(np.percentile(values[valid], 90)), 4),
        "lift_max": round(float(np.max(values[valid])), 4),
    }


def _zero_crossings(values):
    centered = values - np.median(values)
    signs = np.sign(centered)
    for index in range(1, len(signs)):
        if signs[index] == 0:
            signs[index] = signs[index - 1]
    return int(np.sum(signs[1:] * signs[:-1] < 0))


def _smooth_signal(values, width=5):
    if values is None or len(values) < width:
        return values
    kernel = np.ones(width) / width
    return np.convolve(values, kernel, mode="same")


def _robust_range(values):
    if values is None or not len(values):
        return None
    return float(np.percentile(values, 95) - np.percentile(values, 5))


# A window can show two zero crossings, close at the seam, and still hold only
# half a gait cycle: mid-stance to mid-stance, arms neutral -> forward ->
# neutral, never reaching behind. Measured on a shipped half-cycle
# (2026-07-29): the 26-frame window covered 18-47% of the stride range the
# source actually performed, while its true cycle lasted ~72 frames. The only
# reliable definition of a full cycle is the source's own dominant period plus
# the requirement that the window covers the stride's full travel.
GAIT_PERIOD_MIN_CONFIDENCE = 0.30
GAIT_PERIOD_FIT = 0.85
GAIT_COVERAGE_MINIMUM = 0.60
GAIT_COVERAGE_FLOOR = 0.04


def _source_gait_profile(poses):
    """Dominant gait period and stride ranges of the whole source take.

    The period comes from the autocorrelation of the signed ankle-separation
    signal (one full period per two steps). The near-zero-lag ridge is skipped
    by searching only past the first negative dip, so a slow stately walk is
    not mistaken for a fast one. Returns None when no usable leg signal
    exists; period is None when the take holds fewer than ~2 cycles.
    """
    if not poses or len(poses) < 16:
        return None
    count = len(poses)
    separation = _pose_signal(poses, 0, count, "left_ankle", "right_ankle")
    arm_ranges = {}
    for side in ("left", "right"):
        arm = _pose_signal(
            poses, 0, count, f"{side}_wrist", f"{side}_shoulder")
        arm_ranges[side] = _robust_range(arm)
    if separation is None:
        return None
    period = None
    confidence = None
    smoothed = _smooth_signal(separation)
    centered = smoothed - np.mean(smoothed)
    if float(np.std(centered)) > 1e-6:
        correlation = np.correlate(centered, centered, "full")[count - 1:]
        # Unbiased per-lag normalisation: without it a long period (under two
        # cycles in the take) shrinks with its overlap and slips beneath the
        # confidence floor, which is exactly the slow footage that needs it.
        correlation /= np.maximum(1, count - np.arange(count))
        correlation /= correlation[0]
        negative = np.flatnonzero(correlation < 0)
        search_end = count * 2 // 3
        if negative.size and negative[0] + 1 < search_end:
            search_start = int(negative[0]) + 1
            lag = search_start + int(
                np.argmax(correlation[search_start:search_end]))
            if correlation[lag] >= GAIT_PERIOD_MIN_CONFIDENCE and lag >= 10:
                period = int(lag)
                confidence = round(float(correlation[lag]), 4)
    return {
        "period": period,
        "period_confidence": confidence,
        "leg_range": _robust_range(separation),
        "arm_range": arm_ranges,
    }


def _pose_cycle_metrics(
        poses, start, end, profile="office-gait", source_gait=None):
    profiles = {
        "office-gait": {
            "foot_p90": 0.16, "foot_max": 0.22,
            # Wrist ceiling is a style rule, not an artifact gate. At 0.10/0.16
            # a natural clip whose hand briefly reached navel height (p90 0.131,
            # max 0.135, measured 2026-07-29) was rejected despite passing every
            # other check. Genuine airplane-arm failures measure 0.3+, so 0.14
            # keeps rejecting those while letting a navel-height brush ship.
            "wrist_p90": 0.14, "wrist_max": 0.20,
            "arm_excursion": 0.025,
        },
        "stylized-gait": {
            "foot_p90": 0.24, "foot_max": 0.34,
            "wrist_p90": 0.22, "wrist_max": 0.32,
            "arm_excursion": 0.020,
        },
    }
    limits = profiles.get(profile, profiles["office-gait"])
    unavailable = {
        "available": False,
        "valid": False,
        "reason": "body pose unavailable",
    }
    if not poses or start < 0 or end >= len(poses) or end - start < 8:
        return unavailable
    if source_gait is None:
        source_gait = _source_gait_profile(poses)

    closure_errors = []
    velocity_errors = []
    side_metrics = {}
    for side in ("left", "right"):
        arm = _pose_signal(
            poses, start, end, f"{side}_wrist", f"{side}_shoulder")
        leg = _pose_signal(
            poses, start, end, f"{side}_ankle", f"{side}_hip")
        joint_closures = []
        joint_velocities = []
        for joint in (
                f"{side}_wrist", f"{side}_elbow",
                f"{side}_knee", f"{side}_ankle"):
            first = _normalised_pose_point(poses[start], joint)
            second = _normalised_pose_point(poses[start + 1], joint)
            penultimate = _normalised_pose_point(poses[end - 1], joint)
            endpoint = _normalised_pose_point(poses[end], joint)
            if first is None or endpoint is None:
                continue
            closure = float(np.linalg.norm(first - endpoint))
            joint_closures.append(closure)
            closure_errors.append(closure)
            if second is not None and penultimate is not None:
                velocity = float(np.linalg.norm(
                    (second - first) - (endpoint - penultimate)))
                joint_velocities.append(velocity)
                velocity_errors.append(velocity)
        correlation = None
        if (
                arm is not None and leg is not None and
                np.std(arm) > 0.005 and np.std(leg) > 0.005):
            correlation = float(np.corrcoef(arm, leg)[0, 1])
        arm_coverage = None
        source_arm_range = (source_gait or {}).get("arm_range", {}).get(side)
        if (
                arm is not None and source_arm_range and
                source_arm_range >= GAIT_COVERAGE_FLOOR):
            arm_coverage = round(
                (_robust_range(arm) or 0.0) / source_arm_range, 4)
        wrist_height = _wrist_height_metrics(poses, start, end, side)
        side_metrics[side] = {
            "arm_available": arm is not None,
            "leg_available": leg is not None,
            "wrist_height_available": wrist_height["available"],
            "wrist_elevation_p90": wrist_height["elevation_p90"],
            "wrist_elevation_max": wrist_height["elevation_max"],
            "arm_excursion": round(float(np.ptp(arm)), 4)
            if arm is not None else None,
            "arm_crossings": _zero_crossings(arm)
            if arm is not None else None,
            "arm_coverage": arm_coverage,
            "leg_crossings": _zero_crossings(leg)
            if leg is not None else None,
            "closure_error": round(max(joint_closures), 4)
            if joint_closures else None,
            "velocity_error": round(max(joint_velocities), 4)
            if joint_velocities else None,
            "contralateral_correlation": round(correlation, 4)
            if correlation is not None else None,
        }

    reasons = []
    # Wrist height is a taste rule, not an artifact gate: a hand at navel or
    # rib height loops fine, and the power style's own prompt asks for arm
    # drive the old hard ceiling rejected (every 2026-07-29 power candidate
    # lost windows to "hand rises above the waist"). Taste never vetoes
    # physics - the excess becomes a selection penalty instead, so windows
    # with lower hands still win whenever the footage offers both.
    style_penalty = 0.0
    foot_lift = _foot_lift_metrics(poses, start, end)
    if not foot_lift["available"]:
        reasons.append("foot lift tracking unavailable")
    elif (foot_lift["lift_p90"] > limits["foot_p90"] or
          foot_lift["lift_max"] > limits["foot_max"]):
        reasons.append("swing foot lifts too high")
    gait_period = (source_gait or {}).get("period")
    length = end - start
    cycle_coverage = (
        round(length / gait_period, 4) if gait_period else None)
    if gait_period and length < GAIT_PERIOD_FIT * gait_period:
        reasons.append(
            f"loop window holds {length} frames but one full gait cycle "
            f"takes ~{gait_period}")
    stride_coverage = None
    source_leg_range = (source_gait or {}).get("leg_range")
    if source_leg_range and source_leg_range >= GAIT_COVERAGE_FLOOR:
        separation = _pose_signal(
            poses, start, end, "left_ankle", "right_ankle")
        if separation is not None:
            stride_coverage = round(
                (_robust_range(separation) or 0.0) / source_leg_range, 4)
            if stride_coverage < GAIT_COVERAGE_MINIMUM:
                reasons.append(
                    "loop window covers only part of the source stride")
    # A verified full period can still show a single median crossing when the
    # window starts exactly at one, so the crossing floor relaxes to 1 once
    # the period fit has proven the cycle is complete.
    required_crossings = (
        1 if gait_period and length >= GAIT_PERIOD_FIT * gait_period else 2)
    for side, metrics in side_metrics.items():
        if not metrics["arm_available"]:
            reasons.append(f"{side} arm tracking unavailable")
        elif (
                metrics["arm_excursion"] < limits["arm_excursion"] or
                metrics["arm_crossings"] < required_crossings):
            reasons.append(f"{side} arm swing does not complete")
        elif (
                metrics["arm_coverage"] is not None and
                metrics["arm_coverage"] < GAIT_COVERAGE_MINIMUM):
            reasons.append(
                f"{side} arm swing covers only part of the source swing")
        if metrics["wrist_height_available"]:
            style_penalty += max(
                0.0, metrics["wrist_elevation_p90"] - limits["wrist_p90"])
            style_penalty += max(
                0.0, metrics["wrist_elevation_max"] - limits["wrist_max"])
        if not metrics["leg_available"]:
            reasons.append(f"{side} leg tracking unavailable")
        elif metrics["leg_crossings"] < required_crossings:
            reasons.append(f"{side} leg cycle does not complete")
        if (
                metrics["closure_error"] is None or
                metrics["closure_error"] > 0.16):
            reasons.append(f"{side} limb endpoints do not close")
        if (
                metrics["velocity_error"] is None or
                metrics["velocity_error"] > 0.12):
            reasons.append(f"{side} limb direction changes at the seam")
        correlation = metrics["contralateral_correlation"]
        if correlation is None or correlation > 0.20:
            reasons.append(f"{side} arm and leg are not contralateral")

    arm_excursions = [
        metrics["arm_excursion"] or 0 for metrics in side_metrics.values()]
    arm_crossings = [
        metrics["arm_crossings"] or 0 for metrics in side_metrics.values()]
    leg_crossings = [
        metrics["leg_crossings"] or 0 for metrics in side_metrics.values()]
    correlations = [
        metrics["contralateral_correlation"]
        for metrics in side_metrics.values()
        if metrics["contralateral_correlation"] is not None
    ]
    closure_error = max(closure_errors) if closure_errors else 1.0
    velocity_error = max(velocity_errors) if velocity_errors else 1.0
    contralateral = max(correlations) if correlations else None
    return {
        "available": True,
        "valid": not reasons,
        "reason": (
            "; ".join(reasons)
            if reasons else
            "both sides complete one contralateral gait cycle"
        ),
        "gait_period": gait_period,
        "gait_period_confidence": (source_gait or {}).get(
            "period_confidence"),
        "cycle_coverage": cycle_coverage,
        "stride_coverage": stride_coverage,
        "style_penalty": round(style_penalty, 4),
        "tracked_joints": len(closure_errors),
        "closure_error": round(closure_error, 4),
        "velocity_error": round(velocity_error, 4),
        "arm_excursion": round(min(arm_excursions), 4),
        "arm_crossings": min(arm_crossings),
        "leg_crossings": min(leg_crossings),
        "contralateral_correlation": round(contralateral, 4)
        if contralateral is not None else None,
        "foot_lift_p90": foot_lift["lift_p90"],
        "foot_lift_max": foot_lift["lift_max"],
        "sides": side_metrics,
    }


# A tracked hand or heel counts as missing when it sits further than this many
# probe radii from any opaque pixel, or when its probe disc is emptier than this.
# Measured healthy samples: offset <= 0.16 radii, fill >= 0.16.
# Measured genuine holes:   offset >= 0.55 radii, fill <= 0.088.
EXTREMITY_OFFSET_LIMIT = 0.35
EXTREMITY_FILL_MINIMUM = 0.12


def _extremity_integrity(frames, poses, start, end):
    """Check that every tracked hand and heel is actually drawn.

    This used to compare a per-frame opaque-pixel COUNT against the median count
    across the loop. The probe radius scales with the tracked pose height, so it
    changes frame to frame; whenever pose height collapsed, the sampled disc
    shrank quadratically and a completely solid limb was reported as missing. A
    measured cartwheel was rejected on a frame whose ankle discs were 119/119 and
    121/121 opaque - a 100% solid limb called a disappearing heel.

    Both replacement measures are scale-invariant:

      offset - how far the joint sits from the nearest opaque pixel, in multiples
               of its own probe radius. Across measured clips every healthy sample
               sat within 0.16 radii of the body and every genuine hole was 0.55
               radii or further, so this separates cleanly.
      fill   - how much of the probe disc is opaque, which still catches a hollow
               extremity that happens to sit next to the body.
    """
    unavailable = {
        "available": False,
        "valid": False,
        "reason": "extremity pose unavailable",
    }
    if not frames or not poses or start < 0 or end > min(len(frames), len(poses)):
        return unavailable
    specifications = {
        "left_wrist": (0.045, False),
        "right_wrist": (0.045, False),
        "left_ankle": (0.070, True),
        "right_ankle": (0.070, True),
    }
    samples = {joint: [] for joint in specifications}
    for index in range(start, end):
        pose = poses[index]
        height = _pose_height(pose)
        if height is None:
            continue
        alpha = frames[index][:, :, 3]
        # Distance from every pixel to the nearest opaque pixel, computed once per
        # frame so each joint lookup is O(1).
        background = (alpha < 24).astype(np.uint8)
        if not background.any():
            offsets = np.zeros(alpha.shape, dtype=np.float32)
        elif background.all():
            offsets = np.full(alpha.shape, float(max(alpha.shape)), dtype=np.float32)
        else:
            offsets = cv2.distanceTransform(background, cv2.DIST_L2, 3)
        for joint, (radius_fraction, lower_half) in specifications.items():
            point = _pose_point(pose, joint, 0.20)
            if point is None:
                continue
            radius = max(4, round(height * radius_fraction))
            x0 = max(0, math.floor(point[0] - radius))
            y0 = max(0, math.floor(point[1] - radius))
            x1 = min(alpha.shape[1], math.ceil(point[0] + radius + 1))
            y1 = min(alpha.shape[0], math.ceil(point[1] + radius + 1))
            rows, columns = np.ogrid[y0:y1, x0:x1]
            region = ((columns - point[0]) ** 2 + (rows - point[1]) ** 2) <= radius ** 2
            if lower_half:
                region &= rows >= point[1] - radius * 0.15
            area = int(np.sum(region))
            pixels = int(np.sum((alpha[y0:y1, x0:x1] >= 24) & region))
            column = int(round(point[0]))
            row = int(round(point[1]))
            if 0 <= row < offsets.shape[0] and 0 <= column < offsets.shape[1]:
                offset = float(offsets[row, column])
            else:
                offset = float(max(alpha.shape))
            samples[joint].append({
                "pixels": pixels,
                "fill": pixels / max(1, area),
                "offset_ratio": offset / max(1, radius),
            })

    minimum_tracking = max(4, math.ceil((end - start) * 0.60))
    per_joint = {}
    for joint, values in samples.items():
        if len(values) < minimum_tracking:
            continue
        fills = [value["fill"] for value in values]
        offset_ratios = [value["offset_ratio"] for value in values]
        missing = sum(
            value["offset_ratio"] > EXTREMITY_OFFSET_LIMIT
            or value["fill"] < EXTREMITY_FILL_MINIMUM
            for value in values)
        per_joint[joint] = {
            "tracked_frames": len(values),
            "missing_frames": missing,
            "minimum_fill": round(min(fills), 3),
            "median_fill": round(float(np.median(fills)), 3),
            "maximum_offset_ratio": round(max(offset_ratios), 3),
            "minimum_pixels": min(value["pixels"] for value in values),
        }
    if len(per_joint) < 3:
        return unavailable
    missing_total = sum(value["missing_frames"] for value in per_joint.values())
    valid = missing_total == 0
    return {
        "available": True,
        "valid": valid,
        "reason": "hands and heels remain present"
        if valid else "a hand or heel disappears during the loop",
        "tracked_extremities": len(per_joint),
        "missing_frames": missing_total,
        "offset_limit": EXTREMITY_OFFSET_LIMIT,
        "fill_minimum": EXTREMITY_FILL_MINIMUM,
        "joints": per_joint,
    }


def _select_loop(
        frames, fps, target_seconds, minimum_seconds, maximum_seconds,
        poses=None, require_pose_cycle=False, pose_profile="office-gait",
        return_candidates=False):
    features = [_loop_feature(frame) for frame in frames]
    masks = [feature[:, :, 0] > 0.05 for feature in features]
    minimum = max(8, round(minimum_seconds * fps))
    maximum = min(len(frames) - 1, round(maximum_seconds * fps))
    target = round(target_seconds * fps)
    if maximum <= minimum:
        if require_pose_cycle:
            raise RuntimeError("walk video is too short for a complete gait cycle")
        if return_candidates:
            return frames, 0, len(frames), []
        return frames, 0, len(frames)
    best = None
    best_pose_cycle = None
    pose_available = False
    pool = []
    source_gait = _source_gait_profile(poses) if poses else None
    gait_period = (source_gait or {}).get("period")
    if gait_period:
        # Aim the duration preference at a whole number of gait cycles rather
        # than the style's nominal cadence: when the provider walks slower
        # than asked, the nominal target would drag the window toward a
        # half-cycle sliver that no seam test can distinguish from a loop.
        cycles = max(1, round(minimum / gait_period))
        while cycles * gait_period < minimum:
            cycles += 1
        if cycles * gait_period <= maximum:
            target = cycles * gait_period
    for start in range(0, len(frames) - minimum, 2):
        for length in range(minimum, maximum + 1, 2):
            end = start + length
            if end >= len(frames):
                break
            difference = float(np.mean(np.abs(features[start] - features[end])))
            duration_penalty = abs(length - target) / max(1, target) * 0.055
            quality = (
                _pose_cycle_metrics(
                    poses, start, end, profile=pose_profile,
                    source_gait=source_gait)
                if poses else None
            )
            pose_penalty = 0.0
            if quality and quality["available"]:
                pose_available = True
                pose_penalty = (
                    quality["closure_error"] * 0.08
                    + quality["velocity_error"] * 0.04
                    # Taste preference, demoted from a hard veto: prefer the
                    # window with lower hands, never reject a clip over it.
                    + quality["style_penalty"] * 0.5)
            # Weighted so a below-floor pair loses to any above-floor pair in
            # practice (a 0.03 shortfall costs more than typical differences
            # in the other terms), while ranking still works when every pair
            # is below the floor.
            closure_penalty = 1.5 * _thumbnail_closure_shortfall(
                masks[start], masks[end])
            candidate = (
                difference + duration_penalty + pose_penalty + closure_penalty,
                start, end)
            if best is None or candidate[0] < best[0]:
                best = candidate
            pose_valid = bool(quality and quality["available"] and quality["valid"])
            if pose_valid:
                if best_pose_cycle is None or candidate[0] < best_pose_cycle[0]:
                    best_pose_cycle = candidate
            pool.append((candidate[0], start, end, pose_valid))
    if require_pose_cycle:
        if best_pose_cycle is not None:
            best = best_pose_cycle
        elif not pose_available:
            raise RuntimeError(
                "macOS Vision could not track enough body joints to validate the walk loop")
        elif gait_period and GAIT_PERIOD_FIT * gait_period > maximum:
            raise RuntimeError(
                f"walk cadence is too slow: one full gait cycle takes "
                f"~{gait_period / fps:.1f}s but the loop window allows at "
                f"most {maximum / fps:.1f}s; regenerate it")
        else:
            raise RuntimeError(
                "walk video did not contain a complete arm-and-leg gait cycle; regenerate it")
    if best is None:
        if return_candidates:
            return frames, 0, len(frames), []
        return frames, 0, len(frames)
    if not return_candidates:
        return frames[best[1]:best[2]], best[1], best[2]
    # Ranked distinct endpoint pairs, best first, so the caller can test real
    # quality gates against other cuts of the same footage before rejecting
    # the whole clip. Near-duplicates (both endpoints within 3 frames of a
    # kept pair) would re-test essentially the same loop, so skip them.
    if require_pose_cycle:
        ranked = sorted(
            (item for item in pool if item[3]), key=lambda item: item[0])
    else:
        ranked = sorted(pool, key=lambda item: item[0])
    alternates = []
    for _cost, start, end, _valid in ranked:
        if any(abs(start - s) <= 3 and abs(end - e) <= 3 for s, e in alternates):
            continue
        alternates.append((start, end))
        if len(alternates) >= 8:
            break
    return frames[best[1]:best[2]], best[1], best[2], alternates


def _alpha_union(frames):
    left = min(frame.shape[1] for frame in frames)
    top = min(frame.shape[0] for frame in frames)
    right = 0
    bottom = 0
    found = False
    for frame in frames:
        points = cv2.findNonZero((frame[:, :, 3] > 8).astype(np.uint8))
        if points is None:
            continue
        x, y, width, height = cv2.boundingRect(points)
        left = min(left, x)
        top = min(top, y)
        right = max(right, x + width)
        bottom = max(bottom, y + height)
        found = True
    if not found:
        raise RuntimeError("motion alpha sequence is empty")
    pad_x = round((right - left) * 0.07)
    pad_y = round((bottom - top) * 0.035)
    frame_height, frame_width = frames[0].shape[:2]
    return (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(frame_width, right + pad_x),
        min(frame_height, bottom + pad_y),
    )


def _resize_rgba_premultiplied(image, size):
    alpha = image[:, :, 3].astype(np.float32) / 255
    premultiplied = image[:, :, :3].astype(np.float32) * alpha[:, :, None]
    resized_alpha = cv2.resize(alpha, size, interpolation=cv2.INTER_AREA)
    resized_color = cv2.resize(
        premultiplied, size, interpolation=cv2.INTER_AREA)
    output = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    visible = resized_alpha > 1 / 255
    output[:, :, :3][visible] = np.clip(
        resized_color[visible] / resized_alpha[visible, None], 0, 255,
    ).astype(np.uint8)
    output[:, :, 3] = np.clip(
        np.round(resized_alpha * 255), 0, 255).astype(np.uint8)
    return output


def _normalise_frames(frames, include_scale=False):
    left, top, right, bottom = _alpha_union(frames)
    crop_width = right - left
    crop_height = bottom - top
    # Never enlarge at bake time: an upscale here resamples every provider
    # pixel (and INTER_AREA is a downscale filter - enlarging with it is what
    # made baked frames read softer than the raw mp4 in QuickTime). Capped at
    # 1.0 the subject keeps its native pixels and the display's high-quality
    # filter performs the ONE resample, exactly like a video player would.
    scale = min(TARGET_WIDTH * 0.94 / crop_width,
                TARGET_HEIGHT * 0.97 / crop_height, 1.0)
    output_width = max(1, round(crop_width * scale))
    output_height = max(1, round(crop_height * scale))
    offset_x = (TARGET_WIDTH - output_width) // 2
    offset_y = TARGET_HEIGHT - output_height - round(TARGET_HEIGHT * 0.012)
    normalised = []
    for frame in frames:
        crop = frame[top:bottom, left:right]
        resized = _resize_rgba_premultiplied(
            crop, (output_width, output_height))
        resized[:, :, 3][resized[:, :, 3] < 16] = 0
        resized[:, :, :3][resized[:, :, 3] == 0] = 0
        canvas = np.zeros((TARGET_HEIGHT, TARGET_WIDTH, 4), dtype=np.uint8)
        canvas[offset_y:offset_y + output_height, offset_x:offset_x + output_width] = resized
        normalised.append(canvas)
    points = cv2.findNonZero((np.maximum.reduce(
        [(frame[:, :, 3] > 8).astype(np.uint8) for frame in normalised])))
    bounds = [0, 0, TARGET_WIDTH, TARGET_HEIGHT]
    if points is not None:
        bounds = [int(value) for value in cv2.boundingRect(points)]
    return (normalised, bounds, scale) if include_scale else (normalised, bounds)


def _edge_contact_quality(frames, bounds):
    _left, top, width, height = bounds
    unavailable = {
        "available": False,
        "valid": False,
        "reason": "edge-contact silhouette unavailable",
    }
    if not frames or height < 40 or width < 20:
        return unavailable
    contacts = []
    projections = []
    upper_start = top + round(height * 0.14)
    upper_end = top + round(height * 0.48)
    torso_start = top + round(height * 0.30)
    torso_end = top + round(height * 0.70)
    for frame in frames:
        alpha = frame[:, :, 3]
        _rows, upper_columns = np.where(alpha[upper_start:upper_end, :] > 48)
        _rows, torso_columns = np.where(alpha[torso_start:torso_end, :] > 48)
        if upper_columns.size < 20 or torso_columns.size < 20:
            continue
        contact = float(np.percentile(upper_columns, 1))
        contacts.append(contact)
        projections.append(float(np.percentile(torso_columns, 75) - contact))
    minimum_samples = max(4, math.ceil(len(frames) * 0.70))
    if len(contacts) < minimum_samples:
        return unavailable
    contacts = np.asarray(contacts, dtype=np.float64)
    drift = np.abs(contacts - np.median(contacts))
    drift_p90 = float(np.percentile(drift, 90))
    drift_limit = max(3.0, height * 0.035)
    forward_projection = float(np.median(projections))
    reasons = []
    if drift_p90 > drift_limit:
        reasons.append("body contact drifts away from the desktop edge")
    if forward_projection < height * 0.08:
        reasons.append("body does not project into the screen from the edge")
    return {
        "available": True,
        "valid": not reasons,
        "reason": (
            "upper body maintains a stable desktop-edge contact"
            if not reasons else "; ".join(reasons)
        ),
        "tracked_frames": len(contacts),
        "contact_x": round(float(np.median(contacts)), 2),
        "contact_drift_pixels_p90": round(drift_p90, 2),
        "contact_drift_limit_pixels": round(drift_limit, 2),
        "forward_projection_pixels": round(forward_projection, 2),
    }


def _idle_contact_quality(frames, bounds, validation):
    if validation == "back-heel":
        return _wall_contact_quality(frames, bounds)
    return _edge_contact_quality(frames, bounds)


def _wall_contact_quality(frames, bounds):
    x, y, width, height = bounds
    unavailable = {
        "available": False,
        "valid": False,
        "reason": "wall-contact silhouette unavailable",
    }
    if not frames or height < 40 or width < 20:
        return unavailable
    back_contacts = []
    raised_heel_contacts = []
    forward_projections = []
    upper_start = y + round(height * 0.16)
    upper_end = y + round(height * 0.43)
    torso_start = y + round(height * 0.38)
    torso_end = y + round(height * 0.66)
    raised_heel_start = y + round(height * 0.58)
    raised_heel_end = min(frames[0].shape[0], y + round(height * 0.80))
    for frame in frames:
        alpha = frame[:, :, 3]
        _rows, upper_columns = np.where(alpha[upper_start:upper_end, :] > 48)
        _rows, torso_columns = np.where(alpha[torso_start:torso_end, :] > 48)
        _rows, raised_heel_columns = np.where(
            alpha[raised_heel_start:raised_heel_end, :] > 48)
        if upper_columns.size < 20 or torso_columns.size < 20 or raised_heel_columns.size < 4:
            continue
        back_contact = float(np.percentile(upper_columns, 1))
        raised_heel_contact = float(np.percentile(raised_heel_columns, 1))
        back_contacts.append(back_contact)
        raised_heel_contacts.append(raised_heel_contact)
        forward_projections.append(float(np.median(torso_columns) - back_contact))
    minimum_samples = max(4, math.ceil(len(frames) * 0.70))
    if len(back_contacts) < minimum_samples:
        return unavailable
    alignment = np.abs(
        np.asarray(raised_heel_contacts) - np.asarray(back_contacts))
    alignment_p90 = float(np.percentile(alignment, 90))
    alignment_limit = max(3.0, height * 0.035)
    forward_projection = float(np.median(forward_projections))
    reasons = []
    if alignment_p90 > alignment_limit:
        reasons.append("raised heel is not on the back-contact wall line")
    if forward_projection < height * 0.08:
        reasons.append("pelvis and torso do not project forward from the wall")
    return {
        "available": True,
        "valid": not reasons,
        "reason": (
            "back and raised heel share one vertical wall line"
            if not reasons else "; ".join(reasons)
        ),
        "tracked_frames": len(back_contacts),
        "alignment_pixels_median": round(float(np.median(alignment)), 2),
        "alignment_pixels_p90": round(alignment_p90, 2),
        "alignment_limit_pixels": round(alignment_limit, 2),
        "alignment_ratio_p90": round(alignment_p90 / height, 4),
        "forward_projection_pixels": round(forward_projection, 2),
        "back_contact_x": round(float(np.median(back_contacts)), 2),
        "raised_heel_contact_x": round(float(np.median(raised_heel_contacts)), 2),
    }


def _select_idle_wall_loop(
        frames, poses, fps, target_seconds=3.2,
        minimum_seconds=2.0, maximum_seconds=5.2,
        validation="back-heel"):
    features = [_loop_feature(frame) for frame in frames]
    minimum = max(8, round(minimum_seconds * fps))
    maximum = min(len(frames) - 1, round(maximum_seconds * fps))
    target = round(target_seconds * fps)
    best = None
    for length in range(minimum, maximum + 1, 2):
        for start in range(0, len(frames) - length):
            end = start + length
            normalised, bounds = _normalise_frames(frames[start:end])
            wall_quality = _idle_contact_quality(
                normalised, bounds, validation)
            if not wall_quality["valid"]:
                continue
            if poses:
                extremity_quality = _extremity_integrity(
                    frames, poses, start, end)
                if not extremity_quality["valid"]:
                    continue
            difference = float(np.mean(np.abs(
                features[start] - features[end])))
            duration_penalty = abs(length - target) / max(1, target) * 0.055
            candidate = (
                difference + duration_penalty,
                start,
                end,
                wall_quality,
            )
            if best is None or candidate[0] < best[0]:
                best = candidate
    if best is None:
        requirement = (
            "the back and raised heel on one wall line"
            if validation == "back-heel" else
            "a stable upper-body desktop-edge contact"
        )
        raise RuntimeError(
            f"edge-idle video contains no continuous loop with {requirement}")
    return frames[best[1]:best[2]], best[1], best[2], best[3]


def _edge_anchors(frames, bounds):
    """Per-frame wall-contact columns for the edge idle.

    The runtime pins this column to the window edge, so it must be the TRUE
    silhouette extreme: the old 1st-percentile-of-a-band reading sat ~17px
    inside the real shoulder line (a curved contact holds few pixels at its
    apex) and pushed that sliver through the wall every frame. Full height,
    noise-robust minimum - a column only counts with a few opaque rows - so
    neither shoulder nor raised heel can ever be clipped by the window.
    """
    x, y, width, height = bounds
    left_frames = []
    right_frames = []
    for frame in frames:
        opaque = frame[y:y + height, :, 3] > 32
        solid = np.where(opaque.sum(axis=0) >= 4)[0]
        if solid.size:
            left_frames.append(round(float(solid.min()), 2))
            right_frames.append(round(float(solid.max()), 2))
        else:
            left_frames.append(float(x))
            right_frames.append(float(x + width))
    return {
        "left": round(float(np.median(left_frames)), 2),
        "right": round(float(np.median(right_frames)), 2),
        "left_frames": left_frames,
        "right_frames": right_frames,
    }


def _foot_centers(frame, bounds, band_start):
    x, y, width, height = bounds
    mask = frame[y + round(height * band_start):y + height, :, 3] > 48
    _rows, columns = np.where(mask)
    if columns.size < 30:
        return None
    left, right = np.percentile(columns, [25, 75])
    for _iteration in range(12):
        split = (left + right) / 2
        left_group = columns[columns <= split]
        right_group = columns[columns > split]
        if left_group.size < 8 or right_group.size < 8:
            break
        left = float(np.median(left_group))
        right = float(np.median(right_group))
    return np.array([left, right], dtype=np.float64)


def _stance_calibrated_trajectory(frames, bounds, trajectory):
    offsets = np.asarray(trajectory["travel_offsets"], dtype=np.float64)
    cycle_distance = float(trajectory["cycle_distance"])
    base_deltas = np.diff(np.r_[offsets, cycle_distance])
    candidates = []
    for band_start in (0.68, 0.72, 0.76):
        feet = [_foot_centers(frame, bounds, band_start) for frame in frames]
        if any(value is None for value in feet):
            continue
        best = None
        for scale in np.linspace(0.65, 2.2, 156):
            errors = []
            for index, delta in enumerate(base_deltas):
                current = feet[index]
                following = feet[(index + 1) % len(feet)]
                errors.append(min(
                    abs(float(next_x - current_x + delta * scale))
                    for current_x in current for next_x in following
                ))
            score = (float(np.median(errors)), float(np.mean(np.square(errors))))
            if best is None or score < best[0]:
                best = (score, float(scale), errors)
        if best:
            candidates.append(best)
    if not candidates:
        return trajectory
    scale = float(np.median([candidate[1] for candidate in candidates]))
    calibrated = dict(trajectory)
    calibrated["speed_method"] = "stance-foot-calibrated-source-trajectory"
    calibrated["stance_scale"] = round(scale, 3)
    calibrated["stance_slip_pixels"] = round(float(np.median([
        error for _score, _scale, errors in candidates for error in errors
    ])), 3)
    calibrated["cycle_distance"] = round(cycle_distance * scale, 2)
    calibrated["ground_speed"] = round(float(trajectory["ground_speed"]) * scale, 2)
    calibrated["travel_offsets"] = [round(float(value * scale), 2) for value in offsets]
    return calibrated


def _gait_metrics(frames, fps, bounds, trajectory=None):
    x, y, width, height = bounds
    spans = []
    lower_y = min(TARGET_HEIGHT - 1, y + round(height * 0.62))
    upper_y = min(TARGET_HEIGHT, y + height)
    for frame in frames:
        mask = frame[lower_y:upper_y, :, 3] > 48
        _rows, columns = np.where(mask)
        if columns.size < 40:
            continue
        left = float(np.percentile(columns, 25))
        right = float(np.percentile(columns, 75))
        for _iteration in range(8):
            split = (left + right) / 2
            left_group = columns[columns <= split]
            right_group = columns[columns > split]
            if left_group.size < 12 or right_group.size < 12:
                break
            left = float(np.median(left_group))
            right = float(np.median(right_group))
        separation = right - left
        if separation > max(10, width * 0.12):
            spans.append(separation)
    measured_step = float(np.percentile(spans, 85)) if spans else width * 0.28
    cycle_seconds = len(frames) / max(1, fps)
    if trajectory:
        stride_pixels = float(trajectory["cycle_distance"])
        ground_speed = float(trajectory["ground_speed"])
    else:
        stride_pixels = float(np.clip(
            measured_step * 2,
            height * 0.34,
            height * 0.58,
        ))
        ground_speed = stride_pixels / max(0.1, cycle_seconds)
    return {
        "cycle_seconds": round(cycle_seconds, 4),
        "step_span_pixels": round(measured_step, 2),
        "stride_pixels": round(stride_pixels, 2),
        "ground_speed": round(ground_speed, 2),
        **(trajectory or {}),
    }


def _pack_sheets(frames, destination, kind):
    sheets = []
    for sheet_index, start in enumerate(range(0, len(frames), MAX_SHEET_FRAMES)):
        batch = frames[start:start + MAX_SHEET_FRAMES]
        columns = min(8, len(batch))
        rows = math.ceil(len(batch) / columns)
        atlas = np.zeros((rows * TARGET_HEIGHT, columns * TARGET_WIDTH, 4), dtype=np.uint8)
        for local_index, frame in enumerate(batch):
            column = local_index % columns
            row = local_index // columns
            atlas[
                row * TARGET_HEIGHT:(row + 1) * TARGET_HEIGHT,
                column * TARGET_WIDTH:(column + 1) * TARGET_WIDTH,
            ] = frame
        name = f"{kind}-{sheet_index}.png"
        cv2.imwrite(os.path.join(destination, name), atlas, [cv2.IMWRITE_PNG_COMPRESSION, 9])
        sheets.append({
            "image": name,
            "first": start,
            "count": len(batch),
            "columns": columns,
            "rows": rows,
        })
    return sheets


def _encode_alpha_preview(frames, fps, destination):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        encoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL)
        if "prores_ks" not in encoders.stdout:
            return None
        with tempfile.TemporaryDirectory(prefix=".alpha-video-") as directory:
            for index, frame in enumerate(frames):
                cv2.imwrite(os.path.join(directory, f"{index:04d}.png"), frame)
            result = subprocess.run([
                ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps),
                "-i", os.path.join(directory, "%04d.png"),
                "-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le",
                "-an", destination,
            ], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return destination if result.returncode == 0 and os.path.getsize(destination) > 8192 else None
    except Exception:
        return None


def _encode_alpha_stream(frames, fps, destination):
    """VP9-with-alpha WebM: the runtime plays this instead of decoding a
    ~20MB PNG atlas into ~120MB of RAM - Chromium decodes it on the GPU.
    Best-effort: a build whose ffmpeg lacks libvpx-vp9 (the packaged
    minimal ffmpeg does) simply ships the atlas fallback."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        encoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL)
        if "libvpx-vp9" not in encoders.stdout:
            return None
        with tempfile.TemporaryDirectory(prefix=".alpha-stream-") as directory:
            for index, frame in enumerate(frames):
                cv2.imwrite(os.path.join(directory, f"{index:04d}.png"), frame)
            result = subprocess.run([
                ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps),
                "-i", os.path.join(directory, "%04d.png"),
                "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-b:v", "0", "-crf", "24", "-row-mt", "1",
                "-an", destination,
            ], capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        return destination if result.returncode == 0 and os.path.getsize(destination) > 8192 else None
    except Exception:
        return None


# Measured on the alpha channel of normalised loop frames. Whole-body overlap
# alone cannot separate the two failure modes: a drifting idle that never stops
# lowering its head scores 0.827, which is HIGHER than the approved production
# walk at 0.794, because swinging legs legitimately move more than a slipping
# head. Upper-third overlap does separate them, since a walk keeps its head and
# shoulders steady while its legs cycle:
#
#   good  production walk 0.941 | production idle 0.938
#         creature idle   0.974 | creature idle   0.963
#   bad   drifting idle   0.884 | one-shot cartwheel 0.000
#
# So the body floor only guards against gross failure and the upper-body floor
# carries the real pose-drift decision.
LOOP_CLOSURE_BODY_MINIMUM = 0.60
LOOP_CLOSURE_UPPER_MINIMUM = 0.91


def _silhouette_closure_quality(
        frames,
        body_minimum=LOOP_CLOSURE_BODY_MINIMUM,
        upper_minimum=LOOP_CLOSURE_UPPER_MINIMUM):
    """Reject a loop whose last frame never returns to its opening silhouette.

    Contact, extremity and trajectory gates all pass happily on a clip that
    performs an action once and stops, because every individual frame is fine.
    Only comparing the two endpoints catches the pop that such a clip produces
    on every repeat.
    """
    if len(frames) < 2:
        return {
            "available": False, "valid": False,
            "reason": "loop is too short to compare endpoints",
        }

    def mask(frame):
        if frame.ndim != 3 or frame.shape[2] != 4:
            return None
        return (frame[:, :, 3] > 12).astype(np.uint8)

    first, last = mask(frames[0]), mask(frames[-1])
    if first is None or last is None:
        return {
            "available": False, "valid": False,
            "reason": "loop frames carry no alpha channel to compare",
        }

    def overlap(a, b):
        union = int((a | b).sum())
        return None if not union else float((a & b).sum()) / union

    body = overlap(first, last)
    if body is None:
        return {
            "available": False, "valid": False,
            "reason": "loop endpoints are empty",
        }
    # Frames arrive normalised onto a common canvas, so a fixed upper third is
    # a stable stand-in for head and shoulders across avatars.
    cut = max(1, first.shape[0] // 3)
    upper = overlap(first[:cut], last[:cut])
    # An empty band at BOTH endpoints is agreement, not drift. A loop cut at an
    # inverted moment - mid-cartwheel, say - legitimately leaves the head band
    # empty in both frames, and coercing that to 0.0 scored perfect agreement as
    # total failure. Only a band occupied at one endpoint and not the other is
    # real drift, and that case still scores 0.0 through a non-empty union.
    upper_available = upper is not None

    body_ok = body >= body_minimum
    upper_ok = not upper_available or upper >= upper_minimum
    if body_ok and upper_ok:
        reason = (
            "loop returns to its opening silhouette" if upper_available else
            "loop returns to its opening silhouette; "
            "the head band is empty at both endpoints")
    elif not body_ok:
        reason = (
            f"loop ends on a different pose than it started "
            f"({body:.2f} body overlap, needs {body_minimum:.2f})")
    else:
        reason = (
            f"head and shoulders drift across the loop "
            f"({upper:.2f} upper-body overlap, needs {upper_minimum:.2f})")
    return {
        "available": True,
        "valid": body_ok and upper_ok,
        "body_overlap": round(body, 4),
        "upper_overlap": None if upper is None else round(upper, 4),
        "upper_available": upper_available,
        "body_minimum": body_minimum,
        "upper_minimum": upper_minimum,
        "reason": reason,
    }


def _process_clip(
        kind, video, fps, stage, log, idle_validation="back-heel",
        walk_style=None):
    walk_style = resolve_walk_style(walk_style) if kind == "walk" else None
    frames = _decode_video(video, fps)
    with tempfile.TemporaryDirectory(prefix=f".{kind}-frames-", dir=stage) as workspace:
        alpha_frames, poses, matte_method, color_quality = _segment_frames(
            frames, workspace, log)
    if matte_method == "chroma-key-green-screen":
        if not color_quality["available"]:
            raise RuntimeError(
                "could not compare opaque subject colors before and after chroma keying")
        if not color_quality["valid"]:
            raise RuntimeError(
                "chroma key changed opaque subject colors; reject this clip")
    anchors = None
    pose_quality = None
    relaxed = False
    if kind == "walk":
        mode = walk_mode(walk_style)
        relaxed = RELAXED_LOOP_SHIPPING and mode == "loop"
        if mode == "loop":
            # Authored in-place loop: the footage never travels, so there is
            # no root motion to remove and the whole frame sequence is
            # candidate loop material as shot.
            recentered = alpha_frames
        else:
            recentered, anchors = _recenter_walk_frames(alpha_frames)
        loop = walk_style["loop"]
        gait_validation = walk_style["validation"] != "traversal" and not relaxed
        if relaxed:
            # First half of the take, no loop search: the mid-take cut will
            # snap against frame 0, and that is the accepted price for a
            # generation that always ships. Metrics below become receipts.
            half = max(8, len(recentered) // 2)
            selected, loop_start, loop_end = recentered[:half], 0, half
            loop_alternates = []
            log(f"relaxed loop shipping: first {half} frames, gates observed only")
        else:
            selected, loop_start, loop_end, loop_alternates = _select_loop(
                recentered, fps, loop["target"], loop["minimum"], loop["maximum"],
                poses=poses if gait_validation else None,
                require_pose_cycle=gait_validation,
                pose_profile=walk_style["validation"],
                return_candidates=True,
            )
        if mode == "loop":
            drift = _inplace_drift(alpha_frames, loop_start, loop_end)
            if drift is None and not relaxed:
                raise RuntimeError(
                    "could not track the walk loop's root position")
            drift_limit = alpha_frames[0].shape[1] * 0.08
            if drift is not None and drift > drift_limit:
                if relaxed:
                    log(f"observed: walk drifts {round(drift)}px "
                        f"(limit {round(drift_limit)}px); shipping anyway")
                else:
                    raise RuntimeError(
                        f"walk loop drifts {round(drift)}px across the frame "
                        f"(limit {round(drift_limit)}px); the character must "
                        "walk in place; regenerate it")
        # The provider only gets MAX_CANDIDATE_ATTEMPTS videos, so before a
        # near-miss can reject this footage, test the other well-ranked cuts
        # of the same clip against the real gates - a slightly different
        # endpoint pair often closes cleanly. If nothing passes, fall through
        # with the original selection so the shared gates below report their
        # usual detailed rejection.
        for start, end in loop_alternates:
            extremity = _extremity_integrity(alpha_frames, poses, start, end)
            if not (extremity["available"] and extremity["valid"]):
                continue
            closure = _silhouette_closure_quality(
                _normalise_frames(recentered[start:end])[0])
            if not closure["valid"]:
                continue
            if (start, end) != (loop_start, loop_end):
                log(
                    f"walk loop rescued by alternate endpoints {start}:{end} "
                    f"({closure['upper_overlap']} upper-body overlap)")
            selected, loop_start, loop_end = recentered[start:end], start, end
            break
        if relaxed:
            observed = _pose_cycle_metrics(
                poses, loop_start, loop_end,
                profile=walk_style["validation"])
            pose_quality = {
                **observed,
                "valid": True,
                "reason": "relaxed loop shipping: gates observed, not enforced",
                "observed_reason": observed.get("reason"),
                "observed_valid": observed.get("valid"),
            }
        elif gait_validation:
            pose_quality = _pose_cycle_metrics(
                poses, loop_start, loop_end,
                profile=walk_style["validation"])
        else:
            pose_quality = {
                "available": True,
                "valid": True,
                "reason": "traversal style skips gait-cycle constraints; "
                          "loop closure is checked separately",
            }
    elif RELAXED_LOOP_SHIPPING:
        # The idle's authored first-equals-last frame IS the loop seam, so
        # the whole raw take ships uncut at full length.
        relaxed = True
        selected, loop_start, loop_end = alpha_frames, 0, len(alpha_frames)
        log(f"relaxed loop shipping: full {len(alpha_frames)}-frame idle take")
    else:
        selected, loop_start, loop_end = _select_loop(
            alpha_frames, fps, 3.2, 2.0, 5.2)
        probe_frames, probe_bounds = _normalise_frames(selected)
        probe_quality = _idle_contact_quality(
            probe_frames, probe_bounds, idle_validation)
        if not probe_quality["valid"]:
            selected, loop_start, loop_end, probe_quality = _select_idle_wall_loop(
                alpha_frames, poses, fps, validation=idle_validation)
            quality_detail = (
                f"heel alignment p90 {probe_quality['alignment_pixels_p90']}px"
                if idle_validation == "back-heel" else
                f"contact drift p90 {probe_quality['contact_drift_pixels_p90']}px"
            )
            log(
                f"selected strict wall-contact idle loop {loop_start}:{loop_end}; "
                f"{quality_detail}")
    extremity_quality = _extremity_integrity(
        alpha_frames, poses, loop_start, loop_end)
    if not extremity_quality["available"] and not relaxed:
        raise RuntimeError(
            f"macOS Vision could not track enough hands and heels to validate the {kind} clip")
    if not extremity_quality["valid"]:
        if relaxed:
            log(f"observed: {extremity_quality.get('reason') or 'extremity issue'}; shipping anyway")
        else:
            raise RuntimeError(
                f"{kind} clip contains a disappearing hand or heel; regenerate it")
    normalised, bounds, scale = _normalise_frames(selected, include_scale=True)
    wall_contact_quality = None
    if kind == "idle":
        wall_contact_quality = _idle_contact_quality(
            normalised, bounds, idle_validation)
        if not wall_contact_quality["available"] and not relaxed:
            raise RuntimeError("could not measure the edge-idle wall contact")
        if wall_contact_quality["available"] and not wall_contact_quality["valid"]:
            detail = wall_contact_quality.get("reason") or "wall contact is unstable"
            if relaxed:
                log(f"observed: {detail}; shipping anyway")
            else:
                raise RuntimeError(f"edge-idle pose failed contact validation: {detail}")
    closure_quality = _silhouette_closure_quality(normalised)
    if not closure_quality["valid"]:
        if relaxed:
            log(f"observed: {kind} seam {closure_quality.get('reason') or 'does not close'}; shipping anyway")
        else:
            raise RuntimeError(
                f"{kind} loop does not close: {closure_quality['reason']}")
    sheets = _pack_sheets(normalised, stage, kind)
    poster = f"{kind}-poster.png"
    cv2.imwrite(os.path.join(stage, poster), normalised[0], [cv2.IMWRITE_PNG_COMPRESSION, 9])
    alpha_name = f"{kind}-alpha.mov"
    alpha_path = _encode_alpha_preview(normalised, fps, os.path.join(stage, alpha_name))
    alpha_stream_name = None
    if kind in ("idle", "move"):
        # Idle and moves run free (no window-position phase lock like the
        # walk), so they ship as GPU-decoded alpha video, not a PNG atlas.
        candidate = f"{kind}-alpha.webm"
        if _encode_alpha_stream(
                normalised, fps, os.path.join(stage, candidate)):
            alpha_stream_name = candidate
    if kind == "walk":
        if walk_mode(walk_style) == "loop":
            trajectory = _inplace_trajectory(
                poses, loop_start, loop_end, fps, scale)
            if trajectory:
                log(
                    "walk speed from stance-foot slide: "
                    f"{trajectory['ground_speed']}px/s")
            else:
                log("stance slide unreadable; using stride-heuristic walk speed")
        else:
            trajectory = _trajectory_profile(
                anchors, loop_start, loop_end, fps, scale)
            if not trajectory:
                raise RuntimeError(
                    "walk video did not contain a steady left-to-right root trajectory; regenerate it rather than estimating desktop speed")
            if walk_style["validation"] != "traversal":
                trajectory = _stance_calibrated_trajectory(
                    normalised, bounds, trajectory)
        metrics = _gait_metrics(normalised, fps, bounds, trajectory)
        metrics["walk_mode"] = walk_mode(walk_style)
        metrics["walk_style"] = _walk_style_receipt(walk_style)
        metrics["pose_quality"] = pose_quality
        metrics["loop_closure_quality"] = closure_quality
        metrics["extremity_quality"] = extremity_quality
        metrics["color_fidelity_quality"] = color_quality
    else:
        metrics = {
            "edge_anchors": _edge_anchors(normalised, bounds),
            # A free act stands at the docked window rather than pressing a
            # wall: the renderer centers it instead of pinning the silhouette
            # to the screen edge, and never mirrors the performance.
            "anchor_mode": "free" if idle_validation == "free" else "wall",
            "wall_contact_quality": wall_contact_quality,
            "loop_closure_quality": closure_quality,
            "extremity_quality": extremity_quality,
            "color_fidelity_quality": color_quality,
        }
    return {
        "fps": fps,
        "frames": len(normalised),
        "frame_width": TARGET_WIDTH,
        "frame_height": TARGET_HEIGHT,
        "bounds": bounds,
        "sheets": sheets,
        "poster": poster,
        "alpha_video": alpha_name if alpha_path else None,
        "alpha_stream": alpha_stream_name,
        "source_loop": [int(loop_start), int(loop_end)],
        "continuous_source_frames": True,
        "matte_method": matte_method,
        **metrics,
    }


def _body_view_source(body_dir, body_manifest, view):
    view_record = ((body_manifest.get("views") or {}).get(view) or {})
    reference = view_record.get("source")
    if reference:
        candidate = os.path.join(body_dir, os.path.basename(str(reference)))
        if os.path.isfile(candidate):
            return candidate
    prefixes = [f"source-{view}."]
    if view == "front":
        prefixes.append("source.")
    for prefix in prefixes:
        candidate = next((
            os.path.join(body_dir, name)
            for name in sorted(os.listdir(body_dir))
            if name.startswith(prefix) and os.path.isfile(os.path.join(body_dir, name))
        ), None)
        if candidate:
            return candidate
    return None


def _build_context(
        avatar_dir, pose_reference, idle_pose=None, walk_style=None,
        walk_frame=None, move_style=None):
    idle_pose = resolve_idle_pose(idle_pose)
    walk_style = resolve_walk_style(walk_style)
    walk_frame = resolve_walk_frame(walk_frame)
    move_style = resolve_move_style(move_style)
    body_dir = os.path.join(avatar_dir, "body")
    body_manifest_path = os.path.join(body_dir, "body.json")
    if not os.path.isfile(body_manifest_path):
        raise RuntimeError("generate a full body before creating Pet motion")
    with open(body_manifest_path) as handle:
        body_manifest = json.load(handle)
    front_source = _body_view_source(body_dir, body_manifest, "front")
    side_source = _body_view_source(body_dir, body_manifest, "side") or front_source
    if not front_source:
        raise RuntimeError("the generated front full-body source is missing")
    identity_reference = body._identity_reference(avatar_dir)
    if not os.path.isfile(identity_reference):
        raise RuntimeError("the canonical HD identity head is missing")
    if pose_reference and not os.path.isfile(pose_reference):
        raise RuntimeError("idle pose reference is missing")

    image_provider = body.default_provider()
    video_provider = body.default_video_provider()
    body_options = body_manifest.get("options") or {}
    outfit = _clean(
        body_options.get("prompt") or body_options.get("outfit"), 800
    ) or "the exact outfit shown in the generated body plates"
    prompts = {
        "walk_keyframe": _walk_keyframe_prompt(outfit, walk_style),
        "idle_keyframe": _idle_keyframe_prompt(
            outfit, bool(pose_reference), idle_pose),
        "move_keyframe": _move_keyframe_prompt(outfit, move_style),
        "walk_video": _walk_video_prompt(walk_style, walk_frame),
        "idle_video": _idle_video_prompt(idle_pose),
        "move_video": _move_video_prompt(move_style),
    }
    signature_source = "\n".join((
        _sha256(front_source),
        _sha256(side_source),
        _sha256(identity_reference),
        _sha256(pose_reference) if pose_reference else "text-pose",
        json.dumps(idle_pose, sort_keys=True),
        json.dumps(_walk_style_receipt(walk_style), sort_keys=True),
        json.dumps(_walk_frame_receipt(walk_frame), sort_keys=True),
        json.dumps(_move_style_receipt(move_style), sort_keys=True),
        image_provider["command_key"], str(image_provider.get("model")),
        video_provider["command_key"], str(video_provider.get("model")),
        *prompts.values(),
    ))
    signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()
    cache_root = os.path.join(avatar_dir, ".motion-cache")
    cache = os.path.join(cache_root, signature)
    os.makedirs(cache, mode=0o700, exist_ok=True)
    return {
        "body_source": front_source,
        "body_sources": {
            "walk": side_source, "idle": front_source, "move": front_source},
        "body_reference_views": {
            "walk": "side" if side_source != front_source else "front-legacy",
            "idle": "front",
            "move": "front",
        },
        "identity_reference": identity_reference,
        "image_provider": image_provider,
        "video_provider": video_provider,
        "idle_pose": idle_pose,
        "walk_style": walk_style,
        "walk_frame": walk_frame,
        "move_style": move_style,
        "prompts": prompts,
        "signature": signature,
        "cache_root": cache_root,
        "cache": cache,
    }


def _process_approved_original_walk(
        original_video, matte_video, source_loop, previous_walk, stage, log):
    start, end = (int(value) for value in source_loop)
    if start < 0 or end <= start:
        raise RuntimeError("approved walk loop must be START:END with END after START")
    if previous_walk.get("source_loop") != [start, end]:
        raise RuntimeError("approved walk loop does not match the current motion receipt")
    previous_pose_quality = previous_walk.get("pose_quality") or {}
    if not previous_pose_quality.get("valid"):
        raise RuntimeError("current motion metadata has no approved walk-cycle receipt")

    original_frames = _decode_video(original_video, WALK_FPS)
    matte_frames = _decode_video(matte_video, WALK_FPS)
    if len(original_frames) != len(matte_frames):
        raise RuntimeError(
            "approved original and green-matte videos have different frame counts")
    if end >= len(original_frames):
        raise RuntimeError("approved walk loop extends beyond the source video")

    original_cycle = original_frames[start:end + 1]
    matte_cycle = matte_frames[start:end + 1]
    with tempfile.TemporaryDirectory(prefix="walk-original-", dir=stage) as workspace:
        original_segmented, original_poses, original_method, _ = _segment_frames(
            original_cycle, workspace, log)
    with tempfile.TemporaryDirectory(prefix="walk-matte-", dir=stage) as workspace:
        matte_segmented, matte_poses, matte_method, _ = _segment_frames(
            matte_cycle, workspace, log)

    authoritative, alignment_quality, color_quality = _pose_aligned_color_authority(
        matte_segmented,
        matte_poses,
        original_cycle,
        original_poses,
        validation_frames=original_segmented,
    )
    approved_mode = previous_walk.get("walk_mode") or "traversal"
    if approved_mode == "loop":
        recentered, anchors = authoritative, None
    else:
        recentered, anchors = _recenter_walk_frames(authoritative)
    frame_count = end - start
    selected = recentered[:frame_count]
    strict_pose_quality = _pose_cycle_metrics(original_poses, 0, frame_count)
    extremity_quality = _extremity_integrity(
        authoritative, original_poses, 0, frame_count)
    if not extremity_quality.get("valid"):
        raise RuntimeError(
            extremity_quality.get("reason") or "approved walk lost an extremity")

    normalised, bounds, scale = _normalise_frames(selected, include_scale=True)
    if approved_mode == "loop":
        trajectory = _inplace_trajectory(
            original_poses, 0, frame_count, WALK_FPS, scale)
    else:
        trajectory = _trajectory_profile(anchors, 0, frame_count, WALK_FPS, scale)
        if not trajectory:
            raise RuntimeError("approved walk lost its steady source-root trajectory")
        trajectory = _stance_calibrated_trajectory(normalised, bounds, trajectory)
    gait = _gait_metrics(normalised, WALK_FPS, bounds, trajectory)
    gait["walk_mode"] = approved_mode
    sheets = _pack_sheets(normalised, stage, "walk")
    poster = "walk-poster.png"
    cv2.imwrite(
        os.path.join(stage, poster), normalised[0],
        [cv2.IMWRITE_PNG_COMPRESSION, 9])
    alpha_video = _encode_alpha_preview(
        normalised, WALK_FPS, os.path.join(stage, "walk-alpha.mov"))

    def digest(path):
        value = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                value.update(block)
        return value.hexdigest()

    return {
        "fps": WALK_FPS,
        "frames": len(normalised),
        "frame_width": TARGET_WIDTH,
        "frame_height": TARGET_HEIGHT,
        "bounds": bounds,
        "sheets": sheets,
        "poster": poster,
        "alpha_video": os.path.basename(alpha_video) if alpha_video else None,
        "source_loop": [start, end],
        "continuous_source_frames": True,
        "matte_method": matte_method,
        "original_segmentation_method": original_method,
        "source_authority": "approved-original-source-rgb",
        "approval_basis": "current approved walk-cycle receipt",
        "original_source_sha256": digest(original_video),
        "matte_source_sha256": digest(matte_video),
        "alignment_quality": alignment_quality,
        "color_fidelity_quality": color_quality,
        "pose_quality": json.loads(json.dumps(previous_pose_quality)),
        "strict_tracker_observation": strict_pose_quality,
        "extremity_quality": extremity_quality,
        **gait,
    }


def reprocess_approved_walk(
        avatar_dir, original_source, matte_source=None, source_loop=None,
        log=print, progress=None):
    motion_dir = os.path.join(avatar_dir, "motion")
    metadata_path = os.path.join(motion_dir, "motion.json")
    if not os.path.isfile(metadata_path):
        raise RuntimeError("existing motion metadata is required for approved reprocessing")
    if not os.path.isfile(original_source):
        raise RuntimeError(f"approved original walk source not found: {original_source}")
    if matte_source is None:
        matte_source = os.path.join(motion_dir, "raw", "walk-source.mp4")
    if not os.path.isfile(matte_source):
        raise RuntimeError(f"walk matte source not found: {matte_source}")

    with open(metadata_path, encoding="utf-8") as handle:
        metadata = json.load(handle)
    previous_walk = metadata.get("walk") or {}
    source_loop = source_loop or previous_walk.get("source_loop")
    if not isinstance(source_loop, (list, tuple)) or len(source_loop) != 2:
        raise RuntimeError("approved walk source loop is missing")

    progress = progress or (lambda *_: None)
    progress("approved-walk", 0.05, "decoding approved original and matte")
    with tempfile.TemporaryDirectory(prefix=".approved-walk-", dir=motion_dir) as stage:
        walk = _process_approved_original_walk(
            original_source,
            matte_source,
            source_loop,
            previous_walk,
            stage,
            log,
        )
        updated = dict(metadata)
        updated["v"] = MOTION_VERSION
        updated["walk"] = walk
        staged_metadata = os.path.join(stage, "motion.json")
        with open(staged_metadata, "w", encoding="utf-8") as handle:
            json.dump(updated, handle, indent=1)

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_dir = os.path.join(
            motion_dir, "backups", f"walk-{stamp}-{time.time_ns() % 1000000:06d}")
        os.makedirs(backup_dir, mode=0o700)
        old_assets = {"motion.json"}
        old_assets.update(
            item.get("image") for item in previous_walk.get("sheets", [])
            if item.get("image"))
        old_assets.update(
            value for value in (
                previous_walk.get("poster"), previous_walk.get("alpha_video"))
            if value)
        for name in sorted(old_assets):
            source = os.path.join(motion_dir, name)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(backup_dir, name))

        new_assets = {
            item["image"] for item in walk["sheets"]
        }
        new_assets.add(walk["poster"])
        if walk.get("alpha_video"):
            new_assets.add(walk["alpha_video"])
        progress("approved-walk", 0.9, "publishing original-colour walk")
        try:
            for current in Path(motion_dir).glob("walk-[0-9]*.png"):
                if current.name not in new_assets:
                    current.unlink()
            for name in sorted(new_assets):
                os.replace(os.path.join(stage, name), os.path.join(motion_dir, name))
            os.replace(staged_metadata, metadata_path)
        except Exception:
            for name in new_assets:
                destination = os.path.join(motion_dir, name)
                if os.path.isfile(destination) and name not in old_assets:
                    os.remove(destination)
            for name in old_assets:
                backup = os.path.join(backup_dir, name)
                if os.path.isfile(backup):
                    shutil.copy2(backup, os.path.join(motion_dir, name))
            raise
    progress("approved-walk", 1.0, "approved original walk installed")
    return {"walk": walk, "backup": backup_dir, "metadata": metadata_path}


def preview_keyframes(
        avatar_dir, pose_reference=None, idle_pose=None,
        walk_style=None, kinds=None, log=print, walk_frame=None,
        move_style=None):
    requested_kinds = tuple(dict.fromkeys(kinds or ("walk", "idle")))
    unknown_kinds = sorted(set(requested_kinds) - {"walk", "idle", "move"})
    if not requested_kinds or unknown_kinds:
        detail = ", ".join(unknown_kinds) if unknown_kinds else "none"
        raise ValueError(f"unknown motion clip selection: {detail}")
    context = _build_context(
        avatar_dir, pose_reference, idle_pose, walk_style, walk_frame,
        move_style)
    prompts = context["prompts"]
    keyframes = _generate_keyframes(
        context["cache"], context["image_provider"], context["body_sources"],
        context["identity_reference"], pose_reference,
        {kind: prompts[f"{kind}_keyframe"] for kind in requested_kinds},
        log, requested_kinds)
    preview_dir = os.path.join(avatar_dir, ".motion-preview")
    shutil.rmtree(preview_dir, ignore_errors=True)
    os.makedirs(preview_dir, mode=0o700)
    previews = {}
    for kind, source in keyframes.items():
        destination = os.path.join(preview_dir, f"{kind}.png")
        shutil.copy2(source, destination)
        previews[kind] = destination
    return previews


def recut(avatar_dir, kind, log=print, progress=None):
    """Reprocess one kind's RETAINED raw take through the current local
    pipeline - matting, gates, packing - without any generation. This is how
    an existing set picks up pipeline upgrades (like the RVM matte) for
    free: the provider footage is already on disk in motion/raw/.
    """
    kind = _clean(kind, 20)
    if kind not in {"walk", "idle", "move"}:
        raise ValueError(f"unknown motion clip selection: {kind}")
    destination, backup = _motion_transaction_paths(avatar_dir)
    metadata_path = os.path.join(destination, "motion.json")
    if not os.path.isfile(metadata_path):
        raise RuntimeError("no motion has been generated yet")
    with open(metadata_path, encoding="utf-8") as handle:
        metadata = json.load(handle)
    if not metadata.get(kind):
        raise RuntimeError(f"no {kind} clip exists to re-cut")
    raw_video = os.path.join(destination, "raw", f"{kind}-source.mp4")
    if not os.path.isfile(raw_video):
        raise RuntimeError(
            f"the retained raw {kind} take is missing; regenerate instead")

    process_options = {}
    if kind == "walk":
        walk_style = resolve_walk_style(metadata.get("walk_style"))
        if walk_style["id"] != DEFAULT_WALK_STYLE:
            process_options["walk_style"] = walk_style
        fps = WALK_FPS
    else:
        fps = IDLE_FPS
        if kind == "move":
            process_options["idle_validation"] = "free"
        else:
            idle_pose = resolve_idle_pose(metadata.get("idle_pose"))
            if idle_pose["validation"] != "back-heel":
                process_options["idle_validation"] = idle_pose["validation"]

    if os.path.exists(backup):
        rollback_pending_build(avatar_dir)
    _emit(progress, "alpha", 0.2, f"Re-cutting the retained {kind} take")
    stage = tempfile.mkdtemp(prefix=".motion-stage-", dir=avatar_dir)
    swapped = False
    try:
        shutil.copytree(destination, stage, dirs_exist_ok=True)
        _remove_clip_assets(stage, kind)
        # raw/ keeps the source take: it is the whole point of a re-cut.
        raw_dir = os.path.join(stage, "raw")
        os.makedirs(raw_dir, exist_ok=True)
        for name in (f"{kind}-keyframe.png", f"{kind}-source.mp4"):
            source_path = os.path.join(destination, "raw", name)
            if os.path.isfile(source_path):
                shutil.copy2(source_path, os.path.join(raw_dir, name))
        clip = _process_clip(kind, raw_video, fps, stage, log, **process_options)
        metadata[kind] = clip
        metadata["updated"] = datetime.datetime.now().isoformat(
            timespec="seconds")
        with open(os.path.join(stage, "motion.json"), "w") as handle:
            json.dump(metadata, handle, indent=1)
        shutil.rmtree(backup, ignore_errors=True)
        os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except Exception:
            if not os.path.exists(destination) and os.path.exists(backup):
                os.replace(backup, destination)
            raise
        stage = None
        swapped = True
        _emit(progress, "done", 1.0, f"{kind} re-cut ready")
        log(f"re-cut {kind} from the retained take")
        return metadata
    except Exception:
        if swapped:
            rollback_pending_build(avatar_dir)
        raise
    finally:
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)


def _unpack_clip_frames(motion_dir, kind, clip):
    """The packed atlases are lossless RGBA: the shipped frames come back
    out exactly, which is what makes frame surgery possible without raw
    reprocessing."""
    frames = []
    width = int(clip.get("frame_width") or TARGET_WIDTH)
    height = int(clip.get("frame_height") or TARGET_HEIGHT)
    for sheet in clip.get("sheets") or []:
        atlas = cv2.imread(
            os.path.join(motion_dir, os.path.basename(str(sheet["image"]))),
            cv2.IMREAD_UNCHANGED)
        if atlas is None:
            raise RuntimeError(f"packed sheet missing: {sheet['image']}")
        columns = int(sheet["columns"])
        for local in range(int(sheet["count"])):
            row, column = divmod(local, columns)
            frames.append(atlas[row * height:(row + 1) * height,
                                column * width:(column + 1) * width].copy())
    if not frames:
        raise RuntimeError("no packed frames to repair")
    return frames


def repair_frame(avatar_dir, kind, frame_index, mode="patch", note="",
                 log=print, progress=None, frame_end=None):
    """Surgical fix for the bad frame - or RUN of frames - the user points at.

    "patch" rebuilds each flagged frame as the per-pixel temporal median of
    itself and the CLEAN BOUNDARY frames just outside the flagged run. For
    one frame that is its immediate neighbours; for a run (a white flash
    living across frames 1-6) every frame in it votes against the clean
    boundaries, so a defect the neighbours share still loses - static
    regions heal to the boundaries while genuinely moving regions keep
    their own middle value. "drop" removes the flagged frames outright; at
    12-24fps a few missing frames read as nothing. Works on the packed
    lossless frames, so it costs nothing and keeps the approved take.
    """
    kind = _clean(kind, 20)
    if kind not in {"walk", "idle", "move"}:
        raise ValueError(f"unknown motion clip selection: {kind}")
    if mode not in {"patch", "drop"}:
        raise ValueError(f"unknown repair mode: {mode}")
    destination, backup = _motion_transaction_paths(avatar_dir)
    metadata_path = os.path.join(destination, "motion.json")
    if not os.path.isfile(metadata_path):
        raise RuntimeError("no motion has been generated yet")
    with open(metadata_path, encoding="utf-8") as handle:
        metadata = json.load(handle)
    clip = metadata.get(kind)
    if not isinstance(clip, dict):
        raise RuntimeError(f"no {kind} clip exists to repair")
    frames = _unpack_clip_frames(destination, kind, clip)
    total = len(frames)
    frame_index = int(frame_index)
    frame_end = frame_index if frame_end is None else int(frame_end)
    if frame_end < frame_index:
        frame_index, frame_end = frame_end, frame_index
    if not (0 <= frame_index < total and 0 <= frame_end < total):
        raise ValueError(f"frames {frame_index}-{frame_end} are outside 0..{total - 1}")
    run = list(range(frame_index, frame_end + 1))
    if len(run) > total - 3:
        raise RuntimeError("the flagged run leaves too few clean frames")
    if mode == "drop" and total - len(run) < 8:
        raise RuntimeError("too few frames would remain after the drop")
    span = (f"frame {frame_index}" if frame_index == frame_end
            else f"frames {frame_index}-{frame_end}")

    _emit(progress, "repair", 0.3, f"Repairing {kind} {span} ({mode})")
    if mode == "patch":
        # Clean boundaries live just OUTSIDE the flagged run (loop-aware):
        # inside it, every frame may carry the defect and cannot be trusted
        # as a voter.
        before = frames[(frame_index - 1) % total]
        after = frames[(frame_end + 1) % total]
        for index in run:
            stack = np.stack([before, frames[index], after]).astype(np.uint8)
            repaired = np.median(stack, axis=0).astype(np.uint8)
            repaired[:, :, :3][repaired[:, :, 3] == 0] = 0
            frames[index] = repaired
    else:
        for index in reversed(run):
            frames.pop(index)
        total = len(frames)

    stage = tempfile.mkdtemp(prefix=".motion-stage-", dir=avatar_dir)
    swapped = False
    try:
        shutil.copytree(destination, stage, dirs_exist_ok=True)
        for name in list(os.listdir(stage)):
            if re.fullmatch(rf"{kind}-\d+\.png", name):
                os.remove(os.path.join(stage, name))
        clip = dict(clip)
        clip["sheets"] = _pack_sheets(frames, stage, kind)
        clip["frames"] = len(frames)
        poster = clip.get("poster") or f"{kind}-poster.png"
        cv2.imwrite(os.path.join(stage, os.path.basename(str(poster))),
                    frames[0], [cv2.IMWRITE_PNG_COMPRESSION, 9])
        alpha_name = clip.get("alpha_video") or f"{kind}-alpha.mov"
        fps = int(clip.get("fps") or (WALK_FPS if kind == "walk" else IDLE_FPS))
        _encode_alpha_preview(
            frames, fps, os.path.join(stage, os.path.basename(str(alpha_name))))
        if clip.get("alpha_stream"):
            stream_name = os.path.basename(str(clip["alpha_stream"]))
            if not _encode_alpha_stream(
                    frames, fps, os.path.join(stage, stream_name)):
                clip["alpha_stream"] = None
        alphas = np.maximum.reduce(
            [(frame[:, :, 3] > 8).astype(np.uint8) for frame in frames])
        points = cv2.findNonZero(alphas)
        if points is not None:
            clip["bounds"] = [int(value) for value in cv2.boundingRect(points)]
        if kind != "walk" and isinstance(clip.get("edge_anchors"), dict):
            clip["edge_anchors"] = _edge_anchors(frames, clip["bounds"])
        if mode == "drop":
            offsets = clip.get("travel_offsets")
            if isinstance(offsets, list) and offsets:
                # Cumulative distance bookkeeping must shrink WITH the
                # frames: splicing entries alone leaves cycle_distance owing
                # the dropped frames' travel, and the roam engine pays that
                # debt as an instant slide at every loop wrap.
                original = [float(value) for value in offsets]
                count = len(original)
                start = min(frame_index, count - 1)
                end = min(frame_end, count - 1)
                cycle_distance = float(
                    clip.get("cycle_distance") or original[-1] or 0.0)
                gap_end = original[end + 1] if end + 1 < count else cycle_distance
                gap = max(0.0, gap_end - original[start])
                clip["travel_offsets"] = (
                    [round(value, 2) for value in original[:start]] +
                    [round(value - gap, 2) for value in original[end + 1:]])
                new_distance = max(1.0, cycle_distance - gap)
                clip["cycle_distance"] = round(new_distance, 2)
                fps_value = max(1, int(clip.get("fps") or 1))
                new_seconds = len(frames) / fps_value
                clip["cycle_seconds"] = round(new_seconds, 3)
                clip["ground_speed"] = round(
                    new_distance / max(0.1, new_seconds), 2)
            loop = clip.get("source_loop")
            if isinstance(loop, list) and len(loop) == 2:
                clip["source_loop"] = [
                    loop[0], max(loop[0] + 1, loop[1] - len(run))]
        repairs = list(clip.get("repairs") or [])
        repairs.append({
            "frame": frame_index, "end": frame_end, "mode": mode,
            "note": _clean(note, 200),
            "at": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        clip["repairs"] = repairs
        metadata[kind] = clip
        metadata["updated"] = datetime.datetime.now().isoformat(
            timespec="seconds")
        with open(os.path.join(stage, "motion.json"), "w") as handle:
            json.dump(metadata, handle, indent=1)
        shutil.rmtree(backup, ignore_errors=True)
        os.replace(destination, backup)
        try:
            os.replace(stage, destination)
        except Exception:
            if not os.path.exists(destination) and os.path.exists(backup):
                os.replace(backup, destination)
            raise
        stage = None
        swapped = True
        _emit(progress, "done", 1.0, f"{kind} {span} repaired")
        log(f"repaired {kind} {span} ({mode})" + (f": {note}" if note else ""))
        return metadata
    except Exception:
        if swapped:
            rollback_pending_build(avatar_dir)
        raise
    finally:
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)


def _motion_transaction_paths(avatar_dir):
    return (
        os.path.join(avatar_dir, "motion"),
        os.path.join(avatar_dir, "motion.previous"),
    )


def commit_pending_build(avatar_dir):
    _, backup = _motion_transaction_paths(avatar_dir)
    shutil.rmtree(backup, ignore_errors=True)


def rollback_pending_build(avatar_dir):
    destination, backup = _motion_transaction_paths(avatar_dir)
    shutil.rmtree(destination, ignore_errors=True)
    if os.path.exists(backup):
        os.replace(backup, destination)


def _archive_rejected_candidate(
        avatar_dir, signature, kind, attempt, keyframe, video, error):
    rejected_root = os.path.join(avatar_dir, ".motion-rejected")
    os.makedirs(rejected_root, mode=0o700, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    destination = os.path.join(
        rejected_root,
        f"{stamp}-{signature[:10]}-{kind}-attempt-{attempt}",
    )
    os.makedirs(destination, mode=0o700)
    for source, name in (
            (keyframe, f"{kind}-keyframe.png"),
            (video, f"{kind}-source.mp4")):
        if source and os.path.isfile(source):
            shutil.copy2(source, os.path.join(destination, name))
    with open(os.path.join(destination, "rejection.json"), "w") as handle:
        json.dump({
            "kind": kind,
            "attempt": attempt,
            "signature": signature,
            "error": _clean(error, 2000),
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
        }, handle, indent=1)
    return destination


def _invalidate_cached_video(cache, kind):
    video_dir = os.path.join(cache, "videos")
    try:
        os.remove(os.path.join(video_dir, f"{kind}.mp4"))
    except FileNotFoundError:
        pass
    shutil.rmtree(os.path.join(video_dir, f"{kind}-provider"), ignore_errors=True)


def _remove_clip_assets(directory, kind):
    if not os.path.isdir(directory):
        return
    for path in Path(directory).glob(f"{kind}-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    raw_dir = Path(directory) / "raw"
    if raw_dir.is_dir():
        for path in raw_dir.glob(f"{kind}-*"):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)


def build(
        avatar_dir, pose_reference=None, log=print, progress=None,
        keep_previous=False, idle_pose=None, kinds=None, walk_style=None,
        walk_frame=None, move_style=None):
    requested_kinds = tuple(dict.fromkeys(kinds or ("walk", "idle")))
    unknown_kinds = sorted(set(requested_kinds) - {"walk", "idle", "move"})
    if not requested_kinds or unknown_kinds:
        detail = ", ".join(unknown_kinds) if unknown_kinds else "none"
        raise ValueError(f"unknown motion clip selection: {detail}")
    context = _build_context(
        avatar_dir, pose_reference, idle_pose, walk_style, walk_frame,
        move_style)
    body_sources = context.get("body_sources") or {
        "walk": context["body_source"],
        "idle": context["body_source"],
        "move": context["body_source"],
    }
    body_reference_views = context.get("body_reference_views") or {
        "walk": "front-legacy", "idle": "front-legacy", "move": "front-legacy"}
    identity_reference = context.get("identity_reference")
    image_provider = context["image_provider"]
    video_provider = context["video_provider"]
    idle_pose = resolve_idle_pose(context.get("idle_pose"))
    walk_style = resolve_walk_style(
        context.get("walk_style") or walk_style)
    walk_frame = resolve_walk_frame(
        context.get("walk_frame") or walk_frame)
    move_style = resolve_move_style(
        context.get("move_style") or move_style)
    prompts = context["prompts"]
    signature = context["signature"]
    cache_root = context["cache_root"]
    cache = context["cache"]

    destination, backup = _motion_transaction_paths(avatar_dir)
    if os.path.exists(backup):
        rollback_pending_build(avatar_dir)
    previous_metadata = {}
    previous_metadata_path = os.path.join(destination, "motion.json")
    if os.path.isfile(previous_metadata_path):
        with open(previous_metadata_path, encoding="utf-8") as handle:
            previous_metadata = json.load(handle)
    rejections = {kind: 0 for kind in requested_kinds}
    while True:
        retry_count = sum(rejections.values())
        retry_progress = (
            min(0.88, 0.78 + retry_count * 0.04) if retry_count else 0.0)
        kind_labels = {"walk": walk_style["label"], "idle": "Edge Idle",
                       "move": f"Moves · {move_style['label']}"}
        selected_label = " and ".join(
            kind_labels[kind] for kind in requested_kinds)
        if retry_count:
            _emit(
                progress, "retry", retry_progress,
                f"Regenerating rejected {selected_label} candidate")
        else:
            _emit(
                progress, "keyframes", 0.06,
                f"Creating {selected_label} keyframe" +
                ("s" if len(requested_kinds) > 1 else ""))
        keyframes = _generate_keyframes(
            cache, image_provider, body_sources, identity_reference, pose_reference,
            {kind: prompts[f"{kind}_keyframe"] for kind in requested_kinds},
            log, requested_kinds)
        if not retry_count:
            _emit(progress, "video", 0.32, f"Animating {selected_label}")
        videos = _generate_videos(
            cache, video_provider, keyframes,
            {kind: prompts[f"{kind}_video"] for kind in requested_kinds},
            log, requested_kinds,
            walk_frame, walk_style)

        stage = tempfile.mkdtemp(prefix=".motion-stage-", dir=avatar_dir)
        if os.path.isdir(destination):
            shutil.copytree(destination, stage, dirs_exist_ok=True)
        for kind in requested_kinds:
            _remove_clip_assets(stage, kind)
        swapped = False
        try:
            clips = {}
            rejected_kind = None
            rejected_error = None
            all_clip_specs = {
                "walk": (
                    WALK_FPS, max(0.60, retry_progress),
                    f"Alpha-cutting {walk_style['label']} locally",
                ),
                "idle": (
                    IDLE_FPS, max(0.77, min(0.90, retry_progress + 0.02)),
                    "Alpha-cutting Edge Idle locally",
                ),
                "move": (
                    IDLE_FPS, max(0.77, min(0.90, retry_progress + 0.02)),
                    f"Alpha-cutting {move_style['label']} locally",
                ),
            }
            clip_specs = (
                (kind, *all_clip_specs[kind]) for kind in requested_kinds
            )
            for kind, fps, value, label in clip_specs:
                _emit(progress, "alpha", value, label)
                try:
                    process_options = {}
                    if kind == "idle" and idle_pose["validation"] != "back-heel":
                        process_options["idle_validation"] = idle_pose["validation"]
                    if kind == "move":
                        # Every move is a free act: centered, unmirrored,
                        # no wall contact to validate.
                        process_options["idle_validation"] = "free"
                    if kind == "walk" and walk_style["id"] != DEFAULT_WALK_STYLE:
                        process_options["walk_style"] = walk_style
                    clips[kind] = _process_clip(
                        kind, videos[kind], fps, stage, log, **process_options)
                except Exception as error:
                    rejected_kind = kind
                    rejected_error = error
                    break
            if rejected_kind:
                attempt = rejections[rejected_kind] + 1
                try:
                    archived = _archive_rejected_candidate(
                        avatar_dir, signature, rejected_kind, attempt,
                        keyframes[rejected_kind], videos[rejected_kind],
                        rejected_error)
                    log(f"archived rejected {rejected_kind} candidate at {archived}")
                except Exception as archive_error:
                    log(f"could not archive rejected {rejected_kind} candidate: {archive_error}")
                _invalidate_cached_video(cache, rejected_kind)
                rejections[rejected_kind] = attempt
                if attempt < MAX_CANDIDATE_ATTEMPTS:
                    log(
                        f"{rejected_kind} candidate failed quality gates; "
                        "generating one fresh video candidate")
                    continue
                raise RuntimeError(
                    f"{rejected_kind} failed quality gates after "
                    f"{MAX_CANDIDATE_ATTEMPTS} candidates: {rejected_error}"
                ) from rejected_error

            raw_dir = os.path.join(stage, "raw")
            os.makedirs(raw_dir, exist_ok=True)
            for kind in requested_kinds:
                shutil.copy2(
                    keyframes[kind], os.path.join(raw_dir, f"{kind}-keyframe.png"))
                shutil.copy2(
                    videos[kind], os.path.join(raw_dir, f"{kind}-source.mp4"))
            updated = datetime.datetime.now().isoformat(timespec="seconds")
            metadata = dict(previous_metadata)
            metadata.update({
                "v": MOTION_VERSION,
                "signature": signature,
                "image_provider": image_provider,
                "video_provider": video_provider,
                "identity_reference": {
                    "file": os.path.basename(identity_reference)
                    if identity_reference else None,
                    "sha256": _sha256(identity_reference)
                    if identity_reference else None,
                    "use": "canonical HD facial identity",
                },
                "created": previous_metadata.get("created") or updated,
                "updated": updated,
            })
            for kind in requested_kinds:
                metadata[kind] = clips[kind]
            if "walk" in requested_kinds:
                metadata["walk_style"] = _walk_style_receipt(walk_style)
                if walk_mode(walk_style) == "traversal":
                    metadata["walk_frame"] = _walk_frame_receipt(walk_frame)
                else:
                    # In-place loops have no runway; a stale runway receipt
                    # would advertise geometry the footage never used.
                    metadata.pop("walk_frame", None)
            if "idle" in requested_kinds:
                metadata["idle_pose"] = idle_pose
                metadata["reference"] = {
                    "file": None,
                    "sha256": _sha256(pose_reference) if pose_reference else None,
                    "use": "pose geometry only",
                    "retained": False,
                }
            if "move" in requested_kinds:
                metadata["move_style"] = _move_style_receipt(move_style)
            body_references = dict(metadata.get("body_references") or {})
            for kind in requested_kinds:
                body_references[kind] = {
                    "view": body_reference_views[kind],
                    "file": os.path.basename(body_sources[kind]),
                    "sha256": _sha256(body_sources[kind]),
                    "use": (
                        "Horizon Walk side geometry, proportions, and wardrobe"
                        if kind == "walk" else
                        "Show Me Some Moves proportions and wardrobe"
                        if kind == "move" else
                        "Edge Idle proportions and wardrobe"
                    ),
                }
            metadata["body_references"] = body_references
            prompt_receipt = dict(metadata.get("prompts") or {})
            for kind in requested_kinds:
                prompt_receipt[f"{kind}_keyframe"] = prompts[f"{kind}_keyframe"]
                prompt_receipt[f"{kind}_video"] = prompts[f"{kind}_video"]
            metadata["prompts"] = prompt_receipt
            with open(os.path.join(stage, "motion.json"), "w") as handle:
                json.dump(metadata, handle, indent=1)

            shutil.rmtree(backup, ignore_errors=True)
            if os.path.exists(destination):
                os.replace(destination, backup)
            try:
                os.replace(stage, destination)
            except Exception:
                if not os.path.exists(destination) and os.path.exists(backup):
                    os.replace(backup, destination)
                raise
            stage = None
            swapped = True
            shutil.rmtree(cache_root, ignore_errors=True)
            shutil.rmtree(
                os.path.join(avatar_dir, ".motion-preview"), ignore_errors=True)
            _emit(progress, "done", 1.0, f"{selected_label} ready")
            log(f"generated alpha {selected_label}")
            if not keep_previous:
                commit_pending_build(avatar_dir)
            return metadata
        except Exception:
            if swapped:
                rollback_pending_build(avatar_dir)
            elif not os.path.exists(destination) and os.path.exists(backup):
                os.replace(backup, destination)
            raise
        finally:
            if stage and os.path.exists(stage):
                shutil.rmtree(stage, ignore_errors=True)


def remove(avatar_dir, kind=None):
    kind = _clean(kind, 20) or "both"
    if kind not in {"walk", "idle", "move", "both"}:
        raise ValueError(f"unknown motion clip selection: {kind}")
    motion_dir = os.path.join(avatar_dir, "motion")
    cache_dir = os.path.join(avatar_dir, ".motion-cache")
    if kind == "both":
        shutil.rmtree(motion_dir, ignore_errors=True)
        shutil.rmtree(cache_dir, ignore_errors=True)
        return None
    metadata_path = os.path.join(motion_dir, "motion.json")
    if not os.path.isfile(metadata_path):
        shutil.rmtree(cache_dir, ignore_errors=True)
        return None
    with open(metadata_path, encoding="utf-8") as handle:
        metadata = json.load(handle)
    _remove_clip_assets(motion_dir, kind)
    metadata.pop(kind, None)
    body_references = dict(metadata.get("body_references") or {})
    body_references.pop(kind, None)
    metadata["body_references"] = body_references
    prompts = dict(metadata.get("prompts") or {})
    prompts.pop(f"{kind}_keyframe", None)
    prompts.pop(f"{kind}_video", None)
    metadata["prompts"] = prompts
    if kind == "walk":
        metadata.pop("walk_style", None)
    elif kind == "move":
        metadata.pop("move_style", None)
    else:
        metadata.pop("idle_pose", None)
        metadata.pop("reference", None)
    shutil.rmtree(cache_dir, ignore_errors=True)
    if not any(metadata.get(name) for name in ("walk", "idle", "move")):
        shutil.rmtree(motion_dir, ignore_errors=True)
        return None
    metadata["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    temporary = metadata_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=1)
    os.replace(temporary, metadata_path)
    return metadata
