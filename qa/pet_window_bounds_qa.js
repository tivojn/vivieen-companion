'use strict';

const assert = require('node:assert/strict');
const { boundsForPetZoom } = require('../electron/pet-window-bounds.cjs');

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

console.log('pet window bounds QA passed');
