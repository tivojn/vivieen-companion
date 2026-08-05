"""Think, speak, hear - swappable between local models and cloud vendors.

No vendor SDKs. Every cloud provider here is a plain JSON-over-HTTP API and
there are only four request shapes in the whole file (OpenAI-compatible,
Anthropic, Gemini, Ollama), so httpx covers all of them. Four SDKs would be
~400MB of transitive dependencies to send the same POST bodies, and each one
would pin its own httpx.

Model IDs are deliberately NOT hardcoded. Every provider exposes a list
endpoint, so the settings UI fetches the live catalogue with the user's own key
and the app never ships a stale list of model names.

One thing genuinely changes with the provider: LIP-SYNC ACCURACY. Kokoro is a
StyleTTS2 derivative that predicts a per-phoneme frame count and upsamples by
it, so its own duration array IS a forced alignment - exact, free, no second
model. Cloud voices return audio and nothing else. See align.py for what is
done about that; the honest summary is that local Kokoro is sample-accurate and
everything else is estimated. The UI says so.
"""
import os, io, json, base64, shutil, tempfile, subprocess, threading, asyncio, re, time
import numpy as np
import httpx

CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_ROOT = os.path.abspath(os.environ.get("VIVIEEN_DATA_DIR", CODE_ROOT))
CONFIG = os.path.abspath(os.environ.get(
    "VIVIEEN_CONFIG", os.path.join(DATA_ROOT, "config.json")))
SR = 24000
_lock = threading.Lock()
_global_lock = threading.Lock()
_route_lock = threading.Lock()
_cache = {"tts": None, "globals": None, "globals_at": 0.0}
_routes = {}
ENCONVO = shutil.which("enconvo") or os.path.expanduser("~/.config/enconvo/bin/enconvo")
ENCONVO_API = "http://127.0.0.1:54535"
ENCONVO_PREFERENCES = os.path.abspath(os.environ.get(
    "ENCONVO_PREFERENCES_DIR",
    os.path.expanduser("~/.config/enconvo/installed_preferences")))


# ---------------------------------------------------------------- config

