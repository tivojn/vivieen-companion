"""Provider-aware full-body authoring with deterministic face locking.

The image provider designs the body, wardrobe, hair silhouette, and stance. It
is not trusted with identity at runtime: the existing calibrated 1024px face rig
is mapped over the generated head through a robust similarity transform.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# The server package lives beside this one; the direct xAI path reads the
# owner's own key from it.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

from . import cutout, face


CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENCONVO = os.environ.get(
    "ENCONVO_CLI",
    os.path.expanduser("~/.config/enconvo/bin/enconvo"),
)
PROVIDER_ROUTES = {
    "gemini": "gemini/create",
    "gemini-enconvo": "gemini/create",
    "open_ai": "open_ai/create",
    "open_ai-enconvo": "open_ai/create",
    "x_ai": "x_ai/create",
    "kie_ai": "kie_ai/create",
    "azure": "azure/create",
    "together": "together/create",
    "straico": "straico/create",
}
STYLES = {"photorealistic", "editorial", "illustrated", "anime", "soft-3d"}
POSES = {"relaxed", "confident", "friendly", "formal", "casual"}
BODY_VIEWS = ("front", "side", "back")
DEFAULT_BODY_PROMPT = (
    "Create a photorealistic full-body wardrobe at couture level - the poise "
    "of a front-row fashion client, styled with editorial discipline. Build "
    "the palette as a hierarchy: one luminous hero colour (rich, refined, "
    "never neon), one supporting accent such as champagne metal or a tonal "
    "step of the hero, and quiet neutral foundations like ivory, soft taupe, "
    "or charcoal, all named explicitly. Dress the subject in one precisely "
    "tailored, opaque hero garment with sculpted structure: a defined waist, "
    "a clean modern neckline - asymmetric or architectural - and a "
    "streamlined line that follows the figure without restricting movement. "
    "Use a substantial fabric with real behaviour - matte crepe with subtle "
    "stretch, silk mikado, double-faced wool, or fine gabardine - with crisp "
    "internal structure so the silhouette reads smooth and refined from "
    "shoulder to hem. Add exactly ONE statement detail, such as a slim "
    "sculptural metal waist clasp or a single architectural seam, and keep "
    "jewellery restrained to small matching stud earrings and a delicate "
    "ring. Finish with pointed leather pumps in the spirit of a classic "
    "100mm dorsay stiletto - an unmistakable killer heel, immaculately "
    "polished. Maintain flawless seams, realistic fabric tension, "
    "understated luxury, and confident photographic polish. The outfit must "
    "be opaque, properly fitted, and appropriate for public or professional "
    "wear: no nudity, lingerie, sheer fabric, bare midriff, or extreme "
    "neckline. The subject carries nothing at all: both hands stay empty, "
    "with no bag, handbag, clutch, purse, tote, backpack, briefcase, phone, "
    "cup, or umbrella held in either hand and nothing slung over a shoulder "
    "or arm. Preserve the person's identity, any eyeglasses worn in the "
    "reference, groomed hair, natural proportions, real skin texture, and "
    "apparent age."
)


def _clean(value, maximum=800):
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()[:maximum]


def _direction(options):
    """The generation direction, plus whatever the owner asked to keep.

    Notes used to be DROPPED the moment an expanded prompt existed - which
    is the normal path - so "keep his bandana" never reached the model. A
    portrait of a character came back with the right face and none of what
    made him recognisable (owner, 2026-08-04). The note is an ADD-ON: it
    rides after the prompt, never instead of it, and it goes last so it
    reads as the final word.
    """
    notes = _clean(options.get("notes"), 600)
    custom = _clean(options.get("prompt"), 2400)
    if custom:
        return f"{custom} MUST KEEP: {notes}" if notes else custom
    legacy = []
    outfit = _clean(options.get("outfit"), 500)
    if outfit:
        legacy.append(f"Wardrobe: {outfit}")
    if notes:
        legacy.append(f"MUST KEEP: {notes}")
    base = " ".join(legacy)
    if not base:
        return DEFAULT_BODY_PROMPT
    if not _clean(options.get("outfit"), 500):
        # A note alone still wants the house prompt behind it.
        return f"{DEFAULT_BODY_PROMPT} {base}"
    return base


def _enconvo_config(preference_key, includes):
    result = subprocess.run(
        [ENCONVO, "config", "get", preference_key, "--includes", *includes],
        capture_output=True,
        text=True,
        timeout=45,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "provider lookup failed").strip())
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("EnConvo returned invalid provider metadata")
    return payload


def selected_provider(category):
    selection = _enconvo_config(category, ["selected"])
    command_key = str(selection.get("selected") or "")
    prefix = category + "|"
    if not command_key.startswith(prefix) or command_key == prefix:
        label = category.replace("_", "-")
        raise RuntimeError(f"EnConvo has no selected {label} provider")
    metadata = _enconvo_config(
        command_key, ["title", "modelName", "description"])
    name = command_key[len(prefix):]
    return {
        "name": name,
        "command_key": command_key,
        "title": metadata.get("title") or name,
        "description": metadata.get("description") or "",
        "model": metadata.get("modelName") or None,
    }


def default_provider():
    # Chosen xAI Grok Image in THIS app's settings? Then the plates are
    # ours to make: our provider, our key, straight to xAI. Anything else
    # - including "EnConvo Global Default" - resolves through EnConvo
    # exactly as it always has.
    own = _own_config().get("image") or {}
    if (own.get("provider") or "") == "xai" and _xai_key():
        return {"name": "x_ai", "title": "xAI Grok Image",
                "model": own.get("model") or "grok-imagine-image-quality",
                "route": "x_ai/create", "direct": True}
    provider = selected_provider("image_create")
    name = provider["name"]
    route = PROVIDER_ROUTES.get(name)
    if not route:
        raise RuntimeError(f"EnConvo's default image provider is not editable here yet: {name}")
    return {**provider, "route": route}


def default_video_provider():
    return selected_provider("video_create")


def _prompt(options, view="front"):
    if view not in BODY_VIEWS:
        raise ValueError(f"unknown full-body view: {view}")
    style = options.get("style") if options.get("style") in STYLES else "photorealistic"
    pose = options.get("pose") if options.get("pose") in POSES else "relaxed"
    direction = _direction(options)
    style_text = {
        "photorealistic": "Photorealistic editorial portrait photography with natural skin texture",
        "editorial": "High-fashion editorial portrait photography with restrained luxury styling",
        "illustrated": "Polished modern character illustration with sophisticated material rendering",
        "anime": "Premium contemporary anime character art with anatomically coherent proportions",
        "soft-3d": "Refined soft-3D character rendering with realistic materials and restrained stylization",
    }[style]
    pose_text = {
        "relaxed": "balanced relaxed stance, arms naturally separated from the torso",
        "confident": "calm confident stance with excellent posture and relaxed shoulders",
        "friendly": "warm approachable stance with subtle asymmetry and relaxed hands",
        "formal": "formal composed stance, shoulders level, hands naturally at the sides",
        "casual": "natural casual weight shift with hands clearly visible",
    }[pose]
    view_text = {
        "front": (
            "Create the canonical FRONT view. Face, sternum, pelvis, knees, and toes point "
            "straight toward the camera. Keep both shoulders and both sides of the outfit "
            "equally readable; do not rotate into a three-quarter view. Reference 1, the "
            "canonical HD head, is the identity authority."
        ),
        "side": (
            "Create the canonical RIGHT-SIDE view. The nose, chest, knees, and toes point "
            "exactly camera-right in a true 90-degree profile; do not drift toward front or "
            "three-quarter. Reference 1, the canonical HD head, is the identity authority. "
            "Reference 2, the approved front body plate, is the absolute authority for "
            "wardrobe, body proportions, materials, color, accessories, and garment length."
        ),
        "back": (
            "Create the canonical BACK view. The back of the head, shoulders, spine, hips, "
            "knees, and heels face the camera while the face remains completely out of view; "
            "do not turn the head over a shoulder. Reference 1, the canonical HD head, is the "
            "identity and hair authority. Reference 2, the approved front body plate, is the "
            "absolute authority for wardrobe, body proportions, materials, color, "
            "accessories, and garment length."
        ),
    }[view]
    return f"""Create one vertical 3:4 full-body {view}-view character plate of the exact same adult person.

