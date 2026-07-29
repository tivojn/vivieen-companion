'use strict';

/* Replays the real capture qa/proof/enconvo-driven-voice.wav through the REAL
   Follow-EnConvo mouth pipeline and measures how much articulation reaches the
   face. The Swift AudioFeatureAnalyzer is ported here (it is fixed native
   behaviour); the classifier and stabiliser are extracted verbatim from
   web/index.html so this always measures the code that ships.

   Numbers it guards against (docs/follow-enconvo-lipsync-diagnosis.md): the
   original classify-then-confirm gate let 39 of 234 proposed mouth changes
   through and froze the mouth for 2,432 ms mid-speech.

   VV_BASELINE=1 prints measurements without asserting the quality gates. */

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const WAV = path.join(root, 'qa', 'proof', 'enconvo-driven-voice.wav');
const BASELINE = process.env.VV_BASELINE === '1';

/* qa/proof/ is gitignored, so the capture only exists on machines that made
   one - skip (don't fail) where it is absent, e.g. CI. */
if (!fs.existsSync(WAV)) {
  console.log('enconvo replay QA skipped: qa/proof/enconvo-driven-voice.wav not present (local-only capture)');
  process.exit(0);
}

/* ---------------- wav (PCM16 mono, as captured) ---------------- */
function readWav(file) {
  const b = fs.readFileSync(file);
  assert.equal(b.toString('ascii', 0, 4), 'RIFF');
  assert.equal(b.toString('ascii', 8, 12), 'WAVE');
  let off = 12, fmt = null, data = null;
  while (off + 8 <= b.length) {
    const id = b.toString('ascii', off, off + 4);
    const size = b.readUInt32LE(off + 4);
    if (id === 'fmt ') fmt = { code: b.readUInt16LE(off + 8), channels: b.readUInt16LE(off + 10),
      rate: b.readUInt32LE(off + 12), bits: b.readUInt16LE(off + 22) };
    if (id === 'data') data = b.subarray(off + 8, off + 8 + size);
    off += 8 + size + (size & 1);
  }
  assert.ok(fmt && data, 'wav has fmt and data chunks');
  assert.equal(fmt.code, 1, 'PCM');
  assert.equal(fmt.bits, 16, '16-bit');
  const frames = Math.floor(data.length / 2 / fmt.channels);
  const y = new Float64Array(frames);
  for (let i = 0; i < frames; i++) {
    let s = 0;
    for (let c = 0; c < fmt.channels; c++) s += data.readInt16LE((i * fmt.channels + c) * 2);
    y[i] = s / fmt.channels / 32768;
  }
  return { y, rate: fmt.rate };
}

/* ---------------- Swift AudioFeatureAnalyzer port ----------------
   Two one-pole splits at 520/2400 Hz, per-buffer RMS + smoothing constants and
   the 25 ms emission throttle, which quantises to buffer boundaries - at 24 kHz
   with 256-sample buffers that lands on 32 ms packets, matching the tap. */
function analyse(y, rate, buf = 256) {
  const lowC = 1 - Math.exp(-2 * Math.PI * 520 / rate);
  const midC = 1 - Math.exp(-2 * Math.PI * 2400 / rate);
  const low = new Float64Array(y.length), mid = new Float64Array(y.length);
  let ls = 0, ms = 0;
  for (let i = 0; i < y.length; i++) {
    ls += lowC * (y[i] - ls); ms += midC * (y[i] - ms);
    low[i] = ls; mid[i] = ms - ls;
  }
  const packets = [];
  let sR = 0, sP = 0, sL = 0, sM = 0, sH = 0, sZ = 0;
  let lastEmit = -1e18, lastVoice = null;
  for (let b = 0; (b + 1) * buf <= y.length; b++) {
    const a = b * buf, e = a + buf;
    let r = 0, lo = 0, mi = 0, hi = 0, cross = 0, peak = 0;
    for (let i = a; i < e; i++) {
      const h = y[i] - mid[i] - low[i];
      r += y[i] * y[i]; lo += low[i] * low[i]; mi += mid[i] * mid[i]; hi += h * h;
      if (i > a && (y[i] < 0) !== (y[i - 1] < 0)) cross++;
      const p = Math.abs(y[i]); if (p > peak) peak = p;
    }
    r = Math.sqrt(r / buf); lo = Math.sqrt(lo / buf); mi = Math.sqrt(mi / buf); hi = Math.sqrt(hi / buf);
    const bt = Math.max(1e-6, lo + mi + hi);
    sR = sR * 0.58 + r * 0.42;
    sP = Math.max(peak, sP * 0.72);
    sL = sL * 0.55 + (lo / bt) * 0.45;
    sM = sM * 0.55 + (mi / bt) * 0.45;
    sH = sH * 0.55 + (hi / bt) * 0.45;
    sZ = sZ * 0.62 + (cross / buf) * 0.38;
    const now = e / rate * 1000;
    if (sR > 0.003) lastVoice = now;
    if (now - lastEmit < 25) continue;
    lastEmit = now;
    packets.push({ t: now, rms: sR, peak: sP, low: sL, mid: sM, high: sH, zcr: sZ,
      active: lastVoice !== null && now - lastVoice < 190 });
  }
  return packets;
}

