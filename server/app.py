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
import os, sys, io, json, base64, tempfile, threading, time, shutil, subprocess, secrets
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
_jobs = {}                      # slug -> {"log": [...], "phase": str, "done": bool}
_jlock = threading.Lock()


def reg():
    from studio import build as _r
    return _r


# ---------------------------------------------------------------- avatars

def runtime_dir(slug):
    return os.path.join(reg().adir(slug), "runtime")


def active_slug():
    try:
        return reg().get_active()
    except Exception:
        return None


def ensure_runtime(slug, log=print):
    """An avatar is only usable once it has a runtime bundle. Build one on demand
    rather than at activation time, so importing an old avatar folder works."""
    d = runtime_dir(slug)
    if os.path.exists(os.path.join(d, "manifest.json")):
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


@app.get("/api/avatars")
async def api_avatars():
    r = reg()
    out = []
    for a in r.list_avatars():
        s = a.get("slug")
        a["has_runtime"] = os.path.exists(os.path.join(runtime_dir(s), "manifest.json"))
        with _jlock:
            j = _jobs.get(s)
        a["job"] = {"phase": j["phase"], "done": j["done"], "error": j["error"]} if j else None
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


@app.get("/settings")
async def settings():
    return HTMLResponse(open(os.path.join(WEB, "settings.html")).read(),
                        headers={"Cache-Control": "no-store"})
