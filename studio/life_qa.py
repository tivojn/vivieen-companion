"""Render the full liveness stack to a video: mouth + eyelids + gaze + brow +
cheek + head-on-neck movement, driven by a port of the runtime's controllers.

The Node harness proves the runtime's *logic* is sane; this proves the *pixels*
composite correctly when all layers move at once - which is the only place
layering mistakes actually show up (an iris dragged back to centre by a blink, a
brow patch stamping over a raised brow, a seam at a patch edge).

  python -m studio.life_qa [slug] [--dest DIR] [--secs 14]
"""
import os, sys, math, random, shutil, subprocess
import numpy as np, cv2
from . import build as reg, face, blink, expression

FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.config/enconvo/bin/ffmpeg")
FPS = 30
XFADE = 0.055
# a plausible syllable stream rather than the alphabet in order
SPEECH = ["ah", "nn", "eh", "DD", "ih", "SS", "oh", "kk", "ah", "RR", "eh", "PP",
          "oo", "TH", "ah", "nn", "ih", "CH", "oh", "FF"]


def _smoothstep(u):
    u = max(0.0, min(1.0, u))
    return u * u * (3 - 2 * u)


# Vestibulo-ocular gain. The head's apparent movement is mostly rotation about
# the neck (L ~ 420px from neck to eye at this scale), so an apparent shift of
# dx px means a yaw of dx/L and the eye must counter-rotate by that plus a small
# parallax term - about 0.12*dx in the socket. See the runtime for the full note.
VOR_X, VOR_Y, VOR_T = 0.12, 0.10, 0.02


def vn(t, seed):
    """Smooth 1-D value noise, matching the runtime's hash exactly.

    Head drift is noise rather than summed sines because sines never pause, and
    a head that never pauses reads as machinery.
    """
    i = math.floor(t)
    f = t - i
    u = f * f * (3 - 2 * f)

    def h(k):
        x = math.sin(k * 127.1 + seed * 311.7) * 43758.5453
        return (x - math.floor(x)) * 2 - 1

    return h(i) * (1 - u) + h(i + 1) * u


class Head:
    """Head on a neck: continuous drift plus discrete nod and tilt gestures.

    Amplitudes are px at the EYES; the render turns them into a rotation about
    the neck pivot, so the crown swings ~1.5x this and the collar barely moves.
    """

    def __init__(self, rng):
        self.rng, self.nods, self.next_nod = rng, [], 1500.0
        self.tilt, self.next_tilt = None, 3000.0

    def step(self, now, speaking, emph):
        R = self.rng
        if now > self.next_nod:
            self.nods.append(dict(t0=now, a=(1.0 if speaking else 0.6) * (3.5 + R.random() * 4.0),
                                  d=360 + R.random() * 240, n=2 if R.random() < 0.3 else 1))
            self.next_nod = now + (1500 + R.random() * 2800 if speaking
                                   else 4500 + R.random() * 9000)
        self.nods = [g for g in self.nods if now - g["t0"] <= 2200]
        if now > self.next_tilt:
            self.tilt = dict(a=(-1 if R.random() < 0.5 else 1) * (0.25 + R.random() * 0.45),
                             t0=now, ti=380 + R.random() * 320,
                             hold=700 + R.random() * 2300, to=600 + R.random() * 700)
            self.next_tilt = now + 5000 + R.random() * 11000

        nod = 0.0
        for g in self.nods:
            u = (now - g["t0"]) / g["d"]
            if 0 <= u < g["n"]:
                nod += g["a"] * math.sin(2 * math.pi * u) * math.exp(-1.5 * u)
        cant = 0.0
        if self.tilt:
            e, T = now - self.tilt["t0"], self.tilt
            if e < T["ti"]:
                cant = T["a"] * _smoothstep(e / T["ti"])
            elif e < T["ti"] + T["hold"]:
                cant = T["a"]
            else:
                k = (e - T["ti"] - T["hold"]) / T["to"]
                cant = 0.0 if k >= 1 else T["a"] * (1 - _smoothstep(k))

        t = now / 1000.0
        return (3.4 * vn(t * 0.17, 1) + 1.5 * vn(t * 0.43, 2) + 0.55 * vn(t * 1.05, 3),
                2.3 * vn(t * 0.21, 4) + 1.0 * vn(t * 0.51, 5) + 0.35 * vn(t * 1.25, 6)
                + nod + emph,
                0.40 * vn(t * 0.11, 7) + 0.15 * vn(t * 0.33, 8) + cant)


class Micro:
    """Fixational micro-saccades: a flick off target and a corrective flick back,
    with a magnitude floor so every flick crosses at least one baked state."""

    def __init__(self, rng):
        self.rng, self.x, self.y, self.t0, self.next = rng, 0.0, 0.0, 0.0, 0.0

    def step(self, now):
        R = self.rng
        if now > self.next:
            sgn = lambda: -1 if R.random() < 0.5 else 1
            self.x = sgn() * (0.22 + R.random() * 0.28)
            self.y = sgn() * (0.12 + R.random() * 0.18)
            self.t0, self.next = now, now + 300 + R.random() * 620
        k = min(1.0, (now - self.t0) / 420.0)
        return self.x * (1 - k), self.y * (1 - k)


