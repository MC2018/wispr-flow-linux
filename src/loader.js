// Linux shim for Wispr Flow: runs the official Windows build's JS on Linux Electron,
// swapping the Windows-only "Wispr Flow Helper.exe" for a Linux helper.
const path = require('path');
const fs = require('fs');
const cp = require('child_process');
const { app, clipboard, BrowserWindow } = require('electron');

const LOG = path.join(require('os').homedir(), '.cache', 'wispr-linux', 'loader.log');
fs.mkdirSync(path.dirname(LOG), { recursive: true });
const log = (...a) => fs.appendFileSync(LOG, `[${new Date().toISOString()}] ${a.join(' ')}\n`);
log('loader start, electron', process.versions.electron);

const HELPER = path.join(__dirname, 'helper', 'wispr-linux-helper.py');
const isHelperPath = (p) => typeof p === 'string' && p.endsWith('Wispr Flow Helper.exe');

// The app checks the helper exists before spawning it.
const origExists = fs.existsSync;
fs.existsSync = function (p) { return isHelperPath(p) ? true : origExists.apply(this, arguments); };

// Clipboard handoff: the Windows helper sets the clipboard itself; here Electron does it
// (it owns the X selection), then the Linux helper only presses Ctrl+V.
let restoreTimer = null;
function handlePasteRequest(msg) {
  const req = msg.HelperAPIRequest;
  if (!req || !req.PasteText) return;
  const { text, htmlText } = req.PasteText.payload;
  const previous = clipboard.readText();
  if (restoreTimer) clearTimeout(restoreTimer);
  if (htmlText) clipboard.write({ text, html: htmlText }); else clipboard.writeText(text);
  restoreTimer = setTimeout(() => { clipboard.writeText(previous); restoreTimer = null; }, 1500);
}

const unescape = (s) => s.replaceAll('+2', '|').replaceAll('+1', '+');
const origSpawn = cp.spawn;
cp.spawn = function (cmd, args, opts) {
  if (!isHelperPath(cmd)) {
    if (typeof cmd === 'string' && /\.exe$|powershell|wmic|^reg$/i.test(cmd)) log('spawn (windows-only):', cmd, JSON.stringify(args || []).slice(0, 200));
    return origSpawn.apply(this, arguments);
  }
  if (!Array.isArray(args)) { opts = args; args = []; }
  log('spawning Linux helper instead of', cmd);
  const child = origSpawn.call(this, 'python3', ['-u', HELPER, ...args], {
    ...opts,
    env: { ...process.env, ...(opts && opts.env) },
  });
  let pending = '';
  const write = child.stdin.write.bind(child.stdin);
  child.stdin.write = function (chunk, ...rest) {
    try {
      pending += chunk.toString();
      let i;
      while ((i = pending.indexOf('|')) !== -1) {
        const raw = pending.slice(0, i); pending = pending.slice(i + 1);
        if (raw.includes('"PasteText"')) handlePasteRequest(JSON.parse(unescape(raw)));
      }
    } catch (e) { log('paste intercept error', e && e.stack); }
    return write(chunk, ...rest);
  };
  return child;
};

// The status pill and its notifications ignore clicks until the app's poll sees the cursor over them. That poll
// uses screen.getCursorScreenPoint(), which on X11 stays frozen while the pointer is over other apps' windows,
// so the X / checkmark / dismiss buttons never became clickable. Serve the real pointer position instead.
// Electron also drops setIgnoreMouseEvents' {forward: true} on Linux, so a click-through window never sees the
// mouse move over or leave it and the pill's hover state (tooltip, expanded buttons) sticks. Forward moves here.
const forwarding = new Set();
const hovered = new Set();
const setIgnore = BrowserWindow.prototype.setIgnoreMouseEvents;
BrowserWindow.prototype.setIgnoreMouseEvents = function (ignore, opts) {
  if (ignore && opts && opts.forward) forwarding.add(this); else forwarding.delete(this);
  return setIgnore.apply(this, arguments);
};
function forwardPointer(p) {
  for (const win of forwarding) {
    if (win.isDestroyed()) { forwarding.delete(win); hovered.delete(win); continue; }
    const b = win.getBounds();
    if (win.isVisible() && p.x >= b.x && p.y >= b.y && p.x < b.x + b.width && p.y < b.y + b.height) {
      hovered.add(win);
      win.webContents.sendInputEvent({ type: 'mouseMove', x: p.x - b.x, y: p.y - b.y });
    } else if (hovered.delete(win)) {
      win.webContents.sendInputEvent({ type: 'mouseLeave', x: 0, y: 0 });
    }
  }
}

