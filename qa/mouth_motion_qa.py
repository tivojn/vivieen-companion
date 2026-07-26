"""Regression for photographic-mouth cadence and skull-fixed upper teeth."""
import os, sys
import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "server"))

from studio import compose, face
import visemes as speech_visemes


def run(slug="vivieen-front"):
    directory = os.path.join(ROOT, "avatars", slug, "visemes")
    donor = cv2.imread(os.path.join(directory, "v_SS.jpg"))
    if donor is None:
        raise AssertionError(f"{slug}: missing SS dental donor")
    donor_lm, _ = face.detect(donor)
    donor_cavity = compose._mouth_cavity(donor.shape, donor_lm)
    master = compose._tooth_mask(donor, donor_cavity, donor_lm, upper_only=True)
    master_near = cv2.dilate(master, np.ones((7, 7), np.uint8))
    row_grid = np.indices(master.shape)[0]
    upper_row_limit = int(np.where(master > 0)[0].max()) + 1

    rows = []
    lower_counts = {}
    for name in sorted(compose.TEETH_SHAPES - {compose.TEETH_DONOR}):
        img = cv2.imread(os.path.join(directory, f"v_{name}.jpg"))
        if img is None:
            continue
        lm, _ = face.detect(img)
        cavity = compose._mouth_cavity(img.shape, lm)
        actual = compose._tooth_mask(img, cavity)
        actual_upper = np.where(row_grid <= upper_row_limit, actual, 0).astype(np.uint8)
        reveal = cv2.bitwise_and(master,
                                 cv2.erode(cavity, np.ones((2, 2), np.uint8)))
        expected = int(np.count_nonzero(reveal))
        if expected < 8:
            continue
        actual_bool = actual_upper > 0
        extra = (np.count_nonzero(actual_bool & (master_near == 0)) /
                 max(1, np.count_nonzero(actual_bool)))
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
        rows.append((name, best_coverage, extra, best_offset))
        if name in {"ah", "eh"}:
            lower_counts[name] = int(np.count_nonzero(
                (actual > 0) & (row_grid > upper_row_limit + 1)))

    if len(rows) < 6:
        raise AssertionError(f"{slug}: only {len(rows)} tooth-bearing poses detected")
    worst_offset = max(abs(row[3]) for row in rows)
    worst_coverage = min(row[1] for row in rows)
    worst_extra = max(row[2] for row in rows)
    if worst_offset > 1:
        raise AssertionError(f"{slug}: upper teeth offset {worst_offset}px")
    if worst_coverage < 0.70:
        raise AssertionError(f"{slug}: dental template coverage {worst_coverage:.1%}")
    if worst_extra > 0.10:
        raise AssertionError(f"{slug}: non-canonical upper pixels {worst_extra:.1%}")
    if min(lower_counts.values(), default=0) < 40:
        raise AssertionError(f"{slug}: lower incisors were erased {lower_counts}")

    shadow_floor = 255
    for name in compose.visemes.ORDER:
        if name in compose.visemes.EYE_SHAPES:
            continue
        img = cv2.imread(os.path.join(directory, f"v_{name}.jpg"))
        lm, _ = face.detect(img)
        cavity = compose._mouth_cavity(img.shape, lm) > 0
        value = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2]
        shadow_floor = min(shadow_floor, int(np.percentile(value[cavity], 5)))
    if shadow_floor < 60:
        raise AssertionError(f"{slug}: oral cavity shadow floor {shadow_floor}")

    resolved = speech_visemes._resolve([[0.0, "aa"], [.07, None], [.13, "E"]])
    if resolved != [[0.0, "aa"], [0.07, "E"]]:
        raise AssertionError(f"coarticulation resolution changed: {resolved}")

    print(f"PASS · {slug}: {len(rows)} dental poses, max offset {worst_offset}px, "
          f"worst coverage {worst_coverage:.1%}, worst extra {worst_extra:.1%}, "
          f"lower {lower_counts}, shadow p05 {shadow_floor}")


if __name__ == "__main__":
    run()
