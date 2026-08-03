#!/usr/bin/env python3
"""App Store Connect bootstrap for TestFlight: make sure the bundle id
com.vivieen.pocket is registered, and say whether the app record exists.
Auth is an ASC API key (the .p8 the owner downloads once); everything
here is a plain REST call with a short-lived ES256 JWT."""
import json
import os
import sys
import time
import urllib.request

BUNDLE_ID = "com.vivieen.pocket"
API = "https://api.appstoreconnect.apple.com/v1"


def token(key_id, issuer_id, key_path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    import base64

    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    header = b64(json.dumps({"alg": "ES256", "kid": key_id,
                             "typ": "JWT"}).encode())
    now = int(time.time())
    payload = b64(json.dumps({"iss": issuer_id, "iat": now,
                              "exp": now + 900,
                              "aud": "appstoreconnect-v1"}).encode())
    signing_input = header + b"." + payload
    key = serialization.load_pem_private_key(
        open(key_path, "rb").read(), password=None)
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return (signing_input + b"." + b64(raw)).decode()


def call(jwt, method, path, body=None):
    request = urllib.request.Request(
        f"{API}{path}", method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {jwt}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as feed:
            return feed.status, json.loads(feed.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode() or "{}")


def main():
    key_id = os.environ["ASC_KEY_ID"]
    issuer = os.environ["ASC_ISSUER_ID"]
    key_path = os.environ["ASC_KEY_PATH"]
    jwt = token(key_id, issuer, key_path)

    status, found = call(jwt, "GET",
                         f"/bundleIds?filter[identifier]={BUNDLE_ID}")
    if status == 200 and not found.get("data"):
        status, made = call(jwt, "POST", "/bundleIds", {
            "data": {"type": "bundleIds",
                     "attributes": {"identifier": BUNDLE_ID,
                                    "name": "Vivieen Pocket",
                                    "platform": "IOS"}}})
        print(f"bundle id registered ({status})"
              if status in (200, 201) else
              f"bundle id registration: {status} {json.dumps(made)[:200]}")
    else:
        print(f"bundle id already registered" if status == 200
              else f"bundle id lookup failed: {status}")

    status, apps = call(jwt, "GET", f"/apps?filter[bundleId]={BUNDLE_ID}")
    if status == 200 and apps.get("data"):
        print("app record exists:", apps["data"][0]["attributes"]["name"])
    else:
        print("NO app record yet - the upload may create one; if it "
              "complains, add it once in App Store Connect → My Apps → "
              f"'+' → New App → bundle id {BUNDLE_ID}.")


if __name__ == "__main__":
    sys.exit(main())
