"""Generated-set libraries for Horizon Walk, Edge Idle, and full-body views.

Every successful generation is archived as a *set* under
``avatars/<slug>/library/`` so an avatar can keep several walks, several edge
idles, and several three-view bodies, and switch between them without spending
another generation. The canonical ``motion/`` and ``body/`` directories keep
their existing layout and always describe the set that is currently in use:
activating a set copies its files back into the canonical location, which is
what the runtime exporter and the pet window already consume. Nothing
downstream of this module changes shape.

The module is deliberately pure stdlib (no cv2/numpy) so it can be imported by
the server and unit tests without loading the vision stack.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import tempfile

MOTION_KINDS = ("walk", "idle")
SET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
# Keys in motion.json owned by one clip kind rather than shared by the bundle.
_CLIP_KEYS = {
    "walk": ("walk_style", "walk_frame"),
    "idle": ("idle_pose", "reference"),
}
_SHARED_KEYS = ("signature", "image_provider", "video_provider",
                "identity_reference")


def _library_root(avatar_dir):
    return os.path.join(avatar_dir, "library")


def _index_path(avatar_dir):
    return os.path.join(_library_root(avatar_dir), "library.json")


def _motion_set_root(avatar_dir, kind):
    return os.path.join(_library_root(avatar_dir), "motion", kind)


def _body_set_root(avatar_dir):
    return os.path.join(_library_root(avatar_dir), "body")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)
    os.replace(temporary, path)


def _read_index(avatar_dir):
    index = _read_json(_index_path(avatar_dir)) or {}
    active = index.get("active") or {}
    return {"v": 1, "active": {
        "walk": active.get("walk"),
        "idle": active.get("idle"),
        "body": active.get("body"),
    }}


def _write_index(avatar_dir, index):
    _write_json(_index_path(avatar_dir), index)


def _set_active(avatar_dir, slot, set_id):
    index = _read_index(avatar_dir)
    index["active"][slot] = set_id
    _write_index(avatar_dir, index)


def clear_active(avatar_dir, slot):
    """Forget the active pointer after the canonical asset was removed."""
    if slot not in ("walk", "idle", "body"):
        raise ValueError(f"unknown library slot: {slot}")
    _set_active(avatar_dir, slot, None)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_set_id(set_id):
    set_id = str(set_id or "")
    if not SET_ID_PATTERN.match(set_id):
        raise ValueError(f"invalid set id: {set_id!r}")
    return set_id


def _new_set_id(suffix):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    suffix = re.sub(r"[^a-z0-9-]+", "-", str(suffix or "set").lower()).strip("-")
    return f"{stamp}-{suffix or 'set'}"


def _clip_file_names(directory, kind):
    """Top-level clip asset names (walk-0.png, walk-alpha.mov, ...)."""
    if not os.path.isdir(directory):
        return []
    return sorted(
        name for name in os.listdir(directory)
        if name.startswith(f"{kind}-") and
        os.path.isfile(os.path.join(directory, name))
    )


def _remove_clip_files(directory, kind):
    for name in _clip_file_names(directory, kind):
        os.remove(os.path.join(directory, name))
    raw = os.path.join(directory, "raw")
    if os.path.isdir(raw):
        for name in _clip_file_names(raw, kind):
            os.remove(os.path.join(raw, name))


def _motion_content_sha(motion_dir, kind, clip):
    """Content identity of a clip: the alpha movie, else the first sheet."""
    names = [os.path.basename(str(clip.get("alpha_video") or ""))]
    for sheet in clip.get("sheets") or []:
        names.append(os.path.basename(str(sheet.get("image") or "")))
    for name in names:
        path = os.path.join(motion_dir, name)
        if name and os.path.isfile(path):
            return _sha256(path)
    return None


def _body_source_name(body_metadata, kind):
    reference = (body_metadata or {}).get("motion_reference") or {}
    name = os.path.basename(str(reference.get(f"{kind}_source") or ""))
    if name:
        return name
    view = "side" if kind == "walk" else "front"
    return f"source-{view}.png"


def body_source_sha(avatar_dir, kind):
    """Digest of the body plate the given motion kind must match, or None."""
    body_dir = os.path.join(avatar_dir, "body")
    metadata = _read_json(os.path.join(body_dir, "body.json"))
    if not metadata:
        return None
    for name in (_body_source_name(metadata, kind), "source.png"):
        path = os.path.join(body_dir, name)
        if os.path.isfile(path):
            return _sha256(path)
    return None


# ---------------------------------------------------------------- motion sets


def archive_motion(avatar_dir, kind):
    """Snapshot the canonical clip of `kind` into the library.

    Idempotent: if a set with the same content digest already exists it is
    refreshed and marked active instead of duplicated. Returns the set id, or
    None when the canonical motion has no such clip.
    """
    if kind not in MOTION_KINDS:
        raise ValueError(f"unknown motion clip selection: {kind}")
    motion_dir = os.path.join(avatar_dir, "motion")
    metadata = _read_json(os.path.join(motion_dir, "motion.json")) or {}
    clip = metadata.get(kind)
    if not isinstance(clip, dict) or not clip.get("sheets"):
        return None
    content_sha = _motion_content_sha(motion_dir, kind, clip)
    if not content_sha:
        return None
    existing = None
    for record in _motion_set_records(avatar_dir, kind):
        if record.get("content_sha") == content_sha:
            existing = record["id"]
            break

    style = metadata.get("walk_style") if kind == "walk" else None
    pose = metadata.get("idle_pose") if kind == "idle" else None
    label = (
        (style or {}).get("label") if kind == "walk" else
        (pose or {}).get("label")
    ) or ("Horizon Walk" if kind == "walk" else "Edge Idle")
    set_id = existing or _new_set_id(
        (style or {}).get("id") if kind == "walk" else (pose or {}).get("id"))
    destination = os.path.join(_motion_set_root(avatar_dir, kind), set_id)
    stage = tempfile.mkdtemp(
        prefix=".set-stage-", dir=_ensured(_motion_set_root(avatar_dir, kind)))
    try:
        for name in _clip_file_names(motion_dir, kind):
            shutil.copy2(os.path.join(motion_dir, name),
                         os.path.join(stage, name))
        raw_source = os.path.join(motion_dir, "raw")
        raw_names = _clip_file_names(raw_source, kind)
        if raw_names:
            os.makedirs(os.path.join(stage, "raw"))
            for name in raw_names:
                shutil.copy2(os.path.join(raw_source, name),
                             os.path.join(stage, "raw", name))
        record = {
            "v": 1,
            "id": set_id,
            "kind": kind,
            "label": label,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "content_sha": content_sha,
            "shared": {key: metadata.get(key) for key in _SHARED_KEYS},
            "clip": clip,
            "body_reference": (metadata.get("body_references") or {}).get(kind),
            "prompts": {
                key: (metadata.get("prompts") or {}).get(key)
                for key in (f"{kind}_keyframe", f"{kind}_video")
            },
        }
        for key in _CLIP_KEYS[kind]:
            if metadata.get(key) is not None:
                record[key] = metadata[key]
        _write_json(os.path.join(stage, "set.json"), record)
        shutil.rmtree(destination, ignore_errors=True)
        os.replace(stage, destination)
        stage = None
    finally:
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
    _set_active(avatar_dir, kind, set_id)
    return set_id


def _ensured(directory):
    os.makedirs(directory, exist_ok=True)
    return directory


def _motion_set_records(avatar_dir, kind):
    root = _motion_set_root(avatar_dir, kind)
    records = []
    if not os.path.isdir(root):
        return records
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        record = _read_json(os.path.join(root, name, "set.json"))
        if isinstance(record, dict) and record.get("id") == name:
            records.append(record)
    return records


def list_motion_sets(avatar_dir, kind):
    """UI-facing listing, newest first, with activation/compatibility flags."""
    active_id = _read_index(avatar_dir)["active"].get(kind)
    canonical = _read_json(
        os.path.join(avatar_dir, "motion", "motion.json")) or {}
    has_canonical_clip = isinstance(canonical.get(kind), dict)
    current_body_sha = body_source_sha(avatar_dir, kind)
    sets = []
    for record in _motion_set_records(avatar_dir, kind):
        set_dir = os.path.join(_motion_set_root(avatar_dir, kind), record["id"])
        poster = f"{kind}-poster.png"
        clip = record.get("clip") or {}
        reference_sha = (record.get("body_reference") or {}).get("sha256")
        style = record.get("walk_style") or {}
        pose = record.get("idle_pose") or {}
        sets.append({
            "id": record["id"],
            "kind": kind,
            "label": record.get("label") or "",
            "created": record.get("created") or "",
            "style": style.get("id"),
            "pose": pose.get("id"),
            "frames": clip.get("frames"),
            "fps": clip.get("fps"),
            "poster": (
                f"library/motion/{kind}/{record['id']}/{poster}"
                if os.path.isfile(os.path.join(set_dir, poster)) else None),
            "active": has_canonical_clip and record["id"] == active_id,
            "compatible": bool(
                current_body_sha and reference_sha and
                current_body_sha == reference_sha),
        })
    sets.sort(key=lambda item: item["created"], reverse=True)
    return sets


def newest_compatible_motion_set(avatar_dir, kind):
    for record in list_motion_sets(avatar_dir, kind):
        if record["compatible"]:
            return record["id"]
    return None


def _strip_clip_metadata(metadata, kind):
    metadata.pop(kind, None)
    for key in _CLIP_KEYS[kind]:
        metadata.pop(key, None)
    body_references = dict(metadata.get("body_references") or {})
    body_references.pop(kind, None)
    metadata["body_references"] = body_references
    prompts = dict(metadata.get("prompts") or {})
    prompts.pop(f"{kind}_keyframe", None)
    prompts.pop(f"{kind}_video", None)
    metadata["prompts"] = prompts
    return metadata


def activate_motion(avatar_dir, kind, set_id):
    """Copy a library set into the canonical motion bundle.

    Returns the updated motion metadata for the avatar manifest.
    """
    if kind not in MOTION_KINDS:
        raise ValueError(f"unknown motion clip selection: {kind}")
    set_id = _safe_set_id(set_id)
    set_dir = os.path.join(_motion_set_root(avatar_dir, kind), set_id)
    record = _read_json(os.path.join(set_dir, "set.json"))
    if not isinstance(record, dict) or record.get("kind") != kind:
        raise ValueError(f"unknown {kind} set: {set_id}")

    motion_dir = os.path.join(avatar_dir, "motion")
    metadata = _read_json(os.path.join(motion_dir, "motion.json"))
    if not isinstance(metadata, dict):
        metadata = dict(record.get("shared") or {})
        metadata["v"] = metadata.get("v") or 9
        metadata["created"] = record.get("created")
    stage = tempfile.mkdtemp(prefix=".motion-activate-", dir=avatar_dir)
    try:
        if os.path.isdir(motion_dir):
            shutil.copytree(motion_dir, stage, dirs_exist_ok=True)
        _remove_clip_files(stage, kind)
        for name in os.listdir(set_dir):
            if name in ("set.json", "raw"):
                continue
            shutil.copy2(os.path.join(set_dir, name), os.path.join(stage, name))
        set_raw = os.path.join(set_dir, "raw")
        if os.path.isdir(set_raw):
            os.makedirs(os.path.join(stage, "raw"), exist_ok=True)
            for name in os.listdir(set_raw):
                shutil.copy2(os.path.join(set_raw, name),
                             os.path.join(stage, "raw", name))
        metadata[kind] = record.get("clip")
        for key in _CLIP_KEYS[kind]:
            if record.get(key) is not None:
                metadata[key] = record[key]
            else:
                metadata.pop(key, None)
        body_references = dict(metadata.get("body_references") or {})
        if record.get("body_reference"):
            body_references[kind] = record["body_reference"]
        metadata["body_references"] = body_references
        prompts = dict(metadata.get("prompts") or {})
        for key, value in (record.get("prompts") or {}).items():
            if value is not None:
                prompts[key] = value
        metadata["prompts"] = prompts
        metadata["updated"] = datetime.datetime.now().isoformat(
            timespec="seconds")
        _write_json(os.path.join(stage, "motion.json"), metadata)

        backup = motion_dir + ".activate-backup"
        shutil.rmtree(backup, ignore_errors=True)
        if os.path.exists(motion_dir):
            os.replace(motion_dir, backup)
        try:
            os.replace(stage, motion_dir)
            stage = None
        except Exception:
            if not os.path.exists(motion_dir) and os.path.exists(backup):
                os.replace(backup, motion_dir)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
    _set_active(avatar_dir, kind, set_id)
    return metadata


def remove_motion_set(avatar_dir, kind, set_id):
    """Delete one library set. Returns True when it was the active set."""
    if kind not in MOTION_KINDS:
        raise ValueError(f"unknown motion clip selection: {kind}")
    set_id = _safe_set_id(set_id)
    set_dir = os.path.join(_motion_set_root(avatar_dir, kind), set_id)
    if not os.path.isdir(set_dir):
        raise ValueError(f"unknown {kind} set: {set_id}")
    was_active = _read_index(avatar_dir)["active"].get(kind) == set_id
    shutil.rmtree(set_dir)
    if was_active:
        _set_active(avatar_dir, kind, None)
    return was_active


def strip_canonical_motion(avatar_dir, kind):
    """Remove one clip kind from the canonical bundle (library untouched).

    Mirrors studio.motion.remove for a single kind without importing the
    vision stack. Returns the remaining metadata, or None when nothing is left.
    """
    motion_dir = os.path.join(avatar_dir, "motion")
    metadata_path = os.path.join(motion_dir, "motion.json")
    metadata = _read_json(metadata_path)
    if not isinstance(metadata, dict):
        return None
    _remove_clip_files(motion_dir, kind)
    _strip_clip_metadata(metadata, kind)
    if not any(metadata.get(name) for name in MOTION_KINDS):
        shutil.rmtree(motion_dir, ignore_errors=True)
        return None
    metadata["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    _write_json(metadata_path, metadata)
    return metadata


def reconcile_motion_with_body(avatar_dir):
    """After the body changed, keep only motion that matches the new body.

    Clips generated against a different body wardrobe are dropped from the
    canonical bundle (their library sets remain), and the newest compatible
    library set for each missing kind is restored automatically. Returns the
    final canonical motion metadata, or None when no motion remains.
    """
    metadata = _read_json(
        os.path.join(avatar_dir, "motion", "motion.json"))
    metadata = metadata if isinstance(metadata, dict) else None
    for kind in MOTION_KINDS:
        if not metadata or not metadata.get(kind):
            continue
        wanted = body_source_sha(avatar_dir, kind)
        recorded = ((metadata.get("body_references") or {}).get(kind)
                    or {}).get("sha256")
        if not wanted or not recorded or wanted != recorded:
            metadata = strip_canonical_motion(avatar_dir, kind)
            _set_active(avatar_dir, kind, None)
    for kind in MOTION_KINDS:
        if metadata and metadata.get(kind):
            continue
        fallback = newest_compatible_motion_set(avatar_dir, kind)
        if fallback:
            metadata = activate_motion(avatar_dir, kind, fallback)
    return metadata


# ------------------------------------------------------------------ body sets


def archive_body(avatar_dir):
    """Snapshot the canonical body directory as a library set."""
    body_dir = os.path.join(avatar_dir, "body")
    metadata = _read_json(os.path.join(body_dir, "body.json"))
    if not isinstance(metadata, dict):
        return None
    front = os.path.join(
        body_dir, os.path.basename(str(metadata.get("image") or "body.png")))
    if not os.path.isfile(front):
        return None
    content_sha = _sha256(front)
    existing = None
    for record in _body_set_records(avatar_dir):
        if record.get("content_sha") == content_sha:
            existing = record["id"]
            break
    options = metadata.get("options") or {}
    label = " · ".join(part for part in (
        str(options.get("style") or "").replace("-", " ").capitalize(),
        str(options.get("pose") or ""),
    ) if part) or "Full body"
    set_id = existing or _new_set_id(options.get("style"))
    root = _ensured(_body_set_root(avatar_dir))
    destination = os.path.join(root, set_id)
    stage = tempfile.mkdtemp(prefix=".set-stage-", dir=root)
    try:
        shutil.copytree(body_dir, stage, dirs_exist_ok=True)
        _write_json(os.path.join(stage, "set.json"), {
            "v": 1,
            "id": set_id,
            "label": label,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "content_sha": content_sha,
            "options": {
                "style": options.get("style"),
                "pose": options.get("pose"),
            },
        })
        shutil.rmtree(destination, ignore_errors=True)
        os.replace(stage, destination)
        stage = None
    finally:
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
    _set_active(avatar_dir, "body", set_id)
    return set_id


def _body_set_records(avatar_dir):
    root = _body_set_root(avatar_dir)
    records = []
    if not os.path.isdir(root):
        return records
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        record = _read_json(os.path.join(root, name, "set.json"))
        if isinstance(record, dict) and record.get("id") == name:
            records.append(record)
    return records


def list_body_sets(avatar_dir):
    active_id = _read_index(avatar_dir)["active"].get("body")
    canonical = _read_json(
        os.path.join(avatar_dir, "body", "body.json"))
    has_canonical = isinstance(canonical, dict)
    sets = []
    for record in _body_set_records(avatar_dir):
        set_dir = os.path.join(_body_set_root(avatar_dir), record["id"])
        metadata = _read_json(os.path.join(set_dir, "body.json")) or {}
        preview = os.path.basename(
            str(((metadata.get("views") or {}).get("front") or {}).get("image")
                or metadata.get("image") or "body.png"))
        sets.append({
            "id": record["id"],
            "label": record.get("label") or "Full body",
            "created": record.get("created") or "",
            "options": record.get("options") or {},
            "preview": (
                f"library/body/{record['id']}/{preview}"
                if os.path.isfile(os.path.join(set_dir, preview)) else None),
            "active": has_canonical and record["id"] == active_id,
        })
    sets.sort(key=lambda item: item["created"], reverse=True)
    return sets


def newest_body_set(avatar_dir):
    sets = list_body_sets(avatar_dir)
    return sets[0]["id"] if sets else None


def activate_body(avatar_dir, set_id):
    """Copy a body library set into the canonical body directory.

    Returns the body metadata for the avatar manifest. The caller is expected
    to follow up with reconcile_motion_with_body() and a runtime publish.
    """
    set_id = _safe_set_id(set_id)
    set_dir = os.path.join(_body_set_root(avatar_dir), set_id)
    metadata = _read_json(os.path.join(set_dir, "body.json"))
    if not isinstance(metadata, dict):
        raise ValueError(f"unknown body set: {set_id}")
    body_dir = os.path.join(avatar_dir, "body")
    stage = tempfile.mkdtemp(prefix=".body-activate-", dir=avatar_dir)
    try:
        shutil.copytree(set_dir, stage, dirs_exist_ok=True)
        try:
            os.remove(os.path.join(stage, "set.json"))
        except FileNotFoundError:
            pass
        backup = body_dir + ".activate-backup"
        shutil.rmtree(backup, ignore_errors=True)
        if os.path.exists(body_dir):
            os.replace(body_dir, backup)
        try:
            os.replace(stage, body_dir)
            stage = None
        except Exception:
            if not os.path.exists(body_dir) and os.path.exists(backup):
                os.replace(backup, body_dir)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
    _set_active(avatar_dir, "body", set_id)
    return metadata


def remove_body_set(avatar_dir, set_id):
    """Delete one body set. Returns True when it was the active set."""
    set_id = _safe_set_id(set_id)
    set_dir = os.path.join(_body_set_root(avatar_dir), set_id)
    if not os.path.isdir(set_dir):
        raise ValueError(f"unknown body set: {set_id}")
    was_active = _read_index(avatar_dir)["active"].get("body") == set_id
    shutil.rmtree(set_dir)
    if was_active:
        _set_active(avatar_dir, "body", None)
    return was_active


# ------------------------------------------------------------------ migration


def sync_canonical(avatar_dir):
    """Archive canonical assets that are not in the library yet.

    Makes libraries appear for avatars generated before sets existed, and
    doubles as a safety net when a generation finished without its archive
    step. Content digests keep it idempotent.
    """
    if os.path.isfile(os.path.join(avatar_dir, "body", "body.json")):
        archive_body(avatar_dir)
    if os.path.isfile(os.path.join(avatar_dir, "motion", "motion.json")):
        for kind in MOTION_KINDS:
            archive_motion(avatar_dir, kind)
