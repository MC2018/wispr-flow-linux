#!/bin/bash
# Build (or update) the Linux version of Wispr Flow from the official Windows release.
#   ./install.sh          install / update to the latest release
#   ./install.sh --force  rebuild even if already up to date
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/src"
BUILD="$ROOT/build"
APP="$ROOT/wispr-flow"
FORCE="${1:-}"

need() { command -v "$1" >/dev/null || { echo "Missing: $1 ($2)"; exit 1; }; }
need 7z "sudo apt install p7zip-full"
need npm "Node.js"
need curl "sudo apt install curl"
python3 -c 'import Xlib' 2>/dev/null || { echo "Missing python3-xlib (sudo apt install python3-xlib)"; exit 1; }

echo "==> Checking latest Windows release"
manifest="$(curl -fsSL https://dl.wisprflow.com/wispr-flow/win32/latest.json)"
url="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["windows"]["x64"]["url"])' "$manifest")"
sha="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["windows"]["x64"]["sha256"])' "$manifest")"
version="$(python3 -c 'import re,sys; print(re.search(r"v(\d+(?:\.\d+)+)\.exe", sys.argv[1]).group(1))' "$url")"
installed="$(cat "$APP/resources/app/.wispr-version" 2>/dev/null || true)"
echo "    latest $version, installed ${installed:-none}"
if [[ "$installed" == "$version" && "$FORCE" != "--force" ]]; then echo "Up to date."; exit 0; fi

mkdir -p "$BUILD/dl"
setup="$BUILD/dl/WisprFlowSetup-$version.exe"
if [[ ! -f "$setup" ]] || ! echo "$sha  $setup" | sha256sum -c --quiet 2>/dev/null; then
  echo "==> Downloading $url"
  curl -fL --progress-bar -o "$setup" "$url"
fi
echo "$sha  $setup" | sha256sum -c --quiet

echo "==> Extracting"
x="$BUILD/x-$version"
rm -rf "$x"; mkdir -p "$x/pkg"
7z x -y -o"$x" "$setup" >/dev/null
7z x -y -o"$x/pkg" "$x"/WisprFlow-*-full.nupkg >/dev/null
win="$x/pkg/lib/net45"
electron_version="$(strings "$win/Wispr Flow.exe" | grep -oE 'Electron/[0-9]+\.[0-9]+\.[0-9]+' | head -1 | cut -d/ -f2)"
echo "    Electron $electron_version"

echo "==> Fetching Linux Electron $electron_version and sqlite3"
rt="$BUILD/rt-$electron_version"
if [[ ! -x "$rt/node_modules/electron/dist/electron" ]]; then
  mkdir -p "$rt"
  (cd "$rt" && npm init -y >/dev/null && npm i --ignore-scripts "electron@$electron_version" @electron/asar@3 >/dev/null \
    && node node_modules/electron/install.js)
fi
sqlite_version="$(cd "$x" && "$rt/node_modules/.bin/asar" extract-file "$win/resources/app.asar" package.json \
  && python3 -c 'import json; print(json.load(open("package.json"))["dependencies"]["sqlite3"])')"
if [[ ! -f "$rt/sqlite3-$sqlite_version.node" ]]; then
  (cd "$rt" && npm i --ignore-scripts "sqlite3@$sqlite_version" >/dev/null \
    && cd node_modules/sqlite3 && npx --yes prebuild-install -r napi >/dev/null \
    && cp build/Release/node_sqlite3.node "$rt/sqlite3-$sqlite_version.node")
fi

echo "==> Assembling $APP"
new="$APP.new"
rm -rf "$new"
cp -a "$rt/node_modules/electron/dist" "$new"
mv "$new/electron" "$new/wispr-flow"
rm -f "$new/resources/default_app.asar"
cp -a "$win/resources/app.asar" "$new/resources/wispr.asar"
cp -a "$win/resources/app.asar.unpacked" "$new/resources/wispr.asar.unpacked"
for extra in assets migrations ax-inspect.mjs ax-inspect-lib.mjs ax-inspect-server.mjs; do
  [[ -e "$win/resources/$extra" ]] && cp -a "$win/resources/$extra" "$new/resources/"
done
cp "$rt/sqlite3-$sqlite_version.node" "$new/resources/wispr.asar.unpacked/.webpack/main/native_modules/build/Release/node_sqlite3.node"
mkdir -p "$new/resources/app/helper"
cp "$SRC/loader.js" "$new/resources/app/"
sed "s/__VERSION__/$version/" "$SRC/package.json" > "$new/resources/app/package.json"
cp "$SRC/wispr-linux-helper.py" "$new/resources/app/helper/"
echo "$version" > "$new/resources/app/.wispr-version"
cp "$SRC/Update.exe" "$ROOT/Update.exe"   # the app looks for ../Update.exe next to its folder

if pgrep -f "^$APP/wispr-flow" >/dev/null; then
  echo "==> Quitting the running app"
  pkill -f "^$APP/wispr-flow" || true
  sleep 2
fi
rm -rf "$APP.old"
[[ -d "$APP" ]] && mv "$APP" "$APP.old"
mv "$new" "$APP"
rm -rf "$APP.old" "$x"

echo "==> Desktop integration"
cat > "$ROOT/run.sh" <<EOF
#!/bin/bash
export DISPLAY=\${DISPLAY:-:0} XAUTHORITY=\${XAUTHORITY:-\$HOME/.Xauthority}
export XDG_RUNTIME_DIR=\${XDG_RUNTIME_DIR:-/run/user/\$(id -u)}
export DBUS_SESSION_BUS_ADDRESS=\${DBUS_SESSION_BUS_ADDRESS:-unix:path=\$XDG_RUNTIME_DIR/bus}
exec "$APP/wispr-flow" "\$@"
EOF
chmod +x "$ROOT/run.sh"
icon_dir="$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$icon_dir" "$HOME/.local/share/applications"
python3 - "$x" "$icon_dir/wispr-flow.png" "$setup" <<'PY' 2>/dev/null || true
import sys, subprocess, tempfile, os
from PIL import Image
d = tempfile.mkdtemp(); subprocess.run(["7z", "x", "-y", "-o" + d, sys.argv[3], "setupIcon.ico"], capture_output=True)
im = Image.open(os.path.join(d, "setupIcon.ico")); im.size = max(im.info["sizes"]); im.load(); im.save(sys.argv[2])
PY
cp "$icon_dir/wispr-flow.png" "$APP/resources/app/icon.png" 2>/dev/null || true
cat > "$HOME/.local/share/applications/wispr-flow.desktop" <<EOF
[Desktop Entry]
Name=Wispr Flow
Comment=Voice dictation (Linux build of the Windows app)
Exec=$ROOT/run.sh %u
Icon=wispr-flow
Terminal=false
Type=Application
Categories=Utility;Accessibility;
MimeType=x-scheme-handler/wispr-flow;
StartupWMClass=wispr-flow
EOF
xdg-mime default wispr-flow.desktop x-scheme-handler/wispr-flow
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "Done: Wispr Flow $version. Start it from the menu or with $ROOT/run.sh"
