"""Vivieen - one local process: chat, voice, and the avatar factory.

The avatar studio used to be a separate server on another port. Merging it in
is not tidying: the runtime has to be able to SWAP the face while it is running,
and two processes with two views of which avatar is active is exactly how you
end up serving a manifest that describes a different head than the sprites.

Assets are no longer a folder of files inside web/. Each avatar owns its own
runtime bundle at avatars/<slug>/runtime/, and /assets/* resolves through the
active slug on every request. Activating a face is therefore one atomic write
to active.json, and no file is ever copied over another.
"""
import os, sys, io, json, base64, tempfile, threading, time, shutil, subprocess, secrets, asyncio
from contextlib import asynccontextmanager
os.environ["PATH"] = os.pathsep.join(filter(None, (
    os.path.expanduser("~/.config/enconvo/bin"), "/opt/homebrew/bin",
    "/usr/local/bin", os.environ.get("PATH", ""))))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel, Field

import providers as P
import align
from studio import rig

WEB = os.path.join(ROOT, "web")


@asynccontextmanager
async def lifespan(_application):
    _start()
    yield


app = FastAPI(title="Vivieen", lifespan=lifespan)
APP_ID = "com.vivieen.companion"
AUTH_TOKEN = os.environ.get("VIVIEEN_AUTH_TOKEN", "")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,62})$"
CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
       "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; font-src 'self' data:; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _security_headers(response):
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
    return response


@app.middleware("http")
async def security_headers(request, call_next):
    if AUTH_TOKEN:
        supplied = request.headers.get("x-vivieen-token", "")
        if not secrets.compare_digest(supplied, AUTH_TOKEN):
            return _security_headers(JSONResponse({"error": "forbidden"}, status_code=403))
    origin = request.headers.get("origin", "")
    if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
        allowed_origin = f"{request.url.scheme}://{request.url.netloc}"
        if origin != allowed_origin:
            response = JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
            return _security_headers(response)
    return _security_headers(await call_next(request))


_state = {"warm": False, "warming": ""}
_jobs = {}                      # slug -> live build/calibration state
_jlock = threading.Lock()


def _reserve_job(slug, kind, label="Queued"):
    with _jlock:
        current = _jobs.get(slug)
        if current and not current.get("done"):
            return None
        job_id = secrets.token_hex(12)
        _jobs[slug] = dict(
            id=job_id, phase=label, done=False, error="", log=[], kind=kind,
            progress={"stage": "queued", "value": 0.0, "label": label})
        return job_id


def _finish_job(slug, job_id, error=""):
    with _jlock:
        job = _jobs.get(slug)
        if not job or job.get("id") != job_id:
            return
        job["error"] = str(error or job.get("error") or "")
        job["done"] = True


def _already_running(slug):
    with _jlock:
        job_id = (_jobs.get(slug) or {}).get("id")
    return {
        "started": False,
        "reason": "already building",
        "job_id": job_id,
    }


def reg():
    from studio import build as _r
    return _r


# ---------------------------------------------------------------- avatars

def runtime_dir(slug):
    return os.path.join(reg().adir(slug), "runtime")


