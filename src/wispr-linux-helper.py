#!/usr/bin/env python3
"""Linux stand-in for Wispr Flow's Windows "Wispr Flow Helper.exe".

Protocol (taken from the Electron main bundle):
  * Electron -> helper: stdin.  Helper -> Electron: fd 3.
  * Each message is JSON, `+` escaped as `+1`, `|` escaped as `+2`, terminated by `|`.
  * {"HelperAPIRequest": {"uuid": ..., "<Type>": true | {"payload": ...}}}
  * {"HelperAPIResponse": {"uuid": <request uuid>, "ACK": true, "<Type>": {...}}}

Keys are reported as Windows virtual-key codes, which is what the Windows build expects.
Pasting: the Electron loader puts the text on the clipboard; this helper presses Ctrl+V
(Ctrl+Shift+V in terminals) through XTEST.
Edit detection: after a paste, the user's corrections are reported with UpdateEditedText, which
feeds the app's dictionary learning. The text box is read over AT-SPI when the app exposes it;
otherwise (Chromium/Electron apps) the text is rebuilt from keystrokes, mouse clicks and the
X PRIMARY selection.
"""
import difflib
import json
import queue
import os
import platform
import signal
import sys
import threading
import time
import uuid as uuidlib

from Xlib import X, XK, display
from Xlib.ext import record, xtest, xfixes
from Xlib.protocol import rq

START_MS = time.time() * 1000
LOG_PATH = os.environ.get("WISPR_HELPER_LOG") or os.path.expanduser("~/.cache/wispr-linux/helper.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
_log_file = open(LOG_PATH, "a", buffering=1)


def log(*parts):
    _log_file.write(time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts) + "\n")


# ---------------------------------------------------------------- transport
_out = os.fdopen(3, "wb", buffering=0)
_out_lock = threading.Lock()


def _send(obj):
    data = json.dumps(obj, separators=(",", ":"))
    data = data.replace("+", "+1").replace("|", "+2") + "|"
    with _out_lock:
        _out.write(data.encode("utf-8"))


def respond(req_uuid, **fields):
    _send({"HelperAPIResponse": {"uuid": req_uuid, "ACK": True, **fields}})


def request(kind, payload=None):
    _send({"HelperAPIRequest": {"uuid": str(uuidlib.uuid4()), kind: True if payload is None else {"payload": payload}}})


# ---------------------------------------------------------------- key mapping (X keysym -> Windows VK)
VK_BY_KEYSYM_NAME = {
    "Control_L": 162, "Control_R": 163, "Shift_L": 160, "Shift_R": 161,
    "Alt_L": 164, "Alt_R": 165, "ISO_Level3_Shift": 165, "Meta_L": 164, "Meta_R": 165,
    "Super_L": 91, "Super_R": 92, "Hyper_L": 91, "Hyper_R": 92, "Menu": 93,
    "Caps_Lock": 20, "Escape": 27, "Return": 13, "KP_Enter": 13, "Tab": 9, "ISO_Left_Tab": 9,
    "BackSpace": 8, "Delete": 46, "Insert": 45, "space": 32, "Print": 44,
    "Home": 36, "End": 35, "Prior": 33, "Next": 34,
    "Left": 37, "Up": 38, "Right": 39, "Down": 40,
    "minus": 189, "equal": 187, "bracketleft": 219, "bracketright": 221, "backslash": 220,
    "semicolon": 186, "apostrophe": 222, "comma": 188, "period": 190, "slash": 191, "grave": 192,
}
for _i in range(1, 25):
    VK_BY_KEYSYM_NAME[f"F{_i}"] = 111 + _i
MODIFIER_VKS = {160, 161, 162, 163, 164, 165, 91, 92}

_keysym_name = {}
for _name in dir(XK):
    if _name.startswith("XK_"):
        _keysym_name.setdefault(getattr(XK, _name), _name[3:])


def vk_for_keycode(disp, keycode):
    keysym = disp.keycode_to_keysym(keycode, 0)
    if not keysym:
        return None
    if ord("a") <= keysym <= ord("z"):
        return keysym - 32
    if ord("0") <= keysym <= ord("9"):
        return keysym
    return VK_BY_KEYSYM_NAME.get(_keysym_name.get(keysym, ""))


# ---------------------------------------------------------------- active window
TERMINAL_CLASSES = {
    "gnome-terminal-server", "gnome-terminal", "x-terminal-emulator", "xterm", "uxterm", "kitty",
    "alacritty", "konsole", "tilix", "terminator", "xfce4-terminal", "mate-terminal", "lxterminal",
    "org.wezfurlong.wezterm", "wezterm", "foot", "urxvt", "st", "ghostty", "com.mitchellh.ghostty",
    "tabby", "warp", "qterminal", "sakura", "guake", "yakuake", "terminology", "ptyxis", "blackbox",
}
# Linux process names -> the Windows exe names the app knows about.
EXE_ALIASES = {
    "chrome": "chrome.exe", "google-chrome": "chrome.exe", "chromium": "chrome.exe",
    "chromium-browser": "chrome.exe", "firefox": "firefox.exe", "firefox-bin": "firefox.exe",
    "brave": "brave.exe", "brave-browser": "brave.exe", "msedge": "msedge.exe", "vivaldi-bin": "vivaldi.exe",
    "opera": "opera.exe", "code": "code.exe", "cursor": "cursor.exe", "slack": "slack.exe",
    "discord": "discord.exe", "zoom": "zoom.exe", "teams-for-linux": "ms-teams.exe",
    "obsidian": "obsidian.exe", "notion-app": "notion.exe", "thunderbird": "thunderbird.exe",
    "zen": "zen.exe", "zen-bin": "zen.exe", "windsurf": "windsurf.exe",
}


