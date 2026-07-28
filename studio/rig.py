"""Safe regional calibration profiles and landmark inspector geometry."""
import math

import cv2
import numpy as np

from . import face

VERSION = 3
CONTROLS = {
    "lips": dict(label="Lips", minimum=0, maximum=100,
                 safe_minimum=80, safe_maximum=100, step=1,
                 default=100, help="Direct lip and mouth-corner motion."),
    "jaw": dict(label="Jaw and chin", minimum=0, maximum=100,
                safe_minimum=25, safe_maximum=80, step=1,
                default=62, help="How strongly the lower jaw follows speech."),
    "cheeks": dict(label="Cheeks", minimum=0, maximum=100,
                   safe_minimum=0, safe_maximum=70, step=1,
                   default=50, help="Speech-coupled cheek movement."),
    "nasolabial": dict(label="Nasolabial folds", minimum=0, maximum=100,
                       safe_minimum=0, safe_maximum=70, step=1,
                       default=55, help="Motion beside the nose and mouth corners."),
    "nose": dict(label="Nose base and nostrils", minimum=0, maximum=100,
                 safe_minimum=0, safe_maximum=12, step=1,
                 default=8, help="Maximum speech influence; bridge and tip stay locked."),
}
PRESETS = {
    "natural": dict(lips=100, jaw=62, cheeks=50, nasolabial=55, nose=8),
    "subtle": dict(lips=88, jaw=42, cheeks=25, nasolabial=32, nose=4),
    "expressive": dict(lips=100, jaw=76, cheeks=65, nasolabial=68, nose=10),
}
REGION_GROUPS = {
    "lips": [face.OUTER_LIP],
    "jaw": [face.JAW_REGION],
    "cheeks": [face.CHEEK_L, face.CHEEK_R],
    "nasolabial": [face.NASOLABIAL_L, face.NASOLABIAL_R],
    "nose": [face.NOSE_BASE],
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
