"""Safe regional calibration profiles and landmark inspector geometry."""
import math

import cv2
import numpy as np

from . import face

VERSION = 3
# Owner calibration on the live desktop (2026-08-01): the canonical defaults
# were so conservative that every avatar needed a hand rebuild at ~100% to
# read as a normal human face. The owner's proven profile IS the default
# now, sliders run to 150 (the transfer alphas clip at 1.0, so past 100 the
# feathered ring saturates toward full strength - fuller transfer, never
# extrapolation), and the green bands embrace the proven values. Brows stay
# an exception: their strips render fully opaque since the alpha fix, and
# 10 is the owner-approved resting default.
CONTROLS = {
    "lips": dict(label="Lips", minimum=0, maximum=150,
                 safe_minimum=80, safe_maximum=120, step=1,
                 default=100, help="Direct lip and mouth-corner motion."),
    "jaw": dict(label="Jaw and chin", minimum=0, maximum=150,
                safe_minimum=25, safe_maximum=110, step=1,
                default=97, help="How strongly the lower jaw follows speech."),
    "cheeks": dict(label="Cheeks", minimum=0, maximum=150,
                   safe_minimum=0, safe_maximum=110, step=1,
                   default=100, help="Speech-coupled cheek movement."),
    "brows": dict(label="Eyebrows", minimum=0, maximum=150,
                  safe_minimum=5, safe_maximum=85, step=1,
                  default=10, help="Speech-coupled eyebrow gestures. "
                                   "Applied live by the runtime."),
    "forehead": dict(label="Forehead", minimum=0, maximum=150,
                     safe_minimum=15, safe_maximum=110, step=1,
                     default=100, help="How much the forehead skin follows a "
                                       "brow raise. Applied live by the runtime."),
    "nasolabial": dict(label="Nasolabial folds", minimum=0, maximum=150,
                       safe_minimum=0, safe_maximum=110, step=1,
                       default=100, help="Motion beside the nose and mouth corners."),
    "nose": dict(label="Nose base and nostrils", minimum=0, maximum=150,
                 safe_minimum=0, safe_maximum=110, step=1,
                 default=100, help="Maximum speech influence; bridge and tip stay locked."),
    # Owner request 2026-08-01 (carol, upper TH 765px): the dental lock gets
    # a control surface. Strength is a blend alpha, not a motion gain - 100
    # is today's full canonical paste (the only proven value, hence the
    # one-point green band), 0 keeps every frame's own rendered teeth.
    "teeth": dict(label="Teeth lock strength", minimum=0, maximum=100,
                  safe_minimum=100, safe_maximum=100, step=1,
                  default=100, help="How firmly every frame wears the canonical "
                                    "dental row. Below 100 each frame's own "
                                    "teeth blend back in."),
}
# The dental-donor candidate lists live HERE, not in compose: the profile
# normalizer must validate donor overrides, and compose already imports rig
# (the reverse import would be circular). compose aliases these names.
DENTAL_ROWS = ("upper", "lower")
UPPER_TEETH_DONORS = ("SS", "eh", "ih", "ah", "kk", "TH", "DD", "nn", "CH", "FF", "RR")
LOWER_TEETH_DONORS = ("ih", "SS", "eh", "TH", "FF", "ah", "DD", "kk", "CH", "nn", "RR")
DENTAL_DONORS = {
    "upper": UPPER_TEETH_DONORS,
    "lower": LOWER_TEETH_DONORS,
}
PRESETS = {
    "natural": dict(lips=100, jaw=97, cheeks=100, brows=10, forehead=100,
                    nasolabial=100, nose=100),
    "subtle": dict(lips=92, jaw=70, cheeks=60, brows=6, forehead=60,
                   nasolabial=60, nose=30),
    "expressive": dict(lips=120, jaw=115, cheeks=120, brows=20, forehead=120,
                       nasolabial=120, nose=110),
}
REGION_GROUPS = {
    "lips": [face.OUTER_LIP],
    "jaw": [face.JAW_REGION],
    "cheeks": [face.CHEEK_L, face.CHEEK_R],
    "nasolabial": [face.NASOLABIAL_L, face.NASOLABIAL_R],
    "nose": [face.NOSE_BASE],
    "brows": [face.BROW_L, face.BROW_R],
    "locked": [face.NOSE_CORE],
}


