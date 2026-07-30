'use strict';

const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  globalShortcut,
  ipcMain,
  nativeImage,
  screen,
  session,
  shell,
} = require('electron');
const { spawn, execFile, execFileSync } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { EnconvoAudioMonitor } = require('./enconvo-audio-monitor.cjs');
const {
  boundsForPetZoom,
  boundsForPetZoomAtAnchor,
  clampPetZoom,
  dockedPetBounds,
  fitPetZoomToArea,
  petZoomAnchor,
  petZoomSize,
  roamSizeForZoom,
} = require('./pet-window-bounds.cjs');

app.setName('Vivieen');

const HOST = '127.0.0.1';
const DEFAULT_PORT = 8777;
const START_TIMEOUT_MS = 120_000;
const APP_ID = 'com.vivieen.companion';
const backendToken = randomBytes(32).toString('hex');

let port = Number(process.env.VIVIEEN_PORT || DEFAULT_PORT);
let backend = null;
let ownsBackend = false;
let quitting = false;
let mainWindow = null;
let settingsWindow = null;
let bubbleWindow = null;
let appearanceWindow = null;
let bubbleTimer = null;
let pendingBubble = '';
let tray = null;
let state = null;
let saveTimer = null;
let backendLog = null;
let enconvoMonitor = null;
let enconvoSampleSequence = 0;
let petDrag = null;
let petPointerTimer = null;
let petPointerInteractive = null;
let petPointerDebugAt = 0;
let petRoamTimer = null;
let petRoamRuntime = null;
let petZoomGesture = null;
let appearancePushAt = 0;
let petMotionReady = false;
let petMotionProfile = {
  walkSpeed: 64,
  cycleSeconds: 1.1,
  cycleDistance: 70.4,
  travelOffsets: [],
};
const enconvoSampleBuffer = [];
const MAX_ENCONVO_SAMPLE_BACKLOG = 160;
const PET_VIEWS = new Set(['full', 'three-quarter', 'half', 'bust', 'head', 'face']);
const PET_BASE_SIZE = Object.freeze({ width: 560, height: 760 });
const PET_NORMAL_MINIMUM = Object.freeze({ width: 140, height: 190 });
const PET_ZOOM_RANGE = Object.freeze({ min: 0.25, max: 4 });
const PET_DOCK_MARGIN = 28;
const PET_ROAM_SIZE = Object.freeze({ width: 250, height: 340 });
const PET_ROAM_MINIMUM = Object.freeze({ width: 96, height: 130 });
const PET_ROAM_ZOOM_RANGE = Object.freeze({ min: 0.5, max: 3 });
const PET_ROAM_MIN_SPEED = 42;
const PET_ROAM_MAX_SPEED = 150;
const PET_LEDGE_HOLD_MS = 9000;
const PET_INTERACTION_COOLDOWN_MS = 1400;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const baseUrl = () => `http://${HOST}:${port}`;
const codeRoot = () => app.isPackaged
  ? path.join(process.resourcesPath, 'backend')
  : path.resolve(__dirname, '..');
const dataRoot = () => process.env.VIVIEEN_DATA_DIR || (app.isPackaged
  ? path.join(app.getPath('userData'), 'backend-data')
  : codeRoot());
const statePath = () => path.join(app.getPath('userData'), 'window-state.json');
const logPath = () => path.join(app.getPath('userData'), 'backend.log');
const audioTapExecutable = () => path.join(
  app.isPackaged ? process.resourcesPath : codeRoot(),
  app.isPackaged ? 'native' : '.electron-native',
  'enconvo-audio-tap',
);
const keyTapExecutable = () => path.join(
  app.isPackaged ? process.resourcesPath : codeRoot(),
  app.isPackaged ? 'native' : '.electron-native',
  'key-tap',
);

// Holding the pet's head presses EnConvo's voice hotkey (right Option) for
// real: a synthesized flagsChanged event, down while held, up on release.
// Requires the Accessibility permission; the failure surfaces once per run.
let voiceKeyWarned = false;
function postVoiceKey(state) {
  const action = state === 'down' ? 'down' : state === 'up' ? 'up' : 'tap';
  execFile(keyTapExecutable(), [action], (error, _stdout, stderr) => {
    if (!error) return;
    const detail = String(stderr || error.message || '');
    if (detail.includes('accessibility-permission-missing')) {
      if (voiceKeyWarned) return;
      voiceKeyWarned = true;
      showSpeechBubble(
        'To let a head press reach EnConvo, allow Vivieen under System '
        + 'Settings → Privacy & Security → Accessibility.');
      return;
    }
    console.error(`[voice-key] ${detail.trim()}`);
  });
}

function ensureDataRoot() {
  fs.mkdirSync(dataRoot(), { recursive: true, mode: 0o700 });
}

function resolveMotionAsset(slug, relativePath) {
  if (!/^[a-z0-9](?:[a-z0-9-]{0,62})$/.test(String(slug || ''))) {
    throw new Error('Invalid avatar.');
  }
  const root = fs.realpathSync(path.join(dataRoot(), 'avatars', slug, 'motion'));
  const requested = String(relativePath || '').split(path.win32.sep).join('/');
  if (!requested || path.isAbsolute(requested)) throw new Error('Invalid motion asset.');
  const source = fs.realpathSync(path.resolve(root, requested));
  if (!source.startsWith(`${root}${path.sep}`)) throw new Error('Invalid motion asset.');
  if (!fs.statSync(source).isFile()) throw new Error('Motion asset is unavailable.');
  return source;
}

async function saveMotionAsset(event, asset = {}) {
  const source = resolveMotionAsset(asset.slug, asset.relativePath);
  const requestedName = path.basename(String(asset.defaultName || path.basename(source)));
  const defaultName = requestedName && requestedName !== '.' ? requestedName : path.basename(source);
  const extension = path.extname(defaultName).slice(1);
  const options = {
    title: 'Save motion asset',
    buttonLabel: 'Save',
    defaultPath: path.join(app.getPath('downloads'), defaultName),
    properties: ['createDirectory', 'showOverwriteConfirmation'],
    filters: extension ? [{ name: `${extension.toUpperCase()} file`, extensions: [extension] }] : [],
  };
  const owner = BrowserWindow.fromWebContents(event.sender);
  const result = owner
    ? await dialog.showSaveDialog(owner, options)
    : await dialog.showSaveDialog(options);
  if (result.canceled || !result.filePath) return { saved: false, canceled: true };
  if (path.resolve(result.filePath) !== source) {
    await fs.promises.copyFile(source, result.filePath);
  }
  return { saved: true, canceled: false, filePath: result.filePath };
}

function executable(file) {
  try {
    fs.accessSync(file, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function resolvePython() {
  const candidates = [
    process.env.VIVIEEN_PYTHON,
    path.join(codeRoot(), '.venv', 'bin', 'python'),
    path.join(process.resourcesPath, 'python', 'bin', 'python'),
    path.join(dataRoot(), '.venv', 'bin', 'python'),
    path.join(os.homedir(), 'vivieen-companion', '.venv', 'bin', 'python'),
    '/opt/homebrew/bin/python3',
    '/usr/local/bin/python3',
    '/usr/bin/python3',
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (executable(candidate)) return candidate;
  }
  try {
    const found = execFileSync('/usr/bin/which', ['python3'], { encoding: 'utf8' }).trim();
    if (found && executable(found)) return found;
  } catch {
    // Report one useful error below.
  }
  throw new Error(
    'No Python backend found. Set VIVIEEN_PYTHON or run scripts/setup-electron-backend.sh.',
  );
}

function requestJson(pathname, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const request = http.get({
      host: HOST,
      port,
      path: pathname,
      headers: { 'X-Vivieen-Token': backendToken },
    }, (response) => {
      let raw = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => {
        if (raw.length < 1_000_000) raw += chunk;
      });
      response.on('end', () => {
        if (response.statusCode < 200 || response.statusCode >= 300) return resolve(null);
        try { return resolve(JSON.parse(raw)); } catch { return resolve(null); }
      });
    });
    request.setTimeout(timeoutMs, () => request.destroy());
    request.on('error', () => resolve(null));
  });
}

