"""Pose-lock generated frames and composite only speech-coupled regions.

Full-frame swaps create global flicker and head jitter. The lip and jaw core is
therefore transferred at full strength, while a softly weighted lower-face
envelope carries subtle mouth-corner, chin, nasolabial-fold and cheek motion.
Eyes and the upper face remain literal keyframe pixels.
"""
import os, json
import numpy as np, cv2
from . import face, rig, visemes

FEATHER = 9
LOWER_FACE_ALPHA = 0.62
NOSE_LOCK_STRENGTH = 1.0
NOSE_LOCK_FEATHER = 2.5
INNER_MOUTH = [78, 95, 88, 178, 87, 14, 317, 402, 318, 324,
               308, 415, 310, 311, 312, 13, 82, 81, 80, 191]
UPPER_TEETH_DONORS = ("SS", "eh", "ih", "ah", "kk", "TH", "DD", "nn", "CH", "FF", "RR")
LOWER_TEETH_DONORS = ("ih", "SS", "eh", "TH", "FF", "ah", "DD", "kk", "CH", "nn", "RR")
TEETH_DONORS = UPPER_TEETH_DONORS
DENTAL_ROWS = ("upper", "lower")
DENTAL_DONORS = {
    "upper": UPPER_TEETH_DONORS,
    "lower": LOWER_TEETH_DONORS,
}
LOWER_MOUTH_ANCHORS = [14, 17, 84, 181, 91, 146, 314, 405, 321, 375]
TEETH_SHAPES = {"FF", "TH", "DD", "nn", "kk", "CH", "SS", "ah", "eh", "ih"}
MIN_TEETH_PIXELS = {"upper": 20, "lower": 20}


def _mouth_cavity(shape, lm):
    mask = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(mask, [lm[INNER_MOUTH].astype(np.int32)], 255)
    return mask


def _dental_band(shape, lm, cavity=None):
    """The inner-mouth polygon routinely traces the lip line THROUGH the
    teeth on open-mouth renders, leaving most of a bright dental row outside
    the cavity - undetected, unremoved, and doubled under the pasted
    canonical row (gary66 `ah`: 14732 enamel px in the mouth, 174 inside the
    polygon). Grow the search band vertically, clamped to the outer-lip hull
    so it can never wander into skin."""
    if cavity is None:
        cavity = _mouth_cavity(shape, lm)
    ys = np.nonzero(cavity.max(axis=1))[0]
    if not len(ys):
        return cavity
    reach = max(9, int(round((int(ys[-1]) - int(ys[0])) * 0.6))) | 1
    band = cv2.dilate(cavity, np.ones((reach, 3), np.uint8))
    hull = np.zeros_like(cavity)
    cv2.fillPoly(hull, [cv2.convexHull(
        lm[face.OUTER_LIP].astype(np.int32))], 255)
    return cv2.bitwise_and(band, hull)


def _row_zone(cavity, lm, row):
    if row not in DENTAL_ROWS:
        raise ValueError(f"unknown dental row: {row}")
    rows = np.indices(cavity.shape)[0]
    split = int(round(float((lm[13, 1] + lm[14, 1]) * 0.5)))
    if row == "upper":
        selected = (cavity > 0) & (rows <= split)
    else:
        selected = (cavity > 0) & (rows > split)
    return selected.astype(np.uint8) * 255


