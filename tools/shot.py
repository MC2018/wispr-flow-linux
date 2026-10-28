# Screenshot one X window by id (or the whole screen) to PNG.
# Usage: shot.py [0xWINDOWID] out.png
import sys
from Xlib import display, X
from PIL import Image
d = display.Display(':0')
wid = int(sys.argv[1], 16) if len(sys.argv) > 2 else None
out = sys.argv[-1]
root = d.screen().root
if wid:
    w = d.create_resource_object('window', wid)
    g = w.get_geometry()
    t = w.translate_coords(root, 0, 0)
    x, y, wd, ht = -t.x, -t.y, g.width, g.height
else:
    g = root.get_geometry(); x, y, wd, ht = 0, 0, g.width, g.height
raw = root.get_image(x, y, wd, ht, X.ZPixmap, 0xffffffff)
Image.frombytes('RGB', (wd, ht), raw.data, 'raw', 'BGRX').save(out)
print(out, wd, ht)