DEFAULTS = {
    "llm": {"provider": "enconvo", "model": "",
            "base_url": "", "api_key": "", "temperature": 0.8, "max_tokens": 160},
    "tts": {"provider": "enconvo", "model": "", "voice": "",
            "base_url": "", "api_key": "", "speed": 1.0},
    "stt": {"provider": "enconvo", "model": "",
            "base_url": "", "api_key": "", "language": "auto"},
    # Realtime conversation ("Live talk"): a full speech-to-speech loop.
    # xAI is the primary backend - one websocket, one price, Grok included.
    # ElevenLabs runs through a Conversational-AI agent (auto-created on
    # first use and remembered). Keys never leave this server.
    "image": {"provider": "enconvo", "model": "",
              "base_url": "", "api_key": "", "size": "1024x1024"},
    "video": {"provider": "enconvo", "model": "",
              "base_url": "", "api_key": "", "seconds": 5,
              # xAI's Grok Imagine takes a resolution; 1080p needs
              # grok-imagine-video-1.5 (measured 2026-08-04).
              "resolution": "1080p"},
    "live": {"provider": "xai",
             "xai_api_key": "", "xai_voice": "eve",
             "xai_model": "grok-voice-think-fast-1.0",
             "eleven_api_key": "", "eleven_voice_id": "",
             "eleven_agent_id": ""},
    "persona": {
        "name": "Vivieen",
        "system": (
            "You are Vivieen, a sharp global-macro financial analyst with dry wit. "
            "You are SPEAKING ALOUD through a voice interface, so keep every reply to "
            "1-3 short sentences. Never use lists, markdown, headings, bullet points or "
            "emoji. Be direct and warm but never fawning - no 'Great question'. You hold "
            "real opinions on markets and state them plainly, always naming the downside. "
            "Talk like a person talking, not like written prose.")},
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if k == "key_checks":
            out[k] = v      # verdicts replace wholesale: a retired row's
            continue        # green tick must actually disappear from disk
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _read_config_file():
    try:
        with open(CONFIG) as f:
            return _merge(DEFAULTS, json.load(f))
    except Exception:
        return json.loads(json.dumps(DEFAULTS))


_migrated = [False]


# One key per platform (#25). The owner pasted the same xAI key into
# Think, Create, and Live voice separately - six paste boxes for one
# secret. config["keys"] holds one key per platform; a lane whose block
# has no key of its own inherits the platform key for its provider here,
# at load, in memory only - the file never learns the inherited value,
# exactly like the vault's materialised secrets. An explicit lane key
# still wins. The aliases collapse family ids onto their platform.
_PLATFORM_ALIASES = {"minimax_llm": "minimax", "together_image": "together"}


def platform_of(provider):
    return _PLATFORM_ALIASES.get(provider or "", provider or "")


def _inherit_platform_keys(cfg):
    keys = cfg.get("keys")
    if not isinstance(keys, dict) or not keys:
        return cfg
    for kind in ("llm", "tts", "stt", "image", "video"):
        block = cfg.get(kind)
        if isinstance(block, dict) and not block.get("api_key"):
            inherited = keys.get(platform_of(block.get("provider"))) or ""
            if inherited:
                block["api_key"] = inherited
    live = cfg.get("live")
    if isinstance(live, dict):
        if not live.get("xai_api_key") and keys.get("xai"):
            live["xai_api_key"] = keys["xai"]
        if not live.get("eleven_api_key") and keys.get("elevenlabs"):
            live["eleven_api_key"] = keys["elevenlabs"]
    return cfg


def load():
    """Secrets live in the vault (macOS Keychain), the file keeps only
    markers, and load() hands back a config with the real values woven in -
    memory only, so no call site changed and no key touches disk again."""
    import credentials
    cfg = _read_config_file()
    if not _migrated[0]:
        _migrated[0] = True
        if credentials.absorb(cfg):        # first run after the upgrade:
            _write_config_file(cfg)        # sweep plaintext into the vault
    return _inherit_platform_keys(credentials.materialise(cfg))


def _write_config_file(cfg):
    directory = os.path.dirname(CONFIG)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, tmp = tempfile.mkstemp(prefix=".config-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(cfg, handle, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save(cfg):
    """Merge-and-write, so a UI that posts only the TTS block cannot wipe the
    API key sitting in the STT block. Incoming plaintext keys are swept into
    the vault before anything is written; the file only ever sees markers."""
    import credentials
    with _lock:
        cur = _read_config_file()
        new = _merge(cur, cfg)
        credentials.absorb(new)
        _write_config_file(new)
        if (cur.get("tts") or {}) != (new.get("tts") or {}):
            _cache["tts"] = None         # voice or engine changed, drop the pipeline
    return credentials.materialise(new)


def redacted(cfg):
    """Never send a key back to the browser - only whether one is stored."""
    out = json.loads(json.dumps(cfg))
    for k in ("llm", "tts", "stt", "image", "video"):
        block = out.setdefault(k, {})
        if block.get("api_key"):
            block["api_key"] = ""
            block["has_key"] = True
        else:
            block["has_key"] = False
    live = out.setdefault("live", {})
    for field in ("xai_api_key", "eleven_api_key"):
        live["has_" + field] = bool(live.get(field))
        live[field] = ""
    # The platform keyring (#25): same write-only contract as the lanes -
    # the browser learns which platforms hold a key, never the key.
    keys = out.get("keys") if isinstance(out.get("keys"), dict) else {}
    out["keys"] = {name: "" for name in keys}
    out["has_keys"] = {name: bool(value) for name, value in keys.items()}
    return out


# ---------------------------------------------------------------- catalogue

# base_url is the default endpoint; `openai_shape` means /v1/chat/completions,
# /v1/models, /v1/audio/* all behave identically, which is most of the market.
PROVIDERS = {
    "llm": [
        dict(id="enconvo", label="EnConvo Global Default", managed=True, key=False,
             base="", note="Inherits Global Providers Settings → AI Model. "
                            "Credentials remain inside EnConvo."),
        dict(id="ollama", label="Ollama", local=True, key=False,
             base="http://localhost:11434", note="Local models. Nothing leaves the Mac."),
        dict(id="openai", label="OpenAI", key=True, base="https://api.openai.com/v1"),
        dict(id="anthropic", label="Anthropic", key=True, base="https://api.anthropic.com/v1"),
        dict(id="gemini", label="Google Gemini", key=True,
             base="https://generativelanguage.googleapis.com/v1beta"),
        dict(id="xai", label="xAI Grok", key=True, base="https://api.x.ai/v1"),
        dict(id="groq", label="Groq", key=True, base="https://api.groq.com/openai/v1"),
        dict(id="deepseek", label="DeepSeek", key=True, base="https://api.deepseek.com/v1"),
        dict(id="openrouter", label="OpenRouter", key=True, base="https://openrouter.ai/api/v1"),
        # The OpenAI wire shape is the market's lingua franca - one adapter,
        # the whole fleet. Borrowed leaf: EnConvo's credential manager lists
        # the market per category; so does this catalogue now.
        dict(id="mistral", label="Mistral", key=True, base="https://api.mistral.ai/v1"),
        dict(id="together", label="Together AI", key=True, base="https://api.together.xyz/v1"),
        dict(id="fireworks", label="Fireworks", key=True,
             base="https://api.fireworks.ai/inference/v1"),
        dict(id="perplexity", label="Perplexity", key=True, base="https://api.perplexity.ai"),
        dict(id="moonshot", label="Moonshot Kimi", key=True, base="https://api.moonshot.ai/v1"),
        dict(id="qwen", label="Alibaba Qwen", key=True,
             base="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        dict(id="zhipu", label="Zhipu GLM", key=True,
             base="https://open.bigmodel.cn/api/paas/v4"),
        dict(id="minimax_llm", label="MiniMax", key=True, base="https://api.minimax.io/v1"),
        dict(id="cerebras", label="Cerebras", key=True, base="https://api.cerebras.ai/v1"),
        dict(id="nvidia", label="NVIDIA NIM", key=True,
             base="https://integrate.api.nvidia.com/v1"),
        dict(id="lmstudio", label="LM Studio", local=True, key=False,
             base="http://localhost:1234/v1", note="Local OpenAI-compatible server."),
        dict(id="custom", label="Custom (OpenAI-compatible)", key=False, base="",
             note="Any server that speaks /v1/chat/completions."),
    ],
    "tts": [
        dict(id="enconvo", label="EnConvo Global Default", managed=True, key=False,
             base="", note="Inherits Global Providers Settings → Text-to-Speech. "
                            "Credentials remain inside EnConvo."),
        dict(id="kokoro", label="Kokoro 82M", local=True, key=False, base="",
             note="Local, and the only engine with exact lip-sync - it reports its own "
                  "per-phoneme durations.", exact=True),
        dict(id="edge", label="Edge TTS", key=False, base="",
             note="Free Microsoft voices. Ships word boundaries, so lip-sync is good."),
        dict(id="elevenlabs", label="ElevenLabs", key=True, base="https://api.elevenlabs.io/v1",
             note="Returns character-level timestamps, so lip-sync stays tight."),
        dict(id="openai", label="OpenAI", key=True, base="https://api.openai.com/v1",
             note="Audio only - mouth timing is estimated from the text."),
        dict(id="gemini", label="Google Gemini", key=True,
             base="https://generativelanguage.googleapis.com/v1beta",
             note="Audio only - mouth timing is estimated from the text."),
        dict(id="deepgram", label="Deepgram Aura", key=True,
             base="https://api.deepgram.com/v1",
             note="Audio only - mouth timing is estimated from the text."),
        dict(id="cartesia", label="Cartesia Sonic", key=True,
             base="https://api.cartesia.ai",
             note="Audio only - mouth timing is estimated from the text."),
        dict(id="system", label="macOS say", local=True, key=False, base="",
             note="Always available, no download."),
    ],
    "stt": [
        dict(id="enconvo", label="EnConvo Global Default", managed=True, key=False,
             base="", note="Inherits EnConvo's default speech-to-text provider. "
                            "Credentials remain inside EnConvo."),
        # Second on purpose: the direct path for users who skip EnConvo.
        dict(id="soniox", label="Soniox Realtime", key=True, base="",
             note="Realtime dictation over Soniox's WebSocket API - words "
                  "stream into the input field while you speak."),
        dict(id="mlx_whisper", label="Whisper (MLX, local)", local=True, key=False, base="",
             note="Runs on the Metal GPU. Nothing leaves the Mac."),
        dict(id="openai", label="OpenAI", key=True, base="https://api.openai.com/v1"),
        dict(id="groq", label="Groq", key=True, base="https://api.groq.com/openai/v1",
             note="Whisper large v3, very fast."),
        dict(id="gemini", label="Google Gemini", key=True,
             base="https://generativelanguage.googleapis.com/v1beta"),
        dict(id="deepgram", label="Deepgram Nova", key=True,
             base="https://api.deepgram.com/v1"),
        dict(id="elevenlabs", label="ElevenLabs Scribe", key=True,
             base="https://api.elevenlabs.io/v1"),
        dict(id="custom", label="Custom (OpenAI-compatible)", key=False, base=""),
    ],
    # Media generation: EnConvo's global default first (its credentials
    # stay inside EnConvo), then the market on your own keys.
    "image": [
        dict(id="enconvo", label="EnConvo Global Default", managed=True, key=False,
             base="", note="Inherits Global Providers Settings → Image Generation. "
                            "Credentials remain inside EnConvo."),
        dict(id="openai", label="OpenAI Images", key=True,
             base="https://api.openai.com/v1"),
        dict(id="gemini", label="Google Imagen", key=True,
             base="https://generativelanguage.googleapis.com/v1beta"),
        dict(id="xai", label="xAI Grok Image", key=True, base="https://api.x.ai/v1"),
        dict(id="stability", label="Stability AI", key=True,
             base="https://api.stability.ai"),
        dict(id="bfl", label="Black Forest Labs FLUX", key=True,
             base="https://api.bfl.ai"),
        dict(id="together_image", label="Together AI (FLUX)", key=True,
             base="https://api.together.xyz/v1"),
        dict(id="recraft", label="Recraft", key=True,
             base="https://external.api.recraft.ai/v1"),
    ],
    "video": [
        dict(id="enconvo", label="EnConvo Global Default", managed=True, key=False,
             base="", note="Inherits Global Providers Settings → Video Generation. "
                            "Credentials remain inside EnConvo."),
        dict(id="openai", label="OpenAI Sora", key=True,
             base="https://api.openai.com/v1"),
        dict(id="gemini", label="Google Veo", key=True,
             base="https://generativelanguage.googleapis.com/v1beta"),
        dict(id="luma", label="Luma Dream Machine", key=True,
             base="https://api.lumalabs.ai/dream-machine/v1"),
        dict(id="runway", label="Runway", key=True, base="https://api.dev.runwayml.com/v1"),
    ],
}

OPENAI_SHAPE = {"openai", "xai", "groq", "deepseek", "openrouter", "lmstudio", "custom",
                "mistral", "together", "fireworks", "perplexity", "moonshot",
                "qwen", "zhipu", "minimax_llm", "cerebras", "nvidia"}


def spec(kind, pid):
    for p in PROVIDERS[kind]:
        if p["id"] == pid:
            return p
    return None


def _base(kind, c):
    s = spec(kind, c.get("provider")) or {}
    return (c.get("base_url") or s.get("base") or "").rstrip("/")


def catalog():
    return {k: [dict(v) for v in vs] for k, vs in PROVIDERS.items()}


# -------------------------------------------------------- EnConvo inheritance

_PROVIDER_LABELS = {
    "enconvo_ai": "EnConvo Cloud Plan", "open_ai": "OpenAI",
    "anthropic": "Anthropic", "gemini": "Google Gemini AI", "x_ai": "xAI",
    "groq": "Groq", "ollama": "Ollama", "mlx": "MLX",
    "mlx_kokoro": "Kokoro (MLX)", "edge_tts": "Edge TTS",
    "eleven_labs": "ElevenLabs", "open_ai_tts": "OpenAI TTS",
    "gemini_tts": "Gemini TTS", "xai_tts": "xAI TTS",
    "mlx_audio": "Whisper (MLX)", "apple_speech": "Apple Speech",
}
_GLOBAL_SELECTIONS = {
    "llm": ("llm", "llm|"),
    "tts": ("tts", "tts|"),
    # Hold-to-talk is dictation, not file transcription: EnConvo keeps two
    # separate defaults ("stt" is the transcription panel), and the avatar
    # must follow the Dictation one.
    "stt": ("dictation", "transcribe|"),
}
_GLOBAL_FIELDS = {
    "llm": ["modelName", "temperature", "reasoning_effort", "modelName_preferences"],
    "tts": ["modelName", "modelName_preferences", "voice", "speed", "format"],
    "stt": ["modelName", "speechRecognitionLanguage"],
}


def _safe_error(text):
    text = (text or "").strip().replace("\n", " ")
    text = re.sub(
        r"(?i)(api[_ -]?key|authorization|access[_ -]?token)\s*[:=]\s*"
        r"(?:bearer\s+)?[^\s,;}]+", r"\1=[redacted]", text)
    text = re.sub(r"\b(?:sk|xai|gsk)_[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", text)
    return text[-500:] or "Provider command failed"


def safe_error(error, limit=300):
    return _safe_error(str(error))[-limit:]


def failure_hint(error):
    """One plain clause naming why a provider call failed. The bubble is the
    only surface the user sees; a bare 'not answering' once hid an Ollama
    Cloud 402 (out of credit) for a whole evening."""
    text = str(error)
    match = re.search(r"[\s']([45]\d{2})[\s']", f" {text} ")
    status = int(match.group(1)) if match else 0
    if status in (401, 403):
        return "the provider rejected the API key"
    if status == 402:
        return "the provider wants payment or sign-in for this model"
    if status == 404:
        return "the provider does not know this model"
    if status == 410:
        return "the provider no longer serves this model"
    if status == 429:
        return "the provider is rate-limiting"
    if status >= 500:
        return "the provider is having an outage"
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "the request timed out"
    if "connect" in lowered:
        return "the endpoint is unreachable"
    return ""


def _run_enconvo_json(args, timeout=60):
    if not ENCONVO or not os.path.exists(ENCONVO):
        raise RuntimeError("EnConvo CLI is not installed")
    result = subprocess.run([ENCONVO, *args], capture_output=True, text=True,
                            timeout=timeout, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError(_safe_error(result.stderr or result.stdout))
    raw = (result.stdout or "").strip()
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"EnConvo returned invalid JSON: {exc}") from exc


async def _run_enconvo_async(args, timeout=60):
    return await asyncio.to_thread(_run_enconvo_json, args, timeout)


def _read_preference(key):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\|[A-Za-z0-9_.-]+)?", key or ""):
        return {}
    path = os.path.abspath(os.path.join(ENCONVO_PREFERENCES, f"{key}.json"))
    if os.path.dirname(path) != ENCONVO_PREFERENCES:
        return {}
    try:
        with open(path) as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _global_one(kind):
    selection_key, prefix = _GLOBAL_SELECTIONS[kind]
    selection = _read_preference(selection_key)
    selected = selection.get("selected")
    if not isinstance(selected, str):
        selection = _run_enconvo_json(
            ["config", "get", selection_key, "--includes", "selected"], timeout=15)
        selected = selection.get("selected")
    if not isinstance(selected, str) or not selected.startswith(prefix):
        raise RuntimeError(f"EnConvo has no global {kind.upper()} provider selected")

    detail = _run_enconvo_json(
        ["config", "get", selected, "--includes", *_GLOBAL_FIELDS[kind]], timeout=15)
    # Panels like Dictation keep their own per-route overrides (e.g. the
    # dictation model "stt-rt-v5") embedded in their own preference file;
    # those beat the route's standalone defaults.
    embedded = selection.get(selected)
    if isinstance(embedded, dict):
        detail = {**detail, **embedded}
    provider = selected.split("|", 1)[1]
    model = detail.get("modelName") or ""
    voice = detail.get("voice") or ""
    model_preferences = detail.get("modelName_preferences") or {}
    if isinstance(model_preferences, dict):
        if not model and len(model_preferences) == 1:
            model = next(iter(model_preferences))
        selected_preferences = model_preferences.get(model) or {}
        if isinstance(selected_preferences, dict):
            voice = voice or selected_preferences.get("voice") or ""

    label = _PROVIDER_LABELS.get(provider) or provider.replace("_", " ").title()
    parts = [label]
    if model:
        parts.append(model)
    if voice:
        parts.append(voice)
    timing = "estimated"
    return {
        "available": True,
        "command_key": selected,
        "provider": provider,
        "provider_label": label,
        "model": model,
        "voice": voice,
        "language": detail.get("speechRecognitionLanguage") or "",
        "temperature": detail.get("temperature"),
        "reasoning_effort": detail.get("reasoning_effort") or "",
        "speed": detail.get("speed"),
        "format": detail.get("format") or "",
        "timing": timing,
        "display": " · ".join(str(part) for part in parts),
    }


def global_defaults(refresh=False):
    now = time.monotonic()
    cached = _cache.get("globals")
    if not refresh and cached and now - _cache.get("globals_at", 0.0) < 3.0:
        return json.loads(json.dumps(cached))
    with _global_lock:
        now = time.monotonic()
        cached = _cache.get("globals")
        if not refresh and cached and now - _cache.get("globals_at", 0.0) < 3.0:
            return json.loads(json.dumps(cached))
        mapped = {}
        for kind in ("llm", "tts", "stt"):
            try:
                mapped[kind] = _global_one(kind)
            except Exception as exc:
                mapped[kind] = {"available": False, "display": "Unavailable",
                                "error": _safe_error(str(exc))}
        _cache["globals"] = mapped
        _cache["globals_at"] = now
        return json.loads(json.dumps(mapped))


async def global_defaults_async(refresh=False):
    return await asyncio.to_thread(global_defaults, refresh)


def global_default(kind, refresh=False):
    if refresh:
        try:
            value = _global_one(kind)
        except Exception as exc:
            value = {"available": False, "display": "Unavailable",
                     "error": _safe_error(str(exc))}
        with _global_lock:
            cached = json.loads(json.dumps(_cache.get("globals") or {}))
            cached[kind] = value
            _cache["globals"] = cached
            _cache["globals_at"] = time.monotonic()
    else:
        value = global_defaults().get(kind) or {}
    if not value.get("available"):
        raise RuntimeError(value.get("error") or f"EnConvo global {kind} is unavailable")
    return value


def _route_begin(kind, mapped):
    route = {
        "provider": mapped.get("provider") or "",
        "model": mapped.get("model") or "",
        "display": mapped.get("display") or "EnConvo global default",
        "command_key": mapped.get("command_key") or "",
        "state": "routing",
        "started_at": time.time(),
    }
    with _route_lock:
        _routes[kind] = route


def _route_finish(kind, state, **details):
    with _route_lock:
        route = dict(_routes.get(kind) or {})
        route.update(details)
        route["state"] = state
        route["finished_at"] = time.time()
        _routes[kind] = route


def last_route(kind):
    with _route_lock:
        return json.loads(json.dumps(_routes.get(kind) or {}))


def _direct_route(kind, config):
    provider = config.get("provider") or ""
    provider_spec = spec(kind, provider) or {}
    details = []
    if config.get("model"):
        details.append(str(config["model"]))
    if kind == "tts" and config.get("voice"):
        details.append(str(config["voice"]))
    label = provider_spec.get("label") or provider.replace("_", " ").title()
    return {
        "provider": provider,
        "model": config.get("model") or "",
        "display": " · ".join([label, *details]),
        "command_key": "",
    }


# ---------------------------------------------------------------- model lists

OPENAI_TTS_VOICES = [
    "alloy", "ash", "ballad", "cedar", "coral", "echo", "fable", "marin",
    "nova", "onyx", "sage", "shimmer", "verse",
]
GEMINI_TTS_VOICES = [
    "Achernar", "Aoede", "Autonoe", "Callirrhoe", "Charon", "Despina",
    "Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore", "Laomedeia",
    "Leda", "Orus", "Puck", "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager",
    "Schedar", "Sulafat", "Umbriel", "Vindemiatrix", "Zephyr", "Zubenelgenubi",
]


def _filter_models(kind, provider, values):
    values = sorted(set(str(value) for value in values if value))
    if kind == "llm":
        # Exclusion only, never a name allowlist: a prefix list ages the
        # moment the vendor ships a new family (OpenAI's gpt-* allowlist
        # would have hidden every non-gpt-named model). Drop what clearly
        # is not a chat model and keep everything else.
        excluded = ("audio", "realtime", "tts", "transcribe", "whisper", "embedding",
                    "image", "moderation", "dall-e", "sora", "davinci", "babbage")
        return [value for value in values
                if not any(word in value.lower() for word in excluded)]
    if kind == "tts" and provider == "openai":
        return [value for value in values
                if "tts" in value.lower() or "audio" in value.lower()]
    if kind == "stt" and provider in {"openai", "groq", "custom"}:
        return [value for value in values
                if "transcribe" in value.lower() or "whisper" in value.lower()]
    return values


async def list_models(kind, c):
    """Ask the provider what it actually offers after credentials are accepted."""
    p = c.get("provider")
    if p == "enconvo":
        mapped = await asyncio.to_thread(global_default, kind, True)
        model = mapped.get("model") or ""
        return [model] if model else []
    provider_spec = spec(kind, p) or {}
    base, key = _base(kind, c), (c.get("api_key") or "")
    if provider_spec.get("key") and not key:
        raise RuntimeError(f"{provider_spec.get('label', p)} API key is required")
    try:
        async with httpx.AsyncClient(timeout=20) as x:
            if p == "ollama":
                r = await x.get(f"{base or 'http://localhost:11434'}/api/tags")
                r.raise_for_status()
                return sorted(m["name"] for m in r.json().get("models", []))
            if p == "anthropic":
                r = await x.get(f"{base}/models", headers={
                    "x-api-key": key, "anthropic-version": "2023-06-01"})
                r.raise_for_status()
                return [m["id"] for m in r.json().get("data", [])]
            if p == "gemini":
                r = await x.get(f"{base}/models", params={"key": key, "pageSize": 200})
                r.raise_for_status()
                out = []
                for model in r.json().get("models", []):
                    methods = model.get("supportedGenerationMethods") or []
                    name = model["name"].split("/")[-1]
                    if kind in {"llm", "stt"} and "generateContent" not in methods:
                        continue
                    if kind == "tts" and "tts" not in name.lower():
                        continue
                    if kind in {"llm", "stt"} and any(
                            word in name.lower() for word in ("tts", "embedding", "imagen", "veo")):
                        continue
                    out.append(name)
                return sorted(set(out))
            if p == "elevenlabs":
                r = await x.get(f"{base}/voices", headers={"xi-api-key": key})
                r.raise_for_status()
                return [f"{v['voice_id']}  ({v.get('name', '')})"
                        for v in r.json().get("voices", [])]
            if p == "edge":
                return await _edge_voices()
            if p == "system":
                out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
                return [line.split()[0] for line in out.splitlines() if line.strip()]
            if p == "soniox":
                # Listing doubles as the credentials check everywhere else,
                # so prove the key with a real handshake first.
                await _soniox_validate(c)
                return ["stt-rt-v5"]
            if p == "mlx_whisper":
                return ["mlx-community/whisper-large-v3-turbo",
                        "mlx-community/whisper-large-v3-mlx",
                        "mlx-community/whisper-medium-mlx",
                        "mlx-community/whisper-small-mlx",
                        "mlx-community/whisper-tiny-mlx"]
            if p == "kokoro":
                return ["af_aoede", "af_heart", "af_bella", "af_nicole", "af_sarah",
                        "af_sky", "am_adam", "am_michael", "bf_emma", "bf_isabella",
                        "bm_george", "bm_lewis"]
            h = {"Authorization": f"Bearer {key}"} if key else {}
            r = await x.get(f"{base}/models", headers=h)
            r.raise_for_status()
            values = [model["id"] for model in r.json().get("data", [])]
            return _filter_models(kind, p, values)
    except httpx.HTTPStatusError as exc:
        # The raw httpx message buries the status behind a docs URL and the
        # tail-keeping redactor then keeps only the URL - say what actually
        # failed, plainly.
        status = exc.response.status_code
        if status in (401, 403):
            reason = "the provider rejected this API key"
        elif status == 404:
            reason = "no models endpoint at this address - check the Endpoint field"
        elif status == 429:
            reason = "the provider is rate-limiting this key - try again shortly"
        else:
            reason = "the provider refused the request"
        raise RuntimeError(f"{p}: {reason} (HTTP {status})") from exc
    except Exception as exc:
        raise RuntimeError(f"{p}: {_safe_error(str(exc))}") from exc


async def list_choices(kind, c):
    p = c.get("provider")
    if p == "enconvo":
        mapped = await asyncio.to_thread(global_default, kind, True)
        return {"models": [mapped["model"]] if mapped.get("model") else [],
                "voices": [mapped["voice"]] if mapped.get("voice") else []}
    if kind != "tts":
        return {"models": await list_models(kind, c), "voices": []}
    if p == "elevenlabs":
        base, key = _base(kind, c), c.get("api_key") or ""
        if not key:
            raise RuntimeError("ElevenLabs API key is required")
        async with httpx.AsyncClient(timeout=20) as client:
            voice_response, model_response = await asyncio.gather(
                client.get(f"{base}/voices", headers={"xi-api-key": key}),
                client.get(f"{base}/models", headers={"xi-api-key": key}))
            voice_response.raise_for_status()
            model_response.raise_for_status()
        voices = [f"{voice['voice_id']}  ({voice.get('name', '')})"
                  for voice in voice_response.json().get("voices", [])]
        model_payload = model_response.json()
        model_rows = model_payload if isinstance(model_payload, list) else \
            model_payload.get("models", [])
        models = [row.get("model_id") or row.get("id") for row in model_rows]
        return {"models": sorted(value for value in models if value), "voices": voices}
    if p in {"kokoro", "edge", "system"}:
        return {"models": [], "voices": await list_models(kind, c)}
    models = await list_models(kind, c)
    voices = OPENAI_TTS_VOICES if p == "openai" else \
        GEMINI_TTS_VOICES if p == "gemini" else []
    return {"models": models, "voices": voices}


async def _edge_voices():
    try:
        import edge_tts
        vs = await edge_tts.list_voices()
        return sorted(v["ShortName"] for v in vs)
    except Exception:
        return ["en-US-AvaNeural", "en-US-EmmaNeural", "en-US-JennyNeural",
                "en-GB-SoniaNeural", "en-US-AndrewNeural", "en-US-BrianNeural"]


# ---------------------------------------------------------------- think


def _enconvo_text(payload):
    text = payload.get("text") if isinstance(payload, dict) else ""
    if text:
        return str(text).strip()
    message = payload.get("message") if isinstance(payload, dict) else {}
    content = message.get("content") if isinstance(message, dict) else []
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(str(item.get("text") or "") for item in content
                       if isinstance(item, dict)).strip()
    return ""


def _enconvo_route_evidence(payload):
    if not isinstance(payload, dict):
        return {"provider": "", "model": ""}
    message = payload.get("message")
    additional = message.get("additional") if isinstance(message, dict) else {}
    metadata = additional.get("metadata") if isinstance(additional, dict) else {}
    usage = metadata.get("llmUsage") if isinstance(metadata, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    provider = usage.get("provider") or ""
    model = usage.get("model") or ""
    consumption = additional.get("consumption") if isinstance(additional, dict) else None
    if isinstance(consumption, str):
        try:
            consumption = json.loads(consumption)
        except json.JSONDecodeError:
            consumption = {}
    if isinstance(consumption, dict):
        provider = provider or consumption.get("provider") or ""
        model = model or consumption.get("model") or ""
    if isinstance(model, dict):
        model = model.get("value") or model.get("id") or ""
    return {"provider": str(provider), "model": str(model)}


def _provider_identity(value):
    value = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return {"openai": "open_ai", "xai": "x_ai"}.get(value, value)


async def _run_enconvo_api_async(path, payload, timeout=180):
    if not re.fullmatch(r"llm/features/[A-Za-z0-9_.-]+/chat", path or ""):
        raise RuntimeError("Invalid EnConvo provider route")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{ENCONVO_API}/{path}", json=payload)
            response.raise_for_status()
            result = response.json()
    except Exception as exc:
        raise RuntimeError(f"EnConvo provider request failed: {_safe_error(str(exc))}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("EnConvo returned an invalid model response")
    if result.get("error"):
        error = result["error"]
        if isinstance(error, dict):
            error = error.get("message") or error.get("error") or "provider error"
        raise RuntimeError(f"EnConvo provider error: {_safe_error(str(error))}")
    return result


async def _enconvo_chat(messages, c, system=""):
    mapped = await asyncio.to_thread(global_default, "llm", True)
    _route_begin("llm", mapped)
    try:
        provider = str(mapped.get("provider") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
            raise RuntimeError("EnConvo returned an invalid LLM provider")
        request = {"messages": messages}
        if mapped.get("model"):
            request["modelName"] = str(mapped["model"])
        try:
            max_tokens = int(c.get("max_tokens", DEFAULTS["llm"]["max_tokens"]))
        except (TypeError, ValueError):
            max_tokens = DEFAULTS["llm"]["max_tokens"]
        request["modelParams"] = {
            "maxOutputTokens": max(1, min(max_tokens, 32768)),
        }
        if provider == "ollama" and mapped.get("model"):
            request["modelName_preferences"] = {
                str(mapped["model"]): {"reasoning_effort": "disabled"},
            }
        temperature = mapped.get("temperature")
        if temperature is not None:
            request["temperature"] = temperature
        if system:
            request["system"] = system
        payload = await _run_enconvo_api_async(
            f"llm/features/{provider}/chat", request, timeout=180)
        text = _enconvo_text(payload)
        if not text:
            raise RuntimeError("EnConvo returned an empty model response")

        actual = _enconvo_route_evidence(payload)
        expected_model = str(mapped.get("model") or "")
        provider_matches = _provider_identity(actual["provider"]) == \
            _provider_identity(provider)
        model_matches = not expected_model or actual["model"] == expected_model
        if not actual["provider"] or (expected_model and not actual["model"]):
            raise RuntimeError("EnConvo did not report the provider and model it executed")
        if not provider_matches or not model_matches:
            raise RuntimeError(
                "EnConvo executed "
                f"{actual['provider'] or 'an unknown provider'} · "
                f"{actual['model'] or 'an unknown model'} instead of "
                f"{provider} · {expected_model or 'the selected model'}")
        _route_finish("llm", "success", actual_provider=actual["provider"],
                      actual_model=actual["model"])
        return text
    except Exception as exc:
        actual = _enconvo_route_evidence(locals().get("payload"))
        details = {"error": _safe_error(str(exc))}
        if actual["provider"]:
            details["actual_provider"] = actual["provider"]
        if actual["model"]:
            details["actual_model"] = actual["model"]
        _route_finish("llm", "failed", **details)
        raise


async def chat(messages, c, system=""):
    if c.get("provider") == "enconvo":
        return await _enconvo_chat(messages, c, system)
    _route_begin("llm", _direct_route("llm", c))
    try:
        text = await _chat_direct(messages, c, system)
        if not text:
            raise RuntimeError("the selected model returned an empty response")
        _route_finish("llm", "success")
        return text
    except Exception:
        _route_finish("llm", "failed")
        raise


# A provider with a key but no model chosen is the commonest way to a
# dead chat: the request goes out with model:"" and the provider rejects
# it, which surfaced as "ROUTE FAILED - my model is not answering" with
# nothing naming the actual cause (owner, xAI, 2026-08-03). Picking the
# house model is better than failing, and the UI still shows what ran.
FALLBACK_MODEL = {
    "openai": "gpt-5-mini", "xai": "grok-3-mini", "anthropic": "claude-sonnet-5",
    "gemini": "gemini-2.5-flash", "groq": "llama-3.3-70b-versatile",
    "deepseek": "deepseek-chat", "mistral": "mistral-small-latest",
    "openrouter": "openai/gpt-5-mini", "moonshot": "kimi-k2-0905-preview",
    "cerebras": "llama-3.3-70b", "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "fireworks": "accounts/fireworks/models/llama-v3p3-70b-instruct",
    "perplexity": "sonar", "qwen": "qwen-plus", "zhipu": "glm-4.6",
    "minimax_llm": "MiniMax-Text-01", "nvidia": "meta/llama-3.3-70b-instruct",
}


async def _chat_direct(messages, c, system=""):
    p = c.get("provider")
    base, key = _base("llm", c), c.get("api_key") or ""
    model = (c.get("model") or "").strip() or FALLBACK_MODEL.get(p, "")
    temp = float(c.get("temperature", 0.8))
    maxtok = int(c.get("max_tokens", 160))

    async with httpx.AsyncClient(timeout=180) as x:
        if p == "ollama":
            msgs = ([{"role": "system", "content": system}] if system else []) + messages
            r = await x.post(f"{base or 'http://localhost:11434'}/api/chat", json={
                "model": model, "messages": msgs, "stream": False, "think": False,
                "options": {"temperature": temp, "num_predict": maxtok}})
            r.raise_for_status()
            return (r.json().get("message", {}).get("content") or "").strip()

        if p == "anthropic":
            r = await x.post(f"{base}/messages", headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"}, json={
                "model": model, "max_tokens": maxtok, "temperature": temp,
                "system": system, "messages": messages})
            r.raise_for_status()
            return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()

        if p == "gemini":
            contents = [{"role": "model" if m["role"] == "assistant" else "user",
                         "parts": [{"text": m["content"]}]} for m in messages]
            body = {"contents": contents,
                    "generationConfig": {"temperature": temp, "maxOutputTokens": maxtok}}
            if system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            r = await x.post(f"{base}/models/{model}:generateContent",
                             params={"key": key}, json=body)
            r.raise_for_status()
            cands = r.json().get("candidates") or [{}]
            parts = (cands[0].get("content") or {}).get("parts") or []
            return "".join(q.get("text", "") for q in parts).strip()

        # OpenAI-compatible
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        h = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        r = await x.post(f"{base}/chat/completions", headers=h, json={
            "model": model, "messages": msgs,
            "temperature": temp, "max_completion_tokens": maxtok})
        if r.status_code == 400:            # older servers reject the newer field name
            r = await x.post(f"{base}/chat/completions", headers=h, json={
                "model": model, "messages": msgs,
                "temperature": temp, "max_tokens": maxtok})
        r.raise_for_status()
        ch = r.json().get("choices") or [{}]
        return ((ch[0].get("message") or {}).get("content") or "").strip()


# ---------------------------------------------------------------- audio utils

def _ff(raw, in_ext):
    """Anything -> float32 mono at SR. Every engine returns a different container
    (mp3, aiff, raw PCM, opus); normalising here means the browser, the viseme
    track and the duration all see one format."""
    extension = in_ext if re.fullmatch(r"\.[a-z0-9]{1,8}", in_ext or "") else ".bin"
    with tempfile.TemporaryDirectory(prefix="vivieen-audio-") as work_dir:
        src = os.path.join(work_dir, f"input{extension}")
        dst = os.path.join(work_dir, "output.wav")
        with open(src, "wb") as handle:
            handle.write(raw)
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
             "-ar", str(SR), "-ac", "1", dst], capture_output=True, text=True)
        if result.returncode or not os.path.isfile(dst):
            raise RuntimeError(safe_error(result.stderr or "audio conversion failed"))
        import soundfile as sf
        samples, _ = sf.read(dst, dtype="float32")
        return np.asarray(samples).reshape(-1)


def to_wav(y):
    import soundfile as sf
    peak = float(np.max(np.abs(y))) if len(y) else 0.0
    if peak > 0:
        y = (y / peak) * 0.92
    buf = io.BytesIO()
    sf.write(buf, y.astype(np.float32), SR, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# ---------------------------------------------------------------- speak

def _kokoro(c):
    with _lock:
        if _cache["tts"] is None:
            from kokoro import KPipeline
            _cache["tts"] = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    return _cache["tts"]


def _system_say(text, voice, speed):
    with tempfile.TemporaryDirectory(prefix="vivieen-say-") as work_dir:
        aiff = os.path.join(work_dir, "speech.aiff")
        cmd = ["say", "-o", aiff]
        if voice:
            cmd += ["-v", voice]
        cmd += ["-r", str(int(180 * speed)), text]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode or not os.path.isfile(aiff):
            raise RuntimeError(safe_error(result.stderr or "macOS speech failed"))
        with open(aiff, "rb") as handle:
            raw = handle.read()
    return _ff(raw, ".aiff")


def _kokoro_audio(text, c, voice, speed):
    results = list(_kokoro(c)(text, voice=voice or "af_heart", speed=speed))
    output = []
    for result in results:
        audio = result.audio
        audio = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        output.append(np.asarray(audio, dtype=np.float32).reshape(-1))
    samples = np.concatenate(output) if output else np.zeros(0, np.float32)
    return samples, ("kokoro", results)


async def _enconvo_speak(text, c):
    mapped = await asyncio.to_thread(global_default, "tts", True)
    _route_begin("tts", mapped)
    output_dir = tempfile.mkdtemp(prefix="vivieen-enconvo-tts-")
    try:
        payload = await _run_enconvo_async(
            ["tts", "tts", "--text", text, "--speed", str(mapped.get("speed") or 1),
             "--audio_file_name", "speech", "--output_dir", output_dir], timeout=180)
        path = os.path.abspath(str(payload.get("path") or ""))
        if os.path.dirname(path) != output_dir or not os.path.isfile(path):
            raise RuntimeError("EnConvo did not return the requested speech file")
        with open(path, "rb") as handle:
            raw = handle.read()
        _route_finish("tts", "success")
        return await asyncio.to_thread(
            _ff, raw, os.path.splitext(path)[1] or ".wav"), None
    except Exception:
        _route_finish("tts", "failed")
        raise
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


async def speak(text, c):
    """-> (samples, alignment) where alignment is either Kokoro Result objects
    (exact), a word/char timing list, or None (estimate from the text)."""
    if c.get("provider") == "enconvo":
        return await _enconvo_speak(text, c)
    _route_begin("tts", _direct_route("tts", c))
    try:
        result = await _speak_direct(text, c)
        _route_finish("tts", "success")
        return result
    except Exception:
        _route_finish("tts", "failed")
        raise


async def _speak_direct(text, c):
    p = c.get("provider")
    base, key = _base("tts", c), c.get("api_key") or ""
    voice = c.get("voice") or ""
    model = c.get("model") or ""
    speed = float(c.get("speed", 1.0))

    # The synthesis leaves BLOCK - Kokoro is a torch forward, say and _ff
    # are subprocess.run - and speak() is awaited from the live-talk
    # socket loop. Run on the event loop they froze the mic: nothing read
    # audio during synthesis, so barge-in could not fire until she had
    # already finished the sentence (#24 prerequisite). to_thread keeps
    # the loop listening while the leaf grinds.
    if p == "kokoro":
        return await asyncio.to_thread(_kokoro_audio, text, c, voice, speed)

    if p == "system":
        return await asyncio.to_thread(_system_say, text, voice, speed), None

    if p == "edge":
        import edge_tts
        rate = f"{int(round((speed - 1) * 100)):+d}%"
        com = edge_tts.Communicate(text, voice or "en-US-AvaNeural", rate=rate)
        buf, words, sents = bytearray(), [], []
        async for ch in com.stream():
            t = ch.get("type")
            if t == "audio":
                buf += ch["data"]
            elif t == "WordBoundary":
                words.append((ch["offset"] / 1e7, ch["duration"] / 1e7, ch["text"]))
            elif t == "SentenceBoundary":
                sents.append((ch["offset"] / 1e7, ch["duration"] / 1e7, ch["text"]))
        y = await asyncio.to_thread(_ff, bytes(buf), ".mp3")
        # edge-tts 7.x streams SentenceBoundary and no WordBoundary at all, so
        # asking for the finer grain and assuming it arrived produced an EMPTY
        # track - a "timed" tier that was worse than the estimate it replaced.
        # Degrade one rung at a time instead of to nothing.
        if words:
            return y, ("words", words)
        if sents:
            return y, ("spans", sents)
        return y, None

    if p == "elevenlabs":
        vid = (voice or "").split()[0]
        async with httpx.AsyncClient(timeout=180) as x:
            r = await x.post(f"{base}/text-to-speech/{vid}/with-timestamps",
                             headers={"xi-api-key": key},
                             json={"text": text,
                                   "model_id": model or "eleven_turbo_v2_5"})
            r.raise_for_status()
            j = r.json()
        y = await asyncio.to_thread(_ff, base64.b64decode(j["audio_base64"]),
                                    ".mp3")
        al = j.get("alignment") or {}
        chars = list(zip(al.get("characters") or [],
                         al.get("character_start_times_seconds") or [],
                         al.get("character_end_times_seconds") or []))
        return y, (("chars", chars) if chars else None)

    if p == "gemini":
        async with httpx.AsyncClient(timeout=180) as x:
            r = await x.post(f"{base}/models/{model or 'gemini-2.5-flash-preview-tts'}:generateContent",
                             params={"key": key}, json={
                    "contents": [{"parts": [{"text": text}]}],
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                            "voiceName": voice or "Kore"}}}}})
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
        b = next(q["inlineData"]["data"] for q in parts if "inlineData" in q)
        pcm = np.frombuffer(base64.b64decode(b), dtype=np.int16).astype(np.float32) / 32768
        # Gemini returns headerless 24k mono PCM, which is already our rate
        return pcm, None

    if p == "deepgram":
        async with httpx.AsyncClient(timeout=180) as x:
            r = await x.post(
                f"{base}/speak",
                params={"model": model or voice or "aura-2-thalia-en",
                        "encoding": "linear16", "sample_rate": str(SR)},
                headers={"Authorization": f"Token {key}",
                         "Content-Type": "application/json"},
                json={"text": text})
            r.raise_for_status()
            return np.frombuffer(r.content, "<i2").astype(np.float32) / 32768.0, None

    if p == "cartesia":
        async with httpx.AsyncClient(timeout=180) as x:
            r = await x.post(
                f"{base}/tts/bytes",
                headers={"Authorization": f"Bearer {key}",
                         "Cartesia-Version": "2025-04-16"},
                json={"model_id": model or "sonic-3",
                      "transcript": text,
                      "voice": {"mode": "id",
                                "id": voice or "694f9389-aac1-45b6-b726-9d9369183238"},
                      "output_format": {"container": "raw",
                                        "encoding": "pcm_s16le",
                                        "sample_rate": SR}})
            r.raise_for_status()
            return np.frombuffer(r.content, "<i2").astype(np.float32) / 32768.0, None

    # OpenAI /v1/audio/speech (and anything that copies it)
    async with httpx.AsyncClient(timeout=180) as x:
        r = await x.post(f"{base}/audio/speech",
                         headers={"Authorization": f"Bearer {key}"},
                         json={"model": model or "gpt-4o-mini-tts",
                               "input": text, "voice": voice or "alloy",
                               "response_format": "wav", "speed": speed})
        r.raise_for_status()
        return await asyncio.to_thread(_ff, r.content, ".wav"), None


