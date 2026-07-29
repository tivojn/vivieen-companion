'use strict';

const assert = require('node:assert/strict');
const {
  boundsForPetZoom,
  boundsForPetZoomAtAnchor,
  clampPetZoom,
  petZoomAnchor,
  roamSizeForZoom,
} = require('../electron/pet-window-bounds.cjs');

const current = { x: 962, y: 63, width: 706, height: 904 };
const base = { width: 560, height: 760 };
const minimum = { width: 140, height: 190 };

function assertCenterPreserved(bounds) {
  assert.ok(Math.abs(
    bounds.x + bounds.width / 2 - (current.x + current.width / 2),
  ) <= 0.5);
  assert.ok(Math.abs(
    bounds.y + bounds.height / 2 - (current.y + current.height / 2),
  ) <= 0.5);
}

const enlarged = boundsForPetZoom(current, base, minimum, 1.5);
assert.deepEqual(enlarged, { x: 895, y: -55, width: 840, height: 1140 });
assertCenterPreserved(enlarged);

const oversized = boundsForPetZoom(current, base, minimum, 4);
assert.deepEqual(oversized, { x: 195, y: -1005, width: 2240, height: 3040 });
assertCenterPreserved(oversized);

const reduced = boundsForPetZoom(current, base, minimum, 0.1);
assert.deepEqual(reduced, { x: 1245, y: 420, width: 140, height: 190 });
assertCenterPreserved(reduced);

// A live pinch re-applies the zoom every frame. Measured from a fixed anchor
// the window stays put; measured from its own last bounds it walks away.
const anchor = petZoomAnchor(current);
let stepped = { ...current };
for (let step = 0; step <= 200; step += 1) {
  const zoom = 0.5 + step * 0.0175;
  const anchored = boundsForPetZoomAtAnchor(anchor, base, minimum, zoom);
  assert.ok(Math.abs(anchored.x + anchored.width / 2 - anchor.x) <= 0.5);
  assert.ok(Math.abs(anchored.y + anchored.height / 2 - anchor.y) <= 0.5);
  stepped = boundsForPetZoom(stepped, base, minimum, zoom);
}
assert.deepEqual(
  boundsForPetZoomAtAnchor(anchor, base, minimum, 1.5),
  { x: 895, y: -55, width: 840, height: 1140 });

const roamBase = { width: 250, height: 340 };
const roamMinimum = { width: 96, height: 130 };
assert.deepEqual(roamSizeForZoom(roamBase, roamMinimum, 1), { width: 250, height: 340 });
assert.deepEqual(roamSizeForZoom(roamBase, roamMinimum, 2.5), { width: 625, height: 850 });
assert.deepEqual(roamSizeForZoom(roamBase, roamMinimum, 0.2), { width: 96, height: 130 });

const roamRange = { min: 0.5, max: 3 };
assert.equal(clampPetZoom(9, roamRange), 3);
assert.equal(clampPetZoom(0.1, roamRange), 0.5);
assert.equal(clampPetZoom('nope', roamRange), 1);
assert.equal(clampPetZoom(1.33, { min: 0.25, max: 4 }), 1.33);

console.log('pet window bounds QA passed');
