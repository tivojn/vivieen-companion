"""Eyelid TRAVEL: synthesise the in-between lid positions of a blink.

Why this file exists.  Playing a blink as open -> shut -> open is two hard cuts.
The lid never occupies a single intermediate position, so the eye reads as a
dropped frame or a strobe, never as an eyelid.  Vision is far more sensitive to
the SWEEP than to the closed pose: a real lid falls in ~80-100 ms with the
velocity peaking early, rests shut for ~25 ms, then is levered back up over
~200 ms and creeps the last few percent.  That trajectory is the blink.

And it does NOT need more generated images.  The one blink frame already carries
the true closed-lid texture - lash line, crease, lid shadow - in exact
registration with the keyframe (compose.py guarantees every frame is
pixel-identical to the keyframe outside its own mask).  So a partial lid is
geometry, not generation: slide that closed lid UP to a fractional height and
let the untouched open eye show below it.

The tempting shortcut - cross-dissolving open into shut - is the other classic
way to get this wrong: the iris ghosts THROUGH the lid instead of being occluded
by it, and the eye appears to fade out rather than close.  Here the iris is
never touched; it is covered.

Output is a small per-eye RGBA patch strip.  Every viseme frame is identical in
the eye region by construction, so one strip overlays correctly on all of them -
no need for a second full set of frames, and the two eyes stay separable so the
runtime can give them the few ms of asymmetry that real blinks have.
"""
import numpy as np, cv2
from . import face

# Lid contours, ordered along the lid.  (r = viewer-left eye, MediaPipe's 33-side.)
UPPER = {"r": [33, 246, 161, 160, 159, 158, 157, 173, 133],
         "l": [362, 398, 384, 385, 386, 387, 388, 466, 263]}
LOWER = {"r": [33, 7, 163, 144, 145, 153, 154, 155, 133],
         "l": [362, 382, 381, 380, 374, 373, 390, 249, 263]}
EYE = {"r": face.EYE_R, "l": face.EYE_L}
BROW = {"r": face.BROW_R, "l": face.BROW_L}
SIDES = ("r", "l")

N_STATES = 8          # lid positions from just-moving to fully shut
SHADOW = 0.22         # how dark the falling lid shades the eyeball under it


def _line(pts, xs):
    """y(x) along a lid contour, clamped outside its own span."""
    p = np.asarray(pts, np.float64)
    o = np.argsort(p[:, 0])
    return np.interp(xs, p[o, 0], p[o, 1])