TURNAROUND CONTRACT — this is one member of a matched FRONT / RIGHT-SIDE / BACK full-body set. Return exactly one complete figure for this {view} plate, never a triptych, contact sheet, split screen, duplicate person, inset, or labeled diagram. Treat the camera as rotating around one stationary person: preserve the same posture, shoulder level, arm placement, hand state, leg spacing, weight distribution, outfit, body scale, and camera height across all three plates.

IDENTITY LOCK — preserve the reference person's facial identity, skull proportions, skin tone, hairline, hairstyle, eyebrows, eye shape and color, nose, lips, ears, and apparent age wherever those features are visible. If the reference head wears eyeglasses, keep that exact pair on the face in every plate — same frame shape, thickness, color and position — and never remove them; if the reference wears none, do not add any. Keep a neutral closed mouth. Do not beautify, de-age, or redesign the person.

VIEW — {view_text}

COMPOSITION — show the complete figure from the top of the hair through both feet with 7% clear margin around the silhouette. Camera at waist height, long portrait lens, minimal perspective distortion. Use a {pose_text}. Both hands, both legs, and all footwear must be complete and anatomically correct; no crop, no props, no furniture, no text.

CARRY NOTHING — both hands are completely empty and clearly visible. Do NOT place a bag, handbag, clutch, purse, tote, shopping bag, backpack, briefcase, portfolio, folder, book, paper, phone, cup, glass, umbrella, weapon, staff, or any other object in either hand, and do NOT sling a bag, strap, or pouch over a shoulder, hook one on an elbow, or wear one across the body. Nothing is held, carried, hooked, or leaned against the figure in any plate of the turnaround.