class WindowInfo:
    def __init__(self):
        self.d = display.Display()
        self.root = self.d.screen().root
        self.a = {n: self.d.intern_atom(n) for n in ("_NET_ACTIVE_WINDOW", "_NET_WM_NAME", "_NET_WM_PID", "UTF8_STRING", "_NET_CLIENT_LIST")}

    def _prop(self, win, atom, kind=X.AnyPropertyType):
        try:
            p = win.get_full_property(atom, kind)
            return p.value if p else None
        except Exception:
            return None

    def active_wid(self):
        v = self._prop(self.root, self.a["_NET_ACTIVE_WINDOW"])
        return v[0] if v is not None and len(v) and v[0] else None

    def active(self):
        wid = self.active_wid()
        return self.describe(self.d.create_resource_object("window", wid)) if wid else None

    def describe(self, win):
        try:
            cls = win.get_wm_class() or ("", "")
        except Exception:
            cls = ("", "")
        title = self._prop(win, self.a["_NET_WM_NAME"], self.a["UTF8_STRING"])
        title = title.decode("utf-8", "replace") if isinstance(title, bytes) else (title or "")
        pid = self._prop(win, self.a["_NET_WM_PID"])
        exe = ""
        if pid is not None and len(pid):
            try:
                exe = os.path.basename(os.readlink(f"/proc/{pid[0]}/exe"))
            except OSError:
                pass
        instance, wm_class = (cls[0] or "").lower(), (cls[1] or "")
        base = (exe or instance or wm_class.lower() or "unknown")
        bundle = EXE_ALIASES.get(base) or EXE_ALIASES.get(instance) or f"{base}.exe"
        is_terminal = instance in TERMINAL_CLASSES or wm_class.lower() in TERMINAL_CLASSES or base in TERMINAL_CLASSES
        if is_terminal:
            bundle = "windowsterminal.exe"
        return {"appName": wm_class or base, "bundleId": bundle, "windowTitle": title, "terminal": is_terminal,
                "wid": win.id, "pid": int(pid[0]) if pid is not None and len(pid) else None}

    def running_apps(self):
        apps, seen = [], set()
        for wid in self._prop(self.root, self.a["_NET_CLIENT_LIST"]) or []:
            info = self.describe(self.d.create_resource_object("window", wid))
            if info["bundleId"] not in seen:
                seen.add(info["bundleId"])
                apps.append({"bundleId": info["bundleId"], "name": info["appName"]})
        return apps


# ---------------------------------------------------------------- input injection
class Injector:
    def __init__(self):
        self.d = display.Display()
        self.lock = threading.Lock()

    def keycode(self, name):
        return self.d.keysym_to_keycode(XK.string_to_keysym(name))

    def held_modifier_vks(self):
        keymap = self.d.query_keymap()
        held = set()
        for byte_i, byte in enumerate(keymap):
            for bit in range(8):
                if byte & (1 << bit):
                    vk = vk_for_keycode(self.d, byte_i * 8 + bit)
                    if vk in MODIFIER_VKS:
                        held.add(vk)
        return held

    def held_vks(self):
        keymap = self.d.query_keymap()
        held = set()
        for byte_i, byte in enumerate(keymap):
            for bit in range(8):
                if byte & (1 << bit):
                    vk = vk_for_keycode(self.d, byte_i * 8 + bit)
                    if vk is not None:
                        held.add(vk)
        return held

    def wait_for_modifiers_released(self, timeout=0.8):
        end = time.time() + timeout
        while time.time() < end:
            if not self.held_modifier_vks():
                return True
            time.sleep(0.02)
        return False

    def chord(self, *names):
        with self.lock:
            codes = [self.keycode(n) for n in names]
            for c in codes:
                xtest.fake_input(self.d, X.KeyPress, c)
            for c in reversed(codes):
                xtest.fake_input(self.d, X.KeyRelease, c)
            self.d.sync()

    def press_vk(self, vk, flags):
        names = {v: k for k, v in VK_BY_KEYSYM_NAME.items() if not k.endswith("_R")}
        key = chr(vk).lower() if (65 <= vk <= 90 or 48 <= vk <= 57) else names.get(vk)
        if not key:
            log("SimulateKeyPress: unknown vk", vk)
            return
        mods = []
        for f in flags or []:
            f = str(f).lower()
            if f in ("control", "ctrl"):
                mods.append("Control_L")
            elif f in ("shift",):
                mods.append("Shift_L")
            elif f in ("option", "alt"):
                mods.append("Alt_L")
            elif f in ("command", "win", "meta"):
                mods.append("Super_L")
        self.chord(*mods, key)


