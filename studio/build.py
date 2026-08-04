"""Avatar registry + build orchestrator.

An avatar is a self-contained folder:

    avatars/<slug>/
        source.png        the photo the user uploaded
        source-keyframe.png immutable crop of the uploaded photo
        head.png          generated head-only identity reference
        keyframe.png      face-centred 1024 square built from head.png
        raw/v_*.png       untouched generator output (kept for re-composing)
        visemes/v_*.jpg   pose-locked, mouth-only composites - the shipping bank
        preview.mp4       cross-blended demo sentence
        sheet.jpg         mouth-zoom contact sheet of the whole bank
        diag/             masks, landmark overlay, per-shape metrics
        manifest.json     status, metrics, warnings, build log

Swapping the avatar is therefore just pointing `active.json` at another slug -
nothing else in the project is avatar-specific.
"""
import os, re, json, time, shutil, datetime, threading, traceback, tempfile, copy, uuid
from . import anatomy, prep, generate, compose, render, visemes, measure, rig

CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.abspath(os.environ.get("VIVIEEN_DATA_DIR", CODE_ROOT))
AVATARS = os.path.join(ROOT, "avatars")
ACTIVE = os.path.join(ROOT, "active.json")
# The optional second on-desk avatar. It renders in its own desktop window,
# mirrored to the LEFT screen edge while the active avatar owns the right.
COMPANION = os.path.join(ROOT, "companion.json")
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


def get_companion():
    try:
        with open(COMPANION) as f:
            slug = json.load(f).get("slug")
        return slug if valid_slug(slug) else None
    except Exception:
        return None


def set_companion(slug):
    """Set (or with None/empty, clear) the second on-desk avatar."""
    if not slug:
        try:
            os.remove(COMPANION)
        except OSError:
            pass
        return None
    if not os.path.isdir(adir(slug)):
        raise ValueError(f"unknown avatar: {slug}")
    os.makedirs(ROOT, mode=0o700, exist_ok=True)
    descriptor, tmp = tempfile.mkstemp(prefix=".companion-", dir=ROOT)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(dict(slug=slug,
                           set_at=datetime.datetime.now().isoformat(timespec="seconds")),
                      handle, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, COMPANION)
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
    if get_companion() == slug:
        set_companion(None)


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
    ext = os.path.splitext(image_path)[1].lower()
    if ext in prep.HEIC_EXTENSIONS:
        # Everything downstream reads the stored source with OpenCV, which
        # has no HEIC codec - so the source of record becomes a PNG.
        src = os.path.join(d, "source.png")
        prep.decode_heic(image_path, src)
    else:
        src = os.path.join(d, "source" + ext)
        if os.path.abspath(image_path) != os.path.abspath(src):
            shutil.copyfile(image_path, src)
    os.chmod(src, 0o600)

    source_key = os.path.join(d, "source-keyframe.png")
    key = os.path.join(d, "keyframe.png")
    metrics = prep.build_keyframe(
        src, source_key, diag_dir=os.path.join(d, "diag"))
    shutil.copy2(source_key, key)

    return write_manifest(slug, dict(
        slug=slug, name=name,
        created=datetime.datetime.now().isoformat(timespec="seconds"),
        source=os.path.basename(src), source_keyframe="source-keyframe.png",
        keyframe="keyframe.png",
        status="draft", progress=dict(done=0, total=len(visemes.ORDER)),
        metrics=metrics, warnings=metrics.get("warnings", []),
        visemes=[], preview=None, sheet=None, log=[]))


# ---------------------------------------------------------------- build

RIG_ARTIFACTS = ("visemes", "diag", "runtime", "preview.mp4", "sheet.jpg")


def _band_suggestion(keys):
    """Human guidance for a red line: the green band of each named slider."""
    parts = []
    for key in keys:
        spec = rig.CONTROLS.get(key)
        if not spec:
            continue
        parts.append(f"{spec['label']} {spec.get('safe_minimum', 0):.0f}–"
                     f"{spec.get('safe_maximum', 100):.0f}%")
    return ", ".join(parts) or "ease the sliders toward their green bands"


def _articulation_failure(row):
    if row["ratio"] > row["max_ratio"]:
        return (f"{row['name']} aperture {row['ratio']:.3f} exceeds "
                f"{row['max_ratio']:.2f}")
    minimum = row["want_width"] - 0.12
    maximum = row["want_width"] + 0.12
    return (f"{row['name']} width {row['width_ratio']:.2f}x neutral is outside "
            f"{minimum:.2f}-{maximum:.2f} (target {row['want_width']:.2f})")


