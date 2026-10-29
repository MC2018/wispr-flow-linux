#!/usr/bin/env python3
"""Prints the X pointer position as "x y" lines (physical pixels) whenever it moves.

Electron's screen.getCursorScreenPoint() goes stale on X11 while the pointer is over other apps' windows,
so loader.js reads the position from here instead. Exits when Electron (its parent) goes away.
"""
import os
import sys
import time

from Xlib import display

parent = os.getppid()
root = display.Display().screen().root
last = None
while os.getppid() == parent:
    p = root.query_pointer()
    pos = (p.root_x, p.root_y)
    if pos != last:
        last = pos
        try:
            sys.stdout.write("%d %d\n" % pos)
            sys.stdout.flush()
        except BrokenPipeError:
            break
    time.sleep(0.03)