# ---------------------------------------------------------------- hear


async def _enconvo_hear(raw, filename, c):
    mapped = await asyncio.to_thread(global_default, "stt", True)
    _route_begin("stt", mapped)
    extension = os.path.splitext(filename or "")[1].lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension or ""):
        extension = ".webm"
    work_dir = tempfile.mkdtemp(prefix="vivieen-enconvo-stt-")
    path = os.path.join(work_dir, f"speech{extension}")
    try:
        with open(path, "wb") as handle:
            handle.write(raw)
        args = ["transcribe", "transcribe", "--filePaths", path]
        if mapped.get("model"):
            args.extend(["--model", str(mapped["model"])])
        language = mapped.get("language") or ""
        if language not in {"", "auto"}:
            args.extend(["--languages", language])
        payload = await _run_enconvo_async(args, timeout=180)
        text = payload.get("content") if isinstance(payload, dict) else ""
        if not text and isinstance(payload, dict):
            rows = payload.get("results") or []
            if rows and isinstance(rows[0], dict):
                text = rows[0].get("text") or ""
        _route_finish("stt", "success")
        return str(text or "").strip()
    except Exception:
        _route_finish("stt", "failed")
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# The one realtime STT websocket endpoint; the /stt/stream bridge in
# server/app.py speaks the same protocol for live dictation.
SONIOX_RT_WSS = "wss://stt-rt.soniox.com/transcribe-websocket"


