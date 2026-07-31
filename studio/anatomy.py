"""Reusable anatomy QA for staged and published viseme banks."""
import os

import cv2
import numpy as np

from . import compose, face, rig

DENTAL_COLOR_TOLERANCE = 24.0


def _comparison_metrics(rows):
    if not rows:
        return (None, 0), (None, 1.0), (None, 0.0)
    offset = max(rows, key=lambda row: abs(row[3]))
    coverage = min(rows, key=lambda row: row[1])
    extra = max(rows, key=lambda row: row[2])
    return ((offset[0], abs(offset[3])),
            (coverage[0], coverage[1]),
            (extra[0], extra[2]))


def _disconnected_fraction(actual, anchor, ignore_components_at_most=0):
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        actual.astype(np.uint8))
    total = int(np.count_nonzero(actual))
    disconnected = 0
    for index in range(1, count):
        component = labels == index
        area = int(stats[index, cv2.CC_STAT_AREA])
        if (not np.any(component & anchor) and
                area > ignore_components_at_most):
            disconnected += area
    return disconnected / max(1, total)


def _dental_reference(diag_dir, row, name):
    if not diag_dir:
        return None
    root = os.path.join(diag_dir, "dental")
    paths = {
        kind: os.path.join(root, f"{row}_{name}_{kind}.png")
        for kind in ("mask", "zone", "reference")
    }
    if not all(os.path.isfile(path) for path in paths.values()):
        return None
    mask = cv2.imread(paths["mask"], cv2.IMREAD_GRAYSCALE)
    zone = cv2.imread(paths["zone"], cv2.IMREAD_GRAYSCALE)
    reference = cv2.imread(paths["reference"])
    if mask is None or zone is None or reference is None:
        return None
    return mask, zone, reference


def _dental_row_metrics(row, selected, viseme_dir, diag_dir=None):
    donor_name, _, donor_lm, master = selected
    comparisons = []
    counts = {}
    for name in sorted(compose.TEETH_SHAPES - {donor_name}):
        image = cv2.imread(os.path.join(viseme_dir, f"v_{name}.jpg"))
        if image is None:
            continue
        landmarks, _ = face.detect(image)
        if landmarks is None:
            raise AssertionError(f"no face detected in {name}")
        staged = _dental_reference(diag_dir, row, name)
        if staged:
            reveal, zone, reference = staged
            actual = compose._tooth_mask(image, zone)
            difference = np.abs(
                image.astype(np.int16) - reference.astype(np.int16)
            ).mean(axis=2)
            expected = reveal > 0
            color_coverage = float(np.mean(
                difference[expected] <= DENTAL_COLOR_TOLERANCE
            )) if np.any(expected) else 1.0
            canonical = reveal
        else:
            cavity = compose._mouth_cavity(image.shape, landmarks)
            actual = compose._tooth_mask(image, cavity, landmarks, row=row)
            if row == "lower":
                transform = compose._lower_row_transform(donor_lm, landmarks)
                canonical = cv2.warpAffine(
                    master, transform, (master.shape[1], master.shape[0]),
                    flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0)
            else:
                canonical = master
            reveal = cv2.bitwise_and(
                canonical, cv2.erode(cavity, np.ones((2, 2), np.uint8)))
            reveal = cv2.bitwise_and(
                reveal, compose._row_zone(cavity, landmarks, row))
            color_coverage = 1.0
        counts[name] = int(np.count_nonzero(actual))
        if int(np.count_nonzero(reveal)) < 8:
            continue
        actual_bool = actual > 0
        canonical_near = cv2.dilate(
            reveal, np.ones((7, 7), np.uint8)) > 0
        extra = _disconnected_fraction(
            actual_bool, canonical_near, ignore_components_at_most=4)
        offsets = []
        for offset in range(-4, 5):
            shifted = np.roll(reveal > 0, offset, axis=0)
            if offset > 0:
                shifted[:offset] = False
            elif offset < 0:
                shifted[offset:] = False
            intersection = np.count_nonzero(actual_bool & shifted)
            coverage = intersection / max(1, np.count_nonzero(shifted))
            offsets.append((coverage, offset))
        best_coverage, best_offset = max(offsets)
        comparisons.append(
            (name, best_coverage, extra, best_offset, color_coverage))

    if not comparisons:
        raise AssertionError(f"no comparable {row} dental-row poses")
    ((offset_pose, worst_offset),
     (coverage_pose, worst_coverage),
     (extra_pose, worst_extra)) = _comparison_metrics(comparisons)
    color_pose, worst_color = min(
        ((values[0], values[4]) for values in comparisons),
        key=lambda values: values[1],
    )
    if worst_offset > 1:
        raise AssertionError(
            f"{offset_pose} {row} teeth offset {worst_offset}px")
    if worst_coverage < 0.70:
        raise AssertionError(
            f"{coverage_pose} {row} dental coverage {worst_coverage:.1%}")
    if worst_extra > 0.10:
        raise AssertionError(
            f"{extra_pose} non-canonical {row} pixels {worst_extra:.1%}")
    if worst_color < 0.70:
        raise AssertionError(
            f"{color_pose} {row} donor-color identity {worst_color:.1%}")
    return dict(
        donor=donor_name,
        dental_poses=1 + len(comparisons),
        comparison_poses=len(comparisons),
        max_offset_px=worst_offset,
        worst_coverage=round(worst_coverage, 4),
        worst_extra=round(worst_extra, 4),
        worst_color_identity=round(worst_color, 4),
        counts=counts,
    )


