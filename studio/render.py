"""Preview rendering.

The runtime coarticulates away sub-perceptual poses and hands textures over in
about one display frame. A long opacity blend is specifically wrong for teeth:
it displays two rigid dental rows at once. The preview therefore holds readable
poses and uses direct texture handoffs too.
"""
import os, glob, shutil, subprocess, random
import numpy as np, cv2
from . import visemes, blink as blinkmod

FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.config/enconvo/bin/ffmpeg")
# No "blink" here on purpose: the eyes are not a mouth shape you cut to, they
# are an overlay with their own clock.  See blink.py.
DEMO = ["closed","PP","ah","ih","SS","eh","TH","oh","nn","oo","kk","ah","DD","ih","RR","eh",
        "FF","ah","CH","oh","closed","ah","SS","ih","oo","nn","eh","closed"]


def _bank(viseme_dir):
    cache = {}
    def load(v):
        if v not in cache:
            p = os.path.join(viseme_dir, f"v_{v}.jpg")
            img = cv2.imread(p)
            if img is None:
                raise FileNotFoundError(p)
            cache[v] = img.astype(np.float32)
        return cache[v]
    return load


def render_frames(viseme_dir, seq=None, hold=3, blend=0):
    load = _bank(viseme_dir)
    seq = seq or DEMO
    have = {os.path.basename(p)[2:-4] for p in glob.glob(os.path.join(viseme_dir, "v_*.jpg"))}
    seq = [v for v in seq if v in have] or sorted(have)
    per_frame = [v for v in seq for _ in range(hold)]
    n = len(per_frame)
    out = []
    for f in range(n):
        lo, hi = max(0, f - blend), min(n - 1, f + blend)
        idx = list(range(lo, hi + 1))
        w = np.array([0.5 * (1 + np.cos(np.pi * (i - f) / (blend + 1))) for i in idx], np.float32)
        w /= w.sum()
        acc = None
        for i, wi in zip(idx, w):
            img = load(per_frame[i])
            acc = img * wi if acc is None else acc + img * wi
        out.append(np.clip(acc, 0, 255).astype(np.uint8))
    return out


def write_video(frames, path, fps=24):
    tmp = path + "_frames"
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    for i, f in enumerate(frames):
        cv2.imwrite(os.path.join(tmp, f"f{i:04d}.png"), f)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-framerate", str(fps),
                    "-i", os.path.join(tmp, "f%04d.png"),
                    "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                    "-c:v", "h264_videotoolbox", "-pix_fmt", "yuv420p",
                    "-b:v", "8M", "-allow_sw", "1",
                    "-movflags", "+faststart", path], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return path


def blink_overlay(frames, lids, fps, seed=7, speaking=True, log=print):
    """Stamp the eyelid trajectory onto rendered frames on a real clock."""
    ev = blinkmod.schedule(len(frames) * 1000.0 / fps, random.Random(seed),
                           speaking=speaking, start=700.0)
    log(f"  {len(ev)} blinks over {len(frames)/fps:.1f}s")
    for i, f in enumerate(frames):
        ms = i * 1000.0 / fps
        for side in blinkmod.SIDES:
            blinkmod.paste_lid(f, lids, side, blinkmod.lid_at(ev, ms, side))
    return frames


def preview(viseme_dir, path, fps=48, log=print):
    # 48 fps with a doubled hold keeps the mouth cadence identical to the old
    # 24 fps preview while giving a ~315 ms blink the ~15 frames it needs to
    # read as a lid falling instead of a frame dropping.
    frames = render_frames(viseme_dir, hold=6, blend=0)
    key = os.path.join(os.path.dirname(viseme_dir.rstrip("/")), "keyframe.png")
    shut = os.path.join(viseme_dir, "v_blink.jpg")
    if os.path.exists(key) and os.path.exists(shut):
        lids = blinkmod.build(cv2.imread(key), cv2.imread(shut), log=log)
        blink_overlay(frames, lids, fps, log=log)
    else:
        log("  no keyframe/blink frame - preview will not blink")
    return write_video(frames, path, fps)


def contact_sheet(viseme_dir, keyframe, path, cols=4, cell=300):
    """Mouth-zoom grid of the whole bank - the fastest way to eyeball tongue errors."""
    from . import face
    key = cv2.imread(keyframe)
    klm, _ = face.detect(key)
    lip = klm[face.OUTER_LIP]
    cx, cy = lip.mean(0)
    half = max(90.0, float(lip[:, 0].max() - lip[:, 0].min()) * 1.15)
    x0, y0 = int(cx - half), int(cy - half * 0.85)
    x1, y1 = int(cx + half), int(cy + half * 0.85)
    H, W = key.shape[:2]
    x0, y0 = max(0, x0), max(0, y0); x1, y1 = min(W, x1), min(H, y1)

    tiles = []
    for name in visemes.ORDER:
        p = os.path.join(viseme_dir, f"v_{name}.jpg")
        if not os.path.exists(p):
            continue
        img = cv2.imread(p)
        t = cv2.resize(img[y0:y1, x0:x1], (cell, cell), interpolation=cv2.INTER_LANCZOS4)
        cv2.rectangle(t, (0, 0), (cell, 30), (18, 18, 22), -1)
        cv2.putText(t, name, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                    (120, 255, 140), 1, cv2.LINE_AA)
        tiles.append(t)
    if not tiles:
        return None
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    cv2.imwrite(path, np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 92])
    return path