// The status overlay clips itself to the shape of its visible parts, and its hover animation changes that shape every
// frame. On Cinnamon each shape change flashes white boxes and stalls the page's frames, so the pill popped open in
// chunks. Pad the shape so the animation and tooltip fit inside it, grow it at once, shrink it only once things settle.
const SHAPE_PAD = 64;
const SHAPE_SETTLE_MS = 400;
const shapes = new WeakMap();
const setShape = BrowserWindow.prototype.setShape;
const covers = (shape, rects) => rects.every((r) => shape.some((s) =>
  r.x >= s.x && r.y >= s.y && r.x + r.width <= s.x + s.width && r.y + r.height <= s.y + s.height));
BrowserWindow.prototype.setShape = function (rects) {
  const s = shapes.get(this) || {};
  shapes.set(this, s);
  clearTimeout(s.timer);
  const apply = (r) => { s.applied = r; setShape.call(this, r); };
  // The app hides the overlay with a 1px shape (see "status shape never empty"): apply that as is.
  if (rects.length === 1 && rects[0].width <= 1 && rects[0].height <= 1) return apply(rects);
  const { width, height } = this.getBounds();
  const padded = rects.map((r) => {
    const x = Math.max(0, r.x - SHAPE_PAD), y = Math.max(0, r.y - SHAPE_PAD);
    return { x, y, width: Math.min(width, r.x + r.width + SHAPE_PAD) - x, height: Math.min(height, r.y + r.height + SHAPE_PAD) - y };
  }).filter((r) => r.width > 0 && r.height > 0);
  if (!s.applied || !covers(s.applied, rects)) return apply(padded.length ? padded : rects);
  s.timer = setTimeout(() => {
    if (!this.isDestroyed() && covers(s.applied, padded) && !covers(padded, s.applied)) apply(padded);
  }, SHAPE_SETTLE_MS);
};

app.whenReady().then(() => {
  const { screen } = require('electron');
  const orig = screen.getCursorScreenPoint.bind(screen);
  let pointer = null;
  const watcher = origSpawn('python3', ['-u', path.join(__dirname, 'helper', 'pointer-watch.py')],
    { stdio: ['ignore', 'pipe', 'ignore'] });
  let buf = '';
  watcher.stdout.on('data', (chunk) => {
    const lines = (buf + chunk).split('\n');
    buf = lines.pop();
    const [x, y] = lines.pop()?.split(' ').map(Number) ?? [];
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    pointer = { x, y };
    try { forwardPointer(screen.getCursorScreenPoint()); } catch (e) { log('pointer forward error', e.message); }
  });
  watcher.on('error', (e) => log('pointer watcher failed:', e.message));
  watcher.on('exit', (code) => { pointer = null; log('pointer watcher exited', code); });
  app.on('will-quit', () => watcher.kill());
  // X reports physical pixels; Electron works in DIPs (one scale factor for all displays on X11).
  screen.getCursorScreenPoint = () => {
    if (!pointer) return orig();
    const s = screen.getPrimaryDisplay().scaleFactor || 1;
    return { x: Math.round(pointer.x / s), y: Math.round(pointer.y / s) };
  };
});

// Windows-only Electron APIs that don't exist on Linux.
for (const fn of ['setUserTasks', 'setJumpList', 'setAppUserModelId']) {
  if (typeof app[fn] !== 'function') app[fn] = () => { log(`stubbed app.${fn}()`); return true; };
}

