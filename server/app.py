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
import datetime, re, zipfile, hashlib, urllib.parse
from contextlib import asynccontextmanager
from posixpath import normpath as posix_normpath
os.environ["PATH"] = os.pathsep.join(filter(None, (
    os.path.expanduser("~/.config/enconvo/bin"), "/opt/homebrew/bin",
    "/usr/local/bin", os.environ.get("PATH", ""))))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import numpy as np
from fastapi import (FastAPI, UploadFile, File, Form, HTTPException, Query,
                     Request, WebSocket, WebSocketDisconnect)
from fastapi.responses import (HTMLResponse, FileResponse, JSONResponse,
                               Response, StreamingResponse)
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
# Changes every engine start: the pocket page compares it and reloads
# itself, so a long-lived phone session can never run yesterday's code.
BOOT_ID = secrets.token_hex(4)
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024
SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,62})$"
# img/media allow https so chat cards can show pictures and play video the
# model links to; scripts stay same-origin only.
CSP = ("default-src 'self'; img-src 'self' data: blob: https:; "
       "media-src 'self' blob: https:; "
       "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
       # 'self' does not cover the ws: scheme, and live dictation streams
       # over a local WebSocket to this same server.
       "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
       "font-src 'self' data:; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _security_headers(response):
    # Pages are never cached: WKWebView happily served a stale renderer
    # across three debugging rounds (2026-08-02). Assets carry their own
    # cache-busting revs; the HTML must always be current.
    if str(response.headers.get("content-type", "")).startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
    return response


def _client_token(source):
    """The auth token from the Electron-injected header, the pairing
    cookie, or - for websockets only - the query string. iOS runs the
    renderer in a WKWebView, which cannot add a header to a socket; and
    once the page lives on its own origin the cookie stops riding along
    either, so a socket has nowhere else to say who it is."""
    supplied = source.headers.get("x-vivieen-token", "")
    if not supplied:
        try:
            supplied = source.cookies.get("vivieen-token", "") or ""
        except Exception:
            supplied = ""
    if not supplied:
        try:
            supplied = source.query_params.get("token", "") or ""
        except Exception:
            supplied = ""
    return supplied


@app.middleware("http")
async def security_headers(request, call_next):
    if AUTH_TOKEN:
        if not secrets.compare_digest(_client_token(request), AUTH_TOKEN):
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
        # A NEW attempt of the same kind is the only thing that retires the
        # last failure of that kind - not merely the next job of any kind,
        # or the automatic publish that follows a build would erase the
        # explanation before anyone read it.
        _failures.pop((slug, kind), None)
    _build_log_write(slug, f"=== {kind} started: {label}")
    return job_id


def _finish_job(slug, job_id, error=""):
    kind = ""
    with _jlock:
        job = _jobs.get(slug)
        if not job or job.get("id") != job_id:
            # The record was already replaced. The failure still happened,
            # and it is still the answer to "why did nothing arrive".
            if error:
                _failures[(slug, "")] = {
                    "kind": "", "error": str(error),
                    "when": datetime.datetime.now().isoformat(timespec="seconds"),
                }
            return
        kind = str(job.get("kind") or "")
        job["error"] = str(error or job.get("error") or "")
        job["done"] = True
        if job["error"]:
            _failures[(slug, kind)] = {
                "kind": kind, "error": job["error"],
                "when": datetime.datetime.now().isoformat(timespec="seconds"),
            }
    if error:
        _build_log_write(slug, f"=== {kind or 'job'} FAILED: {error}")
    else:
        _build_log_write(slug, f"=== {kind or 'job'} finished")


def _last_failure(slug):
    """The most recent failed build for this avatar, whatever ran since."""
    with _jlock:
        rows = [row for (key, _), row in _failures.items() if key == slug]
    if not rows:
        return None
    return sorted(rows, key=lambda row: row.get("when") or "")[-1]


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
        for kind in ("walk", "idle", "move"):
            clip = runtime_motion.get(kind)
            if not clip:
                continue
            if not isinstance(clip, dict) or not (
                    clip.get("sheets") or clip.get("alpha_stream")):
                raise ValueError(f"runtime {kind} clip assets are missing")
            if clip.get("alpha_stream"):
                _runtime_asset(directory, clip["alpha_stream"])
            if clip.get("alpha_stream_hevc"):
                _runtime_asset(directory, clip["alpha_stream_hevc"])
            for sheet in clip.get("sheets") or []:
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


RUNTIME_VERSION = 16  # v16: high-definition alpha twins (HEVC q85, VP9 crf18)


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


# Punctuation that opens or closes a machine payload - never a sentence
# worth putting on screen as the current phase.
_PHASE_NOISE = "{}[]()\"',^"


def _phase_headline(line):
    """The part of a worker line worth showing as the status, if any."""
    text = line.strip()
    if not text:
        return ""
    # The one JSON line that IS the message: "error": "...".
    hit = re.match(r'^"?(?:error|detail|message)"?\s*:\s*"?(.+?)"?,?$', text)
    if hit:
        return hit.group(1)
    if text[0] in _PHASE_NOISE:
        return ""
    return text


# The last failure per avatar, kept OUTSIDE _jobs so it survives the next
# job. _reserve_job replaces _jobs[slug] wholesale, so a failed build's
# error and its whole log were destroyed by whatever ran next - which is
# how a four-minute failure became "it never showed me any error message,
# it just didn't deliver" (owner, 2026-08-04).
_failures = {}


def _build_log_path(slug):
    try:
        return os.path.join(reg().adir(slug), "build.log")
    except Exception:
        return ""


def _build_log_write(slug, line):
    """One build line, on disk, timestamped. Memory forgets; this does not."""
    path = _build_log_path(slug)
    if not path:
        return
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp}  {line}\n")
        # Bounded: a build log is for the last few builds, not forever.
        if os.path.getsize(path) > 512_000:
            with open(path, encoding="utf-8") as handle:
                tail = handle.readlines()[-2000:]
            with open(path, "w", encoding="utf-8") as handle:
                handle.writelines(tail)
    except Exception:
        pass


def jlog(slug, phase=None):
    with _jlock:
        j = _jobs.setdefault(slug, {"log": [], "phase": "", "done": False, "error": ""})
        if phase:
            j["phase"] = phase
    if phase:
        _build_log_write(slug, f"--- {phase}")

    def w(msg):
        line = str(msg).rstrip()
        if not line:
            return
        with _jlock:
            j["log"].append(line)
            del j["log"][:-400]
            # A provider that fails prints a multi-line JSON blob, and the
            # LAST line of it is "})" - so the status read "})" while the
            # sentence that said what went wrong scrolled past unseen
            # (owner: is this normal, 2026-08-04). Show the headline.
            headline = _phase_headline(line)
            if headline:
                j["phase"] = headline[:120]
        print(f"[avatar:{slug}] {line}", flush=True)
        _build_log_write(slug, line)
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


def _build_thread(slug, shapes=None, notes=""):
    w = jlog(slug, "starting")
    with _jlock:
        _jobs[slug].update(done=False, error="", log=[])
    try:
        build_args = ["-m", "studio.build", "build", slug]
        if shapes:
            build_args.extend(["--shapes", *shapes])
        if notes:
            build_args.extend(["--keep", notes])
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


def _body_stage(slug, options, w, progress):
    """Generate the three body plates and publish them. Shared by the
    standalone body job and the one-click pipeline; raises on failure."""
    from studio import body, library, motion
    progress("generation", .12, "Generating front, side, and back bodies")
    metadata = body.build(
        reg().adir(slug), options, log=w, progress=progress)
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
    progress("runtime", .86, "Publishing transparent companion")
    _publish_runtime_atomic(slug, log=w)


def _body_thread(slug, options):
    w = jlog(slug, "starting full-body generation")
    with _jlock:
        _jobs[slug].update(
            done=False, error="", log=[], kind="body",
            progress={"stage": "provider", "value": .03,
                      "label": "Reading EnConvo image provider"})
    try:
        _body_stage(slug, options, w,
                    lambda stage, value, label:
                        _job_progress(slug, stage, value, label))
        _job_progress(slug, "done", 1.0, "Three full-body views ready")
        w("front, side, and back full-body companion plates ready")
    except Exception as error:
        with _jlock:
            _jobs[slug]["error"] = str(error)
        w(f"FAILED: {error}")
    finally:
        with _jlock:
            _jobs[slug]["done"] = True


def _motion_stage(slug, kinds, idle_pose, walk_style, move_style, writer,
                  progress, reference_path=None):
    """Generate motion takes and publish them, rolling back a half-replaced
    bank on failure. Shared by the standalone motion job and the one-click
    pipeline; raises on failure."""
    from studio import motion
    previous_manifest = reg().read_manifest(slug) or {}
    motion_replaced = False
    runtime_published = False
    try:
        metadata = motion.build(
            reg().adir(slug),
            pose_reference=reference_path,
            idle_pose=idle_pose,
            kinds=kinds,
            walk_style=walk_style,
            move_style=move_style,
            log=writer,
            progress=progress,
            keep_previous=True,
        )
        motion_replaced = True
        manifest = dict(previous_manifest)
        manifest["motion"] = metadata
        reg().write_manifest(slug, manifest)
        progress("runtime", .94, "Publishing alpha motion")
        _publish_runtime_atomic(slug, log=writer)
        runtime_published = True
        motion.commit_pending_build(reg().adir(slug))
        try:
            from studio import library
            for archived_kind in tuple(kinds or ("walk", "idle")):
                library.archive_motion(reg().adir(slug), archived_kind)
        except Exception as archive_error:
            writer(f"could not archive the motion set: {archive_error}")
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
        raise RuntimeError(failure) from error


def _motion_thread(
        slug, reference_path, job_id, idle_pose=None,
        kinds=None, walk_style=None, move_style=None):
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
    failure = ""
    try:
        _motion_stage(
            slug, kinds, idle_pose, walk_style, move_style, writer,
            lambda stage, value, label: _job_progress(
                slug, stage, value, label, job_id=job_id),
            reference_path=reference_path)
        selected = tuple(kinds or ("walk", "idle"))
        kind_labels = {"walk": "Horizon Walk", "idle": "Edge Idle",
                       "move": "Show Me Some Moves"}
        label = " and ".join(kind_labels[k] for k in selected)
        _job_progress(
            slug, "done", 1.0, f"{label} ready", job_id=job_id)
        writer(f"{label} and standing interaction are ready")
    except Exception as error:
        failure = str(error)
        writer(f"FAILED: {failure}")
    finally:
        if reference_path:
            try:
                os.remove(reference_path)
            except FileNotFoundError:
                pass
        _finish_job(slug, job_id, failure)


