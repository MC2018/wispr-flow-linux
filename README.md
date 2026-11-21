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
- Optional, for dictionary suggestions in GTK/Qt/Firefox apps: `python3-gi` and `gir1.2-atspi-2.0`
  (installed by default on Mint)

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
| `src/wispr-linux-helper.py` | The Linux helper. Hotkeys via XRecord, Ctrl+V (Ctrl+Shift+V in terminals) via XTEST, active window via EWMH, edit detection via AT-SPI or keystrokes. |
| `src/pointer-watch.py` | Reports the live mouse position to `loader.js` (see "Clicking the status pill"). |
| `src/windows-preload.js` | Runs one of Wispr's page preloads with that page's Windows flag on. |
| `src/status-preload.js`, `src/hub-preload.js` | Preloads for the status overlay and the hub, using `windows-preload.js`. |
| `src/Update.exe` | Stand-in for Squirrel's `Update.exe`; turns "Launch at login" into an XDG autostart entry. |
| `tools/` | Debugging and test scripts (see below). |

### Bundle patches (`src/loader.js`)

Each patch is a literal string or regex replacement in the minified main bundle. Regexes cover spots where
the minifier's variable names change between releases. If one stops matching after a Wispr update, `~/.cache/wispr-linux/loader.log` says `PATCH NOT APPLIED`.

1. **Treat Linux as Windows.** The Windows code paths (shortcuts, deep links, helper IPC) fit Linux;
   the macOS ones (fn key, Accessibility, `/Applications` check) do not.
2. **Hub window focusable.** On X11 a non-focusable Electron window becomes override-redirect: the window
   manager can't move, raise or close it, and it blocks the desktop.
3. **Status overlay shape never empty.** An empty window shape makes Cinnamon's compositor stop repainting
   under the overlay, leaving a rectangle of stale pixels.
4. **Status hover poll 50 ms.** The overlay's hover poll runs every 400 ms. Windows also wakes it on mouse
   moves; Linux has only the poll, so quick clicks were lost.
5. **Status preload.** Points the status overlay at `status-preload.js` (see below).
6. **Hub preload.** Points the hub at `hub-preload.js`. The hub has no window frame and draws its own title
   bar. Its page adds the minimize, maximize and close buttons only when its own `isWindows` flag is on.

### Clicking the status pill and its pop-ups

The status overlay (pill, tips, usage-limit pop-ups) is click-through. The app turns clicks on only while
its poll sees the cursor over a button. Three Linux gaps broke that, and `loader.js` fixes each one:

- `screen.getCursorScreenPoint()` stays frozen on X11 while the pointer is over other apps, so the poll never
  saw the cursor arrive. `loader.js` serves the live position from `pointer-watch.py` instead.
- Electron ignores `setIgnoreMouseEvents(true, {forward: true})` on Linux, so the page never heard the mouse
  leave and hover states stuck. `loader.js` forwards mouse moves to such windows itself.
- The overlay page has its own `isWindows` flag. Only a Windows page reports its tooltips and shadows as part of
  the window shape; otherwise they are cut off. `status-preload.js` turns that flag on.

The overlay also clips itself to the shape of its visible parts. Its hover animation changes that shape every
frame, and on Cinnamon each change flashes white boxes and drops frames, so the pill opened in chunks.
`loader.js` pads the shape by 64 px so the animation and tooltip fit inside it. It grows the shape at once
and shrinks it only after 400 ms without changes.

### Helper protocol

- Electron → helper on **stdin**; helper → Electron on **fd 3**.
- Messages are JSON, with `+` escaped as `+1` and `|` as `+2`, each terminated by `|`.
- `{"HelperAPIRequest": {"uuid": "...", "<Type>": true | {"payload": {...}}}}` and
  `{"HelperAPIResponse": {"uuid": "<request uuid>", "ACK": true, "<Type>": {...}}}`.
- Keys are reported as **Windows virtual-key codes** (`KeypressEvent`), which the app matches against shortcuts.
- `node tools/dump-schema.js <bundle>/index.js schema.json` prints every message type.
- On `DictationStart` the helper sends an `AppInfoUpdate` for the focused window. The app clears its app info
  when a dictation starts and only takes it from an update sent during the dictation. Without it, the style for
  the app (Personal messages, Work, Email, Other) is never applied.

### Dictionary suggestions (edit detection)

When you fix a word Wispr got wrong, the app offers to add it to your dictionary. On Windows the helper
watches the text box after each paste and reports your edits (`UpdateEditedText`). The Linux helper does
the same in one of two ways.

**Reading the text box (AT-SPI, the Linux accessibility API).** Firefox, Zen and other Gecko browsers,
GTK and Qt apps.

1. Before pasting, it reads the focused text box. After pasting, it finds where the dictated text landed.
2. It re-reads the box every 0.5 s, and 0.15 s after each key.
3. It stops when the box loses focus, its window changes, it gets emptied (a chat message was sent),
   another paste starts, or after 10 minutes. Then it sends the dictated text and your edited version.

**Rebuilding the text from your input.** Electron and Chromium apps (Slack, Discord, VS Code, Chrome)
expose no text boxes over AT-SPI unless started with `--force-renderer-accessibility`, so the helper starts
from the dictated text and replays what you do to it:

- Typing, Backspace/Delete, arrows, Ctrl+arrows/Backspace/Delete (word steps), Shift selections,
  Ctrl+X and Ctrl+V are modelled.
- **Double-click or drag over a word:** the helper reads the selected text from the X PRIMARY selection
  and finds it in the dictated text, so it knows exactly what you replaced.
- **Single click:** the caret position is unknown, so every position is tried. When you finish, the
  result must look like a word fix (close in spelling to a word you had) and clearly beat the other
  positions, or nothing is reported.
- It stops on Enter (sent), Tab, another window, keys it can't follow (Home/End, Up/Down, Ctrl+A,
  Ctrl+Z, shortcuts), another paste, or after 10 minutes. Shift+Enter adds a new line.

The app does the rest: it diffs the two texts, asks Wispr's server which changed words are worth learning,
and shows the notification. The app's auto-learn words setting (on by default) must stay on.
Terminals are skipped.

## Not supported yet

- Sending the focused text box to the app for context-aware formatting (`GetTextBoxInfo`), "edit selected text" commands.
- Meeting notes with system audio.
- Auto-update inside the app: rerun `./install.sh` instead.

## Logs

- `~/.cache/wispr-linux/loader.log`: patches, spawned Windows-only commands
- `~/.cache/wispr-linux/helper.log`: helper message types (never dictated text)
- App log: `./run.sh --enable-logging=stderr`

## Tools

- `tools/test_paste.py`: pastes into a throwaway window through the helper and checks the result.
- `tools/test_edits.py`: edit detection end to end (GTK, Firefox, Chrome, optionally Electron) on a nested Xephyr display, so it never types into your windows.
- `tools/test_overlay.py`: checks the status overlay is truly see-through.
- `tools/inspect_windows.py`: lists Wispr windows with override-redirect and input-shape info.
- `tools/shot.py`, `tools/ctx.py`: screenshots; context search in minified bundles.

## Uninstall

```bash
rm -rf wispr-flow build ~/.config/"Wispr Flow" ~/.cache/wispr-linux
rm -f ~/.local/share/applications/wispr-flow.desktop ~/.config/autostart/wispr-flow.desktop \
      ~/.local/share/icons/hicolor/256x256/apps/wispr-flow.png
```
