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
import base64
import hashlib
import json
import os
import threading
import time
import urllib.request

SUPPORT = os.path.expanduser("~/Library/Application Support/Vivieen")
# Everything the pocket app asks for on the LAN, so the same app can ask
# for it from anywhere: her page, her runtime assets, the files she
# delivers, and the whole local API. The channel is already gated on the
# pairing token, so a caller here could reach these over Wi-Fi anyway.
_ALLOWED_PREFIXES = ("/api/", "/assets/", "/files/", "/health", "/reply",
                     "/say", "/stt", "/settings", "/live-worklet.js", "/c/")
_TEXTUAL = ("text/", "application/json", "application/javascript",
            "image/svg+xml")


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
    # "/" and "/?query" are her page itself.
    if not (path.startswith(_ALLOWED_PREFIXES) or path == "/"
            or path.startswith("/?")):
        send({"id": request_id, "done": True, "status": 403,
              "body": json.dumps({"error": "path not relayed"})})
        return
    # JSON rides as "body"; a recording or a photo rides as base64 "raw"
    # with its own content type, so the request is rebuilt exactly.
    body, raw = req.get("body"), req.get("raw")
    if raw:
        data = base64.b64decode(raw)
        content_type = str(req.get("type") or "application/octet-stream")
    elif body is not None:
        data = json.dumps(body).encode()
        content_type = "application/json"
    else:
        data, content_type = None, "application/json"
    upstream = urllib.request.Request(
        f"http://127.0.0.1:{engine_port}{path}", data=data,
        method=str(req.get("method") or ("POST" if data else "GET")),
        headers={"Content-Type": content_type,
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
                raw = feed.read()
                # Her sprites and audio are bytes, not text - decoding them
                # as utf-8 would quietly corrupt every avatar over the wire.
                textual = any(kind.startswith(k) for k in _TEXTUAL)
                message = {"id": request_id, "done": True,
                           "status": feed.status, "type": kind}
                if textual:
                    message["body"] = raw.decode("utf-8", "replace")
                else:
                    message["b64"] = base64.b64encode(raw).decode("ascii")
                send(message)
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

    def lan_addresses(port):
        """Every address this Mac can be reached on from its own network.

        The phone was given ONE address when it paired. The router hands
        out a new lease and that address is a lie - the phone sits on the
        same Wi-Fi and cannot find a Mac two feet away, so it falls to the
        relay and everything gets slow for no reason (owner, 2026-08-04).
        """
        found = []
        try:
            import socket
            for info in socket.getaddrinfo(socket.gethostname(), None):
                host = info[4][0]
                if ":" in host or host.startswith("127."):
                    continue        # IPv6 and loopback are no use here
                url = f"http://{host}:{port}"
                if url not in found:
                    found.append(url)
        except Exception:
            pass
        return found

    def presence(engine_port):
        """Say where we are and what we hold. Short TTL: a Mac that goes
        to sleep must stop claiming to be reachable on its own."""
        while True:
            try:
                import server.app as _app          # active avatar, version
            except Exception:
                _app = None
            boot = ""
            try:
                boot = getattr(_app, "BOOT_ID", "") if _app else ""
            except Exception:
                boot = ""
            record = {"lan": lan_addresses(engine_port),
                      "boot": boot,
                      "at": int(time.time())}
            try:
                if _app is not None:
                    record["avatar"] = _app.active_slug() or ""
            except Exception:
                pass
            try:
                _call_relay(base, channel, proof, "dir=presence&ttl=120",
                            record, timeout=15)
            except Exception as error:
                print("[viv] presence failed:", str(error)[:120], flush=True)
            time.sleep(45)

    def pump():
        # Start at the TIP, never at 0. Requests still sitting in the box
        # are from sessions that timed out minutes ago; replaying them
        # re-answers stale messages and pushes a fresh copy of every old
        # reply back into the mailbox, which is how it grew to megabytes
        # and made the phone unusable over 5G (owner, 2026-08-03).
        cursor = -1
        quiet = 0
        print(f"[viv] relay agent up: {base} channel={channel}", flush=True)
        while True:
            try:
                got = _call_relay(base, channel, proof,
                                  f"dir=to_mac&after={cursor}&wait=25")
                items = got.get("items") or []
                # A mailbox that empties under us (an instance recycled, an
                # expiry fired) leaves this cursor past the end, and reading
                # past the end returns nothing FOREVER - the phone could
                # never reach the Mac again, silently (2026-08-03). The
                # relay resyncs now, but never depend on the far end for
                # your own liveness: after a long quiet spell, rewind.
                if items:
                    quiet = 0
                else:
                    quiet += 1
                    if quiet >= 8 and cursor:
                        print("[viv] relay: long silence, resyncing to tip",
                              flush=True)
                        # To the tip, not to zero: this only has to cure a
                        # cursor stranded past the end. Rewinding to zero
                        # cured it by re-executing the entire history.
                        cursor, quiet = -1, 0
                        continue
                cursor = got.get("next", cursor)
                for envelope in items:
                    threading.Thread(
                        target=_replay,
                        args=(envelope, engine_port, token, send),
                        daemon=True).start()
            except Exception as error:
                print("[viv] relay poll failed:", str(error)[:120], flush=True)
                time.sleep(5)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    threading.Thread(target=presence, args=(engine_port,), daemon=True).start()
    return thread