# ---------------------------------------------------------------- key listener (XRecord)
class KeyListener(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.index = 0
        self.down = set()
        self.ctrl_d = display.Display()
        self.data_d = display.Display()
        self.lock = threading.Lock()

    def panic_check(self, vk):
        """Esc pressed 3 times within 1.5 s quits Wispr Flow: an escape hatch if a window ever blocks the screen."""
        if vk != 27:
            return
        now = time.time()
        self.esc_times = [t for t in getattr(self, "esc_times", []) if now - t < 1.5] + [now]
        if len(self.esc_times) >= 3:
            log("panic: triple Esc, quitting Wispr Flow (pid %d)" % os.getppid())
            try:
                os.kill(os.getppid(), signal.SIGTERM)
            finally:
                os._exit(0)

    def emit(self, vk, pressed):
        with self.lock:
            if pressed:
                if vk in self.down:
                    return  # autorepeat
                self.panic_check(vk)
                self.down.add(vk)
            else:
                if vk not in self.down:
                    return
                self.down.discard(vk)
            self.index += 1
            if observer:
                observer.on_key(vk, pressed)
            request("KeypressEvent", {
                "eventType": "key_event_press" if pressed else "key_event_release",
                "index": self.index, "key": vk, "inputType": "keyboard",
            })

    def run(self):
        ctx = self.ctrl_d.record_create_context(0, [record.AllClients], [{
            "core_requests": (0, 0), "core_replies": (0, 0),
            "ext_requests": (0, 0, 0, 0), "ext_replies": (0, 0, 0, 0),
            "delivered_events": (0, 0), "device_events": (X.KeyPress, X.ButtonRelease),
            "errors": (0, 0), "client_started": False, "client_died": False,
        }])
        self.ctrl_d.sync()  # the context must exist server-side before the data connection enables it
        log("XRecord key listener started")
        self.data_d.record_enable_context(ctx, self.on_record)

    def on_record(self, reply):
        if reply.category != record.FromServer or reply.client_swapped or not len(reply.data):
            return
        data = reply.data
        while len(data):
            event, data = rq.EventField(None).parse_binary_value(data, self.ctrl_d.display, None, None)
            if observer and event.type in (X.KeyPress, X.KeyRelease, X.ButtonPress, X.ButtonRelease):
                observer.on_raw(event.type, event.detail)
            if event.type not in (X.KeyPress, X.KeyRelease):
                continue
            vk = vk_for_keycode(self.ctrl_d, event.detail)
            if vk is None:
                continue
            pressed = event.type == X.KeyPress
            if not pressed and vk not in MODIFIER_VKS:
                # Swallow X autorepeat (release immediately followed by press) for normal keys.
                threading.Timer(0.03, self._delayed_release, (vk, event.detail)).start()
            else:
                self.emit(vk, pressed)

    def _delayed_release(self, vk, keycode):
        keymap = injector.d.query_keymap()
        if keymap[keycode // 8] & (1 << (keycode % 8)):
            return  # still held: it was autorepeat
        self.emit(vk, False)


# ---------------------------------------------------------------- edit detection (AT-SPI)
# After each paste, watch the focused text box and report the user's corrections with UpdateEditedText.
# The app diffs pasted vs edited text and offers to add corrected words to the dictionary.
try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib
    ATSPI_ERROR = None
except (ImportError, ValueError) as _e:
    Atspi, ATSPI_ERROR = None, repr(_e)

EMBED = "￼"     # AT-SPI placeholder for an embedded child (contenteditable paragraph, mention chip...)
MAX_TEXT = 20000     # ignore text boxes longer than this
MAX_NODES = 400      # cap on accessibles read per snapshot
BLOCK_ROLE_NAMES = ("PARAGRAPH", "HEADING", "SECTION", "LIST_ITEM", "LIST", "BLOCK_QUOTE", "TABLE_ROW", "FORM")


class TextUnavailable(Exception):
    pass


def common_prefix(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def common_suffix(a, b, limit):
    """Length of the common suffix of a and b, at most `limit`."""
    i = 0
    while i < limit and a[-1 - i] == b[-1 - i]:
        i += 1
    return i


def diff_ops(old, new):
    """difflib opcodes for old -> new, cheap for the usual small edit in a long text."""
    p = common_prefix(old, new)
    q = common_suffix(old, new, min(len(old), len(new)) - p)
    a, b = old[p:len(old) - q], new[p:len(new) - q]
    ops = [("equal", 0, p, 0, p)] if p else []
    if len(a) * len(b) > 4_000_000:
        ops.append(("replace", p, p + len(a), p, p + len(b)))
    else:
        ops += [(t, i1 + p, i2 + p, j1 + p, j2 + p)
                for t, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]
    if q:
        ops.append(("equal", len(old) - q, len(old), len(new) - q, len(new)))
    return ops


def map_region(old, start, end, new):
    """Where the region old[start:end] ended up in new. Edits glued to its first/last word count as inside."""
    ops = diff_ops(old, new)

    def map_pos(pos, is_start):
        for tag, i1, i2, j1, j2 in ops:
            if tag == "insert" and i1 == pos:
                ins = new[j1:j2]
                if is_start:
                    glued = pos < len(old) and not old[pos].isspace() and not ins[-1].isspace()
                    return j1 if glued else j2
                glued = pos > 0 and not old[pos - 1].isspace() and not ins[0].isspace()
                return j2 if glued else j1
        for tag, i1, i2, j1, j2 in ops:
            if tag != "insert" and (i1 <= pos < i2 if is_start else i1 < pos <= i2):
                if tag == "equal":
                    return j1 + (pos - i1)
                return j1 if is_start else j2  # a replaced/deleted span crossing the boundary counts as inside
        return len(new) if is_start else 0

    s, e = map_pos(start, True), map_pos(end, False)
    return (s, e) if e > s else (s, s)


def region_kept(old, start, end, new):
    """How many characters of old[start:end] are still there, unchanged, in new."""
    return sum(max(0, min(i2, end) - max(i1, start)) for tag, i1, i2, _, _ in diff_ops(old, new) if tag == "equal")


def edit_distance(a, b):
    if len(a) > 2000 or len(b) > 2000:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---- keystroke model, for apps whose text boxes AT-SPI can't read (Chromium/Electron without a11y)
def is_word_char(t, i):
    """Word character as Chromium's Ctrl+arrow sees it; an apostrophe between letters (don't) counts."""
    ch = t[i]
    if ch.isalnum() or ch == "_":
        return True
    return ch in "'’" and 0 < i < len(t) - 1 and t[i - 1].isalnum() and t[i + 1].isalnum()


def word_left(t, c):
    """Where Ctrl+Left / Ctrl+Backspace lands from c, or None if that leaves the pasted text or is unclear."""
    i = c
    while i > 0 and t[i - 1].isspace():
        i -= 1
    if i == 0:
        return None
    if is_word_char(t, i - 1):
        while i > 0 and is_word_char(t, i - 1):
            i -= 1
        return i if i > 0 else None  # the word may go on before the pasted text
    if i > 1 and not t[i - 2].isspace() and not is_word_char(t, i - 2):
        return None  # run of punctuation: Chromium's behaviour isn't modelled
    return i - 1


def word_right(t, c):
    """Where Ctrl+Right / Ctrl+Delete lands from c on Linux (end of the next word), or None."""
    n, i = len(t), c
    while i < n and t[i].isspace():
        i += 1
    if i >= n:
        return None
    if is_word_char(t, i):
        while i < n and is_word_char(t, i):
            i += 1
        return i if i < n else None
    if i + 1 < n and not t[i + 1].isspace() and not is_word_char(t, i + 1):
        return None
    return i + 1


def edit_similarity(pasted, edited):
    """Worst normalised character distance over the words that changed: 0 = same, 1 = unrelated.
    Dictionary fixes are near-misses ("shuffle" -> "Shuffull"), so a low score means a believable edit."""
    a, b = pasted.split(), edited.split()
    if a == b:
        return 0.0
    worst = 0.0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = " ".join(a[i1:i2]).lower(), " ".join(b[j1:j2]).lower()
        d = edit_distance(old, new)
        if d is None:
            return 1.0
        # a fix that merges or splits words is less likely than one that changes a word in place
        worst = max(worst, d / max(len(old), len(new), 1) + 0.1 * abs((i2 - i1) - (j2 - j1)))
    return min(worst, 1.0)


class TypedText:
    """The pasted text, rebuilt from what the user types. Positions are relative to the pasted text;
    anything that moves the caret outside it, or that we can't model, ends tracking.

    After a mouse click the caret position is unknown, so every position is kept as a candidate and
    each keystroke is applied to all of them. A selection made with the mouse (read back from the X
    PRIMARY selection) pins the candidates down again. At the end one candidate must clearly win."""
    MAX_CANDIDATES = 6000
    MID_WORD_PENALTY = 0.1   # clicks land at word edges more often than inside words
    MAX_SIMILARITY = 0.5     # after a click, reject results that don't look like a word fix
    MIN_MARGIN = 0.1         # ...and results that don't clearly beat the runner-up

    def __init__(self, text):
        self.pasted = text
        self.cands = {(text, len(text), len(text), 0.0)}  # (text, caret, anchor, penalty)
        self.clicked = False
        self.ref = text  # last text known exactly; uncertain edits are scored against it

    def apply(self, fn):
        """fn(text, caret, anchor) -> (text, caret, anchor) or None. False if no candidate survives."""
        new = set()
        for t, c, a, pen in self.cands:
            r = fn(t, c, a)
            if r is not None:
                new.add((*r, pen))
        if not new:
            return False
        self.cands = new
        return True

    def click(self):
        texts = {c[0] for c in self.cands}
        if len(texts) == 1:
            self.ref = next(iter(texts))
        new = set()
        for t, _, _, _ in self.cands:
            for i in range(len(t) + 1):
                mid = 0 < i < len(t) and is_word_char(t, i - 1) and is_word_char(t, i)
                new.add((t, i, i, self.MID_WORD_PENALTY if mid else 0.0))
        if len(new) > self.MAX_CANDIDATES:
            return False
        self.cands, self.clicked = new, True
        return True

    def select_found(self, sel):
        """The mouse selected `sel`: keep the candidates where it occurs and return how many there are.
        One match means the caret is known exactly again. 0 (not in the pasted text) changes nothing."""
        variants = [sel]
        for v in (sel.replace("\xa0", " "), sel.replace("\xa0", " ").strip()):  # browsers may hand out NBSPs
            if v and v not in variants:
                variants.append(v)
        for v in variants:
            new = set()
            for t in {c[0] for c in self.cands}:
                i = t.find(v)
                while i >= 0:
                    new.add((t, i + len(v), i, 0.0))
                    i = t.find(v, i + 1)
            if new and len(new) <= self.MAX_CANDIDATES:
                # a selection found in exactly one place pins the caret down: no fuzzy check needed later
                self.cands, self.clicked = new, len(new) > 1
                return len(new)
        return 0

    def match_selection(self, sel):
        """A keyboard selection changed PRIMARY: drop candidates whose selection doesn't match it."""
        keep = {x for x in self.cands if x[0][min(x[1], x[2]):max(x[1], x[2])] == sel}
        if keep:
            self.cands = keep

    def result(self):
        """(edited text, None) or (None, why)."""
        best = {}
        for t, _, _, pen in self.cands:
            best[t] = min(pen, best.get(t, 1.0))
        if len(best) == 1 and (not self.clicked or self.ref in best):
            return next(iter(best)), None
        if len(best) > 3000:
            return None, "too many candidates"
        scored = []
        for t, pen in best.items():
            sim = edit_similarity(self.ref, t)
            if sim <= self.MAX_SIMILARITY:
                scored.append((sim + pen, t))
        if not scored:
            return None, "edit doesn't look like a word fix"
        scored.sort()
        if len(scored) > 1 and scored[1][0] - scored[0][0] < self.MIN_MARGIN - 1e-9:
            return None, f"ambiguous ({len(scored)} candidates)"
        return scored[0][1], None


def _sel(c, a):
    return (min(c, a), max(c, a)) if c != a else None


def op_insert(s):
    def fn(t, c, a):
        lo, hi = _sel(c, a) or (c, c)
        return t[:lo] + s + t[hi:], lo + len(s), lo + len(s)
    return fn


def op_delete(forward, word):
    def fn(t, c, a):
        sel = _sel(c, a)
        if sel:
            return t[:sel[0]] + t[sel[1]:], sel[0], sel[0]
        if forward:
            e = word_right(t, c) if word else (c + 1 if c < len(t) else None)
            return None if e is None else (t[:c] + t[e:], c, c)
        s = word_left(t, c) if word else (c - 1 if c > 0 else None)
        return None if s is None else (t[:s] + t[c:], s, s)
    return fn


def op_cut(t, c, a):
    sel = _sel(c, a)
    return (t[:sel[0]] + t[sel[1]:], sel[0], sel[0]) if sel else (t, c, a)


def op_move(forward, word, extend):
    def fn(t, c, a):
        sel = _sel(c, a)
        if sel and not extend and not word:
            p = sel[1] if forward else sel[0]
            return t, p, p
        if word:
            p = word_right(t, c) if forward else word_left(t, c)
        else:
            p = c + 1 if forward else c - 1
            p = p if 0 <= p <= len(t) else None
        if p is None:
            return None
        return t, p, (a if extend else p)
    return fn


KEYPAD_CHARS = {"KP_Add": "+", "KP_Subtract": "-", "KP_Multiply": "*", "KP_Divide": "/", "KP_Decimal": ".",
                "KP_Separator": ",", "KP_Space": " ", "KP_Equal": "="}


def keysym_char(keysym):
    """The character a keysym types, or None."""
    if 0x20 <= keysym <= 0x7e or 0xa0 <= keysym <= 0xff:
        return chr(keysym)
    if 0x1000100 <= keysym <= 0x110ffff:
        return chr(keysym - 0x1000000)
    if 0xffb0 <= keysym <= 0xffb9:
        return chr(ord("0") + keysym - 0xffb0)
    return KEYPAD_CHARS.get(_keysym_name.get(keysym, ""))


MOD_NAMES = {"Shift_L": "shift", "Shift_R": "shift", "Control_L": "ctrl", "Control_R": "ctrl", "Alt_L": "alt",
             "Alt_R": "alt", "Meta_L": "alt", "Meta_R": "alt", "Super_L": "super", "Super_R": "super",
             "Hyper_L": "super", "Hyper_R": "super", "ISO_Level3_Shift": "altgr", "Mode_switch": "altgr"}


class EditObserver(threading.Thread):
    """Watches the text box after a paste. Two modes:
      atspi: read the box over AT-SPI (GTK, Qt, Firefox/Gecko).
      keys:  the box can't be read (Chromium/Electron), so rebuild the text from input events (TypedText).
    Owns every AT-SPI call (libatspi isn't thread-safe); other threads talk to it through a queue."""
    POLL = 0.5             # seconds between snapshots while watching
    AFTER_KEY = 0.15       # snapshot this long after a key, so the last state before Enter/focus loss is fresh
    LANDING_TIMEOUT = 2.0  # give up if the paste never shows up in the text box
    MAX_SECONDS = 600      # stop watching a text box after 10 minutes
    MIN_KEPT_CHECK = 10    # a paste this long with under 10% of it left is a cleared box, not a rewrite
    KEYS_GRACE = 0.25      # ignore input this long after our own Ctrl+V
    CLICK_SELECT = 1.2     # a PRIMARY change this soon after a click is a mouse selection
    MAX_TYPED_TEXT = 5000  # don't keystroke-track longer pastes

    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.watching = False  # atspi observation running (key events wanted)
        self.keys = False      # keys observation running (raw input + selection events wanted)
        self.pending = None
        self.obs = None
        self.block_roles = {getattr(Atspi.Role, n) for n in BLOCK_ROLE_NAMES if hasattr(Atspi.Role, n)} if Atspi else set()
        # Roles AT-SPI reports when it can't see into the app (Chromium without renderer accessibility).
        self.opaque_roles = {getattr(Atspi.Role, n) for n in ("FRAME", "WINDOW", "APPLICATION")
                             if hasattr(Atspi.Role, n)} if Atspi else set()

    # ---- called from other threads
    def before_paste(self, paste_id, text, timeout=0.3):
        done = threading.Event()
        self.q.put(("before", paste_id, text, done))
        done.wait(timeout)

    def after_paste(self, paste_id, ok):
        self.q.put(("after", paste_id, ok))

    def on_key(self, vk, pressed):
        if self.watching:
            self.q.put(("key", vk, pressed))

    def on_raw(self, kind, detail):
        """Every key/button event from XRecord, autorepeats included."""
        if self.keys:
            self.q.put(("raw", kind, detail, time.time()))

    def watch_primary(self):
        try:
            d = display.Display()
            d.xfixes_query_version()
            d.xfixes_select_selection_input(d.screen().root, d.intern_atom("PRIMARY"),
                                            xfixes.XFixesSetSelectionOwnerNotifyMask)
        except Exception as e:  # noqa: BLE001
            log("edit observer: no XFixes, mouse selections won't be followed:", repr(e))
            return
        while True:
            d.next_event()
            if self.keys:
                self.q.put(("primary",))

    # ---- observer thread
    def run(self):
        self.win = WindowInfo()
        self.xd = display.Display()
        self.sel_win = self.xd.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
        threading.Thread(target=self.watch_primary, daemon=True).start()
        log("edit observer started", "(AT-SPI + keystrokes)" if Atspi else "(keystrokes only, no AT-SPI)")
        ctx = GLib.MainContext.default() if Atspi else None
        while True:
            timeout = max(0.0, self.obs["next_poll"] - time.time()) if self.obs else None
            if ctx:  # wake up now and then to let libatspi process its D-Bus messages
                timeout = min(timeout, 2.0) if timeout is not None else 2.0
            try:
                cmd = self.q.get(timeout=timeout)
            except queue.Empty:
                cmd = None
            try:
                if ctx:  # cache updates (focus, windows, apps): unprocessed they go stale and pile up
                    for _ in range(5000):
                        if not ctx.iteration(False):
                            break
                if cmd:
                    getattr(self, "cmd_" + cmd[0])(*cmd[1:])
                if self.obs and time.time() >= self.obs["next_poll"]:
                    self.poll()
            except Exception as e:  # noqa: BLE001
                log("edit observer error", repr(e))
                self.obs, self.pending, self.watching, self.keys = None, None, False, False

    def read_selection(self, name, timeout=0.3):
        """Text of an X selection (PRIMARY = last selected text, CLIPBOARD), or None."""
        d, w = self.xd, self.sel_win
        sel, prop = d.intern_atom(name), d.intern_atom("WISPR_SELECTION")
        w.convert_selection(sel, d.intern_atom("UTF8_STRING"), prop, X.CurrentTime)
        d.flush()
        end = time.time() + timeout
        while time.time() < end:
            while d.pending_events():
                e = d.next_event()
                if e.type != X.SelectionNotify or e.selection != sel:
                    continue
                if e.property == X.NONE:
                    return None
                p = w.get_full_property(prop, X.AnyPropertyType)
                w.delete_property(prop)
                if p is None or p.property_type == d.intern_atom("INCR") or not isinstance(p.value, bytes):
                    return None
                return p.value.decode("utf-8", "replace")
            time.sleep(0.005)
        return None

    def read_text(self, acc, budget=None):
        """Plain text of an accessible, with embedded children (paragraphs, links, mentions) inlined."""
        budget = budget if budget is not None else [MAX_NODES]
        budget[0] -= 1
        if budget[0] < 0:
            raise TextUnavailable("too many nodes")
        n = Atspi.Text.get_character_count(acc)
        if n > MAX_TEXT:
            raise TextUnavailable("too long")
        raw = Atspi.Text.get_text(acc, 0, n) if n > 0 else ""
        if EMBED not in raw:
            return raw
        out, child_i = [], 0
        for offset, ch in enumerate(raw):
            if ch != EMBED:
                out.append(ch)
                continue
            child = None
            try:
                link_i = Atspi.Hypertext.get_link_index(acc, offset)
                if link_i >= 0:
                    child = Atspi.Hyperlink.get_object(Atspi.Hypertext.get_link(acc, link_i), 0)
            except Exception:  # noqa: BLE001
                child = None
            if child is None and child_i < acc.get_child_count():
                child = acc.get_child_at_index(child_i)
            child_i += 1
            if child is None or child.get_text_iface() is None:
                continue
            if child.get_role() in self.block_roles and out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append(self.read_text(child, budget))
        return "".join(out)

    def find_focused(self, pid):
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None or (pid is not None and app.get_process_id() != pid):
                continue
            for w in range(app.get_child_count()):
                win = app.get_child_at_index(w)
                if win is None or (pid is None and not win.get_state_set().contains(Atspi.StateType.ACTIVE)):
                    continue
                acc = self.focused_in(win)
                if acc is not None:
                    return acc
        return None

    def focused_in(self, win):
        rule = Atspi.MatchRule.new(Atspi.StateSet.new([Atspi.StateType.FOCUSED]), Atspi.CollectionMatchType.ALL,
                                   {}, Atspi.CollectionMatchType.ALL, [], Atspi.CollectionMatchType.ALL,
                                   [], Atspi.CollectionMatchType.ALL, False)
        try:
            col = win.get_collection_iface()
            if col is not None:
                found = Atspi.Collection.get_matches(win, rule, Atspi.CollectionSortOrder.CANONICAL, 1, True)
                return found[0] if found else None
        except Exception:  # noqa: BLE001
            pass
        stack, seen = [win], 0  # no Collection interface: bounded depth-first search
        while stack and seen < 3000:
            acc = stack.pop()
            seen += 1
            try:
                if acc.get_state_set().contains(Atspi.StateType.FOCUSED):
                    return acc
                stack.extend(c for c in (acc.get_child_at_index(i) for i in range(acc.get_child_count() - 1, -1, -1)) if c)
            except Exception:  # noqa: BLE001
                continue
        return None

    def cmd_before(self, paste_id, text, done):
        try:
            if self.obs:
                self.finish("new_paste")
            self.pending = None
            info = self.win.active()
            if not info or info.get("terminal"):
                return
            base = {"pasteId": paste_id, "text": text, "wid": info["wid"], "app": info.get("bundleId")}
            acc = self.find_focused(info.get("pid")) if Atspi else None
            base["why"] = "no AT-SPI" if not Atspi else "no focused element" if acc is None else acc.get_role_name()
            if acc is not None:
                role = acc.get_role()
                if acc.get_state_set().contains(Atspi.StateType.EDITABLE) and acc.get_text_iface() is not None \
                        and role != Atspi.Role.TERMINAL:
                    try:
                        self.pending = {**base, "mode": "atspi", "acc": acc, "before": self.read_text(acc)}
                    except TextUnavailable as e:
                        log("edit observer: skipping text box,", e)
                    return
                if role not in self.opaque_roles:
                    log("edit observer: focused element is not an editable text box:", acc.get_role_name())
                    return
            if text.strip() and len(text) <= self.MAX_TYPED_TEXT:
                self.pending = {**base, "mode": "keys"}
        finally:
            done.set()

    def cmd_after(self, paste_id, ok):
        p, self.pending = self.pending, None
        if not ok or not p or p["pasteId"] != paste_id:
            return
        now = time.time()
        if p["mode"] == "atspi":
            self.obs = {**p, "phase": "landing", "started": now, "next_poll": now + 0.1, "landing_last": None}
            self.watching = True
            return
        mask = self.xd.screen().root.query_pointer().mask
        self.obs = {**p, "phase": "watching", "model": TypedText(p["text"]), "started": now,
                    "next_poll": now + self.POLL, "ignore_until": now + self.KEYS_GRACE, "mods": set(),
                    "caps": bool(mask & X.LockMask), "numlock": bool(mask & X.Mod2Mask),
                    "last_button": 0.0, "primary_due": None}
        self.keys = True
        log(f"edit observer: no readable text box ({p['why']}), following keystrokes in", p["app"])

    def cmd_key(self, vk, pressed):
        if not self.obs:
            return
        if pressed and vk == 13:  # Enter: grab the text now, chat apps clear the box right after
            self.obs["next_poll"] = 0
        elif not pressed:
            self.obs["next_poll"] = min(self.obs["next_poll"], time.time() + self.AFTER_KEY)

    # ---- keys mode
    def keysym(self, keycode, index):
        return self.xd.keycode_to_keysym(keycode, index)

    def typed_char(self, keycode, shift):
        o = self.obs
        if _keysym_name.get(self.keysym(keycode, 0), "").startswith("KP_"):
            return keysym_char(self.keysym(keycode, 1 if o["numlock"] != shift else 0))
        base = 4 if "altgr" in o["mods"] else 0
        ks = self.keysym(keycode, base + shift)
        if not ks and shift:  # single-keysym letters: X says shift means upper case
            ks = self.keysym(keycode, base)
            ch = keysym_char(ks) if ks else None
            ch = ch.upper() if ch else None
        else:
            ch = keysym_char(ks) if ks else None
        if ch and o["caps"] and ch.isalpha():
            ch = ch.swapcase()
        return ch

    def edit(self, fn, what):
        if not self.obs["model"].apply(fn):
            self.lost(what + " left the pasted text")

    def lost(self, why):
        log("edit observer: stopped following keystrokes:", why)
        self.finish("keystroke_tracking_lost")

    def cmd_raw(self, kind, detail, t):
        o = self.obs
        if not o or o["mode"] != "keys" or t < o["ignore_until"]:
            return
        if kind in (X.ButtonPress, X.ButtonRelease):
            if detail not in (1, 2, 3):
                return  # scroll wheel
            o["last_button"] = t
            if kind == X.ButtonRelease:
                return
            if detail != 1:  # middle-click paste, right-click menu
                return self.lost(f"mouse button {detail}")
            if self.win.active_wid() != o["wid"]:
                return self.finish("focus_changed")
            if not o["model"].click():
                self.lost("click (text too long to guess the caret)")
            return
        name = _keysym_name.get(self.keysym(detail, 0), "")
        if kind == X.KeyRelease:
            if name in MOD_NAMES:
                o["mods"].discard(MOD_NAMES[name])
            return
        if name in MOD_NAMES:
            o["mods"].add(MOD_NAMES[name])
            return
        if self.win.active_wid() != o["wid"]:
            return self.finish("focus_changed")
        if name in ("Caps_Lock", "Num_Lock"):
            o["caps" if name == "Caps_Lock" else "numlock"] ^= True
            return
        mods = o["mods"]
        shift, ctrl = "shift" in mods, "ctrl" in mods
        if "alt" in mods or "super" in mods:
            return self.lost(f"shortcut {sorted(mods)}+{name}")
        if name in ("Return", "KP_Enter"):
            if shift and not ctrl:
                return self.edit(op_insert("\n"), "Shift+Enter")
            return self.finish("submitted")  # chat apps send on Enter; editors just get a new line
        if name in ("Tab", "ISO_Left_Tab"):
            return self.finish("focus_changed")
        if name == "Escape":
            return
        if name in ("BackSpace", "Delete"):
            return self.edit(op_delete(name == "Delete", ctrl), name)
        if name in ("Left", "Right"):
            return self.edit(op_move(name == "Right", ctrl, shift), name)
        if (ctrl and name in ("v", "V")) or (shift and not ctrl and name == "Insert"):
            text = self.read_selection("CLIPBOARD")
            return self.edit(op_insert(text.replace("\r\n", "\n")), "paste") if text else self.lost("paste")
        if ctrl:
            if name in ("c", "C", "b", "B", "i", "I", "u", "U", "Insert"):
                return  # copy, bold, italic, underline: text unchanged
            if name in ("x", "X"):
                return self.edit(op_cut, "cut")
            return self.lost("Ctrl+" + name)  # select all, undo, line/page moves...
        ch = self.typed_char(detail, shift)
        if ch is None:
            return self.lost("key " + (name or str(detail)))
        self.edit(op_insert(ch), "typing")

    def cmd_primary(self):
        o = self.obs
        if o and o["mode"] == "keys":
            # read it a moment later, once the click that caused it has been processed too
            o["primary_due"] = time.time() + 0.08
            o["next_poll"] = min(o["next_poll"], o["primary_due"])

    def read_primary(self):
        o = self.obs
        sel = self.read_selection("PRIMARY")
        since = time.time() - o["last_button"]
        if not sel:
            log(f"edit observer: selection changed {since:.2f}s after a click but couldn't be read")
            return
        if since < self.CLICK_SELECT:
            n = o["model"].select_found(sel)
            # lengths only, never the text
            log(f"edit observer: mouse selection of {len(sel)} chars {since:.2f}s after a click,",
                f"found {n}x in the pasted text" if n else "not in the pasted text, ignored")
        elif len(o["model"].cands) > 1:
            o["model"].match_selection(sel)

    def poll(self):
        o, now = self.obs, time.time()
        o["next_poll"] = now + self.POLL
        if o["mode"] == "keys":
            if o["primary_due"] and now >= o["primary_due"]:
                o["primary_due"] = None
                self.read_primary()
                if not self.obs:
                    return
            if self.win.active_wid() != o["wid"]:
                return self.finish("focus_changed")
            if now - o["started"] > self.MAX_SECONDS:
                self.finish("timeout")
            return
        if self.win.active_wid() != o["wid"]:
            return self.finish("focus_changed")
        try:
            if not o["acc"].get_state_set().contains(Atspi.StateType.FOCUSED):
                return self.finish("focus_changed")
            text = self.read_text(o["acc"])
        except Exception:  # noqa: BLE001
            return self.finish("element_gone")

        if o["phase"] == "landing":
            if text == o["before"] or text != o["landing_last"]:  # not there yet, or still changing
                o["landing_last"] = text
                if now - o["started"] > self.LANDING_TIMEOUT:
                    log("edit observer: paste never showed up in", o["app"])
                    self.obs, self.watching = None, False
                    return
                o["next_poll"] = now + 0.1
                return
            p = common_prefix(o["before"], text)
            q = common_suffix(o["before"], text, min(len(o["before"]), len(text)) - p)
            o.update(phase="watching", base=text, start=p, end=len(text) - q, last=text)
            log("edit observer: watching", o["app"], f"{o['end'] - o['start']} chars")
            return

        if text != o["last"]:
            s, e = map_region(o["base"], o["start"], o["end"], text)
            n = o["end"] - o["start"]
            if e > s and n >= self.MIN_KEPT_CHECK and region_kept(o["base"], o["start"], o["end"], text) < max(3, n // 10):
                # Almost none of a longer paste is left: the box was cleared or its editor rebuilt its nodes
                # (Zen briefly reads back one character). That isn't an edit; keep the last real state.
                log("edit observer: box now reads", len(text), "chars with almost none of the paste left, treating it as cleared")
                e = s
            if e == s:
                ls, le = map_region(o["base"], o["start"], o["end"], o["last"])
                # Emptied in one go (message sent, box cleared): report the last state we saw.
                return self.finish("submitted" if le - ls >= 3 else "transcript_region_removed",
                                   removed=le - ls < 3)
            o["last"] = text
        if now - o["started"] > self.MAX_SECONDS:
            self.finish("timeout")

    def finish(self, reason, removed=False):
        o, self.obs, self.watching, self.keys = self.obs, None, False, False
        if not o or o["phase"] != "watching":
            return
        if o["mode"] == "keys":
            pasted = o["text"]
            edited, why = o["model"].result()
            if edited is None:
                log("edit observer: done,", reason, o["app"], "but not reported:", why)
                return
        elif removed:
            pasted, edited = o["base"][o["start"]:o["end"]], ""
        else:
            pasted = o["base"][o["start"]:o["end"]]
            s, e = map_region(o["base"], o["start"], o["end"], o["last"])
            edited = o["last"][s:e]
        payload = {"pasteId": o["pasteId"], "pastedText": pasted, "editedText": edited,
                   "contentObservationEndReason": reason}
        dist = edit_distance(pasted, edited)
        if dist is not None:
            payload["editDistanceToDictated"] = dist
        request("UpdateEditedText", payload)
        log("edit observer: done,", reason, o["app"], f"edit distance {dist}")


# ---------------------------------------------------------------- request handling
win_info = None
injector = None
listener = None
observer = None


def handle_paste(req_uuid, payload):
    respond(req_uuid)
    t0 = time.time()
    try:
        injector.wait_for_modifiers_released()
        info = win_info.active() or {}
        if observer:
            observer.before_paste(payload.get("pasteId"), payload.get("text", ""))
        time.sleep(0.04)  # let Electron finish taking the clipboard selection
        if info.get("terminal"):
            injector.chord("Control_L", "Shift_L", "v")
        else:
            injector.chord("Control_L", "v")
        if observer:
            observer.after_paste(payload.get("pasteId"), True)
        request("PasteOutcome", {"pasteId": payload.get("pasteId"), "success": True,
                                 "timeElapsedMs": (time.time() - t0) * 1000})
        log("paste ok", info.get("bundleId"), f"{len(payload.get('text', ''))} chars")
    except Exception as e:  # noqa: BLE001
        log("paste failed", repr(e))
        request("PasteOutcome", {"pasteId": payload.get("pasteId"), "success": False, "reason": "internal",
                                 "timeElapsedMs": (time.time() - t0) * 1000})


def app_payload(full=True):
    info = win_info.active() or {"appName": "Unknown", "bundleId": "unknown.exe", "windowTitle": ""}
    p = {"appName": info["appName"], "bundleId": info["bundleId"], "url": ""}
    if full:
        p["windowTitle"] = info.get("windowTitle", "")
    return p


def handle(req):
    rid = req.get("uuid")
    kind = next((k for k in req if k not in ("uuid", "sentryTrace", "sentryBaggage")), None)
    body = req.get(kind)
    payload = body.get("payload", {}) if isinstance(body, dict) else {}
    if kind not in ("IsReady", "CheckStaleKeys"):
        log("<-", kind)
    if kind == "IsReady":
        respond(rid, HelperLaunchTiming={"helperMainEntryMs": START_MS}, TapState={"payload": {"tapEnabled": True}})
    elif kind == "PasteText":
        threading.Thread(target=handle_paste, args=(rid, payload), daemon=True).start()
    elif kind == "GetAccessibilityStatus":
        respond(rid, AccessibilityStatus={"payload": {"status": True}})
    elif kind == "GetActiveAppInfo":
        respond(rid, ActiveAppInfo={"payload": app_payload()})
    elif kind == "GetAppInfo":
        respond(rid, AppInfo={"payload": app_payload(full=False)})
    elif kind == "GetRunningApps":
        respond(rid, RunningApps={"payload": {"apps": win_info.running_apps()}})
    elif kind == "GetMicrophonePrivacyStatus":
        respond(rid, MicrophonePrivacyStatus={"payload": {"desktopAppAccessEnabled": True, "microphoneAccessEnabled": True}})
    elif kind == "GetSystemAudioStatus":
        respond(rid, SystemAudioStatus={"payload": {"authorized": False, "determined": True}})
    elif kind == "RequestSystemAudioPermission":
        respond(rid, SystemAudioPromptResult={"payload": "unavailable"})
    elif kind == "GetDeviceInfo":
        respond(rid, DeviceInfo={"payload": {"status": "ok", "osName": "Linux", "osVersion": platform.release(),
                                             "manufacturer": "Linux", "model": platform.node()}})
    elif kind == "GetHardwareInfo":
        respond(rid, HardwareInfo={"payload": {"connectedMice": [], "hasAppleFnKey": False, "isClamshellMode": False}})
    elif kind == "GetTextBoxInfo":
        respond(rid, TextBoxInfo={"payload": {"accessibilityIsFunctioning": True, "afterText": "", "beforeText": "",
                                              "contents": "", "couldNotGetTextBoxInfo": True, "isEditable": True,
                                              "selectedText": ""}})
    elif kind == "GetSelectedTextViaCopy":
        respond(rid, SelectedTextViaCopy={"payload": {"afterText": "", "beforeText": "", "contents": "", "selectedText": ""}})
    elif kind == "GetDictatedTextPosition":
        respond(rid, DictatedTextPosition={"payload": {}})
    elif kind == "GetMicrophoneHolders":
        respond(rid, MicrophoneHolders={"payload": {"detectionMethod": "unsupported", "deviceInUse": False, "holders": []}})
    elif kind == "SetMicrophoneHoldersMonitor":
        respond(rid, MicrophoneHoldersMonitorStatus={"payload": {"subscribed": False, "supported": False}})
    elif kind == "SetTabStripMonitor":
        respond(rid, TabStripMonitorStatus={"payload": {"subscribed": False, "supported": False}})
    elif kind == "SetNativeDropWitness":
        respond(rid, NativeDropWitnessStatus={"payload": {"accepted": False, "supported": False}})
    elif kind == "GetSlackHuddleState":
        respond(rid, SlackHuddleState={"payload": {"elapsedMs": 0, "reason": "no_app", "verdict": "unknown"}})
    elif kind == "GetSystemSettingsWindowBounds":
        respond(rid, SystemSettingsWindowBounds={"payload": {"found": False, "height": 0, "width": 0, "x": 0, "y": 0}})
    elif kind == "GetConferenceEndState":
        respond(rid, ConferenceEndState={"payload": {"controls": [], "elapsedMs": 0, "status": "unsupported"}})
    elif kind == "GetMeetingTabScan":
        respond(rid, MeetingTabScan={"payload": {"degraded": True, "scannedAgoMs": 0, "status": "unsupported",
                                                 "tabs": [], "uncoveredBrowserBundleIds": []}})
    elif kind == "GetNativeCallSnapshot":
        respond(rid, NativeCallSnapshot={"payload": {"apps": [], "elapsedMs": 0, "status": "unsupported"}})
    elif kind == "BrowserLeaveProof":
        respond(rid, BrowserLeaveProofStatus={"payload": {"accepted": False, "generation": int(payload.get("generation", 0) or 0),
                                                          "reason": "unsupported", "state": "unavailable",
                                                          "supported": False, "version": 0}})
    elif kind == "PositionBrowserWindow":
        respond(rid, BrowserWindowPositioned={"payload": {"browserAnchored": False}})
    elif kind == "DisableGranolaVoiceTyping":
        respond(rid, GranolaVoiceTypingResult="unavailable")
    elif kind == "CheckStaleKeys":
        held = injector.held_vks()
        stale = [k for k in payload.get("keycodes", []) if k not in held]
        with listener.lock:
            listener.down -= set(stale)
        respond(rid, StaleKeysResponse={"payload": {"staleKeys": stale}})
    elif kind == "SimulateKeyPress":
        respond(rid)
        threading.Thread(target=injector.press_vk, args=(payload.get("keycode"), payload.get("flags")), daemon=True).start()
    elif kind == "HelperAppShutdown":
        respond(rid)
        log("shutdown requested")
        os._exit(0)
    else:
        respond(rid)


def handle_message(raw):
    raw = raw.replace("+2", "|").replace("+1", "+")
    try:
        msg = json.loads(raw)
    except ValueError:
        log("bad json", raw[:200])
        return
    if "HelperAPIRequest" in msg:
        try:
            handle(msg["HelperAPIRequest"])
        except Exception as e:  # noqa: BLE001
            log("handler error", repr(e))
            respond(msg["HelperAPIRequest"].get("uuid"))
    # Responses to our own requests (ACKs) need no action.


def watch_active_window():
    last = None
    d = WindowInfo()
    while True:
        try:
            info = d.active()
            key = info and (info["bundleId"], info["windowTitle"])
            if info and key != last:
                last = key
                request("AppInfoUpdate", {"appName": info["appName"], "bundleId": info["bundleId"], "url": "",
                                          "windowTitle": info["windowTitle"]})
        except Exception as e:  # noqa: BLE001
            log("window watch error", repr(e))
        time.sleep(0.4)


def main():
    global win_info, injector, listener, observer
    log("helper start", os.getpid(), "DISPLAY", os.environ.get("DISPLAY"))
    win_info = WindowInfo()
    injector = Injector()
    listener = KeyListener()
    listener.start()
    if Atspi is None:
        log("AT-SPI bindings missing (python3-gi, gir1.2-atspi-2.0), edits are followed by keystrokes only:",
            ATSPI_ERROR)
    observer = EditObserver()
    observer.start()
    threading.Thread(target=watch_active_window, daemon=True).start()
    buf = ""
    stdin = sys.stdin.buffer
    while True:
        chunk = os.read(stdin.fileno(), 65536)
        if not chunk:
            log("stdin closed, exiting")
            os._exit(0)
        buf += chunk.decode("utf-8", "replace")
        while "|" in buf:
            raw, buf = buf.split("|", 1)
            if raw:
                handle_message(raw)


if __name__ == "__main__":
    main()