# A tilted source selfie must not become a tilted avatar: the canonical
# head is prompted frontal, but providers sometimes keep the source pose
# (rachel, 2026-08-01: yaw -9.1, pitch 23, roll 18, foreshortening 0.56 -
# and every mouth stage after degrades: viseme transfer, landmark accuracy,
# the dental band). Measured limits; outside them the head regenerates with
# a corrective note, best candidate wins, and a stubborn tilt ships with an
# ADVISORY - never a block.
FRONTAL_YAW = 8.0
FRONTAL_ROLL = 6.0
FRONTAL_PITCH = (-6.0, 16.0)
FRONTAL_FORESHORTENING = 0.85


def _frontality_issues(metrics):
    issues = []
    yaw = float(metrics.get("yaw") or 0.0)
    roll = float(metrics.get("roll") or 0.0)
    pitch = float(metrics.get("pitch") or 0.0)
    depth = float(metrics.get("foreshortening") or 1.0)
    if abs(yaw) > FRONTAL_YAW:
        issues.append(f"yaw {yaw:+.1f}deg (want within +/-{FRONTAL_YAW:.0f})")
    if abs(roll) > FRONTAL_ROLL:
        issues.append(f"roll {roll:+.1f}deg (want within +/-{FRONTAL_ROLL:.0f})")
    if not FRONTAL_PITCH[0] <= pitch <= FRONTAL_PITCH[1]:
        issues.append(f"pitch {pitch:+.1f}deg (want {FRONTAL_PITCH[0]:.0f}"
                      f"..{FRONTAL_PITCH[1]:.0f})")
    if depth < FRONTAL_FORESHORTENING:
        issues.append(f"foreshortening {depth:.2f} "
                      f"(want >= {FRONTAL_FORESHORTENING:.2f})")
    return issues


def _frontality_score(metrics):
    yaw = abs(float(metrics.get("yaw") or 0.0)) / FRONTAL_YAW
    roll = abs(float(metrics.get("roll") or 0.0)) / FRONTAL_ROLL
    pitch = float(metrics.get("pitch") or 0.0)
    pitch_excess = max(FRONTAL_PITCH[0] - pitch, pitch - FRONTAL_PITCH[1], 0.0) / 10.0
    depth = max(0.0, FRONTAL_FORESHORTENING
                - float(metrics.get("foreshortening") or 1.0)) / 0.10
    return yaw + roll + pitch_excess + depth


def raw_render_gaps(slug):
    raw_dir = os.path.join(adir(slug), "raw")
    return [name for name in visemes.ORDER
            if not any(os.path.exists(os.path.join(raw_dir, f"v_{name}.{ext}"))
                       for ext in ("png", "jpg"))]


def _remove_artifact(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)


def _snapshot_live(slug, prefix="rollback.rig"):
    directory = adir(slug)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    name = f"{prefix}-{stamp}"
    destination = os.path.join(directory, name)
    if os.path.exists(destination):
        destination += f"-{uuid.uuid4().hex[:6]}"
        name = os.path.basename(destination)
    os.makedirs(destination)
    for artifact in RIG_ARTIFACTS:
        source = os.path.join(directory, artifact)
        target = os.path.join(destination, artifact)
        if os.path.isdir(source):
            shutil.copytree(source, target)
        elif os.path.isfile(source):
            shutil.copy2(source, target)
    manifest_file = os.path.join(directory, "manifest.json")
    if os.path.isfile(manifest_file):
        shutil.copy2(
            manifest_file, os.path.join(destination, "manifest.json"))
    return name


def _publish_stage(slug, stage_dir, manifest):
    directory = adir(slug)
    missing = [artifact for artifact in RIG_ARTIFACTS
               if not os.path.exists(os.path.join(stage_dir, artifact))]
    if missing:
        raise RuntimeError(f"staging is incomplete: {', '.join(missing)}")
    displaced = tempfile.mkdtemp(prefix=".rig-live-", dir=directory)
    live_manifest = os.path.join(directory, "manifest.json")
    displaced_manifest = os.path.join(displaced, "manifest.json")
    moved_new = []
    try:
        if os.path.isfile(live_manifest):
            os.replace(live_manifest, displaced_manifest)
        for artifact in RIG_ARTIFACTS:
            live = os.path.join(directory, artifact)
            if os.path.exists(live):
                os.replace(live, os.path.join(displaced, artifact))
        for artifact in RIG_ARTIFACTS:
            staged = os.path.join(stage_dir, artifact)
            os.replace(staged, os.path.join(directory, artifact))
            moved_new.append(artifact)
        write_manifest(slug, manifest)
    except Exception:
        for artifact in moved_new:
            _remove_artifact(os.path.join(directory, artifact))
        for artifact in RIG_ARTIFACTS:
            previous = os.path.join(displaced, artifact)
            if os.path.exists(previous):
                os.replace(previous, os.path.join(directory, artifact))
        if os.path.exists(live_manifest):
            os.unlink(live_manifest)
        if os.path.isfile(displaced_manifest):
            os.replace(displaced_manifest, live_manifest)
        raise
    finally:
        shutil.rmtree(displaced, ignore_errors=True)


