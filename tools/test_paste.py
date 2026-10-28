# End-to-end test of the Linux helper's paste path, in a throwaway GTK window.
import os, json, socket, subprocess, sys, time, uuid
import gi
gi.require_version("Gtk", "3.0"); gi.require_version("GdkX11", "3.0")
from gi.repository import Gtk, Gdk, GLib, GdkX11
from Xlib import display

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "wispr-linux-helper.py")
TEXT = "Hello from the Linux Wispr helper! (+|) ✓"
esc = lambda s: s.replace("+", "+1").replace("|", "+2") + "|"

win = Gtk.Window(title="wispr-paste-test"); win.set_default_size(500, 120)
view = Gtk.TextView(); win.add(view); win.show_all(); win.present(); view.grab_focus()
Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(TEXT, -1)

parent, child = socket.socketpair()
proc = subprocess.Popen([sys.executable, "-u", HELPER], stdin=subprocess.PIPE, pass_fds=(child.fileno(),),
                        preexec_fn=lambda: os.dup2(child.fileno(), 3))
child.close(); parent.setblocking(False)
d = display.Display(); act = d.intern_atom("_NET_ACTIVE_WINDOW")
state = {"sent": False, "buf": "", "t0": time.time()}

def tick():
    xid = win.get_window().get_xid()
    active = d.screen().root.get_full_property(act, 0).value[0]
    if not state["sent"] and active == xid and time.time() - state["t0"] > 1.0:
        for req in ({"IsReady": True}, {"PasteText": {"payload": {"pasteId": "t1", "text": TEXT}}}):
            proc.stdin.write(esc(json.dumps({"HelperAPIRequest": {"uuid": str(uuid.uuid4()), **req}})).encode()); proc.stdin.flush()
        state["sent"] = True; state["ts"] = time.time()
    try:
        state["buf"] += parent.recv(65536).decode()
    except BlockingIOError:
        pass
    if state["sent"] and time.time() - state["ts"] > 1.5 or time.time() - state["t0"] > 8:
        b = view.get_buffer(); got = b.get_text(b.get_start_iter(), b.get_end_iter(), True)
        msgs = [m.replace("+2", "|").replace("+1", "+") for m in state["buf"].split("|") if m]
        print("focused+sent:", state["sent"]); print("pasted text:", repr(got)); print("match:", got == TEXT)
        print("helper replies:", [list(json.loads(m).values())[0].keys() for m in msgs][:6])
        proc.stdin.close(); proc.wait(timeout=3); Gtk.main_quit(); return False
    return True

GLib.timeout_add(100, tick); Gtk.main()
