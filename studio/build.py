"""Avatar registry + build orchestrator.

An avatar is a self-contained folder:

    avatars/<slug>/
        source.png        the photo the user uploaded
        keyframe.png      face-centred 1024 square everything is built on
        raw/v_*.png       untouched generator output (kept for re-composing)
        visemes/v_*.jpg   pose-locked, mouth-only composites - the shipping bank
        preview.mp4       cross-blended demo sentence
        sheet.jpg         mouth-zoom contact sheet of the whole bank
        diag/             masks, landmark overlay, per-shape metrics
        manifest.json     status, metrics, warnings, build log

Swapping the avatar is therefore just pointing `active.json` at another slug -
nothing else in the project is avatar-specific.
"""
import os, re, json, time, shutil, datetime, threading, traceback, tempfile
from . import prep, generate, compose, render, visemes, measure

CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.abspath(os.environ.get("VIVIEEN_DATA_DIR", CODE_ROOT))
AVATARS = os.path.join(ROOT, "avatars")
ACTIVE = os.path.join(ROOT, "active.json")
_locks = {}
_write_lock = threading.Lock()   # progress callbacks fire from worker threads
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")


def valid_slug(slug):
    return isinstance(slug, str) and bool(_SLUG.fullmatch(slug))


def slugify(s):
    value = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "avatar").strip()).strip("-").lower()
    return (value[:63].rstrip("-") or "avatar")


def adir(slug):
    if not valid_slug(slug):
        raise ValueError("invalid avatar slug")
    root = os.path.abspath(AVATARS)
    full = os.path.abspath(os.path.join(root, slug))
    if os.path.commonpath((root, full)) != root:
        raise ValueError("avatar path escapes the registry")
    return full


def manifest_path(slug):
    return os.path.join(adir(slug), "manifest.json")


def read_manifest(slug):
    try:
        with open(manifest_path(slug)) as f:
            return json.load(f)
    except Exception:
        return None


