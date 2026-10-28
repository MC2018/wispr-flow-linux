# Report every Wispr Flow X window: mapped?, override-redirect?, geometry, and its input (click) region.
from Xlib import display, X
from Xlib.ext import shape
d = display.Display(':0'); root = d.screen().root
NAME = d.intern_atom('_NET_WM_NAME')
def walk(w, depth=0):
    try: kids = w.query_tree().children
    except Exception: return
    for k in kids:
        try:
            c = k.get_wm_class()
            if c and 'wispr' in ' '.join(c).lower():
                a = k.get_attributes(); g = k.get_geometry(); t = k.translate_coords(root, 0, 0)
                n = k.get_full_property(NAME, 0); n = n.value.decode() if n else '?'
                inp = shape.get_rectangles(k, shape.SK.Input).rectangles
                area = sum(r.width*r.height for r in inp)
                print(f"{hex(k.id)} {n!r:28} {'MAPPED' if a.map_state==2 else 'hidden'} override={a.override_redirect} "
                      f"at=({-t.x},{-t.y}) {g.width}x{g.height} input_rects={len(inp)} input_area={area}")
        except Exception as e: print('err', e)
        if depth < 2: walk(k, depth+1)
walk(root)
p = root.query_pointer(); print('pointer', p.root_x, p.root_y, 'child', hex(p.child))
