'use strict';

const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  ipcMain,
  nativeImage,
  screen,
  session,
  shell,
} = require('electron');
const { spawn, execFileSync } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');

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
let tray = null;
let state = null;
let saveTimer = null;
let backendLog = null;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const baseUrl = () => `http://${HOST}:${port}`;
const codeRoot = () => app.isPackaged
  ? path.join(process.resourcesPath, 'backend')
  : path.resolve(__dirname, '..');
const dataRoot = () => app.isPackaged
  ? path.join(app.getPath('userData'), 'backend-data')
  : codeRoot();
const statePath = () => path.join(app.getPath('userData'), 'window-state.json');
const logPath = () => path.join(app.getPath('userData'), 'backend.log');

function ensureDataRoot() {
  fs.mkdirSync(dataRoot(), { recursive: true, mode: 0o700 });
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
    bounds: { width: 560, height: 760 },
  };
}

function loadState() {
  try {
    return { ...defaultState(), ...JSON.parse(fs.readFileSync(statePath(), 'utf8')) };
  } catch {
    return defaultState();
  }
}

function saveStateSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    state.bounds = mainWindow.getBounds();
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
    packaged: app.isPackaged,
  };
}

function broadcastState() {
  const value = shellState();
  for (const window of [mainWindow, settingsWindow]) {
    if (window && !window.isDestroyed()) window.webContents.send('vivieen:state', value);
  }
  buildTrayMenu();
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

function guardNavigation(window, kind) {
  window.webContents.on('will-navigate', (event, target) => {
    let targetUrl;
    try { targetUrl = new URL(target); } catch { event.preventDefault(); return; }
    if (targetUrl.origin !== baseUrl()) {
      event.preventDefault();
      if (targetUrl.protocol === 'https:') shell.openExternal(target).catch(() => {});
      return;
    }
    if (kind === 'main' && targetUrl.pathname === '/settings') {
      event.preventDefault();
      openSettings();
    }
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

function createMainWindow() {
  const bounds = visibleBounds(state.bounds);
  mainWindow = new BrowserWindow({
    ...bounds,
    minWidth: 420,
    minHeight: 590,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    roundedCorners: true,
    hasShadow: true,
    resizable: true,
    fullscreenable: false,
    title: 'Vivieen',
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
  applyAlwaysOnTop(state.alwaysOnTop);
  guardNavigation(mainWindow, 'main');
  mainWindow.loadURL(`${baseUrl()}/?electron=1&app=${encodeURIComponent(app.getVersion())}`);
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('move', saveStateSoon);
  mainWindow.on('resize', saveStateSoon);
  mainWindow.on('close', (event) => {
    if (!quitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

function showMain() {
  if (!mainWindow || mainWindow.isDestroyed()) createMainWindow();
  mainWindow.show();
  mainWindow.focus();
  if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.hide();
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
    { type: 'separator' },
    { label: 'Always on Top', type: 'checkbox', checked: state.alwaysOnTop,
      click: (item) => applyAlwaysOnTop(item.checked) },
    { label: 'Restart Voice Engine', enabled: ownsBackend, click: restartBackend },
    { type: 'separator' },
    { label: 'Quit Vivieen', accelerator: 'CommandOrControl+Q', click: () => app.quit() },
  ]));
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
  ipcMain.handle('vivieen:open-settings', () => { openSettings(); return shellState(); });
  ipcMain.handle('vivieen:show-main', () => { showMain(); return shellState(); });
  ipcMain.handle('vivieen:hide-main', () => { if (mainWindow) mainWindow.hide(); });
  ipcMain.handle('vivieen:minimize', () => { if (mainWindow) mainWindow.minimize(); });
  ipcMain.handle('vivieen:toggle-top', () => applyAlwaysOnTop(!state.alwaysOnTop));
  ipcMain.handle('vivieen:avatar-changed', () => {
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
  stopBackend();
  if (backendLog) backendLog.end();
});
app.on('window-all-closed', () => {
  // Tray application: closing the last window does not stop the voice engine.
});
