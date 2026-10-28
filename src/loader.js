// Linux shim for Wispr Flow: runs the official Windows build's JS on Linux Electron,
// swapping the Windows-only "Wispr Flow Helper.exe" for a Linux helper.
const path = require('path');
const fs = require('fs');
const cp = require('child_process');
const { app, clipboard } = require('electron');

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
  ['hub window focusable', 'focusable:!1};w.tD?Object.assign(t,{frame:!1', 'focusable:!0};w.tD?Object.assign(t,{frame:!1'],
  // The status overlay hides itself by setting an empty (or off-window) shape. Cinnamon's compositor then stops
  // repainting under it and shows stale pixels. Keep a 1px on-window shape instead: still invisible.
  ['status shape never empty', 'n!==this.lastShapeKey&&(t.setShape(e),this.lastShapeKey=n)',
    'n!==this.lastShapeKey&&(t.setShape(e.some(r=>r.x+r.width>0&&r.y+r.height>0)?e:[{x:0,y:0,width:1,height:1}]),this.lastShapeKey=n)'],
];
for (const [name, from, to] of PATCHES) {
  if (src.includes(from)) { src = src.split(from).join(to); log('patched:', name); }
  else log('PATCH NOT APPLIED (bundle changed?):', name);
}
const m = new Module(bundle, module);
m.filename = bundle;
m.paths = Module._nodeModulePaths(path.dirname(bundle));
require.cache[bundle] = m;
m._compile(src, bundle);
