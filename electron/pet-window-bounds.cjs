'use strict';

function clampPetZoom(zoom, range) {
  const value = Number(zoom);
  const safe = Number.isFinite(value) && value > 0 ? value : 1;
  const minimum = Number(range && range.min);
  const maximum = Number(range && range.max);
  if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) return safe;
  return Math.max(minimum, Math.min(maximum, safe));
}

function petZoomSize(baseSize, minimumSize, zoom) {
  return {
    width: Math.max(minimumSize.width, Math.round(baseSize.width * zoom)),
    height: Math.max(minimumSize.height, Math.round(baseSize.height * zoom)),
  };
}

function boundsForPetZoom(current, baseSize, minimumSize, zoom) {
  const { width, height } = petZoomSize(baseSize, minimumSize, zoom);
  return {
    x: Math.round(current.x + (current.width - width) / 2),
    y: Math.round(current.y + (current.height - height) / 2),
    width,
    height,
  };
}

function petZoomAnchor(current) {
  return {
    x: current.x + current.width / 2,
    y: current.y + current.height / 2,
  };
}

// Live pinches re-apply the zoom dozens of times a second. Measuring every step
// from one anchor captured at gesture start keeps the rounding error from
// compounding, so the window grows in place instead of crawling across the desk.
function boundsForPetZoomAtAnchor(anchor, baseSize, minimumSize, zoom) {
  const { width, height } = petZoomSize(baseSize, minimumSize, zoom);
  return {
    x: Math.round(anchor.x - width / 2),
    y: Math.round(anchor.y - height / 2),
    width,
    height,
  };
}

function roamSizeForZoom(baseSize, minimumSize, zoom) {
  return petZoomSize(baseSize, minimumSize, zoom);
}

// A runaway pinch can leave the window larger than the screen, and the saved
// zoom then restores the same unreachable size on the next launch. Shrink the
// zoom until the window fits inside the work area (minus the margin) so the
// companion always comes back somewhere the pointer can reach it.
function fitPetZoomToArea(baseSize, minimumSize, zoom, area, margin = 0) {
  const value = Number(zoom);
  const safe = Number.isFinite(value) && value > 0 ? value : 1;
  const availableWidth = Math.max(minimumSize.width, area.width - margin * 2);
  const availableHeight = Math.max(minimumSize.height, area.height - margin * 2);
  const limit = Math.min(
    availableWidth / baseSize.width, availableHeight / baseSize.height);
  if (!Number.isFinite(limit) || limit <= 0) return safe;
  return Math.min(safe, limit);
}

// Bottom-right corner of the work area, which macOS already trims to exclude
// the Dock and the menu bar — the companion lands above the Dock, not under it.
function dockedPetBounds(size, area, margin = 0) {
  return {
    x: Math.round(area.x + area.width - size.width - margin),
    y: Math.round(area.y + area.height - size.height - margin),
    width: size.width,
    height: size.height,
  };
}

module.exports = {
  boundsForPetZoom,
  boundsForPetZoomAtAnchor,
  clampPetZoom,
  dockedPetBounds,
  fitPetZoomToArea,
  petZoomAnchor,
  petZoomSize,
  roamSizeForZoom,
};
