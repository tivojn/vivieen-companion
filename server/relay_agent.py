"""The Mac's half of the Vivieen Relay - the OpenClaw leaf.

The MacBook sits behind NAT, so it DIALS OUT: long-poll the relay's
to_mac box, replay each envelope against the engine on 127.0.0.1, post
the response (streams included, chunk by chunk) back through to_client.
The phone does the mirror image from anywhere on the internet.

Opt-in and rollback-safe by construction: this thread only starts when
~/Library/Application Support/Vivieen/relay-url exists. Delete the file
and restart - the whole feature is off. EnConvo is never touched; the
relay replays the same local API the phone already speaks on the LAN.
"""
import hashlib
import json
import os
import threading
import time
import urllib.request

SUPPORT = os.path.expanduser("~/Library/Application Support/Vivieen")
_ALLOWED_PREFIXES = ("/api/enconvo/", "/api/avatars", "/health")


def _read(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except Exception:
        return ""


def _call_relay(base, channel, proof, path, payload=None, timeout=35):
    url = f"{base.rstrip('/')}/api/relay?channel={channel}&{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json", "x-viv-proof": proof})
    with urllib.request.urlopen(request, timeout=timeout) as feed:
        return json.loads(feed.read().decode() or "{}")


def _replay(envelope, engine_port, engine_token, send):
    """One envelope against the local engine; stream chunks back as they
    come so the phone reads the same SSE cadence it gets on the LAN."""
    request_id = envelope.get("id") or ""
    req = envelope.get("req") or {}
    path = str(req.get("path") or "")
    if not path.startswith(_ALLOWED_PREFIXES):
        send({"id": request_id, "done": True, "status": 403,
              "body": json.dumps({"error": "path not relayed"})})
        return
    body = req.get("body")
    data = json.dumps(body).encode() if body is not None else None
    upstream = urllib.request.Request(
        f"http://127.0.0.1:{engine_port}{path}", data=data,
        method=str(req.get("method") or ("POST" if data else "GET")),
        headers={"Content-Type": "application/json",
                 "x-vivieen-token": engine_token})
    try:
        with urllib.request.urlopen(upstream, timeout=900) as feed:
            kind = feed.headers.get("Content-Type", "")
            if "text/event-stream" in kind:
                part = 0
                buffer = b""
                while True:
                    chunk = feed.read(1)
                    if not chunk:
                        break
                    buffer += chunk
                    if buffer.endswith(b"\n\n"):
                        send({"id": request_id, "part": part,
                              "sse": buffer.decode("utf-8", "replace")})
                        part += 1
                        buffer = b""
                send({"id": request_id, "done": True, "status": 200,
                      "stream": True})
            else:
                send({"id": request_id, "done": True, "status": feed.status,
                      "type": kind,
                      "body": feed.read().decode("utf-8", "replace")})
    except Exception as error:
        send({"id": request_id, "done": True, "status": 502,
              "body": json.dumps({"error": str(error)[:200]})})


def start(engine_port):
    """Called from the engine at boot. No relay-url file, no thread."""
    base = _read(os.path.join(SUPPORT, "relay-url"))
    token = _read(os.path.join(SUPPORT, "remote-token"))
    if not base or not token:
        return None

    channel = hashlib.sha256(token.encode()).hexdigest()[:16]
    proof = hashlib.sha256(b"viv-relay:" + token.encode()).hexdigest()

    def send(message):
        for attempt in (1, 2, 3):
            try:
                _call_relay(base, channel, proof, "dir=to_client",
                            {"items": [message]}, timeout=15)
                return
            except Exception:
                time.sleep(attempt)

    def pump():
        cursor = 0
        print(f"[viv] relay agent up: {base} channel={channel}", flush=True)
        while True:
            try:
                got = _call_relay(base, channel, proof,
                                  f"dir=to_mac&after={cursor}&wait=25")
                cursor = got.get("next", cursor)
                for envelope in got.get("items") or []:
                    threading.Thread(
                        target=_replay,
                        args=(envelope, engine_port, token, send),
                        daemon=True).start()
            except Exception as error:
                print("[viv] relay poll failed:", str(error)[:120], flush=True)
                time.sleep(5)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    return thread