def normalize(profile=None):
    source = dict(PRESETS["natural"])
    if profile:
        source.update({key: value for key, value in dict(profile).items()
                       if key in CONTROLS})
    result = {"version": VERSION}
    for key, spec in CONTROLS.items():
        value = source.get(key, spec["default"])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be numeric")
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{key} must be finite")
        if value < spec["minimum"] or value > spec["maximum"]:
            raise ValueError(
                f"{key} must be between {spec['minimum']} and {spec['maximum']}")
        result[key] = round(value, 3)
    if profile and any(profile.get(key) is False for key in
                       ("teeth_lock", "upper_teeth_lock", "lower_teeth_lock")):
        raise ValueError("upper/lower dental identity lock cannot be disabled")
    result["teeth_lock"] = True
    result["upper_teeth_lock"] = True
    result["lower_teeth_lock"] = True
    # Donor overrides ride the profile as plain shape names; "auto" keeps
    # the largest-detected-row election. Validation is the only gate here -
    # a chosen frame with no detected enamel is compose's advisory fallback,
    # never a rebuild veto.
    for row in DENTAL_ROWS:
        key = f"{row}_teeth_donor"
        choice = (profile.get(key) if profile else None) or "auto"
        if not isinstance(choice, str) or (
                choice != "auto" and choice not in DENTAL_DONORS[row]):
            raise ValueError(
                f"{key} must be auto or one of {', '.join(DENTAL_DONORS[row])}")
        result[key] = choice
    preset = profile.get("preset") if profile else "natural"
    result["preset"] = preset if preset in {*PRESETS, "custom"} else "custom"
    return result


def from_manifest(manifest):
    try:
        return normalize((manifest or {}).get("rig_profile"))
    except ValueError:
        return normalize()


def public_schema():
    return dict(
        version=VERSION,
        controls=CONTROLS,
        presets={name: normalize({**values, "preset": name})
                 for name, values in PRESETS.items()},
        locks=dict(nose_bridge_tip=0, upper_teeth=True, lower_teeth=True),
    )


def mesh_edges(landmarks, width, height):
    subdivision = cv2.Subdiv2D((0, 0, int(width), int(height)))
    points = np.asarray(landmarks, np.float32)
    for x, y in points:
        if 0 <= x < width and 0 <= y < height:
            try:
                subdivision.insert((float(x), float(y)))
            except cv2.error:
                pass
    edges = set()
    for triangle in subdivision.getTriangleList().reshape(-1, 3, 2):
        indices = []
        for vertex in triangle:
            distance = np.square(points - vertex).sum(axis=1)
            index = int(np.argmin(distance))
            if float(distance[index]) > 4.0:
                indices = []
                break
            indices.append(index)
        if len(set(indices)) != 3:
            continue
        for left, right in ((indices[0], indices[1]),
                            (indices[1], indices[2]),
                            (indices[2], indices[0])):
            edges.add(tuple(sorted((left, right))))
    return [list(edge) for edge in sorted(edges)]


def inspector_payload(landmarks, shape):
    height, width = shape[:2]
    points = [[round(float(x / width), 6), round(float(y / height), 6)]
              for x, y in landmarks]
    regions = {}
    for name, groups in REGION_GROUPS.items():
        polygons = []
        for group in groups:
            hull = cv2.convexHull(
                np.asarray(landmarks[group], np.float32)).reshape(-1, 2)
            polygons.append([
                [round(float(x / width), 6), round(float(y / height), 6)]
                for x, y in hull])
        regions[name] = polygons
    # The forehead has no landmark ring - synthesize its band: brow tops up
    # to the face oval's crown, so the panel can answer the Forehead slider.
    brow_points = np.vstack([np.asarray(landmarks[face.BROW_L], np.float32),
                             np.asarray(landmarks[face.BROW_R], np.float32)])
    oval = np.asarray(landmarks[face.FACE_OVAL], np.float32)
    x0, x1 = float(brow_points[:, 0].min()), float(brow_points[:, 0].max())
    y_bottom = float(brow_points[:, 1].min())
    y_top = max(0.0, float(oval[:, 1].min()) - 0.015 * height)
    regions["forehead"] = [[
        [round(x0 / width, 6), round(y_top / height, 6)],
        [round(x1 / width, 6), round(y_top / height, 6)],
        [round(x1 / width, 6), round(y_bottom / height, 6)],
        [round(x0 / width, 6), round(y_bottom / height, 6)],
    ]]
    return dict(points=points, edges=mesh_edges(landmarks, width, height),
                regions=regions, width=width, height=height)


def sampled_weights(alpha, landmarks):
    samples = {}
    for label, index in (("nose_tip", 1), ("nose_base", 2),
                         ("nostril_left", 98), ("nostril_right", 327),
                         ("upper_lip", 13)):
        x, y = landmarks[index].round().astype(int)
        samples[label] = round(float(alpha[y, x]) * 100.0, 1)
    return samples
