"""Generate and publish identity-locked desktop-companion motion loops."""
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time

import cv2
import numpy as np

from . import body, cutout


ENCONVO = body.ENCONVO
MOTION_VERSION = 3
TARGET_WIDTH = 256
TARGET_HEIGHT = 384
WALK_FPS = 24
IDLE_FPS = 12
MAX_SHEET_FRAMES = 32


def _clean(value, maximum=800):
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()[:maximum]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _emit(progress, stage, value, label):
    if progress:
        progress(stage, value, label)


def _walk_keyframe_prompt(outfit):
    return f"""Edit the reference into a full-body side-profile motion keyframe of the exact same adult woman.

IDENTITY AND WARDROBE LOCK — preserve her face, apparent age, body proportions, fuchsia tailored blazer, ivory silk shell, black cigarette trousers, slim watch, and black high heels. Do not redesign or replace any garment.

POSE — exact right-facing side profile in a normal, charming office-floor walking contact pose, as though she is moving purposefully in a straight line from one workplace to another. Use a modest everyday stride: front heel softly contacting the floor, rear toe just about to leave it, low toe clearance, both knees well below hip height, and the rear heel no higher than the lower calf. Keep elbows close to the torso with only a compact counter-swing; both hands remain below the waist near the hip seams. Add polished confidence and a very subtle catwalk narrowness, but no marching, power walk, high knee, long runway lunge, crossed legs, chest-height hand, or theatrical arm swing. Spine tall, gaze level. Both complete shoes, hands, and the full hair silhouette must be visible.

COMPOSITION — one person only, complete figure centered on a vertical 2:3 canvas, locked camera at waist height, long lens, generous clean margin, no crop, no props, no text, no furniture, no floor shadow. Plain high-contrast studio background for later local segmentation.

Wardrobe receipt: {outfit}"""


def _idle_keyframe_prompt(outfit, has_pose_reference):
    reference_note = (
        "Reference 1 is the identity and wardrobe authority. Reference 2 is pose geometry only: "
        "do not copy its person, face, hair, black dress, straps, accessories, or styling."
        if has_pose_reference else
        "The identity reference is the sole authority for face, hair, wardrobe, and body proportions."
    )
    return f"""Create a full-body edge-idle keyframe of the exact same adult woman. {reference_note}

IDENTITY AND WARDROBE LOCK — preserve her face, apparent age, body proportions, fuchsia tailored blazer, ivory silk shell, black cigarette trousers, slim watch, and black high heels. Do not beautify, de-age, or change clothes.

POSE GEOMETRY — author the canonical LEFT-EDGE pose: an unmistakable profile-to-three-quarter backward lean with her screen-left shoulder blade and upper back physically supported by an invisible vertical screen boundary immediately on her left. Her shoulder blades sit at least one head-width behind her hips toward camera-left, her pelvis shifts forward into the screen toward camera-right, and the blazer centerline follows a clear 25-degree backward C-curve toward camera-left. Her chest, face, and gaze turn toward camera-right into the screen, visibly away from the supporting edge—never facing the window edge or leaning against empty air. Arms remain folded calmly. One supporting leg is long toward the screen interior; the other knee lifts to hip height and bends sharply with the lower leg folding behind and the heel tucked backward. This must read as a physically supported back-lean silhouette, never an upright tree pose, ballet balance, or knee crossed in front. Keep it poised and self-possessed. Both complete shoes and all limbs must remain anatomically correct.

COMPOSITION — one person only, full figure centered on a vertical 2:3 canvas with enough margin for the lean and raised heel, locked camera, no crop, no props, no weapons, no garter, no text, no furniture, no cast shadow. Plain high-contrast studio background for later local segmentation.

Wardrobe receipt: {outfit}"""


def _walk_video_prompt():
    return """Animate this exact woman and outfit walking physically from camera-left to camera-right across the supplied wide locked-off frame. The three vertical panel seams and horizontal floor line are fixed camera-registration marks and must remain completely stationary while she passes them. Keep her complete full body visible and camera scale constant. She is a confident, charming office executive walking directly from her desk to a meeting: fluid, purposeful, and elegant at a normal 108–116 steps per minute, with subtle catwalk polish but no runway exaggeration or cautious shuffle. Use a medium natural stride, low ordinary toe clearance, soft high-heel contact, restrained hip rotation, and steady head carriage. Enforce correct contralateral human gait in every frame: whenever the right leg advances, the LEFT arm advances and the right arm moves back; whenever the left leg advances, the RIGHT arm advances and the left arm moves back. Hands remain below the waist and arm swing stays relaxed and moderate. She crosses steadily from roughly 15% to 85% of the frame over the clip while the camera never follows her. Preserve identity, fuchsia blazer, ivory shell, black trousers, watch, hair, hands, shoes, exposure, and fabric color exactly in every frame. No same-side arm-and-leg advance, no 顺拐 or ipsilateral gait, no timid tiptoe, no slow careful shuffle, no marching, high knee, heel kicked toward the knee, power walk, runway lunge, crossed-leg exaggeration, chest-height hand, large arm swing, bounce, moonwalk, foot sliding, camera pan, tracking, zoom, cut, added person, floor shadow, props, text, body-part disappearance, exposure pulse, or color flicker. Produce several continuous clean gait cycles with constant forward velocity."""


