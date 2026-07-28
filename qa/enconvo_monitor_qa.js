'use strict';

const assert = require('node:assert/strict');
const { EventEmitter, once } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const { PassThrough } = require('node:stream');
const vm = require('node:vm');
const { EnconvoAudioMonitor } = require('../electron/enconvo-audio-monitor.cjs');

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.killedWith = null;
  child.kill = (signal) => {
    child.killedWith = signal;
    queueMicrotask(() => child.emit('exit', 0, signal));
    return true;
  };
  return child;
}

async function testStreamingLifecycle() {
  const children = [];
  const calls = [];
  const monitor = new EnconvoAudioMonitor({
    helperPath: '/tmp/enconvo-audio-tap',
    fileExists: () => true,
    spawnProcess: (executable, args) => {
      calls.push({ executable, args });
      const child = fakeChild();
      children.push(child);
      return child;
    },
  });

  monitor.setEnabled(true);
  assert.equal(monitor.snapshot().status, 'starting');
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].args.slice(0, 4), [
    '--bundle-id', 'com.frostyeve.enconvo', '--name', 'EnConvo',
  ]);

  children[0].stdout.write('{"type":"rea');
  children[0].stdout.write('dy","processCount":3}\n');
  assert.equal(monitor.snapshot().status, 'ready');
  assert.equal(monitor.snapshot().processCount, 3);

  const samplePromise = once(monitor, 'sample');
  children[0].stdout.write('{"type":"sample","rms":0.1,"active":true,"high":0.4,"timestamp":12345}\n');
  const [sample] = await samplePromise;
  assert.equal(sample.rms, 0.1);
  assert.equal(sample.active, true);
  assert.equal(sample.high, 0.4);
  assert.equal(sample.timestamp, 12345);

  monitor.setEnabled(false);
  assert.equal(monitor.snapshot().status, 'off');
  assert.equal(children[0].killedWith, 'SIGTERM');
  monitor.dispose();
}

async function testWaitingRetry() {
  const children = [];
  const monitor = new EnconvoAudioMonitor({
    helperPath: '/tmp/enconvo-audio-tap',
    fileExists: () => true,
    retryMs: 5,
    spawnProcess: () => {
      const child = fakeChild();
      children.push(child);
      return child;
    },
  });

  monitor.setEnabled(true);
  children[0].stdout.write('{"type":"error","code":"target_not_running"}\n');
  children[0].emit('exit', 3, null);
  assert.equal(monitor.snapshot().status, 'waiting');
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(children.length, 2);
  monitor.dispose();
}

