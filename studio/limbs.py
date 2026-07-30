"""Limb reaction states: local warps of the standing plate, no generation.

The face already proves the technique: iris, brow, jaw, and head all move by
warping tissue that is already in the keyframe, baked as sprite states. A tap
reaction needs exactly the same thing at body scale - a hand that lifts a
little and settles back, shoulders that shrug - and the skeleton gives the
hinges. Each state rotates the forearm about the ELBOW (or lifts the shoulder
band) with a weight that is rigid inside the limb and decays to nothing in a
soft band around it, so the surrounding pixels stretch instead of tearing and
there is never a hole to inpaint: displacement at the hinge is zero by
construction, and first and last states are the plate itself.

Kept deliberately modest: these are acknowledgements, not choreography. A
theatrical wave needs generated video; a charming flinch does not.
"""
import json
import os

import numpy as np
import cv2

# 7 states from rest to peak; the runtime plays rest -> peak -> rest.
STATES = 7
# The arm motion is a vertical hand+cuff raise, not a rotation: with hands
# hanging beside the thighs, ANY hinge rotation sweeps trouser fabric out of
# the silhouette (measured twice: 16deg smears a fist-size patch, and even
# 3.5+7deg extrudes a green lobe past the leg edge). A vertical lift keeps
# every displaced trouser pixel inside the trouser, exactly like the shrug.
ARM_LIFT_RATIO = 0.11         # of forearm length, at the hand
SHRUG_PEAK_RATIO = 0.055      # of shoulder width, at the shoulders