/* ---------------- the real shipped pipeline ---------------- */
function shippedPipeline() {
  const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
  const slice = (from, to) => {
    const start = html.indexOf(from);
    const end = html.indexOf(to, start);
    assert.ok(start >= 0 && end > start, `found source between ${from} and ${to}`);
    return html.slice(start, end);
  };
  const manifest = JSON.parse(fs.readFileSync(
    path.join(root, 'avatars', 'dario-ref', 'runtime', 'manifest.json'), 'utf8'));
  const context = {
    Date, performance: { now: () => 0 },
    M: { visemes: manifest.visemes },
    externalAudio: { rms: 0, peak: 0, low: 0, mid: 0, high: 0, zcr: 0, active: false },
  };
  vm.runInNewContext(
    slice('const EXTERNAL_XFADE=', '/* Exact alignment legitimately') +
    slice('function availablePose', '/* fallback only:') +
    '\nthis.qa={resetExternalViseme,stabiliseExternalViseme,byExternalEnergy};',
    context,
  );
  return { context, qa: context.qa };
}

/* ---------------- replay + measure ---------------- */
function measure(packets) {
  const { context, qa } = shippedPipeline();
  qa.resetExternalViseme(0);
  let candidate = 'sil', pose = 'sil', proposals = 0;
  const poseChanges = [];
  const trace = [];
  for (const p of packets) {
    Object.assign(context.externalAudio, {
      rms: p.rms, peak: p.peak, low: p.low, mid: p.mid, high: p.high,
      zcr: p.zcr, active: p.active,
    });
    const c = p.active ? qa.byExternalEnergy() : 'sil';
    if (c !== candidate) { proposals++; candidate = c; }
    const next = qa.stabiliseExternalViseme(c, p.t, p.active);
    if (next !== pose) { poseChanges.push({ t: p.t, from: pose, to: next }); pose = next; }
    trace.push({ t: p.t, active: p.active, pose: next });
  }
  /* longest stretch of unchanged mouth while >80% of its packets are voiced */
  const marks = [packets[0].t, ...poseChanges.map((c) => c.t), packets.at(-1).t];
  let freeze = { span: 0, at: 0 };
  for (let i = 0; i + 1 < marks.length; i++) {
    const seg = trace.filter((s) => s.t >= marks[i] && s.t <= marks[i + 1]);
    if (!seg.length || seg.filter((s) => s.active).length / seg.length <= 0.8) continue;
    const span = marks[i + 1] - marks[i];
    if (span > freeze.span) freeze = { span, at: marks[i] };
  }
  const voiced = packets.filter((p) => p.active).length *
    (packets.at(-1).t - packets[0].t) / Math.max(1, packets.length - 1) / 1000;
  const shapes = {};
  for (const c of poseChanges) shapes[c.to] = (shapes[c.to] || 0) + 1;
  return { proposals, rendered: poseChanges.length, voiced, freeze, shapes };
}

const { y, rate } = readWav(WAV);
const packets = analyse(y, rate);
const intervals = packets.slice(1).map((p, i) => p.t - packets[i].t);
const meanInterval = intervals.reduce((a, b) => a + b, 0) / intervals.length;
const r = measure(packets);

console.log('clip: %ss at %d Hz | %d packets, mean interval %s ms, %ss voiced',
  (y.length / rate).toFixed(2), rate, packets.length, meanInterval.toFixed(1), r.voiced.toFixed(1));
console.log('classifier proposed %d viseme changes', r.proposals);
console.log('face rendered      %d changes -> %s/s (%d%% of proposals)',
  r.rendered, (r.rendered / r.voiced).toFixed(1), Math.round(100 * r.rendered / Math.max(1, r.proposals)));
console.log('longest frozen mouth while speaking: %d ms @ %ss',
  Math.round(r.freeze.span), (r.freeze.at / 1000).toFixed(1));
console.log('shapes shown:', Object.entries(r.shapes).sort((a, b) => b[1] - a[1])
  .map(([v, n]) => `${v}:${n}`).join(' '));

if (!BASELINE) {
  /* Quality gates - the shipped counter design measured 39 rendered (3.8/s),
     17% of proposals, and a 2,432 ms freeze. Real speech here runs 7.8
     syllables/s; anything below ~6 mouth changes/s reads as skipped words. */
  assert.ok(r.rendered / r.voiced >= 6,
    `mouth must change >=6x/s during speech (got ${(r.rendered / r.voiced).toFixed(1)})`);
  assert.ok(r.rendered / Math.max(1, r.proposals) >= 0.4,
    `>=40% of proposed articulation must reach the face (got ${Math.round(100 * r.rendered / r.proposals)}%)`);
  assert.ok(r.freeze.span <= 800,
    `mouth may not freeze >800ms mid-speech (got ${Math.round(r.freeze.span)}ms)`);
  console.log('enconvo replay QA passed');
}