async function vivieenMetadata(timeoutMs = 1500) {
  const metadata = await requestJson('/api/meta', timeoutMs);
  return metadata && metadata.app_id === APP_ID ? metadata : null;
}

async function isVivieenBackend(timeoutMs = 1500) {
  return Boolean(await vivieenMetadata(timeoutMs));
}

function portInUse(candidate) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: HOST, port: candidate });
    socket.setTimeout(500);
    socket.once('connect', () => { socket.destroy(); resolve(true); });
    socket.once('timeout', () => { socket.destroy(); resolve(false); });
    socket.once('error', () => resolve(false));
  });
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once('error', reject);
    server.listen(0, HOST, () => {
      const address = server.address();
      const selected = typeof address === 'object' ? address.port : DEFAULT_PORT;
      server.close(() => resolve(selected));
    });
  });
}

async function choosePort() {
  if (!await portInUse(port)) return;
  if (process.env.VIVIEEN_PORT) {
    throw new Error(`Requested backend port ${port} is already in use.`);
  }
  port = await freePort();
}

function backendEnvironment() {
  const root = codeRoot();
  const data = dataRoot();
  return {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONDONTWRITEBYTECODE: '1',
    TOKENIZERS_PARALLELISM: 'false',
    VIVIEEN_DATA_DIR: data,
    VIVIEEN_CONFIG: path.join(data, 'config.json'),
    VIVIEEN_FACE_MODEL: path.join(app.isPackaged ? data : root, 'models', 'face_landmarker.task'),
    VIVIEEN_CUTOUT_HELPER: path.join(
      app.isPackaged ? process.resourcesPath : root,
      app.isPackaged ? 'native' : '.electron-native',
      'person-cutout',
    ),
    VIVIEEN_AUTH_TOKEN: backendToken,
    PATH: [
      path.join(root, 'bin'),
      path.join(os.homedir(), '.config', 'enconvo', 'bin'),
      '/opt/homebrew/bin',
      '/usr/local/bin',
      process.env.PATH || '',
    ].join(path.delimiter),
  };
}

function writeBackendLog(chunk) {
  if (!backendLog) {
    fs.mkdirSync(path.dirname(logPath()), { recursive: true });
    backendLog = fs.createWriteStream(logPath(), { flags: 'a', mode: 0o600 });
    try { fs.chmodSync(logPath(), 0o600); } catch {}
  }
  backendLog.write(chunk);
}

function monitorState() {
  return enconvoMonitor ? enconvoMonitor.snapshot() : {
    enabled: Boolean(state && state.followEnconvo),
    status: 'off',
    message: 'Follow EnConvo is off.',
    processCount: 0,
    target: 'EnConvo',
  };
}

function broadcastMonitorState(value = monitorState()) {
  for (const window of [mainWindow, settingsWindow]) {
    if (window && !window.isDestroyed()) window.webContents.send('vivieen:monitor-state', value);
  }
  buildTrayMenu();
}

function createEnconvoMonitor() {
  enconvoMonitor = new EnconvoAudioMonitor({
    helperPath: audioTapExecutable(),
    simulate: process.env.VIVIEEN_AUDIO_TAP_SIMULATE === '1',
    logger: writeBackendLog,
  });
  enconvoMonitor.on('state', broadcastMonitorState);
  enconvoMonitor.on('sample', (sample) => {
    const value = { ...sample, sequence: ++enconvoSampleSequence };
    enconvoSampleBuffer.push(value);
    if (enconvoSampleBuffer.length > MAX_ENCONVO_SAMPLE_BACKLOG) {
      enconvoSampleBuffer.splice(0, enconvoSampleBuffer.length - MAX_ENCONVO_SAMPLE_BACKLOG);
    }
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('vivieen:monitor-sample', value);
    }
  });
}

function setEnconvoMonitoring(value) {
  state.followEnconvo = Boolean(value);
  saveStateSoon();
  enconvoMonitor.setEnabled(state.followEnconvo);
  if (!state.followEnconvo) enconvoSampleBuffer.length = 0;
  broadcastState();
  return shellState();
}

function stopBackend() {
  if (!backend || !ownsBackend) return;
  backend.removeAllListeners('exit');
  backend.kill('SIGTERM');
  backend = null;
  ownsBackend = false;
}

async function startBackend() {
  ensureDataRoot();
  await choosePort();

  const python = resolvePython();
  const args = [
    '-B', '-W', 'ignore', '-m', 'uvicorn', 'server.app:app',
    '--host', HOST, '--port', String(port),
  ];
  writeBackendLog(`\n\n[Electron ${new Date().toISOString()}] ${python} ${args.join(' ')}\n`);
  backend = spawn(python, args, {
    cwd: codeRoot(),
    env: backendEnvironment(),
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  ownsBackend = true;
  backend.stdout.on('data', writeBackendLog);
  backend.stderr.on('data', writeBackendLog);
  backend.once('exit', (code, signal) => {
    writeBackendLog(`[backend exited] code=${code} signal=${signal}\n`);
    backend = null;
    ownsBackend = false;
    if (!quitting && mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('vivieen:state', shellState());
    }
  });

  const deadline = Date.now() + START_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (await isVivieenBackend(2000)) return;
    if (!backend) break;
    await sleep(500);
  }
  stopBackend();
  throw new Error(`The Python backend did not start. See ${logPath()}`);
}

function defaultState() {
  return {
    alwaysOnTop: true,
    followEnconvo: true,
    enconvoFollowDefaultVersion: 1,
    petMode: true,
    petOpacity: 0.5,
    petView: 'half',
    petZoom: 1,
    petRoamZoom: 1,
    petClickThrough: true,
    petLocked: false,
    petRoam: false,
    petHomeBounds: null,
    bounds: { width: 560, height: 760 },
  };
}

function loadState() {
  try {
    const defaults = defaultState();
    const saved = JSON.parse(fs.readFileSync(statePath(), 'utf8'));
    const next = { ...defaults, ...saved, bounds: { ...defaults.bounds, ...(saved.bounds || {}) } };
    next.petOpacity = Math.max(0, Math.min(1, Number(next.petOpacity) || 0));
    next.petZoom = clampPetZoom(next.petZoom, PET_ZOOM_RANGE);
    next.petRoamZoom = clampPetZoom(next.petRoamZoom, PET_ROAM_ZOOM_RANGE);
    next.petView = PET_VIEWS.has(next.petView) ? next.petView : defaults.petView;
    next.petRoam = Boolean(next.petRoam);
    if (Number(saved.enconvoFollowDefaultVersion || 0) < 1) {
      next.followEnconvo = true;
      next.enconvoFollowDefaultVersion = 1;
    }
    next.petHomeBounds = next.petHomeBounds && typeof next.petHomeBounds === 'object'
      ? next.petHomeBounds : null;
    return next;
  } catch {
    return defaultState();
  }
}

function saveStateSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (!state.petRoam) state.bounds = mainWindow.getBounds();
    fs.mkdirSync(path.dirname(statePath()), { recursive: true });
    fs.writeFileSync(statePath(), JSON.stringify(state, null, 2), { mode: 0o600 });
  }, 180);
}

function visibleBounds(bounds) {
  if (!bounds || !Number.isFinite(bounds.x) || !Number.isFinite(bounds.y)) return bounds;
  const display = screen.getDisplayMatching(bounds);
  const area = display.workArea;
  const intersects = bounds.x < area.x + area.width
    && bounds.x + bounds.width > area.x
    && bounds.y < area.y + area.height
    && bounds.y + bounds.height > area.y;
  return intersects ? bounds : { width: bounds.width, height: bounds.height };
}