def _idle_video_prompt():
    return """Animate a subtle living hold of this exact supported back-lean pose with a locked camera. Preserve identity, fuchsia blazer, ivory shell, black trousers, watch, hair, folded arms, raised backward-bent knee, and both high heels exactly. Her shoulders must remain at least one head-width behind her hips and the blazer centerline must keep its clear 25-degree backward C-curve throughout every frame. Add only natural breathing, one soft blink, a tiny chin adjustment, and restrained fabric and hair settling. Never straighten upright, become a tree pose, cross the raised knee in front, lower the leg, unfold the arms, walk, talk, move the camera, zoom, cut, add objects, or add text. Begin and end in nearly the same leaning silhouette for a seamless idle loop."""


def _image_command(provider, references, output_dir, file_name, prompt):
    route = provider["route"]
    command = [
        ENCONVO, "image_create", "features", route,
        "--prompt", prompt,
        "--reference_images", *references,
        "--output_dir", output_dir,
        "--file_name", file_name,
        "--download",
    ]
    if provider.get("model"):
        command += ["--model", str(provider["model"])]
    if route == "open_ai/create":
        command += [
            "--mode", "edit", "--size", "1024x1536", "--quality", "high",
            "--background", "opaque", "--input_fidelity", "high",
        ]
    elif route == "gemini/create":
        image_size = "1K" if "flash-lite" in str(provider.get("model", "")) else "2K"
        command += ["--mode", "edit", "--aspectRatio", "2:3", "--imageSize", image_size]
    elif route == "x_ai/create":
        command += ["--aspect_ratio", "2:3", "--resolution", "2k"]
    elif route == "kie_ai/create":
        command += ["--mode", "edit", "--aspect_ratio", "2:3", "--resolution", "2k"]
    elif route == "azure/create":
        command += ["--mode", "edit", "--size", "1024x1792"]
    elif route in {"together/create", "straico/create"}:
        command += ["--mode", "edit"]
    return command


def _video_command(provider, keyframe, output_dir, file_name, prompt):
    normalized = provider["name"].replace("-enconvo", "")
    if normalized != "x_ai":
        raise RuntimeError(
            f"EnConvo's selected video provider is not supported for Pet motion yet: {provider['name']}")
    command = [
        ENCONVO, "video_create", "features", "x_ai/create",
        "--prompt", prompt,
        "--mode", "image-to-video",
        "--image", keyframe,
        "--duration", "6",
        "--aspect_ratio", "16:9" if file_name.startswith("walk") else "2:3",
        "--resolution", "720p",
        "--output_dir", output_dir,
        "--file_name", file_name,
        "--download",
    ]
    if provider.get("model"):
        command += ["--model", str(provider["model"])]
    return command


def _paths_from_json(value):
    paths = []
    if isinstance(value, dict):
        for child in value.values():
            paths.extend(_paths_from_json(child))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_paths_from_json(child))
    elif isinstance(value, str) and os.path.isfile(value):
        paths.append(value)
    return paths


def _generated_file(directory, extensions, started, stdout=""):
    try:
        payload = json.loads((stdout or "").strip() or "{}")
        candidates = [path for path in _paths_from_json(payload)
                      if os.path.splitext(path)[1].lower() in extensions]
        if candidates:
            return max(candidates, key=os.path.getmtime)
    except Exception:
        pass
    for _attempt in range(5):
        candidates = []
        for root, _, files in os.walk(directory):
            for name in files:
                path = os.path.join(root, name)
                if (os.path.splitext(name)[1].lower() in extensions
                        and os.path.getmtime(path) >= started - 2
                        and os.path.getsize(path) > 4096):
                    candidates.append(path)
        if candidates:
            return max(candidates, key=os.path.getmtime)
        time.sleep(1)
    detail = (stdout or "").strip()[-1000:]
    raise RuntimeError("the provider returned no downloadable media" +
                       (f": {detail}" if detail else ""))


def _run(command, output_dir, extensions):
    os.makedirs(output_dir, mode=0o700, exist_ok=True)
    started = time.time()
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=1200,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "generation failed").strip()[-1600:]
        raise RuntimeError(detail)
    return _generated_file(output_dir, extensions, started, result.stdout)


def _standard_image(source, destination):
    image = cv2.imread(source, cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"could not decode generated image: {os.path.basename(source)}")
    if not cv2.imwrite(destination, image, [cv2.IMWRITE_PNG_COMPRESSION, 5]):
        raise RuntimeError("could not save generated motion keyframe")
    return destination


