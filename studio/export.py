"""Publish a built avatar into a runtime that consumes viseme frames.

The eye region is pixel-identical across every viseme frame by construction, so
the eyes do not belong in the frames at all.  They ship as two small RGBA sprite
strips - one per eye - holding the lid TRAVEL synthesised in blink.py.  The
runtime draws one mouth frame and stamps the current lid position on top.

That is both lighter and better than the old scheme.  Lighter: 16 frames instead
of 32.  Better: the previous export baked exactly two eye states, so a blink
could only ever be a hard cut to fully-shut and a hard cut back - the lid never
existed in between, which no amount of timing can rescue.  Now the lid has 8
positions per eye and the two eyes can be driven independently.
"""
import os, json
import numpy as np, cv2
from . import face, blink, expression, build as reg

# runtime viseme name -> studio shape name
NAME_MAP = {"sil": "closed", "PP": "PP", "FF": "FF", "TH": "TH", "DD": "DD",
            "kk": "kk", "CH": "CH", "SS": "SS", "nn": "nn", "RR": "RR",
            "aa": "ah", "E": "eh", "ih": "ih", "oh": "oh", "ou": "oo"}


def export(slug, dest, quality=92, states=blink.N_STATES, log=print):
    d = reg.adir(slug)
    m = reg.read_manifest(slug)
    if not m or m.get("status") != "ready":
        raise ValueError(f"{slug} is not built yet")
    vis = os.path.join(d, "visemes")
    key = cv2.imread(os.path.join(d, "keyframe.png"))
    shut = cv2.imread(os.path.join(vis, "v_blink.jpg"))
    if shut is None:
        raise ValueError("missing blink frame")

    log("synthesising eyelid travel")
    lids = blink.build(key, shut, n=states, log=log)

    # Measure exactly which pixels the viseme bank repaints, and forbid the
    # cheek layer from touching them.  A dilated lip hull is a guess; this is
    # the ground truth, and a cheek patch that overlapped the mouth would stamp
    # stale keyframe pixels over a moving jaw.
    touched = np.zeros(key.shape[:2], np.float32)
    for shape in set(NAME_MAP.values()):
        v = cv2.imread(os.path.join(vis, f"v_{shape}.jpg"))
        if v is not None:
            touched = np.maximum(
                touched, np.abs(v.astype(np.float32) - key.astype(np.float32)).max(2))
    avoid = (touched > 6).astype(np.float32)

    log("synthesising gaze, brow and cheek")
    klm, _ = face.detect(key)
    expr = expression.build(key, klm, avoid=avoid, log=log)

    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(dest):
        if f.endswith((".jpg", ".png", ".json")):
            os.remove(os.path.join(dest, f))

    H, W = key.shape[:2]
    frames, names = {}, []
    for rt, shape in NAME_MAP.items():
        src = os.path.join(vis, f"v_{shape}.jpg")
        if not os.path.exists(src):
            log(f"  {rt}: no {shape} frame, skipped")
            continue
        img = cv2.imread(src)
        out = os.path.join(dest, f"{rt}_open.jpg")
        cv2.imwrite(out, img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        frames[rt] = dict(open=f"assets/{rt}_open.jpg")
        names.append(rt)

    def _strip(layer, prefix, meta):
        for side in blink.SIDES:
            e = layer[side]
            p = os.path.join(dest, f"{prefix}_{side}.png")
            cv2.imwrite(p, np.vstack(e["patches"]), [cv2.IMWRITE_PNG_COMPRESSION, 9])
            meta[side] = dict(src=f"assets/{prefix}_{side}.png", box=e["box"])
            log(f"  {prefix}_{side}.png  {len(e['patches'])} states, "
                f"{os.path.getsize(p)/1024:.0f} KB")
        return meta

    eyes = _strip(lids["eyes"], "eye",
                  dict(states=[round(t, 4) for t in lids["states"]]))
    gaze = _strip(expr["gaze"], "gaze",
                  dict(dxs=expr["gaze"]["dxs"], dys=expr["gaze"]["dys"]))
    brow = _strip(expr["brow"], "brow", dict(dys=expr["brow"]["dys"]))
    cheek = _strip(expr["cheek"], "cheek", dict(ups=expr["cheek"]["ups"]))

    timing = dict(close=blink.CLOSE, hold=blink.HOLD, open=blink.OPEN,
                  settle=blink.SETTLE, creep=blink.CREEP)
    manifest = dict(v=5, w=W, h=H, avatar=dict(slug=slug, name=m["name"]),
                    visemes=names, frames=frames, eyes=eyes, gaze=gaze, brow=brow,
                    cheek=cheek, neck=expression.neck(klm),
                    blink=timing, built=m.get("updated"), quality=m.get("quality"))
    with open(os.path.join(dest, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    log(f"exported {len(names)} visemes, {states} lid states, "
        f"{len(gaze['dxs']) * len(gaze['dys'])} gaze states, "
        f"{len(brow['dys'])} brow and {len(cheek['ups'])} cheek states "
        f"per side -> {dest}")
    return manifest


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", nargs="?")
    ap.add_argument("--dest", default=os.path.expanduser("~/vivieen-companion/web/assets"))
    a = ap.parse_args()
    export(a.slug or reg.get_active(), a.dest)