function shellState() {
  return {
    alwaysOnTop: Boolean(state && state.alwaysOnTop),
    backendOwned: ownsBackend,
    backendUrl: baseUrl(),
    enconvoMonitor: monitorState(),
    packaged: app.isPackaged,
    pet: {
      enabled: Boolean(state && state.petMode),
      opacity: Number(state && state.petOpacity),
      view: (state && state.petView) || 'half',
      zoom: Number(state && state.petZoom) || 1,
      roamZoom: Number(state && state.petRoamZoom) || 1,
      clickThrough: Boolean(state && state.petClickThrough),
      locked: Boolean(state && state.petLocked),
      roam: Boolean(state && state.petRoam),
      motionReady: petMotionReady,
      motionProfile: { ...petMotionProfile },
    },
  };
}

function broadcastState() {
  const value = shellState();
  for (const window of [mainWindow, settingsWindow, appearanceWindow]) {
    if (window && !window.isDestroyed()) window.webContents.send('vivieen:state', value);
  }
  buildTrayMenu();
}

// The appearance panel has to track a live pinch without paying for a tray
// rebuild on every frame, so it gets its own cheap, throttled push.
function pushAppearanceState(force = false) {
  if (!appearanceWindow || appearanceWindow.isDestroyed()) return;
  const now = Date.now();
  if (!force && now - appearancePushAt < 70) return;
  appearancePushAt = now;
  appearanceWindow.webContents.send('vivieen:state', shellState());
}

function applyAlwaysOnTop(value) {
  state.alwaysOnTop = Boolean(value);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.setAlwaysOnTop(state.alwaysOnTop, 'floating');
    mainWindow.setVisibleOnAllWorkspaces(state.alwaysOnTop, { visibleOnFullScreen: true });
  }
  saveStateSoon();
  broadcastState();
  return shellState();
}

function setPetHit(interactive, reason = 'renderer') {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const value = Boolean(interactive);
  if (petPointerInteractive === value) return;
  petPointerInteractive = value;
  if (process.env.VIVIEEN_DEBUG_HIT) console.error(`[pet-hit] interactive=${value} reason=${reason}`);
  const ignore = state.petClickThrough && !value;
  mainWindow.setIgnoreMouseEvents(ignore, { forward: true });
}

function stopPetPointerTracking() {
  if (petPointerTimer) clearInterval(petPointerTimer);
  petPointerTimer = null;
  petPointerInteractive = null;
}

function startPetPointerTracking() {
  stopPetPointerTracking();
  petPointerTimer = setInterval(() => {
    if (!mainWindow || mainWindow.isDestroyed() || !mainWindow.isVisible()) return;
    const point = screen.getCursorScreenPoint();
    const bounds = mainWindow.getBounds();
    if (!state.petClickThrough) {
      // The window is always interactive, but the gaze still needs the
      // cursor, so the feed keeps flowing in this branch too.
      setPetHit(true, 'click-through-off');
      mainWindow.webContents.send('vivieen:pet-pointer', {
        x: point.x - bounds.x, y: point.y - bounds.y,
        inside: point.x >= bounds.x && point.x < bounds.x + bounds.width
          && point.y >= bounds.y && point.y < bounds.y + bounds.height,
      });
      return;
    }
    const inside = point.x >= bounds.x && point.x < bounds.x + bounds.width
      && point.y >= bounds.y && point.y < bounds.y + bounds.height;
    // Coordinates are sent even outside the window (they go negative or past
    // the edge) so the renderer's gaze can follow the cursor across the
    // desktop; `inside` keeps the hit-testing semantics unchanged.
    const localPoint = {
      x: point.x - bounds.x, y: point.y - bounds.y, inside,
    };
    if (!inside) {
      if (process.env.VIVIEEN_DEBUG_HIT && petPointerInteractive) {
        console.error(`[pet-pointer] point=${point.x},${point.y} bounds=${bounds.x},${bounds.y},${bounds.width},${bounds.height}`);
      }
      mainWindow.webContents.send('vivieen:pet-pointer', localPoint);
      setPetHit(false, 'outside-window');
      return;
    }
    if (process.env.VIVIEEN_DEBUG_HIT && Date.now() - petPointerDebugAt > 1000) {
      petPointerDebugAt = Date.now();
      console.error(`[pet-pointer] local=${localPoint.x},${localPoint.y}`);
    }
    mainWindow.webContents.send('vivieen:pet-pointer', localPoint);
  }, 32);
  petPointerTimer.unref?.();
}

function applyPetOpacity(value, reveal = true) {
  const opacity = Math.max(0, Math.min(1, Number(value) || 0));
  state.petOpacity = opacity;
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (opacity <= 0.001) mainWindow.hide();
    else {
      mainWindow.setOpacity(opacity);
      if (reveal) mainWindow.showInactive();
    }
  }
  saveStateSoon();
  broadcastState();
  return shellState();
}

function applyPetView(value) {
  if (!PET_VIEWS.has(value)) return shellState();
  state.petView = value;
  saveStateSoon();
  broadcastState();
  return shellState();
}

function petBoundsForZoom(value) {
  if (!mainWindow || mainWindow.isDestroyed()) return null;
  const zoom = Math.max(PET_ZOOM_RANGE.min,
    Math.min(PET_ZOOM_RANGE.max, Number(value) || 1));
  const current = mainWindow.getBounds();
  return boundsForPetZoom(
    current, PET_BASE_SIZE, PET_NORMAL_MINIMUM, zoom);
}

function applyPetZoom(value) {
  petZoomGesture = null;
  state.petZoom = clampPetZoom(value, PET_ZOOM_RANGE);
  if (!state.petRoam) {
    const bounds = petBoundsForZoom(state.petZoom);
    if (bounds) {
      mainWindow.setBounds(bounds, false);
      state.bounds = { ...bounds };
    }
  }
  saveStateSoon();
  broadcastState();
  return shellState();
}

function petRoamSize(zoom) {
  const requested = zoom === undefined ? (state && state.petRoamZoom) : zoom;
  return roamSizeForZoom(
    PET_ROAM_SIZE, PET_ROAM_MINIMUM, clampPetZoom(requested, PET_ROAM_ZOOM_RANGE));
}

function resizePetRoamWindow(zoom) {
  if (!mainWindow || mainWindow.isDestroyed()) return null;
  const size = petRoamSize(zoom);
  const area = petRoamDisplay().workArea;
  const bounds = mainWindow.getBounds();
  const centre = bounds.x + bounds.width / 2;
  const rightEdge = area.x + area.width - size.width;
  const x = Math.round(Math.max(area.x, Math.min(rightEdge, centre - size.width / 2)));
  const y = Math.round(area.y + area.height - size.height + 2);
  mainWindow.setMinimumSize(size.width, size.height);
  mainWindow.setBounds({ x, y, width: size.width, height: size.height }, false);
  if (petRoamRuntime) petRoamRuntime.x = x;
  return size;
}

function applyPetRoamZoom(value) {
  state.petRoamZoom = clampPetZoom(value, PET_ROAM_ZOOM_RANGE);
  if (state.petRoam) resizePetRoamWindow(state.petRoamZoom);
  saveStateSoon();
  broadcastState();
  return shellState();
}

// Pinch feedback: resize on every frame the renderer sends, persist and
// broadcast once the gesture ends. Roaming pinches retune the animation box.
function applyPetZoomLive(payload) {
  if (!mainWindow || mainWindow.isDestroyed()) return shellState();
  const data = payload && typeof payload === 'object' ? payload : { value: payload };
  const phase = data.phase === 'start' || data.phase === 'end' ? data.phase : 'move';
  if (state.petRoam) {
    state.petRoamZoom = clampPetZoom(data.value, PET_ROAM_ZOOM_RANGE);
    resizePetRoamWindow(state.petRoamZoom);
  } else {
    if (phase === 'start' || !petZoomGesture) {
      petZoomGesture = { anchor: petZoomAnchor(mainWindow.getBounds()) };
    }
    state.petZoom = clampPetZoom(data.value, PET_ZOOM_RANGE);
    const bounds = boundsForPetZoomAtAnchor(
      petZoomGesture.anchor, PET_BASE_SIZE, PET_NORMAL_MINIMUM, state.petZoom);
    mainWindow.setBounds(bounds, false);
    state.bounds = { ...bounds };
  }
  if (phase !== 'end') {
    pushAppearanceState();
    return shellState();
  }
  petZoomGesture = null;
  saveStateSoon();
  broadcastState();
  return shellState();
}

