"""Viseme rendering through the Enconvo image_create CLI.

The CLI mirrors the local API 1:1, so the pipeline can generate a whole set on
its own - a user drops in a photo and gets a finished viseme bank without any
agent in the loop.
"""
import os, json, hashlib, shutil, subprocess, time
from concurrent.futures import ThreadPoolExecutor
from . import visemes

ENCONVO = shutil.which("enconvo") or os.path.expanduser("~/.config/enconvo/bin/enconvo")
MAX_WORKERS = 4
RETRIES = 2


def _find_output(out_dir, name, before, stdout=""):
    """Locate the render.  The CLI reports {"paths": [...]}; fall back to the
    expected filename and then to whatever is new in the directory."""
    try:
        payload = json.loads((stdout or "").strip() or "{}")
        for q in payload.get("paths") or []:
            if os.path.exists(q):
                return q
    except Exception:
        pass
    for _ in range(3):                      # the writer can lag the process exit
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            q = os.path.join(out_dir, f"v_{name}{ext}")
            if os.path.exists(q) and os.path.getsize(q) > 4096:
                return q
        time.sleep(1.0)
    new = [f for f in os.listdir(out_dir) if f not in before
           and f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    if new:
        newest = max(new, key=lambda f: os.path.getmtime(os.path.join(out_dir, f)))
        dst = os.path.join(out_dir, f"v_{name}.png")
        os.replace(os.path.join(out_dir, newest), dst)
        return dst
    return None


def generate_one(keyframe, name, out_dir, yaw=None, roll=None,
                 model="gpt-image-2", quality="high", credentials="globaldefault",
                 timeout=1800, log=print, overwrite=False):
    os.makedirs(out_dir, exist_ok=True)
    prompt = visemes.prompt_for(name, yaw, roll)
    sig = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
    sig_file = os.path.join(out_dir, f".{name}.prompt")

    # A cached render is only valid for the prompt that produced it.  Keying the
    # cache on filename alone let an edited prompt silently reuse a stale frame,
    # which is exactly how a calibration pass can appear to do nothing.
    if not overwrite:
        cached = None
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            q = os.path.join(out_dir, f"v_{name}{ext}")
            if os.path.exists(q) and os.path.getsize(q) > 4096:
                cached = q
                break
        if cached:
            have = ""
            if os.path.exists(sig_file):
                with open(sig_file) as fh:
                    have = fh.read().strip()
            if have == sig:
                log(f"  {name:7s} reusing existing render")
                return cached
            log(f"  {name:7s} prompt changed - re-rendering")
    last = ""
    for attempt in range(1, RETRIES + 2):
        before = set(os.listdir(out_dir))
        cmd = [ENCONVO, "image_create", "features", "open_ai/create",
               "--prompt", prompt,
               "--reference_images", keyframe,
               "--mode", "edit",
               "--input_fidelity", "high",
               "--size", "1024x1024",
               "--quality", quality,
               "--model", model,
               "--credentials", credentials,
               "--output_format", "png",
               "--output_dir", out_dir,
               "--file_name", f"v_{name}"]
        rc, so = None, ""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            rc, so = r.returncode, (r.stdout or "")
            last = f"rc={rc} " + so + (r.stderr or "")
        except subprocess.TimeoutExpired:
            last = f"timed out after {timeout}s"
        except Exception as ex:
            last = f"{type(ex).__name__}: {ex}"
        p = _find_output(out_dir, name, before, so)
        if p:
            with open(sig_file, "w") as fh:
                fh.write(sig)
            return p
        log(f"  {name}: attempt {attempt} failed ({last.strip()[-220:] or 'no output file'})")
        time.sleep(2 * attempt)
    return None


def generate_set(keyframe, out_dir, yaw=None, roll=None, names=None,
                 workers=MAX_WORKERS, log=print, on_done=None, **kw):
    names = names or visemes.ORDER
    os.makedirs(out_dir, exist_ok=True)
    results = {}

    def task(n):
        t0 = time.time()
        p = generate_one(keyframe, n, out_dir, yaw, roll, log=log, **kw)
        log(f"  {n:7s} {'rendered' if p else 'FAILED  '} in {time.time()-t0:5.1f}s")
        results[n] = p
        if on_done:
            on_done(n, p)
        return p

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(task, names))
    return results
