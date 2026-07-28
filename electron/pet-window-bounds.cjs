'use strict';

function boundsForPetZoom(current, baseSize, minimumSize, zoom) {
  const width = Math.max(minimumSize.width, Math.round(baseSize.width * zoom));
  const height = Math.max(minimumSize.height, Math.round(baseSize.height * zoom));
  return {
    x: Math.round(current.x + (current.width - width) / 2),
    y: Math.round(current.y + (current.height - height) / 2),
    width,
    height,
  };
}

module.exports = { boundsForPetZoom };
