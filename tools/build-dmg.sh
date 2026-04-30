#!/usr/bin/env bash
# Build Rip Raptor.app and package it into a distributable .dmg.
#
# Output: dist/Rip-Raptor-<VERSION>.dmg
#
# What this does, in order:
#   1. Reads APP_VERSION from src/app.py.
#   2. Lints the Python (syntax-only — fast, catches typos).
#   3. Compiles VideoDownloader.swift → Mach-O binary.
#   4. Builds a fresh .app bundle from src/, resources/, vendor/.
#   5. Ad-hoc signs the bundle (no Apple Developer ID needed for beta — users
#      will get the Gatekeeper warning the first time, can right-click → Open).
#   6. Wraps the .app + an /Applications symlink into a UDZO-compressed dmg.
#
# Usage:
#   tools/build-dmg.sh                   # build with version from app.py
#   tools/build-dmg.sh 0.1.1             # override version (also patches Info.plist)
set -euo pipefail

# Resolve project root regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

# ───── Version handling ─────────────────────────────────────────────────
# Single source of truth is APP_VERSION in src/app.py. The build script
# reads it (or accepts an override on argv) and writes it back into
# Info.plist so all version-display surfaces stay in lock-step.
if [[ $# -ge 1 ]]; then
    VERSION="$1"
else
    VERSION="$(grep -E '^APP_VERSION\s*=' src/app.py | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
fi
if [[ -z "${VERSION:-}" ]]; then
    echo "ERROR: could not determine APP_VERSION from src/app.py" >&2
    exit 1
fi
echo "→ Building Rip Raptor v$VERSION"

# Reflect the chosen version into the plist & app.py if it differs.
PLIST="resources/Info.plist"
sed -i.bak -E "s|<string>[0-9]+\.[0-9]+\.[0-9]+</string>([[:space:]]*<key>CFBundleVersion)|<string>$VERSION</string>\1|g" "$PLIST"
# Two places use the version: CFBundleShortVersionString and CFBundleGetInfoString.
# The latter is "Rip Raptor X.Y.Z — ..." — patch via Python to avoid sed-quoting hell.
python3 - "$PLIST" "$VERSION" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); v = sys.argv[2]
s = p.read_text()
s = re.sub(r"(<key>CFBundleShortVersionString</key>\s*<string>)[^<]+(</string>)",
           rf"\g<1>{v}\g<2>", s)
s = re.sub(r"(<key>CFBundleVersion</key>\s*<string>)[^<]+(</string>)",
           rf"\g<1>{v}\g<2>", s)
s = re.sub(r"(<key>CFBundleGetInfoString</key>\s*<string>Rip Raptor )[0-9]+\.[0-9]+\.[0-9]+( — Created by Henri Scott</string>)",
           rf"\g<1>{v}\g<2>", s)
p.write_text(s)
PY
rm -f "${PLIST}.bak"

# Patch src/app.py APP_VERSION too if it doesn't match (only when overridden via argv).
if [[ $# -ge 1 ]]; then
    python3 - "$VERSION" <<'PY'
import re, sys, pathlib
v = sys.argv[1]
p = pathlib.Path("src/app.py")
s = p.read_text()
s = re.sub(r'^(APP_VERSION\s*=\s*)"[^"]+"', rf'\g<1>"{v}"', s, count=1, flags=re.M)
p.write_text(s)
PY
fi

# ───── Vendor binaries ──────────────────────────────────────────────────
# vendor/ is gitignored — fetch on demand from a clean clone so the
# build is still reproducible. If the user has already run fetch-vendor
# we trust whatever's there (lets you point at custom builds).
if [[ ! -x "vendor/yt-dlp" || ! -x "vendor/ffmpeg" || ! -x "vendor/ffprobe" ]]; then
    echo "→ Vendor binaries missing — running tools/fetch-vendor.sh"
    bash tools/fetch-vendor.sh
fi

# ───── Lint ──────────────────────────────────────────────────────────────
echo "→ Python syntax check"
python3 -m py_compile src/app.py src/hls_fetcher.py
echo "  OK"

# ───── Swift compile ────────────────────────────────────────────────────
echo "→ Compiling Swift host"
mkdir -p build
swiftc src/VideoDownloader.swift \
    -o "build/Rip Raptor" \
    -framework AppKit -framework WebKit -framework Foundation \
    -O
echo "  OK ($(stat -f%z "build/Rip Raptor") bytes)"

# ───── Assemble .app bundle ─────────────────────────────────────────────
APP="build/Rip Raptor.app"
echo "→ Assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources/bin"

# Swift binary.
cp "build/Rip Raptor" "$APP/Contents/MacOS/Rip Raptor"
chmod +x "$APP/Contents/MacOS/Rip Raptor"

# Python source.
cp src/app.py "$APP/Contents/Resources/app.py"
cp src/hls_fetcher.py "$APP/Contents/Resources/hls_fetcher.py"

# Static resources (icon, splash, etc).
cp resources/AppIcon.icns               "$APP/Contents/Resources/AppIcon.icns"
cp resources/banner.png                 "$APP/Contents/Resources/banner.png"
cp resources/get-ripped.png             "$APP/Contents/Resources/get-ripped.png"
cp resources/title.mp4                  "$APP/Contents/Resources/title.mp4"
cp resources/something-went-wrong.mp4   "$APP/Contents/Resources/something-went-wrong.mp4"
cp resources/Info.plist                 "$APP/Contents/Info.plist"

# Bundled binaries (yt-dlp, ffmpeg, ffprobe). These are what app.py's
# _BUNDLED_BIN_DIR lookup expects. Preserve permissions; the symlink-free
# copy keeps the dmg self-contained.
cp vendor/yt-dlp   "$APP/Contents/Resources/bin/yt-dlp"
cp vendor/ffmpeg   "$APP/Contents/Resources/bin/ffmpeg"
cp vendor/ffprobe  "$APP/Contents/Resources/bin/ffprobe"
chmod +x "$APP/Contents/Resources/bin/"*

# ───── Code-sign ─────────────────────────────────────────────────────────
echo "→ Ad-hoc signing"
# --deep walks nested executables. Bundled ffmpeg/ffprobe/yt-dlp need to
# be signed too or Gatekeeper kills the parent process when it spawns them.
# We sign the binaries first (innermost-out) then the .app last.
codesign --force --sign - --timestamp=none \
    "$APP/Contents/Resources/bin/yt-dlp" \
    "$APP/Contents/Resources/bin/ffmpeg" \
    "$APP/Contents/Resources/bin/ffprobe" 2>&1 | grep -v "replacing existing signature" || true
codesign --force --deep --sign - --timestamp=none "$APP" 2>&1 | grep -v "replacing existing signature" || true

# ───── DMG ───────────────────────────────────────────────────────────────
DMG="dist/Rip-Raptor-$VERSION.dmg"
echo "→ Building $DMG"
mkdir -p dist
rm -f "$DMG"

# Stage layout in a temp dir that hdiutil will photograph.
STAGE="$(mktemp -d -t ripraptor-dmg)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP" "$STAGE/Rip Raptor.app"
ln -s /Applications "$STAGE/Applications"

hdiutil create \
    -volname "Rip Raptor $VERSION" \
    -srcfolder "$STAGE" \
    -ov -format UDZO \
    -fs HFS+ \
    "$DMG" >/dev/null

SIZE_MB="$(du -m "$DMG" | cut -f1)"
echo ""
echo "✓ Built $DMG (${SIZE_MB}M)"
echo ""
echo "Smoke test:"
echo "  open \"$DMG\""
echo "  # then drag Rip Raptor.app to /Applications and launch"