def recompose_avatar(slug, profile, log=print, progress=None):
    manifest = read_manifest(slug)
    if not manifest or manifest.get("status") != "ready":
        raise ValueError(f"{slug} is not ready for calibration")
    profile = rig.normalize(profile)
    gaps = raw_render_gaps(slug)
    if gaps:
        raise ValueError(f"missing retained renders: {', '.join(gaps)}")
    directory = adir(slug)
    stage = tempfile.mkdtemp(prefix=".rig-stage-", dir=directory)
    lines = []

    def emit(message):
        text = str(message)
        lines.append(text)
        log(text)

    def advance(stage_name, value, message):
        if progress:
            progress(stage_name, value, message)
        emit(message)

    try:
        stage_visemes = os.path.join(stage, "visemes")
        stage_diag = os.path.join(stage, "diag")
        stage_runtime = os.path.join(stage, "runtime")
        stage_keyframe = os.path.join(stage, "keyframe.png")
        shutil.copy2(
            os.path.join(directory, "keyframe.png"), stage_keyframe)
        advance("compose", .08, "Recomposing retained local renders")
        report, key_metrics = compose.compose_all(
            stage_keyframe, os.path.join(directory, "raw"),
            stage_visemes, diag_dir=stage_diag, log=emit,
            profile=profile)
        expected = len(visemes.ORDER)
        if len(report) != expected:
            raise AssertionError(
                f"staged bank has {len(report)} of {expected} required shapes")
        advance("articulation", .48, "Checking mouth articulation")
        aperture, over = measure.audit(
            stage_keyframe, stage_visemes, log=emit,
            names=visemes.SPEECH_ORDER)
        # The user's contract: a REBUILD never blocks on articulation.
        # These are retained renders re-composed to the user's chosen
        # profile - green band or red, an overshoot is a look, not a
        # defect. Everything publishes and reports with the suggested
        # green bands. (Generation-time audits keep their strict gates -
        # there a rejected candidate is retried for free.)
        experimental = anatomy._experimental_keys(profile)
        soft_overs = list(over)
        for row in soft_overs:
            emit(f"  ADVISORY {row['name']} runs {row['ratio']:.3f} against "
                 f"target {row['max_ratio']:.2f} - published with this "
                 "experimental calibration")
        if experimental:
            emit(f"  ADVISORY experimental targets in play - "
                 f"{_band_suggestion(experimental)}")
        advance("preview", .58, "Rendering local preview")
        render.preview(
            stage_visemes, os.path.join(stage, "preview.mp4"))
        render.contact_sheet(
            stage_visemes, stage_keyframe,
            os.path.join(stage, "sheet.jpg"))
        advance("anatomy", .70, "Running anatomy QA")
        qa = anatomy.validate(
            stage_keyframe, stage_visemes, profile, diag_dir=stage_diag)
        emit("anatomy QA passed: " + anatomy.summary(qa))
        for warning in ((qa.get("structure_warnings") or [])
                        + (qa.get("dental_warnings") or [])):
            emit(f"  ADVISORY {warning}")
        worst_residual = max(row["resid_px"] for row in report)
        worst_drift = max(row["outside_delta"] for row in report)
        next_manifest = copy.deepcopy(manifest)
        next_manifest.update(
            status="ready",
            visemes=report,
            keyframe_metrics=key_metrics,
            aperture=aperture,
            over_articulated=[row["name"] for row in soft_overs],
            preview="preview.mp4",
            sheet="sheet.jpg",
            rig_profile=profile,
            rig_qa=qa,
            rebuild_mode="local_recompose",
            quality=dict(worst_resid_px=worst_residual,
                         worst_off_region_delta=worst_drift,
                         shapes=len(report), missing=[]),
            progress=dict(done=len(report), total=len(report),
                          stage="done"),
            log=lines[-400:],
        )
        next_manifest.pop("error", None)
        from . import export
        advance("runtime", .78, "Exporting runtime sprite strips")
        export.export(
            slug, stage_runtime, log=emit, source_dir=stage,
            manifest_data=next_manifest)
        advance("snapshot", .93, "Snapshotting the published avatar for rollback")
        rollback_name = _snapshot_live(slug)
        next_manifest["last_rollback"] = rollback_name
        advance("publish", .97,
                f"Publishing calibrated runtime; rollback {rollback_name}")
        next_manifest["log"] = lines[-400:]
        _publish_stage(slug, stage, next_manifest)
        if progress:
            progress("done", 1.0, "Published")
        return read_manifest(slug)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def build_avatar(slug, shapes=None, log=None, quality="high", notes=""):
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
        source_keyframe = os.path.join(
            d, m.get("source_keyframe") or "source-keyframe.png")
        if not os.path.isfile(source_keyframe):
            source_image = os.path.join(d, m.get("source") or "")
            if os.path.isfile(source_image):
                prep.build_keyframe(source_image, source_keyframe)
            else:
                shutil.copy2(key, source_keyframe)
            m["source_keyframe"] = os.path.basename(source_keyframe)

        m["progress"] = dict(done=0, total=len(names), stage="head")
        write_manifest(slug, m)
        emit("creating canonical HD head-only identity reference...")
        head_path = os.path.join(d, "head.png")
        head_provider = generate.default_head_provider()
        staged_keyframe = os.path.join(d, ".head-keyframe.png")
        best = None
        pose_note = ""
        for pose_attempt in range(3):
            generate.generate_head(
                source_keyframe, head_path, provider=head_provider,
                log=emit, quality=quality, pose_note=pose_note,
                keep=notes, overwrite=bool(pose_attempt))
            head_metrics = prep.build_keyframe(
                head_path, staged_keyframe, diag_dir=diag)
            issues = _frontality_issues(head_metrics)
            score = _frontality_score(head_metrics)
            if best is None or score < best[0]:
                shutil.copy2(head_path, head_path + ".best")
                shutil.copy2(staged_keyframe, staged_keyframe + ".best")
                best = (score, issues, head_metrics)
            if not issues:
                break
            emit(f"  head pose off-frontal: {'; '.join(issues)}"
                 + (f" - regenerating (retry {pose_attempt + 1}/2)"
                    if pose_attempt < 2 else ""))
            pose_note = (
                "\n\nPOSE CORRECTION - a previous attempt measured "
                + "; ".join(issues)
                + ". Render the head PERFECTLY FRONTAL this time: zero yaw, "
                  "zero roll, camera exactly at eye level with no upward or "
                  "downward tilt, both ears equally visible.")
        os.replace(head_path + ".best", head_path)
        os.replace(staged_keyframe + ".best", staged_keyframe)
        score, issues, head_metrics = best
        if issues:
            emit("ADVISORY head is not fully frontal after retries: "
                 + "; ".join(issues)
                 + " - mouth and dental quality may suffer; a straighter, "
                   "camera-level source photo gives the best result")
        os.replace(staged_keyframe, key)
        m.setdefault("source_metrics", copy.deepcopy(m.get("metrics") or {}))
        m["metrics"] = head_metrics
        m["head"] = dict(
            image="head.png",
            source=os.path.basename(source_keyframe),
            prompt_version=generate.HEAD_PROMPT_VERSION,
            provider=head_provider.get("name"),
            model=head_provider.get("model"),
        )
        write_manifest(slug, m)

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
        profile = rig.from_manifest(m)
        report, kmet = compose.compose_all(
            key, raw, out, diag_dir=diag, log=emit, profile=profile)

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
                 rig_profile=profile,
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
    b.add_argument("--keep", default="",
                   help="what must survive the build, e.g. his bandana")
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
        build_avatar(args.slug, shapes=args.shapes, notes=args.keep)
    elif args.cmd == "list":
        for m in list_avatars():
            print(f"{'*' if m.get('active') else ' '} {m['slug']:24s} {m['status']:9s} "
                  f"{len(m.get('visemes') or [])} shapes")
    elif args.cmd == "activate":
        print("active ->", set_active(args.slug))
    elif args.cmd == "delete":
        delete_avatar(args.slug); print("deleted", args.slug)
