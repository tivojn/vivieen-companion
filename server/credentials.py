"""The credential vault - EnConvo's idea, the Mac's machinery.

EnConvo encrypts its provider keys instead of writing them in the clear;
the right way to borrow that leaf on macOS is not to invent a cipher but
to use the vault the OS already guards: the login Keychain. Keys go in
under one service name, config.json keeps only the marker "@keychain",
and the real value is materialised in memory at load. Nothing on disk in
this repo's data root ever holds a secret again - and the settings API
stays write-only (set, clear, has_key; never echoed back).

Migration is automatic and one-way: the first load() that finds a
plaintext key sweeps it into the vault and rewrites the config with
markers. Rollback never leaks: deleting a marker just means "no key".

Off macOS (tests, CI) the vault is a plain JSON file under the data
root - the tests patch it to a tempdir; the comment in the file says
what it is and is not.
"""
import json
import os
import subprocess
import sys
import threading

SERVICE = "com.vivieen.companion"
MARKER = "@keychain"
_lock = threading.Lock()
_memo = {}


def _fallback_path():
    from providers import DATA_ROOT
    return os.path.join(DATA_ROOT, "vault.json")


def _is_mac():
    return sys.platform == "darwin" and not os.environ.get("VIVIEEN_VAULT_FILE")


def _file_vault_read():
    path = os.environ.get("VIVIEEN_VAULT_FILE") or _fallback_path()
    try:
        with open(path) as handle:
            return json.load(handle)
    except Exception:
        return {}


def _file_vault_write(data):
    path = os.environ.get("VIVIEEN_VAULT_FILE") or _fallback_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(data, handle)


def get(account):
    """The secret for one account ('llm.api_key', 'live.xai_api_key')."""
    with _lock:
        if account in _memo:
            return _memo[account]
    if _is_mac():
        done = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE,
             "-a", account, "-w"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        value = done.stdout.rstrip("\n") if done.returncode == 0 else ""
    else:
        value = _file_vault_read().get(account, "")
    with _lock:
        _memo[account] = value
    return value


def put(account, value):
    if not value:
        return clear(account)
    if _is_mac():
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", SERVICE,
             "-a", account, "-w", value],
            capture_output=True, stdin=subprocess.DEVNULL, check=True)
    else:
        data = _file_vault_read()
        data[account] = value
        _file_vault_write(data)
    with _lock:
        _memo[account] = value


def clear(account):
    if _is_mac():
        subprocess.run(
            ["security", "delete-generic-password", "-s", SERVICE,
             "-a", account],
            capture_output=True, stdin=subprocess.DEVNULL)
    else:
        data = _file_vault_read()
        data.pop(account, None)
        _file_vault_write(data)
    with _lock:
        _memo[account] = ""


# ------------------------------------------------ config <-> vault weaving

# Every field in the config that is a secret, by block.
SECRET_FIELDS = {
    "llm": ("api_key",),
    "tts": ("api_key",),
    "stt": ("api_key",),
    "image": ("api_key",),
    "video": ("api_key",),
    "live": ("xai_api_key", "eleven_api_key"),
}


def absorb(cfg):
    """Sweep plaintext secrets out of a config dict into the vault,
    leaving markers. Returns True if anything moved (caller persists)."""
    moved = False
    for block_name, fields in SECRET_FIELDS.items():
        block = cfg.get(block_name)
        if not isinstance(block, dict):
            continue
        for field in fields:
            value = block.get(field) or ""
            if value == "__clear__":
                clear(f"{block_name}.{field}")
                block[field] = ""
                moved = True
            elif value and value != MARKER:
                put(f"{block_name}.{field}", value)
                block[field] = MARKER
                moved = True
    return moved


def materialise(cfg):
    """Replace markers with the real secrets, in memory only."""
    for block_name, fields in SECRET_FIELDS.items():
        block = cfg.get(block_name)
        if not isinstance(block, dict):
            continue
        for field in fields:
            if block.get(field) == MARKER:
                block[field] = get(f"{block_name}.{field}")
    return cfg