def _smoothstep(a, b, x):
    t = np.clip((x - a) / max(b - a, 1e-6), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _joint(pose, name):
    joint = ((pose or {}).get("joints") or {}).get(name)
    if not joint:
        return None
    return np.array([float(joint["x"]), float(joint["y"])], dtype=np.float64)


def _segment_distance(px, py, a, b):
    ab = b - a
    length2 = float(ab @ ab)
    t = np.clip(((px - a[0]) * ab[0] + (py - a[1]) * ab[1]) / max(length2, 1e-6),
                0.0, 1.0)
    cx = a[0] + t * ab[0]
    cy = a[1] + t * ab[1]
    return np.sqrt((px - cx) ** 2 + (py - cy) ** 2)


def _clip_box(x0, y0, x1, y1, width, height):
    return (max(0, int(x0)), max(0, int(y0)),
            min(width, int(x1)), min(height, int(y1)))


def _arm_states(plate, elbow, wrist):
    """Hand and cuff rise vertically and settle back; the elbow stays quiet."""
    height, width = plate.shape[:2]
    forearm = wrist - elbow
    length = float(np.linalg.norm(forearm))
    if length < 24:
        return None
    tip = wrist + forearm / length * 0.58 * length      # include the fingertips
    rigid = 0.22 * length
    band = 0.12 * length
    lift = ARM_LIFT_RATIO * length

    reach = length * 1.58 + rigid + band + lift
    x0, y0, x1, y1 = _clip_box(
        elbow[0] - reach, elbow[1] - reach,
        elbow[0] + reach, elbow[1] + reach, width, height)
    xs = np.arange(x0, x1, dtype=np.float32)
    ys = np.arange(y0, y1, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys)

    d = _segment_distance(gx, gy, elbow, tip)
    span = tip - elbow
    span_len2 = max(float(span @ span), 1e-6)
    t_axis = np.clip(((gx - elbow[0]) * span[0] + (gy - elbow[1]) * span[1])
                     / span_len2, 0.0, 1.0)
    # Full weight over the hand and cuff, fading to nothing by mid-forearm so
    # the sleeve stretches softly instead of shearing at the elbow.
    w = ((1.0 - _smoothstep(rigid, rigid + band, d))
         * _smoothstep(0.22, 0.52, t_axis)).astype(np.float32)

    base = plate[y0:y1, x0:x1]
    patches = []
    for index in range(STATES):
        amount = index / (STATES - 1)
        map_x = gx.astype(np.float32)
        map_y = (gy + lift * amount * w).astype(np.float32)  # src below -> lifts
        warped = cv2.remap(plate, map_x, map_y, cv2.INTER_LANCZOS4,
                           borderMode=cv2.BORDER_REPLICATE)
        # The patch is a full REPLACEMENT tile for its box: outside the claim
        # it equals the plate exactly, inside it is the warped content - so
        # the runtime clears the box and draws the tile, and a vacated pixel
        # goes properly transparent instead of double-exposing the limb.
        claim = np.clip(w * 1.5, 0.0, 1.0)
        rgb = np.where(claim[..., None] > 1e-3, warped[:, :, :3], base[:, :, :3])
        alpha = (warped[:, :, 3].astype(np.float32) * claim
                 + base[:, :, 3].astype(np.float32) * (1.0 - claim))
        patches.append(np.dstack([
            rgb.astype(np.uint8),
            np.clip(alpha, 0, 255).astype(np.uint8),
        ]))
    return {"box": [x0, y0, x1 - x0, y1 - y0], "patches": patches}


def _shrug_states(plate, left_shoulder, right_shoulder, neck):
    height, width = plate.shape[:2]
    span = float(np.linalg.norm(left_shoulder - right_shoulder))
    if span < 40:
        return None
    lift = span * SHRUG_PEAK_RATIO
    radius = span * 0.42
    x0, y0, x1, y1 = _clip_box(
        min(left_shoulder[0], right_shoulder[0]) - radius,
        min(left_shoulder[1], right_shoulder[1]) - radius - lift * 2,
        max(left_shoulder[0], right_shoulder[0]) + radius,
        max(left_shoulder[1], right_shoulder[1]) + radius, width, height)
    xs = np.arange(x0, x1, dtype=np.float32)
    ys = np.arange(y0, y1, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys)
    w = np.zeros_like(gx, dtype=np.float32)
    for shoulder in (left_shoulder, right_shoulder):
        d = np.sqrt((gx - shoulder[0]) ** 2 + (gy - shoulder[1]) ** 2)
        w = np.maximum(w, 1.0 - _smoothstep(radius * 0.35, radius, d))
    # The neck stays quiet so the shrug never fights the head band.
    if neck is not None:
        dn = np.sqrt((gx - neck[0]) ** 2 + (gy - neck[1]) ** 2)
        w *= _smoothstep(span * 0.10, span * 0.26, dn)

    base = plate[y0:y1, x0:x1]
    patches = []
    for index in range(STATES):
        amount = index / (STATES - 1)
        map_x = gx.astype(np.float32)
        map_y = (gy + lift * amount * w).astype(np.float32)  # src below -> lifts
        warped = cv2.remap(plate, map_x, map_y, cv2.INTER_LANCZOS4,
                           borderMode=cv2.BORDER_REPLICATE)
        # The patch is a full REPLACEMENT tile for its box: outside the claim
        # it equals the plate exactly, inside it is the warped content - so
        # the runtime clears the box and draws the tile, and a vacated pixel
        # goes properly transparent instead of double-exposing the limb.
        claim = np.clip(w * 1.5, 0.0, 1.0)
        rgb = np.where(claim[..., None] > 1e-3, warped[:, :, :3], base[:, :, :3])
        alpha = (warped[:, :, 3].astype(np.float32) * claim
                 + base[:, :, 3].astype(np.float32) * (1.0 - claim))
        patches.append(np.dstack([
            rgb.astype(np.uint8),
            np.clip(alpha, 0, 255).astype(np.uint8),
        ]))
    return {"box": [x0, y0, x1 - x0, y1 - y0], "patches": patches}


def build(plate, pose, log=print):
    """-> {name: {box, patches}} for every reaction the skeleton supports."""
    if plate is None or plate.ndim != 3 or plate.shape[2] != 4:
        raise ValueError("limb reactions need the RGBA standing plate")
    reactions = {}
    for side in ("left", "right"):
        elbow = _joint(pose, f"{side}_elbow")
        wrist = _joint(pose, f"{side}_wrist")
        if elbow is None or wrist is None:
            continue
        states = _arm_states(plate, elbow, wrist)
        if states:
            reactions[f"arm_{side[0]}"] = states
    left_shoulder = _joint(pose, "left_shoulder")
    right_shoulder = _joint(pose, "right_shoulder")
    if left_shoulder is not None and right_shoulder is not None:
        states = _shrug_states(plate, left_shoulder, right_shoulder,
                               _joint(pose, "neck"))
        if states:
            reactions["shrug"] = states
    if reactions:
        log(f"  limb reactions baked: {', '.join(sorted(reactions))} "
            f"({STATES} states each)")
    return reactions
