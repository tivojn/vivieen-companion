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

module.exports = {
  boundsForPetZoom,
  boundsForPetZoomAtAnchor,
  clampPetZoom,
  petZoomAnchor,
  petZoomSize,
  roamSizeForZoom,
};