def _pipeline_thread(slug, job_id, notes=""):
    """One click, everything: talking face (if not built) -> full body ->
    walk, edge idle, and moves - sequentially, in one background job."""
    writer = jlog(slug, "one-click pipeline: face, full body, walk, idle, moves")
    with _jlock:
        job = _jobs.get(slug)
        if not job or job.get("id") != job_id:
            return
        job.update(
            done=False, error="", log=[], kind="pipeline",
            progress={"stage": "face", "value": .01,
                      "label": "One-click 1/3: talking face"})

    def band(base, span, prefix):
        def report(stage, value, label):
            fraction = max(0.0, min(1.0, float(value or 0.0)))
            _job_progress(slug, stage, base + fraction * span,
                          prefix + label, job_id=job_id)
        return report

    failure = ""
    try:
        from studio import motion
        manifest = reg().read_manifest(slug) or {}
        # A keep-note CHANGES the head prompt, so the face that is already
        # built was made without it - skipping the face would have meant
        # the one thing the owner asked to keep never came back. The head
        # cache keys on the prompt, so this re-renders rather than reusing
        # (owner: his bandana is gone, 2026-08-04).
        if manifest.get("status") != "ready" or notes:
            _job_progress(slug, "face", .02,
                          "One-click 1/3: building the talking face"
                          + (" with your notes" if notes else ""),
                          job_id=job_id)
            manifest = reg().build_avatar(slug, notes=notes) or {}
            if manifest.get("status") != "ready":
                raise RuntimeError(
                    manifest.get("error") or "the face build failed")
        _job_progress(slug, "face", .30,
                      "One-click 1/3: talking face ready", job_id=job_id)
        # Resumable: a re-click after a partial run picks up where it
        # stopped instead of regenerating finished stages.
        body_manifest_path = os.path.join(
            reg().adir(slug), "body", "body.json")
        if (reg().read_manifest(slug) or {}).get("body") \
                and os.path.isfile(body_manifest_path):
            writer("full body already built - skipping")
            _job_progress(slug, "body", .58,
                          "One-click 2/3: full body already built",
                          job_id=job_id)
        else:
            _body_stage(slug, BodyProfileInput(notes=notes).model_dump(), writer,
                        band(.30, .28, "One-click 2/3: "))
        # The takes run ONE AT A TIME with backoff retries: firing all
        # three at once burst past xAI's 2-requests-per-second team limit
        # ("Too many requests", eve 2026-08-01) and one provider hiccup
        # killed the whole stage. A kind that still fails after retries is
        # reported and the pipeline moves on to the next.
        kind_labels = {"walk": "Horizon Walk", "idle": "Edge Idle",
                       "move": "Show Me Some Moves"}
        idle_pose = motion.resolve_idle_pose(None, "")
        walk_style = motion.resolve_walk_style(None, "")
        move_style = motion.resolve_move_style(None, "")
        existing = set((reg().read_manifest(slug) or {}).get("motion") or {})
        bands = {"walk": (.58, .14), "idle": (.72, .13), "move": (.85, .13)}
        motion_failures = {}
        for kind in ("walk", "idle", "move"):
            base, span = bands[kind]
            if kind in existing:
                writer(f"{kind} take already built - skipping")
                _job_progress(slug, kind, base + span,
                              f"One-click 3/3: {kind_labels[kind]} already built",
                              job_id=job_id)
                continue
            for attempt in range(3):
                try:
                    _motion_stage(
                        slug, (kind,), idle_pose, walk_style, move_style,
                        writer,
                        band(base, span, f"One-click 3/3 {kind_labels[kind]}: "))
                    motion_failures.pop(kind, None)
                    break
                except Exception as take_error:
                    motion_failures[kind] = str(take_error)
                    if attempt < 2:
                        pause = 25 * (attempt + 1)
                        writer(f"{kind} take failed ({take_error}); "
                               f"retrying in {pause}s ({attempt + 1}/2)")
                        _job_progress(
                            slug, kind, base,
                            f"One-click 3/3 {kind_labels[kind]}: retrying "
                            f"in {pause}s", job_id=job_id)
                        time.sleep(pause)
        if len(motion_failures) == 3:
            raise RuntimeError(
                "all three motion takes failed - last error: "
                + motion_failures["move"])
        if motion_failures:
            failed = ", ".join(kind_labels[k] for k in motion_failures)
            done_label = (f"Ready with gaps - {failed} failed; regenerate "
                          f"from the Full Body Studio")
        else:
            done_label = "Everything ready - face, body, walk, idle, and moves"
        _job_progress(slug, "done", 1.0, done_label, job_id=job_id)
        writer("one-click pipeline complete"
               + (f" ({len(motion_failures)} take(s) failed)"
                  if motion_failures else ""))
    except Exception as error:
        failure = str(error)
        writer(f"FAILED: {failure}")
    finally:
        _finish_job(slug, job_id, failure)


@app.get("/api/pairing")
async def api_pairing(request: Request):
    """What the phone needs to find this Mac, on the page the owner
    actually opens.

    It was only ever in a right-click menu on the avatar - "iPhone on This
    Network", then "Pair iPhone..." - which is two moves nobody finds, and
    the owner had no idea how to pair (owner, 2026-08-04).

    Loopback only. The token is what the caller had to present to get this
    far, so echoing it back tells a stranger nothing - but the phone has
    no reason to ask, and a door that only opens where it is needed is one
    fewer door.
    """
    host = (request.client.host if request.client else "") or ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        return JSONResponse({"error": "desk only"}, status_code=403)
    found = []
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None):
            candidate = info[4][0]
            if ":" in candidate or candidate.startswith("127."):
                continue        # IPv6 and loopback are no use to a phone
            url = f"http://{candidate}:{os.environ.get('VIVIEEN_PORT', '8777')}"
            if url not in found:
                found.append(url)
    except Exception:
        pass
    # Bound to loopback only, the phone cannot reach this Mac at all, and
    # the addresses above would be a promise the engine cannot keep. The
    # bind is a uvicorn argument, not a setting we hold, so read it back
    # from the command line that started us.
    bind = "127.0.0.1"
    argv = sys.argv
    for index, word in enumerate(argv):
        if word == "--host" and index + 1 < len(argv):
            bind = argv[index + 1]
    reachable = bind not in {"127.0.0.1", "localhost", "::1"}
    return {"addresses": found, "code": AUTH_TOKEN, "reachable": reachable}


@app.get("/api/avatars")
async def api_avatars():
    r = reg()
    out = []
    for a in r.list_avatars():
        s = a.get("slug")
        a["has_runtime"] = os.path.exists(os.path.join(runtime_dir(s), "manifest.json"))
        # Her own character, if she has been given one. The card edits it.
        a["persona"] = ((reg().read_manifest(s) or {}).get("persona")
                        or {}).get("system", "")
        with _jlock:
            j = _jobs.get(s)
        a["job"] = {
            "phase": j["phase"], "done": j["done"],
            "error": j["error"], "kind": j.get("kind", "build"),
            "progress": j.get("progress")
        } if j else None
        out.append(a)
    return {"avatars": out, "active": r.get_active(),
            "companion": r.get_companion()}


@app.post("/api/avatar/upload")
async def api_upload(photo: UploadFile = File(...), name: str = Form("", max_length=120)):
    ext = os.path.splitext(photo.filename or "")[1].lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"):
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


class RenameRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    name: str = Field(min_length=1, max_length=120)


class PersonaRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    system: str = Field(default="", max_length=4000)


@app.post("/api/avatar/persona")
def api_avatar_persona(r: PersonaRequest):
    """Give one avatar a character of its own. Empty means "use the
    house persona", which is what every avatar did before this existed."""
    m = reg().read_manifest(r.slug)
    if not m:
        raise HTTPException(404, "unknown avatar")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", r.system).strip()
    if text:
        m["persona"] = {"system": text}
    else:
        m.pop("persona", None)
    reg().write_manifest(r.slug, m)
    return {"slug": r.slug, "system": text}


@app.post("/api/avatar/rename")
def api_avatar_rename(r: RenameRequest):
    name = re.sub(r"[\x00-\x1f\x7f]+", " ", r.name).strip()[:120]
    if not name:
        raise HTTPException(400, "name is empty")
    m = reg().read_manifest(r.slug)
    if not m:
        raise HTTPException(404, "unknown avatar")
    m["name"] = name
    return reg().write_manifest(r.slug, m)


class Slug(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    shapes: list[str] | None = None
    # What the owner asked to keep from the source portrait. Optional
    # everywhere; only the build paths read it.
    notes: str = Field(default="", max_length=600)


def _rig_control_field(name):
    spec = rig.CONTROLS[name]
    return Field(default=spec["default"], ge=spec["minimum"], le=spec["maximum"])


def _dental_donor_field(row):
    return Field(default="auto",
                 pattern="^(auto|" + "|".join(rig.DENTAL_DONORS[row]) + ")$")


class RigProfileInput(BaseModel):
    lips: float = _rig_control_field("lips")
    jaw: float = _rig_control_field("jaw")
    cheeks: float = _rig_control_field("cheeks")
    brows: float = _rig_control_field("brows")
    forehead: float = _rig_control_field("forehead")
    nasolabial: float = _rig_control_field("nasolabial")
    nose: float = _rig_control_field("nose")
    teeth: float = _rig_control_field("teeth")
    upper_teeth_donor: str = _dental_donor_field("upper")
    lower_teeth_donor: str = _dental_donor_field("lower")
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
    prompt: str = Field(default="", max_length=4000)
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
    kind: str = Field(default="both", pattern=r"^(walk|idle|move|both)$")
    walk_style: str = Field(default="office", max_length=40)
    walk_prompt: str = Field(default="", max_length=2400)
    pose: str = Field(default="back-heel", max_length=40)
    pose_prompt: str = Field(default="", max_length=2400)
    move_style: str = Field(default="viral", max_length=40)
    move_prompt: str = Field(default="", max_length=2400)


class MotionRemoveRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: str = Field(default="both", pattern=r"^(walk|idle|move|both)$")


SET_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,80}$"


class MotionSetRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: str = Field(pattern=r"^(walk|idle|move)$")
    set_id: str = Field(pattern=SET_ID_PATTERN)


class BodySetRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    set_id: str = Field(pattern=SET_ID_PATTERN)


def _motion_asset_catalog(slug, directory, motion_metadata):
    motion_root = os.path.join(directory, "motion")
    catalog = {"walk": [], "idle": [], "move": [], "shared": []}
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

    for kind in ("walk", "idle", "move"):
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
    # One enamel scan per row serves both the donor dropdown (every
    # candidate frame with its detected pixel count) and the election the
    # overlay draws - honoring the profile's saved donor overrides.
    viseme_dir = os.path.join(directory, "visemes")
    dental = dict(donor=None, donors={}, rows={}, contours=[],
                  candidates={}, overrides={})
    for row in compose.DENTAL_ROWS:
        candidates = compose._scan_tooth_donors(viseme_dir, row)
        dental["candidates"][row] = [
            dict(name=name, pixels=pixels)
            for name, _, _, _, pixels in candidates]
        choice = profile.get(f"{row}_teeth_donor", "auto")
        dental["overrides"][row] = choice
        selected = compose._elect_tooth_donor(candidates, row, choice)
        if selected is None:
            continue
        donor_name, _, _, master = selected
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
    threading.Thread(target=_build_thread,
                     args=(b.slug, b.shapes, b.notes), daemon=True).start()
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
    has_move = has_motion_clip("move")
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
            for kind in ("walk", "idle", "move")
        },
        "body_sets": library.list_body_sets(directory),
        "has_body": os.path.isfile(os.path.join(directory, "body", "body.json")),
        "has_turnaround": has_turnaround,
        "has_motion": has_walk or has_idle or has_move,
        "has_walk": has_walk,
        "has_idle": has_idle,
        "has_move": has_move,
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


class PromptExpandRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: str = Field(pattern=r"^(body|walk|idle|move)$")
    gist: str = Field(min_length=4, max_length=600)
    # The prompt already in the field. Given one, the expander REVISES it
    # rather than starting over.
    base: str = Field(default="", max_length=4000)


@app.post("/api/avatar/prompt/expand")
async def api_prompt_expand(request: PromptExpandRequest):
    """Expand a rough gist into a field-ready prompt via the selected LLM.

    Serves the AI-draft buttons on the full-body prompt and the custom walk
    and Edge Idle prompt fields; the portrait rides along so the direction
    suits the actual subject.
    """
    if not reg().read_manifest(request.slug):
        raise HTTPException(404, "avatar not found")
    from studio import promptsmith
    directory = reg().adir(request.slug)
    try:
        prompt = await asyncio.to_thread(
            promptsmith.expand, request.kind, request.gist, directory,
            request.base)
    except Exception as error:
        raise HTTPException(400, str(error))
    return {"prompt": prompt, "kind": request.kind}


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


def _recut_thread(slug, kind, job_id):
    writer = jlog(slug, f"re-cutting the retained {kind} take")
    previous_manifest = reg().read_manifest(slug) or {}
    motion_replaced = False
    runtime_published = False
    failure = ""
    try:
        from studio import motion, library
        metadata = motion.recut(
            reg().adir(slug), kind, log=writer,
            progress=lambda stage, value, label: _job_progress(
                slug, stage, value, label, job_id=job_id))
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
            library.archive_motion(reg().adir(slug), kind)
        except Exception as archive_error:
            writer(f"could not archive the re-cut set: {archive_error}")
        _job_progress(slug, "done", 1.0, f"{kind} re-cut ready", job_id=job_id)
        writer(f"{kind} re-cut is live")
    except Exception as error:
        failure = str(error)
        if motion_replaced and not runtime_published:
            try:
                from studio import motion
                motion.rollback_pending_build(reg().adir(slug))
                reg().write_manifest(slug, previous_manifest)
            except Exception as rollback_error:
                failure = f"{failure}; rollback: {rollback_error}"
        writer(f"FAILED: {failure}")
    finally:
        _finish_job(slug, job_id, failure)


