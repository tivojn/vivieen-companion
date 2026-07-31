"""Portrait-aware full-body art direction.

The Full Body Studio used to ship ONE hardcoded fashion paragraph for every
avatar. That paragraph was written for a photoreal adult woman in tailored
separates, so it was wrong for most uploads: it dressed a game hero in office
separates, and it told a stylised anime portrait to keep "real skin texture".

This module reads the uploaded portrait through EnConvo's currently selected
vision LLM and writes the art direction FOR THAT SUBJECT - medium, presentation,
apparent age, implied profession, and existing style register all steer the
brief. A photoreal fashion subject gets silhouette, palette and jewellery
discipline; a game or fantasy character gets costume, armour, material and
lighting detail instead.

Three rules are structural rather than stylistic, so they are enforced in code
after the model writes: NO HEAVY LAYERS, NO BAGGY TROUSERS, and NOTHING IN THE
HANDS. The first two destroy the silhouette the runtime rig depends on - bulky
outerwear hides the shoulder line the face is mapped onto, and wide slouchy legs
break the walk cycle's stride read. The third breaks every downstream pose: a
handbag welded to one hand cannot wave, point, or swing through a walk cycle,
and a carried prop re-appears inconsistently across the front/side/back
turnaround. The model is told, and the result is then checked.

Everything degrades to the static preset: no vision model, no network, bad JSON,
or a banned garment surviving the rewrite all fall back rather than fail the
build.
"""
import base64
import datetime
import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request

import cv2


ENCONVO_API = os.environ.get("ENCONVO_API", "http://127.0.0.1:54535")
CACHE_NAME = ".wardrobe.json"
ANALYSIS_EDGE = 768
PROMPT_LIMIT = 2400
REQUEST_TIMEOUT = 180

# Garments that break the runtime rig rather than merely look wrong. Kept as
# whole words so "overcoat" is caught but "coated" is not.
BANNED_PATTERNS = (
    r"heav(?:y|ily)\s+layer", r"heavy\s+outerwear", r"thick\s+layer",
    r"layered\s+heav", r"bulk(?:y|ier)", r"padded\s+(?:coat|jacket|parka)",
    r"puffer", r"parka", r"overcoat", r"greatcoat", r"duffel\s+coat",
    r"trench\s*coat", r"poncho", r"cloak", r"shawl", r"cape\b",
    r"bagg(?:y|ie)", r"slouch(?:y|ed)", r"wide[-\s]?leg", r"palazzo",
    r"harem\s+pant", r"parachute\s+pant", r"cargo\s+pant", r"oversized\s+pant",
    r"loose[-\s]?fit(?:ting)?\s+(?:pant|trouser|jean)",
    # Carried props: a bag welded to one hand cannot survive the turnaround or
    # any downstream pose, so nothing may be held, slung, or hooked on an arm.
    r"\bbags?\b", r"handbag", r"\bpurse\b", r"\bclutch\b", r"\btotes?\b",
    r"\bsatchel\b", r"briefcase", r"backpack", r"rucksack", r"crossbody",
    r"\bumbrella\b", r"holding\s+(?:a|an|the|any)\b", r"\bhand-?held\b",
    r"in\s+(?:her|his|their|one)\s+hands?\b",
)
BANNED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in BANNED_PATTERNS)

SILHOUETTE_RULE = (
    "Never use heavy layering, bulky outerwear, capes, or padded coats, and never "
    "use baggy, slouchy, wide-leg, or oversized trousers: the silhouette must stay "
    "clean and readable from shoulder to ankle."
)

HANDS_RULE = (
    "The subject carries nothing at all: both hands stay completely empty and "
    "clearly visible, with no bag, handbag, clutch, purse, tote, backpack, "
    "briefcase, phone, cup, umbrella, weapon, or any other held prop, and nothing "
    "slung over a shoulder, hooked on an elbow, or worn across the body."
)

# Appended to every finished brief. Kept out of the ban check, since the rules
# name the very garments and props they forbid.
STRUCTURAL_RULE = f"{SILHOUETTE_RULE} {HANDS_RULE}"

