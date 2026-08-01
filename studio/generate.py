"""Viseme rendering through the Enconvo image_create CLI.

The CLI mirrors the local API 1:1, so the pipeline can generate a whole set on
its own - a user drops in a photo and gets a finished viseme bank without any
agent in the loop.
"""
import os, json, hashlib, shutil, subprocess, tempfile, time
from concurrent.futures import ThreadPoolExecutor

import cv2

from . import visemes

ENCONVO = shutil.which("enconvo") or os.path.expanduser("~/.config/enconvo/bin/enconvo")
MAX_WORKERS = 4
RETRIES = 2
HEAD_PROMPT_VERSION = 3
HEAD_PROMPT = """Create an ultra-high-definition square identity head reference from the supplied photo.

IDENTITY — preserve the exact same adult person's facial identity, skull and facial proportions, skin tone and texture, apparent age, hairline, hairstyle, eyebrows, eye shape and color, nose, lips, ears, and distinctive natural features. Do not beautify, de-age, stylize, or redesign the person.

EYEWEAR — if the supplied photo shows the person wearing eyeglasses, those glasses are part of their identity. Keep the exact same pair on the face: same frame shape, rim style, frame thickness, frame color and material, temple arms, lens shape and any lens tint, sitting at the same position on the nose and ears. Never remove them, never swap them for a different pair, and never render an unglassed version of this person. If the supplied photo shows no eyeglasses, do not add any.

FRAMING — show only the complete head and hair, centered and fully visible, with at most a very small neutral upper-neck transition below the jaw. No shoulders, collarbones, chest, torso, arms, or hands. No clothing of any kind, jewelry, earrings, hats, headwear, headphones, other accessories, props, or text anywhere in the image — eyeglasses already worn in the supplied photo are the single exception and must be kept exactly as described above. Do not crop the hair, chin, jaw, or ears.

POSE — face the camera straight on with an upright head, eyes naturally open, and a neutral closed mouth. Preserve realistic asymmetry. Use even soft studio light and a plain neutral background with clean separation around every hair edge.

This is a reusable identity asset for facial animation and later full-body image editing, not a fashion portrait or profile photograph."""


def _file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _find_output(out_dir, name, before, stdout="", prefix="v_"):
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
            q = os.path.join(out_dir, f"{prefix}{name}{ext}")
            if os.path.exists(q) and os.path.getsize(q) > 4096:
                return q
        time.sleep(1.0)
    new = [f for f in os.listdir(out_dir) if f not in before
           and f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    if new:
        newest = max(new, key=lambda f: os.path.getmtime(os.path.join(out_dir, f)))
        dst = os.path.join(out_dir, f"{prefix}{name}.png")
        os.replace(os.path.join(out_dir, newest), dst)
        return dst
    return None


def default_head_provider():
    from . import body
    return body.default_provider()


def _head_command(provider, reference, out_dir, quality, prompt=None):
    route = provider["route"]
    command = [
        ENCONVO, "image_create", "features", route,
        "--prompt", prompt or HEAD_PROMPT,
        "--reference_images", reference,
        "--output_dir", out_dir,
        "--file_name", "head",
        "--download",
    ]
    if route == "gemini/create":
        image_size = "1K" if "flash-lite" in str(provider.get("model", "")) else "2K"
        command += ["--mode", "edit", "--aspectRatio", "1:1", "--imageSize", image_size]
    elif route == "open_ai/create":
        command += [
            "--mode", "edit", "--size", "1024x1024", "--quality", quality,
            "--background", "opaque", "--input_fidelity", "high",
        ]
    elif route == "x_ai/create":
        command += ["--aspect_ratio", "1:1", "--resolution", "2k"]
    elif route == "kie_ai/create":
        command += ["--mode", "edit", "--aspect_ratio", "1:1", "--resolution", "2k"]
    elif route == "azure/create":
        command += ["--mode", "edit", "--size", "1024x1024"]
    elif route in {"together/create", "straico/create"}:
        command += ["--mode", "edit"]
    return command


def generate_head(reference, destination, provider=None, quality="high",
                  timeout=1800, log=print, overwrite=False, pose_note=""):
    """Create and cache the canonical head-only identity asset used downstream.

    pose_note carries a measured correction ("previous attempt: yaw -9.1deg
    ...") appended to the prompt when a frontality retry runs - tilted
    source selfies (rachel, 2026-08-01: pitch 23, roll 18, foreshortening
    0.56) otherwise keep their tilt and degrade every mouth stage after."""
    provider = provider or default_head_provider()
    prompt = HEAD_PROMPT + pose_note
    signature = hashlib.sha256((
        f"v{HEAD_PROMPT_VERSION}\n{provider['name']}\n{provider.get('model')}\n"
        f"{quality}\n{prompt}\n" + _file_digest(reference)
    ).encode("utf-8")).hexdigest()
    signature_file = destination + ".prompt"
    if not overwrite and os.path.isfile(destination) and os.path.getsize(destination) > 4096:
        try:
            with open(signature_file) as handle:
                if handle.read().strip() == signature:
                    log("reusing canonical HD head")
                    return destination
        except OSError:
            pass

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".head-provider-", dir=os.path.dirname(destination))
    last_error = ""
    try:
        for attempt in range(1, RETRIES + 2):
            before = set(os.listdir(stage))
            stdout = ""
            try:
                result = subprocess.run(
                    _head_command(provider, reference, stage, quality,
                                  prompt=prompt),
                    capture_output=True, text=True, timeout=timeout,
                    stdin=subprocess.DEVNULL)
                stdout = result.stdout or ""
                last_error = (result.stderr or stdout or
                              f"provider exited with status {result.returncode}").strip()
                if result.returncode:
                    raise RuntimeError(last_error)
            except subprocess.TimeoutExpired:
                last_error = f"timed out after {timeout}s"
            except Exception as error:
                last_error = str(error)

            rendered = _find_output(stage, "head", before, stdout, prefix="")
            image = cv2.imread(rendered, cv2.IMREAD_COLOR) if rendered else None
            if image is not None and min(image.shape[:2]) >= 512:
                temporary = destination + ".tmp.png"
                if not cv2.imwrite(temporary, image, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
                    raise RuntimeError("could not save the canonical HD head")
                os.replace(temporary, destination)
                with open(signature_file, "w") as handle:
                    handle.write(signature)
                return destination
            log(f"  head: attempt {attempt} failed ({last_error[-220:] or 'no usable output image'})")
            time.sleep(2 * attempt)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    raise RuntimeError(
        "could not create the canonical HD head" +
        (f": {last_error[-500:]}" if last_error else ""))


def generate_one(keyframe, name, out_dir, yaw=None, roll=None,
                 model="gpt-image-2", quality="high", credentials="globaldefault",
                 timeout=1800, log=print, overwrite=False):
    os.makedirs(out_dir, exist_ok=True)
    prompt = visemes.prompt_for(name, yaw, roll)
    sig = hashlib.sha1(
        (prompt + "\n" + _file_digest(keyframe)).encode("utf-8")
    ).hexdigest()[:12]
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