def _smoothstep(a, b, x):
    t = np.clip((x - a) / max(b - a, 1e-6), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _outer_lash_mask(shut, lm, side, s):
    H, W = shut.shape[:2]
    corners = lm[[UPPER[side][0], UPPER[side][-1]]]
    centre = W * 0.5
    outer = corners[np.argmax(np.abs(corners[:, 0] - centre))]
    inner = corners[np.argmin(np.abs(corners[:, 0] - centre))]
    width = max(abs(float(inner[0] - outer[0])), 1.0)
    direction = 1.0 if outer[0] > inner[0] else -1.0

    rows, cols = np.mgrid[:H, :W]
    y_shut = 0.5 * (_line(lm[UPPER[side]], np.arange(W)) +
                    _line(lm[LOWER[side]], np.arange(W)))
    outward = direction * (cols - outer[0])
    above = y_shut[None, :] - rows
    region = ((outward >= -0.02 * width) &
              (outward <= 0.31 * width) &
              (above >= max(4.0 * s, 0.04 * width)) &
              (above <= 0.27 * width))

    gray = cv2.cvtColor(shut, cv2.COLOR_BGR2GRAY).astype(np.float32)
    local = cv2.GaussianBlur(gray, (0, 0), max(5.0 * s, 0.05 * width))
    mask = ((local - gray > 4.0) & region).astype(np.uint8) * 255
    size = int(round(max(3.0 * s, 0.03 * width))) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return cv2.dilate(mask, kernel)


def clean_source(shut, lm=None, log=None):
    """Remove generated open-lash hairs above the closed outer corners."""
    if lm is None:
        lm, _ = face.detect(shut)
    if lm is None:
        return shut.copy()
    s = max(shut.shape[:2]) / 1024.0
    mask = np.zeros(shut.shape[:2], np.uint8)
    for side in SIDES:
        mask = cv2.bitwise_or(mask, _outer_lash_mask(shut, lm, side, s))
    count = int(np.count_nonzero(mask))
    if not count:
        return shut.copy()
    if log:
        log(f"cleaned {count} stray outer-lash pixels")
    return cv2.inpaint(shut, mask, max(3.0 * s, 2.0), cv2.INPAINT_TELEA)


def _eye_alpha(key, shut, klm, blm, side, s):
    """Eye opening plus the open eyelashes that must move with the upper lid.

    An RGBA overlay can cover the iris, but transparency cannot ERASE the open
    lashes already baked into the keyframe.  The old mask was an 11 px dilation
    of the eye opening, so long lashes sat outside its alpha and survived above
    the correctly closed lash: two eyelids in one frame.

    Do not replace that guess with a bigger guess.  The aligned closed frame is
    ground truth for which upper-eye pixels change.  Add those measured pixels
    to the original eye-opening mask, then clip the extension between the brow
    and the closed lash.  This removes every open-lash remnant without letting a
    blink stamp a neutral brow over the brow layer or neutral lower-lid skin over
    the cheek layer.
    """
    shape = key.shape
    H, W = shape[:2]
    core = face.hull_mask(shape, klm, EYE[side], dilate=int(11 * s) | 1)
    search = face.hull_mask(shape, klm, EYE[side], dilate=int(40 * s) | 1) > 0

    delta = np.abs(shut.astype(np.int16) - key.astype(np.int16)).max(2)
    changed = ((delta > 8) & search).astype(np.uint8) * 255
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                  (int(7 * s) | 1, int(7 * s) | 1))
    changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, k)
    changed = cv2.dilate(changed, k)

    xs = np.arange(W, dtype=np.float64)
    y_open = _line(klm[UPPER[side]], xs)
    y_brow = _line(klm[BROW[side]], xs)
    y_shut = 0.5 * (_line(blm[UPPER[side]], xs) +
                    _line(blm[LOWER[side]], xs))
    rows = np.arange(H, dtype=np.float64)[:, None]
    top = y_brow + 0.28 * (y_open - y_brow)   # leave the brow and its skin alone
    bottom = y_shut + 7.0 * s                 # do not overwrite the cheek layer
    changed[(rows < top[None, :]) | (rows > bottom[None, :])] = 0

    m = cv2.bitwise_or(core, changed)
    return cv2.GaussianBlur(m, (0, 0), 4 * s).astype(np.float32) / 255.0


def _box(alpha, pad, shape):
    ys, xs = np.nonzero(alpha > 0.004)
    H, W = shape[:2]
    x0 = max(int(xs.min()) - pad, 0); x1 = min(int(xs.max()) + pad + 1, W)
    y0 = max(int(ys.min()) - pad, 0); y1 = min(int(ys.max()) + pad + 1, H)
    return x0, y0, x1 - x0, y1 - y0


def _aperture(lm, side):
    return float(np.mean(_line(lm[LOWER[side]], _span(lm, side)) -
                         _line(lm[UPPER[side]], _span(lm, side))))


def _span(lm, side):
    x = lm[EYE[side]][:, 0]
    return np.linspace(x.min(), x.max(), 24)