def _soniox_config(c):
    model = str(c.get("model") or "")
    if not model.startswith("stt-rt"):
        model = "stt-rt-v5"
    config = {"api_key": c.get("api_key") or "", "model": model,
              "audio_format": "auto"}
    language = str(c.get("language") or "")
    if language and language != "auto":
        config["language_hints"] = [language]
    return config


async def _soniox_stream(config, frames):
    """Run one take through Soniox realtime and return the final transcript.

    Measured live: finalisation requires an empty TEXT frame - the
    documented empty-binary alternative just times out.
    """
    import websockets
    finals = []
    async with websockets.connect(
            SONIOX_RT_WSS, max_size=1 << 22, open_timeout=10) as upstream:
        await upstream.send(json.dumps(config))
        for frame in frames:
            await upstream.send(frame)
        await upstream.send("")
        async for message in upstream:
            payload = json.loads(message)
            if payload.get("error_code") or payload.get("error_message"):
                raise RuntimeError(str(
                    payload.get("error_message") or "Soniox error")[:200])
            for token in payload.get("tokens") or []:
                if token.get("is_final"):
                    finals.append(str(token.get("text") or ""))
            if payload.get("finished"):
                break
    return "".join(finals).strip()


async def _soniox_validate(c):
    """A key is proven by a full round trip: config, end frame, response.

    An empty take draws "No audio received." - which means authentication
    already succeeded and the session reached the audio stage, so that
    specific complaint counts as a pass. A bad key fails before it.
    """
    try:
        await _soniox_stream(_soniox_config(c), [])
    except RuntimeError as error:
        if "no audio" in str(error).lower():
            return True
        raise
    return True