EDITABLE ART DIRECTION — {direction}

DECENCY FLOOR — regardless of the editable direction, use tasteful opaque clothing suitable for an adult in public. No nudity, lingerie, transparent fabric, exposed intimate areas, or sexually provocative styling. The result must read as proper, decent, and intentionally fashionable.

STYLE — {style_text}. Match the reference head's lighting direction, color temperature, realism, and photographic texture. Avoid airbrushed skin, plastic fabric, exaggerated anatomy, or game-interface styling.

NO GREEN — ban the color green everywhere in the image: no green clothing, garment parts, or accessories, no green props or jewelry stones, no green background, backdrop tint, or green cast in the lighting. If the editable art direction asks for green, substitute a different color and keep everything else of that direction. Downstream alpha keying misreads green as background, so any green in the plate corrupts the cutout.

NO WHITE WARDROBE — ban white and off-white in everything worn: no white or off-white tops, shirts, dresses, trousers, skirts, jackets, or outerwear, and absolutely no white shoes, sneakers, heels, or soles. The figure is cut out from a light studio backdrop, and white wardrobe dissolves into it and shreds the silhouette. If the editable art direction asks for white, substitute a clearly non-white, non-green color and keep everything else of that direction.

BACKGROUND — simple clean studio backdrop with strong person/background separation, never green or green-tinted. The application will remove the background locally, so preserve fine hair edges and do not add smoke, veils, loose particles, or cast shadows behind the figure."""


def _provider_command(
        provider, keyframe, output_dir, prompt, file_name="body-source"):
    route = provider["route"]
    references = (
        [os.fspath(reference) for reference in keyframe]
        if isinstance(keyframe, (list, tuple)) else
        [os.fspath(keyframe)]
    )
    command = [
        ENCONVO, "image_create", "features", route,
        "--prompt", prompt,
        "--reference_images", *references,
        "--output_dir", output_dir,
        "--file_name", file_name,
        "--download",
    ]
    if route == "gemini/create":
        image_size = "1K" if "flash-lite" in str(provider.get("model", "")) else "2K"
        command += ["--mode", "edit", "--aspectRatio", "3:4", "--imageSize", image_size]
    elif route == "open_ai/create":
        command += [
            "--mode", "edit", "--size", "1024x1536", "--quality", "high",
            "--background", "opaque", "--input_fidelity", "high",
        ]
    elif route == "x_ai/create":
        command += ["--aspect_ratio", "3:4", "--resolution", "2k"]
    elif route == "kie_ai/create":
        command += ["--mode", "edit", "--aspect_ratio", "3:4", "--resolution", "2k"]
    elif route == "azure/create":
        command += ["--mode", "edit", "--size", "1024x1792"]
    elif route in {"together/create", "straico/create"}:
        command += ["--mode", "edit"]
    return command


def _own_config():
    """This app's own settings, read straight off disk.

    Deliberately NOT through providers.load(): that unlocks the vault and
    shells out for EnConvo's defaults, which is both slower than reading
    a file and a side effect no caller here wants. EnConvo's own settings
    are never read.
    """
    # The SAME file providers.CONFIG resolves, by the same rules. This used
    # to fall back to ~/Library/Application Support/Vivieen/config.json,
    # which is not where the config lives - so anywhere VIVIEEN_CONFIG was
    # not exported this read {} and every "did the owner choose xAI?" test
    # quietly answered no (owner, 2026-08-04).
    root = os.path.abspath(os.environ.get(
        "VIVIEEN_DATA_DIR",
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    path = os.environ.get("VIVIEEN_CONFIG") or os.path.join(root, "config.json")
    try:
        with open(path) as handle:
            return json.load(handle) or {}
    except Exception:
        return {}


def _xai_key():
    """An xAI key for a direct image edit - only when the OWNER chose xAI.

    Picking "EnConvo Global Default" must mean exactly that: EnConvo's
    route, untouched, whatever it does. The direct path belongs to the
    explicit "xAI Grok Image" choice, which is this app's own provider
    with this app's own key (owner, 2026-08-04).
    """
    cfg = _own_config()
    image = cfg.get("image") or {}
    if (image.get("provider") or "") != "xai":
        return ""
    for value in (image.get("api_key"),
                  (cfg.get("live") or {}).get("xai_api_key")):
        value = (value or "").strip()
        # The file keeps markers, not secrets; a marker means "ask the
        # vault", which is the one case worth the heavier import.
        if value.startswith("keychain:") or value == "__vault__":
            try:
                sys.path.insert(0, os.path.join(_ROOT, "server"))
                import providers as _P
                real = _P.load()
                value = ((real.get("image") or {}).get("api_key")
                         or (real.get("live") or {}).get("xai_api_key")
                         or "").strip()
            except Exception:
                value = ""
        if value:
            return value
    return ""


def _xai_edit(prompt, references, output_dir, file_name, key,
              aspect_ratio="3:4"):
    """One image-to-image edit, straight to xAI.

    EnConvo's x_ai route sends `n` on every call, and xAI answers edits
    carrying it with "n is only supported for image generation" - so every
    body and head plate failed the moment the owner chose Grok Imagine
    (owner, 2026-08-04). Reproduced with EnConvo's own CLI at its bare
    minimum, so it is not something we pass. EnConvo is left exactly as it
    is; we simply make this one call ourselves, and xAI accepts it:
    image must be an OBJECT with a url, and there is no n.

    Returns the written path, or raises. Callers fall back to the CLI when
    no key is available, so removing this function restores the old path.
    """
    import base64
    import mimetypes
    import urllib.request

    # SAY SO. EnConvo reaches xAI over OAuth, which rides the owner's
    # subscription; this path uses their xAI API KEY, which is metered
    # per image. Same pictures, different bill - nobody should discover
    # that from an invoice (owner asked, 2026-08-04).
    print("  xai: direct edit via your xAI API key (metered) - EnConvo's "
          "OAuth route cannot edit", flush=True)

    first = references[0]
    mime = mimetypes.guess_type(first)[0] or "image/png"
    with open(first, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    body = {
        "model": "grok-imagine-image-quality",
        "prompt": prompt,
        "image": {"url": f"data:{mime};base64,{encoded}"},
    }
    # Measured: an edit DOES accept aspect_ratio, so the plates keep their
    # shape instead of coming back square. (resolution does not belong
    # here - the API takes it only for generation.)
    if aspect_ratio:
        body["aspect_ratio"] = aspect_ratio
    payload_bytes = json.dumps(body).encode()
    # A reference photo is megabytes once base64'd, and this uplink drops
    # mid-POST often enough to lose a whole build ("SSL: UNEXPECTED_EOF").
    # A dropped connection is not an answer - ask again before believing
    # the provider refused (owner: might be network flaky, 2026-08-04).
    answer = None
    last = None
    for attempt in range(3):
        request = urllib.request.Request(
            "https://api.x.ai/v1/images/edits",
            data=payload_bytes,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=600) as feed:
                answer = json.loads(feed.read().decode())
            break
        except urllib.error.HTTPError as refusal:
            # A real refusal; do not hammer it - but xAI puts the REASON in
            # the response body, and re-raising bare threw it away. "HTTP
            # Error 400: Bad Request" is not something anyone can act on
            # (owner's move build, 2026-08-04).
            try:
                detail = refusal.read().decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            raise RuntimeError(
                f"xAI refused the edit ({refusal.code}): "
                f"{detail[:400] or refusal.reason}") from refusal
        except Exception as error:     # dropped socket, DNS, timeout
            last = error
            if attempt == 2:
                raise RuntimeError(
                    f"xAI never received the image ({error}) - the link "
                    "dropped three times") from error
            time.sleep(2 * (attempt + 1))
    rows = answer.get("data") or []
    url = (rows[0] or {}).get("url") if rows else ""
    if not url:
        raise RuntimeError("xAI returned no image for this edit")
    destination = os.path.join(output_dir, file_name + ".jpg")
    # The picture itself comes off a CDN, and that hop drops often enough
    # to lose an otherwise good generation. Paying for the image twice is
    # worse than asking for the bytes twice.
    payload = b""
    for attempt in range(3):
        try:
            # imgen.x.ai answers a bare urllib request with 403 - it wants
            # a browser's User-Agent. Measured: bare 403, with one 114 KB.
            fetch = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Macintosh)"})
            with urllib.request.urlopen(fetch, timeout=600) as feed:
                payload = feed.read()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    if len(payload) < 4096:
        raise RuntimeError("xAI returned an unusably small image")
    with open(destination, "wb") as handle:
        handle.write(payload)
    return destination


def _generated_file(directory, started, stdout=""):
    try:
        payload = json.loads((stdout or "").strip() or "{}")
        for path in payload.get("paths") or []:
            if os.path.isfile(path) and os.path.getsize(path) > 4096:
                return path
    except Exception:
        pass
    for _attempt in range(4):
        candidates = []
        for root, _, files in os.walk(directory):
            for name in files:
                if os.path.splitext(name)[1].lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    continue
                path = os.path.join(root, name)
                if os.path.getmtime(path) >= started - 2 and os.path.getsize(path) > 4096:
                    candidates.append(path)
        if candidates:
            return max(candidates, key=os.path.getmtime)
        time.sleep(1)
    detail = (stdout or "").strip()[-800:]
    raise RuntimeError(
        "the image provider returned no downloadable body image" +
        (f": {detail}" if detail else ""))


def _detect(image, label):
    height, width = image.shape[:2]
    regions = [(0, 0, width, height)]
    if height > width:
        regions += [(0, 0, width, int(height * fraction)) for fraction in (0.58, 0.46, 0.38)]
    for x, y, region_width, region_height in regions:
        crop = image[y:y + region_height, x:x + region_width]
        for scale in (1.0, 2.0, 3.5):
            candidate = crop if scale == 1.0 else cv2.resize(
                crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
            landmarks, _ = face.detect(candidate)
            if landmarks is not None:
                landmarks = landmarks / scale
                landmarks[:, 0] += x
                landmarks[:, 1] += y
                return landmarks
    raise RuntimeError(f"no face detected in the {label}")


def _face_transform(keyframe, body_image):
    key_landmarks = _detect(keyframe, "identity portrait")
    body_landmarks = _detect(body_image, "generated body")
    source = key_landmarks[face.RIGID].astype(np.float32)
    target = body_landmarks[face.RIGID].astype(np.float32)
    transform, inliers = cv2.estimateAffinePartial2D(
        source, target, method=cv2.LMEDS, refineIters=20)
    if transform is None:
        raise RuntimeError("could not align the original face to the generated body")
    projected = cv2.transform(source[None, :, :], transform)[0]
    residual = np.linalg.norm(projected - target, axis=1)
    scale = float(np.sqrt(transform[0, 0] ** 2 + transform[0, 1] ** 2))
    if not 0.18 <= scale <= 1.8:
        raise RuntimeError(f"generated head scale is unsafe ({scale:.2f}x)")
    oval = body_landmarks[face.FACE_OVAL]
    x, y, width, height = cv2.boundingRect(np.round(oval).astype(np.int32))
    return transform, {
        "residual_median_px": round(float(np.median(residual)), 3),
        "residual_max_px": round(float(np.max(residual)), 3),
        "scale": round(scale, 5),
        "face_bounds": [int(x), int(y), int(width), int(height)],
    }, key_landmarks


def _head_mask(cutout_image, landmarks, destination):
    alpha = cutout_image[:, :, 3].astype(np.float32) / 255.0
    oval = landmarks[face.FACE_OVAL]
    chin = float(np.max(oval[:, 1]))
    top = float(np.min(oval[:, 1]))
    left, right = float(np.min(oval[:, 0])), float(np.max(oval[:, 0]))
    center = (left + right) * 0.5
    face_width = max(1.0, right - left)
    face_height = max(1.0, chin - top)
    # Long dissolve: the live head and the generated body render hair with
    # different tone and sharpness, and a short fade turns that difference
    # into a visible horizontal band (carol, 2026-08-01). Half a face-height
    # spreads the handover below anything the eye can anchor on.
    fade_start = min(alpha.shape[0] - 2, chin + face_height * 0.05)
    fade_end = min(alpha.shape[0], chin + face_height * 0.55)
    ys = np.arange(alpha.shape[0], dtype=np.float32)
    fade = np.ones_like(ys)
    if fade_end > fade_start:
        region = (ys - fade_start) / (fade_end - fade_start)
        fade = np.clip(1.0 - region, 0.0, 1.0)
        fade = fade * fade * (3.0 - 2.0 * fade)
    neck_start = chin + face_height * 0.01
    progress = np.clip((ys - neck_start) / max(1.0, fade_end - neck_start), 0.0, 1.0)
    half_width = face_width * (0.34 - 0.07 * progress)
    # A narrow feather sliced vertically through the side hair; the gate
    # now dissolves over ~12% of the face width instead of 3.5%.
    feather = max(16.0, face_width * 0.12)
    xs = np.arange(alpha.shape[1], dtype=np.float32)[None, :]
    neck_gate = np.clip((half_width[:, None] + feather - np.abs(xs - center)) / feather, 0.0, 1.0)
    # The gate must ENGAGE gradually: a binary row switch at neck_start put
    # full portrait hair one row above and body hair one row below - the
    # horizontal border line the owner circled at chin height (carol,
    # 2026-08-01). Ramp it in over 0.28 face-heights: long enough that the
    # side hair dissolves, short enough that a portrait's own clothing
    # cannot ghost onto the generated outfit further down.
    engage = np.clip((ys - neck_start) / max(1.0, face_height * 0.28), 0.0, 1.0)
    engage = engage * engage * (3.0 - 2.0 * engage)
    neck_gate = 1.0 - engage[:, None] * (1.0 - neck_gate)
    mask = np.clip(alpha * fade[:, None] * neck_gate, 0.0, 1.0)
    rgba = np.full((*mask.shape, 4), 255, dtype=np.uint8)
    rgba[:, :, 3] = np.round(mask * 255).astype(np.uint8)
    cv2.imwrite(destination, rgba)


def _seam_tone_match(body_path, keyframe, portrait_cutout, mask_path,
                     transform, face_bounds):
    """The live head is a supersampled portrait; the body plate is a softer
    generated render. Where the head mask hands one to the other, their
    low-frequency tone difference reads as a horizontal band through the
    hair no matter how wide the dissolve is (carol, 2026-08-01). Shift the
    body plate's low frequencies toward the warped portrait inside the
    handover zone, fading out with the portrait's own silhouette."""
    if not face_bounds:
        return
    body_rgba = cv2.imread(body_path, cv2.IMREAD_UNCHANGED)
    height, width = body_rgba.shape[:2]
    warped = cv2.warpAffine(
        keyframe, transform, (width, height), flags=cv2.INTER_AREA)
    silhouette = cv2.warpAffine(
        portrait_cutout[:, :, 3], transform, (width, height)
    ).astype(np.float32) / 255.0
    mask = cv2.imread(mask_path, cv2.IMREAD_UNCHANGED)[:, :, 3]
    handover = cv2.warpAffine(mask, transform, (width, height)
                              ).astype(np.float32) / 255.0
    face_width = max(24.0, float(face_bounds[2]))
    low_sigma = face_width * 0.30
    body_bgr = body_rgba[:, :, :3].astype(np.float32)
    body_alpha = body_rgba[:, :, 3].astype(np.float32) / 255.0

    def masked_blur(image, alpha):
        # alpha-normalized blur: backgrounds and transparent pixels carry
        # zero weight, so they can never leak brightness into the delta
        blurred = cv2.GaussianBlur(image * alpha[..., None], (0, 0), low_sigma)
        cover = cv2.GaussianBlur(alpha, (0, 0), low_sigma)
        return blurred / np.maximum(cover, 1e-3)[..., None]

    delta = np.clip(masked_blur(warped.astype(np.float32), silhouette)
                    - masked_blur(body_bgr, body_alpha), -36.0, 36.0)
    # Correct ONLY around the transition line: the band term peaks where
    # head and body mix half-and-half and is zero where either side owns
    # the pixel outright - a first version weighted by the whole mask
    # washed the neck and chest with portrait brightness.
    band = np.clip(handover * (1.0 - handover) * 4.0, 0.0, 1.0)
    weight = np.clip(cv2.GaussianBlur(band, (0, 0), face_width * 0.12) * 1.4,
                     0.0, 1.0)
    weight *= np.clip(silhouette * 1.5, 0.0, 1.0) * body_alpha
    # Hair columns only: the neck skin of the two renders already agrees,
    # and correcting it painted a faint light collar across the throat.
    center_x = face_bounds[0] + face_bounds[2] * 0.5
    xs = np.abs(np.arange(width, dtype=np.float32) - center_x) / face_width
    hair_side = np.clip((xs - 0.38) / 0.22, 0.0, 1.0)
    hair_side = hair_side * hair_side * (3.0 - 2.0 * hair_side)
    weight *= hair_side[None, :]
    body_rgba[:, :, :3] = np.clip(
        body_bgr + delta * weight[..., None], 0, 255).astype(np.uint8)
    cv2.imwrite(body_path, body_rgba)