def lid_state(key, shut, klm, blm, side, t, s, box, alpha):
    """One lid position t in (0,1]: 0 = wide open (the keyframe), 1 = fully shut."""
    x0, y0, bw, bh = box
    xs = np.arange(x0, x0 + bw, dtype=np.float64)
    rows = np.arange(y0, y0 + bh, dtype=np.float32)

    y_open = _line(klm[UPPER[side]], xs)                       # open lash line
    y_shut = 0.5 * (_line(blm[UPPER[side]], xs) +              # closed lash line
                    _line(blm[LOWER[side]], xs))
    y_brow = _line(klm[BROW[side]], xs)
    lid = y_open + t * (y_shut - y_open)                       # where the lash is now
    dy = lid - y_shut                                          # <=0, lift the shut lid
    fall = np.maximum(y_shut - y_brow, 6.0)                    # lid skin compresses to the brow

    # Vertical warp of the shut frame: the lash line moves to `lid`, the brow stays put.
    map_y = np.empty((bh, bw), np.float32)
    for c in range(bw):
        u = np.clip((y_shut[c] - rows) / fall[c], 0.0, 1.0)
        w = 0.5 * (1.0 + np.cos(np.pi * u))                    # 1 at the lash, 0 at the brow
        dest = rows + dy[c] * w
        map_y[:, c] = np.interp(rows, dest, rows).astype(np.float32)
    map_x = np.repeat(xs.astype(np.float32)[None, :], bh, 0)
    warped = cv2.remap(shut, map_x, map_y, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE).astype(np.float32)

    sh = shut[y0:y0 + bh, x0:x0 + bw].astype(np.float32)
    base = key[y0:y0 + bh, x0:x0 + bw].astype(np.float32)
    yy = rows[:, None]

    # Cut at the lash line: lid above, untouched eyeball below.  Feather ~1px only -
    # lashes are a hard dark edge and a soft one immediately reads as a dissolve.
    fw = max(1.1 * s, 0.8)
    a_lid = np.clip((lid[None, :] + fw - yy) / (2 * fw), 0.0, 1.0)[..., None]

    # The descending lid throws a shadow on the eye just under it.
    band = max(7.0 * s, 4.0)
    shade = (np.clip(1.0 - (yy - lid[None, :]) / band, 0.0, 1.0) *
             (SHADOW * t))[..., None]

    # Stack bottom-up in premultiplied alpha.  Below the moving lash the eyeball
    # stays transparent so the gaze layer survives; above it, the mask also owns
    # the ORIGINAL open-lash footprint so those baked keyframe lashes are erased
    # as the new lash descends.  The measured mask stops before brow and cheek.
    P = np.zeros((bh, bw, 3), np.float32)
    A = np.zeros((bh, bw, 1), np.float32)

    def over(color, a):
        nonlocal P, A
        P = P * (1 - a) + color * a
        A = A * (1 - a) + a

    over(0.0, shade * (1.0 - a_lid))             # contact shadow on the eyeball
    over(sh, np.full_like(A, _smoothstep(0.55, 0.95, t)))   # land on the real closed frame
    over(warped, a_lid)                          # the lid itself, on top

    rgb = np.where(A > 1e-3, P / np.maximum(A, 1e-3), base)
    a = A[..., 0] * alpha[y0:y0 + bh, x0:x0 + bw]
    return np.dstack([np.clip(rgb, 0, 255).astype(np.uint8),
                      (a * 255).astype(np.uint8)])


def build(key, shut, n=N_STATES, pad=10, log=print):
    """-> dict(states=[t...], eyes={side: dict(box=[x,y,w,h], patches=[RGBA...])})"""
    klm, _ = face.detect(key)
    blm, _ = face.detect(shut)
    if klm is None or blm is None:
        raise ValueError("no face landmarks on keyframe or blink frame")
    H, W = key.shape[:2]
    s = max(H, W) / 1024.0
    for _ in range(2):
        shut = clean_source(shut, blm, log=log)
        cleaned_lm, _ = face.detect(shut)
        if cleaned_lm is not None:
            blm = cleaned_lm

    for side in SIDES:
        ao, ac = _aperture(klm, side), _aperture(blm, side)
        log(f"  {side}: aperture open {ao:.1f}px -> shut {ac:.1f}px "
            f"({ac / max(ao, 1e-6):.0%} of open)")
        if ac > 0.55 * ao:
            log(f"  ! {side} eye barely closes in the blink frame - lid travel will be short")

    ts = [(i + 1) / n for i in range(n)]
    out = dict(states=ts, eyes={})
    for side in SIDES:
        alpha = _eye_alpha(key, shut, klm, blm, side, s)
        box = _box(alpha, int(pad * s), key.shape)
        out["eyes"][side] = dict(
            box=[int(v) for v in box],
            patches=[lid_state(key, shut, klm, blm, side, t, s, box, alpha) for t in ts])
        log(f"  {side}: {n} lid states, patch {box[2]}x{box[3]} at ({box[0]},{box[1]})")
    return out