def _wide_walk_keyframe(source, destination, log):
    alpha_path = os.path.splitext(destination)[0] + "-alpha.png"
    if not cutout.render(source, alpha_path, log=log, tight=True):
        raise RuntimeError("could not alpha-cut the walk traversal keyframe")
    rgba = cv2.imread(alpha_path, cv2.IMREAD_UNCHANGED)
    os.remove(alpha_path)
    points = cv2.findNonZero((rgba[:, :, 3] > 16).astype(np.uint8))
    if points is None:
        raise RuntimeError("walk traversal keyframe has no person")
    x, y, width, height = cv2.boundingRect(points)
    person = rgba[y:y + height, x:x + width]
    scale = min(620 / height, 250 / width)
    person = cv2.resize(
        person,
        (round(width * scale), round(height * scale)),
        interpolation=cv2.INTER_AREA,
    )
    canvas = np.full((720, 1280, 3), 238, dtype=np.uint8)
    for panel_x in (320, 640, 960):
        cv2.line(canvas, (panel_x, 0), (panel_x, 678), (226, 226, 226), 2)
    cv2.line(canvas, (0, 679), (1280, 679), (205, 205, 205), 2)
    left = 190 - person.shape[1] // 2
    top = 678 - person.shape[0]
    region = canvas[top:top + person.shape[0], left:left + person.shape[1]]
    alpha = person[:, :, 3:4].astype(np.float32) / 255
    region[:] = (person[:, :, :3] * alpha + region * (1 - alpha)).astype(np.uint8)
    if not cv2.imwrite(destination, canvas, [cv2.IMWRITE_PNG_COMPRESSION, 5]):
        raise RuntimeError("could not save the wide walk traversal keyframe")
    return destination