function applyPetClickThrough(value) {
  state.petClickThrough = Boolean(value);
  setPetHit(!state.petClickThrough);
  saveStateSoon();
  broadcastState();
  return shellState();
}

function applyPetLock(value) {
  state.petLocked = Boolean(value);
  petDrag = null;
  saveStateSoon();
  broadcastState();
  return shellState();
}

function petRoamDisplay() {
  if (petRoamRuntime) {
    const active = screen.getAllDisplays().find(
      (display) => display.id === petRoamRuntime.displayId);
    if (active) return active;
  }
  const home = state.petHomeBounds || state.bounds;
  if (home && Number.isFinite(home.x) && Number.isFinite(home.y)) {
    return screen.getDisplayMatching(home);
  }
  return screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
}

function sendPetRoamMotion(payload = null) {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const value = payload || (petRoamRuntime ? {
    enabled: true,
    mode: petRoamRuntime.mode,
    direction: petRoamRuntime.direction,
    phase: petRoamRuntime.stride % 1,
    edge: petRoamRuntime.mode.startsWith('ledge-')
      ? petRoamRuntime.mode.slice('ledge-'.length) : null,
  } : { enabled: false, mode: 'idle', direction: 1, phase: 0, edge: null });
  mainWindow.webContents.send('vivieen:pet-roam-motion', value);
}

function motionTravelAt(profile, phase) {
  const cycleDistance = Number(profile.cycleDistance)
    || Number(profile.walkSpeed) * Number(profile.cycleSeconds);
  const offsets = Array.isArray(profile.travelOffsets) ? profile.travelOffsets : [];
  const wrapped = ((Number(phase) || 0) % 1 + 1) % 1;
  if (offsets.length < 2) return wrapped * cycleDistance;
  const position = wrapped * offsets.length;
  const index = Math.min(offsets.length - 1, Math.floor(position));
  const fraction = position - index;
  const current = offsets[index];
  const next = index + 1 < offsets.length ? offsets[index + 1] : cycleDistance;
  return current + (next - current) * fraction;
}

function motionTravelDelta(profile, previousPhase, nextPhase) {
  const cycleDistance = Number(profile.cycleDistance)
    || Number(profile.walkSpeed) * Number(profile.cycleSeconds);
  const previous = motionTravelAt(profile, previousPhase);
  const next = motionTravelAt(profile, nextPhase);
  return nextPhase >= previousPhase ? next - previous : cycleDistance - previous + next;
}

function tickPetRoam() {
  if (!state.petRoam || !petRoamRuntime || !mainWindow || mainWindow.isDestroyed()) {
    if (petRoamTimer) clearInterval(petRoamTimer);
    petRoamTimer = null;
    return;
  }
  const now = Date.now();
  const elapsed = Math.max(0, Math.min(0.1, (now - petRoamRuntime.lastAt) / 1000));
  petRoamRuntime.lastAt = now;
  const display = petRoamDisplay();
  const area = display.workArea;
  const bounds = mainWindow.getBounds();
  const minimumX = area.x;
  const maximumX = area.x + area.width - bounds.width;
  const dockLineY = Math.round(area.y + area.height - bounds.height + 2);
  let x = Number.isFinite(petRoamRuntime.x) ? petRoamRuntime.x : bounds.x;

  if (petRoamRuntime.mode === 'stand') {
    x = Math.max(minimumX, Math.min(maximumX, x));
    if (!petRoamRuntime.engaged && now >= petRoamRuntime.resumeAt) {
      petRoamRuntime.mode = petRoamRuntime.resumeMode || 'walk';
      if (petRoamRuntime.mode.startsWith('ledge-')) {
        petRoamRuntime.holdUntil = now + PET_LEDGE_HOLD_MS;
      }
      petRoamRuntime.resumeMode = 'walk';
    } else {
      petRoamRuntime.x = x;
      mainWindow.setPosition(Math.round(x), dockLineY, false);
      sendPetRoamMotion();
      return;
    }
  }
  if (petRoamRuntime.mode === 'walk') {
    const previousStride = petRoamRuntime.stride;
    petRoamRuntime.stride = (petRoamRuntime.stride
      + elapsed / Math.max(0.1, petRoamRuntime.cycleSeconds)) % 1;
    const travelled = motionTravelDelta(
      petRoamRuntime, previousStride, petRoamRuntime.stride);
    x += petRoamRuntime.direction * travelled;
    if (x >= maximumX) {
      x = maximumX;
      petRoamRuntime.mode = 'ledge-right';
      petRoamRuntime.holdUntil = now + PET_LEDGE_HOLD_MS;
    } else if (x <= minimumX) {
      x = minimumX;
      petRoamRuntime.mode = 'ledge-left';
      petRoamRuntime.holdUntil = now + PET_LEDGE_HOLD_MS;
    }
  } else if (petRoamRuntime.mode.startsWith('ledge-')) {
    const right = petRoamRuntime.mode === 'ledge-right';
    x = right ? maximumX : minimumX;
    if (now >= petRoamRuntime.holdUntil) {
      petRoamRuntime.direction = right ? -1 : 1;
      petRoamRuntime.mode = 'walk';
    }
  }

  petRoamRuntime.x = x;
  mainWindow.setPosition(Math.round(x), dockLineY, false);
  sendPetRoamMotion();
}

function startPetRoamMotion() {
  if (!state.petRoam || !petMotionReady || !mainWindow || mainWindow.isDestroyed()) return;
  if (petRoamTimer) clearInterval(petRoamTimer);
  const display = petRoamDisplay();
  const area = display.workArea;
  const home = state.petHomeBounds || state.bounds || {};
  const size = petRoamSize();
  const maximumX = area.x + area.width - size.width;
  const homeCenter = Number.isFinite(home.x) && Number.isFinite(home.width)
    ? home.x + home.width / 2 : area.x + area.width / 2;
  const startX = Math.round(Math.max(area.x, Math.min(
    maximumX, homeCenter - size.width / 2)));
  const startY = Math.round(area.y + area.height - size.height + 2);
  const direction = startX > area.x + area.width / 2 ? -1 : 1;

  mainWindow.setResizable(false);
  mainWindow.setMinimumSize(size.width, size.height);
  mainWindow.setBounds({
    x: startX,
    y: startY,
    width: size.width,
    height: size.height,
  }, false);
  mainWindow.setAlwaysOnTop(true, 'floating');
  mainWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  if (state.petOpacity > 0.001) mainWindow.showInactive();

  petRoamRuntime = {
    displayId: display.id,
    x: startX,
    direction,
    mode: 'walk',
    stride: 0,
    holdUntil: 0,
    engaged: false,
    resumeAt: 0,
    resumeMode: 'walk',
    walkSpeed: petMotionProfile.walkSpeed,
    cycleSeconds: petMotionProfile.cycleSeconds,
    cycleDistance: petMotionProfile.cycleDistance,
    travelOffsets: [...petMotionProfile.travelOffsets],
    lastAt: Date.now(),
  };
  sendPetRoamMotion();
  petRoamTimer = setInterval(tickPetRoam, 32);
  petRoamTimer.unref?.();
}

