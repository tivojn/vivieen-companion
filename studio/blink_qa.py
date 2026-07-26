"""Blink QA: render the old two-state swap next to the new lid travel.

Same blink schedule drives both panels, so any difference you see is the lid,
not the timing.  Second half runs at quarter speed because a 315 ms blink is
over before you can judge it.

    python -m studio.blink_qa [slug] [--dest DIR]
"""
import os, subprocess, shutil, random
import numpy as np, cv2
from . import blink, build as reg

FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/.config/enconvo/bin/ffmpeg")
FPS = 60
OLD_MS = 105.0          # the old hard-cut window
OLD_DOUBLE = 95.0


def _encode(frames, path, fps=FPS):
    h, w = frames[0].shape[:2]
    p = subprocess.Popen(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-crf", "17", "-preset", "slow",
         "-movflags", "+faststart", path], stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(np.ascontiguousarray(f).tobytes())
    p.stdin.close()
    p.wait()
    return path


def _old_shut(events, ms):
    """The previous runtime: a rectangular window of the fully closed frame."""
    for e in events:
        w = OLD_DOUBLE if e.get("second") else OLD_MS
        if 0 <= ms - e["t0"] < w:
            return True
    return False


def _label(img, text, sub=""):
    bar = 34
    cv2.rectangle(img, (0, 0), (img.shape[1], bar), (16, 17, 20), -1)
    cv2.putText(img, text, (14, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.56,
                (238, 235, 229), 1, cv2.LINE_AA)
    if sub:
        (tw, _), _ = cv2.getTextSize(sub, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.putText(img, sub, (img.shape[1] - tw - 14, 23),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (140, 146, 156), 1, cv2.LINE_AA)
    return img


def compare(slug=None, dest=None, seconds=9.0, slow=4.0, log=print):
    slug = slug or reg.get_active()
    d = reg.adir(slug)
    dest = dest or os.path.join(d, "diag", "lid")
    os.makedirs(dest, exist_ok=True)
    key = cv2.imread(os.path.join(d, "keyframe.png"))
    shut = cv2.imread(os.path.join(d, "visemes", "v_blink.jpg"))
    lids = blink.build(key, shut, log=log)

    ev = blink.schedule(seconds * 1000.0, random.Random(3), start=600.0)
    for i, e in enumerate(ev):
        e["second"] = i and abs(e["t0"] - ev[i - 1]["t0"]) < 500
    log(f"  {len(ev)} blinks scheduled")

    # crop tight to the eyes - that is where the whole argument lives
    bx = [lids["eyes"][s]["box"] for s in blink.SIDES]
    x0 = min(b[0] for b in bx) - 26; x1 = max(b[0] + b[2] for b in bx) + 26
    y0 = min(b[1] for b in bx) - 16; y1 = max(b[1] + b[3] for b in bx) + 26
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, key.shape[1]), min(y1, key.shape[0])

    def panel_new(ms):
        f = key.copy()
        for side in blink.SIDES:
            blink.paste_lid(f, lids, side, blink.lid_at(ev, ms, side))
        return f

    def panel_old(ms):
        return shut.copy() if _old_shut(ev, ms) else key.copy()

    # timeline: real time, then the same run again at 1/slow speed
    clock = [i * 1000.0 / FPS for i in range(int(seconds * FPS))]
    t_slow0 = ev[1]["t0"] - 260 if len(ev) > 1 else ev[0]["t0"] - 260
    n_slow = int((blink.blink_ms() + 700) / (1000.0 / FPS) * slow)
    clock += [t_slow0 + i * (1000.0 / FPS) / slow for i in range(n_slow)]
    marks = [False] * (len(clock) - n_slow) + [True] * n_slow

    frames = []
    for ms, is_slow in zip(clock, marks):
        top = panel_old(ms)[y0:y1, x0:x1].copy()
        bot = panel_new(ms)[y0:y1, x0:x1].copy()
        sub = f"1/{slow:g} speed" if is_slow else "real time"
        _label(top, "BEFORE   one closed frame, hard cut in and out", sub)
        _label(bot, "AFTER   synthesised lid travel, 8 positions per eye", sub)
        gap = np.full((3, top.shape[1], 3), 11, np.uint8)
        frames.append(np.vstack([top, gap, bot]))

    h, w = frames[0].shape[:2]
    if w % 2 or h % 2:
        frames = [f[:h - h % 2, :w - w % 2] for f in frames]
    out = _encode(frames, os.path.join(dest, "blink_compare.mp4"))
    log(f"  {out}")

    # and the whole face, new behaviour only
    ff = [panel_new(ms) for ms in clock[:int(seconds * FPS)]]
    out2 = _encode(ff, os.path.join(dest, "blink_face.mp4"))
    log(f"  {out2}")
    return out, out2


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", nargs="?")
    ap.add_argument("--dest")
    a = ap.parse_args()
    compare(a.slug, a.dest)