async def hear(raw, filename, c):
    if c.get("provider") == "enconvo":
        return await _enconvo_hear(raw, filename, c)
    _route_begin("stt", _direct_route("stt", c))
    try:
        text = await _hear_direct(raw, filename, c)
        _route_finish("stt", "success")
        return text
    except Exception:
        _route_finish("stt", "failed")
        raise


async def _hear_direct(raw, filename, c):
    p = c.get("provider")
    base, key = _base("stt", c), c.get("api_key") or ""
    model, lang = c.get("model") or "", c.get("language") or "en"

    if p == "soniox":
        if not key:
            raise RuntimeError("Soniox needs an API key")
        # One whole take through the realtime socket: the same protocol the
        # live-dictation bridge uses, so batch and streaming always agree.
        frames = [raw[start:start + 65536]
                  for start in range(0, len(raw), 65536)]
        return await _soniox_stream(_soniox_config(c), frames)

    if p == "mlx_whisper":
        import mlx_whisper
        extension = os.path.splitext(filename)[1].lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension or ""):
            extension = ".webm"
        with tempfile.TemporaryDirectory(prefix="vivieen-whisper-") as work_dir:
            src = os.path.join(work_dir, f"speech{extension}")
            wav = os.path.join(work_dir, "speech.wav")
            with open(src, "wb") as handle:
                handle.write(raw)
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                 "-ar", "16000", "-ac", "1", wav], capture_output=True, text=True)
            if result.returncode or not os.path.isfile(wav):
                raise RuntimeError(safe_error(result.stderr or "audio conversion failed"))
            response = mlx_whisper.transcribe(
                wav, path_or_hf_repo=model or DEFAULTS["stt"]["model"],
                language=None if lang in ("", "auto") else lang)
            return (response.get("text") or "").strip()

    if p == "gemini":
        async with httpx.AsyncClient(timeout=120) as x:
            r = await x.post(f"{base}/models/{model or 'gemini-2.5-flash'}:generateContent",
                             params={"key": key}, json={"contents": [{"parts": [
                    {"text": "Transcribe this audio verbatim. Reply with the transcript only."},
                    {"inlineData": {"mimeType": "audio/webm",
                                    "data": base64.b64encode(raw).decode()}}]}]})
            r.raise_for_status()
            parts = (r.json()["candidates"][0].get("content") or {}).get("parts") or []
        return "".join(q.get("text", "") for q in parts).strip()

    # OpenAI-compatible multipart
    if p == "deepgram":
        params = {"model": model or "nova-3", "smart_format": "true"}
        if lang and lang != "auto":
            params["language"] = lang
        async with httpx.AsyncClient(timeout=120) as x:
            r = await x.post(f"{base}/listen", params=params,
                             headers={"Authorization": f"Token {key}",
                                      "Content-Type": "application/octet-stream"},
                             content=raw)
            r.raise_for_status()
            alternatives = (((r.json().get("results") or {}).get("channels")
                             or [{}])[0].get("alternatives") or [{}])
            return (alternatives[0].get("transcript") or "").strip()

    if p == "elevenlabs":
        async with httpx.AsyncClient(timeout=120) as x:
            r = await x.post(f"{base}/speech-to-text",
                             headers={"xi-api-key": key},
                             files={"file": (filename or "audio.webm", raw)},
                             data={"model_id": model or "scribe_v1"})
            r.raise_for_status()
            return (r.json().get("text") or "").strip()

    files = {"file": (filename or "audio.webm", raw, "application/octet-stream")}
    data = {"model": model or "whisper-1"}
    if lang and lang != "auto":
        data["language"] = lang
    async with httpx.AsyncClient(timeout=120) as x:
        r = await x.post(f"{base}/audio/transcriptions",
                         headers={"Authorization": f"Bearer {key}"},
                         files=files, data=data)
        r.raise_for_status()
        return (r.json().get("text") or "").strip()