function stopPetRoamMotion(restore = true) {
  if (petRoamTimer) clearInterval(petRoamTimer);
  petRoamTimer = null;
  petRoamRuntime = null;
  sendPetRoamMotion({ enabled: false, mode: 'idle', direction: 1, phase: 0, edge: null });
  if (!mainWindow || mainWindow.isDestroyed()) return;

  mainWindow.setResizable(true);
  mainWindow.setMinimumSize(PET_NORMAL_MINIMUM.width, PET_NORMAL_MINIMUM.height);
  if (restore) {
    const remembered = visibleBounds(state.petHomeBounds || state.bounds) || {};
    const width = Math.max(PET_NORMAL_MINIMUM.width, Number(remembered.width) || 560);
    const height = Math.max(PET_NORMAL_MINIMUM.height, Number(remembered.height) || 760);
    let x = Number(remembered.x);
    let y = Number(remembered.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      const area = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea;
      x = area.x + area.width - width - 28;
      y = area.y + area.height - height - 28;
    }
    mainWindow.setBounds({ x: Math.round(x), y: Math.round(y), width, height }, false);
    state.bounds = mainWindow.getBounds();
  }
  mainWindow.setAlwaysOnTop(state.alwaysOnTop, 'floating');
  mainWindow.setVisibleOnAllWorkspaces(state.alwaysOnTop, { visibleOnFullScreen: true });
  if (restore) applyPetZoom(state.petZoom);
}

function setPetMotionReady(value) {
  const payload = value && typeof value === 'object' ? value : { ready: Boolean(value) };
  const ready = Boolean(payload.ready);
  const requestedSpeed = Number(payload.walkSpeed);
  const requestedCycle = Number(payload.cycleSeconds);
  const walkSpeed = Number.isFinite(requestedSpeed)
    ? Math.max(PET_ROAM_MIN_SPEED, Math.min(PET_ROAM_MAX_SPEED, requestedSpeed))
    : petMotionProfile.walkSpeed;
  const cycleSeconds = Number.isFinite(requestedCycle)
    ? Math.max(0.5, Math.min(2.5, requestedCycle))
    : petMotionProfile.cycleSeconds;
  const requestedDistance = Number(payload.cycleDistance);
  const cycleDistance = Number.isFinite(requestedDistance)
    ? Math.max(10, Math.min(PET_ROAM_MAX_SPEED * 2.5, requestedDistance))
    : walkSpeed * cycleSeconds;
  const requestedOffsets = Array.isArray(payload.travelOffsets)
    ? payload.travelOffsets.map(Number).filter(Number.isFinite).slice(0, 96) : [];
  const travelOffsets = requestedOffsets.length >= 2
    ? requestedOffsets.reduce((values, value) => {
      values.push(Math.max(values.at(-1) || 0, Math.min(cycleDistance, value)));
      return values;
    }, []) : [];
  const nextProfile = { walkSpeed, cycleSeconds, cycleDistance, travelOffsets };
  const profileChanged = Math.abs(nextProfile.walkSpeed - petMotionProfile.walkSpeed) > 0.01
    || Math.abs(nextProfile.cycleSeconds - petMotionProfile.cycleSeconds) > 0.001
    || Math.abs(nextProfile.cycleDistance - petMotionProfile.cycleDistance) > 0.01
    || JSON.stringify(nextProfile.travelOffsets) !== JSON.stringify(petMotionProfile.travelOffsets);
  petMotionProfile = nextProfile;
  if (petRoamRuntime) {
    petRoamRuntime.walkSpeed = nextProfile.walkSpeed;
    petRoamRuntime.cycleSeconds = nextProfile.cycleSeconds;
    petRoamRuntime.cycleDistance = nextProfile.cycleDistance;
    petRoamRuntime.travelOffsets = [...nextProfile.travelOffsets];
  }
  if (petMotionReady === ready && !profileChanged) return shellState();
  petMotionReady = ready;
  if (!ready && state.petRoam) {
    state.petRoam = false;
    stopPetRoamMotion(true);
    state.petHomeBounds = null;
    saveStateSoon();
  } else if (ready && state.petRoam) {
    startPetRoamMotion();
  }
  broadcastState();
  return shellState();
}

function setPetEngaged(value) {
  if (!state.petRoam || !petRoamRuntime || !mainWindow || mainWindow.isDestroyed()) return;
  const engaged = Boolean(value);
  if (petRoamRuntime.engaged === engaged) return;
  petRoamRuntime.engaged = engaged;
  if (engaged) {
    if (petRoamRuntime.mode !== 'stand') {
      petRoamRuntime.resumeMode = petRoamRuntime.mode.startsWith('ledge-')
        ? petRoamRuntime.mode : 'walk';
      if (petRoamRuntime.mode === 'ledge-right') petRoamRuntime.direction = -1;
      if (petRoamRuntime.mode === 'ledge-left') petRoamRuntime.direction = 1;
    }
    petRoamRuntime.mode = 'stand';
    petRoamRuntime.resumeAt = Number.POSITIVE_INFINITY;
    const area = petRoamDisplay().workArea;
    const bounds = mainWindow.getBounds();
    const x = Math.max(area.x, Math.min(area.x + area.width - bounds.width, bounds.x));
    petRoamRuntime.x = x;
    mainWindow.setPosition(Math.round(x),
      Math.round(area.y + area.height - bounds.height + 2), false);
  } else {
    petRoamRuntime.resumeAt = Date.now() + PET_INTERACTION_COOLDOWN_MS;
  }
  sendPetRoamMotion();
}

function applyPetRoam(value) {
  const enabled = Boolean(value);
  if (enabled && !petMotionReady) return shellState();
  if (enabled) {
    const home = mainWindow && !mainWindow.isDestroyed()
      ? mainWindow.getBounds() : state.bounds;
    if (!state.petRoam || !state.petHomeBounds) {
      state.bounds = { ...home };
      state.petHomeBounds = { ...home };
    }
    state.petRoam = true;
    if (!mainWindow || mainWindow.isDestroyed()) createMainWindow();
    startPetRoamMotion();
  } else {
    state.petRoam = false;
    stopPetRoamMotion(true);
    state.petHomeBounds = null;
  }
  petDrag = null;
  saveStateSoon();
  broadcastState();
  return shellState();
}

function guardNavigation(window, kind) {
  window.webContents.on('will-navigate', (event, target) => {
    let targetUrl;
    try { targetUrl = new URL(target); } catch { event.preventDefault(); return; }
    if (targetUrl.origin !== baseUrl()) {
      event.preventDefault();
      if (targetUrl.protocol === 'https:') shell.openExternal(target).catch(() => {});
      return;
    }
    if (kind === 'main') {
      if (targetUrl.pathname === '/settings') {
        event.preventDefault();
        openSettings();
      }
      return;
    }
    // Character Studio and the appearance popover are single documents. Any
    // in-place navigation replaces the entire UI with whatever was linked - a
    // stray <a href> to a .mp4 once swapped the studio for a bare video player
    // with no way back. Nothing in these windows may leave its own page.
    if (targetUrl.pathname !== `/${kind}`) event.preventDefault();
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    let targetUrl;
    try { targetUrl = new URL(url); } catch { return { action: 'deny' }; }
    if (targetUrl.protocol === 'https:') shell.openExternal(url).catch(() => {});
    return { action: 'deny' };
  });
  window.webContents.on('before-input-event', (event, input) => {
    if (input.meta && input.key === ',') {
      event.preventDefault();
      openSettings();
    }
  });
}

function speechBubbleDisplay() {
  if (petRoamRuntime) return petRoamDisplay();
  if (mainWindow && !mainWindow.isDestroyed()) return screen.getDisplayMatching(mainWindow.getBounds());
  return screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
}

function positionSpeechBubble() {
  if (!bubbleWindow || bubbleWindow.isDestroyed()) return;
  const area = speechBubbleDisplay().workArea;
  const width = Math.min(820, Math.max(440, area.width - 96));
  const height = Math.min(300, Math.max(220, Math.round(area.height * 0.28)));
  bubbleWindow.setBounds({
    x: Math.round(area.x + (area.width - width) / 2),
    y: Math.round(area.y + (area.height - height) * 0.42),
    width,
    height,
  }, false);
}

function sendPendingBubble() {
  if (!pendingBubble || !bubbleWindow || bubbleWindow.isDestroyed()) return;
  positionSpeechBubble();
  bubbleWindow.webContents.send('vivieen:bubble-text', { text: pendingBubble });
  bubbleWindow.showInactive();
}

