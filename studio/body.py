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
import tempfile
import time

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
    custom = _clean(options.get("prompt"), 2400)
    if custom:
        return custom
    legacy = []
    outfit = _clean(options.get("outfit"), 500)
    notes = _clean(options.get("notes"), 600)
    if outfit:
        legacy.append(f"Wardrobe: {outfit}")
    if notes:
        legacy.append(f"Additional direction: {notes}")
    return " ".join(legacy) or DEFAULT_BODY_PROMPT


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

BACKGROUND — simple clean studio backdrop with strong person/background separation. The application will remove the background locally, so preserve fine hair edges and do not add smoke, veils, loose particles, or cast shadows behind the figure."""


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
    neck_gate[ys <= neck_start] = 1.0
    mask = np.clip(alpha * fade[:, None] * neck_gate, 0.0, 1.0)
    rgba = np.full((*mask.shape, 4), 255, dtype=np.uint8)
    rgba[:, :, 3] = np.round(mask * 255).astype(np.uint8)
    cv2.imwrite(destination, rgba)


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