def validate(keyframe_path, viseme_dir, profile=None, diag_dir=None):
    profile = rig.normalize(profile)
    if diag_dir is None:
        candidate = os.path.join(os.path.dirname(viseme_dir), "diag")
        diag_dir = candidate if os.path.isdir(candidate) else None
    selected = compose._select_dental_donors(viseme_dir)
    missing = [row for row in compose.DENTAL_ROWS if row not in selected]
    # Some faces never show a full tooth row in any speech shape, and
    # canonicalize_teeth already degrades gracefully then (the lock is
    # skipped with a warning). The QA must not be stricter than the feature
    # it audits - a missing donor is reported, not fatal.
    dental_rows = {
        row: _dental_row_metrics(
            row, selected[row], viseme_dir, diag_dir=diag_dir)
        for row in selected
    }

    keyframe = cv2.imread(keyframe_path)
    if keyframe is None:
        raise AssertionError("missing keyframe for nose-lock QA")
    key_landmarks, _ = face.detect(keyframe)
    if key_landmarks is None:
        raise AssertionError("no face detected in keyframe")
    masks, face_mask = compose._masks(keyframe, key_landmarks, profile)
    mouth_alpha, _ = compose._alpha_ring(
        masks["mouth"], face_mask, max(keyframe.shape[:2]) / 1024.0,
        profile)
    samples = rig.sampled_weights(mouth_alpha, key_landmarks)
    nose_values = [samples[key] for key in
                   ("nose_tip", "nose_base", "nostril_left", "nostril_right")]
    nose_limit = min(12.0, profile["nose"] + 2.0)
    if max(nose_values) > nose_limit:
        raise AssertionError(f"speech mask deforms nose {samples}")
    lip_floor = max(78.0, profile["lips"] - 3.0)
    if samples["upper_lip"] < lip_floor:
        raise AssertionError(
            f"nose lock suppresses upper lip {samples['upper_lip']:.1f}%")

    shadow_floor = 255
    for name in compose.visemes.ORDER:
        if name in compose.visemes.EYE_SHAPES:
            continue
        image = cv2.imread(os.path.join(viseme_dir, f"v_{name}.jpg"))
        if image is None:
            raise AssertionError(f"missing viseme {name}")
        landmarks, _ = face.detect(image)
        cavity = compose._mouth_cavity(image.shape, landmarks) > 0
        value = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[..., 2]
        shadow_floor = min(
            shadow_floor, int(np.percentile(value[cavity], 5)))
    if shadow_floor < 60:
        raise AssertionError(f"oral cavity shadow floor {shadow_floor}")

    return dict(
        donor=(dental_rows.get("upper") or {}).get("donor"),
        donors={row: values["donor"]
                for row, values in dental_rows.items()},
        dental_rows=dental_rows,
        missing_dental_rows=missing,
        dental_poses=sum(values["dental_poses"]
                         for values in dental_rows.values()),
        comparison_poses=sum(values["comparison_poses"]
                             for values in dental_rows.values()),
        max_offset_px=max([values["max_offset_px"]
                           for values in dental_rows.values()] or [0.0]),
        worst_coverage=min([values["worst_coverage"]
                            for values in dental_rows.values()] or [1.0]),
        worst_extra=max([values["worst_extra"]
                         for values in dental_rows.values()] or [0.0]),
        worst_color_identity=min([values["worst_color_identity"]
                                  for values in dental_rows.values()] or [1.0]),
        lower_counts=(dental_rows.get("lower") or {}).get("counts", {}),
        weights=samples,
        shadow_p05=shadow_floor,
    )


def summary(result):
    nose_max = max(result["weights"][key] for key in
                   ("nose_tip", "nose_base", "nostril_left", "nostril_right"))
    row_summary = ", ".join(
        f"{row} {values['donor']} / {values['dental_poses']} poses"
        for row, values in result["dental_rows"].items()) or "none"
    for row in result.get("missing_dental_rows") or []:
        row_summary += f", {row} row not visible (lock skipped)"
    return (
        f"donors {row_summary}, {result['comparison_poses']} comparisons, "
        f"max offset {result['max_offset_px']}px, "
        f"worst coverage {result['worst_coverage']:.1%}, "
        f"worst extra {result['worst_extra']:.1%}, "
        f"donor color {result['worst_color_identity']:.1%}, "
        f"nose max {nose_max:.1f}%, "
        f"upper lip {result['weights']['upper_lip']:.1f}%, "
        f"shadow p05 {result['shadow_p05']}")
