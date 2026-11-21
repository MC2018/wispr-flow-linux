// Runs one of Wispr's own page preloads with its isWindows flag on. Each page bundle has its own copy of
// that flag, separate from the main process one that loader.js turns on. Used by status-preload.js and
// hub-preload.js.
const fs = require('fs');
const path = require('path');
const Module = require('module');

const IS_WINDOWS = /(\w+)="win32"===process\.platform(?=,\w+="darwin"===process\.platform)/;

module.exports = function runWindowsPreload(page) {
  const file = path.join(process.resourcesPath, 'wispr.asar', '.webpack', 'renderer', page, 'preload.js');
  let src = fs.readFileSync(file, 'utf8');
  if (IS_WINDOWS.test(src)) src = src.replace(IS_WINDOWS, '$1=!0');
  else {
    const log = path.join(require('os').homedir(), '.cache', 'wispr-linux', 'loader.log');
    fs.appendFileSync(log, `[${new Date().toISOString()}] PATCH NOT APPLIED (bundle changed?): ${page} preload isWindows\n`);
  }
  const m = new Module(file, module);
  m.filename = file;
  m.paths = Module._nodeModulePaths(path.dirname(file));
  m._compile(src, file);
};