def _repair_thread(slug, kind, frame, mode, note, job_id, frame_end=None):
    writer = jlog(slug, f"repairing {kind} frame {frame}")
    previous_manifest = reg().read_manifest(slug) or {}
    motion_replaced = False
    runtime_published = False
    failure = ""
    try:
        from studio import motion, library
        metadata = motion.repair_frame(
            reg().adir(slug), kind, frame, mode=mode, note=note, log=writer,
            frame_end=frame_end,
            progress=lambda stage, value, label: _job_progress(
                slug, stage, value, label, job_id=job_id))
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
            library.archive_motion(reg().adir(slug), kind)
        except Exception as archive_error:
            writer(f"could not archive the repaired set: {archive_error}")
        _job_progress(
            slug, "done", 1.0, f"{kind} frame {frame} repaired", job_id=job_id)
    except Exception as error:
        failure = str(error)
        if motion_replaced and not runtime_published:
            try:
                from studio import motion
                motion.rollback_pending_build(reg().adir(slug))
                reg().write_manifest(slug, previous_manifest)
            except Exception as rollback_error:
                failure = f"{failure}; rollback: {rollback_error}"
        writer(f"FAILED: {failure}")
    finally:
        _finish_job(slug, job_id, failure)


class MotionRepairRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: str = Field(pattern=r"^(walk|idle|move)$")
    frame: int = Field(ge=0, le=4096)
    frame_end: int | None = Field(default=None, ge=0, le=4096)
    mode: str = Field(default="patch", pattern=r"^(patch|drop)$")
    note: str = Field(default="", max_length=200)


class PipelineRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    # What the owner wants kept from the source portrait - a bandana, an
    # earring, a scar. Rides with the house prompt, never replaces it.
    notes: str = Field(default="", max_length=600)


@app.post("/api/avatar/pipeline")
async def api_pipeline(request: PipelineRequest):
    """One click, everything: build the face if needed, then the full body,
    then walk + edge idle + moves - one sequential background job."""
    manifest = reg().read_manifest(request.slug)
    if not manifest:
        raise HTTPException(404, "avatar not found")
    job_id = _reserve_job(request.slug, "pipeline",
                          "One-click: face, body, walk, idle, moves")
    if not job_id:
        return _already_running(request.slug)
    try:
        threading.Thread(
            target=_pipeline_thread,
            args=(request.slug, job_id, request.notes), daemon=True).start()
    except BaseException as error:
        _finish_job(request.slug, job_id, getattr(error, "detail", error))
        raise
    return {"started": True, "slug": request.slug, "kind": "pipeline",
            "job_id": job_id}


@app.post("/api/avatar/motion/repair")
async def api_motion_repair(request: MotionRepairRequest):
    """Fix ONE flagged frame: patch it from its loop neighbours, or drop
    it. Works on the packed lossless frames - no generation, no re-matte."""
    if not reg().read_manifest(request.slug):
        raise HTTPException(404, "avatar not found")
    job_id = _reserve_job(
        request.slug, "motion",
        f"Repairing {request.kind} frame {request.frame}")
    if not job_id:
        return _already_running(request.slug)
    try:
        threading.Thread(
            target=_repair_thread,
            args=(request.slug, request.kind, request.frame, request.mode,
                  request.note, job_id, request.frame_end),
            daemon=True).start()
    except BaseException as error:
        _finish_job(request.slug, job_id, getattr(error, "detail", error))
        raise
    return {"started": True, "slug": request.slug, "kind": request.kind,
            "frame": request.frame, "frame_end": request.frame_end,
            "mode": request.mode, "job_id": job_id}


