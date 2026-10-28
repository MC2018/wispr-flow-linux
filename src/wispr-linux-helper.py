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
"""
import json
import os
import platform
import signal
import sys
import threading
import time
import uuid as uuidlib

from Xlib import X, XK, display
from Xlib.ext import record, xtest
from Xlib.protocol import rq

START_MS = time.time() * 1000
LOG_PATH = os.path.expanduser("~/.cache/wispr-linux/helper.log")
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

    def active(self):
        v = self._prop(self.root, self.a["_NET_ACTIVE_WINDOW"])
        if v is None or not len(v) or not v[0]:
            return None
        return self.describe(self.d.create_resource_object("window", v[0]))

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
        return {"appName": wm_class or base, "bundleId": bundle, "windowTitle": title, "terminal": is_terminal}

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
            request("KeypressEvent", {
                "eventType": "key_event_press" if pressed else "key_event_release",
                "index": self.index, "key": vk, "inputType": "keyboard",
            })

    def run(self):
        ctx = self.ctrl_d.record_create_context(0, [record.AllClients], [{
            "core_requests": (0, 0), "core_replies": (0, 0),
            "ext_requests": (0, 0, 0, 0), "ext_replies": (0, 0, 0, 0),
            "delivered_events": (0, 0), "device_events": (X.KeyPress, X.KeyRelease),
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


# ---------------------------------------------------------------- request handling
win_info = None
injector = None
listener = None


def handle_paste(req_uuid, payload):
    respond(req_uuid)
    t0 = time.time()
    try:
        injector.wait_for_modifiers_released()
        info = win_info.active() or {}
        time.sleep(0.04)  # let Electron finish taking the clipboard selection
        if info.get("terminal"):
            injector.chord("Control_L", "Shift_L", "v")
        else:
            injector.chord("Control_L", "v")
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
    global win_info, injector, listener
    log("helper start", os.getpid(), "DISPLAY", os.environ.get("DISPLAY"))
    win_info = WindowInfo()
    injector = Injector()
    listener = KeyListener()
    listener.start()
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
