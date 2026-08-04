"""Her own hands, decoupled: image and video generation as provider slots.

Same grammar as the LLM/TTS/STT slots - "enconvo" inherits EnConvo's
Global Provider (credentials stay inside EnConvo, called through its
local gateway), everything else is bring-your-own-key against the
provider's public API. Every adapter ends the same way: a real file
under ~/Downloads/Vivieen, which the engine already serves to the phone
as a playable card.
"""
import asyncio
import base64
import json
import os
import re
import time
import urllib.request

import httpx

OUT_DIR = os.path.expanduser("~/Downloads/Vivieen")
ENCONVO_API = "http://127.0.0.1:54535/api"


def _out(prefix, suffix):
    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.join(OUT_DIR, f"{prefix}-{int(time.time() * 1000)}{suffix}")


def _write(prefix, suffix, data):
    path = _out(prefix, suffix)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


async def _download(url, prefix, suffix, headers=None):
    async with httpx.AsyncClient(timeout=600, follow_redirects=True) as x:
        r = await x.get(url, headers=headers or {})
        r.raise_for_status()
        return _write(prefix, suffix, r.content)


def _enconvo_call(path, params, timeout):
    request = urllib.request.Request(
        f"{ENCONVO_API}/{path}", method="POST",
        data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as feed:
        return json.loads(feed.read().decode() or "{}")


async def _enconvo_default_feature(extension):
    rows = await asyncio.to_thread(
        _enconvo_call, f"{extension}/list_providers", {}, 20)
    chosen = next((r for r in rows if r.get("isDefault")), rows[0] if rows else None)
    if not chosen:
        raise RuntimeError(f"EnConvo has no {extension} provider configured")
    return chosen["name"]


async def _enconvo_generate(extension, prompt, timeout):
    # The default provider's NAME is not always its feature route:
    # "gemini-enconvo" (the Cloud Plan flavour) creates through
    # features/gemini/create. Try the name, then the stripped name.
    name = await _enconvo_default_feature(extension)
    last = "returned nothing"
    for candidate in dict.fromkeys([name, name.replace("-enconvo", "")]):
        try:
            answer = await asyncio.to_thread(
                _enconvo_call, f"{extension}/features/{candidate}/create",
                {"prompt": prompt}, timeout)
        except Exception as error:
            last = str(error)[:120]
            continue
        if answer.get("error"):
            last = str(answer["error"])[:120]
            continue
        paths = [p for p in (answer.get("paths") or []) if os.path.isfile(p)]
        if paths:
            return paths[0]
    raise RuntimeError(f"EnConvo {extension}: {last}")


# ---------------------------------------------------------------- image

async def generate_image(prompt, c):
    p = c.get("provider") or "enconvo"
    key = c.get("api_key") or ""
    model = c.get("model") or ""
    size = c.get("size") or "1024x1024"
    base = (c.get("base_url") or "").rstrip("/")

    if p == "enconvo":
        return await _enconvo_generate("image_create", prompt, 300)

    if p in ("openai", "xai", "together_image", "recraft"):
        defaults = {"openai": ("https://api.openai.com/v1", "gpt-image-1"),
                    "xai": ("https://api.x.ai/v1", "grok-2-image"),
                    "together_image": ("https://api.together.xyz/v1",
                                       "black-forest-labs/FLUX.1-schnell"),
                    "recraft": ("https://external.api.recraft.ai/v1", "recraftv3")}
        root, default_model = defaults[p]
        body = {"model": model or default_model, "prompt": prompt, "n": 1}
        if p == "openai":
            body["size"] = size
        if p in ("xai", "together_image"):
            body["response_format"] = "b64_json"
        async with httpx.AsyncClient(timeout=300) as x:
            r = await x.post(f"{base or root}/images/generations",
                             headers={"Authorization": f"Bearer {key}"}, json=body)
            r.raise_for_status()
            row = (r.json().get("data") or [{}])[0]
        if row.get("b64_json"):
            return _write("image", ".png", base64.b64decode(row["b64_json"]))
        if row.get("url"):
            return await _download(row["url"], "image", ".png")
        raise RuntimeError("image provider returned no data")

    if p == "gemini":
        root = base or "https://generativelanguage.googleapis.com/v1beta"
        name = model or "imagen-4.0-generate-001"
        async with httpx.AsyncClient(timeout=300) as x:
            r = await x.post(f"{root}/models/{name}:predict",
                             params={"key": key},
                             json={"instances": [{"prompt": prompt}],
                                   "parameters": {"sampleCount": 1}})
            r.raise_for_status()
            blob = (r.json().get("predictions") or [{}])[0]
        data = blob.get("bytesBase64Encoded")
        if not data:
            raise RuntimeError("Imagen returned no image")
        return _write("image", ".png", base64.b64decode(data))

    if p == "stability":
        root = base or "https://api.stability.ai"
        async with httpx.AsyncClient(timeout=300) as x:
            r = await x.post(f"{root}/v2beta/stable-image/generate/core",
                             headers={"Authorization": f"Bearer {key}",
                                      "Accept": "image/*"},
                             files={"prompt": (None, prompt),
                                    "output_format": (None, "png")})
            r.raise_for_status()
            return _write("image", ".png", r.content)

    if p == "bfl":
        root = base or "https://api.bfl.ai"
        async with httpx.AsyncClient(timeout=300) as x:
            r = await x.post(f"{root}/v1/{model or 'flux-pro-1.1'}",
                             headers={"x-key": key}, json={"prompt": prompt})
            r.raise_for_status()
            poll = r.json().get("polling_url") or ""
            for _ in range(90):
                await asyncio.sleep(2)
                status = (await x.get(poll, headers={"x-key": key})).json()
                if status.get("status") == "Ready":
                    return await _download(
                        (status.get("result") or {}).get("sample"),
                        "image", ".jpg")
                if status.get("status") in ("Error", "Content Moderated",
                                            "Request Moderated"):
                    raise RuntimeError(f"FLUX: {status.get('status')}")
        raise RuntimeError("FLUX timed out")

    raise RuntimeError(f"unknown image provider {p}")


# ---------------------------------------------------------------- video

async def _poll(x, method, url, headers, is_done, extract, every=5, cap=600):
    started = time.time()
    while time.time() - started < cap:
        await asyncio.sleep(every)
        r = await x.request(method, url, headers=headers)
        r.raise_for_status()
        body = r.json()
        if is_done(body):
            return extract(body)
    raise RuntimeError("video generation timed out")


async def generate_video(prompt, c):
    p = c.get("provider") or "enconvo"
    key = c.get("api_key") or ""
    model = c.get("model") or ""
    seconds = int(c.get("seconds") or 5)
    base = (c.get("base_url") or "").rstrip("/")

    if p == "enconvo":
        return await _enconvo_generate("video_create", prompt, 900)

    if p == "openai":
        root = base or "https://api.openai.com/v1"
        headers = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient(timeout=900) as x:
            r = await x.post(f"{root}/videos", headers=headers,
                             json={"model": model or "sora-2", "prompt": prompt,
                                   "seconds": str(seconds)})
            r.raise_for_status()
            vid = r.json().get("id")
            await _poll(x, "GET", f"{root}/videos/{vid}", headers,
                        lambda b: b.get("status") in ("completed", "failed"),
                        lambda b: b)
            done = (await x.get(f"{root}/videos/{vid}", headers=headers)).json()
            if done.get("status") != "completed":
                raise RuntimeError(f"Sora: {done.get('status')}")
            content = await x.get(f"{root}/videos/{vid}/content", headers=headers)
            content.raise_for_status()
            return _write("video", ".mp4", content.content)

    if p == "xai":
        # Grok Imagine video, current API. Measured 2026-08-04 against the
        # live service: submit returns a request_id, the job is polled at
        # /v1/videos/{id}, and 1080p is available on grok-imagine-video-1.5
        # only - the older grok-imagine-video answers "1080p video
        # resolution is not available for this model". EnConvo's own video
        # default is untouched; this runs only when the owner picks xAI.
        root = base or "https://api.x.ai/v1"
        headers = {"Authorization": f"Bearer {key}"}
        name = model or "grok-imagine-video-1.5"
        wanted = (c.get("resolution") or "1080p").lower()
        async with httpx.AsyncClient(timeout=900) as x:
            payload = {"model": name, "prompt": prompt, "resolution": wanted}
            r = await x.post(f"{root}/videos/generations",
                             headers=headers, json=payload)
            if r.status_code == 400 and "1080p" in r.text and wanted == "1080p":
                # Say which model can, rather than just refusing.
                raise RuntimeError(
                    f"{name} cannot do 1080p — use grok-imagine-video-1.5, "
                    "or set the resolution to 720p")
            r.raise_for_status()
            job = r.json().get("request_id") or r.json().get("id")
            if not job:
                raise RuntimeError("xAI accepted the job but named no id")
            body = await _poll(
                x, "GET", f"{root}/videos/{job}", headers,
                lambda b: b.get("status") not in ("pending", "processing"),
                lambda b: b)
            if body.get("status") != "done":
                raise RuntimeError(f"xAI video: {body.get('status')}")
            url = ((body.get("video") or {}).get("url") or "")
            if not url:
                raise RuntimeError("xAI reported done but returned no video")
            # The CDN refuses a bare client, same as the image one.
            return await _download(url, "video", ".mp4",
                                   {"User-Agent": "Mozilla/5.0 (Macintosh)"})

    if p == "gemini":
        root = base or "https://generativelanguage.googleapis.com/v1beta"
        name = model or "veo-3.1-fast-generate-001"
        async with httpx.AsyncClient(timeout=900) as x:
            r = await x.post(f"{root}/models/{name}:predictLongRunning",
                             params={"key": key},
                             json={"instances": [{"prompt": prompt}]})
            r.raise_for_status()
            op = r.json().get("name")
            body = await _poll(
                x, "GET", f"{root}/{op}?key={key}", {},
                lambda b: b.get("done"), lambda b: b)
            samples = (((body.get("response") or {})
                        .get("generateVideoResponse") or {})
                       .get("generatedSamples") or [])
            uri = ((samples[0].get("video") or {}).get("uri")
                   if samples else "")
            if not uri:
                raise RuntimeError("Veo returned no video")
            joiner = "&" if "?" in uri else "?"
            return await _download(f"{uri}{joiner}key={key}", "video", ".mp4")

    if p == "luma":
        root = base or "https://api.lumalabs.ai/dream-machine/v1"
        headers = {"Authorization": f"Bearer {key}"}
        async with httpx.AsyncClient(timeout=900) as x:
            r = await x.post(f"{root}/generations", headers=headers,
                             json={"prompt": prompt, "model": model or "ray-2"})
            r.raise_for_status()
            gid = r.json().get("id")
            body = await _poll(
                x, "GET", f"{root}/generations/{gid}", headers,
                lambda b: b.get("state") in ("completed", "failed"),
                lambda b: b)
            if body.get("state") != "completed":
                raise RuntimeError(f"Luma: {body.get('failure_reason')}")
            return await _download((body.get("assets") or {}).get("video"),
                                   "video", ".mp4")

    if p == "runway":
        root = base or "https://api.dev.runwayml.com/v1"
        headers = {"Authorization": f"Bearer {key}",
                   "X-Runway-Version": "2024-11-06"}
        async with httpx.AsyncClient(timeout=900) as x:
            r = await x.post(f"{root}/text_to_video", headers=headers,
                             json={"model": model or "veo3.1_fast",
                                   "promptText": prompt,
                                   "duration": seconds, "ratio": "1280:720"})
            r.raise_for_status()
            task = r.json().get("id")
            body = await _poll(
                x, "GET", f"{root}/tasks/{task}", headers,
                lambda b: b.get("status") in ("SUCCEEDED", "FAILED"),
                lambda b: b)
            if body.get("status") != "SUCCEEDED":
                raise RuntimeError(f"Runway: {body.get('failure') or 'failed'}")
            output = body.get("output") or []
            return await _download(output[0] if output else "", "video", ".mp4")

    raise RuntimeError(f"unknown video provider {p}")
