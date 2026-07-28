"""Amplitude QA.

Composition quality (does the head stay put?) was already measured.  This adds
the other half: does the mouth open a PLAUSIBLE amount?  The first pass looked
correctly aligned and still wrong, because the prompts described citation-form
articulation and the model rendered someone shouting.

Aperture is measured between the inner lip centres and normalised by mouth
width, so it is scale- and crop-independent.
"""
import os
import numpy as np, cv2
from . import face, visemes

IN_UP, IN_LO, C_L, C_R = 13, 14, 61, 291
APERTURE_DETECTOR_EPSILON = 0.002


def _aperture_within_limit(ratio, maximum):
    """Allow only subpixel landmark jitter at a mouth-scale ratio."""
    return ratio <= maximum + APERTURE_DETECTOR_EPSILON


def mouth_metrics(lm):
    w = float(np.linalg.norm(lm[C_L] - lm[C_R]))
    ap = float(np.linalg.norm(lm[IN_UP] - lm[IN_LO]))
    return dict(width=w, aperture=ap, ratio=ap / w if w else 0.0)


def audit(keyframe_path, viseme_dir, log=None, names=None):
    key = cv2.imread(keyframe_path)
    klm, _ = face.detect(key)
    neutral_w = float(np.linalg.norm(klm[C_L] - klm[C_R]))

    rows, over = [], []
    for name in (names or visemes.ORDER):
        p = os.path.join(viseme_dir, f"v_{name}.jpg")
        if not os.path.exists(p):
            continue
        lm, _ = face.detect(cv2.imread(p))
        if lm is None:
            continue
        m = mouth_metrics(lm)
        max_ratio, want_w = visemes.TARGETS.get(name, (1.0, 1.0))
        wr = m["width"] / neutral_w if neutral_w else 1.0
        aperture_ok = _aperture_within_limit(m["ratio"], max_ratio)
        ok = aperture_ok and abs(wr - want_w) <= 0.12
        row = dict(name=name, ratio=round(m["ratio"], 3), max_ratio=max_ratio,
                   width_ratio=round(wr, 3), want_width=want_w, ok=bool(ok))
        rows.append(row)
        if not ok:
            over.append(row)
        if log:
            why = "" if ok else ("  <-- too open" if not aperture_ok
                                 else "  <-- width off")
            log(f"  {name:7s} aperture {m['ratio']:.3f} / {max_ratio:.2f}   "
                f"width {wr:.2f} / {want_w:.2f}{why}")
    return rows, over
