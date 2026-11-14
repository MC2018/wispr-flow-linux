# End-to-end test of edit detection (UpdateEditedText), run on a nested Xephyr display with a private
# D-Bus/AT-SPI session, so no keys reach your real windows.
# The helper pastes, the "user" fixes a word, then the box loses focus or gets cleared (chat-style send).
# gtk-rebuild: the box briefly reads one letter (as Zen's editors do) before focus moves; the fix must still be reported.
# GTK and Firefox are read over AT-SPI; Chrome and Electron can't be, so their cases test the keystroke model
# (typing, a double-clicked word retyped close to or unlike the original, a single click followed by Backspace).
# Usage: python3 tools/test_edits.py [case...]
#   needs Xephyr; Firefox and google-chrome/chromium for those cases; set WISPR_TEST_ELECTRON=/path/to/electron
#   to add Electron cases.
import json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.parse, uuid

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "wispr-linux-helper.py")

if os.environ.get("WISPR_TEST_NESTED") != "1":  # outer run: start Xephyr + a private session, re-run inside it
    disp = ":97"
    xephyr = subprocess.Popen(["Xephyr", disp, "-screen", "1000x500", "-ac", "-br", "-noreset"],
                              env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    env = {k: v for k, v in os.environ.items() if k != "AT_SPI_BUS_ADDRESS"}
    env.update(DISPLAY=disp, WISPR_TEST_NESTED="1", WISPR_HELPER_LOG=os.path.join(tempfile.gettempdir(), "wispr-test-helper.log"))
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    cases = sys.argv[1:] or (["gtk-focus", "gtk-clear", "gtk-rebuild"] + (["firefox"] if shutil.which("firefox") else [])
                             + (["chrome-keys", "chrome-dblclick", "chrome-dblclick-new", "chrome-click"] if chrome else [])
                             + (["electron-dblclick"] if os.environ.get("WISPR_TEST_ELECTRON") else []))
    if chrome:
        env["WISPR_TEST_CHROME"] = chrome
    results = []
    for case in cases:
        r = subprocess.run(["dbus-run-session", "--", sys.executable, __file__, case], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        print(r.stdout.strip())
        results.append(r.returncode == 0)
    xephyr.terminate()
    sys.exit(0 if all(results) else 1)

import gi  # noqa: E402
gi.require_version("Gtk", "3.0"); gi.require_version("GdkX11", "3.0")
from gi.repository import Gtk, Gdk, GLib, GdkX11  # noqa: E402,F401
from Xlib import display, X, Xatom, XK  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

CASE = sys.argv[1]
GTK = CASE.startswith("gtk")
BLINK = CASE.startswith(("chrome", "electron"))  # no AT-SPI text: the keystroke model is tested
TEXT = "I pushed the shuffle fix" if CASE == "firefox" else "I pushed the shuffle fix today."
NEW_WORD = "Spotify" if CASE == "chrome-dblclick-new" else "Shuffull"  # a retyped word needn't look alike
WANT = {"firefox": "I pushed the shuffull fix", "chrome-keys": "I pushed the shuffull fix today."}.get(
    CASE, f"I pushed the {NEW_WORD} fix today.")
CHAR_W, LINE_Y = 12.0, 12  # Blink page: 20px monospace textarea at the window's top-left corner
esc = lambda s: s.replace("+", "+1").replace("|", "+2") + "|"  # noqa: E731

d = display.Display(); root = d.screen().root; act = d.intern_atom("_NET_ACTIVE_WINDOW")
Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(TEXT, -1)

if GTK:
    win = Gtk.Window(title="wispr-edit-test"); win.set_default_size(500, 120)
    view = Gtk.TextView(); win.add(view); win.show_all(); view.grab_focus()
    buf = view.get_buffer(); buf.set_text("Hi team. ", -1); buf.place_cursor(buf.get_end_iter())
    other = Gtk.Window(title="other"); other.move(600, 0); other.show_all()
elif BLINK:
    tmp = tempfile.mkdtemp()
    url = "data:text/html," + urllib.parse.quote(
        '<body style=margin:0><textarea autofocus style="font:20px monospace;position:absolute;left:0;top:0;'
        'width:900px;height:300px;padding:0;border:0;outline:0"></textarea>')
    if CASE.startswith("electron"):
        with open(os.path.join(tmp, "package.json"), "w") as f:
            f.write('{"name": "wispr-edit-test", "main": "main.js"}')
        with open(os.path.join(tmp, "main.js"), "w") as f:
            f.write("const {app,BrowserWindow}=require('electron');app.whenReady().then(()=>new BrowserWindow("
                    "{x:0,y:0,width:950,height:400,frame:false}).loadURL(%s));" % json.dumps(url))
        ff = subprocess.Popen([os.environ["WISPR_TEST_ELECTRON"], tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        ff = subprocess.Popen([os.environ["WISPR_TEST_CHROME"], "--user-data-dir=" + tmp, "--no-first-run",
                               "--window-position=0,0", "--window-size=950,400", "--app=" + url],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
else:
    tmp = tempfile.mkdtemp()
    page = os.path.join(tmp, "t.html")
    with open(page, "w") as f:
        f.write("""<div id=e contenteditable style="min-height:200px"><p>Hi team.</p><p>Next: </p></div><script>
const e=document.getElementById('e'),p=e.lastElementChild.firstChild,r=document.createRange();e.focus();
r.setStart(p,p.length);getSelection().removeAllRanges();getSelection().addRange(r);</script>""")
    ff = subprocess.Popen(["firefox", "--no-remote", "--new-instance", "--profile", tmp, "file://" + page],
                          env={**os.environ, "GNOME_ACCESSIBILITY": "1"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

parent, child = socket.socketpair()
proc = subprocess.Popen(["bash", "-c", f'exec 3>&{child.fileno()}; exec "$0" -u "$1"', sys.executable, HELPER],
                        stdin=subprocess.PIPE, pass_fds=(child.fileno(),))
child.close(); parent.setblocking(False)


def xwin(w):
    return d.create_resource_object("window", w.get_window().get_xid()) if GTK else w


def activate(w):
    root.change_property(act, Xatom.WINDOW, 32, [w.id if w else 0])
    if w:
        w.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()


def blink_window():
    for w in root.query_tree().children:
        try:
            if w.get_attributes().map_state == X.IsViewable and w.get_geometry().width > 300:
                return w
        except Exception:  # noqa: BLE001
            pass


def tap(*names):
    codes = [d.keysym_to_keycode(XK.string_to_keysym(n)) for n in names]
    for kc in codes:
        xtest.fake_input(d, X.KeyPress, kc)
    for kc in reversed(codes):
        xtest.fake_input(d, X.KeyRelease, kc)
    d.sync(); time.sleep(0.05)


def type_text(s):
    for ch in s:
        if ch in " .,":
            tap({" ": "space", ".": "period", ",": "comma"}[ch])
        elif ch.isupper():
            tap("Shift_L", ch.lower())
        else:
            tap(ch)


def click(char_index, times=1):
    w = blink_window()
    pos = w.translate_coords(root, 0, 0)
    xtest.fake_input(d, X.MotionNotify, x=int(-pos.x + CHAR_W * char_index), y=int(-pos.y + LINE_Y))
    for _ in range(times):
        xtest.fake_input(d, X.ButtonPress, 1); xtest.fake_input(d, X.ButtonRelease, 1); d.sync(); time.sleep(0.05)
    time.sleep(0.3)


def send(req):
    proc.stdin.write(esc(json.dumps({"HelperAPIRequest": {"uuid": str(uuid.uuid4()), **req}})).encode())
    proc.stdin.flush()


def user_edit():
    word = TEXT.index("shuffle")
    if CASE == "chrome-keys":  # caret is at the end of the paste: backspace to the word and retype
        for _ in range(len(TEXT) - word - 5):
            tap("BackSpace")
        type_text("ull fix today.")
    elif "dblclick" in CASE:  # double-click the word, type over it
        click(word + 3.5, times=2)
        type_text(NEW_WORD)
    elif CASE == "chrome-click":  # click at the end of the word, backspace it, retype
        click(word + 7)
        for _ in range(7):
            tap("BackSpace")
        type_text("Shuffull")
    elif GTK:  # replace the word programmatically
        txt = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        i = txt.index("shuffle")
        buf.delete(buf.get_iter_at_offset(i), buf.get_iter_at_offset(i + 7))
        buf.insert(buf.get_iter_at_offset(i), "Shuffull")
        if CASE == "gtk-rebuild":  # then the editor rebuilds its nodes and the box reads one letter of the paste until focus moves
            GLib.timeout_add(600, lambda: buf.set_text("x", -1) or False)
    else:  # retype it with real keystrokes
        for k in ["BackSpace"] * 6 + list("ull") + ["space"] + list("fix"):
            tap(k)


def end_observation():
    if CASE in ("chrome-keys", "chrome-click"):
        tap("Return")  # chat-style send
    elif "dblclick" in CASE:
        tap("Tab")
    elif CASE == "gtk-clear":
        buf.set_text("", -1)  # a chat app sent the message and emptied the box
    else:
        activate(xwin(other) if GTK else None)


steps = [  # (seconds to wait before, action)
    (1.5 if GTK else 8, lambda: activate(xwin(win) if GTK else blink_window())),
    (0.7, lambda: (send({"IsReady": True}), send({"PasteText": {"payload": {"pasteId": "p1", "text": TEXT}}}))),
    (2.0, user_edit),
    (1.5, end_observation),
    (1.5, None),
]
st = {"i": 0, "t": time.time(), "buf": ""}


def tick():
    try:
        return step()
    except Exception as e:  # noqa: BLE001  (an exception in a GLib callback would hang the main loop)
        print(f"FAIL {CASE}: {e!r}")
        Gtk.main_quit()
        return False


def step():
    try:
        st["buf"] += parent.recv(65536).decode()
    except BlockingIOError:
        pass
    wait, action = steps[st["i"]]
    if time.time() - st["t"] < wait:
        return True
    if action:
        action(); st["i"] += 1; st["t"] = time.time()
        return True
    msgs = [json.loads(m.replace("+2", "|").replace("+1", "+")) for m in st["buf"].split("|") if m]
    upd = [m["HelperAPIRequest"]["UpdateEditedText"]["payload"] for m in msgs
           if "UpdateEditedText" in m.get("HelperAPIRequest", {})]
    ok = len(upd) == 1 and upd[0]["editedText"].strip() == WANT and upd[0]["pastedText"].strip() == TEXT
    got = upd[0] if upd else "no UpdateEditedText"
    print(f"{'PASS' if ok else 'FAIL'} {CASE}: {json.dumps(got)}")
    proc.stdin.close(); proc.wait(timeout=3)
    if not GTK:
        ff.terminate()
    st["ok"] = ok
    Gtk.main_quit()
    return False


GLib.timeout_add(100, tick)
Gtk.main()
sys.exit(0 if st.get("ok") else 1)
