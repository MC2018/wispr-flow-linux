// Preload for the status overlay (pill + notifications): runs Wispr's own status preload with its isWindows flag on.
// The main process already treats Linux as Windows (see loader.js) and clips the overlay to the shape the page
// reports. Only a Windows page reports its visible parts (tooltips, pop-up shadows) as well as its buttons;
// otherwise those get cut off.
const fs = require('fs');
const path = require('path');
const Module = require('module');

const file = path.join(process.resourcesPath, 'wispr.asar', '.webpack', 'renderer', 'status', 'preload.js');
const IS_WINDOWS = /(\w+)="win32"===process\.platform(?=,\w+="darwin"===process\.platform)/;
let src = fs.readFileSync(file, 'utf8');
if (IS_WINDOWS.test(src)) src = src.replace(IS_WINDOWS, '$1=!0');
else {
  const log = path.join(require('os').homedir(), '.cache', 'wispr-linux', 'loader.log');
  fs.appendFileSync(log, `[${new Date().toISOString()}] PATCH NOT APPLIED (bundle changed?): status preload isWindows\n`);
}
const m = new Module(file, module);
m.filename = file;
m.paths = Module._nodeModulePaths(path.dirname(file));
m._compile(src, file);