// Windows reads the window icon from the .exe; on Linux each window has to be given one.
const ICON = path.join(__dirname, 'icon.png');
if (fs.existsSync(ICON)) {
  app.on('browser-window-created', (_e, win) => {
    try { win.setIcon(ICON); } catch (e) { log('setIcon failed', e.message); }
  });
}

// Log any other Windows-only programs the app tries to run (reg.exe, Update.exe, powershell...).
for (const fn of ['exec', 'execFile', 'execSync', 'execFileSync', 'spawnSync']) {
  const orig = cp[fn];
  cp[fn] = function (cmd, ...rest) {
    if (typeof cmd === 'string' && /\.exe\b|powershell|wmic|\breg\b/i.test(cmd)) log(`${fn}:`, cmd.slice(0, 200));
    return orig.call(this, cmd, ...rest);
  };
}

// The Windows build keeps its data under %APPDATA%; point that at XDG dirs.
const home = require('os').homedir();
process.env.APPDATA = process.env.APPDATA || path.join(home, '.config');
process.env.LOCALAPPDATA = process.env.LOCALAPPDATA || path.join(home, '.local', 'share');

// Load the official main bundle with a few targeted patches.
const Module = require('module');
const bundle = path.join(process.resourcesPath, 'wispr.asar', '.webpack', 'main', 'index.js');
let src = fs.readFileSync(bundle, 'utf8');
const PATCHES = [
  // Treat Linux as Windows: Windows code paths (default shortcuts, deep links, helper IPC) fit Linux far better
  // than the macOS ones (fn key, Accessibility permissions, /Applications gate).
  ['isWindows flag', 'u="win32"===process.platform,d=l&&', 'u=!0,d=l&&'],
  // On X11 a non-focusable window becomes override-redirect: the window manager can't move, raise or
  // dismiss it, so the hub would sit on top of everything. Make the hub a normal window.
  ['hub window focusable', /(title:"Flow Hub",[^;]*?)focusable:!1\}/, '$1focusable:!0}'],
  // The status overlay hides itself by setting an empty (or off-window) shape. Cinnamon's compositor then stops
  // repainting under it and shows stale pixels. Keep a 1px on-window shape instead: still invisible.
  ['status shape never empty', 'n!==this.lastShapeKey&&(t.setShape(e),this.lastShapeKey=n)',
    'n!==this.lastShapeKey&&(t.setShape(e.some(r=>r.x+r.width>0&&r.y+r.height>0)?e:[{x:0,y:0,width:1,height:1}]),this.lastShapeKey=n)'],
  // The status overlay takes clicks only once its hover poll sees the cursor over a button. Every 400 ms is fine on
  // Windows, where mouse moves also trigger it; Linux has only the poll, so quick clicks got lost. Poll every 50 ms.
  ['status hover poll 50ms', /(\w=100,\w=\(e,t=0,n=)400(\)=>\{if\(!e\|\|e\.isDestroyed\(\)\))/, '$150$2'],
  // The status page has its own isWindows flag; without it, tooltips and pop-ups are cut off. See status-preload.js.
  ['status preload', 'preload:require("path").resolve(__dirname,"../renderer","status","preload.js")',
    `preload:${JSON.stringify(path.join(__dirname, 'status-preload.js'))}`],
  // The hub has no window frame and draws its own title bar, with minimize/maximize/close only on Windows.
  // See hub-preload.js.
  ['hub preload', 'preload:require("path").resolve(__dirname,"../renderer","hub","preload.js")',
    `preload:${JSON.stringify(path.join(__dirname, 'hub-preload.js'))}`],
];
// Each patch is a literal string (every copy replaced) or a regex (first match replaced) for spots where the
// minifier's variable names change between releases.
for (const [name, from, to] of PATCHES) {
  const found = typeof from === 'string' ? src.includes(from) : from.test(src);
  if (!found) { log('PATCH NOT APPLIED (bundle changed?):', name); continue; }
  src = typeof from === 'string' ? src.split(from).join(to) : src.replace(from, to);
  log('patched:', name);
}
const m = new Module(bundle, module);
m.filename = bundle;
m.paths = Module._nodeModulePaths(path.dirname(bundle));
require.cache[bundle] = m;
m._compile(src, bundle);