function testExternalVisemeStability() {
  const root = path.resolve(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
  const start = html.indexOf('const EXTERNAL_XFADE=');
  const end = html.indexOf('/* Exact alignment legitimately', start);
  assert.ok(start >= 0 && end > start, 'external stabilizer source is present');
  const context = { Date, performance: { now: () => 0 } };
  vm.runInNewContext(
    html.slice(start, end) + '\nthis.qa={resetExternalViseme,stabiliseExternalViseme,EXTERNAL_XFADE};',
    context,
  );
  const { resetExternalViseme, stabiliseExternalViseme, EXTERNAL_XFADE } = context.qa;
  assert.equal(EXTERNAL_XFADE, 0.065);

  resetExternalViseme(0);
  assert.equal(stabiliseExternalViseme('aa', 0, true), 'sil');
  assert.equal(stabiliseExternalViseme('aa', 25, true), 'sil');
  assert.equal(stabiliseExternalViseme('aa', 50, true), 'aa');

  assert.equal(stabiliseExternalViseme('CH', 75, true), 'aa');
  assert.equal(stabiliseExternalViseme('CH', 75, true), 'aa',
    're-reading one packet on render frames cannot confirm a pose');
  assert.equal(stabiliseExternalViseme('E', 100, true), 'aa');
  assert.equal(stabiliseExternalViseme('aa', 125, true), 'aa',
    'single-packet spectrum changes do not flip the mouth');

  assert.equal(stabiliseExternalViseme('E', 150, true), 'aa');
  assert.equal(stabiliseExternalViseme('E', 175, true), 'aa');
  assert.equal(stabiliseExternalViseme('E', 200, true), 'aa');
  assert.equal(stabiliseExternalViseme('E', 225, true), 'E');
  assert.equal(stabiliseExternalViseme('CH', 220, true), 'E',
    'out-of-order packets cannot rewind articulation');

  assert.equal(stabiliseExternalViseme('aa', 250, true), 'E');
  assert.equal(stabiliseExternalViseme('aa', 275, true), 'E');
  assert.equal(stabiliseExternalViseme('aa', 300, true), 'E');
  assert.equal(stabiliseExternalViseme('aa', 325, true), 'E');
  assert.equal(stabiliseExternalViseme('aa', 350, true), 'aa');
  assert.equal(stabiliseExternalViseme('sil', 375, false), 'aa');
  assert.equal(stabiliseExternalViseme('sil', 400, false), 'aa');
  assert.equal(stabiliseExternalViseme('sil', 425, false), 'aa');
  assert.equal(stabiliseExternalViseme('sil', 450, false), 'sil');
}

function testNoDropPollingFallback() {
  const backlog = [];
  const delivered = [];
  let sequence = 0;
  let deliveredSequence = 0;

  const fetchSamples = (afterSequence) => {
    const after = afterSequence > 0 ? afterSequence : Math.max(0, sequence - 1);
    return backlog.filter((sample) => sample.sequence > after);
  };
  const poll = () => {
    const samples = fetchSamples(deliveredSequence);
    delivered.push(...samples);
    if (samples.length) deliveredSequence = samples.at(-1).sequence;
  };

  for (let elapsed = 0; elapsed < 3840; elapsed += 16) {
    if (elapsed % 32 === 0) {
      sequence += 1;
      backlog.push({
        sequence,
        active: sequence <= 111 && (sequence - 1) % 10 === 0,
      });
      if (backlog.length > 160) backlog.shift();
    }
    const delayedPoll = (elapsed >= 640 && elapsed < 800)
      || (elapsed >= 1760 && elapsed < 1920);
    if (!delayedPoll) poll();
  }
  poll();

  assert.deepEqual(
    delivered.map((sample) => sample.sequence),
    Array.from({ length: sequence }, (_value, index) => index + 1),
    '16ms polling plus the sequence backlog delivers every native packet exactly once',
  );
  assert.equal(delivered.filter((sample) => sample.active).length, 12,
    'single-packet short words survive delayed renderer polls');
}

function testIntegrationContract() {
  const root = path.resolve(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'web', 'index.html'), 'utf8');
  const main = fs.readFileSync(path.join(root, 'electron', 'main.cjs'), 'utf8');
  const preload = fs.readFileSync(path.join(root, 'electron', 'preload.cjs'), 'utf8');
  const nativeBuild = fs.readFileSync(path.join(root, 'scripts', 'build-audio-tap.sh'), 'utf8');
  const afterSign = fs.readFileSync(path.join(root, 'scripts', 'after-sign.cjs'), 'utf8');
  const packageConfig = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const audioResource = packageConfig.build.extraResources.find(
    (resource) => resource.to === 'native/enconvo-audio-tap',
  );
  assert.match(html, /id="monitor"/);
  assert.match(html, /#row\.monitoring #manual/);
  assert.match(html, /stabiliseExternalViseme\(candidate,sampleTimestamp,externalAudio\.active\)/);
  assert.match(html, /if\(monitorEnabled\)return externalPose/);
  assert.match(html, /const xfade=monitorEnabled\?EXTERNAL_XFADE:XFADE/);
  assert.match(html, /const MONITOR_POLL_MS=16/);
  assert.match(html, /SHELL\.getEnconvoSamples\(monitorSampleSequence\)/);
  assert.match(html, /packet\.samples\.forEach\(handleMonitorSample\)/);
  assert.match(html, /!monitorEnabled&&e\.code==='Space'/);
  assert.match(main, /vivieen:set-enconvo-monitor/);
  assert.match(main, /vivieen:get-enconvo-samples/);
  assert.match(main, /MAX_ENCONVO_SAMPLE_BACKLOG = 160/);
  assert.match(main, /sample\.sequence > after/);
  assert.match(main, /'enconvo-audio-tap'/);
  assert.doesNotMatch(main, /VivieenAudioTap\.app/);
  assert.deepEqual(audioResource, {
    from: '.electron-native/enconvo-audio-tap',
    to: 'native/enconvo-audio-tap',
  });
  assert.equal(packageConfig.build.afterSign, 'scripts/after-sign.cjs');
  assert.match(nativeBuild, /--identifier com\.vivieen\.companion/);
  assert.match(afterSign, /const APP_ID = 'com\.vivieen\.companion'/);
  assert.match(afterSign, /const appSignature = inspectSignature\(appPath\)/);
  assert.match(afterSign, /identifier: APP_ID/);
  assert.match(afterSign, /sign\(appPath, identity/);
  assert.equal(
    packageConfig.build.extraResources.some((resource) => /\.app$/.test(resource.to)),
    false,
    'the release contains no nested audio-monitor application',
  );
  assert.match(preload, /onEnconvoAudio/);
  assert.match(preload, /getEnconvoSamples/);
}

(async () => {
  await testStreamingLifecycle();
  await testWaitingRetry();
  testExternalVisemeStability();
  testNoDropPollingFallback();
  testIntegrationContract();
  console.log('enconvo monitor QA passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});