class Brow:
    def __init__(self, rng):
        self.rng, self.amp, self.t0, self.dur = rng, 0.0, -1e9, 1.0
        self.env, self.next = 0.0, 2200.0
        self.asym = 0.86 + rng.random() * 0.1

    def plan(self, now, speaking):
        r, R = self.rng.random(), self.rng
        if speaking:
            if r < 0.52:
                self.amp, self.dur = 0.9 + R.random() * 1.1, 260 + R.random() * 360
            elif r < 0.80:
                self.amp, self.dur = 2.0 + R.random() * 1.3, 340 + R.random() * 460
            else:
                self.amp, self.dur = -(0.5 + R.random() * 0.9), 380 + R.random() * 560
            self.next = now + 650 + R.random() * 1700
        else:
            if r < 0.55:
                self.amp, self.dur = 0.5 + R.random() * 0.8, 420 + R.random() * 700
            elif r < 0.82:
                self.amp, self.dur = -(0.4 + R.random() * 0.7), 600 + R.random() * 900
            else:
                self.amp, self.dur = 1.4 + R.random() * 1.1, 350 + R.random() * 450
            self.next = now + 2400 + R.random() * 4600
        self.t0 = now

    def gesture(self, now):
        ms = now - self.t0
        if ms < 0 or ms > self.dur + 320:
            return 0.0
        rise = _smoothstep(min(1.0, ms / 130))
        f = max(0.0, 1 - (ms - self.dur) / 300) if ms > self.dur else 1.0
        return self.amp * rise * f * f

    def value(self, now, side, lid):
        t = now / 1000.0
        k = self.asym if side == "l" else 1.0
        drift = (0.20 * math.sin(t * 0.31 + (1.7 if side == "l" else 0)) +
                 0.11 * math.sin(t * 0.73))
        v = (self.gesture(now) + 1.1 * self.env) * k + drift - 0.4 * lid
        return max(-1.5, min(3.5, v))