class MotionRecutRequest(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    kind: str = Field(pattern=r"^(walk|idle|move)$")


@app.post("/api/avatar/motion/recut")
async def api_motion_recut(request: MotionRecutRequest):
    """Reprocess the retained raw take through the current local pipeline -
    no generation spend; how existing sets pick up matte upgrades."""
    slug = request.slug
    if not reg().read_manifest(slug):
        raise HTTPException(404, "avatar not found")
    raw = os.path.join(
        reg().adir(slug), "motion", "raw", f"{request.kind}-source.mp4")
    if not os.path.isfile(raw):
        raise HTTPException(400, "no retained raw take for this clip; generate first")
    job_id = _reserve_job(
        slug, "motion", f"Re-cutting {request.kind} from the retained take")
    if not job_id:
        return _already_running(slug)
    try:
        threading.Thread(
            target=_recut_thread, args=(slug, request.kind, job_id),
            daemon=True).start()
    except BaseException as error:
        _finish_job(slug, job_id, getattr(error, "detail", error))
        raise
    return {"started": True, "slug": slug, "kind": request.kind,
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
            motion.resolve_walk_style(request.walk_style, request.walk_prompt)
            if "walk" in kinds else None
        )
        idle_pose = (
            motion.resolve_idle_pose(request.pose, request.pose_prompt)
            if "idle" in kinds else None
        )
        move_style = (
            motion.resolve_move_style(request.move_style, request.move_prompt)
            if "move" in kinds else None
        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    kind_labels = {"walk": "Horizon Walk", "idle": "Edge Idle",
                   "move": "Show Me Some Moves"}
    label = "Validating " + " and ".join(kind_labels[k] for k in kinds)
    job_id = _reserve_job(slug, "motion", label)
    if not job_id:
        return _already_running(slug)
    try:
        threading.Thread(
            target=_motion_thread,
            args=(slug, None, job_id, idle_pose, kinds, walk_style, move_style),
            daemon=True).start()
    except BaseException as error:
        _finish_job(slug, job_id, getattr(error, "detail", error))
        raise
    return {
        "started": True, "slug": slug, "kind": request.kind,
        "job_id": job_id,
        "pose": idle_pose["id"] if idle_pose else None,
        "walk_style": walk_style["id"] if walk_style else None,
        "move_style": move_style["id"] if move_style else None,
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
    # The last failure rides along even when the job that failed is long
    # gone, so coming back to the page still answers "what happened".
    return {"manifest": m, "job": j, "last_failure": _last_failure(slug)}


def _publish_runtime(slug, label):
    """ensure_runtime with a job entry that actually completes. jlog alone
    creates a done=False job that nothing ever finishes, which left avatar
    cards stuck on 'publishing 100%' with disabled buttons until restart."""
    job_id = _reserve_job(slug, "publish", label)
    writer = jlog(slug, label) if job_id else (
        lambda msg: print(f"[avatar:{slug}] {msg}", flush=True))
    try:
        ensure_runtime(slug, log=writer)
    except Exception as error:
        if job_id:
            _finish_job(slug, job_id, error)
        raise
    if job_id:
        _finish_job(slug, job_id)


@app.post("/api/avatar/activate")
async def api_activate(b: Slug):
    r = reg()
    m = r.read_manifest(b.slug) or {}
    if m.get("status") != "ready":
        raise HTTPException(400, "build this avatar before activating it")
    try:
        _publish_runtime(b.slug, "publishing")
    except Exception as e:
        raise HTTPException(400, f"could not publish runtime: {e}")
    r.set_active(b.slug)
    if r.get_companion() == b.slug:
        # One avatar cannot hold both desks; promoting the companion to the
        # active face vacates the left desk.
        r.set_companion(None)
    return {"active": b.slug, "companion": r.get_companion()}


class CompanionRequest(BaseModel):
    # Empty clears the second desk; the strict pattern applies only when set.
    slug: str = Field(default="", max_length=64)


@app.post("/api/avatar/companion")
async def api_companion(request: CompanionRequest):
    r = reg()
    slug = request.slug.strip()
    if not slug:
        r.set_companion(None)
        return {"companion": None}
    if not re.fullmatch(SLUG_PATTERN, slug):
        raise HTTPException(422, "invalid avatar slug")
    manifest = r.read_manifest(slug) or {}
    if manifest.get("status") != "ready":
        raise HTTPException(400, "build this avatar before putting it on the desk")
    if slug == active_slug():
        raise HTTPException(
            400, "this avatar is already active; pick a different one for the left desk")
    try:
        _publish_runtime(slug, "publishing companion")
    except Exception as e:
        raise HTTPException(400, f"could not publish runtime: {e}")
    r.set_companion(slug)
    return {"companion": slug}


# ---------------------------------------------------------------- .avtr

AVTR_FORMAT = "vivieen-avatar"
AVTR_VERSION = 1
# The archive carries the avatar's source of truth; the runtime bundle and
# caches are deliberately absent so the importing app rebakes a runtime at
# ITS pipeline version - an imported avatar always gets the current eyes,
# skeleton, and reactions.
AVTR_TOP_EXCLUDES = {"runtime", "diag"}
MAX_AVTR_BYTES = 4 * 1024 * 1024 * 1024
MAX_AVTR_ENTRIES = 40000


def _avtr_pruned(name):
    return (name in AVTR_TOP_EXCLUDES or name == ".DS_Store"
            or name == "__pycache__" or name.endswith(".previous")
            or name.endswith(".activate-backup") or name.startswith(".runtime-")
            or name.startswith(".motion-") or name.startswith(".body-")
            or name.startswith(".import-"))


def _avatar_archive(slug, directory, destination):
    manifest = reg().read_manifest(slug) or {}
    root = os.path.abspath(directory)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("avtr.json", json.dumps({
            "format": AVTR_FORMAT,
            "version": AVTR_VERSION,
            "slug": slug,
            "name": manifest.get("name") or slug,
            "status": manifest.get("status") or "draft",
            "exported": datetime.datetime.now().isoformat(timespec="seconds"),
        }, indent=1))
        for base, folders, files in os.walk(root):
            folders[:] = sorted(f for f in folders if not _avtr_pruned(f))
            relative = os.path.relpath(base, root)
            for name in sorted(files):
                if _avtr_pruned(name):
                    continue
                full = os.path.join(base, name)
                if os.path.islink(full):
                    continue
                inner = name if relative == "." else f"{relative}/{name}"
                archive.write(full, f"avatar/{inner.replace(os.sep, '/')}")


def _import_avatar_archive(path, on_progress=None):
    with zipfile.ZipFile(path) as archive:
        try:
            meta = json.loads(archive.read("avtr.json"))
        except (KeyError, ValueError) as error:
            raise ValueError("not a Vivieen .avtr file") from error
        if meta.get("format") != AVTR_FORMAT:
            raise ValueError("not a Vivieen .avtr file")
        if int(meta.get("version") or 0) > AVTR_VERSION:
            raise ValueError(
                "this .avtr was exported by a newer Vivieen; update first")
        entries = archive.infolist()
        if len(entries) > MAX_AVTR_ENTRIES:
            raise ValueError("archive holds too many files")
        total = 0
        for info in entries:
            name = info.filename
            if name == "avtr.json" or name.endswith("/"):
                continue
            normal = posix_normpath(name)
            if (not normal.startswith("avatar/") or normal.startswith("/")
                    or ".." in normal.split("/")):
                raise ValueError(f"unsafe archive entry: {name}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("archive contains symlinks")
            total += info.file_size
            if total > MAX_AVTR_BYTES:
                raise ValueError("archive is too large")
        if "avatar/manifest.json" not in archive.namelist():
            raise ValueError("archive has no avatar manifest")

        base = re.sub(r"[^a-z0-9-]+", "-",
                      str(meta.get("slug") or "avatar").lower()).strip("-")[:40]
        base = base or "avatar"
        slug, counter = base, 2
        while os.path.exists(reg().adir(slug)):
            slug = f"{base}-{counter}"
            counter += 1
        target = reg().adir(slug)
        stage = target + ".import-stage"
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage, mode=0o700)
        try:
            stage_root = os.path.abspath(stage)
            # Unpacking a third of a gigabyte is not instant, and a bar
            # frozen at one number reads as a hang (owner, 2026-08-03).
            # Report by BYTES written, not files: the sprite sheets are
            # thousands of times bigger than the json beside them.
            unpacked = 0
            for info in entries:
                name = info.filename
                if name == "avtr.json" or name.endswith("/"):
                    continue
                relative = posix_normpath(name)[len("avatar/"):]
                destination = os.path.abspath(os.path.join(
                    stage_root, *relative.split("/")))
                if os.path.commonpath((stage_root, destination)) != stage_root:
                    raise ValueError(f"unsafe archive entry: {name}")
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with archive.open(info) as source, open(destination, "wb") as sink:
                    shutil.copyfileobj(source, sink)
                unpacked += info.file_size
                if on_progress and total:
                    on_progress(unpacked, total)
            manifest_path = os.path.join(stage_root, "manifest.json")
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = json.load(handle)
            manifest["slug"] = slug
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=1)
            os.replace(stage, target)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    return {"slug": slug, "name": manifest.get("name") or slug,
            "status": manifest.get("status") or "draft"}


@app.get("/api/avatar/export")
async def api_avatar_export(slug: str = Query(pattern=SLUG_PATTERN)):
    if not reg().read_manifest(slug):
        raise HTTPException(404, "avatar not found")
    handle = tempfile.NamedTemporaryFile(suffix=".avtr", delete=False)
    archive_path = handle.name
    handle.close()
    try:
        await asyncio.to_thread(
            _avatar_archive, slug, reg().adir(slug), archive_path)
    except Exception:
        if os.path.exists(archive_path):
            os.unlink(archive_path)
        raise
    from starlette.background import BackgroundTask

    def _cleanup():
        if os.path.exists(archive_path):
            os.unlink(archive_path)
    return FileResponse(
        archive_path, media_type="application/zip",
        filename=f"{slug}.avtr", background=BackgroundTask(_cleanup))


@app.post("/api/avatar/import")
async def api_avatar_import(archive: UploadFile = File(...)):
    handle = tempfile.NamedTemporaryFile(suffix=".avtr", delete=False)
    temp = handle.name
    try:
        total = 0
        while True:
            chunk = await archive.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_AVTR_BYTES:
                raise HTTPException(413, "avatar archive exceeds the 4 GB limit")
            handle.write(chunk)
        handle.close()
        try:
            result = await asyncio.to_thread(_import_avatar_archive, temp)
        except ValueError as error:
            raise HTTPException(422, str(error))
    finally:
        handle.close()
        if os.path.exists(temp):
            os.unlink(temp)
    slug = result["slug"]
    if result.get("status") == "ready":
        job_id = _reserve_job(slug, "import", "Publishing imported avatar")
        if job_id:
            def publish():
                failure = ""
                try:
                    ensure_runtime(slug, log=jlog(slug, "publishing import"))
                except Exception as error:
                    failure = str(error)
                finally:
                    _finish_job(slug, job_id, failure)
            threading.Thread(target=publish, daemon=True).start()
    return {"imported": True, **result}


# ------------------------------------------------------------- avatar store
# Ready-made companions hosted as GitHub release assets. The server does the
# downloading so the browser never streams a 300 MB file through fetch; the
# settings page polls /api/avatar/store for progress.
AVATAR_STORE_BASE = ("https://github.com/tivojn/vivieen-companion"
                     "/releases/download/avatar-store-v1/")
AVATAR_STORE = [
    {"id": "captain-ayer", "name": "Captain Ayer", "file": "Captain-Ayer.avtr",
     "bytes": 327227995,
     "face": "captain-ayer-face.jpg", "body": "captain-ayer-body.png",
     "blurb": ("The captain himself - dreadlocks, red bandana, and a full "
               "rig with body, walk, and edge idle, ready to hold the desk.")},
    {"id": "vvn", "name": "Vvn", "file": "Vvn.avtr", "bytes": 308302113,
     "face": "vvn-face.jpg", "body": "vvn-body.png",
     "blurb": ("Compact starter companion - face, full body, office walk, "
               "high-heel touch edge idle, and k-pop point dance, all in.")},
    {"id": "vivieen", "name": "Vivieen", "file": "Vivieen.avtr",
     "bytes": 448109330,
     "face": "vivieen-face.jpg", "body": "vivieen-body.png",
     "blurb": ("The signature companion - a complete rig with every "
               "animation set, ready for the desk the moment it lands.")},
]
_store_lock = threading.Lock()
_store_jobs = {}
_store_art = {}
STORE_BUSY_PHASES = ("downloading", "installing", "publishing")


def _store_entry(item_id):
    for item in AVATAR_STORE:
        if item["id"] == item_id:
            return item
    return None


def _store_install(item):
    import urllib.request
    key = item["id"]
    handle = tempfile.NamedTemporaryFile(suffix=".avtr", delete=False)
    temp = handle.name
    try:
        request = urllib.request.Request(
            AVATAR_STORE_BASE + item["file"],
            headers={"User-Agent": "vivieen-companion"})
        with urllib.request.urlopen(request, timeout=60) as feed:
            expect = int(feed.headers.get("Content-Length")
                         or item["bytes"] or 0)
            got = 0
            while True:
                chunk = feed.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_AVTR_BYTES:
                    raise ValueError("download exceeds the 4 GB limit")
                handle.write(chunk)
                with _store_lock:
                    # The download is most of the wait, so it owns most of
                    # the bar; the byte counts ride along so a slow line
                    # still shows something moving between percents.
                    _store_jobs[key].update(
                        phase="downloading",
                        pct=min(70, int(got * 70 / expect)) if expect else 0,
                        done_bytes=got, total_bytes=expect)
        handle.close()
        with _store_lock:
            _store_jobs[key].update(phase="installing", pct=70,
                                    done_bytes=0, total_bytes=0)

        def unpacking(written, total):
            with _store_lock:
                _store_jobs[key].update(
                    phase="installing", pct=70 + int(written * 25 / total),
                    done_bytes=written, total_bytes=total)

        result = _import_avatar_archive(temp, on_progress=unpacking)
        slug = result["slug"]
        with _store_lock:
            _store_jobs[key].update(phase="publishing", pct=95, slug=slug,
                                    done_bytes=0, total_bytes=0)
        if result.get("status") == "ready":
            ensure_runtime(slug, log=jlog(slug, "publishing store install"))
        with _store_lock:
            _store_jobs[key].update(phase="done", pct=100)
    except Exception as error:
        with _store_lock:
            _store_jobs[key].update(phase="error", error=str(error))
    finally:
        handle.close()
        if os.path.exists(temp):
            os.unlink(temp)


@app.get("/api/avatar/store")
async def api_avatar_store():
    with _store_lock:
        jobs = {key: dict(value) for key, value in _store_jobs.items()}
    return {"items": [
        {**item, "url": AVATAR_STORE_BASE + item["file"],
         "job": jobs.get(item["id"])}
        for item in AVATAR_STORE]}


@app.get("/api/avatar/thumb")
async def api_avatar_thumb(slug: str = Query(...), size: int = Query(320)):
    """A card-sized face, made once and kept.

    The carousel used to pull the full 1024px keyframe for every avatar -
    well over a megabyte each, so opening the deck crawled (owner,
    2026-08-03). This is the same face at card size, cached on disk after
    the first request and immutable thereafter, which lets the phone keep
    its own copy forever."""
    import cv2
    r = reg()
    if slug not in {a["slug"] for a in r.list_avatars()}:
        raise HTTPException(404, "no such avatar")
    size = max(64, min(512, int(size)))
    cache = os.path.join(r.AVATARS, slug, f"thumb-{size}.jpg")
    if not os.path.isfile(cache):
        source = None
        for name in ("keyframe.png", "source-keyframe.png", "source.jpg"):
            candidate = os.path.join(r.AVATARS, slug, name)
            if os.path.isfile(candidate):
                source = candidate
                break
        if not source:
            raise HTTPException(404, "no face to show")
        image = cv2.imread(source, cv2.IMREAD_COLOR)
        if image is None:
            raise HTTPException(404, "unreadable face")
        height, width = image.shape[:2]
        side = min(height, width)
        # Square on the face, which sits in the upper middle of a portrait.
        x0 = max(0, (width - side) // 2)
        crop = image[0:side, x0:x0 + side]
        thumb = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
        cv2.imwrite(cache, thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
    return FileResponse(cache, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/avatar/store/art")
async def api_avatar_store_art(id: str = Query(...),
                               kind: str = Query("face")):
    """Face/body preview for a store card. The page's CSP only allows
    same-origin images, so the server fetches the GitHub asset once and
    keeps it in memory (~25-75 KB each)."""
    item = _store_entry(id)
    if not item or kind not in ("face", "body"):
        raise HTTPException(404, "no such store art")
    name = item[kind]
    cached = _store_art.get(name)
    if cached is None:
        import urllib.request
        request = urllib.request.Request(
            AVATAR_STORE_BASE + name,
            headers={"User-Agent": "vivieen-companion"})
        def fetch():
            with urllib.request.urlopen(request, timeout=30) as feed:
                return feed.read(4 << 20)
        cached = await asyncio.to_thread(fetch)
        _store_art[name] = cached
    media = "image/jpeg" if name.endswith(".jpg") else "image/png"
    return Response(cached, media_type=media,
                    headers={"Cache-Control": "max-age=86400"})


class StoreInstall(BaseModel):
    id: str


@app.post("/api/avatar/store/install")
async def api_avatar_store_install(request: StoreInstall):
    item = _store_entry(request.id)
    if not item:
        raise HTTPException(404, "no such avatar in the store")
    with _store_lock:
        job = _store_jobs.get(item["id"])
        if job and job.get("phase") in STORE_BUSY_PHASES:
            return {"started": False, "job": dict(job)}
        _store_jobs[item["id"]] = {"phase": "downloading", "pct": 0,
                                   "error": "", "slug": "",
                                   "done_bytes": 0,
                                   "total_bytes": int(item.get("bytes") or 0)}
    threading.Thread(target=_store_install, args=(item,), daemon=True).start()
    return {"started": True}


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
    return {"app_id": APP_ID, "active": active_slug(),
            # Which look the owner picked. It lived in localStorage, which
            # is PER DEVICE, so the desk and the phone drifted apart and
            # stayed that way (owner, 2026-08-04). One answer, from here.
            "design": ((P.load().get("ui") or {}).get("design") or "quiet"),
            "companion": reg().get_companion()}


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
    for k in ("llm", "tts", "stt", "image", "video"):
        blk = body.get(k)
        if not isinstance(blk, dict):
            continue
        blk.pop("has_key", None)
        provider_changed = bool(blk.get("provider")) and \
            blk.get("provider") != (cur.get(k) or {}).get("provider")
        requested_key = blk.get("api_key")
        if blk.get("provider") == "enconvo" or requested_key == "__clear__" or \
           (provider_changed and not requested_key):
            # "__clear__" rides through to the vault, which deletes the
            # Keychain entry - not just the marker in the file.
            blk["api_key"] = "__clear__"
        elif not requested_key:
            blk.pop("api_key", None)
    live = body.get("live")
    if isinstance(live, dict):
        for field in ("xai_api_key", "eleven_api_key"):
            live.pop("has_" + field, None)
            if live.get(field) == "__clear__":
                if field == "eleven_api_key":
                    live["eleven_agent_id"] = ""
            elif not live.get(field):
                live.pop(field, None)
        # The agent bakes its voice in at creation - a different voice
        # means a fresh agent next time the line opens.
        previous_voice = (cur.get("live") or {}).get("eleven_voice_id") or ""
        if "eleven_voice_id" in live and live["eleven_voice_id"] != previous_voice:
            live["eleven_agent_id"] = ""
    new = P.save(body)
    if (new.get("stt") or {}).get("provider") != (cur.get("stt") or {}).get("provider") or \
       (new.get("tts") or {}).get("provider") != (cur.get("tts") or {}).get("provider"):
        _state["warm"] = False
        threading.Thread(target=_warm, daemon=True).start()
    # A live-talk change while a line is open hot-swaps the provider leg:
    # every open call reconnects with the new provider/voice mid-call.
    if _live_swaps and _live_hot_fields(new) != _live_hot_fields(cur):
        for waiting in list(_live_swaps):
            waiting.set()
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


def _ollama_listener():
    """(pid, address) of whatever holds 11434, or (0, "")."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP:11434", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return 0, ""
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9 and ":11434" in parts[-2]:
            try:
                return int(parts[1]), parts[-2]
            except ValueError:
                return 0, parts[-2]
    return 0, ""


def _open_ollama_to_the_lan():
    """Ollama on loopback is invisible to the phone.

    Ollama binds 127.0.0.1 unless OLLAMA_HOST says otherwise, so with Think
    set to Ollama the phone syncs a base_url pointing at ITSELF and solo has
    no brain at all (owner, 2026-08-04).

    Ollama.app is what spawns the server here, and killing its child only
    gets the child respawned on loopback again. So set the variable where
    the app will inherit it - launchctl's user session - and restart the
    app itself. The menu bar app keeps managing models and updates; it
    simply listens on every interface now.

    Never touched unless Ollama is BOTH the chosen brain and currently
    loopback-only: a machine already open, or not using Ollama, is left
    exactly alone.
    """
    try:
        provider = ((P.load().get("llm") or {}).get("provider") or "").lower()
        if provider != "ollama":
            return
        pid, address = _ollama_listener()
        if not address:
            return                      # not running; nothing to reopen
        if not address.startswith("127.0.0.1") and not address.startswith("[::1]"):
            return                      # already reachable from the network
        print(f"[viv] ollama is on {address} - the phone cannot reach that; "
              "reopening it on the LAN", flush=True)
        subprocess.run(["launchctl", "setenv", "OLLAMA_HOST", "0.0.0.0"],
                       capture_output=True, timeout=10)
        app = "/Applications/Ollama.app"
        if os.path.isdir(app):
            subprocess.run(
                ["osascript", "-e", 'quit app "Ollama"'],
                capture_output=True, timeout=20)
            time.sleep(2)
            subprocess.run(["open", "-a", app], capture_output=True, timeout=20)
        else:
            # No menu bar app: stop the bare server and start our own.
            if pid:
                subprocess.run(["kill", str(pid)], capture_output=True, timeout=10)
            time.sleep(1)
            binary = shutil.which("ollama")
            if not binary:
                print("[viv] ollama binary not found; left as it was", flush=True)
                return
            subprocess.Popen(
                [binary, "serve"],
                env={**os.environ, "OLLAMA_HOST": "0.0.0.0"},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        for _ in range(20):
            time.sleep(1)
            _, now = _ollama_listener()
            if now and not now.startswith("127.0.0.1"):
                print(f"[viv] ollama now listening on {now}", flush=True)
                return
        print("[viv] ollama did not come back on the LAN - it may need "
              "OLLAMA_HOST=0.0.0.0 set by hand", flush=True)
    except Exception as error:
        # Never let this stop the engine starting.
        print("[viv] could not reopen ollama:", P.safe_error(error), flush=True)


def _start():
    s = active_slug()
    if s:
        try:
            ensure_runtime(s)
        except Exception as e:
            print("[viv] runtime bundle missing:", e, flush=True)
    threading.Thread(target=_open_ollama_to_the_lan, daemon=True).start()
    threading.Thread(target=_warm, daemon=True).start()
    threading.Thread(target=_warm_media_tools, daemon=True).start()
    # Internet reach, opt-in: the relay agent only exists while
    # ~/Library/Application Support/Vivieen/relay-url does. Delete the
    # file and restart to roll the whole feature back.
    try:
        import relay_agent
        relay_agent.start(os.environ.get("VIVIEEN_PORT", "8777"))
    except Exception as error:
        print("[viv] relay agent skipped:", P.safe_error(error, 120), flush=True)


def _warm_media_tools():
    """The first run of a Homebrew binary can stall for a minute while the
    system vets it - long enough that an agent's first video shipped as an
    unplayable card. Pay that cost here, not inside somebody's first reply."""
    for name in ("ffprobe", "ffmpeg"):
        found = shutil.which(name)
        if not found:
            continue
        try:
            subprocess.run([found, "-version"], capture_output=True,
                           timeout=240, stdin=subprocess.DEVNULL)
        except Exception:
            pass


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

    return {"app_id": APP_ID, "boot": BOOT_ID,
            # Both pages read the look from HERE - it was on /api/meta,
            # which neither of them fetches, so the fix never fired.
            "design": ((cfg.get("ui") or {}).get("design") or "quiet"),
            # Light/dark rides the same channel (#30). Empty means the
            # owner never chose, and every device keeps following its
            # own room.
            "theme": ((cfg.get("ui") or {}).get("theme") or ""),
            "warm": _state["warm"], "warming": _state["warming"],
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


# ------------------------------------------------------------ live dictation
# Hold-to-talk streams here for word-by-word dictation into the input field.
# The bridge speaks Soniox's realtime WebSocket protocol server-side (the
# API key never reaches the renderer) and forwards MediaRecorder's webm
# chunks as-is - Soniox's audio_format "auto" decodes the container. If the
# dictation default is not a Soniox realtime model, the endpoint reports
# unavailable and the client falls back to batch interim transcription.

SONIOX_RT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"


def _soniox_stream_config():
    # Vivieen's own Soniox provider (direct API key, no EnConvo) wins when
    # selected; otherwise EnConvo's dictation default is honoured when it
    # points at Soniox, with the key borrowed from EnConvo's credentials.
    own = P.load().get("stt") or {}
    if own.get("provider") == "soniox" and own.get("api_key"):
        return P._soniox_config(own)
    mapped = P.global_default("stt")
    if mapped.get("provider") != "soniox":
        return None
    model = str(mapped.get("model") or "")
    if not model.startswith("stt-rt"):
        model = "stt-rt-v5"
    detail = P._run_enconvo_json(
        ["config", "get", "credentials|soniox", "--includes", "apiKey"],
        timeout=15)
    key = str(detail.get("apiKey") or "")
    if not key:
        return None
    config = {"api_key": key, "model": model, "audio_format": "auto"}
    language = str(mapped.get("language") or "")
    if language and language != "auto":
        config["language_hints"] = [language]
    return config


@app.websocket("/stt/stream")
async def stt_stream(client: WebSocket):
    # The http auth middleware does not run for websocket scopes, so the
    # token check happens here; Electron injects the header on the upgrade.
    if AUTH_TOKEN:
        if not secrets.compare_digest(_client_token(client), AUTH_TOKEN):
            await client.close(code=4403)
            return
    await client.accept()
    try:
        config = await asyncio.to_thread(_soniox_stream_config)
    except Exception as e:
        config = None
        print("[viv] dictation stream config failed:", P.safe_error(e), flush=True)
    if not config:
        await client.send_json({"error": "realtime dictation unavailable",
                                "finished": True})
        await client.close()
        return
    import websockets
    finals = []
    try:
        async with websockets.connect(
                SONIOX_RT_URL, max_size=1 << 22, open_timeout=10) as upstream:
            await upstream.send(json.dumps(config))

            async def pump_audio():
                while True:
                    data = await client.receive_bytes()
                    if not data:
                        # End-of-take. Soniox's docs accept "an empty binary
                        # or text frame", but measured live only the empty
                        # TEXT frame finalises - empty binary just times out.
                        await upstream.send("")
                        return
                    await upstream.send(data)

            audio_task = asyncio.create_task(pump_audio())
            try:
                async for message in upstream:
                    payload = json.loads(message)
                    if payload.get("error_code") or payload.get("error_message"):
                        await client.send_json({
                            "error": str(payload.get("error_message")
                                         or "provider error")[:200],
                            "finished": True})
                        return
                    interim = []
                    for token in payload.get("tokens") or []:
                        text = str(token.get("text") or "")
                        if token.get("is_final"):
                            finals.append(text)
                        else:
                            interim.append(text)
                    text = ("".join(finals) + "".join(interim)).strip()
                    if payload.get("finished"):
                        await client.send_json({
                            "finished": True,
                            "final": "".join(finals).strip() or text})
                        return
                    await client.send_json({"text": text})
            finally:
                audio_task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("[viv] dictation stream failed:", P.safe_error(e), flush=True)
        try:
            await client.send_json({"error": P.safe_error(e, 200),
                                    "finished": True})
        except Exception:
            pass
    finally:
        try:
            await client.close()
        except Exception:
            pass


# ------------------------------------------------------------ live voice
# "Live talk": a realtime speech-to-speech conversation. The renderer
# streams raw PCM16 mic frames as binary websocket messages; this bridge
# speaks the provider's protocol server-side (keys never reach the
# renderer) and forwards a unified event stream back:
#   {type:ready,input_rate,output_rate,provider} | {type:audio,data,rate}
#   {type:user_text,text} | {type:agent_text,text,final} | {type:interrupt}
#   {type:closed,reason} | {type:error,message}
# A silence watchdog hangs up after two quiet minutes: realtime providers
# bill per OPEN-LINE minute, and an idle line must never be a meter.

XAI_LIVE_VOICES = [
    {"id": "eve", "name": "Eve · warm, expressive"},
    {"id": "ara", "name": "Ara · bright, upbeat"},
    {"id": "leo", "name": "Leo · steady, male"},
    {"id": "rex", "name": "Rex · deep, male"},
    {"id": "sal", "name": "Sal · neutral, calm"},
]


@app.get("/api/live/voices")
async def api_live_voices(provider: str = Query(pattern=r"^(xai|elevenlabs)$")):
    if provider == "xai":
        return {"voices": XAI_LIVE_VOICES}
    key = (P.load().get("live") or {}).get("eleven_api_key") or ""
    if not key:
        return {"voices": [], "error": "no ElevenLabs key stored"}
    import requests

    def fetch():
        r = requests.get("https://api.elevenlabs.io/v1/voices",
                         headers={"xi-api-key": key}, timeout=20)
        r.raise_for_status()
        return [{"id": v["voice_id"], "name": v.get("name") or v["voice_id"]}
                for v in r.json().get("voices") or []]
    try:
        return {"voices": await asyncio.to_thread(fetch)}
    except Exception as e:
        return {"voices": [], "error": P.safe_error(e, 120)}


@app.get("/api/live/voice-preview")
async def api_live_voice_preview(
        provider: str = Query(pattern=r"^(xai|elevenlabs)$"),
        voice: str = Query(default="", max_length=64)):
    """A short spoken sample so a voice can be chosen by EAR - the whole
    point of a voice list (owner request 2026-08-02). xAI generates a
    line via its TTS REST; ElevenLabs ships ready preview clips with its
    voice roster."""
    live = P.load().get("live") or {}
    import requests

    def fetch():
        if provider == "xai":
            key = live.get("xai_api_key") or ""
            if not key:
                raise RuntimeError("no xAI key stored")
            r = requests.post(
                "https://api.x.ai/v1/tts",
                headers={"Authorization": "Bearer " + key},
                json={"text": "Hi, I'm Vivieen - this is how I sound live.",
                      "voice": voice or "eve", "language": "en"},
                timeout=30)
            r.raise_for_status()
            return r.content, r.headers.get("content-type") or "audio/mpeg"
        key = live.get("eleven_api_key") or ""
        if not key:
            raise RuntimeError("no ElevenLabs key stored")
        r = requests.get("https://api.elevenlabs.io/v1/voices",
                         headers={"xi-api-key": key}, timeout=20)
        r.raise_for_status()
        url = next((v.get("preview_url") for v in r.json().get("voices") or []
                    if v.get("voice_id") == voice and v.get("preview_url")), "")
        if not url:
            raise RuntimeError("no preview for this voice")
        clip = requests.get(url, timeout=20)
        clip.raise_for_status()
        return clip.content, clip.headers.get("content-type") or "audio/mpeg"
    try:
        data, mime = await asyncio.to_thread(fetch)
        return Response(content=data, media_type=mime,
                        headers={"Cache-Control": "max-age=3600"})
    except Exception as e:
        raise HTTPException(502, P.safe_error(e, 160))


@app.get("/live-worklet.js")
async def live_worklet():
    return FileResponse(
        os.path.join(WEB, "live-worklet.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"})


XAI_REALTIME_URL = "wss://api.x.ai/v1/realtime"
ELEVEN_CONVAI_URL = "wss://api.elevenlabs.io/v1/convai/conversation"
LIVE_SILENCE_HANGUP_S = 15


# Live lines currently open. Saving a live-talk change in Settings sets
# every event here; each open call drops its provider leg and reconnects
# with the new settings while the browser socket stays up - voice and even
# provider switch mid-call.
_live_swaps = set()


def _live_hot_fields(cfg):
    """The live-talk settings whose change should hot-swap an open call."""
    live = cfg.get("live") or {}
    return (live.get("provider") or "xai",
            live.get("xai_voice") or "", live.get("xai_model") or "",
            live.get("eleven_voice_id") or "",
            bool(live.get("xai_api_key")), bool(live.get("eleven_api_key")))


def _live_settings():
    cfg = P.load()
    live = cfg.get("live") or {}
    persona = effective_persona(cfg)
    provider = live.get("provider") or "xai"
    if provider == "xai" and live.get("xai_api_key"):
        return dict(provider="xai", key=live["xai_api_key"],
                    voice=live.get("xai_voice") or "eve",
                    model=live.get("xai_model") or "grok-voice-think-fast-1.0",
                    persona=persona)
    if provider == "elevenlabs" and live.get("eleven_api_key"):
        return dict(provider="elevenlabs", key=live["eleven_api_key"],
                    voice=live.get("eleven_voice_id") or "",
                    agent_id=live.get("eleven_agent_id") or "",
                    persona=persona)
    return None


def _ensure_eleven_agent(settings):
    """Create (once) and remember the Conversational-AI agent that carries
    the persona; ElevenLabs realtime only talks through an agent."""
    if settings.get("agent_id"):
        return settings["agent_id"]
    import requests
    agent = {
        "name": "Vivieen live talk",
        "conversation_config": {
            "agent": {
                "first_message": "",
                "language": "en",
                "prompt": {"prompt": settings["persona"] or
                           "You are Vivieen, a warm desktop companion. "
                           "Keep replies to one or two short spoken sentences."},
            },
        },
    }
    if settings.get("voice"):
        agent["conversation_config"]["tts"] = {"voice_id": settings["voice"]}
    response = requests.post(
        "https://api.elevenlabs.io/v1/convai/agents/create",
        headers={"xi-api-key": settings["key"]}, json=agent, timeout=30)
    response.raise_for_status()
    agent_id = str(response.json().get("agent_id") or "")
    if not agent_id:
        raise RuntimeError("agent creation returned no agent_id")
    P.save({"live": {"eleven_agent_id": agent_id}})
    return agent_id


@app.websocket("/live/voice")
async def live_voice(client: WebSocket):
    if AUTH_TOKEN:
        if not secrets.compare_digest(_client_token(client), AUTH_TOKEN):
            await client.close(code=4403)
            return
    await client.accept()
    import websockets
    last_audio = [time.time()]
    swap = asyncio.Event()
    _live_swaps.add(swap)
    try:
        while True:
            settings = _live_settings()
            if not settings:
                await client.send_json({
                    "type": "error",
                    "message": "Live talk is not configured - add an xAI or "
                               "ElevenLabs key under Settings > Models > "
                               "Live voice."})
                break
            # The live mistake (2026-08-01): the console's key ID (a UUID)
            # pasted where the API key goes - upstream answers an opaque
            # HTTP 400. Catch it here with a message that names the fix.
            if settings["provider"] == "xai" and \
                    not settings["key"].startswith("xai-"):
                await client.send_json({
                    "type": "error",
                    "message": "That xAI value looks like a key ID, not an "
                               "API key - real keys start with 'xai-'. Copy "
                               "the full key from console.x.ai and save it "
                               "again."})
                break
            swap.clear()
            if settings["provider"] == "xai":
                url = f"{XAI_REALTIME_URL}?model={settings['model']}"
                headers = {"Authorization": "Bearer " + settings["key"]}
                async with websockets.connect(
                        url, additional_headers=headers,
                        max_size=1 << 24, open_timeout=15) as upstream:
                    await upstream.send(json.dumps({
                        "type": "session.update", "session": {
                            "voice": settings["voice"],
                            "instructions": settings["persona"],
                            "turn_detection": {"type": "server_vad",
                                               "silence_duration_ms": 700},
                            "audio": {
                                "input": {"format": {"type": "audio/pcm",
                                                     "rate": 24000},
                                          "transport": "json"},
                                "output": {"format": {"type": "audio/pcm",
                                                      "rate": 24000},
                                           "transport": "json"},
                            }}}))
                    await client.send_json({"type": "ready",
                                            "provider": "xai",
                                            "input_rate": 24000,
                                            "output_rate": 24000})
                    swapped = await _live_pump(
                        client, upstream, _xai_event, last_audio,
                        lambda b64: json.dumps(
                            {"type": "input_audio_buffer.append",
                             "audio": b64}), swap=swap)
            else:
                agent_id = await asyncio.to_thread(_ensure_eleven_agent,
                                                   settings)
                url = f"{ELEVEN_CONVAI_URL}?agent_id={agent_id}"
                headers = {"xi-api-key": settings["key"]}
                async with websockets.connect(
                        url, additional_headers=headers,
                        max_size=1 << 24, open_timeout=15) as upstream:
                    await client.send_json({"type": "ready",
                                            "provider": "elevenlabs",
                                            "input_rate": 16000,
                                            "output_rate": 16000})
                    swapped = await _live_pump(
                        client, upstream, _eleven_event, last_audio,
                        lambda b64: json.dumps(
                            {"user_audio_chunk": b64}), swap=swap)
            if not swapped:
                break
            # Hot swap: the provider leg just closed; tell the renderer the
            # line is switching, forgive the gap on the silence clock, and
            # reconnect with the freshly saved settings.
            last_audio[0] = time.time()
            await client.send_json({"type": "switching"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print("[viv] live voice failed:", P.safe_error(e), flush=True)
        try:
            await client.send_json({"type": "error",
                                    "message": P.safe_error(e, 200)})
        except Exception:
            pass
    finally:
        _live_swaps.discard(swap)
        try:
            await client.close()
        except Exception:
            pass


async def _live_pump(client, upstream, translate, last_audio, wrap_audio,
                     swap=None):
    """Three tasks: mic frames up, provider events down, silence watchdog.
    A fourth waits on the hot-swap event; returns True when that one fired
    so the caller can reconnect the provider leg mid-call."""
    async def uplink():
        while True:
            message = await client.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data:
                # Only a VOICE resets the silence clock. The phone streams
                # continuously - zeroed frames while she speaks, room tone
                # while nobody does - and counting those as "audio" meant
                # the quiet-line hangup could never fire.
                samples = np.frombuffer(data, dtype="<i2")
                if samples.size and float(np.sqrt(np.mean(
                        (samples.astype(np.float32) / 32768.0) ** 2))) > 0.012:
                    last_audio[0] = time.time()
                await upstream.send(wrap_audio(
                    base64.b64encode(data).decode("ascii")))
            elif message.get("text"):
                control = json.loads(message["text"])
                if control.get("type") == "stop":
                    return

    async def downlink():
        async for raw in upstream:
            if isinstance(raw, (bytes, bytearray)):
                continue
            payload = json.loads(raw)
            if payload.get("type") == "ping":       # ElevenLabs keep-alive
                await upstream.send(json.dumps({
                    "type": "pong",
                    "event_id": (payload.get("ping_event") or {}).get("event_id")}))
                continue
            for event in translate(payload):
                await client.send_json(event)

    async def watchdog():
        while True:
            await asyncio.sleep(5)
            if time.time() - last_audio[0] > LIVE_SILENCE_HANGUP_S:
                await client.send_json({"type": "closed", "reason": "silence"})
                return

    tasks = [asyncio.create_task(coro(), name=coro.__name__)
             for coro in (uplink, downlink, watchdog)]
    if swap is not None:
        async def swapwait():
            await swap.wait()
        tasks.append(asyncio.create_task(swapwait(), name="swapwait"))
    try:
        done, _ = await asyncio.wait(tasks,
                                     return_when=asyncio.FIRST_COMPLETED)
        return any(task.get_name() == "swapwait" for task in done)
    finally:
        for task in tasks:
            task.cancel()


def _xai_event(payload):
    kind = payload.get("type") or ""
    if kind == "response.output_audio.delta":
        data = payload.get("delta") or payload.get("audio") or ""
        if data:
            yield {"type": "audio", "data": data, "rate": 24000}
    elif kind == "response.output_audio_transcript.delta":
        yield {"type": "agent_text",
               "text": payload.get("delta") or "", "final": False}
    elif kind == "response.output_audio_transcript.done":
        yield {"type": "agent_text",
               "text": payload.get("transcript") or "", "final": True}
    elif kind == "conversation.item.input_audio_transcription.updated":
        yield {"type": "user_text", "text": payload.get("transcript") or ""}
    elif kind == "input_audio_buffer.speech_started":
        yield {"type": "interrupt"}
    elif kind == "error":
        yield {"type": "error",
               "message": str((payload.get("error") or {}).get("message")
                              or payload)[:200]}


def _eleven_event(payload):
    kind = payload.get("type") or ""
    if kind == "audio":
        data = (payload.get("audio_event") or {}).get("audio_base_64") or ""
        if data:
            yield {"type": "audio", "data": data, "rate": 16000}
    elif kind == "user_transcript":
        yield {"type": "user_text",
               "text": (payload.get("user_transcription_event") or {}
                        ).get("user_transcript") or ""}
    elif kind == "agent_response":
        yield {"type": "agent_text",
               "text": (payload.get("agent_response_event") or {}
                        ).get("agent_response") or "", "final": True}
    elif kind == "interruption":
        yield {"type": "interrupt"}


class Turn(BaseModel):
    history: list


# Uncoupled Vivieen's own hands. The directive-in-prompt design is
# legitimate HERE, unlike in the coupled lane: this brain is ours, so its
# tool belt is ours to strap on. One directive per turn, on its own line.
_OWN_TOOLS = (
    "\n\nYou can create media. To do it, put ONE of these on its own line "
    "and end your reply there - the result is attached for you:\n"
    "<<viv:image detailed description of the picture>>\n"
    "<<viv:video detailed description of the clip>>\n"
    "Use them only when the user asks for a picture/image/photo or a "
    "video/clip. Never mention the directive syntax.")
_OWN_TOOL_CALL = re.compile(r"<<viv:(image|video)\s+(.+?)>>", re.S)


def effective_persona(cfg=None):
    """Who she is right now.

    A face and a character are the same thing to whoever is talking to
    her: put Captain Sparrow on the desk and he should answer as Sparrow,
    not as the house assistant wearing his face (owner, 2026-08-04). The
    ACTIVE avatar's own persona wins; the global one is the fallback for
    every avatar that has not been given one, so an empty field keeps
    today's behaviour exactly.
    """
    cfg = cfg if cfg is not None else P.load()
    house = ((cfg.get("persona") or {}).get("system") or "").strip()
    slug = active_slug()
    if not slug:
        return house
    manifest = reg().read_manifest(slug) or {}
    mine = ((manifest.get("persona") or {}).get("system") or "").strip()
    return mine or house


@app.post("/reply")
async def reply(t: Turn):
    import media_gen
    cfg = P.load()
    try:
        text = await P.chat(t.history[-12:], cfg["llm"],
                            system=effective_persona(cfg) + _OWN_TOOLS)
    except Exception as e:
        print("[viv] llm failed:", P.safe_error(e), flush=True)
        hint = P.failure_hint(e)
        text = (f"My model is not answering — {hint}. Check the provider in Settings."
                if hint else
                "My model is not answering. Check the provider in Settings.")
    if not text:
        text = "I lost that thread for a second. Say it again?"
    cards = []
    call = _OWN_TOOL_CALL.search(text)
    if call:
        kind, prompt = call.group(1), call.group(2).strip()
        text = _OWN_TOOL_CALL.sub("", text).strip()
        try:
            if kind == "image":
                made = await media_gen.generate_image(prompt, cfg["image"])
            else:
                made = await media_gen.generate_video(prompt, cfg["video"])
            url = await asyncio.to_thread(_enconvo_share, made)
            if url:
                cards.append({"url": url, "name": prompt[:60]})
            if not text:
                text = "Here it is." if kind == "image" else \
                       "Here's the clip."
        except Exception as error:
            detail = P.safe_error(error, 140)
            print("[viv] media generation failed:", detail, flush=True)
            text = (text + " " if text else "") + \
                f"(I tried to make the {kind}, but the provider said: {detail})"
    result = await _say(text, cfg)
    result["media"] = cards
    result["llm_route"] = P.last_route("llm")
    return result


class Say(BaseModel):
    text: str


@app.post("/say")
async def say(s: Say):
    return await _say(s.text, P.load())


# ------------------------------------------------------------ solo sync
def _lan_base_url(url):
    """Rewrite a loopback provider URL to this Mac's LAN address, keeping
    scheme, port, and path. Anything already reachable passes through, and
    with no LAN address to offer the loopback stays - a wrong address is
    worse than an honest unreachable one."""
    from urllib.parse import urlsplit, urlunsplit
    from server.relay_agent import lan_addresses
    try:
        parts = urlsplit(url)
        if parts.hostname not in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
            return url
        lan = lan_addresses(parts.port or 80)
        if not lan:
            return url
        host = urlsplit(lan[0]).hostname
        port = f":{parts.port}" if parts.port else ""
        return urlunsplit((parts.scheme, host + port, parts.path,
                           parts.query, parts.fragment))
    except Exception:
        return url


# The phone's independent mode needs the provider config and the keys.
# Config travels in the clear; every SECRET is AES-GCM encrypted under a
# key derived (HKDF) from the pairing token - which the relay never sees -
# so the payload can cross the blind mailbox without anything readable
# ever resting on third-party disk.

@app.get("/api/sync/solo")
async def api_sync_solo():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes as _hashes

    cfg = P.load()
    key = HKDF(algorithm=_hashes.SHA256(), length=32,
               salt=b"viv-solo-sync", info=b"v1").derive(
                   AUTH_TOKEN.encode())
    aead = AESGCM(key)

    def seal(value):
        nonce = secrets.token_bytes(12)
        return {"n": base64.b64encode(nonce).decode(),
                "c": base64.b64encode(
                    aead.encrypt(nonce, value.encode(), None)).decode()}

    sealed = {}
    for block_name, fields in (("llm", ("api_key",)), ("tts", ("api_key",)),
                               ("stt", ("api_key",)), ("image", ("api_key",)),
                               ("video", ("api_key",)),
                               ("live", ("xai_api_key", "eleven_api_key"))):
        for field in fields:
            value = (cfg.get(block_name) or {}).get(field) or ""
            if value:
                sealed[f"{block_name}.{field}"] = seal(value)

    config = {}
    for name in ("llm", "tts", "stt", "image", "video", "live"):
        block = dict(cfg.get(name) or {})
        for field in ("api_key", "xai_api_key", "eleven_api_key"):
            block.pop(field, None)
        # A loopback base_url is this Mac talking to itself. Shipped
        # verbatim, the phone stores an address that points at the PHONE,
        # so the provider only ever "worked" where it could never run
        # (#28). Send the Mac's LAN address instead; off that Wi-Fi the
        # page falls back to a key it holds - and names the swap.
        # An EMPTY base_url hides the same trap one layer down: the
        # engine's ollama default is localhost:11434, so spell it out
        # before rewriting or the phone inherits the note-to-self.
        if name == "llm" and block.get("provider") == "ollama" \
                and not block.get("base_url"):
            block["base_url"] = "http://127.0.0.1:11434"
        if block.get("base_url"):
            block["base_url"] = _lan_base_url(block["base_url"])
        config[name] = block
    config["persona"] = dict(cfg.get("persona") or {})
    # The phone answers with whoever is on ITS stage, and that is the
    # avatar the Mac has active - send the resolved persona, not the house
    # one, or solo would break character the moment the Mac was gone.
    config["persona"]["system"] = effective_persona(cfg)
    return {"v": 1, "updated_at": int(time.time()),
            "config": config, "secrets": sealed}


# ------------------------------------------------------------ enconvo lane
# EnConvo's local gateway (port 54535) routes into every extension. The
# pocket app couples to any EnConvo agent through here and behaves as one
# more IM channel: the agent's brain and its whole tool belt, her face and
# her voice. Sessions persist per agent; the agent does the work itself.
def _enconvo_call(path, params, timeout=120):
    import urllib.request
    request = urllib.request.Request(
        f"{ENCONVO_API}/{path}", method="POST",
        data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as feed:
        return json.loads(feed.read().decode() or "{}")


# Ask Mavis to fetch a video and she answers with a PATH on the Mac's disk -
# useless to a phone. Every path she names that points at real media becomes
# a served URL, so the reply lands in the thread as a card you can play
# (owner: "I expect mavis download it and send the video in the thread").
# Handles are opaque and minted only here: the phone can never name a file
# the agent did not already hand it.
_ENCONVO_FILES = {}
_ENCONVO_MEDIA = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".heic": "image/heic", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".wav": "audio/wav", ".aac": "audio/aac", ".ogg": "audio/ogg",
    ".pdf": "application/pdf",
}
# The folder every home lives in - "/Users" on a Mac - taken from this
# machine rather than spelled out, so the pattern travels.
_HOME_ROOT = os.path.dirname(os.path.expanduser("~")) or "/home"
_PATH_IN_TEXT = re.compile(
    r"(?:file://)?(?:~|" + re.escape(_HOME_ROOT) + r"/[^/\s]+"
    r"|/private/var/folders|/tmp|/var/folders)"
    r"(?:/[^\s()\[\]<>\"'`|]+)+")


def _enconvo_roots():
    home = os.path.expanduser("~")
    roots = [os.path.join(home, part) for part in
             (".enconvo", "Downloads", "Movies", "Pictures", "Documents",
              "Desktop")]
    roots.append(os.path.realpath(tempfile.gettempdir()))
    roots.append(os.path.realpath(os.path.join(reg().AVATARS, "..")))
    return [os.path.realpath(root) for root in roots]


def _enconvo_share(path):
    """Mint a handle for one on-disk file, or None if it is not ours to
    serve. Returns the URL the renderer should link to."""
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except Exception:
        return None
    if not os.path.isfile(real):
        return None
    if os.path.splitext(real)[1].lower() not in _ENCONVO_MEDIA:
        return None
    if not any(real == root or real.startswith(root + os.sep)
               for root in _enconvo_roots()):
        return None
    real = _phone_playable(real)
    handle = hashlib.sha256(real.encode()).hexdigest()[:20]
    _ENCONVO_FILES[handle] = real
    name = urllib.parse.quote(os.path.basename(real))
    return f"api/enconvo/file/{handle}/{name}"


def _enconvo_media(text):
    """Rewrite every servable path in her reply into a link, and report the
    cards alongside so the phone can render them even if the prose does
    not read like a link."""
    cards = []
    seen = {}

    def swap(match):
        raw = match.group(0)
        # Trailing punctuation belongs to the sentence, not the filename.
        trimmed = raw.rstrip(".,;:!?)")
        target = trimmed[7:] if trimmed.startswith("file://") else trimmed
        url = seen.get(target)
        if url is None:
            url = _enconvo_share(target) or ""
            seen[target] = url
            if url:
                cards.append({"url": url, "name": os.path.basename(target)})
        if not url:
            return raw
        # Already inside a markdown link - swap the destination, or the
        # result nests brackets and renders as literal junk.
        if match.string[max(0, match.start() - 2):match.start()] == "](":
            return url + raw[len(trimmed):]
        return f"[{os.path.basename(target)}]({url})" + raw[len(trimmed):]

    return _PATH_IN_TEXT.sub(swap, text or ""), cards


@app.get("/api/enconvo/file/{handle}/{name}")
async def api_enconvo_file(handle: str, name: str):
    path = _ENCONVO_FILES.get(handle)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "no such file")
    media = _ENCONVO_MEDIA.get(os.path.splitext(path)[1].lower(),
                               "application/octet-stream")
    # Video needs ranges or iOS will not scrub, and Safari refuses to play
    # a source that answers a range request with the whole file.
    return FileResponse(path, media_type=media,
                        headers={"Accept-Ranges": "bytes",
                                 "Cache-Control": "private, max-age=600"})


@app.get("/api/enconvo/agents")
async def api_enconvo_agents():
    try:
        agents = await asyncio.to_thread(
            _enconvo_call, "agent/list", {}, 15)
    except Exception as error:
        raise HTTPException(
            503, f"EnConvo is not reachable - is it running? ({P.safe_error(error, 120)})")
    out = []
    for agent in agents if isinstance(agents, list) else []:
        out.append({
            "name": agent.get("name") or "",
            "title": agent.get("title") or agent.get("name") or "",
            "description": agent.get("description") or "",
            "portrait": bool(str(agent.get("icon") or "").startswith("file:")),
        })
    return {"agents": out}


@app.get("/api/enconvo/portrait")
async def api_enconvo_portrait(name: str = Query(...)):
    agents = await asyncio.to_thread(_enconvo_call, "agent/list", {}, 15)
    for agent in agents if isinstance(agents, list) else []:
        if agent.get("name") == name:
            icon = str(agent.get("icon") or "")
            if icon.startswith("file:"):
                path = icon[len("file:"):]
                base = os.path.expanduser("~/.enconvo/workspace")
                real = os.path.realpath(path)
                if real.startswith(os.path.realpath(base)) and os.path.isfile(real):
                    media = "image/png" if real.endswith(".png") else "image/jpeg"
                    with open(real, "rb") as handle:
                        return Response(handle.read(), media_type=media,
                                        headers={"Cache-Control": "max-age=3600"})
    raise HTTPException(404, "no portrait")


@app.post("/api/enconvo/photo")
async def api_enconvo_photo(photo: UploadFile = File(...)):
    """A phone photo, landed on the Mac so an EnConvo agent can actually
    see it (agents take context_files by path)."""
    uploads = os.path.join(reg().AVATARS, "..", "phone-uploads")
    os.makedirs(uploads, exist_ok=True)
    suffix = os.path.splitext(photo.filename or "photo.jpg")[1] or ".jpg"
    name = f"phone-{int(time.time()*1000)}{suffix}"
    destination = os.path.join(uploads, name)
    total = 0
    with open(destination, "wb") as handle:
        while True:
            chunk = await photo.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > 30 * 1024 * 1024:
                handle.close()
                os.unlink(destination)
                raise HTTPException(413, "photo too large")
            handle.write(chunk)
    return {"path": destination}


# The pocket app is a channel, and a channel talks to an agent the way
# EnConvo's own IM channels do (launch_channel.js): POST the agent's
# command route - /<extension>/<command>, no /api prefix - as an event
# stream, with the session and the invoke source, and let EnConvo run its
# whole flow. The agent picks its own tools, executes them itself, and
# narrates the result. Nothing here tells it HOW to do anything.
#
# The one parameter that matters is run_mode. Mavis's saved config carries
# run_mode "chat", which is a brain with no hands - through EVERY route,
# which is why agent/messages looked broken. "agent" is the mode where the
# tool belt is attached, and it is ours to ask for per request.
ENCONVO_HOST = "http://127.0.0.1:54535"
ENCONVO_API = f"{ENCONVO_HOST}/api"


def _enconvo_command_key(agent):
    """'main' and 'agent|main' and 'agent/main' all mean Mavis."""
    key = (agent or "main").strip().replace("/", "|")
    return key if "|" in key else f"agent|{key}"


def _enconvo_agent_run(agent, session, message, files=None, source="vivieen",
                       on_step=None, on_text=None):
    """One turn through EnConvo's real flow. Returns (final text, steps)."""
    import urllib.request
    extension, _, command = _enconvo_command_key(agent).partition("|")
    body = {
        "sessionId": session,
        "input_text": message,
        "invoke_source": source,
        "stream": True,
        # Verbose, the way a Telegram channel runs: the thread narrates each
        # step the agent takes instead of going quiet for three minutes.
        "im_verbose": True,
        # Hands on. Without this the agent answers from the model alone.
        "run_mode": "agent",
        "commandName": command,
        "extensionName": extension,
        "runType": "command",
        "environment": {"sessionId": session},
    }
    if files:
        body["context_files"] = list(files)
    request = urllib.request.Request(
        f"{ENCONVO_HOST}/{extension}/{command}", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    # An agent that fetches a video or renders an image works for minutes.
    with urllib.request.urlopen(request, timeout=900) as feed:
        return _enconvo_read_stream(feed, on_step, on_text)


def _enconvo_step_note(step):
    """EnConvo's own rule for what a verbose channel announces, mirrored
    from launch_channel.js: only a call that has actually started, never a
    hidden one, never the channel plumbing talking to itself. The label is
    the agent's description of what it is doing, in its words."""
    if step.get("type") != "flow_step" or step.get("hide") is True:
        return None
    if step.get("flowRunStatus") != "running":
        return None
    flow = str(step.get("flowName") or "").strip("/").replace("|", "/")
    params = step.get("flowParams")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    if not isinstance(params, dict):
        params = {}
    path = str(params.get("path") or "").strip("/").replace("|", "/") \
        if flow == "local_api" else ""
    # The channel's own tool calls are not news to the channel.
    if flow.startswith("im_channels") or path.startswith("im_channels"):
        return None
    for candidate in (params.get("description"), step.get("title"), path, flow):
        label = str(candidate or "").strip()
        if label:
            return {"key": f"{step.get('flowId') or ''}::{path or flow}",
                    "text": label, "tool": path or flow}
    return None


def _enconvo_read_stream(feed, on_step=None, on_text=None):
    """EnConvo streams the message as it is written: text arrives as deltas
    under 'append_...' actions, and every so often a frame carries the whole
    thing again. Rebuild per content id so either shape lands the same."""
    order, chunks, steps = [], {}, []
    announced = set()
    last_text_id = None
    for raw in feed:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            frame = json.loads(line[5:].strip())
        except Exception:
            continue
        action = frame.get("action") or ""
        for message in (frame.get("messages") or []):
            if message.get("role") != "assistant":
                continue
            for chunk in (message.get("content") or []):
                kind = chunk.get("type")
                if kind == "flow_step":
                    steps.append(chunk)
                    note = _enconvo_step_note(chunk)
                    if on_step and note and note["key"] not in announced:
                        announced.add(note["key"])
                        on_step(note)
                    continue
                if kind != "text":
                    continue
                text = chunk.get("text") or ""
                key = chunk.get("id") or last_text_id or "text"
                last_text_id = key
                if key not in chunks:
                    order.append(key)
                    chunks[key] = ""
                before = chunks[key]
                if action.startswith("append"):
                    chunks[key] += text
                elif len(text) >= len(chunks[key]):
                    # A whole-message frame supersedes what we assembled.
                    chunks[key] = text
                # Her sentence as she writes it, the way a channel sends the
                # reply in pieces instead of one silent blob at the end.
                if on_text and chunks[key] != before:
                    on_text(chunks[key][len(before):]
                            if chunks[key].startswith(before) else chunks[key])
    return "\n\n".join(chunks[key] for key in order if chunks[key]).strip(), steps


def _enconvo_step_files(steps):
    """What the agent chose to hand over. EnConvo agents deliver artifacts
    through delivery/present_files - the same call that drops a photo into a
    Telegram chat - and the tool tells them NOT to repeat the path in prose.
    So this is the only place the file is ever named, and reading it is what
    makes the pocket app a real channel rather than a transcript."""
    found, seen = [], set()

    def keep(path, title=""):
        if not path:
            return
        real = os.path.expanduser(str(path))
        if real.startswith("file://"):
            real = real[7:]
        if not os.path.isfile(real):
            return
        if real in seen:
            # The tool that wrote it reports a path; the delivery that
            # follows carries her name for it. Let the fuller name win.
            for item in found:
                if item["path"] == real and len(str(title)) > len(item["title"]):
                    item["title"] = str(title)
            return
        seen.add(real)
        found.append({"path": real, "title": str(title or "")})

    for step in steps:
        # A call is streamed argument by argument: mid-flight, a title of
        # "Cat astronaut in orbit" is just "C". Read the finished call.
        if step.get("flowRunStatus") not in ("success", "error", None):
            continue
        params = step.get("flowParams")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        if not isinstance(params, dict):
            params = {}
        inner = params.get("params") if isinstance(
            params.get("params"), dict) else {}
        if str(params.get("path") or "").strip("/") == "delivery/present_files":
            for item in (inner.get("deliverables") or []):
                if isinstance(item, dict):
                    keep(item.get("url") or item.get("path"),
                         item.get("title"))
        # A tool that reports what it wrote, whether or not she delivered it.
        output = step.get("output")
        if isinstance(output, dict):
            for path in (output.get("paths") or []):
                keep(path)
    return found


def _viv_note(line):
    """Engine stdout is swallowed by the shell; leave a readable trail."""
    try:
        with open(os.path.join(tempfile.gettempdir(), "vivieen-lane.log"),
                  "a") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except Exception:
        pass


def _phone_playable(path):
    """WebKit will not play AV1 or Opus - the small streams a downloader
    prefers - and renders them as a crossed-out play button. Hand back
    something the phone can actually open, transcoding only if it must."""
    ffmpeg = shutil.which("ffmpeg")
    probe = shutil.which("ffprobe")
    if not ffmpeg or not path.lower().endswith(
            (".mp4", ".m4v", ".mov", ".webm", ".mkv")):
        return path
    found = []
    try:
        # stdin=DEVNULL is not optional: ffmpeg and ffprobe read stdin, and
        # the engine's stdin is a pipe the shell never closes, so the probe
        # blocked forever and the card shipped unplayable - which read as a
        # renderer bug for an hour. If it still will not answer, re-encode
        # rather than guess.
        if probe:
            found = subprocess.run(
                [probe, "-v", "error", "-show_entries", "stream=codec_name",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=180,
                stdin=subprocess.DEVNULL).stdout.split()
    except Exception as error:
        _viv_note(f"ffprobe gave up on {os.path.basename(path)}: "
                  f"{P.safe_error(error, 120)} - re-encoding to be safe")
    if found and all(name in ("h264", "aac", "mp3") for name in found) \
            and path.lower().endswith((".mp4", ".m4v", ".mov")):
        return path
    safe = os.path.splitext(path)[0] + ".phone.mp4"
    if os.path.isfile(safe):
        return safe
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-y", "-i", path, "-c:v", "h264_videotoolbox",
             "-b:v", "2M", "-c:a", "aac", "-movflags", "+faststart", safe],
            capture_output=True, timeout=1800, check=True,
            stdin=subprocess.DEVNULL)
    except Exception as error:
        # A silent fallback here ships an unplayable card and looks like a
        # renderer bug; say what went wrong where it can be read.
        detail = getattr(error, "stderr", b"") or b""
        print("[viv] transcode failed:", P.safe_error(error, 160),
              detail[-400:].decode("utf-8", "replace"), flush=True)
        return path
    return safe if os.path.isfile(safe) else path


class EnconvoChat(BaseModel):
    agent: str
    message: str
    session_id: str = ""
    context_files: list = []


@app.post("/api/enconvo/chat")
async def api_enconvo_chat(request: EnconvoChat):
    """An agent turn, streamed. A Telegram channel shows "typing" and then
    narrates each step; the pocket thread does the same, so a three-minute
    job reads as work in progress rather than a frozen app."""
    uploads = os.path.realpath(os.path.join(reg().AVATARS, "..", "phone-uploads"))
    safe_files = [path for path in (request.context_files or [])
                  if isinstance(path, str)
                  and os.path.realpath(path).startswith(uploads)
                  and os.path.isfile(path)]

    # The agent sees where it is answering from, the way an IM channel
    # names itself - Telegram's handler passes "telegram-<chat>".
    source = "vivieen-pocket"
    events = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(payload):
        loop.call_soon_threadsafe(events.put_nowait, payload)

    def run():
        key = _enconvo_command_key(request.agent)
        session = request.session_id
        if not session:
            fresh = _enconvo_call(
                "agent/session/new",
                {"agentId": key, "invokeSource": source}, 30)
            session = fresh.get("sessionId") or fresh.get("id") or ""
        text, steps = _enconvo_agent_run(
            key, session, request.message, safe_files, source,
            on_step=lambda note: emit({"type": "step", **note}),
            on_text=lambda piece: emit({"type": "say", "text": piece}))
        return session, text, _enconvo_step_files(steps)

    async def feed():
        turn = asyncio.create_task(asyncio.to_thread(run))
        try:
            yield _sse({"type": "typing"})
            while True:
                drain = asyncio.create_task(events.get())
                done, _ = await asyncio.wait(
                    {drain, turn}, return_when=asyncio.FIRST_COMPLETED,
                    timeout=20)
                if drain in done:
                    yield _sse(drain.result())
                    continue
                drain.cancel()
                if turn in done:
                    break
                # Nothing to report and still working: keep the pipe warm so
                # a long job never looks like a dropped connection.
                yield _sse({"type": "typing"})
            while not events.empty():
                yield _sse(events.get_nowait())
            try:
                payload = await _enconvo_finish(*turn.result())
            except Exception as error:
                payload = {"type": "error",
                           "detail": f"EnConvo did not answer "
                                     f"({P.safe_error(error, 160)})"}
            yield _sse(payload)
        finally:
            # /stop, or the phone simply walked away: do not leave a turn
            # running against an audience that has gone home.
            turn.cancel()

    return StreamingResponse(feed(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})


def _sse(payload):
    return f"data: {json.dumps(payload)}\n\n"


async def _enconvo_finish(session, text, made):
    if not text:
        text = "…the agent finished without saying anything."
    shown, cards = _enconvo_media(text)
    # What she delivered is a card - and it carries the name SHE gave it,
    # not a uuid off the disk.
    seen = {card["url"] for card in cards}
    for item in made:
        url = await asyncio.to_thread(_enconvo_share, item["path"])
        if url and url not in seen:
            seen.add(url)
            cards.append({"url": url,
                          "name": item["title"] or os.path.basename(item["path"])})
    # She SAYS the prose, not the URLs - a spoken file path is noise.
    spoken = await _say(_PATH_IN_TEXT.sub("that file", text), P.load())
    return {"type": "done", "session_id": session, "text": shown,
            "media": cards, "audio": spoken.get("audio", ""),
            "track": spoken.get("track", [])}


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


# The pet page addressed to ONE avatar rather than "the active one". The page
# is served under /c/<slug>/ so its relative "assets/..." references resolve
# to the per-slug asset route below - the renderer needs no URL changes. This
# is how the second on-desk avatar window renders its own runtime while the
# main window keeps following the active avatar.
@app.get("/c/{slug}/")
async def companion_page(slug: str):
    if not re.fullmatch(SLUG_PATTERN, slug):
        raise HTTPException(404, "not found")
    if not os.path.isfile(os.path.join(runtime_dir(slug), "manifest.json")):
        raise HTTPException(404, "this avatar has no runtime bundle")
    return HTMLResponse(open(os.path.join(WEB, "index.html")).read(),
                        headers={"Cache-Control": "no-store"})


@app.get("/c/{slug}/assets/{path:path}")
async def companion_assets(slug: str, path: str):
    if not re.fullmatch(SLUG_PATTERN, slug):
        raise HTTPException(404, "not found")
    full = _safe_file(runtime_dir(slug), path)
    if not full:
        raise HTTPException(404, "not found")
    return FileResponse(full, headers={"Cache-Control": "no-store"})


@app.get("/bubble")
async def bubble():
    return HTMLResponse(open(os.path.join(WEB, "bubble.html")).read(),
                        headers={"Cache-Control": "no-store"})


@app.get("/menu")
async def pet_menu():
    return HTMLResponse(open(os.path.join(WEB, "menu.html")).read(),
                        headers={"Cache-Control": "no-store"})


@app.get("/appearance")
async def appearance():
    return HTMLResponse(open(os.path.join(WEB, "appearance.html")).read(),
                        headers={"Cache-Control": "no-store"})


@app.get("/settings")
async def settings():
    return HTMLResponse(open(os.path.join(WEB, "settings.html")).read(),
                        headers={"Cache-Control": "no-store"})
