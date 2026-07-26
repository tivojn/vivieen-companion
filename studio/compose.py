"""Pose-lock + region-only composite.

The generator always re-renders the WHOLE frame and always re-poses the head a
little.  Swapping whole frames therefore produces global flicker plus a head
that jitters between shapes.  (Global phase correlation will report zero drift
and fool you - the unchanged background dominates.  Measure the HEAD with
landmarks.)

So: fit an affine from RIGID (non-mouth) landmarks only, warp the render onto
the keyframe's face frame, then cut out ONLY the mouth (or the eyes for a
blink), tone-match it to the surrounding untouched skin and feather it onto the
pristine keyframe.  The viseme's mouth SHAPE survives; its head-pose error does
not.  Every output frame is then pixel-identical to the keyframe outside the
mask, which is what makes temporal cross-blending possible later.
"""
import os, json
import numpy as np, cv2
from . import face, visemes

FEATHER = 9
INNER_MOUTH = [78, 95, 88, 178, 87, 14, 317, 402, 318, 324,
               308, 415, 310, 311, 312, 13, 82, 81, 80, 191]
TEETH_DONOR = "SS"
TEETH_SHAPES = {"FF", "TH", "DD", "nn", "kk", "CH", "SS", "ah", "eh", "ih"}


def _mouth_cavity(shape, lm):
    mask = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(mask, [lm[INNER_MOUTH].astype(np.int32)], 255)
    return mask


