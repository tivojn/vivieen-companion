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
import os, json, shutil
import numpy as np, cv2
from . import face, blink, expression, cutout, limbs, build as reg

# runtime viseme name -> studio shape name
NAME_MAP = {"sil": "closed", "PP": "PP", "FF": "FF", "TH": "TH", "DD": "DD",
            "kk": "kk", "CH": "CH", "SS": "SS", "nn": "nn", "RR": "RR",
            "aa": "ah", "E": "eh", "ih": "ih", "oh": "oh", "ou": "oo"}


def _runtime_body_metadata(source):
    runtime = dict(source)
    runtime.pop("views", None)
    runtime.pop("turnaround", None)
    runtime.pop("motion_reference", None)
    return runtime


def _body_pose(body_dir, log=print):
    """Skeleton joints of the standing plate, in body-plate pixels.

    Baked once and cached beside the body: the runtime classifies a click by
    its nearest bone segment (arm, hand, torso, leg), so the pet can react to
    the part that was actually touched instead of treating the whole
    silhouette as one button. Head stays mask-exact and is not part of this.
    """
    cache = os.path.join(body_dir, "pose.json")
    source = next((os.path.join(body_dir, name)
                   for name in ("source-front.png", "source.png")
                   if os.path.isfile(os.path.join(body_dir, name))), None)
    if not source:
        return None
    if os.path.isfile(cache) and os.path.getmtime(cache) >= os.path.getmtime(source):
        try:
            with open(cache) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
    import tempfile
    with tempfile.TemporaryDirectory() as work:
        pose_path = os.path.join(work, "pose.json")
        cutout.render(source, os.path.join(work, "cut.png"),
                      log=lambda _m: None, tight=True,
                      pose_destination=pose_path)
        try:
            with open(pose_path) as handle:
                pose = json.load(handle)
        except (OSError, ValueError):
            return None
    joints = {}
    for name, joint in (pose.get("joints") or {}).items():
        try:
            if float(joint.get("confidence", 0)) < 0.3:
                continue
            joints[name] = {
                "x": round(float(joint["x"]), 1),
                "y": round(float(joint["y"]), 1),
                "confidence": round(float(joint["confidence"]), 2),
            }
        except (KeyError, TypeError, ValueError):
            continue
    if len(joints) < 6:
        log("  body skeleton too sparse; part reactions limited to the head")
        return None
    result = {"joints": joints}
    with open(cache, "w") as handle:
        json.dump(result, handle, indent=1)
    log(f"  body skeleton baked: {len(joints)} joints")
    return result


def _publish_body_extras(body_dir, body_meta, destination, log):
    """Skeleton + limb-reaction strips for the standing plate."""
    for name in os.listdir(destination):
        if name.startswith("react_") and name.endswith(".png"):
            os.remove(os.path.join(destination, name))
    pose = _body_pose(body_dir, log=log)
    if not pose:
        return
    body_meta["pose"] = pose
    plate = cv2.imread(os.path.join(destination, "body.png"),
                       cv2.IMREAD_UNCHANGED)
    if plate is None or plate.ndim != 3 or plate.shape[2] != 4:
        return
    reactions = {}
    for name, reaction in limbs.build(plate, pose, log=log).items():
        strip = f"react_{name}.png"
        cv2.imwrite(os.path.join(destination, strip),
                    np.vstack(reaction["patches"]),
                    [cv2.IMWRITE_PNG_COMPRESSION, 9])
        reactions[name] = {
            "src": f"assets/{strip}",
            "box": reaction["box"],
            "states": len(reaction["patches"]),
        }
    if reactions:
        body_meta["reactions"] = reactions