SYSTEM = (
    "You are a senior costume designer and fashion director. You look at one "
    "reference portrait and write the wardrobe brief for a full-body character "
    "plate of that exact person.\n\n"
    "Return STRICT JSON only, no prose and no code fence, with these keys:\n"
    '"presentation" - feminine, masculine, or androgynous.\n'
    '"age_band" - young adult, adult, or mature.\n'
    '"medium" - photograph, game art, anime, illustration, or 3d render.\n'
    '"register" - three to six words naming the aesthetic, e.g. "fashion-forward "'
    'contemporary womenswear" or "mythic Chinese action-game hero".\n'
    '"profession" - the implied role or profession, or "unspecified".\n'
    '"palette" - three to five concrete colours read from the portrait.\n'
    '"direction" - the wardrobe and styling brief itself, 90 to 150 words, '
    "written as instructions to an image model.\n\n"
    "RULES FOR \"direction\":\n"
    "1. Match the MEDIUM. A photograph gets real fabrics, tailoring and "
    "photographic realism. Game art, anime, or 3d art gets high-detail costume, "
    "armour, ornament, material breakdown, and dramatic practical or rim lighting "
    "instead of everyday clothing.\n"
    "2. Match the PERSON. Dress the presentation, apparent age and implied "
    "profession you actually see. Do not default to office separates.\n"
    "3. Match the STYLE already in the portrait, then raise it to couture "
    "level. Write tailoring in a cutter's language: a sculpted waist, a clean "
    "asymmetric or architectural neckline, a streamlined skirt or trouser "
    "line that follows the figure without restricting movement. Name a "
    "substantial fabric with real behaviour - matte crepe with subtle "
    "stretch, silk mikado, double-faced wool, fine gabardine - and give it "
    "crisp internal structure and realistic tension at the seams. A heroic "
    "or fantasy subject should read powerful instead, through costume "
    "detail, armour plating, weathering and ornament.\n"
    "4. Keep colour disciplined and hierarchical: one luminous hero colour "
    "(rich and refined, never neon), one supporting accent - champagne "
    "metal, polished gold, or a tonal step - and quiet neutral foundations, "
    "all named explicitly.\n"
    "5. Choose exactly ONE statement detail - a sculptural metal clasp, an "
    "architectural seam, one jewellery focal point - and keep the rest "
    "restrained: small stud earrings, a delicate ring at most. For fashion "
    "subjects finish with pointed leather pumps in the spirit of a classic "
    "100mm dorsay stiletto - a killer heel - and close the brief demanding "
    "immaculate seams, understated luxury, and confident photographic "
    "polish.\n"
    "6. HARD BAN: never heavy layering, bulky or padded outerwear, puffers, "
    "parkas, trench coats, capes, cloaks or shawls; and never baggy, slouchy, "
    "wide-leg, cargo, or oversized trousers. Keep trousers, skirts and armour "
    "greaves fitted and the full silhouette readable from shoulder to ankle.\n"
    "7. HARD BAN: the subject carries NOTHING. Never mention, describe, or imply "
    "a bag, handbag, clutch, purse, tote, backpack, briefcase, phone, cup, "
    "umbrella, weapon, staff, or any other held or carried object, and never "
    "sling a bag or strap over a shoulder, an elbow, or across the body. Both "
    "hands stay empty. Carried props break the pose rig and cannot survive the "
    "front/side/back turnaround.\n"
    "8. Clothing stays opaque and suitable for public view: no nudity, lingerie, "
    "sheer fabric, exposed intimate areas, or vulgar styling. Allure comes from "
    "cut, fit and confidence, never from exposure.\n"
    "9. Never describe the face, hairstyle, skin tone, or identity - those are "
    "locked elsewhere, and any eyeglasses already worn in the portrait stay "
    "exactly as they are. Write only wardrobe, materials, palette, accessories, "
    "footwear, and for stylised media the lighting and rendering detail."
)

USER_TEXT = (
    "Analyse this reference portrait and write the full-body wardrobe brief for "
    "this exact subject. Return the strict JSON object only."
)


def _clean(value, maximum=PROMPT_LIMIT):
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()[:maximum]


def banned_terms(text):
    """Every rig-breaking garment named in the text, for logs and tests."""
    found = []
    for pattern in BANNED:
        match = pattern.search(text or "")
        if match and match.group(0).lower() not in found:
            found.append(match.group(0).lower())
    return found