def strip(patches):
    """Stack lid states into one vertical RGBA sprite."""
    return np.vstack(patches)


def paste(frame, lids, side, i, alpha=1.0):
    """Draw lid state `i` of one eye onto a BGR frame (preview/QA only)."""
    if i < 0 or alpha <= 0:
        return frame
    x, y, w, h = lids["eyes"][side]["box"]
    p = lids["eyes"][side]["patches"][i]
    a = (p[..., 3:4].astype(np.float32) / 255.0) * float(alpha)
    roi = frame[y:y + h, x:x + w].astype(np.float32)
    frame[y:y + h, x:x + w] = np.clip(
        roi * (1 - a) + p[..., :3].astype(np.float32) * a, 0, 255).astype(np.uint8)
    return frame


def paste_lid(frame, lids, side, L):
    """Draw a continuous lid height by straddling the two nearest baked states -
    exactly what the runtime does, so QA and production move identically."""
    st = lids["states"]
    if L <= 0.004:
        return frame
    n = len(st)
    hi = 0
    while hi < n - 1 and st[hi] < L:
        hi += 1
    lo = hi - 1
    a = L / st[0] if lo < 0 else (L - st[lo]) / (st[hi] - st[lo])
    if lo >= 0:
        paste(frame, lids, side, lo, 1.0)
    return paste(frame, lids, side, hi, min(max(a, 0.0), 1.0))


# ---- blink trajectory ------------------------------------------------------
# Shared by the studio preview and the companion runtime so QA and production
# move the same way.  Milliseconds.
CLOSE, HOLD, OPEN, SETTLE, CREEP = 85.0, 25.0, 205.0, 150.0, 0.09


def lid_curve(ms, amp=1.0, close=CLOSE, hold=HOLD, open_=OPEN):
    """Lid fraction at `ms` after a blink starts. 0 = open, 1 = shut."""
    if ms < 0:
        return 0.0
    if ms < close:                                   # fall: fast off the mark, decelerating
        u = ms / close
        return amp * (1.0 - (1.0 - u) ** 2.3)
    if ms < close + hold:                            # brief contact
        return amp
    u = (ms - close - hold) / open_
    if u < 1.0:                                      # levator lift: eased both ends
        return amp * (1.0 - u * u * (3 - 2 * u)) * (1 - CREEP) + amp * CREEP
    u2 = (ms - close - hold - open_) / SETTLE        # the last few percent creeps home
    if u2 < 1.0:
        return amp * CREEP * (1.0 - u2) ** 2
    return 0.0


def blink_ms(close=CLOSE, hold=HOLD, open_=OPEN):
    return close + hold + open_ + SETTLE


def schedule(duration_ms, rng=None, speaking=False, start=900.0):
    """Blink events over a span, with the same statistics the runtime uses:
    irregular spacing, slight timing variation, and a few ms of eye-to-eye
    asymmetry. Each event is one full blink; the random interval supplies the
    natural variation without rapid double blinks."""
    import random
    r = rng or random.Random(7)
    ev, t = [], start
    while t < duration_ms:
        ev.append(dict(t0=t, k=0.88 + r.random() * 0.26,
                       amp=0.93 + r.random() * 0.07,
                       skew=(r.random() * 2 - 1) * 13))
        t += (1500 + r.random() * 3400) if speaking else (2400 + r.random() * 5400)
    return ev


def lid_at(events, ms, side):
    off = 1.0 if side == "r" else 0.0
    v = [lid_curve(ms - e["t0"] - off * e["skew"], e["amp"],
                   CLOSE * e["k"], HOLD * e["k"], OPEN * e["k"]) for e in events]
    return min(1.0, max(v)) if v else 0.0