# ---------------------------------------------------------------- test

async def test(kind, c):
    try:
        if kind == "llm":
            t = await chat([{"role": "user", "content": "Reply with exactly: ok"}], c,
                           system="You reply with one word.")
            return dict(ok=True, detail=(t or "")[:80] or "empty reply")
        if kind == "tts":
            y, al = await speak("Testing, one two.", c)
            kind_of = al[0] if al else "estimated"
            return dict(ok=len(y) > 0,
                        detail=f"{len(y)/SR:.1f}s of audio, timing: {kind_of}")
        if kind == "stt":
            y = np.zeros(SR // 2, np.float32)
            return dict(ok=True, detail="reachable (speak to test properly)") \
                if c.get("provider") == "mlx_whisper" else \
                dict(ok=bool(await list_models("stt", c)), detail="credentials accepted")
        if kind == "image":
            # A real (tiny) render: the only test that proves the whole path.
            import media_gen
            path = await media_gen.generate_image(
                "a single small blue circle on white, minimal test pattern", c)
            return dict(ok=os.path.isfile(path),
                        detail=f"rendered {os.path.getsize(path) // 1024} KB")
        if kind == "video":
            # Credentials only - a real render costs real money.
            import media_gen
            p = c.get("provider")
            if p == "enconvo":
                name = await media_gen._enconvo_default_feature("video_create")
                return dict(ok=True, detail=f"EnConvo default: {name}")
            if not (c.get("api_key") or ""):
                return dict(ok=False, detail="no API key stored")
            checks = {
                "openai": ("https://api.openai.com/v1/models",
                           {"Authorization": f"Bearer {c.get('api_key')}"}),
                "gemini": ("https://generativelanguage.googleapis.com/v1beta/"
                           f"models?key={c.get('api_key')}", {}),
                "luma": ("https://api.lumalabs.ai/dream-machine/v1/generations"
                         "?limit=1",
                         {"Authorization": f"Bearer {c.get('api_key')}"}),
                "runway": ("https://api.dev.runwayml.com/v1/organization",
                           {"Authorization": f"Bearer {c.get('api_key')}",
                            "X-Runway-Version": "2024-11-06"}),
            }
            url, headers = checks.get(p, (None, None))
            if not url:
                return dict(ok=False, detail=f"unknown provider {p}")
            async with httpx.AsyncClient(timeout=30) as x:
                r = await x.get(url, headers=headers)
                return dict(ok=r.status_code < 400,
                            detail="credentials accepted" if r.status_code < 400
                            else f"provider said {r.status_code}")
    except Exception as e:
        return dict(ok=False, detail=safe_error(e))
    return dict(ok=False, detail="unknown check")
