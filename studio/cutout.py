"""Local portrait cut-out through macOS Vision.

The runtime keeps JPEG viseme plates for compactness and ships one RGBA mask.
Every viseme was pose-locked to the same keyframe, so that single mask can clip
all mouth poses without producing a halo that changes while the avatar speaks.
"""
import os
import subprocess

import cv2
import numpy as np


CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def helper_path():
    configured = os.environ.get("VIVIEEN_CUTOUT_HELPER")
    candidates = [
        configured,
        os.path.join(CODE_ROOT, ".electron-native", "person-cutout"),
        os.path.join(CODE_ROOT, "native", "person-cutout"),
    ]
    return next((path for path in candidates if path and os.access(path, os.X_OK)), None)


def _decontaminate_edges(image):
    alpha = image[:, :, 3]
    kernel = np.ones((3, 3), np.uint8)
    foreground = alpha > 8
    core = cv2.erode((alpha > 96).astype("uint8"), kernel, iterations=3).astype(bool)
    filled = core.copy()
    colors = image[:, :, :3].astype("float32")
    propagated = np.zeros_like(colors)
    propagated[core] = colors[core]
    for _step in range(16):
        weights = cv2.boxFilter(filled.astype("float32"), -1, (3, 3), normalize=False)
        new = (~filled) & (weights > 0)
        if not np.any(new):
            break
        for channel in range(3):
            total = cv2.boxFilter(
                propagated[:, :, channel] * filled, -1, (3, 3), normalize=False)
            propagated[:, :, channel][new] = total[new] / weights[new]
        filled[new] = True
    interior = cv2.erode(foreground.astype("uint8"), kernel, iterations=3).astype(bool)
    boundary = foreground & ~interior
    contaminated = foreground & (alpha < 246)
    replace = (boundary | contaminated) & filled
    image[:, :, :3][replace] = np.clip(propagated[replace], 0, 255).astype("uint8")
    return image


def render(source, destination, log=print, tight=False):
    helper = helper_path()
    if not helper:
        log("  cutout unavailable: macOS Vision helper is not installed")
        return None
    try:
        result = subprocess.run(
            [helper, source, destination],
            capture_output=True,
            text=True,
            timeout=180,
            stdin=subprocess.DEVNULL,
        )
    except Exception as error:
        log(f"  cutout failed: {error}")
        return None
    if result.returncode or not os.path.exists(destination):
        detail = (result.stderr or result.stdout or "unknown error").strip()[-240:]
        log(f"  cutout failed: {detail}")
        return None
    image = cv2.imread(destination, cv2.IMREAD_UNCHANGED)
    if image is None or image.ndim != 3 or image.shape[2] != 4:
        log("  cutout failed: helper did not produce an RGBA image")
        return None
    if tight:
        image = _decontaminate_edges(image)
        alpha = cv2.erode(image[:, :, 3], np.ones((3, 3), np.uint8), iterations=1)
        image[:, :, 3] = cv2.GaussianBlur(alpha, (0, 0), 0.55)
        cv2.imwrite(destination, image)
    alpha = image[:, :, 3]
    points = cv2.findNonZero((alpha > 8).astype("uint8"))
    if points is None:
        log("  cutout failed: person mask is empty")
        return None
    x, y, width, height = cv2.boundingRect(points)
    coverage = float((alpha > 8).mean())
    log(f"  cutout ready: {coverage * 100:.1f}% foreground")
    return {
        "src": "assets/cutout.png",
        "bounds": [int(x), int(y), int(width), int(height)],
        "coverage": round(coverage, 4),
    }