def _tooth_mask(img, cavity, lm=None, upper_only=False):
    """Segment photographic teeth only inside the inner-lip opening."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    selected = ((cavity > 0) & (hsv[..., 2] > 108) &
                (hsv[..., 1] < 105) & (lab[..., 0] > 112))
    if upper_only and lm is not None:
        rows = np.indices(cavity.shape)[0]
        selected &= rows < float((lm[13, 1] + lm[14, 1]) * 0.5) + 3.0
    mask = selected.astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    clean = np.zeros_like(mask)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] >= 4:
            clean[labels == index] = 255
    return clean


def canonicalize_teeth(viseme_dir, diag_dir=None, log=print):
    """Lock the upper row while preserving jaw-attached lower teeth."""
    donor_path = os.path.join(viseme_dir, f"v_{TEETH_DONOR}.jpg")
    donor = cv2.imread(donor_path)
    if donor is None:
        log("  dental lock skipped: SS donor missing")
        return []
    donor_lm, _ = face.detect(donor)
    if donor_lm is None:
        log("  dental lock skipped: no face in SS donor")
        return []
    donor_cavity = _mouth_cavity(donor.shape, donor_lm)
    master = _tooth_mask(donor, donor_cavity, donor_lm, upper_only=True)
    if int(np.count_nonzero(master)) < 20:
        log("  dental lock skipped: SS tooth edge not found")
        return []
    if diag_dir:
        cv2.imwrite(os.path.join(diag_dir, "04_teeth_master.png"), master)

    report = []
    for name in visemes.ORDER:
        if name not in TEETH_SHAPES or name == TEETH_DONOR:
            continue
        path = os.path.join(viseme_dir, f"v_{name}.jpg")
        img = cv2.imread(path)
        if img is None:
            continue
        lm, _ = face.detect(img)
        if lm is None:
            continue
        cavity = _mouth_cavity(img.shape, lm)
        generated = _tooth_mask(img, cavity, lm, upper_only=True)

        rows = np.indices(cavity.shape)[0]
        upper_zone = ((cavity > 0) &
                      (rows < float((lm[13, 1] + lm[14, 1]) * 0.5) + 3.0))
        remove = cv2.dilate(generated, np.ones((3, 3), np.uint8))
        remove = np.where(upper_zone, remove, 0).astype(np.uint8)
        work = cv2.inpaint(img, remove, 2.0, cv2.INPAINT_TELEA).astype(np.float32)

        reveal = cv2.bitwise_and(master,
                                 cv2.erode(cavity, np.ones((2, 2), np.uint8)))
        reveal_alpha = cv2.GaussianBlur(reveal, (0, 0), .55).astype(np.float32) / 255.0
        work = (work * (1 - reveal_alpha[..., None]) +
                donor.astype(np.float32) * reveal_alpha[..., None])
        cv2.imwrite(path, np.clip(work, 0, 255).astype(np.uint8),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        row = dict(name=name, removed_upper=int(np.count_nonzero(generated)),
                   revealed=int(np.count_nonzero(reveal)))
        report.append(row)
        log(f"  {name:7s} upper lock removed {row['removed_upper']:4d}px   "
            f"revealed {row['revealed']:4d}px")
    return report


def soften_oral_shadows(viseme_dir, log=print):
    """Soften near-black cavity pixels and ink-like inner-lip contours."""
    report = []
    cavity_target = np.array([52.0, 58.0, 98.0], np.float32)
    contour_target = np.array([72.0, 78.0, 128.0], np.float32)
    kernel = np.ones((5, 5), np.uint8)
    for name in visemes.ORDER:
        if name in visemes.EYE_SHAPES:
            continue
        path = os.path.join(viseme_dir, f"v_{name}.jpg")
        img = cv2.imread(path)
        if img is None:
            continue
        lm, _ = face.detect(img)
        if lm is None:
            continue
        cavity = _mouth_cavity(img.shape, lm)
        value = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., 2].astype(np.float32)
        darkness = np.clip((105.0 - value) / 105.0, 0.0, 1.0)
        region = cv2.GaussianBlur(cavity, (0, 0), .65).astype(np.float32) / 255.0
        alpha = .84 * np.power(darkness, 1.35) * region
        work = (img.astype(np.float32) * (1.0 - alpha[..., None]) +
                cavity_target[None, None, :] * alpha[..., None])

        contour = cv2.subtract(cv2.dilate(cavity, kernel),
                               cv2.erode(cavity, kernel))
        contour = cv2.GaussianBlur(contour, (0, 0), .7).astype(np.float32) / 255.0
        work_value = cv2.cvtColor(np.clip(work, 0, 255).astype(np.uint8),
                                  cv2.COLOR_BGR2HSV)[..., 2].astype(np.float32)
        contour_darkness = np.clip((115.0 - work_value) / 115.0, 0.0, 1.0)
        contour_alpha = .45 * np.power(contour_darkness, 1.2) * contour
        work = (work * (1.0 - contour_alpha[..., None]) +
                contour_target[None, None, :] * contour_alpha[..., None])
        work = np.clip(work, 0, 255).astype(np.uint8)

        before = int(np.percentile(value[cavity > 0], 5))
        after_value = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)[..., 2]
        after = int(np.percentile(after_value[cavity > 0], 5))
        cv2.imwrite(path, work, [cv2.IMWRITE_JPEG_QUALITY, 95])
        report.append(dict(name=name, shadow_p05_before=before,
                           shadow_p05_after=after))
        log(f"  {name:7s} oral shadow p05 {before:3d} -> {after:3d}")
    return report


def _masks(key, klm):
    H, W = key.shape[:2]
    s = max(H, W) / 1024.0
    face_m = face.hull_mask(key.shape, klm, face.FACE_OVAL)
    face_m = cv2.erode(face_m, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (int(23 * s) | 1, int(23 * s) | 1)))

    mouth = face.hull_mask(key.shape, klm, face.OUTER_LIP + face.CHIN,
                           dilate=int(41 * s) | 1)
    mouth = cv2.bitwise_and(mouth, face_m)

    eyes = np.zeros((H, W), np.uint8)
    for idx in (face.EYE_L + face.BROW_L, face.EYE_R + face.BROW_R):
        eyes = cv2.bitwise_or(eyes, face.hull_mask(key.shape, klm, idx,
                                                   dilate=int(21 * s) | 1))
    eyes = cv2.bitwise_and(eyes, face_m)
    return dict(mouth=mouth, eyes=eyes), face_m


def _alpha_ring(mask, face_m, scale):
    alpha = cv2.GaussianBlur(mask, (0, 0), FEATHER * scale).astype(np.float32) / 255.0
    k1 = np.ones((int(35 * scale) | 1,) * 2, np.uint8)
    k2 = np.ones((int(9 * scale) | 1,) * 2, np.uint8)
    ring = cv2.bitwise_and(cv2.dilate(mask, k1), cv2.bitwise_not(cv2.dilate(mask, k2))) > 0
    ring = np.logical_and(ring, face_m > 0)
    return alpha, ring


def compose_all(keyframe_path, raw_dir, out_dir, diag_dir=None, log=print):
    key = cv2.imread(keyframe_path)
    H, W = key.shape[:2]
    scale = max(H, W) / 1024.0
    klm, kM = face.detect(key)
    if klm is None:
        raise ValueError("no face in keyframe")
    kmet = face.metrics(klm, kM)

    masks, face_m = _masks(key, klm)
    prepared = {k: _alpha_ring(m, face_m, scale) for k, m in masks.items()}
    os.makedirs(out_dir, exist_ok=True)
    if diag_dir:
        os.makedirs(diag_dir, exist_ok=True)
        cv2.imwrite(os.path.join(diag_dir, "02_mask_mouth.png"),
                    (prepared["mouth"][0] * 255).astype(np.uint8))
        cv2.imwrite(os.path.join(diag_dir, "03_mask_eyes.png"),
                    (prepared["eyes"][0] * 255).astype(np.uint8))

    kl = key.astype(np.float32)
    report = []
    for name in visemes.ORDER:
        src_path = os.path.join(raw_dir, f"v_{name}.png")
        if not os.path.exists(src_path):
            src_path = os.path.join(raw_dir, f"v_{name}.jpg")
        if not os.path.exists(src_path):
            log(f"  {name}: raw render missing, skipped")
            continue
        src = cv2.imread(src_path)
        if src.shape[:2] != (H, W):
            src = cv2.resize(src, (W, H), interpolation=cv2.INTER_LANCZOS4)
        slm, sM = face.detect(src)
        if slm is None:
            log(f"  {name}: no face in render, skipped")
            continue

        M, _ = cv2.estimateAffine2D(slm[face.RIGID], klm[face.RIGID],
                                    method=cv2.LMEDS, refineIters=50)
        if M is None:
            M = cv2.estimateAffinePartial2D(slm[face.RIGID], klm[face.RIGID])[0]
        warped = cv2.warpAffine(src, M, (W, H), flags=cv2.INTER_LANCZOS4,
                                borderMode=cv2.BORDER_REPLICATE)
        proj = (M[:, :2] @ slm[face.RIGID].T).T + M[:, 2]
        resid = float(np.linalg.norm(proj - klm[face.RIGID], axis=1).mean())

        alpha, ring = prepared["eyes" if name in visemes.EYE_SHAPES else "mouth"]
        wl = warped.astype(np.float32)
        off = kl[ring].mean(axis=0) - wl[ring].mean(axis=0)
        wl = np.clip(wl + off, 0, 255)
        out = (kl * (1 - alpha[..., None]) + wl * alpha[..., None]).astype(np.uint8)
        cv2.imwrite(os.path.join(out_dir, f"v_{name}.jpg"), out,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])

        d = np.abs(out.astype(np.float32) - kl).mean(axis=2)
        outside = float(d[alpha < 0.02].mean())
        olm, _ = face.detect(out)
        fs = float(face.foreshortening(olm)) if olm is not None else None
        report.append(dict(name=name, resid_px=round(resid, 2),
                           outside_delta=round(outside, 4),
                           foreshortening=None if fs is None else round(fs, 3),
                           tone_shift=[round(float(v), 1) for v in off]))
        log(f"  {name:7s} rigid residual {resid:5.2f}px   off-region delta {outside:.4f}"
            + (f"   foreshortening {fs:.2f}" if fs else ""))

    teeth = canonicalize_teeth(out_dir, diag_dir, log)
    oral_shadows = soften_oral_shadows(out_dir, log)
    if diag_dir:
        json.dump(dict(keyframe=kmet, visemes=report, teeth=teeth,
                       oral_shadows=oral_shadows),
                  open(os.path.join(diag_dir, "compose.json"), "w"), indent=1)
    return report, kmet
