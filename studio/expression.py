"""Micro-expression layers: gaze and brow, synthesised from the keyframe alone.

A face whose mouth and eyelids move while everything else is frozen does not
read as alive - it reads as a corpse being puppeteered, and the strongest single
cause is the DEAD STARE.  Real eyes never hold still: they micro-saccade a few
times a second, drift between fixations, and look away when their owner is
thinking.  Second cause is the frozen brow: humans raise and knit their brows
constantly while speaking, and a brow that never moves over a mouth that does is
profoundly wrong.

Neither needs a generated image.  Both are small, local deformations of tissue
that is already in the keyframe:

  gaze - the iris is a rigid disc sliding across a featureless white sclera.
         Warp it: displacement is constant inside the iris and decays to zero
         before the lid margins, so the iris translates rigidly, the sclera
         compresses on one side and stretches on the other (invisible - it has
         no texture to betray it), and the lids do not move at all.

  brow  - a slab of skin that slides vertically, dragging the lid skin below it
         and fading out into the forehead above.  Raises are arch-weighted;
         lowers are weighted to the medial end and pull slightly inward, which
         is what corrugator actually does.

Both bake to small RGBA sprite strips, like the eyelids in blink.py.  Draw order
is base frame -> brow -> gaze -> lid, which is also the anatomical order: the
lid occludes the eyeball, so it must be painted last.
"""
import numpy as np, cv2
from . import face
from .blink import EYE, BROW, SIDES, UPPER, LOWER, _line, _box

IRIS = {"r": 468, "l": 473}                       # refined-landmark iris centres
IRIS_RING = {"r": [469, 470, 471, 472], "l": [474, 475, 476, 477]}
NOSE_X = 1                                        # nose tip: tells medial from lateral

# The resting gaze is locked to the lens, and the counter-rotation that holds
# that lock while the head moves is under a pixel and a half - one degree of
# eye rotation is only ~0.73px on this face.  So the CENTRE of the grid stays
# small and fine, quarter-pixel pitch, and the runtime snaps to the nearest
# state instead of blending: at that pitch the quantisation is invisible and
# the limbus is never cross-dissolved with itself.
#
# The FLANKS exist for directed attention - the eyes following the cursor.
# A glance that reads as "looking left" needs the iris to actually travel the
# sclera, several pixels, not a micro-tremor.  Out there the pitch coarsens:
# a directed glance is a saccade, and saccades jump.
GAZE_DX = ([-9.0, -7.5, -6.0, -4.8, -3.6, -2.4] +
           [round(-1.5 + 0.25 * i, 3) for i in range(13)] +
           [2.4, 3.6, 4.8, 6.0, 7.5, 9.0])                    # +-9px
GAZE_DY = [-3.5, -2.5, -1.5, -0.75, -0.375, 0.0, 0.375, 0.75, 1.5, 2.5, 3.5]
# Brow offsets, negative = knitted.  Subtle-anchor range, not big acting: half a
# pixel apart so the runtime can snap here too rather than ghost the brow hair.
BROW_DY = [round(-1.5 + 0.5 * i, 3) for i in range(11)]       # -1.5 .. +3.5
# Cheek raise, in px of lift at the lower lid margin.  Small on purpose - this
# is the warmth cue that rides under speech, not a smile.
CHEEK_UP = [0.0, 0.65, 1.3, 2.0, 2.7]


def neck(lm):
    """Where the head pivots on the neck, in keyframe px.

    Idle head movement is the head turning ON THE NECK, so it has to TAPER: the
    crown swings furthest, the chin much less, the shoulders barely at all.
    Translating the whole frame instead - head, shoulders, jacket, backdrop as
    one block - is what makes a talking head look stiff-necked; it reads as the
    camera moving, and a nod is not a nod if the shoulders come with it.

    Because a rigid rotation about a pivot is LINEAR in y, that taper is an
    affine transform: one draw call, no bands, no seams, and the foreshortening
    of a nod (chin moves less than brow, so the face shortens) falls out of it
    for free.  `ref` is where the taper equals 1 - the eyes, since the gaze
    counter-rotation is derived at exactly that height.
    """
    chin = float(lm[face.CHIN][:, 1].max())
    top = float(lm[face.FACE_OVAL][:, 1].min())
    eye = float(0.5 * (lm[IRIS["r"]][1] + lm[IRIS["l"]][1]))
    return dict(ref=round(eye, 1),
                pivot=round(chin + 0.38 * (chin - top), 1),
                x=round(float(lm[NOSE_X][0]), 1))