def _alpha_bounds(image):
    points = cv2.findNonZero((image[:, :, 3] > 8).astype(np.uint8))
    if points is None:
        raise RuntimeError("the generated body cutout is empty")
    return [int(value) for value in cv2.boundingRect(points)]


def _identity_reference(avatar_dir):
    head = os.path.join(avatar_dir, "head.png")
    return head if os.path.isfile(head) else os.path.join(avatar_dir, "keyframe.png")


def _emit(progress, stage, value, label):
    if progress:
        progress(stage, value, label)


def _cached_view_source(cache_dir, view):
    if not os.path.isdir(cache_dir):
        return None
    prefix = f"source-{view}."
    return next((
        os.path.join(cache_dir, name)
        for name in sorted(os.listdir(cache_dir))
        if name.startswith(prefix) and os.path.isfile(os.path.join(cache_dir, name))
    ), None)


def build(avatar_dir, options, log=print, progress=None):
    keyframe_path = os.path.join(avatar_dir, "keyframe.png")
    keyframe = cv2.imread(keyframe_path)
    if keyframe is None:
        raise RuntimeError("avatar keyframe is missing")
    identity_reference = _identity_reference(avatar_dir)
    if not os.path.isfile(identity_reference):
        raise RuntimeError("avatar identity head is missing")
    provider = default_provider()
    prompts = {view: _prompt(options, view=view) for view in BODY_VIEWS}
    with open(identity_reference, "rb") as handle:
        identity_digest = hashlib.sha256(handle.read()).hexdigest()
    signature = hashlib.sha256(
        (provider["name"] + "\n" + str(provider.get("model")) + "\n" +
         identity_digest + "\n" + "\n--- VIEW ---\n".join(
             prompts[view] for view in BODY_VIEWS)).encode("utf-8")
    ).hexdigest()
    cache_dir = os.path.join(avatar_dir, ".body-cache")
    cache_signature = os.path.join(cache_dir, "signature")
    cache_matches = False
    if os.path.isfile(cache_signature):
        with open(cache_signature) as handle:
            cache_matches = handle.read().strip() == signature
    if not cache_matches:
        shutil.rmtree(cache_dir, ignore_errors=True)
        os.makedirs(cache_dir, mode=0o700)
        with open(cache_signature, "w") as handle:
            handle.write(signature)
    cached = {
        view: _cached_view_source(cache_dir, view)
        for view in BODY_VIEWS
    }
    stage = tempfile.mkdtemp(prefix=".body-stage-", dir=avatar_dir)
    raw_dir = os.path.join(stage, "raw")
    os.makedirs(raw_dir)
    try:
        log(f"using EnConvo default image provider: {provider['title']}")
        sources = {}
        for view_index, view in enumerate(BODY_VIEWS):
            generated = cached[view]
            if generated:
                log(f"reusing the generated {view} body plate after a local QA retry")
            else:
                _emit(
                    progress, "generation", .14 + view_index * .18,
                    f"Generating {view} full-body view")
                log(f"generating {view} full body from the canonical HD head")
                references = [identity_reference]
                if view != "front":
                    references.append(sources["front"])
                provider_dir = os.path.join(raw_dir, view)
                os.makedirs(provider_dir, mode=0o700)
                started = time.time()
                # xAI edits go straight to xAI: EnConvo's route sends `n`,
                # which xAI refuses on an edit. Every other provider keeps
                # the CLI exactly as before, and so does xAI when we have
                # no key of our own to use.
                key = _xai_key() if provider.get("direct") else ""
                if key:
                    generated = _xai_edit(
                        prompts[view], references, provider_dir,
                        f"body-source-{view}", key)
                else:
                    result = subprocess.run(
                        _provider_command(
                            provider, references, provider_dir, prompts[view],
                            file_name=f"body-source-{view}"),
                        capture_output=True,
                        text=True,
                        timeout=900,
                        stdin=subprocess.DEVNULL,
                    )
                    if result.returncode:
                        detail = (result.stderr or result.stdout or "generation failed").strip()[-1200:]
                        raise RuntimeError(f"{view} view: {detail}")
                    generated = _generated_file(provider_dir, started, result.stdout)
                extension = os.path.splitext(generated)[1].lower() or ".png"
                cached_path = os.path.join(cache_dir, f"source-{view}{extension}")
                shutil.copy2(generated, cached_path)
                generated = cached_path
            sources[view] = generated

        staged_sources = {}
        for view in BODY_VIEWS:
            extension = os.path.splitext(sources[view])[1].lower() or ".png"
            staged_sources[view] = os.path.join(stage, f"source-{view}{extension}")
            shutil.copy2(sources[view], staged_sources[view])
        front_extension = os.path.splitext(staged_sources["front"])[1]
        shutil.copy2(
            staged_sources["front"], os.path.join(stage, "source" + front_extension))

        log("removing all three backgrounds locally with macOS Vision")
        view_metadata = {}
        view_images = {}
        purposes = {
            "front": "standing runtime body",
            "side": "Horizon Walk image reference",
            "back": "turn-around continuity reference",
        }
        for view_index, view in enumerate(BODY_VIEWS):
            _emit(
                progress, "cutout", .64 + view_index * .05,
                f"Cutting out {view} full-body view")
            body_path = os.path.join(stage, f"body-{view}.png")
            if not cutout.render(
                    staged_sources[view], body_path, log=log, tight=True):
                raise RuntimeError(f"local person cutout failed for the {view} view")
            body_rgba = cv2.imread(body_path, cv2.IMREAD_UNCHANGED)
            if body_rgba is None or body_rgba.ndim != 3 or body_rgba.shape[2] != 4:
                raise RuntimeError(f"generated {view} body did not produce an RGBA plate")
            height, width = body_rgba.shape[:2]
            view_images[view] = body_rgba
            view_metadata[view] = {
                "image": os.path.basename(body_path),
                "source": os.path.basename(staged_sources[view]),
                "width": int(width),
                "height": int(height),
                "bounds": _alpha_bounds(body_rgba),
                "purpose": purposes[view],
            }
        shutil.copy2(os.path.join(stage, "body-front.png"), os.path.join(stage, "body.png"))

        log("locking the calibrated face onto the generated front body")
        _emit(progress, "identity", .80, "Locking the calibrated face to the front view")
        transform, alignment, key_landmarks = _face_transform(
            keyframe, view_images["front"][:, :, :3])
        portrait_cutout_path = os.path.join(stage, "portrait-cutout.png")
        if not cutout.render(keyframe_path, portrait_cutout_path, log=lambda _message: None):
            raise RuntimeError("could not build the identity overlay mask")
        portrait_cutout = cv2.imread(portrait_cutout_path, cv2.IMREAD_UNCHANGED)
        _head_mask(portrait_cutout, key_landmarks, os.path.join(stage, "head-mask.png"))
        _seam_tone_match(
            os.path.join(stage, "body.png"), keyframe, portrait_cutout,
            os.path.join(stage, "head-mask.png"), transform,
            alignment.get("face_bounds"))
        os.remove(portrait_cutout_path)

        height, width = view_images["front"].shape[:2]
        face_transform = [
            [round(float(value), 7) for value in row]
            for row in transform
        ]
        view_metadata["front"]["face_transform"] = face_transform
        view_metadata["front"]["alignment"] = alignment
        metadata = {
            "v": 3,
            "image": "body.png",
            "head_mask": "head-mask.png",
            "identity_reference": os.path.basename(identity_reference),
            "width": int(width),
            "height": int(height),
            "bounds": _alpha_bounds(view_images["front"]),
            "face_transform": face_transform,
            "alignment": alignment,
            "turnaround": list(BODY_VIEWS),
            "views": view_metadata,
            "motion_reference": {
                "walk_view": "side",
                "walk_source": view_metadata["side"]["source"],
                "idle_view": "front",
                "idle_source": view_metadata["front"]["source"],
            },
            "provider": provider,
            "options": {
                "style": options.get("style", "photorealistic"),
                "pose": options.get("pose", "relaxed"),
                "prompt": _direction(options),
                "outfit": _clean(options.get("outfit"), 500),
                "notes": _clean(options.get("notes"), 600),
            },
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        with open(os.path.join(stage, "body.json"), "w") as handle:
            json.dump(metadata, handle, indent=1)

        destination = os.path.join(avatar_dir, "body")
        backup = destination + ".previous"
        if os.path.exists(backup):
            shutil.rmtree(backup)
        if os.path.exists(destination):
            os.replace(destination, backup)
        os.replace(stage, destination)
        stage = None
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(cache_dir, ignore_errors=True)
        return metadata
    finally:
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)


def remove(avatar_dir):
    shutil.rmtree(os.path.join(avatar_dir, "body"), ignore_errors=True)
    shutil.rmtree(os.path.join(avatar_dir, ".body-cache"), ignore_errors=True)