def render(slug=None, dest=None, secs=14.0, seed=11, log=print):
    slug = slug or reg.get_active()
    d = reg.adir(slug)
    dest = dest or os.path.join(d, "diag", "life")
    os.makedirs(dest, exist_ok=True)
    key = cv2.imread(os.path.join(d, "keyframe.png"))
    shut = cv2.imread(os.path.join(d, "visemes", "v_blink.jpg"))
    klm, _ = face.detect(key)
    lids = blink.build(key, shut, log=log)

    vdir = os.path.join(d, "visemes")
    frames = {}
    for f in os.listdir(vdir):
        if f.startswith("v_") and not f.startswith("v_blink"):
            frames[f[2:].rsplit(".", 1)[0]] = cv2.imread(os.path.join(vdir, f))
    log(f"  {len(frames)} mouth shapes")

    # the same measured avoid mask export bakes with, so this video is a QA of
    # the shipped assets and not of a near-miss rebuild
    touched = np.zeros(key.shape[:2], np.float32)
    for v in frames.values():
        touched = np.maximum(touched, np.abs(v.astype(np.float32)
                                             - key.astype(np.float32)).max(2))
    expr = expression.build(key, klm, avoid=(touched > 6).astype(np.float32), log=log)
    N = expression.neck(klm)
    lever = N["pivot"] - N["ref"]
    log(f"  neck pivot y={N['pivot']:.0f}, lever {lever:.0f}px "
        f"(crown {(N['pivot'] - 120) / lever:.2f}x, collar {(N['pivot'] - 950) / lever:.2f}x)")

    rng = random.Random(seed)
    micro = Micro(rng)
    brow = Brow(rng)
    head = Head(rng)
    cheek = 0.0
    n = int(secs * FPS)
    talk_from = int(n * 0.42)

    # blink schedule, planned the way the runtime plans it
    blinks, next_blink = [], 900.0
    def plan_blink(ms):
        nonlocal next_blink
        blinks.append(dict(t0=ms, amp=0.93 + rng.random() * 0.07,
                           skew=rng.uniform(-13, 13)))
        return ms

    # mouth track
    track, t, i = [], 0.0, 0
    while t < secs:
        track.append((t, SPEECH[i % len(SPEECH)] if t >= talk_from / FPS else "closed"))
        t += (0.09 + rng.random() * 0.07) if t >= talk_from / FPS else 0.25
        i += 1

    H, W = key.shape[:2]
    OUT = 640
    p = subprocess.Popen(
        [FFMPEG, "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT}x{OUT}",
         "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-crf", "17", "-preset", "slow", os.path.join(dest, "life.mp4")],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ti = 0
    prev_v, cur_v, switch = "closed", "closed", 0.0
    stats = dict(gaze=set(), brow=[0.0, 0.0])
    for f in range(n):
        ms, ts = f * 1000.0 / FPS, f / FPS
        speaking = f >= talk_from
        lvl = (0.09 + 0.13 * abs(math.sin(ms / 170))) if speaking else 0.0

        while ti + 1 < len(track) and track[ti + 1][0] <= ts:
            ti += 1
        w = track[ti][1]
        if w != cur_v:
            prev_v, cur_v, switch = cur_v, w, ts
        mix = min(1.0, (ts - switch) / XFADE)

        if ms > next_blink:
            plan_blink(ms)
            next_blink = ms + (1500 + rng.random() * 3400 if speaking
                               else 2400 + rng.random() * 5400)
        if ms > brow.next:
            brow.plan(ms, speaking)
        bt = max(0.0, min(1.0, lvl * 8 - 0.35)) if speaking else 0.0
        brow.env += (bt - brow.env) * (0.18 if bt > brow.env else 0.05)

        # head on the neck, in keyframe px at eye height - and the eyes hold the
        # lens through it, which is what makes the iris move at all
        hx, hy, roll = head.step(ms, speaking, lvl * 2.2 * 1.6 if speaking else 0.0)
        mx, my = micro.step(ms)
        # two gains: a yaw swings the socket (0.12), a cant only carries the eye
        # sideways so it needs the parallax term alone (0.02)
        gxv = -VOR_X * hx - VOR_T * (lever * math.radians(roll)) + mx
        gyv = -VOR_Y * hy + my
        blinks[:] = [b for b in blinks if ms - b["t0"] <= 1500]

        L = {}
        for side in ("l", "r"):
            v = 0.0
            for b in blinks:
                off = b["skew"] * (1 if side == "r" else 0)
                v = max(v, blink.lid_curve(ms - b["t0"] - off, b["amp"]))
            L[side] = min(1.0, v)
        B = {s: brow.value(ms, s, L[s]) for s in ("l", "r")}
        tgt = (0.30 + 1.05 * brow.env + 0.75 * lvl if speaking
               else 0.22 + 0.34 * (vn(ts * 0.13, 11) + 1) / 2)
        cheek += (tgt - cheek) * (0.11 if tgt > cheek else 0.035)
        C = max(0.0, min(2.2, cheek + 0.35 * (L["l"] + L["r"]) / 2))
        stats["gaze"].add(round(gxv, 2))
        stats["brow"][0] = min(stats["brow"][0], B["l"])
        stats["brow"][1] = max(stats["brow"][1], B["l"])

        img = frames[cur_v].astype(np.float32)
        if mix < 1 and prev_v in frames:
            img = frames[prev_v].astype(np.float32) * (1 - mix) + img * mix
        img = img.astype(np.uint8)

        # anatomical order: brow, cheek, eyeball, lid
        for side in ("l", "r"):
            expression.paste_snap(img, expr["brow"], side, expr["brow"]["dys"], B[side])
        for side in ("l", "r"):
            expression.paste_snap(img, expr["cheek"], side, expr["cheek"]["ups"], C)
        row = expression.nearest(expr["gaze"]["dys"], gyv)
        for side in ("l", "r"):
            expression.paste_snap(img, expr["gaze"], side, expr["gaze"]["dxs"], gxv,
                                  row=row)
        for side in ("l", "r"):
            blink.paste_lid(img, lids, side, L[side])

        # The head-on-neck affine. w(y) = (pivot - y)/lever tapers the motion
        # from ~1.5x at the crown through 1.0 at the eyes to ~0 at the collar,
        # and because w is linear in y the whole graded warp is exactly this
        # 2x3 matrix - no bands, no seams. Moving the whole frame instead is
        # what made the neck look stiff: that is a camera pan, not a neck.
        th, kx, ky = math.radians(roll), hx / lever, hy / lever
        Mx = np.float32([[1, -(kx + th), (kx + th) * N["pivot"]],
                         [th, 1 - ky, ky * N["pivot"] - th * N["x"]]])
        img = cv2.warpAffine(img, Mx, (W, H), flags=cv2.INTER_LANCZOS4,
                             borderMode=cv2.BORDER_REPLICATE)
        p.stdin.write(cv2.resize(img, (OUT, OUT), interpolation=cv2.INTER_AREA).tobytes())

    p.stdin.close()
    p.wait()
    log(f"  {len(stats['gaze'])} distinct gaze positions, "
        f"brow {stats['brow'][0]:+.1f}..{stats['brow'][1]:+.1f}px, {len(blinks)} blinks live")
    log(f"wrote {os.path.join(dest, 'life.mp4')}")
    return os.path.join(dest, "life.mp4")


if __name__ == "__main__":
    argv, opts, pos = sys.argv[1:], {}, []
    i = 0
    while i < len(argv):                 # a flag EATS its value, or --secs 22
        if argv[i].startswith("--"):     # gets mistaken for the slug
            opts[argv[i]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
        else:
            pos.append(argv[i])
            i += 1
    render(pos[0] if pos else None, opts.get("--dest"),
           float(opts.get("--secs", 14.0)))
