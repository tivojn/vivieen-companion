"""Regressions for blink duplication and speech-driven upper-lash shimmer.

The lid sprite must erase every open-lash pixel during a blink. The gaze sprite
must do the opposite while the eye is open: move the eyeball without owning a
single dark lash pixel, or quantized VOR states make the upper lid shake.
"""
import os
import sys

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from studio import blink, build as reg, expression, face


def _old_build(key, shut):
    klm, _ = face.detect(key)
    blm, _ = face.detect(shut)
    scale = max(key.shape[:2]) / 1024.0
    states = [(i + 1) / blink.N_STATES for i in range(blink.N_STATES)]
    result = {"states": states, "eyes": {}}
    for side in blink.SIDES:
        mask = face.hull_mask(key.shape, klm, blink.EYE[side],
                              dilate=int(11 * scale) | 1)
        alpha = cv2.GaussianBlur(mask, (0, 0), 5 * scale).astype(np.float32) / 255.0
        box = blink._box(alpha, int(10 * scale), key.shape)
        result["eyes"][side] = {
            "box": [int(value) for value in box],
            "patches": [blink.lid_state(key, shut, klm, blm, side, state,
                                         scale, box, alpha) for state in states],
        }
    return result


def _closed(key, lids):
    frame = key.copy()
    index = len(lids["states"]) - 1
    for side in blink.SIDES:
        blink.paste(frame, lids, side, index)
    return frame