def _smoothstep(a, b, x):
    t = np.clip((x - a) / max(b - a, 1e-6), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _iris(lm, side):
    c = lm[IRIS[side]]
    r = float(np.linalg.norm(lm[IRIS_RING[side]] - c, axis=1).mean())
    return c, r


# ---- gaze ------------------------------------------------------------------

def _eyeball_mask(shape, lm, side, s):
    """The wet eye inset from lid margins, with lash contours hard-protected."""
    mask = face.hull_mask(shape, lm, EYE[side])
    kernel_size = max(int(3 * s) | 1, 3)
    mask = cv2.erode(mask, np.ones((kernel_size, kernel_size), np.uint8))
    alpha = cv2.GaussianBlur(mask, (0, 0), 1.6 * s).astype(np.float32) / 255.0

    guard = np.zeros(shape[:2], np.uint8)
    thickness = max(int(7 * s) | 1, 3)
    for contour in (UPPER[side], LOWER[side]):
        points = np.rint(lm[contour]).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(guard, [points], False, 255, thickness=thickness,
                      lineType=cv2.LINE_AA)
    alpha[guard > 0] = 0.0
    return alpha


def gaze_state(key, lm, side, dx, dy, s, box, ball):
    x0, y0, bw, bh = box
    c, r = _iris(lm, side)
    xs = np.arange(x0, x0 + bw, dtype=np.float32)
    ys = np.arange(y0, y0 + bh, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys)
    d = np.sqrt((gx - c[0]) ** 2 + (gy - c[1]) ** 2)

    # Rigid out past the limbus - the iris edge is the one hard edge in there and
    # a partial warp across it would ghost - then decayed to nothing well before
    # the corners and the caruncle.
    w = 1.0 - _smoothstep(1.15 * r, 1.9 * r, d)
    w = (w * ball[y0:y0 + bh, x0:x0 + bw]).astype(np.float32)   # and 0 at the lid margins

    warped = cv2.remap(key, (gx - dx * w).astype(np.float32),
                       (gy - dy * w).astype(np.float32),
                       cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
    base = key[y0:y0 + bh, x0:x0 + bw]
    # Only claim the pixels that actually moved.
    a = np.clip(w * 1.6, 0.0, 1.0).astype(np.float32)
    rgb = np.where(a[..., None] > 1e-3, warped, base)
    return np.dstack([rgb.astype(np.uint8), (a * 255).astype(np.uint8)])


# ---- brow ------------------------------------------------------------------

def _brow_alpha(shape, lm, side, s):
    m = face.hull_mask(shape, lm, BROW[side], dilate=int(17 * s) | 1)
    return cv2.GaussianBlur(m, (0, 0), 7 * s).astype(np.float32) / 255.0


def brow_state(key, lm, side, dy, s, box, alpha):
    x0, y0, bw, bh = box
    b = lm[BROW[side]]
    bx0, bx1 = float(b[:, 0].min()), float(b[:, 0].max())
    btop, bbot = float(b[:, 1].min()), float(b[:, 1].max())
    lash = float(_line(lm[UPPER[side]], np.linspace(bx0, bx1, 12)).min())
    medial_right = lm[NOSE_X][0] > 0.5 * (bx0 + bx1)   # which end points at the nose

    xs = np.arange(x0, x0 + bw, dtype=np.float32)
    ys = np.arange(y0, y0 + bh, dtype=np.float32)

    # along the brow: 0 at the medial end, 1 at the tail
    u = np.clip((xs - bx0) / max(bx1 - bx0, 1.0), 0.0, 1.0)
    if medial_right:
        u = 1.0 - u
    if dy >= 0:
        h = 0.55 + 0.45 * np.sin(np.pi * np.clip(u, 0, 1) ** 0.85)   # arch-weighted lift
        sway = np.zeros_like(u)
    else:
        h = 1.0 - 0.5 * u                                            # corrugator: medial
        sway = (1.0 - u) * (0.35 if medial_right else -0.35)
    h = h * (1.0 - _smoothstep(1.0, 1.22, np.abs(xs - 0.5 * (bx0 + bx1))
                               / max(0.5 * (bx1 - bx0), 1.0)))

    # across the brow: full over the hair, fading into the forehead and dying
    # before the lash line so it never fights the eyelid layer
    span = max(bbot - btop, 6.0)
    up = 1.0 - _smoothstep(btop - 0.35 * span, btop - 1.7 * span, ys)
    dn = 1.0 - _smoothstep(bbot + 0.15 * span, lash - 4.0 * s, ys)
    v = np.clip(np.minimum(up, dn), 0.0, 1.0)

    W = (v[:, None] * h[None, :]).astype(np.float32)
    gx, gy = np.meshgrid(xs, ys)
    warped = cv2.remap(key,
                       (gx - (sway[None, :] * v[:, None]) * abs(dy)).astype(np.float32),
                       (gy - dy * W).astype(np.float32),
                       cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
    base = key[y0:y0 + bh, x0:x0 + bw]
    a = (np.clip(W * 1.6, 0.0, 1.0) * alpha[y0:y0 + bh, x0:x0 + bw]).astype(np.float32)
    rgb = np.where(a[..., None] > 1e-3, warped, base)
    return np.dstack([rgb.astype(np.uint8), (a * 255).astype(np.uint8)])


# ---- cheek -----------------------------------------------------------------
# A cheek raise is the micro-expression that reads as warmth, and the obvious
# way to build it - warp the cheek - does not work.  Bare cheek skin has no
# texture, so a 2px shift of it is literally invisible; the same warp that sells
# the iris (hard limbus) and the brow (hair) buys nothing here.
#
# What actually shows a cheek raise in a portrait is the STRUCTURE ALONG ITS TOP
# EDGE: the lower lid margin lifts, the lash line rises with it, the infraorbital
# crease moves and deepens.  So this layer is anchored on the lower lid and fades
# downward over the malar eminence, rather than being centred on the cheek.

def _cheek_weight(shape, lm, side, s, avoid=None):
    H, W = shape[:2]
    c, _ = _iris(lm, side)
    corners = np.array([lm[face.MOUTH_L], lm[face.MOUTH_R]], np.float32)
    mc = corners[int(np.argmin(np.abs(corners[:, 0] - c[0])))]   # same-side corner
    lat = 1.0 if c[0] > lm[NOSE_X][0] else -1.0                  # which way is lateral
    span = float(np.linalg.norm(mc - c))

    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy = float(c[0] + lat * 0.10 * span), float(c[1] + 0.34 * span)
    rho = np.sqrt(((xs - cx) / (0.62 * span)) ** 2 + ((ys - cy) / (0.52 * span)) ** 2)
    w = 1.0 - _smoothstep(0.45, 1.0, rho)

    # ride the lower lid margin, and stop dead above it so the eyeball layer
    # underneath is never disturbed
    low = lm[LOWER[side]]
    lidy = _line(low, np.clip(xs[0], float(low[:, 0].min()), float(low[:, 0].max())))
    w = w * _smoothstep(-3.0 * s, 4.0 * s, ys - lidy[None, :])

    # and stay clear of whatever the viseme frames repaint, or this stamps
    # keyframe pixels over a moving mouth
    if avoid is None:
        avoid = face.hull_mask(shape, lm, face.OUTER_LIP,
                              dilate=int(34 * s) | 1).astype(np.float32) / 255.0
    # dilate BEFORE blurring, so the soft ramp sits entirely outside the region
    # the visemes touch.  Blurring alone leaves alpha ~0.5 along the boundary -
    # which is exactly the feather band where a stale patch would show.
    kk = int(20 * s) | 1
    avoid = cv2.dilate(avoid, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk)))
    w = w * (1.0 - cv2.GaussianBlur(avoid, (0, 0), 6 * s))
    oval = face.hull_mask(shape, lm, face.FACE_OVAL)      # hull_mask only dilates
    k = int(12 * s) | 1
    oval = cv2.erode(oval, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    w = w * (cv2.GaussianBlur(oval, (0, 0), 5 * s).astype(np.float32) / 255.0)
    return np.clip(w, 0.0, 1.0).astype(np.float32), lat


def cheek_state(key, up, s, box, w, lat):
    x0, y0, bw, bh = box
    xs = np.arange(x0, x0 + bw, dtype=np.float32)
    ys = np.arange(y0, y0 + bh, dtype=np.float32)
    gx, gy = np.meshgrid(xs, ys)
    Wl = w[y0:y0 + bh, x0:x0 + bw]
    warped = cv2.remap(key,
                       (gx + lat * 0.22 * up * Wl).astype(np.float32),   # slightly medial
                       (gy + up * Wl).astype(np.float32),                # and up
                       cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
    base = key[y0:y0 + bh, x0:x0 + bw]
    a = np.clip(Wl * 1.5, 0.0, 1.0)
    rgb = np.where(a[..., None] > 1e-3, warped, base)
    return np.dstack([rgb.astype(np.uint8), (a * 255).astype(np.uint8)])


# ---- build -----------------------------------------------------------------

def build(key, lm=None, dxs=None, dys=None, brow_dys=None, ups=None,
          avoid=None, log=print):
    """-> dict(gaze={dxs,dys,<side>:...}, brow={dys,...}, cheek={ups,...})

    `avoid` is an optional float mask of pixels the viseme frames repaint; the
    cheek layer is forced to zero there.  Pass the measured one when you have
    the viseme bank to hand - it is exact, where a dilated lip hull is a guess.
    """
    if lm is None:
        lm, _ = face.detect(key)
    if lm is None:
        raise ValueError("no face landmarks on keyframe")
    dxs = list(GAZE_DX if dxs is None else dxs)
    dys = list(GAZE_DY if dys is None else dys)
    bdys = list(BROW_DY if brow_dys is None else brow_dys)
    cups = list(CHEEK_UP if ups is None else ups)
    H, W = key.shape[:2]
    s = max(H, W) / 1024.0

    out = dict(gaze=dict(dxs=dxs, dys=dys), brow=dict(dys=bdys),
               cheek=dict(ups=cups))
    for side in SIDES:
        c, r = _iris(lm, side)
        ball = _eyeball_mask(key.shape, lm, side, s)
        box = _box(ball, int(7 * s), key.shape)
        out["gaze"][side] = dict(
            box=[int(v) for v in box],
            patches=[gaze_state(key, lm, side, dx, dy, s, box, ball)
                     for dy in dys for dx in dxs])       # row-major: dy outer
        log(f"  gaze {side}: iris r={r:.1f}px, {len(dxs)}x{len(dys)} states, "
            f"patch {box[2]}x{box[3]}")

        alpha = _brow_alpha(key.shape, lm, side, s)
        bbox = _box(alpha, int(6 * s), key.shape)
        out["brow"][side] = dict(
            box=[int(v) for v in bbox],
            patches=[brow_state(key, lm, side, dy, s, bbox, alpha) for dy in bdys])
        log(f"  brow {side}: {len(bdys)} states, patch {bbox[2]}x{bbox[3]}")

        cw, lat = _cheek_weight(key.shape, lm, side, s, avoid)
        cbox = _box(cw, int(4 * s), key.shape)
        out["cheek"][side] = dict(
            box=[int(v) for v in cbox],
            patches=[cheek_state(key, u, s, cbox, cw, lat) for u in cups])
        log(f"  cheek {side}: {len(cups)} states, patch {cbox[2]}x{cbox[3]}")
    return out


def paste(frame, layer, side, i, alpha=1.0):
    """Draw state `i` of one side onto a BGR frame (preview/QA only)."""
    if i < 0 or alpha <= 0:
        return frame
    x, y, w, h = layer[side]["box"]
    p = layer[side]["patches"][i]
    a = (p[..., 3:4].astype(np.float32) / 255.0) * float(alpha)
    roi = frame[y:y + h, x:x + w].astype(np.float32)
    frame[y:y + h, x:x + w] = np.clip(
        roi * (1 - a) + p[..., :3].astype(np.float32) * a, 0, 255).astype(np.uint8)
    return frame


def nearest(values, v):
    return min(range(len(values)), key=lambda i: abs(values[i] - v))


def paste_snap(frame, layer, side, values, v, row=0):
    """Nearest baked state. Blending two baked warps is a cross-dissolve, which
    doubles every hard edge inside the patch; the grids are fine enough that
    snapping is both sharper and, at a quarter of a pixel, invisible."""
    return paste(frame, layer, side, row * len(values) + nearest(values, v), 1.0)


def strip(patches):
    return np.vstack(patches)
