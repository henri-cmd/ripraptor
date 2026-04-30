#!/usr/bin/env bash
# Download the third-party binaries we bundle with Rip Raptor:
#   yt-dlp  — universal Mach-O (arm64 + x86_64), curl_cffi-enabled
#   ffmpeg  — static arm64 with VideoToolbox encoders
#   ffprobe — paired with ffmpeg, same source
#
# We don't ship these in git — the repo would balloon by ~130MB per
# release. Instead, build-dmg.sh runs this if vendor/ is empty so the
# build is still reproducible from a clean clone.
#
# Re-run this script to upgrade to whatever the upstream sources have
# published since the last fetch (we always pull "latest" rather than
# pinning — yt-dlp specifically benefits from being kept current).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENDOR="$ROOT/vendor"
mkdir -p "$VENDOR"

YTDLP_URL="https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
FFMPEG_URL="https://www.osxexperts.net/ffmpeg81arm.zip"
FFPROBE_URL="https://www.osxexperts.net/ffprobe81arm.zip"

TMP="$(mktemp -d -t ripraptor-vendor)"
trap 'rm -rf "$TMP"' EXIT

echo "→ yt-dlp (universal)"
curl -L --fail -o "$TMP/yt-dlp" "$YTDLP_URL" --silent --show-error
chmod +x "$TMP/yt-dlp"
"$TMP/yt-dlp" --version >/dev/null
mv "$TMP/yt-dlp" "$VENDOR/yt-dlp"

echo "→ ffmpeg (arm64 static)"
curl -L --fail -o "$TMP/ffmpeg.zip" "$FFMPEG_URL" --silent --show-error
unzip -qo "$TMP/ffmpeg.zip" -d "$TMP"
rm -rf "$TMP/__MACOSX"
chmod +x "$TMP/ffmpeg"
"$TMP/ffmpeg" -version >/dev/null
mv "$TMP/ffmpeg" "$VENDOR/ffmpeg"

echo "→ ffprobe (arm64 static)"
curl -L --fail -o "$TMP/ffprobe.zip" "$FFPROBE_URL" --silent --show-error
unzip -qo "$TMP/ffprobe.zip" -d "$TMP"
rm -rf "$TMP/__MACOSX"
chmod +x "$TMP/ffprobe"
"$TMP/ffprobe" -version >/dev/null
mv "$TMP/ffprobe" "$VENDOR/ffprobe"

echo ""
echo "✓ Vendor binaries:"
ls -lh "$VENDOR/"
