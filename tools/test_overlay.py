# Put a solid-colour test window under the Status overlay, change its colour, and check the screen follows.
import time, gi
gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib
from Xlib import display, X
d = display.Display(':0'); root = d.screen().root
def status_rect():
    for w in root.query_tree().children:
        n = w.get_full_property(d.intern_atom('_NET_WM_NAME'), 0)
        if n and n.value == b'Status' and w.get_attributes().map_state == 2:
            g = w.get_geometry(); t = w.translate_coords(root, 0, 0); return -t.x, -t.y, g.width, g.height
x, y, w, h = status_rect()
win = Gtk.Window(title="overlay-test"); win.set_decorated(False); win.move(x - 50, y - 50); win.resize(w + 100, h + 100)
css = Gtk.CssProvider(); win.get_style_context().add_provider(css, 800)
def color(c): css.load_from_data(f"window {{ background: {c}; }}".encode())
def sample():
    px = root.get_image(x + w // 2, y + h // 2, 1, 1, X.ZPixmap, 0xffffffff).data
    return (px[2], px[1], px[0])
steps = [("#ff0000", (255, 0, 0)), ("#0000ff", (0, 0, 255)), ("#00ff00", (0, 255, 0))]
results = []
def run(i=0):
    if i: results.append((steps[i - 1][1], sample()))
    if i == len(steps):
        from PIL import Image; g = root.get_geometry(); Image.frombytes("RGB", (g.width, g.height), root.get_image(0, 0, g.width, g.height, X.ZPixmap, 0xffffffff).data, "raw", "BGRX").crop((x - 80, y - 80, x + w + 80, min(1080, y + h + 80))).save("/tmp/wispr-overlay-test.png")
        ok = all(e == s for e, s in results)
        print("overlay see-through:", "YES" if ok else "NO (stale pixels)", results); Gtk.main_quit(); return False
    color(steps[i][0]); GLib.timeout_add(700, run, i + 1); return False
color("#ff0000"); win.show_all(); GLib.timeout_add(900, run, 0); Gtk.main()