def _residual(key, shut, rendered, landmarks, side):
    scale = max(key.shape[:2]) / 1024.0
    search = face.hull_mask(key.shape, landmarks, blink.EYE[side],
                            dilate=int(35 * scale) | 1) > 0
    key_gray = cv2.cvtColor(key, cv2.COLOR_BGR2GRAY).astype(np.float32)
    shut_gray = cv2.cvtColor(shut, cv2.COLOR_BGR2GRAY).astype(np.float32)
    out_gray = cv2.cvtColor(rendered, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lash = search & ((shut_gray - key_gray) > 14)
    source = (shut_gray - key_gray)[lash]
    remaining = np.maximum(shut_gray - out_gray, 0)[lash]
    return float(remaining.sum() / max(source.sum(), 1.0))


def _outer_score(image, landmarks):
    scale = max(image.shape[:2]) / 1024.0
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    local = cv2.GaussianBlur(gray, (0, 0), 5.0 * scale)
    contrast = np.maximum(local - gray - 4.0, 0.0)
    return float(sum(contrast[blink._outer_lash_mask(
        image, landmarks, side, scale) > 0].sum() for side in blink.SIDES))


def _brow_delta(key, rendered, landmarks):
    mask = np.zeros(key.shape[:2], np.uint8)
    scale = max(key.shape[:2]) / 1024.0
    for side in blink.SIDES:
        part = face.hull_mask(key.shape, landmarks, blink.BROW[side],
                              dilate=int(5 * scale) | 1)
        mask = cv2.bitwise_or(mask, part)
    delta = np.abs(rendered.astype(np.float32) - key.astype(np.float32)).mean(2)
    return float(delta[mask > 0].mean())


def _upper_lash_mask(key, landmarks, side):
    scale = max(key.shape[:2]) / 1024.0
    gray = cv2.cvtColor(key, cv2.COLOR_BGR2GRAY).astype(np.float32)
    local = cv2.GaussianBlur(gray, (0, 0), 5.0 * scale)
    rows, cols = np.mgrid[:gray.shape[0], :gray.shape[1]]
    points = landmarks[blink.UPPER[side]]
    line = blink._line(points, np.arange(gray.shape[1]))
    band = ((cols >= points[:, 0].min() - 8.0 * scale) &
            (cols <= points[:, 0].max() + 8.0 * scale) &
            (rows >= line[None, :] - 15.0 * scale) &
            (rows <= line[None, :] + 2.0 * scale))
    return band & ((local - gray) > 4.0)


def _gaze_lash_delta(key, landmarks, side):
    scale = max(key.shape[:2]) / 1024.0
    ball = expression._eyeball_mask(key.shape, landmarks, side, scale)
    box = blink._box(ball, int(7 * scale), key.shape)
    x, y, width, height = box
    roi = key[y:y + height, x:x + width].astype(np.float32)
    lash = _upper_lash_mask(key, landmarks, side)[y:y + height, x:x + width]
    worst_mean = worst_max = 0.0
    for dx, dy in ((-1.5, -0.75), (-1.5, 0.75),
                   (1.5, -0.75), (1.5, 0.75)):
        patch = expression.gaze_state(
            key, landmarks, side, dx, dy, scale, box, ball).astype(np.float32)
        alpha = patch[..., 3:4] / 255.0
        rendered = roi * (1.0 - alpha) + patch[..., :3] * alpha
        delta = np.abs(rendered - roi).max(2)[lash]
        worst_mean = max(worst_mean, float(delta.mean()))
        worst_max = max(worst_max, float(delta.max()))
    return worst_mean, worst_max


def _label(image, text):
    image = image.copy()
    cv2.rectangle(image, (0, 0), (image.shape[1], 34), (13, 14, 18), -1)
    cv2.putText(image, text, (12, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.53,
                (242, 240, 236), 1, cv2.LINE_AA)
    return image


def _proof(slug, key, raw_shut, cleaned_shut, old, fixed, landmarks):
    points = np.vstack([landmarks[blink.EYE[side]] for side in blink.SIDES])
    x0 = max(0, int(points[:, 0].min()) - 58)
    x1 = min(key.shape[1], int(points[:, 0].max()) + 58)
    y0 = max(0, int(points[:, 1].min()) - 72)
    y1 = min(key.shape[0], int(points[:, 1].max()) + 70)
    panels = [
        _label(key[y0:y1, x0:x1], "OPEN BASE"),
        _label(old[y0:y1, x0:x1], "BEFORE · DOUBLE LASH"),
        _label(fixed[y0:y1, x0:x1], "AFTER · CLEAN CORNER"),
        _label(raw_shut[y0:y1, x0:x1], "RAW CLOSED SOURCE"),
        _label(cleaned_shut[y0:y1, x0:x1], "CLEANED CLOSED SOURCE"),
    ]
    target_h = max(panel.shape[0] for panel in panels)
    panels = [cv2.copyMakeBorder(panel, 0, target_h - panel.shape[0], 0, 0,
                                 cv2.BORDER_CONSTANT, value=(13, 14, 18))
              for panel in panels]
    sheet = np.hstack(panels)
    out = os.path.join(ROOT, "qa", "proof", f"blink_lash_{slug}.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cv2.imwrite(out, sheet)
    return out


def run():
    rows = []
    proof = None
    proofs = []
    active = reg.get_active()
    for avatar in reg.list_avatars():
        slug = avatar["slug"]
        directory = reg.adir(slug)
        key = cv2.imread(os.path.join(directory, "keyframe.png"))
        shut = cv2.imread(os.path.join(directory, "visemes", "v_blink.jpg"))
        if key is None or shut is None:
            continue
        landmarks, _ = face.detect(key)
        blink_landmarks, _ = face.detect(shut)
        cleaned_once = blink.clean_source(shut, blink_landmarks)
        cleaned_once_landmarks, _ = face.detect(cleaned_once)
        cleaned = blink.clean_source(cleaned_once, cleaned_once_landmarks)
        cleaned_landmarks, _ = face.detect(cleaned)
        old = _closed(key, _old_build(key, shut))
        fixed = _closed(
            key, blink.build(key, shut, log=lambda *_: None))
        old_values = [_residual(key, cleaned, old, landmarks, side)
                      for side in blink.SIDES]
        fixed_values = [_residual(key, cleaned, fixed, landmarks, side)
                        for side in blink.SIDES]
        before_outer = _outer_score(shut, blink_landmarks)
        after_outer = _outer_score(cleaned, cleaned_landmarks)
        brow = _brow_delta(key, fixed, landmarks)
        gaze = max((_gaze_lash_delta(key, landmarks, side)
                    for side in blink.SIDES), key=lambda value: value[0])
        rows.append((slug, max(old_values), max(fixed_values),
                     before_outer, after_outer, brow, *gaze))
        avatar_proof = _proof(
            slug, key, shut, cleaned, old, fixed, landmarks)
        proofs.append(avatar_proof)
        if slug == active:
            proof = avatar_proof

    print("avatar                         old lash   fixed lash   outer line     brow  gaze mean/max")
    for (slug, old_value, fixed_value, before_outer, after_outer,
         brow, gaze_mean, gaze_max) in rows:
        print(f"{slug:<30} {old_value:8.1%} {fixed_value:11.1%} "
              f"{before_outer:5.0f}->{after_outer:<5.0f} {brow:9.3f} "
              f"{gaze_mean:8.3f}/{gaze_max:.1f}")
        assert fixed_value < 0.07, (slug, fixed_value)
        assert fixed_value < old_value * 0.36, (slug, old_value, fixed_value)
        assert after_outer <= max(12.0, before_outer * 0.45), (
            slug, before_outer, after_outer)
        assert brow < 0.35, (slug, brow)
        assert gaze_mean < 0.02, (slug, gaze_mean)
        assert gaze_max < 1.0, (slug, gaze_max)
    assert proof and os.path.exists(proof)
    print(f"PASS · active proof: {proof}")
    print(f"PASS · {len(proofs)} avatar proofs written")
    return rows, proof


if __name__ == "__main__":
    run()