def _preference(key):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\|[A-Za-z0-9_.-]+)?", key or ""):
        return {}
    root = os.path.abspath(os.environ.get(
        "ENCONVO_PREFERENCES_DIR",
        os.path.expanduser("~/.config/enconvo/installed_preferences")))
    path = os.path.abspath(os.path.join(root, f"{key}.json"))
    if os.path.dirname(path) != root:
        return {}
    try:
        with open(path) as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _llm_route():
    """EnConvo's currently selected chat provider, as a validated route."""
    selected = _preference("llm").get("selected")
    if not isinstance(selected, str) or not selected.startswith("llm|"):
        from . import body
        selected = body.selected_provider("llm")["command_key"]
    provider = str(selected).split("|", 1)[1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
        raise RuntimeError("EnConvo returned an invalid LLM provider")
    detail = _preference(selected)
    return f"llm/features/{provider}/chat", str(detail.get("modelName") or "")


def _encoded_reference(image_path):
    """Downscale before upload: the brief needs the look, not the megapixels."""
    image = cv2.imread(image_path, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError("could not read the identity portrait")
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > ANALYSIS_EDGE:
        scale = ANALYSIS_EDGE / float(longest)
        image = cv2.resize(
            image, (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        raise RuntimeError("could not encode the identity portrait")
    return base64.b64encode(buffer.tobytes()).decode("ascii")


def _chat(route, model, encoded):
    request = {
        "system": SYSTEM,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": USER_TEXT},
            {"type": "image_url",
             "image_url": {"url": "data:image/jpeg;base64," + encoded}},
        ]}],
        "modelParams": {"maxOutputTokens": 900},
    }
    if model:
        request["modelName"] = model
    payload = json.dumps(request).encode("utf-8")
    handle = urllib.request.Request(
        f"{ENCONVO_API}/{route}", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(handle, timeout=REQUEST_TIMEOUT) as response:
        result = json.loads(response.read().decode("utf-8") or "{}")
    if not isinstance(result, dict):
        raise RuntimeError("the vision model returned an invalid response")
    if result.get("error"):
        error = result["error"]
        if isinstance(error, dict):
            error = error.get("message") or "provider error"
        raise RuntimeError(str(error)[:300])
    text = result.get("text")
    if not text:
        message = result.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, list):
            content = "".join(str(part.get("text") or "") for part in content
                              if isinstance(part, dict))
        text = content
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("the vision model returned an empty response")
    return text


def _parse(text):
    body_text = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                       flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        parsed = json.loads(body_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", body_text, re.DOTALL)
        if not match:
            raise RuntimeError("the vision model did not return JSON")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("the vision model did not return a JSON object")
    direction = _clean(parsed.get("direction"), PROMPT_LIMIT - 600)
    if len(direction) < 60:
        raise RuntimeError("the vision model returned an unusably short brief")
    palette = parsed.get("palette")
    if isinstance(palette, list):
        palette = ", ".join(str(value) for value in palette if value)
    traits = {
        "presentation": _clean(parsed.get("presentation"), 40),
        "age_band": _clean(parsed.get("age_band"), 40),
        "medium": _clean(parsed.get("medium"), 40),
        "register": _clean(parsed.get("register"), 90),
        "profession": _clean(parsed.get("profession"), 90),
        "palette": _clean(palette, 140),
    }
    return direction, traits


def _finalise(direction):
    """Refuse anything that broke a hard ban, then append the structural rules.

    The check runs on the model's own words BEFORE the rules are appended: the
    rules have to name the banned garments and props to forbid them, so checking
    the joined text would flag the cure as the disease.
    """
    violations = banned_terms(direction)
    if violations:
        raise RuntimeError(
            "the vision model kept a banned garment: " + ", ".join(violations))
    if not direction.endswith((".", "!", "?")):
        direction += "."
    return _clean(f"{direction} {STRUCTURAL_RULE}", PROMPT_LIMIT)


def preset_prompt():
    from . import body
    return _clean(f"{body.DEFAULT_BODY_PROMPT} {STRUCTURAL_RULE}", PROMPT_LIMIT)


def _identity_reference(avatar_dir):
    head = os.path.join(avatar_dir, "head.png")
    if os.path.isfile(head):
        return head
    keyframe = os.path.join(avatar_dir, "keyframe.png")
    if os.path.isfile(keyframe):
        return keyframe
    raise RuntimeError("avatar identity portrait is missing")


def _digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_cache(avatar_dir, digest):
    try:
        with open(os.path.join(avatar_dir, CACHE_NAME)) as handle:
            cached = json.load(handle)
    except Exception:
        return None
    if not isinstance(cached, dict) or cached.get("digest") != digest:
        return None
    prompt = _clean(cached.get("prompt"), PROMPT_LIMIT)
    if len(prompt) < 60:
        return None
    traits = cached.get("traits")
    return {
        "prompt": prompt,
        "source": "tailored",
        "traits": traits if isinstance(traits, dict) else {},
        "cached": True,
    }


def _write_cache(avatar_dir, payload):
    directory = os.path.abspath(avatar_dir)
    descriptor, temporary = tempfile.mkstemp(prefix=".wardrobe-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(payload, handle, indent=1)
        os.chmod(temporary, 0o600)
        os.replace(temporary, os.path.join(directory, CACHE_NAME))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def cached_prompt(avatar_dir):
    """The stored tailored brief, or None. Never calls a model."""
    try:
        return _read_cache(avatar_dir, _digest(_identity_reference(avatar_dir)))
    except Exception:
        return None


def tailored_prompt(avatar_dir, refresh=False, log=None):
    """Portrait-specific art direction, falling back to the static preset."""
    def note(message):
        if log:
            log(message)

    try:
        reference = _identity_reference(avatar_dir)
        digest = _digest(reference)
    except Exception as error:
        return {"prompt": preset_prompt(), "source": "preset",
                "traits": {}, "error": str(error)[:300]}

    if not refresh:
        cached = _read_cache(avatar_dir, digest)
        if cached:
            return cached

    try:
        route, model = _llm_route()
        direction, traits = _parse(_chat(route, model, _encoded_reference(reference)))
        prompt = _finalise(direction)
    except Exception as error:
        note(f"portrait-tailored prompt unavailable, using the preset: {error}")
        return {"prompt": preset_prompt(), "source": "preset",
                "traits": {}, "error": str(error)[:300]}

    payload = {
        "digest": digest,
        "prompt": prompt,
        "traits": traits,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        _write_cache(avatar_dir, payload)
    except Exception:
        pass
    note("composed a portrait-tailored full-body prompt")
    return {"prompt": prompt, "source": "tailored", "traits": traits, "cached": False}