function createSpeechBubbleWindow() {
  if (bubbleWindow && !bubbleWindow.isDestroyed()) return bubbleWindow;
  bubbleWindow = new BrowserWindow({
    width: 760,
    height: 260,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    roundedCorners: false,
    hasShadow: false,
    resizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    focusable: false,
    alwaysOnTop: true,
    webPreferences: {
      preload: path.join(__dirname, 'bubble-preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  });
  bubbleWindow.setAlwaysOnTop(true, 'floating');
  bubbleWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  bubbleWindow.setIgnoreMouseEvents(true);
  guardNavigation(bubbleWindow, 'bubble');
  bubbleWindow.loadURL(`${baseUrl()}/bubble?electron=1`);
  bubbleWindow.webContents.once('did-finish-load', sendPendingBubble);
  bubbleWindow.on('closed', () => { bubbleWindow = null; });
  return bubbleWindow;
}

function showSpeechBubble(value) {
  const text = String(value || '').replace(/\s+/g, ' ').trim().slice(0, 1200);
  clearTimeout(bubbleTimer);
  bubbleTimer = null;
  pendingBubble = text;
  if (!text) {
    if (bubbleWindow && !bubbleWindow.isDestroyed()) bubbleWindow.hide();
    return;
  }
  const window = createSpeechBubbleWindow();
  if (!window.webContents.isLoadingMainFrame()) sendPendingBubble();
  const visibleMs = Math.max(5000, Math.min(18_000, 3500 + text.length * 48));
  bubbleTimer = setTimeout(() => {
    pendingBubble = '';
    if (bubbleWindow && !bubbleWindow.isDestroyed()) bubbleWindow.hide();
  }, visibleMs);
  bubbleTimer.unref?.();
}

function positionAppearanceWindow() {
  if (!appearanceWindow || appearanceWindow.isDestroyed()) return;
  const point = screen.getCursorScreenPoint();
  const area = screen.getDisplayNearestPoint(point).workArea;
  const [width, height] = appearanceWindow.getSize();
  let x = Math.round(point.x - width / 2);
  let y = Math.round(point.y + 18);
  if (y + height > area.y + area.height) y = Math.round(point.y - height - 18);
  x = Math.max(area.x + 8, Math.min(area.x + area.width - width - 8, x));
  y = Math.max(area.y + 8, Math.min(area.y + area.height - height - 8, y));
  appearanceWindow.setPosition(x, y, false);
}

function createAppearanceWindow() {
  if (appearanceWindow && !appearanceWindow.isDestroyed()) return appearanceWindow;
  appearanceWindow = new BrowserWindow({
    width: 390,
    height: 316,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    roundedCorners: true,
    hasShadow: true,
    resizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    title: 'Vivieen Appearance',
    webPreferences: {
      preload: path.join(__dirname, 'appearance-preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  });
  appearanceWindow.setAlwaysOnTop(true, 'floating');
  appearanceWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  guardNavigation(appearanceWindow, 'appearance');
  appearanceWindow.loadURL(`${baseUrl()}/appearance?electron=1`);
  appearanceWindow.on('blur', () => appearanceWindow?.hide());
  appearanceWindow.on('closed', () => { appearanceWindow = null; });
  return appearanceWindow;
}

function showAppearanceWindow() {
  const window = createAppearanceWindow();
  positionAppearanceWindow();
  window.show();
  window.focus();
  pushAppearanceState(true);
}

function triggerEnconvoVoiceCommand() {
  return new Promise((resolve) => {
    execFile(audioTapExecutable(), ['--trigger-right-option'],
      { timeout: 4000 }, (error, stdout, stderr) => {
        let payload = null;
        try {
          const lines = String(stdout || '').trim().split('\n').filter(Boolean);
          payload = JSON.parse(lines.at(-1) || '{}');
        } catch {}
        if (error || !payload || payload.ok !== true) {
          const message = payload?.message
            || 'Allow Vivieen in Privacy & Security → Accessibility, then choose Talk via EnConvo again.';
          const detail = payload?.code || stderr || error?.message || 'native event failed';
          writeBackendLog(`[EnConvo voice trigger failed] ${String(detail).trim()}\n`);
          showSpeechBubble(message);
          resolve({ ok: false, error: String(detail).trim() });
          return;
        }
        resolve({ ok: true });
      });
  });
}

// Every launch starts from the bottom-right corner of the work area, at a
// zoom that fits on screen. A pinch once left the companion larger than the
// display and pinned under the Dock; restoring the saved corner would bring
// that stuck state back, so the saved x/y is deliberately ignored here.
function startupPetBounds() {
  const area = screen.getDisplayNearestPoint(screen.getCursorScreenPoint()).workArea;
  state.petZoom = clampPetZoom(
    fitPetZoomToArea(
      PET_BASE_SIZE, PET_NORMAL_MINIMUM, state.petZoom, area, PET_DOCK_MARGIN),
    PET_ZOOM_RANGE);
  const size = petZoomSize(PET_BASE_SIZE, PET_NORMAL_MINIMUM, state.petZoom);
  return dockedPetBounds(size, area, PET_DOCK_MARGIN);
}

function createMainWindow() {
  const bounds = startupPetBounds();
  state.bounds = { ...bounds };
  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: PET_NORMAL_MINIMUM.width,
    minHeight: PET_NORMAL_MINIMUM.height,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    roundedCorners: false,
    hasShadow: false,
    resizable: true,
    enableLargerThanScreen: true,
    fullscreenable: false,
    skipTaskbar: true,
    acceptFirstMouse: true,
    title: 'Vivieen',
    webPreferences: {
      backgroundThrottling: false,
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  });
  mainWindow.setOpacity(state.petOpacity > 0 ? state.petOpacity : 0.5);
  petPointerInteractive = null;
  setPetHit(false);
  startPetPointerTracking();
  applyAlwaysOnTop(state.alwaysOnTop);
  guardNavigation(mainWindow, 'main');
  mainWindow.loadURL(`${baseUrl()}/?electron=1&app=${encodeURIComponent(app.getVersion())}`);
  mainWindow.once('ready-to-show', () => {
    if (state.petRoam && petMotionReady) startPetRoamMotion();
    else applyPetZoom(state.petZoom);
    if (state.petOpacity > 0.001) mainWindow.show();
  });
  mainWindow.on('move', () => { if (!state.petRoam) saveStateSoon(); });
  mainWindow.on('resize', () => { if (!state.petRoam) saveStateSoon(); });
  mainWindow.on('close', (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on('closed', () => {
    stopPetPointerTracking();
    stopPetRoamMotion(false);
    mainWindow = null;
  });
}

function showMain() {
  if (!mainWindow || mainWindow.isDestroyed()) createMainWindow();
  if (state.petOpacity <= 0.001) {
    state.petOpacity = 0.5;
    saveStateSoon();
  }
  mainWindow.setOpacity(state.petOpacity);
  mainWindow.show();
  mainWindow.focus();
  broadcastState();
  if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.hide();
}

function recoverCompanion() {
  if (!mainWindow || mainWindow.isDestroyed()) createMainWindow();
  // Reset the zoom before anything re-applies it: keeping the current size
  // once "recovered" a pinch-blown window to a spot still off every edge.
  state.petZoom = 1;
  if (state.petRoam) {
    state.petRoam = false;
    stopPetRoamMotion(true);
    state.petHomeBounds = null;
  }
  state.petOpacity = 0.5;
  state.petClickThrough = false;
  state.petLocked = false;
  const display = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const area = display.workArea;
  const size = petZoomSize(PET_BASE_SIZE, PET_NORMAL_MINIMUM, state.petZoom);
  mainWindow.setBounds(dockedPetBounds(size, area, PET_DOCK_MARGIN));
  state.bounds = mainWindow.getBounds();
  mainWindow.setOpacity(0.5);
  mainWindow.setIgnoreMouseEvents(false);
  mainWindow.show();
  mainWindow.focus();
  saveStateSoon();
  broadcastState();
}

function installRecoveryShortcut() {
  const accelerator = 'CommandOrControl+Shift+0';
  if (!globalShortcut.register(accelerator, recoverCompanion)) {
    writeBackendLog(`[shortcut unavailable] ${accelerator}\n`);
  }
}

function openSettings() {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.show();
    settingsWindow.focus();
    return;
  }
  settingsWindow = new BrowserWindow({
    width: 1080,
    height: 790,
    minWidth: 760,
    minHeight: 620,
    show: false,
    title: 'Vivieen Settings',
    backgroundColor: '#0b0b0d',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  });
  guardNavigation(settingsWindow, 'settings');
  settingsWindow.loadURL(`${baseUrl()}/settings?electron=1&app=${encodeURIComponent(app.getVersion())}`);
  settingsWindow.once('ready-to-show', () => settingsWindow.show());
  settingsWindow.on('closed', () => { settingsWindow = null; });
}

function trayImage() {
  const file = path.join(__dirname, 'trayTemplate.png');
  if (fs.existsSync(file)) {
    const image = nativeImage.createFromPath(file).resize({ width: 18, height: 18 });
    image.setTemplateImage(true);
    return image;
  }
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18"><path fill="black" d="M9 1.4c-3.25 0-5.9 2.7-5.9 6.05 0 2.15 1.08 4.04 2.72 5.1L4.9 16.6l4.1-2.12 4.1 2.12-.92-4.05a6.1 6.1 0 0 0 2.72-5.1C14.9 4.1 12.25 1.4 9 1.4Zm-2.45 5.7a1.05 1.05 0 1 1 0-2.1 1.05 1.05 0 0 1 0 2.1Zm4.9 0a1.05 1.05 0 1 1 0-2.1 1.05 1.05 0 0 1 0 2.1ZM9 11.8c-1.6 0-2.8-.82-3.25-1.8h6.5c-.45.98-1.65 1.8-3.25 1.8Z"/></svg>';
  const image = nativeImage.createFromDataURL(
    `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`,
  );
  image.setTemplateImage(true);
  return image;
}

function buildTrayMenu() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: mainWindow && mainWindow.isVisible() ? 'Hide Avatar' : 'Show Avatar', click: () => {
      if (mainWindow && mainWindow.isVisible()) mainWindow.hide(); else showMain();
    } },
    { label: 'Settings…', accelerator: 'CommandOrControl+,', click: openSettings },
    { label: 'Size & Opacity…', click: showAppearanceWindow },
    { type: 'separator' },
    { label: 'Follow EnConvo Audio', type: 'checkbox', checked: monitorState().enabled,
      click: (item) => setEnconvoMonitoring(item.checked) },
    { label: 'Always on Top', type: 'checkbox', checked: state.alwaysOnTop,
      click: (item) => applyAlwaysOnTop(item.checked) },
    { label: petMotionReady ? 'Horizon Walk Along Dock' : 'Horizon Walk · Generate Motion First',
      type: 'checkbox', checked: state.petRoam, enabled: petMotionReady,
      click: (item) => applyPetRoam(item.checked) },
    { label: 'Click Through Empty Space', type: 'checkbox', checked: state.petClickThrough,
      click: (item) => applyPetClickThrough(item.checked) },
    { label: 'Lock Position', type: 'checkbox', checked: state.petLocked,
      enabled: !state.petRoam, click: (item) => applyPetLock(item.checked) },
    { label: 'Recover Companion', accelerator: 'CommandOrControl+Shift+0', click: recoverCompanion },
    { label: 'Restart Voice Engine', enabled: ownsBackend, click: restartBackend },
    { type: 'separator' },
    { label: 'Quit Vivieen', accelerator: 'CommandOrControl+Q', click: () => app.quit() },
  ]));
}

function petViewItems() {
  const views = [
    ['Full Body', 'full'],
    ['Three-Quarter', 'three-quarter'],
    ['Half Body', 'half'],
    ['Bust', 'bust'],
    ['Head & Shoulders', 'head'],
    ['Face', 'face'],
  ];
  return views.map(([label, value]) => ({
    label,
    type: 'radio',
    checked: state.petView === value,
    click: () => applyPetView(value),
  }));
}

function showPetMenu() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const followingEnconvo = monitorState().enabled;
  // Short one-row labels; " · " carries the gesture that does the same thing.
  const menu = Menu.buildFromTemplate([
    { label: followingEnconvo ? 'Talk · hold head' : 'Talk to Vivieen…', click: () => {
      if (followingEnconvo) {
        void triggerEnconvoVoiceCommand();
        return;
      }
      mainWindow.show();
      mainWindow.focus();
      mainWindow.webContents.send('vivieen:pet-chat');
    } },
    { label: followingEnconvo ? 'Unfollow EnConvo' : 'Follow EnConvo',
      click: () => setEnconvoMonitoring(!followingEnconvo) },
    { type: 'separator' },
    { label: !petMotionReady ? 'Walk · generate first'
        : state.petRoam ? 'Walking · hover to stop' : 'Walk · 2×tap leg',
      type: 'checkbox', checked: state.petRoam, enabled: petMotionReady,
      click: (item) => applyPetRoam(item.checked) },
    { label: 'React · tap arm or chest', enabled: false },
    { label: 'Rest · still for 10s', enabled: false },
    { type: 'separator' },
    { label: 'Opacity + · 2×tap chest',
      click: () => applyPetOpacity(Math.min(1, state.petOpacity + 0.12)) },
    { label: 'Opacity − · 2×tap foot',
      click: () => applyPetOpacity(Math.max(0.15, state.petOpacity - 0.12)) },
    { label: 'Size & Opacity…', click: showAppearanceWindow },
    { label: 'View', enabled: !state.petRoam, submenu: petViewItems() },
    { type: 'separator' },
    { label: 'Click-Through Gaps', type: 'checkbox', checked: state.petClickThrough,
      click: (item) => applyPetClickThrough(item.checked) },
    { label: 'Lock Position', type: 'checkbox', checked: state.petLocked,
      enabled: !state.petRoam, click: (item) => applyPetLock(item.checked) },
    { label: 'Always on Top', type: 'checkbox', checked: state.alwaysOnTop,
      click: (item) => applyAlwaysOnTop(item.checked) },
    { type: 'separator' },
    { label: 'Character Studio…', click: openSettings },
    { label: 'Hide Companion', click: () => mainWindow.hide() },
    { label: 'Quit Vivieen', click: () => app.quit() },
  ]);
  menu.popup({
    window: mainWindow,
    callback: () => {
      const point = screen.getCursorScreenPoint();
      const bounds = mainWindow.getBounds();
      mainWindow.webContents.send('vivieen:pet-pointer', {
        x: point.x - bounds.x,
        y: point.y - bounds.y,
        inside: point.x >= bounds.x && point.x < bounds.x + bounds.width
          && point.y >= bounds.y && point.y < bounds.y + bounds.height,
      });
    },
  });
}

function createTray() {
  tray = new Tray(trayImage());
  tray.setToolTip('Vivieen');
  tray.on('click', () => {
    if (mainWindow && mainWindow.isVisible()) mainWindow.hide(); else showMain();
  });
  buildTrayMenu();
}

async function restartBackend() {
  if (!ownsBackend) {
    await dialog.showMessageBox({
      type: 'info',
      message: 'Vivieen is using an engine started outside Electron.',
      detail: 'Restart it from the terminal, or quit that engine and reopen Vivieen.',
    });
    return { ok: false, error: 'external backend' };
  }
  stopBackend();
  await sleep(350);
  try {
    await startBackend();
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.loadURL(`${baseUrl()}/?electron=1&app=${encodeURIComponent(app.getVersion())}`);
    }
    if (settingsWindow && !settingsWindow.isDestroyed()) {
      settingsWindow.loadURL(`${baseUrl()}/settings?electron=1&app=${encodeURIComponent(app.getVersion())}`);
    }
    broadcastState();
    return { ok: true };
  } catch (error) {
    dialog.showErrorBox('Voice engine failed to restart', String(error.message || error));
    return { ok: false, error: String(error.message || error) };
  }
}

function installIpc() {
  ipcMain.handle('vivieen:get-state', () => shellState());
  ipcMain.handle('vivieen:save-motion-asset', saveMotionAsset);
  ipcMain.handle('vivieen:get-enconvo-samples', (_event, afterSequence = 0) => {
    const requested = Number(afterSequence);
    const after = Number.isFinite(requested) && requested > 0
      ? requested : Math.max(0, enconvoSampleSequence - 1);
    return {
      sequence: enconvoSampleSequence,
      samples: enconvoSampleBuffer.filter((sample) => sample.sequence > after),
    };
  });
  ipcMain.handle('vivieen:open-settings', () => { openSettings(); return shellState(); });
  ipcMain.handle('vivieen:open-appearance', () => { showAppearanceWindow(); return shellState(); });
  ipcMain.handle('vivieen:show-main', () => { showMain(); return shellState(); });
  ipcMain.handle('vivieen:hide-main', () => { if (mainWindow) mainWindow.hide(); });
  ipcMain.handle('vivieen:minimize', () => { if (mainWindow) mainWindow.minimize(); });
  ipcMain.handle('vivieen:toggle-top', () => applyAlwaysOnTop(!state.alwaysOnTop));
  ipcMain.handle('vivieen:pet-menu', () => { showPetMenu(); return shellState(); });
  ipcMain.handle('vivieen:set-pet-view', (_event, value) => applyPetView(value));
  ipcMain.handle('vivieen:set-pet-opacity', (_event, value) => applyPetOpacity(value));
  ipcMain.handle('vivieen:set-pet-zoom', (_event, value) => applyPetZoom(value));
  ipcMain.handle('vivieen:set-pet-roam-zoom', (_event, value) => applyPetRoamZoom(value));
  ipcMain.on('vivieen:pet-zoom-live', (event, payload) => {
    if (mainWindow && event.sender === mainWindow.webContents) applyPetZoomLive(payload);
  });
  ipcMain.handle('vivieen:set-pet-click-through', (_event, value) => applyPetClickThrough(value));
  ipcMain.handle('vivieen:set-pet-lock', (_event, value) => applyPetLock(value));
  ipcMain.handle('vivieen:set-pet-roam', (_event, value) => applyPetRoam(value));
  ipcMain.handle('vivieen:trigger-enconvo-voice', (event) => {
    if (!mainWindow || event.sender !== mainWindow.webContents) return { ok: false };
    return triggerEnconvoVoiceCommand();
  });
  ipcMain.on('vivieen:show-speech-bubble', (event, value) => {
    if (mainWindow && event.sender === mainWindow.webContents) showSpeechBubble(value);
  });
  ipcMain.on('vivieen:pet-motion-ready', (event, value) => {
    if (mainWindow && event.sender === mainWindow.webContents) setPetMotionReady(value);
  });
  ipcMain.on('vivieen:pet-engaged', (event, value) => {
    if (mainWindow && event.sender === mainWindow.webContents) setPetEngaged(value);
  });
  ipcMain.on('vivieen:pet-hit', (event, value) => {
    if (mainWindow && event.sender === mainWindow.webContents) setPetHit(Boolean(value), 'renderer-alpha');
  });
  ipcMain.on('vivieen:pet-voice-key', (event, state) => {
    if (mainWindow && event.sender === mainWindow.webContents) postVoiceKey(String(state || ''));
  });
  ipcMain.on('vivieen:pet-dock', (event) => {
    // The stillness idle leans on the right screen edge, so the window
    // settles bottom-right above the Dock first. Locked or roaming pets
    // stay where they are and idle in place.
    if (!mainWindow || event.sender !== mainWindow.webContents) return;
    if (state.petRoam || state.petLocked) return;
    const area = screen.getDisplayMatching(mainWindow.getBounds()).workArea;
    const size = petZoomSize(PET_BASE_SIZE, PET_NORMAL_MINIMUM, state.petZoom);
    const bounds = dockedPetBounds(size, area, PET_DOCK_MARGIN);
    mainWindow.setBounds(bounds, false);
    state.bounds = { ...bounds };
    saveStateSoon();
  });
  ipcMain.on('vivieen:drag-start', (event, point) => {
    if (!mainWindow || event.sender !== mainWindow.webContents
        || state.petLocked || state.petRoam) return;
    petDrag = {
      x: Number(point && point.screenX) || 0,
      y: Number(point && point.screenY) || 0,
      bounds: mainWindow.getBounds(),
    };
  });
  ipcMain.on('vivieen:drag-move', (event, point) => {
    if (!petDrag || !mainWindow || event.sender !== mainWindow.webContents
        || state.petLocked || state.petRoam) return;
    const x = Math.round(petDrag.bounds.x + (Number(point && point.screenX) - petDrag.x));
    const y = Math.round(petDrag.bounds.y + (Number(point && point.screenY) - petDrag.y));
    if (Number.isFinite(x) && Number.isFinite(y)) mainWindow.setPosition(x, y, false);
  });
  ipcMain.on('vivieen:drag-end', () => { petDrag = null; saveStateSoon(); });
  ipcMain.handle('vivieen:set-enconvo-monitor', (_event, value) => setEnconvoMonitoring(value));
  ipcMain.handle('vivieen:avatar-changed', () => {
    if (state.petRoam) applyPetRoam(false);
    petMotionReady = false;
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.reloadIgnoringCache();
    return true;
  });
  ipcMain.handle('vivieen:restart-backend', restartBackend);
}

function installRequestAuthentication() {
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: ['<all_urls>'] },
    (details, callback) => {
      try {
        if (new URL(details.url).origin === baseUrl()) {
          details.requestHeaders['X-Vivieen-Token'] = backendToken;
        }
      } catch {}
      callback({ requestHeaders: details.requestHeaders });
    },
  );
}

