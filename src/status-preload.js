// Preload for the status overlay (pill + notifications). The main process clips the overlay to the shape the page
// reports. Only a Windows page reports its visible parts (tooltips, pop-up shadows) as well as its buttons;
// otherwise those get cut off.
require('./windows-preload')('status');