def _runtime_manifest(directory):
    manifest_path = os.path.join(directory, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ValueError("runtime manifest is missing")
    with open(manifest_path) as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ValueError("runtime manifest is not an object")
    return manifest


def _runtime_asset(directory, reference):
    if not isinstance(reference, str) or not reference.startswith("assets/"):
        raise ValueError("runtime asset reference is invalid")
    root = os.path.abspath(directory)
    asset = os.path.abspath(os.path.join(root, reference[len("assets/"):]))
    if os.path.commonpath((root, asset)) != root:
        raise ValueError("runtime asset escapes its bundle")
    if not os.path.isfile(asset) or os.path.getsize(asset) <= 0:
        raise ValueError(f"runtime asset is missing: {reference}")


def _validate_runtime_bundle(directory, expect_motion=None):
    manifest = _runtime_manifest(directory)
    runtime_motion = manifest.get("motion")
    if expect_motion is False and runtime_motion:
        raise ValueError("runtime still contains motion after removal")
    available = []
    if runtime_motion:
        if not isinstance(runtime_motion, dict):
            raise ValueError("runtime motion metadata is missing")
        for kind in ("walk", "idle"):
            clip = runtime_motion.get(kind)
            if not clip:
                continue
            if not isinstance(clip, dict) or not clip.get("sheets"):
                raise ValueError(f"runtime {kind} atlas metadata is missing")
            for sheet in clip["sheets"]:
                _runtime_asset(directory, sheet.get("image"))
            if clip.get("poster"):
                _runtime_asset(directory, clip["poster"])
            available.append(kind)
    if expect_motion is True and not available:
        raise ValueError("runtime motion metadata is missing")
    return manifest


def _recover_runtime_swap(slug):
    live = runtime_dir(slug)
    previous = live + ".previous"
    if not os.path.exists(live) and os.path.isdir(previous):
        _validate_runtime_bundle(previous)
        os.replace(previous, live)
    return live


def active_slug():
    try:
        return reg().get_active()
    except Exception:
        return None


RUNTIME_VERSION = 9  # bundles below this are rebaked on activation


def ensure_runtime(slug, log=print):
    """An avatar is only usable once it has a runtime bundle. Build one on demand
    rather than at activation time, so importing an old avatar folder works.
    Bundles from an older exporter are republished the same way, so asset
    upgrades (like the widened gaze grid) reach existing avatars without a
    manual rebuild."""
    d = _recover_runtime_swap(slug)
    manifest_path = os.path.join(d, "manifest.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as handle:
                version = int((json.load(handle) or {}).get("v") or 0)
        except (OSError, ValueError):
            version = 0
        if version >= RUNTIME_VERSION:
            return d
        log(f"runtime bundle is v{version}; republishing as v{RUNTIME_VERSION}")
        try:
            _publish_runtime_atomic(slug, log=log)
            return d
        except Exception as error:
            log(f"runtime refresh failed, keeping v{version}: {error}")
            return d
    from studio import export
    log("publishing runtime bundle")
    export.export(slug, d, log=log)
    return d


def jlog(slug, phase=None):
    with _jlock:
        j = _jobs.setdefault(slug, {"log": [], "phase": "", "done": False, "error": ""})
        if phase:
            j["phase"] = phase

    def w(msg):
        line = str(msg).rstrip()
        if not line:
            return
        with _jlock:
            j["log"].append(line)
            del j["log"][:-400]
            j["phase"] = line[:120]
        print(f"[avatar:{slug}] {line}", flush=True)
    return w


def _job_progress(slug, stage, value, label, job_id=None):
    payload = {
        "stage": str(stage),
        "value": max(0.0, min(1.0, float(value))),
        "label": str(label),
    }
    with _jlock:
        job = _jobs.get(slug)
        if job_id and (not job or job.get("id") != job_id):
            return
        if not job:
            job = _jobs.setdefault(
                slug, {"log": [], "phase": "", "done": False, "error": ""})
        job["progress"] = payload
        job["phase"] = payload["label"]


def _run_avatar_worker(args, log):
    process = subprocess.Popen(
        [sys.executable, "-W", "ignore", *args],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for line in process.stdout:
        log(line.rstrip())
    code = process.wait()
    if code:
        raise RuntimeError(f"avatar worker exited with status {code}")


def _build_thread(slug, shapes=None):
    w = jlog(slug, "starting")
    with _jlock:
        _jobs[slug].update(done=False, error="", log=[])
    try:
        build_args = ["-m", "studio.build", "build", slug]
        if shapes:
            build_args.extend(["--shapes", *shapes])
        _run_avatar_worker(build_args, w)
        d = runtime_dir(slug)
        if os.path.isdir(d):
            shutil.rmtree(d)
        w("publishing runtime bundle")
        _run_avatar_worker(["-m", "studio.export", slug, "--dest", d], w)
        w("ready")
    except Exception as e:
        with _jlock:
            _jobs[slug]["error"] = str(e)
        w(f"FAILED: {e}")
    finally:
        with _jlock:
            _jobs[slug]["done"] = True


def _recompose_thread(slug, profile):
    w = jlog(slug, "calibrating")
    with _jlock:
        _jobs[slug].update(
            done=False, error="", log=[], kind="calibration",
            progress={"stage": "starting", "value": .02,
                      "label": "Starting calibration"})
    try:
        reg().recompose_avatar(
            slug, profile, log=w,
            progress=lambda stage, value, label:
                _job_progress(slug, stage, value, label))
        w("ready")
    except Exception as e:
        with _jlock:
            _jobs[slug]["error"] = str(e)
        w(f"FAILED: {e}")
    finally:
        with _jlock:
            _jobs[slug]["done"] = True


def _publish_runtime_atomic(slug, log=print):
    from studio import export
    directory = reg().adir(slug)
    _recover_runtime_swap(slug)
    staged = tempfile.mkdtemp(prefix=".runtime-stage-", dir=directory)
    live = runtime_dir(slug)
    previous = live + ".previous"
    try:
        blink_source = os.path.join(directory, "visemes", "v_blink.jpg")
        if os.path.isfile(blink_source):
            export.export(slug, staged, log=log)
        elif os.path.isfile(os.path.join(live, "manifest.json")):
            shutil.copytree(live, staged, dirs_exist_ok=True)
            export.publish_pet_assets(slug, staged, log=log)
        else:
            raise ValueError("avatar has neither source visemes nor a published runtime")
        expect_motion = os.path.isfile(
            os.path.join(directory, "motion", "motion.json"))
        _validate_runtime_bundle(staged, expect_motion=expect_motion)
        shutil.rmtree(previous, ignore_errors=True)
        if os.path.exists(live):
            os.replace(live, previous)
        try:
            os.replace(staged, live)
            staged = None
            _validate_runtime_bundle(live, expect_motion=expect_motion)
        except Exception:
            shutil.rmtree(live, ignore_errors=True)
            if os.path.exists(previous):
                os.replace(previous, live)
            raise
        shutil.rmtree(previous, ignore_errors=True)
    except Exception:
        if not os.path.exists(live) and os.path.exists(previous):
            os.replace(previous, live)
        raise
    finally:
        if staged and os.path.exists(staged):
            shutil.rmtree(staged, ignore_errors=True)


def _body_thread(slug, options):
    w = jlog(slug, "starting full-body generation")
    with _jlock:
        _jobs[slug].update(
            done=False, error="", log=[], kind="body",
            progress={"stage": "provider", "value": .03,
                      "label": "Reading EnConvo image provider"})
    try:
        from studio import body, library, motion
        _job_progress(slug, "generation", .12, "Generating front, side, and back bodies")
        metadata = body.build(
            reg().adir(slug), options, log=w,
            progress=lambda stage, value, label:
                _job_progress(slug, stage, value, label))
        motion.remove(reg().adir(slug))
        for slot in ("walk", "idle"):
            library.clear_active(reg().adir(slug), slot)
        manifest = reg().read_manifest(slug) or {}
        manifest["body"] = metadata
        manifest.pop("motion", None)
        reg().write_manifest(slug, manifest)
        try:
            library.archive_body(reg().adir(slug))
        except Exception as archive_error:
            w(f"could not archive the body set: {archive_error}")
        _job_progress(slug, "runtime", .86, "Publishing transparent companion")
        _publish_runtime_atomic(slug, log=w)
        _job_progress(slug, "done", 1.0, "Three full-body views ready")
        w("front, side, and back full-body companion plates ready")
    except Exception as error:
        with _jlock:
            _jobs[slug]["error"] = str(error)
        w(f"FAILED: {error}")
    finally:
        with _jlock:
            _jobs[slug]["done"] = True


def _motion_thread(
        slug, reference_path, job_id, idle_pose=None,
        kinds=None, walk_style=None):
    writer = jlog(slug, "starting desktop motion generation")
    with _jlock:
        job = _jobs.get(slug)
        if not job or job.get("id") != job_id:
            if reference_path:
                try:
                    os.remove(reference_path)
                except FileNotFoundError:
                    pass
            return
        job.update(
            done=False, error="", log=[], kind="motion",
            progress={"stage": "provider", "value": .03,
                      "label": "Reading EnConvo media providers"})
    previous_manifest = reg().read_manifest(slug) or {}
    motion_replaced = False
    runtime_published = False
    failure = ""
    try:
        from studio import motion
        metadata = motion.build(
            reg().adir(slug),
            pose_reference=reference_path,
            idle_pose=idle_pose,
            kinds=kinds,
            walk_style=walk_style,
            log=writer,
            progress=lambda stage, value, label: _job_progress(
                slug, stage, value, label, job_id=job_id),
            keep_previous=True,
        )
        motion_replaced = True
        manifest = dict(previous_manifest)
        manifest["motion"] = metadata
        reg().write_manifest(slug, manifest)
        _job_progress(
            slug, "runtime", .94, "Publishing alpha motion", job_id=job_id)
        _publish_runtime_atomic(slug, log=writer)
        runtime_published = True
        motion.commit_pending_build(reg().adir(slug))
        try:
            from studio import library
            for archived_kind in tuple(kinds or ("walk", "idle")):
                library.archive_motion(reg().adir(slug), archived_kind)
        except Exception as archive_error:
            writer(f"could not archive the motion set: {archive_error}")
        selected = tuple(kinds or ("walk", "idle"))
        label = (
            "Horizon Walk and Edge Idle"
            if len(selected) == 2 else
            "Horizon Walk" if selected == ("walk",) else "Edge Idle"
        )
        _job_progress(
            slug, "done", 1.0, f"{label} ready", job_id=job_id)
        writer(f"{label} and standing interaction are ready")
    except Exception as error:
        failure = str(error)
        rollback_errors = []
        if motion_replaced and not runtime_published:
            try:
                motion.rollback_pending_build(reg().adir(slug))
            except Exception as rollback_error:
                rollback_errors.append(f"motion rollback: {rollback_error}")
            try:
                reg().write_manifest(slug, previous_manifest)
            except Exception as rollback_error:
                rollback_errors.append(f"manifest rollback: {rollback_error}")
        if rollback_errors:
            failure = f"{failure}; {'; '.join(rollback_errors)}"
        writer(f"FAILED: {failure}")
    finally:
        if reference_path:
            try:
                os.remove(reference_path)
            except FileNotFoundError:
                pass
        _finish_job(slug, job_id, failure)


@app.get("/api/avatars")
async def api_avatars():
    r = reg()
    out = []
    for a in r.list_avatars():
        s = a.get("slug")
        a["has_runtime"] = os.path.exists(os.path.join(runtime_dir(s), "manifest.json"))
        with _jlock:
            j = _jobs.get(s)
        a["job"] = {
            "phase": j["phase"], "done": j["done"],
            "error": j["error"], "kind": j.get("kind", "build"),
            "progress": j.get("progress")
        } if j else None
        out.append(a)
    return {"avatars": out, "active": r.get_active()}


@app.post("/api/avatar/upload")
async def api_upload(photo: UploadFile = File(...), name: str = Form("", max_length=120)):
    ext = os.path.splitext(photo.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".heic", ".bmp", ".tif", ".tiff"):
        raise HTTPException(400, f"unsupported image type {ext}")
    raw = await photo.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "portrait exceeds the 20 MB upload limit")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as handle:
        handle.write(raw)
        tmp = handle.name
    try:
        m = reg().create_avatar(tmp, name or os.path.splitext(photo.filename or "Avatar")[0])
    except Exception as e:
        raise HTTPException(400, str(e))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return m


class Slug(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    shapes: list[str] | None = None


def _rig_control_field(name):
    spec = rig.CONTROLS[name]
    return Field(default=spec["default"], ge=spec["minimum"], le=spec["maximum"])


class RigProfileInput(BaseModel):
    lips: float = _rig_control_field("lips")
    jaw: float = _rig_control_field("jaw")
    cheeks: float = _rig_control_field("cheeks")
    nasolabial: float = _rig_control_field("nasolabial")
    nose: float = _rig_control_field("nose")
    teeth_lock: bool = True
    upper_teeth_lock: bool = True
    lower_teeth_lock: bool = True
    preset: str = Field(default="custom", pattern=r"^(natural|subtle|expressive|custom)$")


class RigRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    profile: RigProfileInput


class BodyProfileInput(BaseModel):
    style: str = Field(default="photorealistic", pattern=r"^(photorealistic|editorial|illustrated|anime|soft-3d)$")
    pose: str = Field(default="relaxed", pattern=r"^(relaxed|confident|friendly|formal|casual)$")
    prompt: str = Field(default="", max_length=2400)
    outfit: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=600)


class BodyRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    profile: BodyProfileInput


class BodyPromptRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    refresh: bool = False


class MotionRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: str = Field(default="both", pattern=r"^(walk|idle|both)$")
    walk_style: str = Field(default="office", max_length=40)
    pose: str = Field(default="back-heel", max_length=40)
    pose_prompt: str = Field(default="", max_length=600)


class MotionRemoveRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: str = Field(default="both", pattern=r"^(walk|idle|both)$")


SET_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,80}$"


class MotionSetRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: str = Field(pattern=r"^(walk|idle)$")
    set_id: str = Field(pattern=SET_ID_PATTERN)


class BodySetRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    set_id: str = Field(pattern=SET_ID_PATTERN)


def _motion_asset_catalog(slug, directory, motion_metadata):
    motion_root = os.path.join(directory, "motion")
    catalog = {"walk": [], "idle": [], "shared": []}
    seen = set()

    def add(kind, relative, role, stage, label, order, extra=None):
        relative = str(relative or "").replace("\\", "/").lstrip("/")
        if not relative or relative in seen:
            return
        full = _safe_file(motion_root, relative)
        if not full:
            return
        extension = os.path.splitext(relative)[1].lower()
        media_type = (
            "video" if extension in {".mp4", ".mov", ".webm", ".m4v"} else
            "image" if extension in {".png", ".jpg", ".jpeg", ".webp"} else
            "json" if extension == ".json" else "file"
        )
        stat = os.stat(full)
        record = {
            "kind": kind,
            "role": role,
            "stage": stage,
            "label": label,
            "order": order,
            "name": os.path.basename(relative),
            "relative_path": relative,
            "media_type": media_type,
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
        }
        if extra:
            record.update({key: value for key, value in extra.items()
                           if value is not None})
        catalog[kind].append(record)
        seen.add(relative)

    for kind in ("walk", "idle"):
        clip = motion_metadata.get(kind) or {}
        if not clip:
            continue
        title = "Horizon Walk" if kind == "walk" else "Edge Idle"
        add(
            kind, f"raw/{kind}-keyframe.png", "keyframe", "01 · Keyframe",
            f"{title} generated keyframe", 10)
        add(
            kind, f"raw/{kind}-source.mp4", "raw-video", "02 · Raw I2V",
            "Raw xAI image-to-video", 20)
        for sheet_index, sheet in enumerate(clip.get("sheets") or []):
            name = os.path.basename(str(sheet.get("image") or ""))
            add(
                kind, name, "alpha-frames", "03 · Alpha frames",
                f"Transparent frame atlas {sheet_index + 1}", 30 + sheet_index,
                {
                    "frame_first": sheet.get("first", sheet.get("start", 0)),
                    "frame_count": sheet.get("count", sheet.get("frames")),
                    "columns": sheet.get("columns"),
                    "rows": sheet.get("rows"),
                    "frame_width": clip.get("frame_width"),
                    "frame_height": clip.get("frame_height"),
                    "fps": clip.get("fps"),
                })
        add(
            kind, os.path.basename(str(clip.get("poster") or f"{kind}-poster.png")),
            "poster", "04 · Loop poster", "Transparent loop poster", 70)
        add(
            kind,
            os.path.basename(str(clip.get("alpha_video") or f"{kind}-alpha.mov")),
            "alpha-video", "05 · Alpha video",
            "Final transparent animation", 80,
            {"frame_count": clip.get("frames"), "fps": clip.get("fps")})
        catalog[kind].sort(key=lambda asset: (asset["order"], asset["name"]))

    add(
        "shared", "motion.json", "receipt", "06 · Production receipt",
        "Motion metadata and quality receipt", 90)
    return catalog


@app.get("/api/avatar/rig")
async def api_rig(slug: str = Query(pattern=SLUG_PATTERN)):
    registry = reg()
    manifest = registry.read_manifest(slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    if manifest.get("status") != "ready":
        raise HTTPException(400, "build this avatar before calibrating it")
    from studio import compose, face, rig
    import cv2
    directory = registry.adir(slug)
    keyframe = cv2.imread(os.path.join(directory, "keyframe.png"))
    if keyframe is None:
        raise HTTPException(400, "avatar keyframe is missing")
    landmarks, _ = face.detect(keyframe)
    if landmarks is None:
        raise HTTPException(400, "no face detected in avatar keyframe")
    profile = rig.from_manifest(manifest)
    masks, face_mask = compose._masks(keyframe, landmarks, profile)
    alpha, _ = compose._alpha_ring(
        masks["mouth"], face_mask,
        max(keyframe.shape[:2]) / 1024.0, profile)
    payload = rig.inspector_payload(landmarks, keyframe.shape)
    payload["weights"] = rig.sampled_weights(alpha, landmarks)
    payload["profile"] = profile
    payload["schema"] = rig.public_schema()
    gaps = registry.raw_render_gaps(slug)
    payload["raw_gaps"] = gaps
    payload["can_recompose"] = not gaps
    payload["uses_generation"] = False
    payload["preview_visemes"] = [
        name for name in ("closed", "ah", "eh", "oo")
        if os.path.isfile(os.path.join(
            directory, "visemes", f"v_{name}.jpg"))
    ]
    selected = compose._select_dental_donors(
        os.path.join(directory, "visemes"))
    dental = dict(donor=None, donors={}, rows={}, contours=[])
    for row in compose.DENTAL_ROWS:
        if row not in selected:
            continue
        donor_name, _, _, master = selected[row]
        height, width = master.shape
        contours, _ = cv2.findContours(
            master, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        normalized = [[
            [round(float(point[0][0] / width), 6),
             round(float(point[0][1] / height), 6)]
            for point in contour]
            for contour in contours if len(contour) >= 3]
        dental["donors"][row] = donor_name
        dental["rows"][row] = dict(donor=donor_name, contours=normalized)
        dental["contours"].extend(normalized)
    dental["donor"] = dental["donors"].get("upper")
    payload["dental"] = dental
    return payload


@app.post("/api/avatar/recompose")
async def api_recompose(request: RigRequest):
    registry = reg()
    manifest = registry.read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    if manifest.get("status") != "ready":
        raise HTTPException(400, "avatar is not ready for calibration")
    profile_data = (request.profile.model_dump()
                    if hasattr(request.profile, "model_dump")
                    else request.profile.dict())
    from studio import rig
    try:
        profile = rig.normalize(profile_data)
    except ValueError as error:
        raise HTTPException(422, str(error))
    gaps = registry.raw_render_gaps(request.slug)
    if gaps:
        raise HTTPException(
            400, "retained expression renders are incomplete: " +
            ", ".join(gaps))
    with _jlock:
        job = _jobs.get(request.slug)
        if job and not job["done"]:
            return {"started": False, "reason": "already building"}
        _jobs[request.slug] = dict(
            phase="Queued", done=False, error="", log=[], kind="calibration",
            progress={"stage": "queued", "value": 0.0,
                      "label": "Queued"})
    threading.Thread(
        target=_recompose_thread,
        args=(request.slug, profile), daemon=True).start()
    return {"started": True, "slug": request.slug,
            "kind": "calibration", "uses_generation": False}


@app.post("/api/avatar/build")
async def api_build(b: Slug):
    if b.shapes:
        unknown = sorted(set(b.shapes) - set(reg().visemes.ORDER))
        if unknown:
            raise HTTPException(422, f"unknown viseme shapes: {', '.join(unknown)}")
    with _jlock:
        j = _jobs.get(b.slug)
        if j and not j["done"]:
            return {"started": False, "reason": "already building"}
    threading.Thread(target=_build_thread, args=(b.slug, b.shapes), daemon=True).start()
    return {"started": True, "slug": b.slug}


@app.get("/api/avatar/body")
async def api_body(slug: str = Query(pattern=SLUG_PATTERN)):
    manifest = reg().read_manifest(slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    try:
        from studio import body
        provider = body.default_provider()
        provider_error = None
    except Exception as error:
        provider = None
        provider_error = str(error)
    try:
        from studio import body
        video_provider = body.default_video_provider()
        video_provider_error = None
    except Exception as error:
        video_provider = None
        video_provider_error = str(error)
    directory = reg().adir(slug)
    body_metadata = manifest.get("body") or {}
    motion_metadata = manifest.get("motion") or {}
    body_views = body_metadata.get("views") or {}
    has_turnaround = all(
        isinstance(body_views.get(view), dict) and
        os.path.isfile(os.path.join(
            directory, "body",
            os.path.basename(str(body_views[view].get("image") or ""))))
        for view in ("front", "side", "back")
    )
    def has_motion_clip(kind):
        clip = motion_metadata.get(kind) or {}
        sheets = clip.get("sheets") or []
        return bool(sheets) and all(
            os.path.isfile(os.path.join(
                directory, "motion", os.path.basename(str(sheet.get("image") or ""))))
            for sheet in sheets
        )

    has_walk = has_motion_clip("walk")
    has_idle = has_motion_clip("idle")
    from studio import library
    try:
        # Adopt pre-library avatars: their canonical body and motion become
        # the first archived set. Content digests keep this idempotent.
        library.sync_canonical(directory)
    except Exception as sync_error:
        print(f"[avatar:{slug}] library sync failed: {sync_error}", flush=True)
    with _jlock:
        job = _jobs.get(slug)
        job = dict(job) if job and (
            job.get("kind") == "body" or
            str(job.get("kind") or "").startswith("motion")) else None
    from studio import wardrobe
    cached_prompt = wardrobe.cached_prompt(directory)
    return {
        "body": manifest.get("body"),
        "motion": manifest.get("motion"),
        "motion_assets": _motion_asset_catalog(
            slug, directory, motion_metadata),
        "motion_sets": {
            kind: library.list_motion_sets(directory, kind)
            for kind in ("walk", "idle")
        },
        "body_sets": library.list_body_sets(directory),
        "has_body": os.path.isfile(os.path.join(directory, "body", "body.json")),
        "has_turnaround": has_turnaround,
        "has_motion": has_walk or has_idle,
        "has_walk": has_walk,
        "has_idle": has_idle,
        "provider": provider,
        "provider_error": provider_error,
        "video_provider": video_provider,
        "video_provider_error": video_provider_error,
        "default_prompt": (cached_prompt or {}).get(
            "prompt") or wardrobe.preset_prompt(),
        "prompt_source": (cached_prompt or {}).get("source") or "preset",
        "prompt_traits": (cached_prompt or {}).get("traits") or {},
        "job": job,
    }


@app.post("/api/avatar/body/prompt")
async def api_body_prompt(request: BodyPromptRequest):
    """Compose art direction from the uploaded portrait itself.

    Kept off the status route because it calls a vision model: the modal opens
    on the cached or preset text immediately and upgrades in place.
    """
    if not reg().read_manifest(request.slug):
        raise HTTPException(404, "avatar not found")
    from studio import wardrobe
    directory = reg().adir(request.slug)
    result = await asyncio.to_thread(
        wardrobe.tailored_prompt, directory, request.refresh)
    return {
        "prompt": result.get("prompt") or wardrobe.preset_prompt(),
        "source": result.get("source") or "preset",
        "traits": result.get("traits") or {},
        "error": result.get("error") or "",
    }


@app.get("/api/media/defaults")
async def api_media_defaults():
    from studio import body

    result = {}
    for kind, resolver in (
        ("image", body.default_provider),
        ("video", body.default_video_provider),
    ):
        try:
            result[kind] = {"available": True, "provider": resolver(), "error": ""}
        except Exception as error:
            result[kind] = {"available": False, "provider": None, "error": str(error)}
    return result


@app.post("/api/avatar/body/generate")
async def api_body_generate(request: BodyRequest):
    manifest = reg().read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    if manifest.get("status") != "ready":
        raise HTTPException(400, "build this avatar before generating a body")
    job_id = _reserve_job(request.slug, "body")
    if not job_id:
        return _already_running(request.slug)
    profile = (request.profile.model_dump()
               if hasattr(request.profile, "model_dump")
               else request.profile.dict())
    try:
        threading.Thread(
            target=_body_thread,
            args=(request.slug, profile), daemon=True).start()
    except Exception as error:
        _finish_job(request.slug, job_id, error)
        raise
    return {
        "started": True, "slug": request.slug, "kind": "body",
        "job_id": job_id}


@app.post("/api/avatar/motion/generate")
async def api_motion_generate(request: MotionRequest):
    slug = request.slug
    manifest = reg().read_manifest(slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    directory = reg().adir(slug)
    if not os.path.isfile(os.path.join(directory, "body", "body.json")):
        raise HTTPException(400, "generate a full body before creating motion")
    from studio import motion
    kinds = (
        ("walk", "idle") if request.kind == "both" else (request.kind,)
    )
    try:
        walk_style = (
            motion.resolve_walk_style(request.walk_style)
            if "walk" in kinds else None
        )
        idle_pose = (
            motion.resolve_idle_pose(request.pose, request.pose_prompt)
            if "idle" in kinds else None
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    label = (
        "Validating Horizon Walk and Edge Idle"
        if len(kinds) == 2 else
        "Validating Horizon Walk style" if kinds == ("walk",) else
        "Validating Edge Idle pose"
    )
    job_id = _reserve_job(slug, "motion", label)
    if not job_id:
        return _already_running(slug)
    try:
        threading.Thread(
            target=_motion_thread,
            args=(slug, None, job_id, idle_pose, kinds, walk_style),
            daemon=True).start()
    except BaseException as error:
        _finish_job(slug, job_id, getattr(error, "detail", error))
        raise
    return {
        "started": True, "slug": slug, "kind": request.kind,
        "job_id": job_id,
        "pose": idle_pose["id"] if idle_pose else None,
        "walk_style": walk_style["id"] if walk_style else None,
    }


@app.post("/api/avatar/motion/remove")
async def api_motion_remove(request: MotionRemoveRequest):
    manifest = reg().read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    kind = getattr(request, "kind", "both")
    label = (
        "Removing Horizon Walk" if kind == "walk" else
        "Removing Edge Idle" if kind == "idle" else
        "Removing desktop motion"
    )
    job_id = _reserve_job(request.slug, "motion-remove", label)
    if not job_id:
        raise HTTPException(409, "avatar generation is still running")
    failure = ""
    try:
        from studio import library, motion
        metadata = motion.remove(reg().adir(request.slug), kind)
        for slot in ("walk", "idle") if kind == "both" else (kind,):
            library.clear_active(reg().adir(request.slug), slot)
        if metadata:
            manifest["motion"] = metadata
        else:
            manifest.pop("motion", None)
        reg().write_manifest(request.slug, manifest)
        _publish_runtime_atomic(
            request.slug, log=jlog(request.slug, label.lower()))
        return {"removed": True, "slug": request.slug, "kind": kind}
    except Exception as error:
        failure = str(error)
        raise
    finally:
        _finish_job(request.slug, job_id, failure)


def _apply_motion_metadata(manifest, metadata):
    if metadata:
        manifest["motion"] = metadata
    else:
        manifest.pop("motion", None)


@app.post("/api/avatar/motion/set/activate")
async def api_motion_set_activate(request: MotionSetRequest):
    manifest = reg().read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    title = "Horizon Walk" if request.kind == "walk" else "Edge Idle"
    job_id = _reserve_job(
        request.slug, "motion-set", f"Switching {title} set")
    if not job_id:
        raise HTTPException(409, "avatar generation is still running")
    failure = ""
    try:
        from studio import library
        directory = reg().adir(request.slug)
        sets = {record["id"]: record
                for record in library.list_motion_sets(directory, request.kind)}
        record = sets.get(request.set_id)
        if not record:
            raise HTTPException(404, f"unknown {request.kind} set")
        if not record["compatible"]:
            raise HTTPException(
                409, f"this {title} set was generated for a different body set")
        metadata = library.activate_motion(
            directory, request.kind, request.set_id)
        _apply_motion_metadata(manifest, metadata)
        reg().write_manifest(request.slug, manifest)
        _publish_runtime_atomic(
            request.slug, log=jlog(request.slug, f"switching {title.lower()}"))
        return {"activated": True, "slug": request.slug,
                "kind": request.kind, "set_id": request.set_id}
    except Exception as error:
        failure = getattr(error, "detail", None) or str(error)
        raise
    finally:
        _finish_job(request.slug, job_id, failure)


@app.post("/api/avatar/motion/set/remove")
async def api_motion_set_remove(request: MotionSetRequest):
    manifest = reg().read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    title = "Horizon Walk" if request.kind == "walk" else "Edge Idle"
    job_id = _reserve_job(
        request.slug, "motion-set", f"Deleting a {title} set")
    if not job_id:
        raise HTTPException(409, "avatar generation is still running")
    failure = ""
    try:
        from studio import library
        directory = reg().adir(request.slug)
        try:
            was_active = library.remove_motion_set(
                directory, request.kind, request.set_id)
        except ValueError as error:
            raise HTTPException(404, str(error))
        if was_active:
            metadata = library.strip_canonical_motion(directory, request.kind)
            fallback = library.newest_compatible_motion_set(
                directory, request.kind)
            if fallback:
                metadata = library.activate_motion(
                    directory, request.kind, fallback)
            _apply_motion_metadata(manifest, metadata)
            reg().write_manifest(request.slug, manifest)
            _publish_runtime_atomic(
                request.slug,
                log=jlog(request.slug, f"deleting a {title.lower()} set"))
        return {"removed": True, "slug": request.slug, "kind": request.kind,
                "set_id": request.set_id, "was_active": was_active}
    except Exception as error:
        failure = getattr(error, "detail", None) or str(error)
        raise
    finally:
        _finish_job(request.slug, job_id, failure)


@app.post("/api/avatar/body/set/activate")
async def api_body_set_activate(request: BodySetRequest):
    manifest = reg().read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    job_id = _reserve_job(request.slug, "body-set", "Switching body set")
    if not job_id:
        raise HTTPException(409, "avatar generation is still running")
    failure = ""
    try:
        from studio import library
        directory = reg().adir(request.slug)
        try:
            manifest["body"] = library.activate_body(directory, request.set_id)
        except ValueError as error:
            raise HTTPException(404, str(error))
        _apply_motion_metadata(
            manifest, library.reconcile_motion_with_body(directory))
        reg().write_manifest(request.slug, manifest)
        _publish_runtime_atomic(
            request.slug, log=jlog(request.slug, "switching body set"))
        return {"activated": True, "slug": request.slug,
                "set_id": request.set_id}
    except Exception as error:
        failure = getattr(error, "detail", None) or str(error)
        raise
    finally:
        _finish_job(request.slug, job_id, failure)


@app.post("/api/avatar/body/set/remove")
async def api_body_set_remove(request: BodySetRequest):
    manifest = reg().read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    job_id = _reserve_job(request.slug, "body-set", "Deleting a body set")
    if not job_id:
        raise HTTPException(409, "avatar generation is still running")
    failure = ""
    try:
        from studio import body, library, motion
        directory = reg().adir(request.slug)
        try:
            was_active = library.remove_body_set(directory, request.set_id)
        except ValueError as error:
            raise HTTPException(404, str(error))
        if was_active:
            fallback = library.newest_body_set(directory)
            if fallback:
                manifest["body"] = library.activate_body(directory, fallback)
                _apply_motion_metadata(
                    manifest, library.reconcile_motion_with_body(directory))
            else:
                body.remove(directory)
                motion.remove(directory)
                for slot in ("walk", "idle"):
                    library.clear_active(directory, slot)
                manifest.pop("body", None)
                manifest.pop("motion", None)
            reg().write_manifest(request.slug, manifest)
            _publish_runtime_atomic(
                request.slug, log=jlog(request.slug, "deleting a body set"))
        return {"removed": True, "slug": request.slug,
                "set_id": request.set_id, "was_active": was_active}
    except Exception as error:
        failure = getattr(error, "detail", None) or str(error)
        raise
    finally:
        _finish_job(request.slug, job_id, failure)


@app.post("/api/avatar/body/remove")
async def api_body_remove(request: Slug):
    manifest = reg().read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    job_id = _reserve_job(request.slug, "body-remove", "Removing full body")
    if not job_id:
        raise HTTPException(409, "avatar generation is still running")
    failure = ""
    try:
        from studio import body, library, motion
        body.remove(reg().adir(request.slug))
        motion.remove(reg().adir(request.slug))
        for slot in ("walk", "idle", "body"):
            library.clear_active(reg().adir(request.slug), slot)
        manifest.pop("body", None)
        manifest.pop("motion", None)
        reg().write_manifest(request.slug, manifest)
        _publish_runtime_atomic(
            request.slug, log=jlog(request.slug, "publishing portrait mode"))
        return {"removed": True, "slug": request.slug}
    except Exception as error:
        failure = str(error)
        raise
    finally:
        _finish_job(request.slug, job_id, failure)


@app.get("/api/avatar/progress")
async def api_progress(slug: str = Query(pattern=SLUG_PATTERN)):
    m = reg().read_manifest(slug) or {}
    with _jlock:
        j = _jobs.get(slug) or {"log": [], "phase": "", "done": True, "error": ""}
        j = dict(j)
    return {"manifest": m, "job": j}


@app.post("/api/avatar/activate")
async def api_activate(b: Slug):
    r = reg()
    m = r.read_manifest(b.slug) or {}
    if m.get("status") != "ready":
        raise HTTPException(400, "build this avatar before activating it")
    try:
        ensure_runtime(b.slug, log=jlog(b.slug, "publishing"))
    except Exception as e:
        raise HTTPException(400, f"could not publish runtime: {e}")
    r.set_active(b.slug)
    return {"active": b.slug}


@app.post("/api/avatar/delete")
async def api_delete(b: Slug):
    r = reg()
    if r.get_active() == b.slug:
        raise HTTPException(400, "that avatar is active - activate another one first")
    r.delete_avatar(b.slug)
    return {"deleted": b.slug}


def _safe_file(root, relative):
    root = os.path.abspath(root)
    full = os.path.abspath(os.path.join(root, relative))
    try:
        inside = os.path.commonpath((root, full)) == root
    except ValueError:
        inside = False
    return full if inside and os.path.isfile(full) else None


@app.get("/files/{path:path}")
async def api_files(path: str):
    full = _safe_file(reg().AVATARS, path)
    if not full:
        raise HTTPException(404, "not found")
    return FileResponse(full, headers={"Cache-Control": "no-store"})


@app.get("/assets/{path:path}")
async def api_assets(path: str):
    """Resolved per request against the active avatar, so switching a face needs
    no file copy and no restart - just a page reload."""
    s = active_slug()
    if not s:
        raise HTTPException(404, "no active avatar")
    full = _safe_file(runtime_dir(s), path)
    if not full:
        raise HTTPException(404, "not found")
    return FileResponse(full, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- settings

@app.get("/api/meta")
async def api_meta():
    return {"app_id": APP_ID, "active": active_slug()}


@app.get("/api/config")
async def api_config():
    return {"config": P.redacted(P.load()), "catalog": P.catalog(),
            "globals": await P.global_defaults_async(refresh=True),
            "routes": {kind: P.last_route(kind) for kind in ("llm", "tts", "stt")},
            "active": active_slug()}


@app.post("/api/config")
async def api_config_set(body: dict):
    # An empty api_key from the UI means "unchanged", never "erase" - the browser
    # is never sent the stored key, so it cannot echo one back.
    cur = P.load()
    for k in ("llm", "tts", "stt"):
        blk = body.get(k)
        if not isinstance(blk, dict):
            continue
        blk.pop("has_key", None)
        provider_changed = bool(blk.get("provider")) and \
            blk.get("provider") != (cur.get(k) or {}).get("provider")
        requested_key = blk.get("api_key")
        if blk.get("provider") == "enconvo" or requested_key == "__clear__" or \
           (provider_changed and not requested_key):
            blk["api_key"] = ""
        elif not requested_key:
            blk.pop("api_key", None)
    new = P.save(body)
    if (new.get("stt") or {}).get("provider") != (cur.get("stt") or {}).get("provider") or \
       (new.get("tts") or {}).get("provider") != (cur.get("tts") or {}).get("provider"):
        _state["warm"] = False
        threading.Thread(target=_warm, daemon=True).start()
    return {"config": P.redacted(new),
            "globals": await P.global_defaults_async(refresh=True)}


def _with_key(kind, blk):
    """Reuse a stored key only for the same provider."""
    cur = P.load().get(kind) or {}
    incoming = blk or {}
    incoming_provider = incoming.get("provider") or cur.get("provider")
    same_provider = incoming_provider == cur.get("provider")
    out = dict(cur) if same_provider else {"provider": incoming_provider}
    out.update({k: v for k, v in incoming.items()
                if k != "has_key" and v not in (None, "")})
    if same_provider and not out.get("api_key"):
        out["api_key"] = cur.get("api_key", "")
    return out


@app.post("/api/models")
async def api_models(body: dict):
    kind = body.get("kind", "llm")
    if kind not in ("llm", "tts", "stt"):
        raise HTTPException(400, "unknown model kind")
    cfg = _with_key(kind, body.get("cfg"))
    provider_spec = P.spec(kind, cfg.get("provider")) or {}
    if provider_spec.get("key") and not cfg.get("api_key"):
        return JSONResponse({"error": "Enter an API key before loading models.",
                             "models": [], "voices": [], "validated": False}, 200)
    try:
        choices = await P.list_choices(kind, cfg)
        return {**choices, "validated": True}
    except Exception as e:
        return JSONResponse({"error": P.safe_error(e), "models": [], "voices": [],
                             "validated": False}, 200)


@app.post("/api/test")
async def api_test(body: dict):
    kind = body.get("kind", "llm")
    return await P.test(kind, _with_key(kind, body.get("cfg")))


# ---------------------------------------------------------------- chat

def _warm():
    """Only load what the current settings will actually use. A user on cloud
    providers should not wait for Kokoro and Whisper to page in."""
    cfg = P.load()
    try:
        if (cfg["stt"]["provider"]) == "mlx_whisper":
            _state["warming"] = "whisper"
            import mlx_whisper, soundfile as sf
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                p = handle.name
            try:
                sf.write(p, np.zeros(16000, np.float32), 16000)
                mlx_whisper.transcribe(
                    p, path_or_hf_repo=cfg["stt"]["model"], language="en")
            finally:
                if os.path.exists(p):
                    os.unlink(p)
        tts_cfg = cfg["tts"]
        if tts_cfg["provider"] == "kokoro":
            _state["warming"] = "kokoro"
            voice = tts_cfg.get("voice") or "af_heart"
            speed = float(tts_cfg.get("speed") or 1.0)
            list(P._kokoro(tts_cfg)("Ready.", voice=voice, speed=speed))
        _state["warming"] = ""
        _state["warm"] = True
        print("[viv] warm", flush=True)
    except Exception as e:
        _state["warming"] = ""
        _state["warm"] = True          # cloud-only setups have nothing to warm
        print("[viv] warmup skipped:", P.safe_error(e), flush=True)


def _start():
    s = active_slug()
    if s:
        try:
            ensure_runtime(s)
        except Exception as e:
            print("[viv] runtime bundle missing:", e, flush=True)
    threading.Thread(target=_warm, daemon=True).start()


@app.get("/health")
async def health():
    cfg = P.load()
    mapped = await P.global_defaults_async()
    ok = False
    try:
        await P.list_models("llm", cfg["llm"])
        ok = True
    except Exception:
        pass

    def label(kind):
        block = cfg[kind]
        if block.get("provider") == "enconvo":
            return (mapped.get(kind) or {}).get("display") or "EnConvo global default"
        detail = block.get("voice") if kind == "tts" else block.get("model")
        return f"{block.get('provider')} / {detail or 'default'}"

    return {"app_id": APP_ID, "warm": _state["warm"], "warming": _state["warming"],
            "ollama": cfg["llm"].get("provider") == "ollama" and ok,
            "provider_ok": ok, "llm_ok": ok,
            "llm": label("llm"), "voice": label("tts"), "ears": label("stt"),
            "last_llm": P.last_route("llm"), "avatar": active_slug()}


@app.post("/stt")
async def stt(audio: UploadFile = File(...)):
    cfg = P.load()["stt"]
    raw = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(raw) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "recording exceeds the 25 MB upload limit")
    try:
        return {"text": await P.hear(raw, audio.filename or "a.webm", cfg)}
    except Exception as e:
        print("[viv] stt failed:", P.safe_error(e), flush=True)
        return {"text": "", "error": P.safe_error(e, 200)}


class Turn(BaseModel):
    history: list


@app.post("/reply")
async def reply(t: Turn):
    cfg = P.load()
    try:
        text = await P.chat(t.history[-12:], cfg["llm"], system=cfg["persona"]["system"])
    except Exception as e:
        print("[viv] llm failed:", P.safe_error(e), flush=True)
        text = "My model is not answering. Check the provider in Settings."
    if not text:
        text = "I lost that thread for a second. Say it again?"
    result = await _say(text, cfg)
    result["llm_route"] = P.last_route("llm")
    return result


class Say(BaseModel):
    text: str


@app.post("/say")
async def say(s: Say):
    return await _say(s.text, P.load())


async def _say(text, cfg):
    try:
        y, al = await P.speak(text, cfg["tts"])
        track, dur, tier = align.build(text, y, al)
        wav = P.to_wav(y)
    except Exception as e:
        print("[viv] tts failed:", P.safe_error(e), flush=True)
        return {"text": text, "audio": "", "track": [], "dur": 0.0,
                "tier": "none", "error": P.safe_error(e, 200)}
    return {"text": text, "audio": base64.b64encode(wav).decode(),
            "track": track, "dur": dur, "tier": tier}


# ---------------------------------------------------------------- pages

@app.get("/")
async def index():
    return HTMLResponse(open(os.path.join(WEB, "index.html")).read(),
                        headers={"Cache-Control": "no-store"})


@app.get("/bubble")
async def bubble():
    return HTMLResponse(open(os.path.join(WEB, "bubble.html")).read(),
                        headers={"Cache-Control": "no-store"})


@app.get("/appearance")
async def appearance():
    return HTMLResponse(open(os.path.join(WEB, "appearance.html")).read(),
                        headers={"Cache-Control": "no-store"})


@app.get("/settings")
async def settings():
    return HTMLResponse(open(os.path.join(WEB, "settings.html")).read(),
                        headers={"Cache-Control": "no-store"})
