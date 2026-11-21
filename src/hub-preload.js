// Preload for the hub (the main Wispr Flow window). The hub has no window frame, so it draws its own title bar.
// Only a Windows page adds the minimize, maximize and close buttons to it.
require('./windows-preload')('hub');