function installPermissions() {
  const allowedOrigin = baseUrl();
  const sameOrigin = (value) => {
    try { return new URL(value).origin === allowedOrigin; } catch { return false; }
  };
  const audioOnly = (details = {}) => {
    const mediaTypes = details.mediaTypes || [];
    return mediaTypes.length === 0 || mediaTypes.every((value) => value === 'audio');
  };
  session.defaultSession.setPermissionCheckHandler(
    (_webContents, permission, requestingOrigin, details) => (
      permission === 'media' && sameOrigin(requestingOrigin) && audioOnly(details)
    ),
  );
  session.defaultSession.setPermissionRequestHandler(
    (webContents, permission, callback, details = {}) => {
      const url = webContents.getURL();
      callback(permission === 'media' && sameOrigin(url)
        && (details.mediaTypes || []).length > 0 && audioOnly(details));
    },
  );
}

async function boot() {
  state = loadState();
  createEnconvoMonitor();
  installIpc();
  try {
    await startBackend();
  } catch (error) {
    await dialog.showMessageBox({
      type: 'error',
      message: 'Vivieen could not start her voice engine.',
      detail: `${error.message || error}\n\nBackend log: ${logPath()}`,
    });
    app.quit();
    return;
  }
  installRequestAuthentication();
  installPermissions();
  createMainWindow();
  createTray();
  installRecoveryShortcut();
  if (state.followEnconvo) enconvoMonitor.setEnabled(true);
  const metadata = await vivieenMetadata(3000);
  if (!metadata || !metadata.active) openSettings();
}

const lock = app.requestSingleInstanceLock();
if (!lock) {
  app.quit();
} else {
  app.on('second-instance', showMain);
  app.whenReady().then(boot);
}

app.on('activate', showMain);
app.on('before-quit', () => {
  quitting = true;
  globalShortcut.unregisterAll();
  stopPetPointerTracking();
  clearTimeout(bubbleTimer);
  if (bubbleWindow && !bubbleWindow.isDestroyed()) bubbleWindow.destroy();
  if (appearanceWindow && !appearanceWindow.isDestroyed()) appearanceWindow.destroy();
  if (enconvoMonitor) enconvoMonitor.dispose();
  stopBackend();
  if (backendLog) backendLog.end();
});
app.on('window-all-closed', () => {
  // Tray application: closing the last window does not stop the voice engine.
});