def _tooth_mask(img, cavity, lm=None, upper_only=False, row=None):
    """Segment photographic teeth only inside the requested inner-mouth row."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    selected = ((cavity > 0) & (hsv[..., 2] > 108) &
                (hsv[..., 1] < 105) & (lab[..., 0] > 112))
    if upper_only:
        row = "upper"
    if row is not None:
        if lm is None:
            raise ValueError("landmarks are required for dental row segmentation")
        selected &= _row_zone(cavity, lm, row) > 0
    mask = selected.astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    clean = np.zeros_like(mask)
    for index in range(1, count):
        if stats[index, cv2.CC_STAT_AREA] >= 4:
            clean[labels == index] = 255
    return clean


def _select_tooth_donor(viseme_dir, row="upper"):
    """Pick the frame with the MOST complete detected row, not the first
    acceptable one: a fixed priority order elected SS's clenched, lip-shaded
    sliver as the canonical enamel while ah/eh held wide, well-lit rows."""
    if row not in DENTAL_ROWS:
        raise ValueError(f"unknown dental row: {row}")
    best = None
    best_pixels = 0
    for name in DENTAL_DONORS[row]:
        path = os.path.join(viseme_dir, f"v_{name}.jpg")
        donor = cv2.imread(path)
        if donor is None:
            continue
        donor_lm, _ = face.detect(donor)
        if donor_lm is None:
            continue
        band = _dental_band(donor.shape, donor_lm)
        master = _tooth_mask(donor, band, donor_lm, row=row)
        pixels = int(np.count_nonzero(master))
        if pixels >= MIN_TEETH_PIXELS[row] and pixels > best_pixels:
            best = name, donor, donor_lm, master
            best_pixels = pixels
    return best


def _select_dental_donors(viseme_dir):
    return {
        row: selected
        for row in DENTAL_ROWS
        if (selected := _select_tooth_donor(viseme_dir, row)) is not None
    }


def _tooth_plate(master):
    return cv2.dilate(
        master, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 3)))


def _lower_row_transform(donor_lm, target_lm):
    source = np.asarray(donor_lm[LOWER_MOUTH_ANCHORS], np.float32)
    target = np.asarray(target_lm[LOWER_MOUTH_ANCHORS], np.float32)

    def translation():
        delta = (target - source).mean(axis=0)
        return np.array([[1.0, 0.0, delta[0]],
                         [0.0, 1.0, delta[1]]], np.float32)

    if (not np.isfinite(source).all() or not np.isfinite(target).all() or
            float(np.linalg.norm(source - source.mean(axis=0), axis=1).max()) < 1.0):
        return translation()
    transform, _ = cv2.estimateAffinePartial2D(
        source, target, method=cv2.LMEDS)
    if transform is None or not np.isfinite(transform).all():
        return translation()
    scale = float(np.hypot(transform[0, 0], transform[1, 0]))
    if not 0.78 <= scale <= 1.28:
        return translation()
    return transform.astype(np.float32)


def _row_assets(donor, donor_lm, master, target_lm, row):
    if row == "upper":
        return donor, master, _tooth_plate(master)
    transform = _lower_row_transform(donor_lm, target_lm)
    height, width = master.shape
    donor_frame = cv2.warpAffine(
        donor, transform, (width, height), flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REPLICATE)
    transformed = cv2.warpAffine(
        master, transform, (width, height), flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return donor_frame, transformed, _tooth_plate(transformed)


def canonicalize_teeth(viseme_dir, diag_dir=None, log=print, selected=None):
    """Lock skull-attached upper teeth and jaw-attached lower teeth."""
    if selected is None:
        selected = _select_dental_donors(viseme_dir)
    if not selected:
        log("  dental lock skipped: no canonical dental rows found")
        return []
    missing = [row for row in DENTAL_ROWS if row not in selected]
    if missing:
        log(f"  dental lock warning: no canonical {', '.join(missing)} row found")
    donor_summary = ", ".join(
        f"{row} {values[0]} ({np.count_nonzero(values[3])}px)"
        for row, values in selected.items())
    log(f"  dental lock donors: {donor_summary}")
    dental_diag_dir = None
    if diag_dir:
        dental_diag_dir = os.path.join(diag_dir, "dental")
        os.makedirs(dental_diag_dir, exist_ok=True)
        for index, (row, values) in enumerate(selected.items()):
            master = values[3]
            cv2.imwrite(
                os.path.join(diag_dir, f"{4 + index * 2:02d}_teeth_{row}_master.png"),
                master)
            cv2.imwrite(
                os.path.join(diag_dir, f"{5 + index * 2:02d}_teeth_{row}_plate.png"),
                _tooth_plate(master))

    report = []
    for name in visemes.ORDER:
        if name not in TEETH_SHAPES:
            continue
        path = os.path.join(viseme_dir, f"v_{name}.jpg")
        img = cv2.imread(path)
        if img is None:
            continue
        lm, _ = face.detect(img)
        if lm is None:
            continue
        cavity = _mouth_cavity(img.shape, lm)
        band = _dental_band(img.shape, lm, cavity)
        rows = {}
        remove = np.zeros(cavity.shape, np.uint8)
        for row, values in selected.items():
            donor_name, donor, donor_lm, master = values
            donor_frame, canonical, plate = _row_assets(
                donor, donor_lm, master, lm, row)
            zone = _row_zone(band, lm, row)
            generated = _tooth_mask(img, band, lm, row=row)
            replace = name != donor_name
            if replace:
                row_remove = cv2.bitwise_and(
                    cv2.dilate(generated, np.ones((3, 3), np.uint8)), zone)
                remove = cv2.bitwise_or(remove, row_remove)
            rows[row] = dict(
                donor=donor_name, donor_frame=donor_frame,
                canonical=canonical, plate=plate, zone=zone,
                generated=generated, replace=replace)

        if np.any(remove):
            work = cv2.inpaint(img, remove, 2.0, cv2.INPAINT_TELEA).astype(np.float32)
        else:
            work = img.astype(np.float32)
        cavity_inner = cv2.erode(band, np.ones((2, 2), np.uint8))
        details = {}
        for row, values in rows.items():
            reveal = cv2.bitwise_and(values["plate"], cavity_inner)
            reveal = cv2.bitwise_and(reveal, values["zone"])
            enamel = cv2.bitwise_and(values["canonical"], cavity_inner)
            enamel = cv2.bitwise_and(enamel, values["zone"])
            if dental_diag_dir:
                reference = np.zeros_like(img)
                reference[enamel > 0] = values["donor_frame"][enamel > 0]
                cv2.imwrite(os.path.join(
                    dental_diag_dir, f"{row}_{name}_mask.png"), enamel)
                cv2.imwrite(os.path.join(
                    dental_diag_dir, f"{row}_{name}_zone.png"), values["zone"])
                cv2.imwrite(os.path.join(
                    dental_diag_dir, f"{row}_{name}_reference.png"), reference)
            if values["replace"]:
                reveal_alpha = cv2.GaussianBlur(
                    reveal, (0, 0), .55).astype(np.float32) / 255.0
                soft_zone = cv2.GaussianBlur(
                    values["zone"], (0, 0), .45).astype(np.float32) / 255.0
                reveal_alpha *= soft_zone
                reveal_alpha[enamel > 0] = 1.0
                work = (work * (1 - reveal_alpha[..., None]) +
                        values["donor_frame"].astype(np.float32) *
                        reveal_alpha[..., None])
            details[row] = dict(
                donor=values["donor"],
                removed=(int(np.count_nonzero(values["generated"]))
                         if values["replace"] else 0),
                revealed=int(np.count_nonzero(enamel)),
            )
        cv2.imwrite(path, np.clip(work, 0, 255).astype(np.uint8),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        report.append(dict(name=name, rows=details))
        detail = "   ".join(
            f"{row} removed {values['removed']:4d}px / revealed {values['revealed']:4d}px"
            for row, values in details.items())
        log(f"  {name:7s} {detail}")
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
        dental = _tooth_mask(img, cavity)
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
        work[dental > 0] = img[dental > 0]

        before = int(np.percentile(value[cavity > 0], 5))
        after_value = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)[..., 2]
        after = int(np.percentile(after_value[cavity > 0], 5))
        cv2.imwrite(path, work, [cv2.IMWRITE_JPEG_QUALITY, 95])
        report.append(dict(name=name, shadow_p05_before=before,
                           shadow_p05_after=after))
        log(f"  {name:7s} oral shadow p05 {before:3d} -> {after:3d}")
    return report


def _regional_mask(key, landmarks, groups, dilate, face_mask, eye_guard):
    mask = np.zeros(key.shape[:2], np.uint8)
    for group in groups:
        mask = cv2.bitwise_or(
            mask, face.hull_mask(key.shape, landmarks, group, dilate=dilate))
    mask = cv2.bitwise_and(mask, face_mask)
    return cv2.bitwise_and(mask, cv2.bitwise_not(eye_guard))


def _masks(key, klm, profile=None):
    profile = rig.normalize(profile)
    height, width = key.shape[:2]
    scale = max(height, width) / 1024.0
    face_mask = face.hull_mask(key.shape, klm, face.FACE_OVAL)
    face_mask = cv2.erode(face_mask, cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (int(23 * scale) | 1, int(23 * scale) | 1)))
    eye_guard = face.hull_mask(
        key.shape, klm, face.EYE_L + face.EYE_R,
        dilate=int(41 * scale) | 1)

    lip_core = _regional_mask(
        key, klm, [face.OUTER_LIP], int(31 * scale) | 1,
        face_mask, eye_guard)
    regions = {
        "jaw": _regional_mask(
            key, klm, [face.JAW_REGION], int(17 * scale) | 1,
            face_mask, eye_guard),
        "cheeks": _regional_mask(
            key, klm, [face.CHEEK_L, face.CHEEK_R],
            int(19 * scale) | 1, face_mask, eye_guard),
        "nasolabial": _regional_mask(
            key, klm, [face.NASOLABIAL_L, face.NASOLABIAL_R],
            int(13 * scale) | 1, face_mask, eye_guard),
    }
    support = np.zeros((height, width), np.uint8)
    for region in regions.values():
        support = cv2.bitwise_or(support, region)
    nose_core = face.hull_mask(
        key.shape, klm, face.NOSE_CORE, dilate=int(5 * scale) | 1)
    nose_base = face.hull_mask(
        key.shape, klm, face.NOSE_BASE, dilate=int(7 * scale) | 1)
    mouth = dict(kind="mouth", core=lip_core, regions=regions,
                 support=support, nose_core=nose_core,
                 nose_base=nose_base, profile=profile)

    eyes = np.zeros((height, width), np.uint8)
    for indices in (face.EYE_L + face.BROW_L,
                    face.EYE_R + face.BROW_R):
        eyes = cv2.bitwise_or(
            eyes, face.hull_mask(key.shape, klm, indices,
                                 dilate=int(21 * scale) | 1))
    eyes = cv2.bitwise_and(eyes, face_mask)
    return dict(mouth=mouth, eyes=eyes), face_mask


def _alpha_ring(mask, face_m, scale, profile=None):
    sigma = FEATHER * scale
    if isinstance(mask, dict) and mask.get("kind") == "mouth":
        profile = rig.normalize(profile or mask.get("profile"))
        core_alpha = cv2.GaussianBlur(
            mask["core"], (0, 0), sigma).astype(np.float32) / 255.0
        alpha = core_alpha * (profile["lips"] / 100.0)
        for name, region in mask["regions"].items():
            region_alpha = cv2.GaussianBlur(
                region, (0, 0), sigma * 1.35).astype(np.float32) / 255.0
            alpha = np.maximum(
                alpha, region_alpha * (profile[name] / 100.0))
        nose_base = cv2.GaussianBlur(
            mask["nose_base"], (0, 0), NOSE_LOCK_FEATHER * scale
        ).astype(np.float32) / 255.0
        nose_cap = profile["nose"] / 100.0
        alpha = (alpha * (1.0 - nose_base) +
                 np.minimum(alpha, nose_cap) * nose_base)
        alpha[mask["nose_base"] > 0] = np.minimum(
            alpha[mask["nose_base"] > 0], nose_cap)
        nose_core = cv2.GaussianBlur(
            mask["nose_core"], (0, 0), NOSE_LOCK_FEATHER * scale
        ).astype(np.float32) / 255.0
        alpha *= 1.0 - nose_core
        alpha[mask["nose_core"] > 0] = 0.0
        ring_base = mask["support"]
    elif isinstance(mask, tuple):
        core, support, nose_guard = mask
        core_alpha = cv2.GaussianBlur(
            core, (0, 0), sigma).astype(np.float32) / 255.0
        support_alpha = cv2.GaussianBlur(
            support, (0, 0), sigma * 1.35).astype(np.float32) / 255.0
        alpha = np.maximum(core_alpha, support_alpha * LOWER_FACE_ALPHA)
        nose_lock = cv2.GaussianBlur(
            nose_guard, (0, 0), NOSE_LOCK_FEATHER * scale
        ).astype(np.float32) / 255.0
        alpha *= 1.0 - NOSE_LOCK_STRENGTH * nose_lock
        ring_base = support
    else:
        alpha = cv2.GaussianBlur(
            mask, (0, 0), sigma).astype(np.float32) / 255.0
        ring_base = mask
    alpha = np.clip(alpha, 0.0, 1.0)
    k1 = np.ones((int(35 * scale) | 1,) * 2, np.uint8)
    k2 = np.ones((int(9 * scale) | 1,) * 2, np.uint8)
    ring = cv2.bitwise_and(
        cv2.dilate(ring_base, k1),
        cv2.bitwise_not(cv2.dilate(ring_base, k2))) > 0
    ring = np.logical_and(ring, face_m > 0)
    return alpha, ring


def compose_all(keyframe_path, raw_dir, out_dir, diag_dir=None, log=print,
                profile=None):
    key = cv2.imread(keyframe_path)
    H, W = key.shape[:2]
    scale = max(H, W) / 1024.0
    klm, kM = face.detect(key)
    if klm is None:
        raise ValueError("no face in keyframe")
    kmet = face.metrics(klm, kM)

    profile = rig.normalize(profile)
    masks, face_m = _masks(key, klm, profile)
    prepared = {
        name: _alpha_ring(mask, face_m, scale, profile)
        for name, mask in masks.items()
    }
    os.makedirs(out_dir, exist_ok=True)
    if diag_dir:
        os.makedirs(diag_dir, exist_ok=True)
        cv2.imwrite(os.path.join(diag_dir, "02_mask_mouth.png"),
                    (prepared["mouth"][0] * 255).astype(np.uint8))
        cv2.imwrite(os.path.join(diag_dir, "03_mask_eyes.png"),
                    (prepared["eyes"][0] * 255).astype(np.uint8))
        for name, region in masks["mouth"]["regions"].items():
            cv2.imwrite(os.path.join(diag_dir, f"02_mask_{name}.png"), region)

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

    dental_donors = _select_dental_donors(out_dir)
    oral_shadows = soften_oral_shadows(out_dir, log)
    teeth = canonicalize_teeth(
        out_dir, diag_dir, log, selected=dental_donors)
    if diag_dir:
        json.dump(dict(keyframe=kmet, visemes=report, teeth=teeth,
                       oral_shadows=oral_shadows, rig_profile=profile),
                  open(os.path.join(diag_dir, "compose.json"), "w"), indent=1)
    return report, kmet
