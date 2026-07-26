"""Turn any uploaded portrait into a clean square keyframe for the viseme set.

Why a dedicated prep step: the first Vivieen set was built straight off a video
still whose head carried yaw + ~13 deg roll.  Every generated mouth then had to
fight the model's frontal prior.  Prep now measures the pose up front, crops a
face-centred square at the resolution the mouth actually needs, and reports
whether the source is frontal enough to build from.
"""
import os, json
import numpy as np, cv2
from . import face

KEY_SIZE = 1024          # gpt-image-2 native square - max mouth pixels
FACE_FRAC = 0.46         # face width as a fraction of the keyframe
EYE_LINE = 0.40          # eye line this far down the keyframe


def square_crop(img, lm):
    """Face-centred square crop box (x0, y0, size), clamped to the image."""
    H, W = img.shape[:2]
    oval = lm[face.FACE_OVAL]
    fw = float(oval[:, 0].max() - oval[:, 0].min())
    cx = float(oval[:, 0].mean())
    eye_y = float((lm[face.EYE_L_OUT][1] + lm[face.EYE_R_OUT][1]) / 2)

    size = int(round(fw / FACE_FRAC))
    size = min(size, H, W)
    x0 = int(round(cx - size / 2))
    y0 = int(round(eye_y - size * EYE_LINE))
    x0 = max(0, min(x0, W - size))
    y0 = max(0, min(y0, H - size))
    return x0, y0, size


def build_keyframe(src_path, out_path, diag_dir=None):
    img = cv2.imread(src_path, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"could not read image: {src_path}")
    lm, M = face.detect(img)
    if lm is None:
        raise ValueError("no face detected in the uploaded image")

    x0, y0, size = square_crop(img, lm)
    crop = img[y0:y0 + size, x0:x0 + size]
    interp = cv2.INTER_AREA if size > KEY_SIZE else cv2.INTER_LANCZOS4
    key = cv2.resize(crop, (KEY_SIZE, KEY_SIZE), interpolation=interp)
    cv2.imwrite(out_path, key, [cv2.IMWRITE_PNG_COMPRESSION, 3])

    klm, kM = face.detect(key)
    if klm is None:
        raise ValueError("face lost after cropping - try a less tightly cropped photo")
    m = face.metrics(klm, kM)

    lip = klm[face.OUTER_LIP]
    m["mouth_width_px"] = float(lip[:, 0].max() - lip[:, 0].min())
    m["crop"] = dict(x0=x0, y0=y0, size=size, source=[int(img.shape[1]), int(img.shape[0])])
    m["warnings"] = warnings_for(m)

    if diag_dir:
        os.makedirs(diag_dir, exist_ok=True)
        vis = key.copy()
        for idx, col in ((face.OUTER_LIP, (120, 255, 140)), (face.EYE_L, (255, 190, 90)),
                         (face.EYE_R, (255, 190, 90)), (face.RIGID, (110, 130, 255))):
            for p in klm[idx]:
                cv2.circle(vis, tuple(np.int32(p)), 2, col, -1)
        txt = (f"yaw {m['yaw']:+.1f}  pitch {m['pitch']:+.1f}  roll {m['roll']:+.1f}   "
               f"foreshortening {m['foreshortening']:.2f}   mouth {m['mouth_width_px']:.0f}px")
        cv2.rectangle(vis, (0, 0), (KEY_SIZE, 40), (18, 18, 22), -1)
        cv2.putText(vis, txt, (12, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (120, 255, 140), 1, cv2.LINE_AA)
        cv2.imwrite(os.path.join(diag_dir, "01_keyframe_landmarks.jpg"), vis,
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
    return m


def warnings_for(m):
    """Human-readable quality gates - shown in the UI before a build is spent."""
    w = []
    if m["yaw"] is not None and abs(m["yaw"]) > 8:
        w.append(f"head is turned {abs(m['yaw']):.0f} deg off-axis - a front-facing photo "
                 f"gives noticeably better mouth shapes")
    if m["roll"] is not None and abs(m["roll"]) > 8:
        w.append(f"head is tilted {abs(m['roll']):.0f} deg")
    if abs(m["foreshortening"] - 1.0) > 0.25:
        w.append(f"mouth is foreshortened (ratio {m['foreshortening']:.2f}, frontal is 1.00)")
    if m["mouth_width_px"] < 120:
        w.append(f"mouth is only {m['mouth_width_px']:.0f}px wide - crop tighter on the face")
    return w
