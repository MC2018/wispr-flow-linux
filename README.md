# Wispr Flow on Linux

Runs the official **Wispr Flow** desktop app natively on Linux (X11).

Wispr Flow ships for macOS and Windows only. The Windows build is an Electron app plus one native
program, `Wispr Flow Helper.exe`, that does global hotkeys, pasting and active-app detection. This
project runs the official Windows JavaScript on Linux Electron and replaces the helper with a Linux one
that speaks the same protocol.

> Unofficial and unaffiliated with Wispr AI. This repo contains no Wispr code or binaries:
> `install.sh` downloads the official Windows release from Wispr's servers on your machine.

## Requirements

- Linux on **X11** (tested on Linux Mint 22 / Cinnamon). Wayland is not supported.
- `node`/`npm`, `7z` (`p7zip-full`), `curl`, `python3`, `python3-xlib`, `python3-pil`

## Install / update

```bash
./install.sh          # installs, or updates to the latest Wispr release
./install.sh --force  # rebuild even if already on the latest version
```

Then start **Wispr Flow** from the app menu (or `./run.sh`) and sign in.
Default shortcuts are the Windows ones: hold **Ctrl+Win** to dictate, **Ctrl+Win+Space** for hands-free.

**Escape hatch:** press **Esc three times** quickly to quit Wispr Flow if a window ever gets stuck.

## How it works

| Piece | What it does |
| --- | --- |
| `install.sh` | Downloads the latest Windows installer (checks its SHA-256), extracts it, fetches the matching Linux Electron and a Linux `sqlite3` build, assembles `wispr-flow/`, and registers the menu entry and `wispr-flow://` sign-in links. |
| `src/loader.js` | Electron entry point. Patches the main bundle in memory, swaps in the Linux helper, and puts dictated text on the clipboard. |
| `src/wispr-linux-helper.py` | The Linux helper. Hotkeys via XRecord, Ctrl+V (Ctrl+Shift+V in terminals) via XTEST, active window via EWMH. |
| `src/Update.exe` | Stand-in for Squirrel's `Update.exe`; turns "Launch at login" into an XDG autostart entry. |
| `tools/` | Debugging and test scripts (see below). |

### Bundle patches (`src/loader.js`)

Each patch is a literal string replacement in the minified main bundle. If one stops matching after a Wispr
update, `~/.cache/wispr-linux/loader.log` says `PATCH NOT APPLIED`.

1. **Treat Linux as Windows.** The Windows code paths (shortcuts, deep links, helper IPC) fit Linux;
   the macOS ones (fn key, Accessibility, `/Applications` check) do not.
2. **Hub window focusable.** On X11 a non-focusable Electron window becomes override-redirect: the window
   manager can't move, raise or close it, and it blocks the desktop.
3. **Status overlay shape never empty.** An empty window shape makes Cinnamon's compositor stop repainting
   under the overlay, leaving a rectangle of stale pixels.

### Helper protocol

- Electron → helper on **stdin**; helper → Electron on **fd 3**.
- Messages are JSON, with `+` escaped as `+1` and `|` as `+2`, each terminated by `|`.
- `{"HelperAPIRequest": {"uuid": "...", "<Type>": true | {"payload": {...}}}}` and
  `{"HelperAPIResponse": {"uuid": "<request uuid>", "ACK": true, "<Type>": {...}}}`.
- Keys are reported as **Windows virtual-key codes** (`KeypressEvent`), which the app matches against shortcuts.
- `node tools/dump-schema.js <bundle>/index.js schema.json` prints every message type.

## Not supported yet

- Reading the focused text box (context-aware formatting), "edit selected text" commands.
- Meeting notes with system audio. Clicking the floating status pill.
- Auto-update inside the app: rerun `./install.sh` instead.

## Logs

- `~/.cache/wispr-linux/loader.log`: patches, spawned Windows-only commands
- `~/.cache/wispr-linux/helper.log`: helper message types (never dictated text)
- App log: `./run.sh --enable-logging=stderr`

## Tools

- `tools/test_paste.py`: pastes into a throwaway window through the helper and checks the result.
- `tools/test_overlay.py`: checks the status overlay is truly see-through.
- `tools/inspect_windows.py`: lists Wispr windows with override-redirect and input-shape info.
- `tools/shot.py`, `tools/ctx.py`: screenshots; context search in minified bundles.

## Uninstall

```bash
rm -rf wispr-flow build ~/.config/"Wispr Flow" ~/.cache/wispr-linux
rm -f ~/.local/share/applications/wispr-flow.desktop ~/.config/autostart/wispr-flow.desktop \
      ~/.local/share/icons/hicolor/256x256/apps/wispr-flow.png
```