def _publish_motion(directory, destination, log):
    for name in os.listdir(destination):
        if name.startswith("motion-") and name.endswith(".png"):
            os.remove(os.path.join(destination, name))
    motion_dir = os.path.join(directory, "motion")
    manifest_path = os.path.join(motion_dir, "motion.json")
    if not os.path.isfile(manifest_path):
        return None
    with open(manifest_path) as handle:
        source = json.load(handle)
    runtime = {"v": source.get("v", 1)}
    published = False
    for kind in ("walk", "idle"):
        clip = dict(source.get(kind) or {})
        if not clip.get("sheets"):
            continue
        sheets = []
        for index, sheet in enumerate(clip.get("sheets") or []):
            name = f"motion-{kind}-{index}.png"
            shutil.copy2(os.path.join(motion_dir, sheet["image"]), os.path.join(destination, name))
            sheets.append({**sheet, "image": f"assets/{name}"})
        poster_name = f"motion-{kind}-poster.png"
        if clip.get("poster") and os.path.isfile(os.path.join(motion_dir, clip["poster"])):
            shutil.copy2(os.path.join(motion_dir, clip["poster"]),
                         os.path.join(destination, poster_name))
            clip["poster"] = f"assets/{poster_name}"
        clip["sheets"] = sheets
        clip.pop("alpha_video", None)
        clip.pop("source_loop", None)
        runtime[kind] = clip
        published = True
    if not published:
        return None
    log("  alpha Pet motion published")
    return runtime


def publish_pet_assets(slug, runtime_dir=None, log=print):
    """Add Pet layers without rebuilding the calibrated face bank."""
    directory = reg.adir(slug)
    destination = runtime_dir or os.path.join(directory, "runtime")
    manifest_path = os.path.join(destination, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ValueError("avatar runtime is missing")
    with open(manifest_path) as handle:
        runtime = json.load(handle)
    source_manifest = reg.read_manifest(slug) or {}
    cutout_meta = cutout.render(
        os.path.join(directory, "keyframe.png"),
        os.path.join(destination, "cutout.png"),
        log=log,
    )
    body_meta = None
    body_dir = os.path.join(directory, "body")
    body_manifest = os.path.join(body_dir, "body.json")
    if os.path.isfile(body_manifest):
        with open(body_manifest) as handle:
            body_meta = _runtime_body_metadata(json.load(handle))
        shutil.copy2(os.path.join(body_dir, "body.png"), os.path.join(destination, "body.png"))
        shutil.copy2(os.path.join(body_dir, "head-mask.png"), os.path.join(destination, "head-mask.png"))
        body_meta["image"] = "assets/body.png"
        body_meta["head_mask"] = "assets/head-mask.png"
        _publish_body_extras(body_dir, body_meta, destination, log)
    else:
        for name in ("body.png", "head-mask.png"):
            try:
                os.remove(os.path.join(destination, name))
            except FileNotFoundError:
                pass
    motion_meta = _publish_motion(directory, destination, log)
    runtime.update(
        v=max(12, int(runtime.get("v", 0))),
        cutout=cutout_meta,
        body=body_meta,
        motion=motion_meta,
        built=source_manifest.get("updated", runtime.get("built")),
    )
    temporary = manifest_path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(runtime, handle, indent=1)
    os.replace(temporary, manifest_path)
    log("Pet runtime layers published")
    return runtime


def export(slug, dest, quality=92, states=blink.N_STATES, log=print,
           source_dir=None, manifest_data=None):
    d = source_dir or reg.adir(slug)
    m = manifest_data or reg.read_manifest(slug)
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
    log("extracting transparent person silhouette")
    cutout_meta = cutout.render(
        os.path.join(d, "keyframe.png"),
        os.path.join(dest, "cutout.png"),
        log=log,
    )
    body_meta = None
    body_dir = os.path.join(d, "body")
    body_manifest = os.path.join(body_dir, "body.json")
    if os.path.isfile(body_manifest):
        with open(body_manifest) as handle:
            body_meta = _runtime_body_metadata(json.load(handle))
        shutil.copy2(os.path.join(body_dir, "body.png"), os.path.join(dest, "body.png"))
        shutil.copy2(os.path.join(body_dir, "head-mask.png"), os.path.join(dest, "head-mask.png"))
        body_meta["image"] = "assets/body.png"
        body_meta["head_mask"] = "assets/head-mask.png"
        _publish_body_extras(body_dir, body_meta, dest, log)
        log("  full-body plate published")
    motion_meta = _publish_motion(d, dest, log)

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
    manifest = dict(v=12, w=W, h=H, avatar=dict(slug=slug, name=m["name"]),
                    visemes=names, frames=frames, eyes=eyes, gaze=gaze, brow=brow,
                    cheek=cheek, neck=expression.neck(klm), cutout=cutout_meta,
                    body=body_meta, motion=motion_meta, blink=timing,
                    built=m.get("updated"), quality=m.get("quality"),
                    rig_profile=m.get("rig_profile"))
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