def _generate_keyframes(cache, image_provider, body_source, pose_reference, prompts, log):
    keyframe_dir = os.path.join(cache, "keyframes")
    os.makedirs(keyframe_dir, mode=0o700, exist_ok=True)

    def generate(kind):
        destination = os.path.join(keyframe_dir, f"{kind}.png")
        if os.path.getsize(destination) > 4096 if os.path.isfile(destination) else False:
            return destination
        references = [body_source]
        if kind == "idle" and pose_reference:
            references.append(pose_reference)
        output_dir = os.path.join(keyframe_dir, f"{kind}-provider")
        generated = _run(
            _image_command(
                image_provider, references, output_dir, f"{kind}-keyframe", prompts[kind]),
            output_dir,
            {".png", ".jpg", ".jpeg", ".webp"},
        )
        return _standard_image(generated, destination)

    log(f"using EnConvo image default: {image_provider['title']} / {image_provider.get('model') or 'provider default'}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {kind: executor.submit(generate, kind) for kind in ("walk", "idle")}
        return {kind: future.result() for kind, future in futures.items()}


def _generate_videos(cache, video_provider, keyframes, prompts, log):
    video_dir = os.path.join(cache, "videos")
    os.makedirs(video_dir, mode=0o700, exist_ok=True)

    def generate(kind):
        destination = os.path.join(video_dir, f"{kind}.mp4")
        if os.path.getsize(destination) > 8192 if os.path.isfile(destination) else False:
            return destination
        output_dir = os.path.join(video_dir, f"{kind}-provider")
        source_keyframe = keyframes[kind]
        if kind == "walk":
            source_keyframe = _wide_walk_keyframe(
                source_keyframe, os.path.join(video_dir, "walk-traversal-keyframe.png"), log)
        generated = _run(
            _video_command(
                video_provider, source_keyframe, output_dir, f"{kind}-source", prompts[kind]),
            output_dir,
            {".mp4", ".mov", ".webm", ".m4v"},
        )
        shutil.copy2(generated, destination)
        return destination

    log(f"using EnConvo video default: {video_provider['title']} / {video_provider.get('model') or 'provider default'}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {kind: executor.submit(generate, kind) for kind in ("walk", "idle")}
        return {kind: future.result() for kind, future in futures.items()}


def _decode_video(path, target_fps):
    capture = cv2.VideoCapture(path)
    if not capture.isOpened():
        raise RuntimeError(f"could not decode generated video: {os.path.basename(path)}")
    source_fps = float(capture.get(cv2.CAP_PROP_FPS)) or 24.0
    frames = []
    source_index = 0
    next_sample = 0.0
    while True:
        available, frame = capture.read()
        if not available:
            break
        if source_index + 0.001 >= next_sample:
            frames.append(frame)
            next_sample += source_fps / target_fps
        source_index += 1
        if len(frames) >= target_fps * 10:
            break
    capture.release()
    if len(frames) < target_fps:
        raise RuntimeError("generated motion clip is too short")
    return frames


def _segment_frames(frames, workspace, log):
    source_dir = os.path.join(workspace, "source-frames")
    alpha_dir = os.path.join(workspace, "alpha-frames")
    os.makedirs(source_dir)
    os.makedirs(alpha_dir)
    sources = []
    for index, frame in enumerate(frames):
        source = os.path.join(source_dir, f"{index:04d}.jpg")
        cv2.imwrite(source, frame, [cv2.IMWRITE_JPEG_QUALITY, 96])
        sources.append(source)

    def segment(index):
        destination = os.path.join(alpha_dir, f"{index:04d}.png")
        if not cutout.render(sources[index], destination, log=lambda _message: None, tight=True):
            raise RuntimeError(f"local person segmentation failed on frame {index + 1}")
        image = cv2.imread(destination, cv2.IMREAD_UNCHANGED)
        if image is None or image.ndim != 3 or image.shape[2] != 4:
            raise RuntimeError(f"frame {index + 1} did not produce RGBA output")
        return image

    log(f"alpha-cutting {len(frames)} frames locally with macOS Vision")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        segmented = list(executor.map(segment, range(len(frames))))

    return _stabilise_segmented(segmented)


def _fill_lower_body_alpha_holes(alpha):
    mask = (alpha >= 24).astype(np.uint8)
    points = cv2.findNonZero(mask)
    if points is None:
        return alpha
    x, y, width, height = cv2.boundingRect(points)
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return alpha
    maximum_area = max(64, float(mask.sum()) * 0.035)
    output = alpha.copy()
    for index, contour in enumerate(contours):
        if hierarchy[0][index][3] < 0:
            continue
        area = cv2.contourArea(contour)
        moments = cv2.moments(contour)
        center_y = moments["m01"] / moments["m00"] if moments["m00"] else 0
        if 4 <= area <= maximum_area and center_y >= y + height * 0.40:
            cv2.drawContours(output, [contour], -1, 255, thickness=cv2.FILLED)
    return output


def _stabilise_segmented(segmented):
    repaired = []
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    for index, image in enumerate(segmented):
        current_alpha = image[:, :, 3].astype(np.float32)
        stable_alpha = current_alpha.copy()
        if 0 < index < len(segmented) - 1:
            consensus = np.median(np.stack([
                segmented[index - 1][:, :, 3],
                image[:, :, 3],
                segmented[index + 1][:, :, 3],
            ], axis=0), axis=0)
            stable_alpha = np.maximum(stable_alpha, consensus)
        stable_alpha = cv2.morphologyEx(
            np.clip(stable_alpha, 0, 255).astype(np.uint8),
            cv2.MORPH_CLOSE,
            close_kernel,
        )
        alpha_before_hole_fill = stable_alpha.copy()
        stable_alpha = _fill_lower_body_alpha_holes(stable_alpha)
        filled_holes = ((alpha_before_hole_fill < 24) & (stable_alpha >= 24)).astype(np.uint8)
        presence = (stable_alpha >= 24).astype(np.uint8)
        interior = cv2.distanceTransform(presence, cv2.DIST_L2, 3) >= 1.5
        stable_alpha[interior] = 255
        stable_alpha[stable_alpha < 16] = 0
        current = image.copy()
        if filled_holes.any():
            current[:, :, :3] = cv2.inpaint(
                current[:, :, :3], filled_holes * 255, 5, cv2.INPAINT_TELEA)
        current[:, :, 3] = stable_alpha
        current = cutout._decontaminate_edges(current)
        repaired.append(current)
    return repaired


def _torso_anchor(frame):
    alpha = frame[:, :, 3]
    points = cv2.findNonZero((alpha > 32).astype(np.uint8))
    if points is None:
        raise RuntimeError("walk frame has no person alpha")
    x, y, width, height = cv2.boundingRect(points)
    band = alpha[
        y + round(height * 0.20):y + round(height * 0.60),
        x:x + width,
    ] > 32
    _rows, columns = np.where(band)
    return float(x + (np.median(columns) if columns.size else width / 2))


def _recenter_walk_frames(frames):
    anchors = np.array([_torso_anchor(frame) for frame in frames], dtype=np.float64)
    target = frames[0].shape[1] / 2
    recentered = []
    for frame, anchor in zip(frames, anchors):
        matrix = np.array([[1, 0, target - anchor], [0, 1, 0]], dtype=np.float32)
        recentered.append(cv2.warpAffine(
            frame,
            matrix,
            (frame.shape[1], frame.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0, 0),
        ))
    return recentered, anchors


def _trajectory_profile(anchors, start, end, fps, scale):
    selected = np.asarray(anchors[start:end], dtype=np.float64)
    if selected.size < 8:
        return None
    times = np.arange(selected.size, dtype=np.float64) / max(1, fps)
    slope, intercept = np.polyfit(times, selected, 1)
    predicted = slope * times + intercept
    residual = float(np.sum((selected - predicted) ** 2))
    total = float(np.sum((selected - selected.mean()) ** 2))
    r_squared = 1.0 - residual / max(total, 1e-6)
    cycle_seconds = selected.size / max(1, fps)
    cycle_distance = float(slope * cycle_seconds * scale)
    if slope <= 0 or r_squared < 0.72 or cycle_distance < 12:
        return None

    offsets = (selected - selected[0]) * scale
    if offsets.size > 2:
        smooth = offsets.copy()
        for index in range(1, offsets.size - 1):
            smooth[index] = np.median(offsets[index - 1:index + 2])
        offsets = smooth
    offsets = np.maximum.accumulate(np.maximum(0, offsets))
    target_last = cycle_distance * (selected.size - 1) / selected.size
    if offsets[-1] > 1e-6:
        offsets *= target_last / offsets[-1]
    else:
        offsets = np.linspace(0, target_last, selected.size)
    return {
        "speed_method": "source-root-trajectory",
        "trajectory_r2": round(r_squared, 4),
        "cycle_distance": round(cycle_distance, 2),
        "ground_speed": round(cycle_distance / cycle_seconds, 2),
        "travel_offsets": [round(float(value), 2) for value in offsets],
        "continuous_source_frames": True,
    }


def _loop_feature(frame):
    resized = cv2.resize(frame, (48, 72), interpolation=cv2.INTER_AREA).astype(np.float32)
    alpha = resized[:, :, 3:4] / 255.0
    premultiplied = resized[:, :, :3] * alpha / 255.0
    return np.concatenate((alpha, premultiplied), axis=2)


def _select_loop(frames, fps, target_seconds, minimum_seconds, maximum_seconds):
    features = [_loop_feature(frame) for frame in frames]
    minimum = max(8, round(minimum_seconds * fps))
    maximum = min(len(frames) - 1, round(maximum_seconds * fps))
    target = round(target_seconds * fps)
    if maximum <= minimum:
        return frames, 0, len(frames)
    best = None
    for start in range(0, len(frames) - minimum, 2):
        for length in range(minimum, maximum + 1, 2):
            end = start + length
            if end >= len(frames):
                break
            difference = float(np.mean(np.abs(features[start] - features[end])))
            duration_penalty = abs(length - target) / max(1, target) * 0.055
            score = difference + duration_penalty
            if best is None or score < best[0]:
                best = (score, start, end)
    if best is None:
        return frames, 0, len(frames)
    return frames[best[1]:best[2]], best[1], best[2]


def _alpha_union(frames):
    left = min(frame.shape[1] for frame in frames)
    top = min(frame.shape[0] for frame in frames)
    right = 0
    bottom = 0
    found = False
    for frame in frames:
        points = cv2.findNonZero((frame[:, :, 3] > 8).astype(np.uint8))
        if points is None:
            continue
        x, y, width, height = cv2.boundingRect(points)
        left = min(left, x)
        top = min(top, y)
        right = max(right, x + width)
        bottom = max(bottom, y + height)
        found = True
    if not found:
        raise RuntimeError("motion alpha sequence is empty")
    pad_x = round((right - left) * 0.07)
    pad_y = round((bottom - top) * 0.035)
    frame_height, frame_width = frames[0].shape[:2]
    return (
        max(0, left - pad_x),
        max(0, top - pad_y),
        min(frame_width, right + pad_x),
        min(frame_height, bottom + pad_y),
    )


def _normalise_frames(frames, include_scale=False):
    left, top, right, bottom = _alpha_union(frames)
    crop_width = right - left
    crop_height = bottom - top
    scale = min(TARGET_WIDTH * 0.94 / crop_width, TARGET_HEIGHT * 0.97 / crop_height)
    output_width = max(1, round(crop_width * scale))
    output_height = max(1, round(crop_height * scale))
    offset_x = (TARGET_WIDTH - output_width) // 2
    offset_y = TARGET_HEIGHT - output_height - round(TARGET_HEIGHT * 0.012)
    normalised = []
    for frame in frames:
        crop = frame[top:bottom, left:right]
        resized = cv2.resize(crop, (output_width, output_height), interpolation=cv2.INTER_AREA)
        resized[:, :, 3][resized[:, :, 3] < 16] = 0
        resized = cutout._decontaminate_edges(resized)
        canvas = np.zeros((TARGET_HEIGHT, TARGET_WIDTH, 4), dtype=np.uint8)
        canvas[offset_y:offset_y + output_height, offset_x:offset_x + output_width] = resized
        normalised.append(canvas)
    points = cv2.findNonZero((np.maximum.reduce(
        [(frame[:, :, 3] > 8).astype(np.uint8) for frame in normalised])))
    bounds = [0, 0, TARGET_WIDTH, TARGET_HEIGHT]
    if points is not None:
        bounds = [int(value) for value in cv2.boundingRect(points)]
    return (normalised, bounds, scale) if include_scale else (normalised, bounds)


def _edge_anchors(frames, bounds):
    x, y, width, height = bounds
    bottom = y + round(height * 0.62)
    left_frames = []
    right_frames = []
    for frame in frames:
        _rows, columns = np.where(frame[y:bottom, :, 3] > 32)
        if columns.size:
            left_frames.append(round(float(np.percentile(columns, 1)), 2))
            right_frames.append(round(float(np.percentile(columns, 99)), 2))
        else:
            left_frames.append(float(x))
            right_frames.append(float(x + width))
    return {
        "left": round(float(np.median(left_frames)), 2),
        "right": round(float(np.median(right_frames)), 2),
        "left_frames": left_frames,
        "right_frames": right_frames,
    }


def _foot_centers(frame, bounds, band_start):
    x, y, width, height = bounds
    mask = frame[y + round(height * band_start):y + height, :, 3] > 48
    _rows, columns = np.where(mask)
    if columns.size < 30:
        return None
    left, right = np.percentile(columns, [25, 75])
    for _iteration in range(12):
        split = (left + right) / 2
        left_group = columns[columns <= split]
        right_group = columns[columns > split]
        if left_group.size < 8 or right_group.size < 8:
            break
        left = float(np.median(left_group))
        right = float(np.median(right_group))
    return np.array([left, right], dtype=np.float64)


def _stance_calibrated_trajectory(frames, bounds, trajectory):
    offsets = np.asarray(trajectory["travel_offsets"], dtype=np.float64)
    cycle_distance = float(trajectory["cycle_distance"])
    base_deltas = np.diff(np.r_[offsets, cycle_distance])
    candidates = []
    for band_start in (0.68, 0.72, 0.76):
        feet = [_foot_centers(frame, bounds, band_start) for frame in frames]
        if any(value is None for value in feet):
            continue
        best = None
        for scale in np.linspace(0.65, 2.2, 156):
            errors = []
            for index, delta in enumerate(base_deltas):
                current = feet[index]
                following = feet[(index + 1) % len(feet)]
                errors.append(min(
                    abs(float(next_x - current_x + delta * scale))
                    for current_x in current for next_x in following
                ))
            score = (float(np.median(errors)), float(np.mean(np.square(errors))))
            if best is None or score < best[0]:
                best = (score, float(scale), errors)
        if best:
            candidates.append(best)
    if not candidates:
        return trajectory
    scale = float(np.median([candidate[1] for candidate in candidates]))
    calibrated = dict(trajectory)
    calibrated["speed_method"] = "stance-foot-calibrated-source-trajectory"
    calibrated["stance_scale"] = round(scale, 3)
    calibrated["stance_slip_pixels"] = round(float(np.median([
        error for _score, _scale, errors in candidates for error in errors
    ])), 3)
    calibrated["cycle_distance"] = round(cycle_distance * scale, 2)
    calibrated["ground_speed"] = round(float(trajectory["ground_speed"]) * scale, 2)
    calibrated["travel_offsets"] = [round(float(value * scale), 2) for value in offsets]
    return calibrated


def _gait_metrics(frames, fps, bounds, trajectory=None):
    x, y, width, height = bounds
    spans = []
    lower_y = min(TARGET_HEIGHT - 1, y + round(height * 0.62))
    upper_y = min(TARGET_HEIGHT, y + height)
    for frame in frames:
        mask = frame[lower_y:upper_y, :, 3] > 48
        _rows, columns = np.where(mask)
        if columns.size < 40:
            continue
        left = float(np.percentile(columns, 25))
        right = float(np.percentile(columns, 75))
        for _iteration in range(8):
            split = (left + right) / 2
            left_group = columns[columns <= split]
            right_group = columns[columns > split]
            if left_group.size < 12 or right_group.size < 12:
                break
            left = float(np.median(left_group))
            right = float(np.median(right_group))
        separation = right - left
        if separation > max(10, width * 0.12):
            spans.append(separation)
    measured_step = float(np.percentile(spans, 85)) if spans else width * 0.28
    cycle_seconds = len(frames) / max(1, fps)
    if trajectory:
        stride_pixels = float(trajectory["cycle_distance"])
        ground_speed = float(trajectory["ground_speed"])
    else:
        stride_pixels = float(np.clip(
            measured_step * 2,
            height * 0.34,
            height * 0.58,
        ))
        ground_speed = stride_pixels / max(0.1, cycle_seconds)
    return {
        "cycle_seconds": round(cycle_seconds, 4),
        "step_span_pixels": round(measured_step, 2),
        "stride_pixels": round(stride_pixels, 2),
        "ground_speed": round(ground_speed, 2),
        **(trajectory or {}),
    }


def _pack_sheets(frames, destination, kind):
    sheets = []
    for sheet_index, start in enumerate(range(0, len(frames), MAX_SHEET_FRAMES)):
        batch = frames[start:start + MAX_SHEET_FRAMES]
        columns = min(8, len(batch))
        rows = math.ceil(len(batch) / columns)
        atlas = np.zeros((rows * TARGET_HEIGHT, columns * TARGET_WIDTH, 4), dtype=np.uint8)
        for local_index, frame in enumerate(batch):
            column = local_index % columns
            row = local_index // columns
            atlas[
                row * TARGET_HEIGHT:(row + 1) * TARGET_HEIGHT,
                column * TARGET_WIDTH:(column + 1) * TARGET_WIDTH,
            ] = frame
        name = f"{kind}-{sheet_index}.png"
        cv2.imwrite(os.path.join(destination, name), atlas, [cv2.IMWRITE_PNG_COMPRESSION, 9])
        sheets.append({
            "image": name,
            "first": start,
            "count": len(batch),
            "columns": columns,
            "rows": rows,
        })
    return sheets


def _encode_alpha_preview(frames, fps, destination):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    try:
        encoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
            timeout=30, stdin=subprocess.DEVNULL)
        if "prores_ks" not in encoders.stdout:
            return None
        with tempfile.TemporaryDirectory(prefix=".alpha-video-") as directory:
            for index, frame in enumerate(frames):
                cv2.imwrite(os.path.join(directory, f"{index:04d}.png"), frame)
            result = subprocess.run([
                ffmpeg, "-y", "-loglevel", "error", "-framerate", str(fps),
                "-i", os.path.join(directory, "%04d.png"),
                "-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le",
                "-an", destination,
            ], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return destination if result.returncode == 0 and os.path.getsize(destination) > 8192 else None
    except Exception:
        return None


def _process_clip(kind, video, fps, stage, log):
    frames = _decode_video(video, fps)
    with tempfile.TemporaryDirectory(prefix=f".{kind}-frames-", dir=stage) as workspace:
        alpha_frames = _segment_frames(frames, workspace, log)
    anchors = None
    if kind == "walk":
        recentered, anchors = _recenter_walk_frames(alpha_frames)
        selected, loop_start, loop_end = _select_loop(recentered, fps, 1.05, 0.85, 1.35)
    else:
        selected, loop_start, loop_end = _select_loop(alpha_frames, fps, 3.2, 2.0, 5.2)
    normalised, bounds, scale = _normalise_frames(selected, include_scale=True)
    sheets = _pack_sheets(normalised, stage, kind)
    poster = f"{kind}-poster.png"
    cv2.imwrite(os.path.join(stage, poster), normalised[0], [cv2.IMWRITE_PNG_COMPRESSION, 9])
    alpha_name = f"{kind}-alpha.mov"
    alpha_path = _encode_alpha_preview(normalised, fps, os.path.join(stage, alpha_name))
    if kind == "walk":
        trajectory = _trajectory_profile(anchors, loop_start, loop_end, fps, scale)
        if not trajectory:
            raise RuntimeError(
                "walk video did not contain a steady left-to-right root trajectory; regenerate it rather than estimating desktop speed")
        trajectory = _stance_calibrated_trajectory(normalised, bounds, trajectory)
        metrics = _gait_metrics(normalised, fps, bounds, trajectory)
    else:
        metrics = {"edge_anchors": _edge_anchors(normalised, bounds)}
    return {
        "fps": fps,
        "frames": len(normalised),
        "frame_width": TARGET_WIDTH,
        "frame_height": TARGET_HEIGHT,
        "bounds": bounds,
        "sheets": sheets,
        "poster": poster,
        "alpha_video": alpha_name if alpha_path else None,
        "source_loop": [int(loop_start), int(loop_end)],
        "continuous_source_frames": True,
        **metrics,
    }


def _build_context(avatar_dir, pose_reference):
    body_dir = os.path.join(avatar_dir, "body")
    body_manifest_path = os.path.join(body_dir, "body.json")
    body_source = next((
        os.path.join(body_dir, name) for name in os.listdir(body_dir)
        if name.startswith("source.") and os.path.isfile(os.path.join(body_dir, name))
    ), None) if os.path.isdir(body_dir) else None
    if not os.path.isfile(body_manifest_path) or not body_source:
        raise RuntimeError("generate a full body before creating Pet motion")
    with open(body_manifest_path) as handle:
        body_manifest = json.load(handle)
    if pose_reference and not os.path.isfile(pose_reference):
        raise RuntimeError("idle pose reference is missing")

    image_provider = body.default_provider()
    video_provider = body.default_video_provider()
    outfit = _clean((body_manifest.get("options") or {}).get("outfit"), 500) or "the exact existing outfit"
    prompts = {
        "walk_keyframe": _walk_keyframe_prompt(outfit),
        "idle_keyframe": _idle_keyframe_prompt(outfit, bool(pose_reference)),
        "walk_video": _walk_video_prompt(),
        "idle_video": _idle_video_prompt(),
    }
    signature_source = "\n".join((
        _sha256(body_source),
        _sha256(pose_reference) if pose_reference else "text-pose",
        image_provider["command_key"], str(image_provider.get("model")),
        video_provider["command_key"], str(video_provider.get("model")),
        *prompts.values(),
    ))
    signature = hashlib.sha256(signature_source.encode("utf-8")).hexdigest()
    cache_root = os.path.join(avatar_dir, ".motion-cache")
    cache = os.path.join(cache_root, signature)
    os.makedirs(cache, mode=0o700, exist_ok=True)
    return {
        "body_source": body_source,
        "image_provider": image_provider,
        "video_provider": video_provider,
        "prompts": prompts,
        "signature": signature,
        "cache_root": cache_root,
        "cache": cache,
    }


def preview_keyframes(avatar_dir, pose_reference=None, log=print):
    context = _build_context(avatar_dir, pose_reference)
    prompts = context["prompts"]
    keyframes = _generate_keyframes(
        context["cache"], context["image_provider"], context["body_source"], pose_reference,
        {"walk": prompts["walk_keyframe"], "idle": prompts["idle_keyframe"]}, log)
    preview_dir = os.path.join(avatar_dir, ".motion-preview")
    shutil.rmtree(preview_dir, ignore_errors=True)
    os.makedirs(preview_dir, mode=0o700)
    previews = {}
    for kind, source in keyframes.items():
        destination = os.path.join(preview_dir, f"{kind}.png")
        shutil.copy2(source, destination)
        previews[kind] = destination
    return previews


def build(avatar_dir, pose_reference=None, log=print, progress=None):
    context = _build_context(avatar_dir, pose_reference)
    body_source = context["body_source"]
    image_provider = context["image_provider"]
    video_provider = context["video_provider"]
    prompts = context["prompts"]
    signature = context["signature"]
    cache_root = context["cache_root"]
    cache = context["cache"]

    _emit(progress, "keyframes", 0.06, "Creating walk and edge-pose keyframes")
    keyframes = _generate_keyframes(
        cache, image_provider, body_source, pose_reference,
        {"walk": prompts["walk_keyframe"], "idle": prompts["idle_keyframe"]}, log)
    _emit(progress, "video", 0.32, "Animating natural walk and edge idle")
    videos = _generate_videos(
        cache, video_provider, keyframes,
        {"walk": prompts["walk_video"], "idle": prompts["idle_video"]}, log)

    stage = tempfile.mkdtemp(prefix=".motion-stage-", dir=avatar_dir)
    backup = os.path.join(avatar_dir, "motion.previous")
    destination = os.path.join(avatar_dir, "motion")
    try:
        _emit(progress, "alpha", 0.60, "Alpha-cutting the walk loop locally")
        walk = _process_clip("walk", videos["walk"], WALK_FPS, stage, log)
        _emit(progress, "alpha", 0.77, "Alpha-cutting the edge-idle loop locally")
        idle = _process_clip("idle", videos["idle"], IDLE_FPS, stage, log)
        raw_dir = os.path.join(stage, "raw")
        os.makedirs(raw_dir)
        for kind in ("walk", "idle"):
            shutil.copy2(keyframes[kind], os.path.join(raw_dir, f"{kind}-keyframe.png"))
            shutil.copy2(videos[kind], os.path.join(raw_dir, f"{kind}-source.mp4"))
        metadata = {
            "v": MOTION_VERSION,
            "signature": signature,
            "image_provider": image_provider,
            "video_provider": video_provider,
            "walk": walk,
            "idle": idle,
            "reference": {
                "file": None,
                "sha256": _sha256(pose_reference) if pose_reference else None,
                "use": "pose geometry only",
                "retained": False,
            },
            "prompts": prompts,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        with open(os.path.join(stage, "motion.json"), "w") as handle:
            json.dump(metadata, handle, indent=1)

        shutil.rmtree(backup, ignore_errors=True)
        if os.path.exists(destination):
            os.replace(destination, backup)
        os.replace(stage, destination)
        stage = None
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(cache_root, ignore_errors=True)
        shutil.rmtree(os.path.join(avatar_dir, ".motion-preview"), ignore_errors=True)
        _emit(progress, "done", 1.0, "Desktop motion ready")
        log("generated alpha walk and edge-idle motion")
        return metadata
    except Exception:
        if not os.path.exists(destination) and os.path.exists(backup):
            os.replace(backup, destination)
        raise
    finally:
        if stage and os.path.exists(stage):
            shutil.rmtree(stage, ignore_errors=True)


def remove(avatar_dir):
    shutil.rmtree(os.path.join(avatar_dir, "motion"), ignore_errors=True)
    shutil.rmtree(os.path.join(avatar_dir, ".motion-cache"), ignore_errors=True)