def write_manifest(slug, m):
    """Atomic and thread-safe: worker threads report progress concurrently, so a
    shared temp filename would let one thread rename the file out from under
    another."""
    with _write_lock:
        os.makedirs(adir(slug), mode=0o700, exist_ok=True)
        m["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        tmp = f"{manifest_path(slug)}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(m, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, manifest_path(slug))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return m


def list_avatars():
    os.makedirs(AVATARS, mode=0o700, exist_ok=True)
    out = []
    for slug in sorted(os.listdir(AVATARS)):
        if valid_slug(slug) and os.path.isdir(adir(slug)):
            m = read_manifest(slug)
            if m:
                out.append(m)
    active = get_active()
    for m in out:
        m["active"] = (m["slug"] == active)
    return out


def get_active():
    try:
        with open(ACTIVE) as f:
            slug = json.load(f).get("slug")
        return slug if valid_slug(slug) else None
    except Exception:
        return None


def set_active(slug):
    if not os.path.isdir(adir(slug)):
        raise ValueError(f"unknown avatar: {slug}")
    os.makedirs(ROOT, mode=0o700, exist_ok=True)
    descriptor, tmp = tempfile.mkstemp(prefix=".active-", dir=ROOT)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(dict(slug=slug,
                           set_at=datetime.datetime.now().isoformat(timespec="seconds")),
                      handle, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, ACTIVE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return slug


def delete_avatar(slug):
    shutil.rmtree(adir(slug), ignore_errors=True)
    if get_active() == slug:
        try:
            os.remove(ACTIVE)
        except OSError:
            pass


# ---------------------------------------------------------------- create

def create_avatar(image_path, name=None, slug=None):
    """Register an uploaded photo and prepare its keyframe.  No generation yet -
    this returns fast so the UI can show the crop and any pose warnings first."""
    name = name or os.path.splitext(os.path.basename(image_path))[0]
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", str(name)).strip()[:120] or "Avatar"
    slug = slugify(slug or name)
    base, index = slug, 2
    while os.path.isdir(adir(slug)):
        suffix = f"-{index}"
        slug = f"{base[:63 - len(suffix)].rstrip('-')}{suffix}"
        index += 1

    d = adir(slug)
    os.makedirs(d, mode=0o700, exist_ok=True)
    src = os.path.join(d, "source" + os.path.splitext(image_path)[1].lower())
    if os.path.abspath(image_path) != os.path.abspath(src):
        shutil.copyfile(image_path, src)
    os.chmod(src, 0o600)

    key = os.path.join(d, "keyframe.png")
    metrics = prep.build_keyframe(src, key, diag_dir=os.path.join(d, "diag"))

    return write_manifest(slug, dict(
        slug=slug, name=name,
        created=datetime.datetime.now().isoformat(timespec="seconds"),
        source=os.path.basename(src), keyframe="keyframe.png",
        status="draft", progress=dict(done=0, total=len(visemes.ORDER)),
        metrics=metrics, warnings=metrics.get("warnings", []),
        visemes=[], preview=None, sheet=None, log=[]))


# ---------------------------------------------------------------- build

def build_avatar(slug, shapes=None, log=None, quality="high"):
    d = adir(slug)
    m = read_manifest(slug)
    if not m:
        raise ValueError(f"unknown avatar: {slug}")
    lines = []

    def emit(msg):
        lines.append(str(msg))
        m["log"] = lines[-400:]
        if log:
            log(msg)
        else:
            print(msg, flush=True)

    key = os.path.join(d, "keyframe.png")
    raw, out = os.path.join(d, "raw"), os.path.join(d, "visemes")
    diag = os.path.join(d, "diag")
    os.makedirs(raw, exist_ok=True); os.makedirs(out, exist_ok=True)
    names = list(shapes or visemes.ORDER)
    unknown = sorted(set(names) - set(visemes.ORDER))
    if unknown:
        raise ValueError(f"unknown viseme shapes: {', '.join(unknown)}")

    m["status"] = "building"
    m["progress"] = dict(done=0, total=len(names), stage="render")
    m["log"] = []
    m.pop("error", None)          # a retry must not inherit the last failure
    write_manifest(slug, m)

    try:
        yaw = m["metrics"].get("yaw")
        roll = m["metrics"].get("roll")
        emit(f"keyframe pose: yaw {yaw:+.1f} pitch {m['metrics'].get('pitch'):+.1f} "
             f"roll {roll:+.1f}, foreshortening {m['metrics']['foreshortening']:.2f}, "
             f"mouth {m['metrics']['mouth_width_px']:.0f}px")
        emit(f"rendering {len(names)} shapes ({generate.MAX_WORKERS} at a time)...")

        done = [0]
        def on_done(name, path):
            done[0] += 1
            m["progress"] = dict(done=done[0], total=len(names), stage="render", current=name)
            write_manifest(slug, m)

        got = generate.generate_set(key, raw, yaw=yaw, roll=roll, names=names,
                                    log=emit, on_done=on_done, quality=quality)
        missing = [n for n, p in got.items() if not p]
        if missing:
            emit(f"WARNING: no render for {', '.join(missing)}")

        emit("pose-locking and compositing...")
        m["progress"] = dict(done=len(names), total=len(names), stage="compose")
        write_manifest(slug, m)
        report, kmet = compose.compose_all(key, raw, out, diag_dir=diag, log=emit)

        emit("checking mouth amplitude...")
        aperture, over = measure.audit(key, out, log=emit)
        if over:
            emit("over-articulated: " + ", ".join(r["name"] for r in over))

        emit("rendering preview...")
        m["progress"] = dict(done=len(names), total=len(names), stage="preview")
        write_manifest(slug, m)
        render.preview(out, os.path.join(d, "preview.mp4"))
        render.contact_sheet(out, key, os.path.join(d, "sheet.jpg"))

        worst = max((r["resid_px"] for r in report), default=0)
        drift = max((r["outside_delta"] for r in report), default=0)
        emit(f"done - {len(report)} shapes, worst rigid residual {worst:.2f}px, "
             f"worst off-region drift {drift:.4f}")

        m.update(status="ready", visemes=report, keyframe_metrics=kmet,
                 aperture=aperture, over_articulated=[r["name"] for r in over],
                 preview="preview.mp4", sheet="sheet.jpg",
                 quality=dict(worst_resid_px=worst, worst_off_region_delta=drift,
                              shapes=len(report), missing=missing))
        m["progress"] = dict(done=len(names), total=len(names), stage="done")
    except Exception as e:
        emit("ERROR: " + str(e))
        emit(traceback.format_exc()[-1500:])
        m["status"] = "error"
        m["error"] = str(e)
    write_manifest(slug, m)
    return m


def build_async(slug, **kw):
    if _locks.get(slug) and _locks[slug].is_alive():
        return False
    t = threading.Thread(target=build_avatar, args=(slug,), kwargs=kw, daemon=True)
    _locks[slug] = t
    t.start()
    return True


# ---------------------------------------------------------------- cli

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="avatar-studio", description="Build a talking-head viseme bank from any portrait.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="register a photo and prepare its keyframe")
    a.add_argument("image"); a.add_argument("--name"); a.add_argument("--slug")
    a.add_argument("--build", action="store_true", help="generate the full set right away")
    b = sub.add_parser("build", help="generate + compose + preview")
    b.add_argument("slug"); b.add_argument("--shapes", nargs="*")
    sub.add_parser("list", help="list avatars")
    c = sub.add_parser("activate"); c.add_argument("slug")
    e = sub.add_parser("delete"); e.add_argument("slug")
    args = ap.parse_args()

    if args.cmd == "add":
        m = create_avatar(args.image, args.name, args.slug)
        print(f"{m['slug']}: keyframe ready")
        for w in m["warnings"]:
            print("  warning:", w)
        if args.build:
            build_avatar(m["slug"])
    elif args.cmd == "build":
        build_avatar(args.slug, shapes=args.shapes)
    elif args.cmd == "list":
        for m in list_avatars():
            print(f"{'*' if m.get('active') else ' '} {m['slug']:24s} {m['status']:9s} "
                  f"{len(m.get('visemes') or [])} shapes")
    elif args.cmd == "activate":
        print("active ->", set_active(args.slug))
    elif args.cmd == "delete":
        delete_avatar(args.slug); print("deleted", args.slug)
