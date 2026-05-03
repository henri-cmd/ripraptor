#!/usr/bin/env python3
"""Video Downloader micro-app: local web UI wrapping yt-dlp."""

# PEP 563: defer annotation evaluation. Without this, the PEP 604
# union syntax (`dict | None`) used in type hints throughout this
# file blows up at function-definition time on Python 3.9 (Apple's
# stock /usr/bin/python3 on macOS 12-15 without Homebrew). With it,
# annotations become strings and never get evaluated at runtime, so
# the file imports cleanly on any Python 3.7+.
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlparse, quote, unquote

HOST = "127.0.0.1"
PORT_RANGE = range(8765, 8780)
PORT_FALLBACK = 8765
DEFAULT_DEST = str(Path.home() / "Downloads")

# App version. Single source of truth — surfaces in status bar, About panel,
# Settings → About, /versions endpoint, and is what the update checker
# compares against the latest GitHub release tag.
APP_VERSION = "0.1.11"

# Bundled-binary directory inside the .app:
#   /Applications/Rip Raptor.app/Contents/Resources/bin/{yt-dlp,ffmpeg,ffprobe}
# When running the dev source tree (no Resources/bin/), this dir won't
# exist — _pick_ytdlp / _FFMPEG fall through to PATH-based discovery.
_BUNDLED_BIN_DIR = Path(__file__).resolve().parent / "bin"

# URL rewriters — applied at every entry point that takes a user-supplied
# URL (the page's addUrl, the /queue endpoint). Each entry is
# (compiled_pattern, builder); the first match wins. Mirrors the JS
# URL_REWRITERS list in INDEX_HTML so the rewrite happens identically
# whether the URL arrives via the address-bar paste, a bookmarklet, a
# curl POST to /queue, or a drag-drop.
#
# Currently handles:
#   IMDB title page → 111movies.net by IMDB id (yt-dlp can rip from
#   111movies, but not from IMDB itself).
# IMDB inputs are now handled client-side via the movie/TV prompt
# (see _detectImdbId + _showImdbPrompt in INDEX_HTML). Backend
# rewriting can't ask the user, so an IMDB URL hitting /queue from a
# bookmarklet/script flows through unchanged and the page-side prompt
# still fires once the SSE listener routes the URL into addUrl().
# This list stays for future non-interactive rewrites.
_URL_REWRITERS = []


def _rewrite_url(url: str) -> str:
    """Apply the URL_REWRITERS list to a single URL. Returns the URL
    unchanged when no rewriter matches. Mirrors the JS _rewriteUrl in
    INDEX_HTML so backend-only callers (e.g. POST /queue from a script)
    get the same rewrites the page-level addUrl applies."""
    for pat, fn in _URL_REWRITERS:
        m = pat.match(url or "")
        if m:
            return fn(m)
    return url


# Per-user vendor directory for runtime-installed Python deps
# (curl_cffi). Lives in Application Support so it persists across
# launches — the install runs once on first start. Exposed as a
# module-level constant so the /hls-fetch subprocess launcher can
# reuse it via PYTHONPATH.
_CURL_CFFI_VENDOR = (Path.home() / "Library" / "Application Support" /
                     "Rip Raptor" / "python-deps")


def _ensure_curl_cffi_runtime():
    """Make `curl_cffi` importable for the rest of this process. Tried
    in order:

      1. Direct import — already installed in the running Python.
      2. Pipx venv re-exec — the dev path. If the user has
         `pipx install yt-dlp` set up, curl_cffi is already injected
         into that venv; we re-exec there so the import is free.
      3. Vendor-dir bootstrap — for users who installed the dmg but
         have no pipx and no curl_cffi anywhere. We pip-install it
         into Application Support/Rip Raptor/python-deps and add
         that dir to sys.path. ~10-30s on first launch, free on
         every subsequent launch.

    Falls through silently if all three fail; curl_cffi-dependent
    features (HLS impersonation, editor segment proxy) will fail at
    use time but the app still launches.
    """
    # Path 1: direct import.
    try:
        import curl_cffi  # noqa: F401
        return
    except Exception:
        pass

    # Path 2: pipx venv re-exec (dev convenience).
    venv_dir = str(Path.home() / ".local/pipx/venvs/yt-dlp")
    try:
        already_in_venv = os.path.realpath(sys.prefix).startswith(
            os.path.realpath(venv_dir))
    except Exception:
        already_in_venv = False
    if not already_in_venv:
        for cand in (
            str(Path(venv_dir) / "bin/python"),
            str(Path(venv_dir) / "bin/python3"),
        ):
            if not Path(cand).exists():
                continue
            try:
                # Pass -u so stdout stays unbuffered after re-exec — Swift
                # parses our startup banner from stdout and gates the UI on it.
                os.execv(cand, [cand, "-u", os.path.abspath(__file__)] + sys.argv[1:])
            except Exception:
                continue

    # Path 3: vendor dir bootstrap. Persistent install in Application
    # Support so subsequent launches skip the pip step.
    try:
        _CURL_CFFI_VENDOR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    if str(_CURL_CFFI_VENDOR) not in sys.path:
        sys.path.insert(0, str(_CURL_CFFI_VENDOR))
    try:
        import curl_cffi  # noqa: F401
        return  # already installed from a prior launch
    except Exception:
        pass

    # Need to install. Print BEFORE we run pip so users on the very
    # first launch see "Installing dependencies..." instead of an
    # apparent freeze. The Swift host watches stdout for our HTTP
    # banner — extra "[setup]" lines ahead of it don't trip anything.
    try:
        sys.stdout.write("[setup] Installing curl_cffi (one-time, ~10-30s)…\n")
        sys.stdout.flush()
    except Exception:
        pass

    pip_cmd = [sys.executable, "-m", "pip", "install",
               "--target", str(_CURL_CFFI_VENDOR),
               "--quiet", "--upgrade",
               "curl_cffi"]
    try:
        result = subprocess.run(pip_cmd, capture_output=True, text=True, timeout=240)
    except Exception as e:
        try:
            sys.stdout.write(f"[setup] curl_cffi install error: {e}\n")
            sys.stdout.flush()
        except Exception: pass
        return

    if result.returncode != 0:
        tail = ((result.stderr or result.stdout or "")[-400:]).strip()
        try:
            sys.stdout.write(f"[setup] curl_cffi install failed (rc={result.returncode}): {tail}\n")
            sys.stdout.flush()
        except Exception: pass
        return

    # Re-attempt import now that the wheel is on disk + on path.
    try:
        import curl_cffi  # noqa: F401
        sys.stdout.write("[setup] curl_cffi installed.\n"); sys.stdout.flush()
    except Exception as e:
        try:
            sys.stdout.write(f"[setup] curl_cffi still not importable: {e}\n")
            sys.stdout.flush()
        except Exception: pass


_ensure_curl_cffi_runtime()

def _pick_ytdlp() -> str:
    """Prefer a yt-dlp binary that has impersonation support (curl_cffi).

    Search order:
      1. Bundled standalone yt-dlp_macos in the .app's Resources/bin/ — this
         is what ships in the dmg and is PyInstaller-frozen with curl_cffi
         baked in, so it's known-good.
      2. The pipx-managed install at ~/.local/bin (dev / advanced users).
      3. Whatever is on PATH.

    For the bundled candidate we skip the impersonation probe — it's a
    known-good build, and probing on every launch wastes ~200ms."""
    bundled = _BUNDLED_BIN_DIR / "yt-dlp"
    if bundled.exists() and os.access(str(bundled), os.X_OK):
        return str(bundled)
    candidates = [
        str(Path.home() / ".local/bin/yt-dlp"),
        shutil.which("yt-dlp"),
        "/opt/homebrew/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
    ]
    seen = set()
    for c in candidates:
        if not c or c in seen or not Path(c).exists():
            continue
        seen.add(c)
        try:
            r = subprocess.run([c, "--list-impersonate-targets"],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and "curl_cffi" in r.stdout:
                return c
        except Exception:
            continue
    # Fall back to whichever is on PATH, even without curl_cffi
    return shutil.which("yt-dlp") or candidates[0]


def _pick_ffmpeg() -> str | None:
    """Prefer the bundled ffmpeg in Resources/bin/, else PATH. Returning
    None lets the rest of the app gracefully degrade — features that
    need ffmpeg get a friendly 'not installed' error instead of a crash."""
    bundled = _BUNDLED_BIN_DIR / "ffmpeg"
    if bundled.exists() and os.access(str(bundled), os.X_OK):
        return str(bundled)
    return shutil.which("ffmpeg")


YT_DLP = _pick_ytdlp()
GALLERY_DL = shutil.which("gallery-dl")
ARIA2C = shutil.which("aria2c")
_FFMPEG = _pick_ffmpeg()
FFMPEG_DIR = str(Path(_FFMPEG).parent) if _FFMPEG else None


def _detect_videotoolbox() -> dict:
    """On Apple Silicon, ffmpeg ships with VideoToolbox encoders that do
    h.264 / h.265 in hardware — 5-10x faster than libx264 with negligible
    quality loss for our settings. Detect what's available so we can
    transparently use it whenever a recode is needed."""
    if not _FFMPEG:
        return {"h264": False, "hevc": False}
    try:
        r = subprocess.run([_FFMPEG, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=5)
        out = r.stdout or ""
        return {
            "h264": "h264_videotoolbox" in out,
            "hevc": "hevc_videotoolbox" in out,
        }
    except Exception:
        return {"h264": False, "hevc": False}


VIDEOTOOLBOX = _detect_videotoolbox()

# Speed flags for yt-dlp downloads. Strategy:
#   - HLS/DASH fragments: yt-dlp's native downloader with high concurrency.
#     Re-uses keep-alive across fragments, respects --impersonate, and is
#     faster than spawning aria2c per-fragment for typical 1-4MB segments.
#   - Direct HTTP files (single mp4 etc): aria2c does multi-chunk parallel
#     range requests, which is the genuine speed-up case.
#   - Robust retries with exponential backoff so a transient 429/502 from a
#     CDN doesn't kill the whole download mid-way.
SPEED_FLAGS = [
    "--concurrent-fragments", "16",
    "--retries", "10",
    "--fragment-retries", "10",
    "--retry-sleep", "fragment:exp=1:30",
    "--retry-sleep", "http:exp=1:30",
    "--http-chunk-size", "10M",
]
if ARIA2C:
    # Restrict aria2c to direct HTTP/HTTPS — let native handle HLS/DASH.
    SPEED_FLAGS += [
        "--downloader", f"http,https:{ARIA2C}",
        "--downloader-args",
        "aria2c:-x 16 -s 16 -k 1M --max-tries=5 --retry-wait=1 --console-log-level=warn --summary-interval=1 --file-allocation=none",
    ]

# Hosts known to publish multi-asset posts (carousels, albums, threads). When
# yt-dlp returns a single asset for one of these we re-probe with gallery-dl
# to make sure we're not missing siblings.
# Hosts where the URL is overwhelmingly likely to be a *video* page —
# if our cascade collapses to a single-image fallback (just an og:image
# or a yt-dlp thumbnail) on one of these hosts, we're almost certainly
# missing the real media. Mark those responses low-confidence so the
# frontend can fall through to the in-page sniffer.
# URL paths that strongly imply "this is a video page" even on a host we
# don't recognise. /embed/, /watch/, /player/, /video/, etc. land here.
# Used to flag single-image probe responses as low-confidence so the
# in-page sniffer gets a chance.
VIDEO_HINT_PATH_RE = re.compile(
    r"/(?:embed|watch|player|video|videos|stream|streaming|movie|movies|"
    r"film|films|tv|episode|episodes|series|shows|live|broadcast|vod|playback)"
    r"(?:/|$|\?|#)",
    re.IGNORECASE,
)


def _url_looks_like_video_page(u: str) -> bool:
    try:
        p = urlparse(u or "")
        return bool(VIDEO_HINT_PATH_RE.search(p.path or ""))
    except Exception:
        return False


VIDEO_LIKELY_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be",
    "vimeo.com", "www.vimeo.com", "player.vimeo.com",
    "twitch.tv", "www.twitch.tv", "clips.twitch.tv",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "dailymotion.com", "www.dailymotion.com",
    "rumble.com", "www.rumble.com",
    "bitchute.com", "www.bitchute.com",
    "odysee.com", "www.odysee.com",
    "streamable.com", "www.streamable.com",
    "facebook.com", "www.facebook.com", "fb.watch",
    "twitter.com", "x.com", "www.twitter.com", "www.x.com",
}

GALLERY_HOSTS = {
    "instagram.com", "www.instagram.com",
    "pinterest.com", "www.pinterest.com", "pin.it",
    "twitter.com", "x.com", "www.twitter.com", "www.x.com",
    "reddit.com", "www.reddit.com", "old.reddit.com", "redd.it",
    "tumblr.com", "www.tumblr.com",
    "imgur.com", "www.imgur.com", "i.imgur.com",
    "flickr.com", "www.flickr.com",
    "deviantart.com", "www.deviantart.com",
    "artstation.com", "www.artstation.com",
    "behance.net", "www.behance.net",
    "weibo.com", "www.weibo.com",
    "vk.com", "www.vk.com",
    "facebook.com", "www.facebook.com", "m.facebook.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "bsky.app",
    "threads.net", "www.threads.net",
    "newgrounds.com", "www.newgrounds.com",
}

VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "mkv", "avi", "flv", "ts", "m3u8", "mpd"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "avif", "bmp", "tiff", "tif"}
AUDIO_EXTS = {"mp3", "m4a", "ogg", "opus", "wav", "flac", "aac"}

# Browsers yt-dlp / gallery-dl can extract cookies from. Whitelisted to
# prevent CLI-flag injection from a request body.
VALID_BROWSERS = {"chrome", "firefox", "safari", "edge", "brave",
                  "chromium", "opera", "vivaldi", "whale"}

def _sanitize_browser(name: str) -> str:
    n = (name or "").strip().lower()
    return n if n in VALID_BROWSERS else ""


# Keywords in yt-dlp / gallery-dl stderr that mean "this needs auth or is
# blocked"; we map them to a short human hint shown above the raw error
# AND a `kind` token the frontend uses to decide whether to surface a
# retry-with-cookies UI on the failed card.
_AUTH_HINT_PATTERNS = [
    (("sign in", "log in", "login required", "private video",
      "members-only", "members only", "this is private",
      "must be authenticated"),
     "Looks like this needs a login. Pick the browser whose cookies "
     "should be used (Chrome / Safari / Firefox / Brave / Edge) and retry.",
     "auth"),
    (("age-restricted", "age restricted", "confirm your age", "age verification",
      "inappropriate for some users"),
     "This video is age-restricted. Pick a browser you're signed in to "
     "with an age-confirmed account.",
     "age"),
    (("geo-restricted", "geo restricted", "not available in your country",
      "not available in your region"),
     "This is geo-blocked in your region. A VPN or proxy will be needed.",
     "geo"),
    # Cloudflare's "blocked by site" — usually means the CDN's bot
    # heuristics flagged the request. Fresh cookies + a real-browser
    # impersonation usually works.
    (("error 1010", "cloudflare", "challenge required",
      "verify you are human", "ray id"),
     "The site's Cloudflare protection blocked the request. Try the "
     "Sniff fallback (in-app browser) — it impersonates a real Safari "
     "session more thoroughly than yt-dlp's direct fetch.",
     "cloudflare"),
    # YouTube's bot-detection. Same fix: cookies from a logged-in browser.
    (("sign in to confirm you're not a bot", "sign in to confirm",
      "not a bot", "captcha required"),
     "YouTube wants confirmation you're not a bot. Pick a browser "
     "you're signed in to (Cookies source… in Settings).",
     "auth"),
    # Rate-limited: server is throttling.
    (("rate-limited", "too many requests", "http error 429",
      "retry-after"),
     "The server is rate-limiting you. Wait a minute and try again, "
     "or change networks.",
     "rate"),
    # Disk full / write error.
    (("no space left on device", "disk full", "errno 28"),
     "Disk is full — free up space and retry.",
     "disk"),
    # Format/codec extraction failure.
    (("requested format is not available", "no video formats found",
      "no formats found"),
     "yt-dlp couldn't find a usable stream. Try the Sniff fallback or "
     "update yt-dlp (Settings → Check for yt-dlp updates).",
     "format"),
    # SSL / cert issue.
    (("ssl: certificate", "certificate verify failed",
      "self-signed certificate", "ssl_error"),
     "TLS certificate problem. Check the system clock + that the URL "
     "really uses HTTPS; corporate proxies sometimes break this.",
     "tls"),
    # Network down / unreachable.
    (("name or service not known", "could not resolve host",
      "no route to host", "network is unreachable",
      "connection refused", "connection reset"),
     "Couldn't reach the server. Check your network connection.",
     "network"),
    # ffmpeg missing — installation problem.
    (("ffmpeg: not found", "ffmpeg not found", "no ffmpeg",
      "[ffmpeg] is required"),
     "ffmpeg isn't installed. Install with Homebrew: `brew install ffmpeg`.",
     "ffmpeg"),
    # Unsupported URL / extractor doesn't know the site.
    (("unsupported url", "no suitable extractor"),
     "This site isn't directly supported by yt-dlp. The Sniff fallback "
     "(in-app browser) will probe the page for video streams instead.",
     "unsupported"),
]


def _classify_error(msg: str) -> str:
    """Return 'auth', 'age', 'geo', or '' based on the error text."""
    if not msg:
        return ""
    low = msg.lower()
    for needles, _hint, kind in _AUTH_HINT_PATTERNS:
        if any(n in low for n in needles):
            return kind
    return ""


def _augment_error_hint(msg: str) -> str:
    """Prepend a short user-friendly hint to the raw error if it matches
    a known pattern; otherwise return the message unchanged."""
    if not msg:
        return msg
    low = msg.lower()
    for needles, hint, _kind in _AUTH_HINT_PATTERNS:
        if any(n in low for n in needles):
            return hint + "\n\n" + msg
    return msg


def _err_payload(msg: str) -> dict:
    """Standard error JSON: { error, hint } where hint is a classification
    token the frontend can switch on (e.g. 'auth' → show cookies retry)."""
    return {"error": _augment_error_hint(msg), "hint": _classify_error(msg)}


def _pick_python_with_curl_cffi() -> str:
    """Find a Python interpreter that can `import curl_cffi`. Order:

      1. The currently-running Python (sys.executable). After
         _ensure_curl_cffi_runtime ran at module-load time, curl_cffi
         is already importable here for users with pipx OR users
         who got the auto-installed vendor dir.
      2. The pipx venv (dev path).

    Returns "" if no Python with curl_cffi can be found — callers fall
    back to stdlib paths or surface a missing-helper error.
    """
    # Test current Python first. We have to run a subprocess (rather
    # than just importing here) because the same call also probes for
    # other Python installs below.
    candidates = [
        sys.executable,
        str(Path.home() / ".local/pipx/venvs/yt-dlp/bin/python"),
        str(Path.home() / ".local/pipx/venvs/yt-dlp/bin/python3"),
    ]
    # Probe with PYTHONPATH including our vendor dir, since the
    # auto-installed curl_cffi lives there for users without pipx.
    env = dict(os.environ)
    extra = str(_CURL_CFFI_VENDOR)
    if extra:
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = extra + (os.pathsep + prev if prev else "")
    for c in candidates:
        if not c or not Path(c).exists():
            continue
        try:
            r = subprocess.run([c, "-c", "import curl_cffi"],
                               capture_output=True, timeout=5, env=env)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return ""


CURL_PYTHON = _pick_python_with_curl_cffi()
HLS_FETCHER = str(Path(__file__).parent / "hls_fetcher.py")

PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")
# aria2c progress lines look like:
#   [#abc123 12MiB/100MiB(12%) CN:16 DL:5.2MiB ETA:17s]
ARIA2_PROGRESS_RE = re.compile(r"\(\s*(\d+(?:\.\d+)?)%\s*\)")
DEST_RE = re.compile(r"\[(?:download|ExtractAudio)\] Destination: (.+)$")
MERGE_RE = re.compile(r'\[Merger\] Merging formats into "([^"]+)"')

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")
UNSUPPORTED_HINT = "Unsupported URL"


def origin_of(u: str) -> str:
    try:
        p = urlparse(u)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


def common_args(url: str, generic: bool, referer: str = "", cookies_file: str = "",
                cookies_browser: str = "") -> list:
    ref = referer or url
    args = [
        "--user-agent", BROWSER_UA,
        "--impersonate", "chrome",
        # Force curl_cffi for the generic extractor's initial webpage fetch
        # too — without this, yt-dlp uses urllib there and gets 403'd by CDNs.
        "--extractor-args", "generic:impersonate=chrome",
        # Headers a real Chrome sends. TLS impersonation alone isn't enough —
        # CDNs commonly check Origin / Sec-Fetch-* / Accept too.
        "--add-header", f"Referer:{ref}",
        "--add-header", "Accept:*/*",
        "--add-header", "Accept-Language:en-US,en;q=0.9",
        "--add-header", "Sec-Fetch-Dest:empty",
        "--add-header", "Sec-Fetch-Mode:cors",
        "--add-header", "Sec-Fetch-Site:cross-site",
    ]
    o = origin_of(ref)
    if o:
        args += ["--add-header", f"Origin:{o}"]
    # Cookie sources are mutually exclusive. An explicit cookies file (e.g.
    # captured from the in-app sniff session) wins over browser cookies.
    if cookies_file and Path(cookies_file).exists():
        args += ["--cookies", cookies_file]
    elif cookies_browser:
        b = _sanitize_browser(cookies_browser)
        if b:
            args += ["--cookies-from-browser", b]
    if generic:
        args += ["--force-generic-extractor"]
    # User plugin directory — yt-dlp loads any extractor packages that
    # live here. Lets advanced users drop in custom site support without
    # shipping app updates.
    try:
        if YTDLP_PLUGIN_DIR.exists():
            args += ["--plugin-dirs", str(YTDLP_PLUGIN_DIR)]
    except Exception:
        pass
    return args

jobs: dict = {}
jobs_lock = threading.Lock()
shutdown_event = threading.Event()

# Stash for browser-fetched manifests served back to yt-dlp via localhost
manifests: dict = {}  # id -> str
manifests_lock = threading.Lock()

# Cross-app URL queue. Other tools (a browser bookmarklet, a curl from
# the terminal, an Automator action) POST to /queue and the page picks
# the URL up via SSE on /queue/events.
_QUEUE_LISTENERS: list = []  # list of Queue() instances, one per SSE client
_QUEUE_LISTENERS_LOCK = threading.Lock()

# Editor sessions: each one wraps a source media stream behind a local
# HTTP proxy so the editor's <video> + ffmpeg both consume it without
# needing CDN auth/Referer fudging.
editor_sessions: dict = {}  # sid -> dict (see _make_editor_session)
editor_lock = threading.Lock()


def _cookies_for_host(url: str, cookies: list) -> dict:
    host = (urlparse(url).hostname or "").lower()
    out = {}
    for c in cookies or []:
        d = (c.get("domain") or "").lstrip(".").lower()
        if not d or host == d or host.endswith("." + d):
            out[c["name"]] = c["value"]
    return out


_proxy_session = None
_proxy_session_lock = threading.Lock()


def _simple_get(url: str, *, timeout: float = 10.0,
                headers: dict | None = None) -> tuple[int, str]:
    """Plain stdlib urllib GET. Returns (status, body_text).

    Used for endpoints that don't need TLS impersonation — IMDB's
    suggest API, cinemeta. These are public JSON APIs without
    anti-bot fingerprinting, so the heavyweight curl_cffi machinery
    isn't needed and shouldn't gate them. Critically, IMDB search
    keeps working on user installs where curl_cffi isn't present in
    the system Python (Apple's stock 3.9 most notably).

    Returns (status, body) on any HTTP response — including 4xx/5xx.
    Raises RuntimeError only on transport failures (DNS, TLS, etc.).
    """
    import urllib.request
    import urllib.error
    h = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                       "Version/17.0 Safari/605.1.15"),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception as e:
        raise RuntimeError(f"http error: {e}") from e


def _proxy_get(url: str, cookies: list, *, range_header: str = "", stream: bool = True):
    """One curl_cffi-impersonated GET. Reuses a process-wide Session so
    HTTP keep-alive works across segment fetches. Returns the curl_cffi
    Response object."""
    global _proxy_session
    from curl_cffi import requests as cr
    with _proxy_session_lock:
        if _proxy_session is None:
            _proxy_session = cr.Session()
        s = _proxy_session
    headers = {"Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if range_header:
        headers["Range"] = range_header
    return s.get(
        url, headers=headers,
        cookies=_cookies_for_host(url, cookies),
        impersonate="chrome120", timeout=60, stream=stream,
    )


_EXTINF_RE = re.compile(r"#EXTINF:\s*([\d.]+)")
_MAP_URI_RE = re.compile(r'URI="([^"]+)"')
_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_RES_HEIGHT_RE = re.compile(r"RESOLUTION=\d+x(\d+)")


def _parse_master_variants(text: str, base: str) -> list:
    """Return [(height, bandwidth, abs_url), ...] sorted highest-first."""
    from urllib.parse import urljoin
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not _STREAM_INF_RE.search(line):
            continue
        bw = int((_BANDWIDTH_RE.search(line) or [0, "0"])[1] or 0) if _BANDWIDTH_RE.search(line) else 0
        h = int((_RES_HEIGHT_RE.search(line) or [0, "0"])[1] or 0) if _RES_HEIGHT_RE.search(line) else 0
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not nxt.startswith("#"):
                out.append((h, bw, urljoin(base, nxt)))
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return out


def _parse_variant_segments(text: str, base: str) -> tuple:
    """Return (init_url_or_none, [(idx, abs_url, duration_s), ...], total_s)."""
    from urllib.parse import urljoin
    init_url = None
    segs = []
    pending_dur = 0.0
    total = 0.0
    for raw in text.splitlines():
        t = raw.strip()
        if not t:
            continue
        if t.startswith("#EXT-X-MAP:"):
            m = _MAP_URI_RE.search(t)
            if m:
                init_url = urljoin(base, m.group(1))
        elif t.startswith("#EXTINF:"):
            m = _EXTINF_RE.search(t)
            if m:
                pending_dur = float(m.group(1))
        elif not t.startswith("#"):
            segs.append((len(segs), urljoin(base, t), pending_dur))
            total += pending_dur
            pending_dur = 0.0
    return init_url, segs, total


# In-process cache of yt-dlp resolutions. Keyed by (url, height,
# audio_only, generic, referer, cookies_file, cookies_browser,
# playlist_items). Value is the same tuple _resolve_via_ytdlp returns.
# Wins on:
#   - Editor reopen on the same card (was already cardSid-cached, but
#     the user closes the editor sometimes which drops cardSid)
#   - User reopens after a Done → re-Edit cycle
#   - Multiple cards on the same URL (sniffer carousels, etc.)
# Cache lives for the session; a process restart re-fetches. Resolved
# URLs are typically signed CDN tokens with 1-2h expiry; if a cached
# entry is stale ffmpeg will 403 mid-rip and the user will retry.
_ytdlp_resolution_cache: dict = {}
_ytdlp_resolution_lock = threading.Lock()


def _resolve_via_ytdlp(url: str, *, height=None, audio_only: bool = False,
                       generic: bool = False, referer: str = "",
                       cookies_file: str = "",
                       cookies_browser: str = "",
                       playlist_items: str = "") -> tuple:
    """Use yt-dlp -g to get a single direct URL the editor's <video> can play.
    YouTube and most yt-dlp sources have separate video+audio streams that
    don't fit a plain <video src>; we restrict the format selector to
    formats that already contain both. Returns (kind, src_url_or_manifest_url,
    manifest_text_or_empty, duration, title)."""
    cache_key = (url, str(height or ""), bool(audio_only),
                 bool(generic), referer, cookies_file, cookies_browser,
                 str(playlist_items or ""))
    with _ytdlp_resolution_lock:
        cached = _ytdlp_resolution_cache.get(cache_key)
    if cached is not None:
        return cached
    fmt = ("ba/b" if audio_only
           else (f"b[height<={int(height)}]" if (height and str(height) != "best")
                 else "b"))
    pl_flag = ["--playlist-items", str(playlist_items)] if playlist_items else ["--no-playlist"]
    cmd = ([YT_DLP] + pl_flag + ["-J", "-f", fmt]
           + common_args(url, generic, referer, cookies_file, cookies_browser) + [url])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "yt-dlp resolve failed")
    info = json.loads(res.stdout)
    # With --playlist-items <N>, yt-dlp returns a single-entry playlist;
    # collapse into a normal info dict so the rest of this function works.
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise RuntimeError("playlist had no entries")
        info = entries[0]
    title = info.get("title") or info.get("id") or ""
    duration = float(info.get("duration") or 0.0)
    direct = info.get("url") or ""
    if not direct:
        # Selected format split into requested+formats list — pick one with url
        formats = info.get("requested_formats") or info.get("formats") or []
        for f in formats:
            if f.get("url"):
                direct = f["url"]; break
    if not direct:
        raise RuntimeError("yt-dlp didn't return a single direct URL "
                           "(probably split video/audio). Try 'Rip' first, "
                           "then edit the file.")
    proto = (info.get("protocol") or "").lower()
    if "m3u8" in proto or direct.endswith(".m3u8"):
        result = ("hls", direct, "", duration, title)
    else:
        result = ("mp4", direct, "", duration, title)
    with _ytdlp_resolution_lock:
        _ytdlp_resolution_cache[cache_key] = result
    return result


def _make_editor_session(*, kind: str, page_url: str, cookies: list,
                        manifest_text: str = "", manifest_url: str = "",
                        src_url: str = "", title: str = "",
                        filename_hint: str = "",
                        url: str = "", height=None, audio_only: bool = False,
                        generic: bool = False, referer: str = "",
                        cookies_file: str = "",
                        cookies_browser: str = "",
                        playlist_items: str = "") -> dict:
    """Build a session record. For HLS, resolves master playlists and
    parses out the segment list ahead of time so /seg/<n> is just a lookup.
    For 'ytdlp' kind, runs yt-dlp -g first to get a direct URL.
    """
    sess = {
        "kind": kind, "page_url": page_url, "cookies": cookies,
        "title": title, "filename_hint": filename_hint or title or "video",
        "created": time.time(),
    }
    if kind == "ytdlp":
        rk, rurl, rtext, rdur, rtitle = _resolve_via_ytdlp(
            url, height=height, audio_only=audio_only,
            generic=generic, referer=referer, cookies_file=cookies_file,
            cookies_browser=cookies_browser, playlist_items=playlist_items)
        if not sess["title"]: sess["title"] = rtitle
        if not sess["filename_hint"]: sess["filename_hint"] = rtitle or "video"
        kind = rk
        if rk == "hls":
            manifest_url = rurl
            r = _proxy_get(rurl, cookies, stream=False)
            if r.status_code != 200:
                raise RuntimeError(f"manifest fetch HTTP {r.status_code}")
            manifest_text = r.text
        else:
            src_url = rurl
            sess["duration"] = rdur
        sess["kind"] = kind
    if kind == "hls":
        text = manifest_text
        base = manifest_url
        if "#EXT-X-STREAM-INF" in text:
            variants = _parse_master_variants(text, base)
            if variants:
                _, _, vurl = variants[0]
                r = _proxy_get(vurl, cookies, stream=False)
                if r.status_code != 200:
                    raise RuntimeError(f"variant fetch HTTP {r.status_code}")
                text = r.text
                base = vurl
        init_url, segs, dur = _parse_variant_segments(text, base)
        if not segs:
            raise RuntimeError("no segments in playlist")
        sess.update({
            "manifest_url": base,
            "init_url": init_url,
            "segments": segs,  # [(idx, abs_url, duration_s), ...]
            "duration": dur,
        })
    elif kind == "mp4":
        sess.setdefault("duration", 0.0)
        sess.update({"src_url": src_url})
    else:
        raise ValueError(f"unknown editor kind {kind!r}")
    sess["cache_lock"] = threading.Lock()
    sess["cached_path"] = ""
    sess["items"] = []
    sess["markers"] = []   # [{id, t, label}, …] — navigational bookmarks
    # "best" = no scaling (ffmpeg passes through source resolution). Same
    # spelling as the main-page card's quality dropdown so the editor's
    # default matches whatever the user picked when starting the rip.
    sess["default_quality"] = "best"
    return sess


def _ensure_cached_source(sess: dict) -> str:
    """Download the editor's source to a temp file once, then re-use it for
    every clip/still. ffmpeg over the live HTTP proxy works for sequential
    playback but breaks on seek-heavy patterns (Invalid data found / exit
    183) — local files don't have that problem."""
    lock = sess["cache_lock"]
    with lock:
        cached = sess.get("cached_path")
        if cached and Path(cached).exists():
            return cached
        ext = "mp4"
        kind = sess["kind"]
        tmp = Path(f"/tmp/rr-edit-{uuid.uuid4().hex[:10]}.{ext}")
        cookies = sess.get("cookies") or []
        # Reset progress counters — /editor/cache-status reads these to
        # surface a download bar while the editor's player is still on
        # the live /proxy stream.
        sess["cache_bytes"] = 0
        sess["cache_total"] = 0
        sess["cache_error"] = ""
        if kind == "mp4":
            try:
                r = _proxy_get(sess["src_url"], cookies, stream=True)
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"source HTTP {r.status_code}")
                # Content-Length lets the UI render a determinate bar;
                # if absent (chunked encoding) we keep total=0 and the
                # frontend falls back to an indeterminate spinner.
                try:
                    sess["cache_total"] = int(r.headers.get("content-length") or 0)
                except (TypeError, ValueError):
                    sess["cache_total"] = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            f.write(chunk)
                            sess["cache_bytes"] = sess.get("cache_bytes", 0) + len(chunk)
                try: r.close()
                except Exception: pass
            except Exception as e:
                sess["cache_error"] = str(e)
                raise
        elif kind == "hls":
            # Reuse the existing helper subprocess so impersonation/cookies
            # match the player's network stack exactly. We don't get a
            # streaming-progress channel here (hls_fetcher is a black-box
            # subprocess), so cache_total stays 0 and the UI shows an
            # indeterminate spinner instead of a percent bar.
            try:
                tmp_ts = tmp.with_suffix(".ts")
                spec = json.dumps({
                    "manifest_text": _build_local_playlist_text(sess),
                    "manifest_url": sess["manifest_url"],
                    "page_url": sess.get("page_url") or sess["manifest_url"],
                    "cookies": cookies,
                    "out_path": str(tmp_ts),
                }).encode()
                if not CURL_PYTHON or not Path(HLS_FETCHER).exists():
                    raise RuntimeError("hls_fetcher unavailable")
                # Pass PYTHONPATH=vendor so the subprocess can find
                # our auto-installed curl_cffi when the system Python
                # doesn't already have it (the standard user case).
                _env = dict(os.environ)
                _env["PYTHONPATH"] = str(_CURL_CFFI_VENDOR) + (
                    os.pathsep + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
                res = subprocess.run(
                    [CURL_PYTHON, "-u", HLS_FETCHER],
                    input=spec, capture_output=True, timeout=3600, env=_env,
                )
                if not tmp_ts.exists() or tmp_ts.stat().st_size == 0:
                    raise RuntimeError("hls fetch produced empty output")
                # Remux to a clean MP4 so seeking works frame-accurately.
                mux_args = [_ffmpeg_bin(), "-y", "-loglevel", "error",
                            "-i", str(tmp_ts),
                            "-c", "copy", "-bsf:a", "aac_adtstoasc",
                            "-movflags", "+faststart", "-fflags", "+genpts",
                            str(tmp)]
                mr = subprocess.run(mux_args, capture_output=True, text=True, timeout=600)
                try: tmp_ts.unlink()
                except Exception: pass
                if mr.returncode != 0 or not tmp.exists():
                    tail = (mr.stderr or "").strip().splitlines()
                    raise RuntimeError("remux failed: " + (tail[-1] if tail else "no stderr"))
            except Exception as e:
                sess["cache_error"] = str(e)
                raise
        else:
            raise RuntimeError(f"can't cache kind={kind}")
        # Final size — let the status endpoint display the total even when
        # the upstream Content-Length wasn't available.
        try:
            sess["cache_total"] = max(int(sess.get("cache_total", 0)),
                                      tmp.stat().st_size)
            sess["cache_bytes"] = tmp.stat().st_size
        except OSError:
            pass
        sess["cached_path"] = str(tmp)
        return str(tmp)


def _prefetch_cached_source(sid: str) -> None:
    """Background warmup so the local cache is ready when the user clicks
    Save All. Errors are swallowed — the on-demand path will retry."""
    try:
        sess = _editor_get(sid)
        if not sess:
            return
        _ensure_cached_source(sess)
    except Exception:
        pass


def _build_local_playlist_text(sess: dict) -> str:
    """Reconstruct a flat media playlist from the parsed segments — the
    HLS helper expects a manifest text, not a session."""
    segs = sess["segments"]
    has_init = bool(sess.get("init_url"))
    target = max(1, int(max((d for _, _, d in segs), default=6.0))) + 1
    lines = ["#EXTM3U",
             "#EXT-X-VERSION:7" if has_init else "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{target}",
             "#EXT-X-MEDIA-SEQUENCE:0",
             "#EXT-X-PLAYLIST-TYPE:VOD"]
    if has_init:
        lines.append(f'#EXT-X-MAP:URI="{sess["init_url"]}"')
    for _, abs_url, dur in segs:
        lines.append(f"#EXTINF:{dur:.3f},")
        lines.append(abs_url)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _editor_get(sid: str):
    with editor_lock:
        return editor_sessions.get(sid)


def _prefetch_manifest(url: str, referer: str = "", cookies_file: str = "") -> str:
    """Server-side fetch of an HLS/DASH manifest via curl_cffi with Chrome
    impersonation + the sniffer's captured cookies. Used when the user
    picks a variant whose playlist content wasn't already grabbed during
    the in-page sniff (the browser only fetches a variant playlist when
    you switch to that quality). The CDN typically 403s a fresh yt-dlp
    request even with --impersonate, but a curl_cffi session that mirrors
    the WebView's cookies + Origin almost always passes.

    Returns the manifest text on success, "" on any failure."""
    if not url:
        return ""
    try:
        from curl_cffi import requests as cr
    except Exception:
        return ""
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
        o = origin_of(referer)
        if o:
            headers["Origin"] = o
    cookies = {}
    if cookies_file and Path(cookies_file).exists():
        try:
            for line in Path(cookies_file).read_text().splitlines():
                if line.startswith("#") or "\t" not in line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
        except Exception:
            pass
    try:
        r = cr.get(url, headers=headers, cookies=cookies,
                   impersonate="chrome", timeout=20, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        pass
    return ""


def store_manifest(content: str) -> str:
    mid = uuid.uuid4().hex[:12]
    with manifests_lock:
        manifests[mid] = content
    return mid


def store_master_with_variants(master_content: str, variant_contents: dict,
                               host_port: str) -> str:
    """Store master + all variants we have cached. Rewrites variant URLs
    inside the master to point to /manifest/<id> on localhost so yt-dlp
    never has to hit the CDN for a manifest. Segments still hit the CDN."""
    rewritten = master_content
    for vurl, vcontent in variant_contents.items():
        if not vurl or not isinstance(vcontent, str) or not vcontent:
            continue
        vid = store_manifest(vcontent)
        local = f"http://{host_port}/manifest/{vid}"
        rewritten = rewritten.replace(vurl, local)
    return store_manifest(rewritten)


def make_job() -> dict:
    jid = uuid.uuid4().hex[:8]
    job = {
        "id": jid,
        "queue": Queue(),
        "process": None,
        "status": "pending",
        "filename": "",
    }
    with jobs_lock:
        jobs[jid] = job
    return job


def _probe_once(url: str, generic: bool, referer: str = "", cookies_file: str = "",
                cookies_browser: str = "") -> dict:
    cmd = ([YT_DLP, "-J", "--no-playlist"]
           + common_args(url, generic, referer, cookies_file, cookies_browser) + [url])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "probe failed")
    return json.loads(res.stdout)


def _probe_playlist(url: str, generic: bool, referer: str = "", cookies_file: str = "",
                    cookies_browser: str = "") -> dict:
    """Like _probe_once but lets yt-dlp expose a playlist (carousel, album)
    when there is one. Used to detect multi-asset posts."""
    cmd = ([YT_DLP, "-J", "--yes-playlist"]
           + common_args(url, generic, referer, cookies_file, cookies_browser) + [url])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if res.returncode != 0:
        msg = (res.stderr or res.stdout).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "probe failed")
    return json.loads(res.stdout)


def _normalize_url(url: str) -> str:
    """Canonicalize URLs for known multi-asset hosts so we don't probe a
    slide-deep-link instead of the whole post. e.g. Instagram URLs of
    shape /p/<short>/?img_index=4 collapse to /p/<short>/ so yt-dlp's
    playlist probe returns the full carousel."""
    try:
        from urllib.parse import urlencode, parse_qsl
        p = urlparse(url)
    except Exception:
        return url
    host = (p.hostname or "").lower()
    if host.endswith("instagram.com"):
        qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
              if k.lower() != "img_index"]
        return p._replace(query=urlencode(qs)).geturl()
    return url


def _classify_ext(ext: str) -> str:
    e = (ext or "").lower().lstrip(".")
    if e in VIDEO_EXTS: return "video"
    if e in IMAGE_EXTS: return "image"
    if e in AUDIO_EXTS: return "audio"
    return "video"  # default — unknown ext treated as video so we route through yt-dlp


def _resolve_via_gallery_dl(url: str, cookies_file: str = "", timeout: int = 90,
                            try_browser_cookies: bool = True,
                            cookies_browser: str = "") -> list:
    """Ask gallery-dl to enumerate every asset on the URL, returning a list
    of normalized items: {url, ext, kind, title, width, height, filename,
    referer, thumbnail, num, ytdl_id}.

    gallery-dl's --resolve-json output is a single top-level JSON array
    whose elements are tuples [<level>, ...]. Level 3 entries are
    downloadable files: [3, "<url>", {<metadata>}]. URLs may be prefixed
    with "ytdl:" indicating gallery-dl wants yt-dlp to resolve them at
    download time (carousel videos on Instagram, etc.).

    For auth-walled hosts (Instagram, private Twitter, etc.) we try the
    user's logged-in browser cookies. Safari's cookie file is sandboxed
    and generally inaccessible, so Chrome is the practical default."""
    if not GALLERY_DL:
        return []

    def _run(extra_args: list) -> str:
        cmd = [GALLERY_DL, "--resolve-json", "-q",
               "--option", "extractor.cookies-update=false"] + extra_args + [url]
        if cookies_file and Path(cookies_file).exists():
            cmd += ["--cookies", cookies_file]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return res.stdout or ""
        except subprocess.TimeoutExpired:
            return ""

    # If the user explicitly picked a browser, use only that. Otherwise
    # auto-iterate (Chrome → Firefox → Brave → Edge) for known gallery hosts
    # — most users have *some* signed-in browser, and we don't want to
    # burden them with picking it manually.
    raw = ""
    user_browser = _sanitize_browser(cookies_browser)
    if cookies_file and Path(cookies_file).exists():
        # Explicit cookies file → use it, no browser fallback
        raw = _run([])
    elif user_browser:
        raw = _run(["--cookies-from-browser", user_browser])
    elif try_browser_cookies:
        for browser in ("chrome", "firefox", "brave", "edge"):
            raw = _run(["--cookies-from-browser", browser])
            if raw and '"error":' not in raw[:200]:
                break
    if not raw:
        raw = _run([])
    raw = raw.strip()
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except Exception:
        return []
    if not isinstance(doc, list):
        return []
    items = []
    for entry in doc:
        if not isinstance(entry, list) or len(entry) < 3:
            continue
        if entry[0] != 3:
            continue
        item_url = entry[1] or ""
        meta = entry[2] if isinstance(entry[2], dict) else {}
        # gallery-dl prefixes URLs that need yt-dlp resolution at download
        # time (e.g. Instagram carousel videos) with "ytdl:". We strip the
        # prefix and store a flag so run_gallery_item knows to route these
        # through yt-dlp instead of aria2c.
        needs_ytdlp = False
        if item_url.startswith("ytdl:"):
            item_url = item_url[5:]
            needs_ytdlp = True
        ext = str(meta.get("extension") or "").lower()
        if not ext:
            try:
                p = urlparse(item_url).path
                if "." in p:
                    ext = p.rsplit(".", 1)[-1].lower().split("?")[0]
            except Exception:
                pass
        kind = _classify_ext(ext)
        fname = str(meta.get("filename") or meta.get("title") or meta.get("description") or "")
        # For images the URL itself IS the preview; videos use the post's
        # cover frame if available (otherwise the picker shows a fallback).
        thumb = meta.get("thumbnail") or (item_url if kind == "image" else "")
        items.append({
            "url": item_url,
            "ext": ext,
            "kind": kind,
            "needs_ytdlp": needs_ytdlp,
            # Position in the carousel/album, 1-indexed (gallery-dl provides
            # this on most extractors). Used to merge with yt-dlp results.
            "num": meta.get("num"),
            "title": str(meta.get("title") or meta.get("description") or "")[:200],
            "width": int(meta.get("width") or 0) or None,
            "height": int(meta.get("height") or 0) or None,
            "duration": meta.get("duration"),
            "filename": fname[:200],
            "referer": url,
            "webpage_url": meta.get("post_url") or url,
            "thumbnail": thumb,
        })
    return items


def _merge_gallery_with_ytdlp(g_items: list, yt_video_items: list) -> list:
    """For carousels/albums where gallery-dl knows ALL items (images + videos)
    but only has placeholder ytdl: URLs for videos, and yt-dlp knows the
    direct CDN URLs for the videos: replace each gallery-dl video item's
    URL with the next yt-dlp video's direct URL. The two lists' video
    sub-sequences are in the same order, so a positional zip is correct.

    Also fills in video thumbnails (yt-dlp gives us the cover frame; gallery-dl
    typically doesn't on Instagram)."""
    yt_iter = iter(yt_video_items)
    out = []
    for it in g_items:
        if it.get("kind") == "video" and (it.get("needs_ytdlp") or it.get("url", "").startswith("ytdl:")):
            try:
                yi = next(yt_iter)
                merged = dict(it)
                if yi.get("url"):
                    merged["url"] = yi["url"]
                    merged["needs_ytdlp"] = False
                if yi.get("thumbnail"):
                    merged["thumbnail"] = yi["thumbnail"]
                if yi.get("webpage_url"):
                    merged["webpage_url"] = yi["webpage_url"]
                if not merged.get("ext") and yi.get("ext"):
                    merged["ext"] = yi["ext"]
                if yi.get("ytdlp_id"):
                    merged["ytdlp_id"] = yi["ytdlp_id"]
                out.append(merged)
            except StopIteration:
                out.append(it)
        else:
            out.append(it)
    return out


def _yt_entries_to_items(entries: list, referer: str) -> list:
    """Normalize a yt-dlp playlist's `entries` to the same item shape as
    gallery-dl results. Used when gallery-dl can't help but yt-dlp itself
    surfaced a multi-entry playlist."""
    items = []
    for e in entries:
        if not e: continue
        # Each entry from a non-flat playlist has its own formats[]; pick the
        # best video URL (or fall back to the entry's `url` field).
        u = e.get("url") or ""
        ext = str(e.get("ext") or "").lower()
        if not u:
            fmts = e.get("formats") or []
            if fmts:
                u = fmts[-1].get("url") or ""
                ext = str(fmts[-1].get("ext") or ext).lower()
        kind = _classify_ext(ext)
        items.append({
            "url": u,
            "ext": ext,
            "kind": kind,
            "title": str(e.get("title") or e.get("id") or "")[:200],
            "width": e.get("width"),
            "height": e.get("height"),
            "duration": e.get("duration"),
            "filename": str(e.get("title") or e.get("id") or "")[:200],
            "referer": e.get("webpage_url") or referer,
            "thumbnail": e.get("thumbnail") or "",
            # Carry through the source webpage so the per-item downloader
            # can hand it back to yt-dlp instead of the raw media URL.
            "webpage_url": e.get("webpage_url") or "",
            "ytdlp_id": e.get("id") or "",
        })
    return items


def _http_get_text(url: str, referer: str = "", cookies_file: str = "",
                   timeout: int = 25) -> str:
    """Fetch a URL with Chrome impersonation. Returns the response body as
    text (decoded by curl_cffi). Empty string on any error."""
    try:
        from curl_cffi import requests as cr
    except Exception:
        return ""
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
        o = origin_of(referer)
        if o:
            headers["Origin"] = o
    cookies = {}
    if cookies_file and Path(cookies_file).exists():
        try:
            for line in Path(cookies_file).read_text().splitlines():
                if line.startswith("#") or "\t" not in line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
        except Exception:
            pass
    try:
        r = cr.get(url, headers=headers, cookies=cookies,
                   impersonate="chrome", timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return ""
        return r.text or ""
    except Exception:
        return ""


# ─── Generic-page scraper (Phase 2) ─────────────────────────────────────
# When neither yt-dlp nor gallery-dl can resolve a URL, fall back to
# fetching the page HTML and scraping it for media references and embedded
# players. Anything we find is normalized into the same item shape and
# returned to the gallery picker.

# Embed → canonical-URL mappers. Each pattern matches a `src=` attribute
# of an iframe (or other embed marker); the lambda turns the match into
# the URL we hand back to yt-dlp for proper extraction.
_EMBED_PATTERNS = [
    # Vimeo player embed → vimeo.com/<id>
    (re.compile(r'https?://player\.vimeo\.com/video/(\d+)'),
     lambda m, base: f"https://vimeo.com/{m.group(1)}"),
    # YouTube embed → standard watch URL
    (re.compile(r'https?://(?:www\.)?youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_\-]+)'),
     lambda m, base: f"https://www.youtube.com/watch?v={m.group(1)}"),
    (re.compile(r'https?://youtu\.be/([A-Za-z0-9_\-]+)'),
     lambda m, base: f"https://www.youtube.com/watch?v={m.group(1)}"),
    # Streamable
    (re.compile(r'https?://streamable\.com/(?:o/|e/|s/)?([A-Za-z0-9]+)'),
     lambda m, base: f"https://streamable.com/{m.group(1)}"),
    # Twitch clip iframe / VOD iframe
    (re.compile(r'https?://clips\.twitch\.tv/embed\?clip=([^&"\']+)'),
     lambda m, base: f"https://clips.twitch.tv/{m.group(1)}"),
    (re.compile(r'https?://player\.twitch\.tv/\?video=([0-9]+)'),
     lambda m, base: f"https://www.twitch.tv/videos/{m.group(1)}"),
    # Bitchute / Rumble / Odysee / Dailymotion
    (re.compile(r'https?://(?:www\.)?bitchute\.com/embed/([A-Za-z0-9]+)'),
     lambda m, base: f"https://www.bitchute.com/video/{m.group(1)}/"),
    (re.compile(r'https?://(?:www\.)?rumble\.com/embed/([A-Za-z0-9.]+)'),
     lambda m, base: f"https://rumble.com/embed/{m.group(1)}/"),
    (re.compile(r'https?://odysee\.com/\$/embed/([^"\']+)'),
     lambda m, base: f"https://odysee.com/{m.group(1)}"),
    (re.compile(r'https?://(?:www\.)?dailymotion\.com/embed/video/([A-Za-z0-9]+)'),
     lambda m, base: f"https://www.dailymotion.com/video/{m.group(1)}"),
    # Wistia (script-tag embed: id is the media hashed id)
    (re.compile(r'wistia[_-]async[_-]([A-Za-z0-9]+)'),
     lambda m, base: f"https://fast.wistia.net/embed/iframe/{m.group(1)}"),
    # JW Player file= attribute (rare but seen): just yield the file URL
    (re.compile(r'jwplayer\([^)]*\)\.setup\([^)]*"file"\s*:\s*"([^"]+)"'),
     lambda m, base: m.group(1)),
    # Brightcove (account_id + video_id pair)
    (re.compile(r'players\.brightcove\.net/(\d+)/[^/]+/index\.html\?videoId=(\d+)'),
     lambda m, base: f"https://players.brightcove.net/{m.group(1)}/default_default/index.html?videoId={m.group(2)}"),
    # Kaltura (entry_id parameter)
    (re.compile(r'kaltura\.com/[^"\']*entry_id[/=]([0-9_]+)'),
     lambda m, base: f"https://www.kaltura.com/index.php/extwidget/preview/entry_id/{m.group(1)}"),
]

_MEDIA_LINK_RE = re.compile(
    r'\.(mp4|m4v|webm|mkv|mov|m3u8|mpd|mp3|m4a|wav|ogg|opus|flac|jpg|jpeg|png|webp|gif|avif|heic)(\?[^"\'\s>]*)?',
    re.IGNORECASE,
)


def _abs_url(base: str, href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        scheme = (urlparse(base).scheme or "https")
        return f"{scheme}:{href}"
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        try:
            p = urlparse(base)
            return f"{p.scheme}://{p.netloc}{href}"
        except Exception:
            return href
    # Relative path
    try:
        from urllib.parse import urljoin
        return urljoin(base, href)
    except Exception:
        return href


def _attr(tag_html: str, name: str) -> str:
    """Pull a single attribute value out of a single-tag HTML fragment.
    Tolerates single quotes, double quotes, and unquoted values."""
    m = re.search(rf'\b{name}\s*=\s*"([^"]*)"', tag_html, re.IGNORECASE)
    if m: return m.group(1)
    m = re.search(rf"\b{name}\s*=\s*'([^']*)'", tag_html, re.IGNORECASE)
    if m: return m.group(1)
    m = re.search(rf'\b{name}\s*=\s*([^\s>]+)', tag_html, re.IGNORECASE)
    if m: return m.group(1)
    return ""


def _scrape_meta(html: str, base: str) -> list:
    """OpenGraph + Twitter card video / image tags."""
    out = []
    for m in re.finditer(r'<meta\b[^>]*>', html, re.IGNORECASE):
        tag = m.group(0)
        prop = (_attr(tag, "property") or _attr(tag, "name")).lower()
        content = _attr(tag, "content")
        if not content: continue
        u = _abs_url(base, content)
        if prop in ("og:video", "og:video:url", "og:video:secure_url",
                    "twitter:player:stream"):
            out.append({"url": u, "kind": "video", "_source": "meta"})
        elif prop in ("og:image", "og:image:url", "og:image:secure_url",
                      "twitter:image", "twitter:image:src"):
            out.append({"url": u, "kind": "image", "_source": "meta"})
    return out


def _scrape_video_tags(html: str, base: str) -> list:
    """Direct <video src> + <video><source src> + poster attribute."""
    out = []
    for m in re.finditer(r'<video\b([^>]*)>(.*?)</video>', html, re.IGNORECASE | re.DOTALL):
        attrs, inner = m.group(1), m.group(2)
        src = _attr("<x " + attrs + ">", "src")
        poster = _attr("<x " + attrs + ">", "poster")
        if src:
            out.append({"url": _abs_url(base, src), "kind": "video",
                        "thumbnail": _abs_url(base, poster), "_source": "video"})
        for sm in re.finditer(r'<source\b[^>]*>', inner, re.IGNORECASE):
            ssrc = _attr(sm.group(0), "src")
            if ssrc:
                out.append({"url": _abs_url(base, ssrc), "kind": "video",
                            "thumbnail": _abs_url(base, poster), "_source": "video"})
    # Standalone <source> outside a video tag (rare but happens)
    return out


def _scrape_audio_tags(html: str, base: str) -> list:
    out = []
    for m in re.finditer(r'<audio\b([^>]*)>(.*?)</audio>', html, re.IGNORECASE | re.DOTALL):
        attrs, inner = m.group(1), m.group(2)
        src = _attr("<x " + attrs + ">", "src")
        if src:
            out.append({"url": _abs_url(base, src), "kind": "audio", "_source": "audio"})
        for sm in re.finditer(r'<source\b[^>]*>', inner, re.IGNORECASE):
            ssrc = _attr(sm.group(0), "src")
            if ssrc:
                out.append({"url": _abs_url(base, ssrc), "kind": "audio", "_source": "audio"})
    return out


def _scrape_anchor_links(html: str, base: str) -> list:
    """<a href$=mp4|jpg|png|...> — direct file links."""
    out = []
    for m in re.finditer(r'<a\b([^>]*)>', html, re.IGNORECASE):
        href = _attr(m.group(0), "href")
        if not href: continue
        if not _MEDIA_LINK_RE.search(href): continue
        u = _abs_url(base, href)
        ext = (_MEDIA_LINK_RE.search(href).group(1) or "").lower()
        kind = _classify_ext(ext)
        out.append({"url": u, "kind": kind, "ext": ext, "_source": "link"})
    return out


def _scrape_images(html: str, base: str) -> list:
    """<img> tags, filtering out clearly-not-content images by name + size."""
    out = []
    for m in re.finditer(r'<img\b[^>]*>', html, re.IGNORECASE):
        tag = m.group(0)
        src = _attr(tag, "src") or _attr(tag, "data-src") or _attr(tag, "data-original")
        if not src or src.startswith("data:"):
            continue
        sl = src.lower()
        if any(k in sl for k in ("favicon", "sprite", "spacer", "pixel", "blank.gif")):
            continue
        try:
            w = int(_attr(tag, "width") or 0)
            h = int(_attr(tag, "height") or 0)
        except ValueError:
            w = h = 0
        # Skip tiny images when size is declared. Allow unknown sizes.
        if (w and w < 200) or (h and h < 200):
            continue
        u = _abs_url(base, src)
        out.append({"url": u, "kind": "image",
                    "width": w or None, "height": h or None,
                    "_source": "img"})
    return out


def _scrape_jsonld(html: str, base: str) -> list:
    """JSON-LD VideoObject / ImageObject blocks."""
    out = []
    for m in re.finditer(
        r'<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            v = stack.pop()
            if isinstance(v, list):
                stack.extend(v); continue
            if not isinstance(v, dict): continue
            t = v.get("@type", "")
            if isinstance(t, list): t = next(iter(t), "")
            if t == "VideoObject":
                u = v.get("contentUrl") or v.get("embedUrl") or ""
                thumb = v.get("thumbnailUrl") or v.get("thumbnail") or ""
                if isinstance(thumb, list): thumb = thumb[0] if thumb else ""
                if u:
                    out.append({"url": _abs_url(base, u), "kind": "video",
                                "title": str(v.get("name") or "")[:200],
                                "duration": v.get("duration"),
                                "thumbnail": _abs_url(base, thumb) if thumb else "",
                                "_source": "jsonld"})
            elif t == "ImageObject":
                u = v.get("contentUrl") or v.get("url") or ""
                if u:
                    out.append({"url": _abs_url(base, u), "kind": "image",
                                "title": str(v.get("name") or "")[:200],
                                "_source": "jsonld"})
            # Recurse into nested values
            for vv in v.values():
                if isinstance(vv, (list, dict)):
                    stack.append(vv)
    return out


def _scrape_embeds(html: str, base: str) -> list:
    """Scan iframes + script bodies for known player URLs. Returns canonical
    URLs that yt-dlp can re-probe (via the existing probe machinery)."""
    out = []
    seen = set()
    # Iframe srcs
    for m in re.finditer(r'<iframe\b[^>]*>', html, re.IGNORECASE):
        src = _attr(m.group(0), "src") or _attr(m.group(0), "data-src")
        if not src: continue
        u = _abs_url(base, src)
        for pat, mapper in _EMBED_PATTERNS:
            mm = pat.search(u)
            if mm:
                canonical = mapper(mm, base)
                if canonical not in seen:
                    seen.add(canonical)
                    out.append({"embed_url": canonical})
                break
    # Inline script bodies (Wistia / JW / Brightcove / Kaltura)
    for pat, mapper in _EMBED_PATTERNS:
        for mm in pat.finditer(html):
            canonical = mapper(mm, base)
            if canonical and canonical not in seen:
                seen.add(canonical)
                out.append({"embed_url": canonical})
    return out


def _resolve_via_generic_scrape(url: str, cookies_file: str = "",
                                resolve_embeds: bool = True) -> list:
    """Fetch the page HTML, harvest every media reference + embed we can,
    and probe each detected embed via yt-dlp. Returns a normalized item
    list ready for _gallery_response."""
    html = _http_get_text(url, cookies_file=cookies_file)
    if not html:
        return []
    base = url
    items = []

    # 1) Static media
    items += _scrape_meta(html, base)
    items += _scrape_video_tags(html, base)
    items += _scrape_audio_tags(html, base)
    items += _scrape_jsonld(html, base)
    items += _scrape_anchor_links(html, base)
    items += _scrape_images(html, base)

    # 2) Embeds — re-probe each via yt-dlp and collect their entries.
    if resolve_embeds:
        embeds = _scrape_embeds(html, base)
        # De-dup against known iframe pages we've already pulled URLs from
        for e in embeds:
            embed_url = e.get("embed_url") or ""
            if not embed_url: continue
            try:
                probe = _probe_once(embed_url, generic=False, referer=url, cookies_file=cookies_file)
            except RuntimeError:
                # Fall back to playlist mode
                try:
                    probe = _probe_playlist(embed_url, generic=False, referer=url, cookies_file=cookies_file)
                except RuntimeError:
                    continue
            if probe.get("_type") == "playlist":
                ents = [x for x in (probe.get("entries") or []) if x]
                items.extend(_yt_entries_to_items(ents, embed_url))
            else:
                fmts = probe.get("formats") or []
                if fmts:
                    best = fmts[-1]
                    items.append({
                        "url": best.get("url") or "",
                        "ext": (best.get("ext") or "").lower() or "mp4",
                        "kind": "video",
                        "title": str(probe.get("title") or "")[:200],
                        "duration": probe.get("duration"),
                        "thumbnail": probe.get("thumbnail") or "",
                        "webpage_url": embed_url,
                        "referer": url,
                        "needs_ytdlp": True,  # safer to defer to yt-dlp at download time
                        "_source": "embed",
                    })

    # 3) Normalize, dedupe, fill required fields
    seen_urls = set()
    norm = []
    for it in items:
        u = (it.get("url") or "").strip()
        if not u or u in seen_urls:
            continue
        seen_urls.add(u)
        ext = (it.get("ext") or "").lower()
        if not ext:
            try:
                p = urlparse(u).path
                if "." in p:
                    ext = p.rsplit(".", 1)[-1].lower().split("?")[0][:6]
            except Exception: pass
        kind = it.get("kind") or _classify_ext(ext)
        thumb = it.get("thumbnail") or (u if kind == "image" else "")
        norm.append({
            "url": u,
            "ext": ext,
            "kind": kind,
            "title": it.get("title") or "",
            "filename": it.get("title") or "",
            "width": it.get("width"),
            "height": it.get("height"),
            "duration": it.get("duration"),
            "referer": url,
            "webpage_url": it.get("webpage_url") or url,
            "thumbnail": thumb,
            "needs_ytdlp": bool(it.get("needs_ytdlp")),
        })
    return norm


def _proxify_thumb(thumb_url: str, referer: str) -> str:
    """Wrap a remote thumbnail URL in our local /thumb proxy so the WKWebView
    can load it. Instagram/X/Pinterest CDNs commonly refuse cross-origin
    image loads from a localhost page; routing through curl_cffi server-side
    with the right Referer/UA makes them load."""
    if not thumb_url:
        return ""
    if thumb_url.startswith(("/thumb?", "data:")):
        return thumb_url
    if not (thumb_url.startswith("http://") or thumb_url.startswith("https://")):
        return thumb_url
    return f"/thumb?u={quote(thumb_url, safe='')}&r={quote(referer or '', safe='')}"


def _gallery_response(items: list, source_url: str, info: dict | None = None,
                      generic: bool = False, low_confidence: bool = False) -> dict:
    titles = [i.get("title") for i in items if i.get("title")]
    main_title = ((info or {}).get("title")
                  or (titles[0] if titles else "")
                  or source_url)
    n_video = sum(1 for i in items if i.get("kind") == "video")
    n_image = sum(1 for i in items if i.get("kind") == "image")
    n_audio = sum(1 for i in items if i.get("kind") == "audio")
    # Proxy every thumbnail through /thumb so cross-origin CDNs (Instagram,
    # X/Twitter, Pinterest, etc.) actually load in the WebView.
    for i in items:
        ref = i.get("referer") or i.get("webpage_url") or source_url
        i["thumbnail"] = _proxify_thumb(i.get("thumbnail") or "", ref)
    # First decent thumbnail (already proxied)
    thumb = ""
    for i in items:
        if i.get("thumbnail"):
            thumb = i["thumbnail"]; break
    return {
        "kind": "gallery",
        "title": main_title,
        "uploader": (info or {}).get("uploader") or "",
        "thumbnail": thumb,
        "items": items,
        "n_video": n_video,
        "n_image": n_image,
        "n_audio": n_audio,
        "generic": generic,
        "low_confidence": low_confidence,
    }


def probe_url(url: str, referer: str = "", cookies_file: str = "",
              cookies_browser: str = "") -> dict:
    url = _normalize_url(url)
    host = (urlparse(url).hostname or "").lower()
    is_known_gallery_host = (host in GALLERY_HOSTS or
                             any(host.endswith("." + h) for h in GALLERY_HOSTS))
    is_video_likely_host = (host in VIDEO_LIKELY_HOSTS or
                            any(host.endswith("." + h) for h in VIDEO_LIKELY_HOSTS))

    def _items_are_low_confidence(items):
        """Heuristic: items are 'low confidence' if they look like the
        page's poster/thumbnail rather than the actual media. The frontend
        uses this signal to hand off to the WKWebView sniffer when the URL
        looks like a video page that our cascade misread.

        A response is weak when ALL of:
          - all items are images
          - there are at most 2 of them (real galleries are bigger)
          - AND ANY of:
              - the items came from generic-scrape meta/img tags (og:image),
              - the host is a known video host (YouTube, Vimeo, …),
              - the URL path screams 'video page' (/embed/, /watch/, …).
        On a clean Instagram /p/<short>/ single-photo post (none of those
        triggers fire), we still treat the result as the real media."""
        if not items: return False
        if any(it.get("kind") != "image" for it in items): return False
        if len(items) > 2: return False
        if all(it.get("_source") in ("meta", "img") for it in items):
            return True
        if is_video_likely_host or _url_looks_like_video_page(url):
            return True
        return False

    used_generic = False
    info = None
    yt_err = None

    if is_known_gallery_host:
        # Multi-asset hosts (Instagram carousels, Pinterest boards, Twitter
        # threads, etc.) need --yes-playlist to expose siblings. We probe
        # BOTH yt-dlp playlist mode AND gallery-dl in parallel, then merge:
        #   - yt-dlp gives direct video CDN URLs (so we can aria2c them)
        #     but on Instagram only enumerates videos, missing image slides.
        #   - gallery-dl with browser cookies enumerates EVERY slide in the
        #     carousel (images + videos) but for videos only gives a "ytdl:"
        #     placeholder. Merging gives us a complete carousel with fast
        #     direct URLs everywhere yt-dlp has them.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_yt = ex.submit(_probe_playlist, url, False, referer, cookies_file, cookies_browser)
            f_g = ex.submit(_resolve_via_gallery_dl, url, cookies_file, 90, True, cookies_browser)
            try: info = f_yt.result(timeout=120)
            except RuntimeError as e:
                if UNSUPPORTED_HINT in str(e):
                    try:
                        info = _probe_playlist(url, generic=True, referer=referer,
                                               cookies_file=cookies_file,
                                               cookies_browser=cookies_browser)
                        used_generic = True
                    except RuntimeError as e2:
                        yt_err = e2
                else:
                    yt_err = e
            except Exception as e:
                yt_err = e
            try: g_items = f_g.result(timeout=120)
            except Exception:
                g_items = []

        yt_entries = []
        if info and info.get("_type") == "playlist":
            yt_entries = [e for e in (info.get("entries") or []) if e]
        yt_video_items = _yt_entries_to_items(yt_entries, url) if yt_entries else []

        # Pick the best representation. gallery-dl wins when it has strictly
        # more items (i.e. it caught image slides yt-dlp ignored). When both
        # see the same count, prefer yt-dlp because its direct URLs are
        # ready to feed straight into aria2c.
        if len(g_items) > len(yt_video_items) and len(g_items) > 1:
            merged = _merge_gallery_with_ytdlp(g_items, yt_video_items)
            return _gallery_response(merged, source_url=url, info=info,
                                     generic=used_generic)
        if len(yt_video_items) > 1:
            return _gallery_response(yt_video_items, source_url=url, info=info,
                                     generic=used_generic)
        if len(g_items) > 1:
            return _gallery_response(g_items, source_url=url, info=info,
                                     generic=used_generic)
        # Single item — collapse to one-video flow
        if yt_entries and len(yt_entries) == 1:
            info = yt_entries[0]
        elif info and info.get("_type") == "playlist":
            info = None
    else:
        try:
            info = _probe_once(url, generic=False, referer=referer,
                               cookies_file=cookies_file, cookies_browser=cookies_browser)
        except RuntimeError as e:
            if UNSUPPORTED_HINT in str(e):
                try:
                    info = _probe_once(url, generic=True, referer=referer,
                                       cookies_file=cookies_file, cookies_browser=cookies_browser)
                    used_generic = True
                except RuntimeError as e2:
                    yt_err = e2
            else:
                yt_err = e

    # Path A — yt-dlp returned nothing usable. Try gallery-dl, then the
    # generic page scraper.
    if info is None:
        # If yt-dlp failed with an auth/age/geo error, don't paper over it
        # with the fallback chain — gallery-dl on YouTube just returns the
        # video thumbnail as a single "image", which masquerades as a real
        # gallery and prevents the user from getting the actual video. Raise
        # so the frontend's auth-retry UI fires.
        if yt_err and _classify_error(str(yt_err)) in ("auth", "age", "geo"):
            raise yt_err
        g_items = _resolve_via_gallery_dl(url, cookies_file, cookies_browser=cookies_browser)
        if g_items:
            return _gallery_response(g_items, source_url=url, generic=False,
                                     low_confidence=_items_are_low_confidence(g_items))
        # Maybe yt-dlp can see a playlist even though single-probe failed
        try:
            pl = _probe_playlist(url, generic=False, referer=referer,
                                 cookies_file=cookies_file, cookies_browser=cookies_browser)
            entries = [e for e in (pl.get("entries") or []) if e]
            if entries:
                return _gallery_response(_yt_entries_to_items(entries, url),
                                         source_url=url, info=pl, generic=False)
        except Exception:
            pass
        # Last resort: scrape the page for media + embeds.
        scraped = _resolve_via_generic_scrape(url, cookies_file)
        if scraped:
            return _gallery_response(scraped, source_url=url, generic=True,
                                     low_confidence=_items_are_low_confidence(scraped))
        raise yt_err or RuntimeError("probe failed")

    fmts = info.get("formats") or []
    heights = sorted(
        {f["height"] for f in fmts
         if f.get("height") and f.get("vcodec") not in (None, "none")},
        reverse=True,
    )
    has_audio_only = any(
        f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
        for f in fmts
    )

    # Path B — yt-dlp returned a single asset but the host commonly serves
    # carousels/albums. Re-probe with gallery-dl; if it finds more, return
    # the multi-item shape.
    if is_known_gallery_host:
        g_items = _resolve_via_gallery_dl(url, cookies_file, cookies_browser=cookies_browser)
        if len(g_items) > 1:
            return _gallery_response(g_items, source_url=url, info=info, generic=used_generic)

    # Path C — yt-dlp returned a single thing with no video formats and no
    # audio (i.e. an image-only post, or a generic page yt-dlp half-resolved).
    # Try gallery-dl, then the generic page scraper, before falling back to
    # the synthesized single-thumbnail gallery.
    if not heights and not has_audio_only:
        g_items = _resolve_via_gallery_dl(url, cookies_file, cookies_browser=cookies_browser)
        if g_items:
            return _gallery_response(g_items, source_url=url, info=info,
                                     generic=used_generic,
                                     low_confidence=_items_are_low_confidence(g_items))
        # Generic scrape: for a "no formats" yt-dlp result, the page likely has
        # iframe embeds / OG tags / direct media links yt-dlp missed.
        scraped = _resolve_via_generic_scrape(url, cookies_file)
        if scraped:
            return _gallery_response(scraped, source_url=url, info=info,
                                     generic=True,
                                     low_confidence=_items_are_low_confidence(scraped))
        thumb = info.get("thumbnail") or ""
        if thumb:
            single = [{
                "url": thumb, "ext": "jpg", "kind": "image",
                "title": info.get("title") or "",
                "filename": info.get("title") or info.get("id") or "image",
                "referer": url, "thumbnail": thumb,
                "width": None, "height": None, "duration": None,
            }]
            # Synthesised-from-thumbnail is always low confidence — yt-dlp
            # had something but couldn't extract real media; sniff is much
            # more likely to find the actual stream.
            return _gallery_response(single, source_url=url, info=info, generic=used_generic, low_confidence=True)

    # Path D — standard single-video flow
    return {
        "kind": "single",
        "title": info.get("title") or info.get("id") or "",
        "uploader": info.get("uploader") or "",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or "",
        "heights": heights,
        "has_audio_only": has_audio_only,
        "has_video": bool(heights),
        "generic": used_generic,
    }


def build_format(height, audio_only: bool) -> list:
    if audio_only:
        # The actual audio extension is set by build_container_args.
        return ["-f", "bestaudio/best"]
    if not height or height == "best":
        return ["-f", "bv*+ba/b"]
    h = int(height)
    return ["-f", f"bv*[height<={h}]+ba/b[height<={h}]/b"]


def build_container_args(container: str, audio_only: bool) -> list:
    """Container/codec args. The default 'mp4' path produces a QuickTime-friendly
    .mp4: faststart-flagged moov, AAC ADTS rewritten to MP4-AAC. 'mp4-h264' is
    the safety net when the source has codecs QuickTime can't decode (VP9, AV1)
    — we re-encode video to H.264 + audio to AAC. 'mp4-h265' uses the same
    pipe but emits HEVC (smaller files, similar quality). On Apple Silicon
    both recode paths transparently use videotoolbox HW encoders."""
    if audio_only:
        if container == "m4a":
            return ["-x", "--audio-format", "m4a", "--audio-quality", "0"]
        if container == "wav":
            return ["-x", "--audio-format", "wav"]
        if container == "flac":
            return ["-x", "--audio-format", "flac"]
        if container == "opus":
            return ["-x", "--audio-format", "opus", "--audio-quality", "0"]
        return ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    if container == "mp4-web":
        # Web-safe target: H.264 Main@4.0 + AAC-LC 48 kHz stereo.
        # `--format-sort vcodec:h264,acodec:aac` biases yt-dlp toward
        # H.264 + AAC SOURCE streams so the merge produces a file
        # that's already QuickTime-compatible — making the recode
        # pass a no-op in the common case. Only when the source
        # genuinely lacks H.264 (rare today) does VideoConvertor
        # actually run, and then it does so on VideoToolbox HW.
        if VIDEOTOOLBOX.get("h264"):
            pp = ("VideoConvertor:-c:v h264_videotoolbox "
                  "-profile:v main -level 4.0 -b:v 8M -allow_sw 1 "
                  "-pix_fmt yuv420p "
                  "-c:a aac -profile:a aac_low -ar 48000 -ac 2 -b:a 192k "
                  "-movflags +faststart")
        else:
            pp = ("VideoConvertor:-c:v libx264 -preset fast -crf 20 "
                  "-profile:v main -level 4.0 -pix_fmt yuv420p "
                  "-c:a aac -profile:a aac_low -ar 48000 -ac 2 -b:a 192k "
                  "-movflags +faststart")
        return ["--format-sort", "vcodec:h264,acodec:aac",
                "--merge-output-format", "mp4", "--recode-video", "mp4",
                "--postprocessor-args", pp]
    if container == "mp4-h264":
        # --recode-video forces a re-encode pass through ffmpeg; we override
        # its ffmpeg args via VideoConvertor: scope. videotoolbox uses bitrate
        # control rather than CRF; pick a high bitrate so quality stays near
        # the libx264 -crf 20 baseline. Audio is normalized to AAC-LC 48 kHz
        # stereo so embed services don't drop it. Same format-sort bias as
        # mp4-web — most YouTube videos have H.264 source streams natively
        # so the recode becomes a fast remux rather than a CPU-bound
        # transcode.
        if VIDEOTOOLBOX.get("h264"):
            pp = ("VideoConvertor:-c:v h264_videotoolbox -b:v 8M -allow_sw 1 "
                  "-pix_fmt yuv420p "
                  "-c:a aac -profile:a aac_low -ar 48000 -ac 2 -b:a 192k "
                  "-movflags +faststart")
        else:
            pp = ("VideoConvertor:-c:v libx264 -preset fast -crf 20 "
                  "-pix_fmt yuv420p "
                  "-c:a aac -profile:a aac_low -ar 48000 -ac 2 -b:a 192k "
                  "-movflags +faststart")
        return ["--format-sort", "vcodec:h264,acodec:aac",
                "--merge-output-format", "mp4", "--recode-video", "mp4",
                "--postprocessor-args", pp]
    if container == "mp4-h265":
        if VIDEOTOOLBOX.get("hevc"):
            pp = ("VideoConvertor:-c:v hevc_videotoolbox -b:v 6M -allow_sw 1 "
                  "-tag:v hvc1 -pix_fmt yuv420p "
                  "-c:a aac -profile:a aac_low -ar 48000 -ac 2 -b:a 192k "
                  "-movflags +faststart")
        else:
            pp = ("VideoConvertor:-c:v libx265 -preset fast -crf 22 "
                  "-tag:v hvc1 -pix_fmt yuv420p "
                  "-c:a aac -profile:a aac_low -ar 48000 -ac 2 -b:a 192k "
                  "-movflags +faststart")
        return ["--merge-output-format", "mp4", "--recode-video", "mp4",
                "--postprocessor-args", pp]
    if container == "mkv":
        return ["--merge-output-format", "mkv"]
    if container == "webm":
        return ["--merge-output-format", "webm"]
    # default: mp4 with the moov atom at the front so QuickTime can open
    # the file before the download has been fully read. yt-dlp's Merger
    # runs ffmpeg `-c copy` to combine the bv+ba streams; we add
    # `-movflags +faststart` to its output side. We also force a remux
    # pass so the flag is applied even when no merge happened.
    #
    # Audio normalization is handled by `_ensure_aac_in_place()` after
    # yt-dlp finishes — that's more reliable than yt-dlp's PP scopes
    # (`--recode-video mp4` is a no-op when the merged file is already
    # mp4, so postprocessor-args never reach ffmpeg).
    #
    # `--format-sort vcodec:h264,acodec:aac` biases yt-dlp toward AAC-
    # capable variants when the source offers them, often making the
    # post-pass a no-op (codec already AAC → nothing to do).
    return [
        "--format-sort", "vcodec:h264,acodec:aac",
        "--merge-output-format", "mp4",
        "--remux-video", "mp4",
        "--postprocessor-args", "Merger:-movflags +faststart",
        "--postprocessor-args", "VideoRemuxer:-movflags +faststart",
    ]


def _ensure_aac_in_place(path: str) -> tuple[bool, str]:
    """If `path` is an mp4 with a non-AAC audio stream, re-encode just
    the audio to AAC-LC 48 kHz stereo and replace the file in place. The
    video stream is copied (`-c:v copy`), so the cost is the audio
    re-encode only — typically faster than real-time.
    Returns (changed, summary):
      - (False, "already aac") if no work was needed,
      - (True,  "<old codec> -> aac") if a re-encode happened,
      - (False, "<error>") on probe / encode failure.
    Why this exists:
      Many sources (YouTube premium streams in particular) ship Opus
      audio. yt-dlp's default Merger uses `-c copy`, so the Opus track
      survives into our mp4. Safari and QuickTime play Opus-in-MP4 fine,
      but every web embed pipeline of consequence (Readymag, Squarespace,
      Cloudinary, Mux, Cloudflare Stream) silently drops it during their
      transcode-and-validate step. Forcing AAC here makes the file
      universally playable.
    """
    if not path or not os.path.exists(path):
        return False, "missing"
    info = _quick_audio_probe(path)
    if info.get("error"):
        # Probe couldn't read an audio stream — maybe a video-only file.
        # Don't touch it.
        return False, info.get("error", "no-probe")
    codec = (info.get("codec") or "").lower()
    if not codec or codec == "aac":
        return False, "already aac"
    # Temp file lives in the system temp dir (not next to the
    # destination) so a partial / orphaned encode never pollutes the
    # user's Downloads folder. The os.replace at the end renames
    # across filesystems if needed.
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".mp4", prefix="rr-aacnorm-")
    os.close(fd)
    cmd = [_ffmpeg_bin(), "-y", "-loglevel", "error",
           "-i", path,
           "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "copy",
           "-c:a", "aac", "-profile:a", "aac_low",
           "-ar", "48000", "-ac", "2", "-b:a", "192k",
           "-movflags", "+faststart"]
    cmd.append(tmp)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as e:
        try: os.remove(tmp)
        except Exception: pass
        return False, f"ffmpeg crash: {e}"
    if res.returncode != 0 or not os.path.exists(tmp):
        try: os.remove(tmp)
        except Exception: pass
        tail = (res.stderr or res.stdout or "").strip().splitlines()
        return False, "ffmpeg rc=%d: %s" % (res.returncode, tail[-1] if tail else "")
    try:
        os.replace(tmp, path)
    except Exception as e:
        try: os.remove(tmp)
        except Exception: pass
        return False, f"replace failed: {e}"
    return True, f"{codec} -> aac"


def _ensure_h264_in_place(path: str) -> tuple[bool, str]:
    """If `path` is an mp4 with a non-H.264 video stream (e.g. VP9 or AV1
    from a YouTube bestvideo merge), re-encode the video stream to H.264
    in place. Audio is copied — already normalized to AAC by
    _ensure_aac_in_place. Returns (changed, summary) like the audio
    sibling.

    Why this exists:
      yt-dlp's `--recode-video mp4` is a no-op when the merged file is
      already mp4, so VP9-in-mp4 / AV1-in-mp4 (the common YouTube case)
      slips through with no actual transcode. QuickTime's mp4 demuxer
      only handles H.264 / HEVC / ProRes — a VP9 track makes it raise
      "media isn't compatible" even though the .mp4 container is fine.
      Forcing H.264 here makes the file open in QuickTime and play
      everywhere mp4 normally plays.
    """
    if not path or not os.path.exists(path):
        return False, "missing"
    ffprobe = _ffmpeg_bin().replace("ffmpeg", "ffprobe")
    if not Path(ffprobe).exists():
        ffprobe = "ffprobe"
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=codec_name",
           "-of", "default=nokey=1:noprint_wrappers=1", path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        return False, f"probe crash: {e}"
    codec = (res.stdout or "").strip().lower()
    if not codec:
        return False, "no video stream"
    # h264 (a.k.a. avc1) is the QuickTime-friendly default. hevc/h265 are
    # also fine, but we don't bother re-encoding them either way — if the
    # source is HEVC, leave it.
    if codec in ("h264", "hevc", "h265"):
        return False, f"already {codec}"
    # Pick the fastest H.264 encoder available. VideoToolbox is hardware
    # on Apple Silicon — much faster than libx264 with imperceptible
    # quality difference at our bitrates. Audio is copied (already AAC
    # by the time we run, since _ensure_aac_in_place ran first).
    if VIDEOTOOLBOX.get("h264"):
        v_args = ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-allow_sw", "1"]
    else:
        v_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
    # Temp file in /tmp so orphaned encodes don't litter the user's
    # save folder. Renamed-into-place via os.replace once ffmpeg
    # finishes successfully; cleaned up on failure.
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".mp4", prefix="rr-h264norm-")
    os.close(fd)
    cmd = [_ffmpeg_bin(), "-y", "-loglevel", "error",
           "-i", path,
           "-map", "0:v:0", "-map", "0:a:0?"] + v_args + [
           "-pix_fmt", "yuv420p",
           "-c:a", "copy",
           "-movflags", "+faststart",
           tmp]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except Exception as e:
        try: os.remove(tmp)
        except Exception: pass
        return False, f"ffmpeg crash: {e}"
    if res.returncode != 0 or not os.path.exists(tmp):
        try: os.remove(tmp)
        except Exception: pass
        tail = (res.stderr or res.stdout or "").strip().splitlines()
        return False, "ffmpeg rc=%d: %s" % (res.returncode, tail[-1] if tail else "")
    try:
        os.replace(tmp, path)
    except Exception as e:
        try: os.remove(tmp)
        except Exception: pass
        return False, f"replace failed: {e}"
    return True, f"{codec} -> h264"


def safe_basename(s: str) -> str:
    s = re.sub(r'[\x00-\x1f/\\:?*"<>|]+', "_", s).strip(" .")
    return s[:180] or "video"


def run_download(job: dict, url: str, dest: str, height, audio_only: bool,
                 container: str, generic: bool = False, referer: str = "",
                 filename_hint: str = "", cookies_file: str = "",
                 cookies_browser: str = "", embed_subs: bool = True,
                 playlist_items: str = ""):
    Path(dest).mkdir(parents=True, exist_ok=True)
    if filename_hint:
        out_tpl = str(Path(dest) / f"{safe_basename(filename_hint)}.%(ext)s")
    else:
        out_tpl = str(Path(dest) / "%(title)s.%(ext)s")
    # When called with playlist_items=N, run yt-dlp in playlist mode but
    # restricted to that single carousel/playlist position. This is how
    # we pull a *specific* video out of an Instagram/Reddit/etc carousel
    # when our merged probe didn't have a direct CDN URL for it.
    pl_flag = ["--playlist-items", str(playlist_items)] if playlist_items else ["--no-playlist"]
    cmd = [YT_DLP] + pl_flag + ["--newline", "-o", out_tpl,
           # Fail fast instead of looping for minutes.
           "--retries", "3", "--fragment-retries", "3",
           "--socket-timeout", "20"]
    if FFMPEG_DIR:
        cmd += ["--ffmpeg-location", FFMPEG_DIR]
    cmd += SPEED_FLAGS
    cmd += common_args(url, generic, referer, cookies_file, cookies_browser)
    cmd += build_format(height, audio_only)
    cmd += build_container_args(container, audio_only)
    # Subtitle embedding — pulls manual + auto-generated English captions
    # and bakes them into the output container as soft tracks. mp4 only
    # supports mov_text (yt-dlp converts), mkv supports any subtitle codec.
    # Audio-only and webm don't carry subs in our pipeline so skip there.
    if embed_subs and not audio_only and container not in ("webm",):
        cmd += [
            "--write-subs", "--write-auto-subs",
            "--sub-langs", "en.*,en,en-orig",
            "--embed-subs", "--embed-chapters", "--embed-metadata",
        ]
    cmd.append(url)

    job["status"] = "running"
    q: Queue = job["queue"]
    q.put({"type": "status", "status": "running"})

    last_lines: list = []

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        job["process"] = proc
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                last_lines.append(line)
                if len(last_lines) > 50:
                    last_lines.pop(0)
            m = PROGRESS_RE.search(line)
            if not m:
                m = ARIA2_PROGRESS_RE.search(line)
            if m:
                pct = float(m.group(1))
                q.put({"type": "progress", "percent": pct, "line": line})
            elif line.startswith(("[download]", "[hlsnative]", "[generic]", "[Merger]",
                                  "[ExtractAudio]", "WARNING:", "ERROR:")):
                q.put({"type": "activity", "line": line})
            md = DEST_RE.search(line)
            if md:
                job["filename"] = md.group(1).strip()
            mm = MERGE_RE.search(line)
            if mm:
                job["filename"] = mm.group(1).strip()
            q.put({"type": "log", "line": line})
        proc.wait()
        if proc.returncode == 0:
            # Audio normalization safety net — runs after every successful
            # mp4-family download. ffprobes the output and, if the audio
            # codec isn't AAC (most commonly Opus from YouTube premium
            # streams), re-encodes audio to AAC-LC in place. No-op when
            # the source already gave us AAC, so cheap to always call.
            #
            # Video normalization runs right after — yt-dlp's
            # `--recode-video mp4` is a no-op when the merged file is
            # already mp4, so VP9 / AV1 video tracks slip through and
            # break QuickTime ("media isn't compatible"). _ensure_h264_
            # in_place forces H.264 when the codec is VP9/AV1 and is a
            # no-op when the source is already H.264 or HEVC.
            if container in ("mp4", "mp4-h264", "mp4-h265", "mp4-web"):
                fname = (job.get("filename") or "").strip()
                if fname:
                    try:
                        changed, why = _ensure_aac_in_place(fname)
                        if changed:
                            q.put({"type": "log",
                                   "line": f"[audio-normalize] {why} for web compat"})
                        else:
                            q.put({"type": "log",
                                   "line": f"[audio-normalize] skipped ({why})"})
                    except Exception as e:
                        q.put({"type": "log",
                               "line": f"[audio-normalize] warning: {e}"})
                    try:
                        q.put({"type": "status", "status": "Normalizing video codec…"})
                        changed, why = _ensure_h264_in_place(fname)
                        if changed:
                            q.put({"type": "log",
                                   "line": f"[video-normalize] {why} for QuickTime compat"})
                        else:
                            q.put({"type": "log",
                                   "line": f"[video-normalize] skipped ({why})"})
                    except Exception as e:
                        q.put({"type": "log",
                               "line": f"[video-normalize] warning: {e}"})
            job["status"] = "done"
            # Record this rip in the history log so the user can find,
            # re-rip, or reveal it later. Best-effort — never blocks the
            # success notification on a write failure.
            try:
                history_record(
                    title=filename_hint or "",
                    url=url,
                    file_path=job.get("filename", ""),
                    container=container,
                    height=height,
                    audio_only=audio_only,
                )
            except Exception:
                pass
            q.put({"type": "done", "filename": job["filename"]})
        else:
            err_idx = -1
            for i in range(len(last_lines) - 1, -1, -1):
                if last_lines[i].startswith("ERROR:"):
                    err_idx = i; break
            ctx = ""
            if err_idx >= 0:
                start = max(0, err_idx - 4)
                ctx = "\n".join(last_lines[start:err_idx + 1])
            elif last_lines:
                ctx = "\n".join(last_lines[-5:])
            ctx = ctx or f"yt-dlp exited {proc.returncode}"
            if len(ctx) > 600:
                ctx = ctx[-600:]
            kind = _classify_error(ctx)
            ctx = _augment_error_hint(ctx)
            job["status"] = "error"
            q.put({"type": "error", "error": ctx, "hint": kind})
    except Exception as e:
        job["status"] = "error"
        q.put({"type": "error", "error": str(e)})
    finally:
        q.put({"type": "_close"})


def kill_job(job: dict):
    proc = job.get("process")
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, 15)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def _aria2c_download(job: dict, item_url: str, dest_dir: str,
                     filename: str, referer: str = "",
                     cookies_file: str = "") -> str:
    """Download a single direct-HTTP file as fast as possible. Returns
    the resulting file path. Pushes progress events on job["queue"]."""
    q: Queue = job["queue"]
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    out_name = safe_basename(filename)
    out_path = str(Path(dest_dir) / out_name)
    if not ARIA2C:
        # Fall back to a single-stream Python download — slower but works.
        return _python_stream_download(job, item_url, out_path, referer, cookies_file)
    cmd = [ARIA2C,
           "-x", "16", "-s", "16", "-k", "1M",
           "--max-tries=3", "--retry-wait=1",
           "--console-log-level=warn",
           "--summary-interval=1",
           "--file-allocation=none",
           "--auto-file-renaming=true",
           "--allow-overwrite=true",
           "--user-agent", BROWSER_UA,
           "-d", dest_dir, "-o", out_name,
           item_url]
    if referer:
        cmd += ["--referer", referer]
    if cookies_file and Path(cookies_file).exists():
        cmd += ["--load-cookies", cookies_file]

    job["status"] = "running"
    q.put({"type": "status", "status": "running"})
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, start_new_session=True)
    job["process"] = proc
    last_err = []
    final_path = ""
    for line in proc.stdout:
        line = line.rstrip()
        if not line: continue
        last_err.append(line)
        if len(last_err) > 30: last_err.pop(0)
        m = ARIA2_PROGRESS_RE.search(line)
        if m:
            try:
                pct = float(m.group(1))
                q.put({"type": "progress", "percent": pct, "line": line})
            except Exception: pass
        if line.startswith(("ERROR", "WARN")):
            q.put({"type": "activity", "line": line})
        # aria2c summary line: "Download Results:" then a table; the resulting
        # path appears as "<gid> OK XYZ <path>" in some versions. We track the
        # planned out_path instead of parsing.
    proc.wait()
    if proc.returncode == 0:
        final_path = out_path
        job["filename"] = final_path
    else:
        ctx = "\n".join(last_err[-8:]) or f"aria2c exited {proc.returncode}"
        raise RuntimeError(ctx)
    return final_path


def _python_stream_download(job: dict, url: str, out_path: str,
                            referer: str = "", cookies_file: str = "") -> str:
    """Curl_cffi-impersonated streaming download as a fallback when aria2c
    isn't available. No parallelism; only used as last resort."""
    from curl_cffi import requests as cr
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    cookies = {}
    if cookies_file and Path(cookies_file).exists():
        try:
            for line in Path(cookies_file).read_text().splitlines():
                if line.startswith("#") or "\t" not in line: continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
        except Exception: pass
    q: Queue = job["queue"]
    q.put({"type": "status", "status": "running"})
    with cr.get(url, headers=headers, cookies=cookies, impersonate="chrome",
                stream=True, timeout=60) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} on {url}")
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 17):
                if chunk:
                    f.write(chunk)
                    got += len(chunk)
                    if total > 0:
                        pct = got * 100.0 / total
                        q.put({"type": "progress", "percent": pct, "line": f"{got}/{total}"})
    job["filename"] = out_path
    return out_path


SIPS = shutil.which("sips")  # macOS-native image converter; handles HEIC


def _convert_image(src_path: str, target_format: str) -> str:
    """Convert an image to the requested format using sips (preferred — it
    handles HEIC, which most ffmpeg builds don't) or ffmpeg as a fallback.
    Returns the new file path on success; the original on failure or when
    target_format is empty/'original'."""
    fmt = (target_format or "").lower().strip()
    if not fmt or fmt == "original":
        return src_path
    if fmt not in ("jpeg", "jpg", "png", "webp"):
        return src_path
    new_ext = "jpg" if fmt == "jpeg" else fmt
    base = src_path.rsplit(".", 1)[0]
    new_path = f"{base}.{new_ext}"
    if new_path == src_path:
        return src_path
    if SIPS:
        sips_fmt = {"jpeg": "jpeg", "jpg": "jpeg", "png": "png", "webp": "webp"}[fmt]
        try:
            r = subprocess.run([SIPS, "-s", "format", sips_fmt,
                                src_path, "--out", new_path],
                               capture_output=True, timeout=30)
            if r.returncode == 0 and Path(new_path).exists():
                try: os.remove(src_path)
                except Exception: pass
                return new_path
        except Exception: pass
    if _FFMPEG:
        try:
            r = subprocess.run([_FFMPEG, "-y", "-loglevel", "error",
                                "-i", src_path, "-frames:v", "1", new_path],
                               capture_output=True, timeout=30)
            if r.returncode == 0 and Path(new_path).exists():
                try: os.remove(src_path)
                except Exception: pass
                return new_path
        except Exception: pass
    return src_path


def run_gallery_item(job: dict, item: dict, dest_dir: str,
                     height=None, audio_only: bool = False,
                     container: str = "mp4", cookies_file: str = "",
                     cookies_browser: str = "", embed_subs: bool = True,
                     image_format: str = ""):
    """Download a single MediaSet item. Routes:
      - needs_ytdlp flag → yt-dlp pipeline (carousel videos gallery-dl couldn't
        resolve directly; e.g. Instagram items where we lacked a yt-dlp pair).
      - kind=video with manifest (m3u8/mpd) → yt-dlp + aria2c fragments
      - kind=video / image direct HTTP URL → aria2c (parallel chunks)
    """
    q: Queue = job["queue"]
    try:
        webpage = item.get("webpage_url") or ""
        item_url = item.get("url") or ""
        kind = item.get("kind") or "video"
        ext = (item.get("ext") or "").lower()
        referer = item.get("referer") or webpage or ""
        title = item.get("filename") or item.get("title") or item.get("ytdlp_id") or "media"
        needs_ytdlp = bool(item.get("needs_ytdlp")) or item_url.startswith("ytdl:")
        if item_url.startswith("ytdl:"):
            item_url = item_url[5:]

        is_manifest = ext in ("m3u8", "mpd") or "/m3u8" in item_url or "/manifest" in item_url

        # yt-dlp path: gallery-dl marked this item as needing yt-dlp resolution
        # (e.g. Instagram carousel videos the merge didn't have a direct URL
        # for), or it's a streaming manifest. For carousels, gallery-dl's
        # `num` is the 1-indexed slide position — pass it as
        # --playlist-items so yt-dlp picks the right slide rather than
        # iterating the whole carousel and crashing on image-only entries.
        if needs_ytdlp or is_manifest:
            target_url = webpage or item_url or referer
            pl_items = ""
            if needs_ytdlp:
                num = item.get("num")
                if isinstance(num, int) and num > 0:
                    pl_items = str(num)
            run_download(job, target_url, dest_dir, height, audio_only,
                         container, generic=False, referer=referer,
                         filename_hint=title, cookies_file=cookies_file,
                         cookies_browser=cookies_browser, embed_subs=embed_subs,
                         playlist_items=pl_items)
            return

        # Direct HTTP file → aria2c. Append the right extension.
        if not ext:
            ext = "mp4" if kind == "video" else ("mp3" if kind == "audio" else "jpg")
        fname = f"{safe_basename(title)}.{ext}"
        out_path = _aria2c_download(job, item_url, dest_dir, fname,
                                    referer=referer, cookies_file=cookies_file)
        # Optional image format conversion (HEIC → JPG/PNG/WebP, etc.)
        if kind == "image" and image_format and image_format.lower() != "original":
            new_path = _convert_image(out_path, image_format)
            if new_path != out_path:
                out_path = new_path
                job["filename"] = out_path
        job["status"] = "done"
        # Add to history so this gallery item shows up in the History
        # panel just like a yt-dlp rip would. Source URL is the parent
        # webpage (the post / album page), so re-rip from history takes
        # the user back to the carousel picker rather than the dead
        # one-shot CDN URL we got the asset from.
        try:
            history_record(
                title=title,
                url=webpage or item_url,
                file_path=out_path,
                container=ext,
                height=height,
                audio_only=bool(audio_only) if kind == "audio" else False,
            )
        except Exception:
            pass
        q.put({"type": "done", "filename": out_path})
    except Exception as e:
        job["status"] = "error"
        q.put({"type": "error", "error": str(e)})
    finally:
        q.put({"type": "_close"})


_FFMPEG_PROGRESS_RE = re.compile(r"out_time_ms=(\d+)")


def _ffmpeg_bin() -> str:
    return _FFMPEG or "ffmpeg"


def _quality_to_height(q: str):
    """Translate a quality token to a target output height. None = source."""
    if not q:
        return None
    q = str(q).strip().lower().rstrip("p")
    if q in ("", "source", "best", "auto"):
        return None
    try:
        n = int(q)
        return n if n > 0 else None
    except ValueError:
        return None


def _quick_audio_probe(path: str) -> dict:
    """ffprobe the first audio stream of `path` and return a small summary
    we can surface to the UI. Lets us verify, after every encode, that the
    output actually has playable AAC-LC at a web-safe sample rate — and
    tell the user when something looks off. Returns {} on probe failure."""
    ffprobe = _ffmpeg_bin().replace("ffmpeg", "ffprobe")
    if not Path(ffprobe).exists():
        ffprobe = "ffprobe"
    cmd = [ffprobe, "-v", "error", "-select_streams", "a:0",
           "-show_entries",
           "stream=codec_name,profile,sample_rate,channels,channel_layout,bit_rate,duration",
           "-of", "json", path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return {"error": f"ffprobe rc={res.returncode}"}
        data = json.loads(res.stdout or "{}")
        streams = data.get("streams") or []
        if not streams:
            return {"error": "no audio stream"}
        s = streams[0]
        return {
            "codec":          s.get("codec_name") or "",
            "profile":        s.get("profile") or "",
            "sample_rate":    s.get("sample_rate") or "",
            "channels":       s.get("channels") or 0,
            "channel_layout": s.get("channel_layout") or "",
            "bit_rate":       s.get("bit_rate") or "",
            "duration":       s.get("duration") or "",
        }
    except Exception as e:
        return {"error": str(e)}


def _build_vf_chain(crop: dict | None, target_h: int | None) -> list:
    """Build the ffmpeg `-vf` filter chain combining an optional crop
    rect (sanitized to even integers — libx264 / yuv420p requires it)
    with an optional scale to target height. Returns a list of args
    suitable for splatting into a command. Returns [] if neither is set.
    crop dict shape: {x, y, w, h} in source pixels."""
    parts = []
    if crop and isinstance(crop, dict):
        try:
            cw = max(2, int(crop.get("w") or 0))
            ch = max(2, int(crop.get("h") or 0))
            cx = max(0, int(crop.get("x") or 0))
            cy = max(0, int(crop.get("y") or 0))
            # Round to even for chroma alignment.
            cw -= (cw % 2); ch -= (ch % 2)
            cx -= (cx % 2); cy -= (cy % 2)
            if cw > 0 and ch > 0:
                parts.append(f"crop={cw}:{ch}:{cx}:{cy}")
        except (TypeError, ValueError):
            pass
    if target_h:
        parts.append(f"scale=-2:{target_h}")
    return ["-vf", ",".join(parts)] if parts else []


def run_clip(job: dict, src_url: str, out_path: str, start: float,
             end: float, container: str, quality: str = "source",
             crop: dict | None = None):
    """Frame-accurate trim via ffmpeg. We re-encode (no -c copy) so the
    cut is exact regardless of keyframe alignment in the source. Reads
    from the local proxy so cookies/Referer concerns are already handled.
    `crop` (if set) is applied via ffmpeg's `crop` filter before any
    scale step — output dimensions become the crop's w×h (or scaled)."""
    duration = max(0.001, end - start)
    cmd = [_ffmpeg_bin(), "-y",
           "-ss", f"{start:.3f}",
           "-i", src_url,
           "-t", f"{duration:.3f}",
           "-progress", "pipe:1", "-nostats", "-loglevel", "error"]
    target_h = _quality_to_height(quality)
    # crop (if any) chained with scale — single -vf arg.
    scale_args = _build_vf_chain(crop, target_h)
    # Pick H.264 encoder: videotoolbox on Apple Silicon is 5-10× faster
    # than libx264 with no perceptible quality loss at 8 Mbps.
    if VIDEOTOOLBOX.get("h264"):
        h264_v = ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-allow_sw", "1"]
    else:
        h264_v = ["-c:v", "libx264", "-preset", "fast", "-crf", "20"]
    if VIDEOTOOLBOX.get("hevc"):
        hevc_v = ["-c:v", "hevc_videotoolbox", "-b:v", "6M", "-allow_sw", "1", "-tag:v", "hvc1"]
    else:
        hevc_v = ["-c:v", "libx265", "-preset", "fast", "-crf", "22", "-tag:v", "hvc1"]
    # Web-embed services (Readymag, Squarespace, Webflow, etc.) re-validate
    # uploads and silently drop audio if it's HE-AAC, weird sample rates,
    # or 5.1+ channel layouts. Force the universally-supported AAC-LC at
    # 48 kHz stereo so the output Just Plays everywhere — including HTML5
    # <video> in every modern browser. yuv420p is similar insurance for
    # the video stream (videotoolbox will sometimes hand back a non-yuv420
    # pixel format depending on source).
    web_audio = ["-c:a", "aac", "-profile:a", "aac_low",
                 "-ar", "48000", "-ac", "2", "-b:a", "192k"]
    web_pix = ["-pix_fmt", "yuv420p"]
    # Explicit stream mapping — `?` makes audio optional so a video-only
    # source doesn't fail. Without -map ffmpeg sometimes picks unexpected
    # streams when the source has multiple audio tracks.
    map_args = ["-map", "0:v:0", "-map", "0:a:0?"]
    if container == "mp4-web":
        # Bulletproof preset for upload services: libx264 Main@4.0 +
        # normalized AAC. Slightly slower than the videotoolbox-backed
        # mp4 option but guaranteed to play in any HTML5 player.
        # Extra hardening for picky web players (Readymag, Squarespace's
        # transcoder, etc.):
        #   -fps_mode cfr            Force constant frame rate. Some web
        #                            players desync or drop audio on VFR.
        #   -af aresample=async=1    Resample audio to keep it locked to
        #                            video PTS — fixes sync drift that
        #                            silent-audio bugs are often blamed on.
        #   -disposition:a:0 default Mark our audio track as the default;
        #                            web players occasionally pick a
        #                            non-default first track and play silence.
        #   -shortest                End both tracks together — avoids a
        #                            trailing audio-only segment that some
        #                            players treat as "no audio at all".
        cmd += (map_args + scale_args
                + ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                   "-profile:v", "main", "-level", "4.0"]
                + web_pix
                + ["-fps_mode", "cfr"]
                + web_audio
                + ["-af", "aresample=async=1:first_pts=0",
                   "-disposition:a:0", "default", "-shortest"]
                + ["-movflags", "+faststart", "-fflags", "+genpts"])
    elif container == "mp4-h264" or container == "mp4":
        cmd += (map_args + scale_args + h264_v + web_pix + web_audio
                + ["-movflags", "+faststart", "-fflags", "+genpts"])
    elif container == "mp4-h265":
        cmd += (map_args + scale_args + hevc_v + web_pix + web_audio
                + ["-movflags", "+faststart", "-fflags", "+genpts"])
    elif container == "mkv":
        cmd += map_args + scale_args + h264_v + ["-c:a", "aac", "-b:a", "192k"]
    elif container == "webm":
        cmd += (map_args + scale_args
                + ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32",
                   "-c:a", "libopus", "-b:a", "160k"])
    else:
        cmd += (map_args + scale_args + h264_v + web_pix + web_audio
                + ["-movflags", "+faststart"])
    cmd.append(out_path)

    job["status"] = "running"
    job["filename"] = out_path
    q: Queue = job["queue"]
    q.put({"type": "status", "status": "running"})
    q.put({"type": "filename", "filename": out_path})

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        job["process"] = proc
        total_ms = duration * 1_000_000.0
        for line in proc.stdout:
            line = line.rstrip()
            m = _FFMPEG_PROGRESS_RE.search(line)
            if m and total_ms > 0:
                pct = min(100.0, int(m.group(1)) / total_ms * 100.0)
                q.put({"type": "progress", "percent": pct})
        rc = proc.wait()
        if rc == 0:
            # Surface a short audio-stream summary so the user can verify the
            # output without leaving the app. Especially useful when a 3rd
            # party (Readymag, Squarespace, Webflow) silently refuses to
            # play audio — comparing the actual encoded codec/profile/rate
            # against what we *intended* to emit makes the cause obvious.
            try:
                info = _quick_audio_probe(out_path)
            except Exception:
                info = {}
            q.put({"type": "audio_info", "info": info})
            job["status"] = "done"
            q.put({"type": "done", "path": out_path, "audio": info})
        else:
            job["status"] = "error"
            q.put({"type": "error", "error": f"ffmpeg exited {rc}"})
    except Exception as e:
        job["status"] = "error"
        q.put({"type": "error", "error": str(e)})
    finally:
        q.put({"type": "_close"})


def _probe_video_dims(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream of `path`. Falls
    back to (1280, 720) if probe fails — that's a sensible default for
    web export and avoids crashing the concat pipeline on edge cases."""
    ffprobe = _ffmpeg_bin().replace("ffmpeg", "ffprobe")
    if not Path(ffprobe).exists():
        ffprobe = "ffprobe"
    cmd = [ffprobe, "-v", "error",
           "-select_streams", "v:0",
           "-show_entries", "stream=width,height",
           "-of", "csv=print_section=0:nk=1",
           path]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            parts = res.stdout.strip().split(",")
            if len(parts) >= 2:
                return (int(parts[0]), int(parts[1]))
    except Exception:
        pass
    return (1280, 720)


def run_concat(job: dict, src_url: str, clips: list, out_path: str):
    """Concatenate multiple time ranges from a single source into one
    output file. Each clip can have its own crop. We use ffmpeg's
    filter_complex with `trim`/`atrim`+`setpts`/`asetpts`+optional
    `crop`+`scale` chains, then a `concat` filter — single pass, no
    intermediate files, web-safe codec params on output.

    Output dimensions = the FIRST clip's effective size (post-crop).
    Subsequent clips are scaled to match for concat compatibility.
    """
    if not clips:
        raise RuntimeError("no clips supplied")

    # Source dims for clips that don't crop.
    src_w, src_h = _probe_video_dims(src_url)

    # Helper to round even (libx264/yuv420p alignment).
    def even(v: int) -> int:
        v = max(2, int(v))
        return v - (v % 2)

    # Compute target output dimensions from the first clip's crop or source.
    first = clips[0] or {}
    fc = first.get("crop") if isinstance(first.get("crop"), dict) else None
    if fc and fc.get("w") and fc.get("h"):
        target_w = even(fc["w"]); target_h = even(fc["h"])
    else:
        target_w = even(src_w); target_h = even(src_h)

    # Build the filter_complex graph.
    # For each clip i:
    #   [0:v]trim=...,setpts=PTS-STARTPTS[,crop=...],scale=W:H[v{i}];
    #   [0:a]atrim=...,asetpts=PTS-STARTPTS[a{i}];
    # Then [v0][a0][v1][a1]...concat=n=N:v=1:a=1[outv][outa]
    parts = []
    concat_in = []
    total_dur = 0.0
    for i, clip in enumerate(clips):
        s = float(clip.get("start") or 0.0)
        e = float(clip.get("end") or 0.0)
        if e <= s:
            raise RuntimeError(f"clip {i+1}: end must be > start")
        total_dur += (e - s)
        v_chain = [f"[0:v]trim=start={s:.3f}:end={e:.3f}",
                   "setpts=PTS-STARTPTS"]
        cr = clip.get("crop") if isinstance(clip.get("crop"), dict) else None
        if cr and cr.get("w") and cr.get("h"):
            cw = even(cr.get("w") or 0)
            ch = even(cr.get("h") or 0)
            cx = max(0, int(cr.get("x") or 0)); cx -= (cx % 2)
            cy = max(0, int(cr.get("y") or 0)); cy -= (cy % 2)
            v_chain.append(f"crop={cw}:{ch}:{cx}:{cy}")
        # Always scale to target so concat sees uniform dimensions.
        v_chain.append(f"scale={target_w}:{target_h}:force_original_aspect_ratio=disable")
        parts.append(",".join(v_chain) + f"[v{i}]")
        # Audio chain. `?` would be nice but atrim doesn't accept missing
        # streams; we trust sources have audio (web-safe path normalises
        # to AAC LC anyway).
        parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS,"
                     f"aresample=48000:async=1:first_pts=0[a{i}]")
        concat_in.append(f"[v{i}][a{i}]")

    n = len(clips)
    parts.append("".join(concat_in) + f"concat=n={n}:v=1:a=1[outv][outa]")
    fc_str = ";".join(parts)

    cmd = [_ffmpeg_bin(), "-y",
           "-i", src_url,
           "-filter_complex", fc_str,
           "-map", "[outv]", "-map", "[outa]",
           "-progress", "pipe:1", "-nostats", "-loglevel", "error",
           # Web-safe encode for the concatenated output (matches mp4-web).
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-profile:v", "main", "-level", "4.0", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-profile:a", "aac_low",
           "-ar", "48000", "-ac", "2", "-b:a", "192k",
           "-disposition:a:0", "default", "-shortest",
           "-movflags", "+faststart", "-fflags", "+genpts",
           out_path]

    job["status"] = "running"
    job["filename"] = out_path
    q: Queue = job["queue"]
    q.put({"type": "status", "status": f"concatenating {n} clips"})
    q.put({"type": "filename", "filename": out_path})

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
        job["process"] = proc
        total_ms = total_dur * 1_000_000.0
        for line in proc.stdout:
            line = line.rstrip()
            m = _FFMPEG_PROGRESS_RE.search(line)
            if m and total_ms > 0:
                pct = min(100.0, int(m.group(1)) / total_ms * 100.0)
                q.put({"type": "progress", "percent": pct})
        rc = proc.wait()
        if rc == 0:
            try:
                info = _quick_audio_probe(out_path)
            except Exception:
                info = {}
            q.put({"type": "audio_info", "info": info})
            job["status"] = "done"
            q.put({"type": "done", "path": out_path, "audio": info})
        else:
            job["status"] = "error"
            q.put({"type": "error", "error": f"ffmpeg exited {rc}"})
    except Exception as e:
        job["status"] = "error"
        q.put({"type": "error", "error": str(e)})
    finally:
        q.put({"type": "_close"})


def run_still(src_url: str, out_path: str, t: float, fmt: str,
              quality: str = "source", crop: dict | None = None):
    """Single-frame grab. JPEG = -q:v 2 (visually lossless-ish, small),
    PNG = lossless. Reads via the local proxy. Optional crop is applied
    via ffmpeg's `crop` filter, chained with any scale step."""
    cmd = [_ffmpeg_bin(), "-y", "-loglevel", "error",
           "-ss", f"{t:.3f}", "-i", src_url,
           "-frames:v", "1"]
    target_h = _quality_to_height(quality)
    cmd += _build_vf_chain(crop, target_h)
    if fmt == "png":
        cmd += ["-c:v", "png"]
    else:
        cmd += ["-c:v", "mjpeg", "-q:v", "2"]
    cmd.append(out_path)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "ffmpeg failed").strip().splitlines()[-1])


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(n)) if n else {}

    # ---- Editor proxy helpers --------------------------------------------
    def _relay_response(self, r):
        """Forward a curl_cffi streaming response body + headers to the client.
        Used by all /hls/* and /proxy/* handlers."""
        try:
            self.send_response(r.status_code)
            keep = ("content-type", "content-length", "content-range",
                    "accept-ranges", "last-modified", "etag")
            for k, v in r.headers.items():
                if k.lower() in keep:
                    self.send_header(k, v)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        self.wfile.write(chunk)
                try: self.wfile.flush()
                except Exception: pass
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return
        finally:
            try: r.close()
            except Exception: pass

    def _serve_local_playlist(self, sid, sess):
        """Synthesize a VOD m3u8 whose segments resolve to /hls/<sid>/seg/<n>
        on this same server. The editor's <video> element loads this playlist
        via hls.js (or natively on Safari)."""
        segs = sess["segments"]
        has_init = bool(sess.get("init_url"))
        target = max(1, int(max((d for _, _, d in segs), default=6.0))) + 1
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7" if has_init else "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        if has_init:
            lines.append(f'#EXT-X-MAP:URI="/hls/{sid}/init/0"')
        for idx, _, dur in segs:
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(f"/hls/{sid}/seg/{idx}")
        lines.append("#EXT-X-ENDLIST")
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _proxy_hls_segment(self, sess, n):
        segs = sess["segments"]
        if n < 0 or n >= len(segs):
            self.send_response(404); self.end_headers(); return
        _, abs_url, _ = segs[n]
        range_h = self.headers.get("Range") or ""
        try:
            r = _proxy_get(abs_url, sess.get("cookies") or [],
                           range_header=range_h, stream=True)
        except Exception:
            self.send_response(502); self.end_headers(); return
        self._relay_response(r)

    def _proxy_hls_init(self, sess):
        init_url = sess.get("init_url")
        if not init_url:
            self.send_response(404); self.end_headers(); return
        range_h = self.headers.get("Range") or ""
        try:
            r = _proxy_get(init_url, sess.get("cookies") or [],
                           range_header=range_h, stream=True)
        except Exception:
            self.send_response(502); self.end_headers(); return
        self._relay_response(r)

    def _proxy_mp4(self, sess):
        src = sess.get("src_url")
        if not src:
            self.send_response(404); self.end_headers(); return
        range_h = self.headers.get("Range") or ""
        try:
            r = _proxy_get(src, sess.get("cookies") or [],
                           range_header=range_h, stream=True)
        except Exception:
            self.send_response(502); self.end_headers(); return
        self._relay_response(r)

    def _serve_cached_file(self, sess):
        """Serve sess['cached_path'] with HTTP Range support so the editor
        player can swap to a local-disk source once the background prefetch
        finishes. Range parsing here is the same shape the browser uses for
        seeking, just pointed at a local file instead of an upstream proxy.

        Returns 503 with Retry-After if the cache isn't ready yet — the
        frontend polls /editor/cache-status and only swaps to /cached/<sid>
        after that endpoint goes ready, so 503 should be unreachable in
        practice. Belt-and-suspenders for race conditions."""
        cached = sess.get("cached_path")
        if not cached or not os.path.exists(cached):
            self.send_response(503)
            self.send_header("Retry-After", "2")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        try:
            file_size = os.path.getsize(cached)
        except OSError:
            self.send_response(503)
            self.send_header("Retry-After", "2")
            self.end_headers()
            return
        # Parse `Range: bytes=START-END` (single-range only — multi-range is
        # legal HTTP but no <video> implementation actually emits it).
        range_h = self.headers.get("Range") or ""
        start, end = 0, file_size - 1
        is_partial = False
        if range_h.startswith("bytes="):
            try:
                spec = range_h[6:].split(",", 1)[0].strip()
                s, _, e = spec.partition("-")
                if s:
                    start = int(s)
                end = int(e) if e else file_size - 1
                end = min(end, file_size - 1)
                if start < 0 or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                is_partial = True
            except (ValueError, AttributeError):
                # Malformed Range header — fall through to a 200 with the
                # full body. Browsers will retry with a sane range.
                start, end, is_partial = 0, file_size - 1, False
        length = end - start + 1
        self.send_response(206 if is_partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if is_partial:
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{file_size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            with open(cached, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    remaining -= len(chunk)
        except Exception:
            return

    def do_OPTIONS(self):
        # CORS preflight for /queue (the bookmarklet) and /thumb (used by
        # gallery picker tiles loading from any origin if proxied externally).
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/history":
            # Return the persistent rip log, newest first. Frontend
            # renders this in the History panel below the main stages.
            entries = _history_load()
            # Annotate each with a "still exists on disk" flag so the
            # UI can dim out entries whose files were moved/deleted.
            for e in entries:
                try:
                    e["exists"] = bool(e.get("file_path") and os.path.exists(e["file_path"]))
                except Exception:
                    e["exists"] = False
            entries.sort(key=lambda x: x.get("ts", 0), reverse=True)
            return self._json(200, {"entries": entries})

        if path == "/versions":
            # Versions of every external tool we depend on, plus the
            # plugin / app-data directory so the settings panel can
            # show users where things live. Also a snapshot of which
            # browsers are installed (so the cookie picker doesn't
            # offer Vivaldi to someone who doesn't have it).
            installed_browsers = []
            for key, app_path in [
                ("chrome",   "/Applications/Google Chrome.app"),
                ("safari",   "/Applications/Safari.app"),
                ("firefox",  "/Applications/Firefox.app"),
                ("brave",    "/Applications/Brave Browser.app"),
                ("edge",     "/Applications/Microsoft Edge.app"),
                ("chromium", "/Applications/Chromium.app"),
                ("opera",    "/Applications/Opera.app"),
                ("vivaldi",  "/Applications/Vivaldi.app"),
            ]:
                if Path(app_path).exists():
                    installed_browsers.append(key)
            ffmpeg_v = ""
            try:
                res = subprocess.run([_ffmpeg_bin(), "-version"],
                                     capture_output=True, text=True, timeout=5)
                first = (res.stdout or res.stderr or "").splitlines()[:1]
                if first:
                    # e.g. "ffmpeg version 7.0 Copyright ...". Take the
                    # token after "version".
                    parts = first[0].split()
                    if "version" in parts:
                        i = parts.index("version")
                        if i + 1 < len(parts):
                            ffmpeg_v = parts[i + 1]
            except Exception:
                pass
            try:
                snap = _ytdlp_check_versions(force=False)
                ytdlp_v = snap.get("installed", "")
            except Exception:
                ytdlp_v = ""
            return self._json(200, {
                "app":     APP_VERSION,
                "ffmpeg":  ffmpeg_v,
                "yt_dlp":  ytdlp_v,
                "downloads_dir": DEFAULT_DEST,
                "plugin_dir":    str(YTDLP_PLUGIN_DIR),
                "data_dir":      str(APP_SUPPORT),
                "installed_browsers": installed_browsers,
            })

        if path == "/yt-dlp/version":
            # Return installed + latest available yt-dlp versions. Optional
            # ?force=1 bypasses the 6h cache. Network-bound — calls GitHub.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            force = (qs.get("force", [""])[0] or "").strip() in ("1", "true", "yes")
            try:
                snap = _ytdlp_check_versions(force=force)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            installed = snap.get("installed", "")
            latest = snap.get("latest", "")
            update_available = _ytdlp_update_available(installed, latest)
            return self._json(200, {
                "installed": installed,
                "latest": latest,
                "update_available": update_available,
                "checked_at": snap.get("checked", 0),
            })

        if path == "/imdb/title":
            # Hit IMDB's auto-suggest endpoint (the API the homepage's
            # search box uses). Returns clean JSON with title + kind
            # hint, and crucially it doesn't trip the WAF that
            # imdb.com/title/<id>/ does — that page now returns 202
            # to non-browser clients pending a JS challenge. The
            # suggest API is auth-free and keys-free.
            #   https://v3.sg.media-imdb.com/suggestion/t/<id>.json
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            tt = (qs.get("id", [""])[0] or "").strip().lower()
            if not re.match(r"^tt\d+$", tt):
                return self._json(400, {"error": "invalid id"})
            api_url = f"https://v3.sg.media-imdb.com/suggestion/t/{tt}.json"
            try:
                status, body = _simple_get(api_url)
            except Exception as e:
                return self._json(502, {"error": f"imdb fetch failed: {e}"})
            if status != 200:
                return self._json(502, {"error": f"imdb HTTP {status}"})
            try:
                data = json.loads(body)
            except Exception:
                return self._json(502, {"error": "imdb response not JSON"})
            entries = (data.get("d") or [])
            if not entries:
                return self._json(404, {"error": "no entries", "id": tt})
            # The first entry whose id matches our tt is the canonical
            # one — the suggest API can return cast/related results
            # alongside the title itself.
            entry = next((e for e in entries if (e.get("id") or "") == tt), entries[0])
            title = (entry.get("l") or "").strip()
            year  = entry.get("y")
            qid   = (entry.get("qid") or "").strip()  # "movie" / "tvSeries" / ...
            # Map qid → "movie" | "tv" | "" for the frontend modal.
            kind = ""
            if qid in ("movie", "short", "tvMovie", "video"):
                kind = "movie"
            elif qid in ("tvSeries", "tvMiniSeries", "tvSpecial", "tvEpisode"):
                kind = "tv"
            return self._json(200, {
                "id":    tt,
                "title": title,
                "year":  year,
                "kind":  kind,
            })

        if path == "/imdb/search":
            # Title search backed by *two* sources fanned out in parallel:
            #   1. IMDB's typeahead suggest API
            #        https://v3.sg.media-imdb.com/suggestion/<letter>/<query>.json
            #      Fast, well-ranked, but it's an autocomplete endpoint —
            #      caps at ~8 popular results. Misses anything obscure.
            #   2. Stremio cinemeta catalog search (we already use it for
            #      episode names):
            #        https://v3-cinemeta.strem.io/catalog/{series,movie}/top/search=<q>.json
            #      More comprehensive — surfaces lower-popularity titles
            #      that IMDB suggest drops. We hit series + movie in
            #      parallel so a single search covers both kinds.
            #
            # Results from all three calls are merged + deduped by tt-id;
            # IMDB suggest's poster URLs win when both sides have the
            # same title (suggest's images are sized for inline display).
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            query = (qs.get("q", [""])[0] or "").strip()
            if not query:
                return self._json(400, {"error": "no query"})
            safe = re.sub(r"[^A-Za-z0-9]+", "_", query).strip("_").lower()
            if not safe:
                return self._json(400, {"error": "empty query"})
            first = safe[0]
            from urllib.parse import quote as _urlquote
            q_enc = _urlquote(query)
            urls = [
                ("imdb",   f"https://v3.sg.media-imdb.com/suggestion/{first}/{safe}.json"),
                ("series", f"https://v3-cinemeta.strem.io/catalog/series/top/search={q_enc}.json"),
                ("movie",  f"https://v3-cinemeta.strem.io/catalog/movie/top/search={q_enc}.json"),
            ]
            # Concurrent fetch — total wall time is max(of the three)
            # rather than sum. All three are public JSON APIs that
            # respond in <1s under normal conditions.
            out: dict = {}
            errors: list = []
            def _fetch(name: str, u: str):
                try:
                    s, b = _simple_get(u, timeout=8.0)
                    if s == 200:
                        out[name] = b
                    else:
                        errors.append(f"{name} HTTP {s}")
                except Exception as e:
                    errors.append(f"{name}: {e}")
            ts = [threading.Thread(target=_fetch, args=(n, u), daemon=True)
                  for (n, u) in urls]
            for t in ts: t.start()
            for t in ts: t.join(timeout=10.0)

            # Merge. dedupe by tt-id. IMDB suggest provides better
            # poster thumbs (sized for autocomplete), so its entries
            # win on conflict. Cinemeta fills in the long tail.
            results: list = []
            seen: set = set()

            def _add(rec: dict):
                tt = rec.get("id") or ""
                if not tt.startswith("tt") or tt in seen:
                    return
                seen.add(tt)
                results.append(rec)

            # IMDB suggest first (better thumbs).
            try:
                if "imdb" in out:
                    data = json.loads(out["imdb"])
                    for e in (data.get("d") or []):
                        eid = (e.get("id") or "").strip()
                        if not eid.startswith("tt"):
                            continue
                        qid = (e.get("qid") or "").strip()
                        if qid in ("movie", "short", "tvMovie", "video"):
                            kind = "movie"
                        elif qid in ("tvSeries", "tvMiniSeries",
                                     "tvSpecial", "tvEpisode"):
                            kind = "tv"
                        else:
                            continue
                        thumb = ""
                        i = e.get("i")
                        if isinstance(i, dict):
                            thumb = i.get("imageUrl") or ""
                        _add({
                            "id":     eid,
                            "title":  (e.get("l") or "").strip(),
                            "year":   e.get("y"),
                            "kind":   kind,
                            "qLabel": (e.get("q") or "").strip(),
                            "thumb":  thumb,
                            "extra":  (e.get("s") or "").strip(),
                        })
            except Exception as ex:
                errors.append(f"imdb parse: {ex}")

            # Cinemeta — series first, then movies. Each contributes
            # whatever IMDB suggest missed. releaseInfo is "2021" for
            # finite runs / "2021-" for ongoing / "1992–1994" for
            # closed ranges; first 4-digit number wins as the year.
            for source_key, kind_label, qlabel in (
                ("series", "tv",    "TV Series"),
                ("movie",  "movie", "Movie"),
            ):
                try:
                    if source_key not in out:
                        continue
                    data = json.loads(out[source_key])
                    for m in (data.get("metas") or []):
                        eid = (m.get("imdb_id") or m.get("id") or "").strip()
                        if not eid.startswith("tt"):
                            continue
                        ri = (m.get("releaseInfo") or "").strip()
                        ym = re.search(r"\d{4}", ri)
                        try:
                            year = int(ym.group(0)) if ym else None
                        except ValueError:
                            year = None
                        _add({
                            "id":     eid,
                            "title":  (m.get("name") or "").strip(),
                            "year":   year,
                            "kind":   kind_label,
                            "qLabel": qlabel,
                            "thumb":  (m.get("poster") or "").strip(),
                            "extra":  ri,
                        })
                except Exception as ex:
                    errors.append(f"{source_key} parse: {ex}")

            if not results:
                # Surface a real error message rather than an empty list
                # — empty + 200 looks like "no matches" to the UI when
                # actually all three upstreams may have failed.
                if errors:
                    return self._json(502, {"error": " · ".join(errors[:3])})
                return self._json(200, {"query": query, "results": []})

            # Re-rank: exact title match first, then title-starts-with,
            # then anything else. Within each tier, newer first. Keeps
            # niche-but-perfect matches ("The Line" 2021) from being
            # buried under fuzzy IMDB-suggest results when the user's
            # query exactly names the title they want.
            ql = query.strip().lower()
            def _rank(r):
                tl = (r.get("title") or "").strip().lower()
                if tl == ql:
                    tier = 0
                elif tl.startswith(ql):
                    tier = 1
                elif ql in tl:
                    tier = 2
                else:
                    tier = 3
                # Negative year so DESC sort comes naturally.
                year = r.get("year") if isinstance(r.get("year"), int) else 0
                return (tier, -year)
            results.sort(key=_rank)

            return self._json(200, {"query": query, "results": results})

        if path == "/imdb/episodes":
            # Episode list for an IMDB TV series, sourced from Stremio's
            # cinemeta — a public, auth-free aggregator that maps IMDB
            # ids to full season/episode trees with names. Returns
            # {seasons: [{season:N, episodes:[{episode, name}, ...]}, ...]}.
            # Season 0 (extras / behind-the-scenes shorts) is filtered
            # out — the user wants the actual show, not commentary
            # snippets.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            tt = (qs.get("id", [""])[0] or "").strip().lower()
            if not re.match(r"^tt\d+$", tt):
                return self._json(400, {"error": "invalid id"})
            api_url = f"https://v3-cinemeta.strem.io/meta/series/{tt}.json"
            try:
                status, body = _simple_get(api_url)
            except Exception as e:
                return self._json(502, {"error": f"cinemeta fetch failed: {e}"})
            if status != 200:
                # 307 = id is a movie, not a series; that's fine, frontend
                # treats no-seasons as "stay on number inputs".
                return self._json(200, {"id": tt, "name": "", "seasons": []})
            try:
                data = json.loads(body)
            except Exception:
                return self._json(502, {"error": "cinemeta response not JSON"})
            meta = data.get("meta") or {}
            videos = meta.get("videos") or []
            grouped: dict = {}
            for v in videos:
                s = v.get("season")
                e = v.get("episode")
                if not isinstance(s, int) or s < 1:
                    continue
                if not isinstance(e, int) or e < 1:
                    continue
                grouped.setdefault(s, []).append({
                    "season":  s,
                    "episode": e,
                    "name":    (v.get("name") or f"Episode {e}").strip(),
                })
            for s in grouped:
                grouped[s].sort(key=lambda v: v["episode"])
            seasons = [{"season": s, "episodes": grouped[s]}
                       for s in sorted(grouped.keys())]
            return self._json(200, {
                "id":      tt,
                "name":    (meta.get("name") or "").strip(),
                "seasons": seasons,
            })

        if path == "/app/version":
            # Return installed + latest available Rip Raptor versions.
            # Mirrors /yt-dlp/version but compares against the GitHub
            # releases of henri-cmd/ripraptor.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            force = (qs.get("force", [""])[0] or "").strip() in ("1", "true", "yes")
            try:
                snap = _app_check_versions(force=force)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            installed = snap.get("installed", "")
            latest = snap.get("latest", "")
            update_available = _app_update_available(installed, latest)
            return self._json(200, {
                "installed": installed,
                "latest": latest,
                "update_available": update_available,
                "release_url": snap.get("release_url", ""),
                "checked_at": snap.get("checked", 0),
            })

        if path == "/probe":
            # ffprobe pass-through. Lets the user verify the audio stream
            # of any saved file by hitting:
            #   curl 'http://127.0.0.1:8765/probe?path=/Users/.../clip.mp4'
            # Mostly intended as a debugging aid when a 3rd party (Readymag,
            # Squarespace) refuses to play audio — the response shows the
            # actual codec / profile / sample rate / channel layout that
            # ended up in the file.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            target = (qs.get("path", [""])[0] or "").strip()
            if not target or not os.path.exists(target):
                return self._json(404, {"error": "file not found"})
            try:
                info = _quick_audio_probe(target)
            except Exception as e:
                return self._json(500, {"error": str(e)})
            return self._json(200, {"path": target, "audio": info})
        if path == "/":
            here = os.path.dirname(os.path.abspath(__file__))
            try:
                banner_v = str(int(os.path.getmtime(os.path.join(here, "banner.png"))))
            except Exception:
                banner_v = "0"
            html = (INDEX_HTML
                    .replace("__DEFAULT_DEST__", DEFAULT_DEST)
                    .replace("__BANNER_VERSION__", banner_v)
                    .replace("__APP_VERSION__", APP_VERSION)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if path.startswith("/manifest/"):
            mid = path[len("/manifest/"):]
            with manifests_lock:
                content = manifests.get(mid)
            if content is None:
                self.send_response(404); self.end_headers(); return
            body = content.encode()
            # DASH (.mpd) manifests are XML; HLS (.m3u8) are text. yt-dlp
            # downloaders pick the right one from the response Content-Type
            # when the URL doesn't carry an obvious extension.
            head = content.lstrip()[:200].lower()
            is_dash = head.startswith("<?xml") or "<mpd" in head or "<period" in head
            ctype = "application/dash+xml" if is_dash else "application/vnd.apple.mpegurl"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/banner.png", "/get-ripped.png", "/title.mp4",
                    "/something-went-wrong.mp4"):
            here = os.path.dirname(os.path.abspath(__file__))
            fp = os.path.join(here, path.lstrip("/"))
            try:
                with open(fp, "rb") as f:
                    body = f.read()
                ctype = "video/mp4" if path.endswith(".mp4") else "image/png"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_response(404); self.end_headers()
            return
        if path == "/editor/thumb":
            # Editor item thumbnails. Canvas-readback from the live <video>
            # element is unreliable on WebKit (hardware-decoded frames often
            # come back black even on same-origin / crossorigin sources). We
            # extract the frame server-side with ffmpeg from the editor's
            # already-cached local source — guaranteed to produce a valid
            # JPEG. ?sid=...&t=1.234[&w=200]
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = (qs.get("sid", [""])[0] or "").strip()
            try: t = float(qs.get("t", ["0"])[0] or 0)
            except Exception: t = 0.0
            try: w = max(40, min(600, int(qs.get("w", ["200"])[0] or 200)))
            except Exception: w = 200
            sess = _editor_get(sid)
            if not sess:
                return self._json(404, {"error": "unknown sid"})
            try:
                src_path = _ensure_cached_source(sess)
            except Exception as e:
                return self._json(500, {"error": f"cache failed: {e}"})
            # ffmpeg sometimes can't seek precisely on a partial-mp4 with
            # output -ss before -i; we put -ss AFTER -i (slower for big files
            # but reliable). For speed, do a coarse keyframe seek first
            # (-ss before -i), then fine-tune with output-side -ss.
            t_pre = max(0.0, t - 1.0)
            t_post = min(1.0, max(0.0, t - t_pre))
            cmd = [_ffmpeg_bin(), "-y", "-loglevel", "error",
                   "-ss", f"{t_pre:.3f}",
                   "-i", src_path,
                   "-ss", f"{t_post:.3f}",
                   "-frames:v", "1", "-q:v", "4",
                   "-vf", f"scale={w}:-2",
                   "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=15)
            except subprocess.TimeoutExpired:
                return self._json(504, {"error": "ffmpeg timed out"})
            if proc.returncode != 0 or not proc.stdout:
                err = (proc.stderr or b"").decode("utf-8", "ignore").strip()
                tail = err.splitlines()[-1] if err else f"ffmpeg exit {proc.returncode}"
                return self._json(500, {"error": tail[:200]})
            body = proc.stdout
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try: self.wfile.write(body)
            except Exception: pass
            return

        if path == "/thumb":
            # Proxy a remote image through curl_cffi so cross-origin CDNs
            # (Instagram, X/Twitter, Pinterest, FB) serve it without 403'ing
            # the WebView. ?u=<url>&r=<referer>.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            target = (qs.get("u", [""])[0] or "").strip()
            ref = (qs.get("r", [""])[0] or "").strip()
            if not target or not (target.startswith("http://") or target.startswith("https://")):
                self.send_response(400); self.end_headers(); return
            try:
                from curl_cffi import requests as cr
                headers = {
                    "User-Agent": BROWSER_UA,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
                if ref:
                    headers["Referer"] = ref
                    o = origin_of(ref)
                    if o:
                        headers["Origin"] = o
                resp = cr.get(target, headers=headers, impersonate="chrome",
                              stream=True, timeout=15)
                if resp.status_code >= 400:
                    self.send_response(resp.status_code); self.end_headers(); return
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                clen = resp.headers.get("Content-Length")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                if clen: self.send_header("Content-Length", clen)
                # Cache for an hour — these URLs are signed and rotate.
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if not chunk: continue
                    try: self.wfile.write(chunk)
                    except Exception: break
                try: resp.close()
                except Exception: pass
            except Exception:
                try: self.send_response(502); self.end_headers()
                except Exception: pass
            return

        if path == "/editor/detect-crop":
            # Auto-detect black bars in the editor's cached source via
            # ffmpeg's `cropdetect` filter. Samples ~10 frames spread
            # across the video so a single black opener doesn't bias
            # the result. Returns the detected crop = {w, h, x, y} plus
            # the source dimensions so the client can show the crop as
            # a fraction. cropdetect emits "crop=W:H:X:Y" lines per frame
            # — we take the most common one.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = (qs.get("sid", [""])[0] or "").strip()
            sess = _editor_get(sid)
            if not sess:
                return self._json(404, {"error": "unknown session"})
            try:
                src_path = _ensure_cached_source(sess)
            except Exception as e:
                return self._json(500, {"error": f"cache failed: {e}"})
            dur = float(sess.get("duration") or 0.0)
            # Sample at 10 evenly-spaced timestamps. -ss before -i seeks
            # cheap; -frames:v 1 grabs one frame per call.
            sample_count = 10
            crops = []
            src_w = src_h = 0
            for i in range(sample_count):
                t = (dur * (i + 0.5) / sample_count) if dur > 0 else 0
                cmd = [_ffmpeg_bin(), "-y",
                       "-ss", f"{t:.3f}", "-i", src_path,
                       "-vframes", "3",
                       "-vf", "cropdetect=24:16:0",
                       "-f", "null", "-"]
                try:
                    res = subprocess.run(cmd, capture_output=True,
                                         text=True, timeout=20)
                except subprocess.TimeoutExpired:
                    continue
                # cropdetect lines look like:
                #   [Parsed_cropdetect_0 ...] x1:240 x2:1679 ... w:1440 h:1080 x:240 y:0 ...
                #   crop=1440:1080:240:0
                for line in (res.stderr or "").splitlines():
                    m = re.search(r"crop=(\d+):(\d+):(\d+):(\d+)", line)
                    if m:
                        w, h, x, y = (int(m.group(1)), int(m.group(2)),
                                      int(m.group(3)), int(m.group(4)))
                        if w > 0 and h > 0:
                            crops.append((w, h, x, y))
                # Source dimensions come from the same stderr.
                if not src_w:
                    sm = re.search(r"Stream.*Video:.* (\d+)x(\d+)", res.stderr or "")
                    if sm:
                        src_w, src_h = int(sm.group(1)), int(sm.group(2))
            if not crops:
                return self._json(200, {"detected": False,
                                        "src_w": src_w, "src_h": src_h})
            # Most common crop wins; on ties pick the largest (least crop).
            from collections import Counter
            counts = Counter(crops)
            best = max(counts, key=lambda c: (counts[c], c[0] * c[1]))
            w, h, x, y = best
            no_crop = (src_w and src_h
                       and w == src_w and h == src_h and x == 0 and y == 0)
            return self._json(200, {
                "detected": not no_crop,
                "w": w, "h": h, "x": x, "y": y,
                "src_w": src_w, "src_h": src_h,
                "samples_seen": len(crops),
            })

        if path == "/editor/keyframes":
            # Return the timestamps of every keyframe (pict_type=I) in the
            # cached source's video stream. Used by the editor's
            # "Snap to keyframes" toggle so the user can set in/out marks
            # that align with GOP boundaries — cleaner cuts, smaller files.
            # Cached per session: ffprobe is expensive for long videos
            # (~1s per ~100MB), so we run it once and reuse.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = (qs.get("sid", [""])[0] or "").strip()
            sess = _editor_get(sid)
            if not sess:
                return self._json(404, {"error": "unknown session"})
            cached = sess.get("keyframes")
            if cached is not None:
                return self._json(200, {"times": cached})
            try:
                src_path = _ensure_cached_source(sess)
            except Exception as e:
                return self._json(500, {"error": f"cache failed: {e}"})
            ffprobe = _ffmpeg_bin().replace("ffmpeg", "ffprobe")
            if not Path(ffprobe).exists():
                ffprobe = "ffprobe"
            cmd = [ffprobe, "-v", "error",
                   "-select_streams", "v:0",
                   "-skip_frame", "nokey",
                   "-show_entries", "frame=pkt_pts_time",
                   "-of", "csv=print_section=0",
                   src_path]
            try:
                res = subprocess.run(cmd, capture_output=True,
                                     text=True, timeout=120)
            except subprocess.TimeoutExpired:
                return self._json(504, {"error": "ffprobe timed out"})
            if res.returncode != 0:
                tail = (res.stderr or "").strip().splitlines()
                return self._json(500, {"error": tail[-1] if tail else "ffprobe failed"})
            times = []
            for line in (res.stdout or "").splitlines():
                line = line.strip().rstrip(",")
                if not line:
                    continue
                try:
                    times.append(float(line))
                except ValueError:
                    pass
            sess["keyframes"] = times
            return self._json(200, {"times": times})

        if path == "/editor/cache-status":
            # Lightweight readiness probe for the background prefetch.
            # Frontend polls this every couple of seconds and swaps the
            # player to /cached/<sid> once `ready` flips true. While not
            # ready, surface bytes/total so the user can see progress;
            # for HLS sources we don't always know the total upfront, in
            # which case `total` stays 0 and the UI shows an indeterminate
            # spinner.
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = (qs.get("sid", [""])[0] or "").strip()
            sess = _editor_get(sid)
            if not sess:
                return self._json(404, {"error": "unknown session"})
            cached = sess.get("cached_path")
            ready = bool(cached and os.path.exists(cached))
            body = {"ready": ready}
            if ready:
                try:
                    body["size"] = os.path.getsize(cached)
                except OSError:
                    body["size"] = 0
            else:
                body["bytes"] = int(sess.get("cache_bytes", 0))
                body["total"] = int(sess.get("cache_total", 0))
                body["error"] = sess.get("cache_error", "")
            return self._json(200, body)

        if path == "/editor/state":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = (qs.get("sid", [""])[0] or "").strip()
            sess = _editor_get(sid)
            if not sess:
                return self._json(404, {"error": "unknown session"})
            return self._json(200, {
                "sid": sid,
                "items": sess.get("items", []),
                "markers": sess.get("markers", []),
                "default_quality": sess.get("default_quality", "best"),
                "title": sess.get("title", ""),
                "filename_hint": sess.get("filename_hint", ""),
                "duration": sess.get("duration", 0.0),
                "kind": sess.get("kind", ""),
            })

        if path == "/editor":
            from urllib.parse import parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = (qs.get("sid", [""])[0] or "").strip()
            sess = _editor_get(sid)
            if not sess:
                self.send_response(404); self.end_headers(); return
            html = (EDITOR_HTML
                    .replace("__SID__", sid)
                    .replace("__DURATION__", f"{sess.get('duration', 0):.3f}")
                    .replace("__TITLE__", json.dumps(sess.get("title", ""))[1:-1])
                    .replace("__FILENAME_HINT__", json.dumps(sess.get("filename_hint", ""))[1:-1])
                    .replace("__KIND__", sess.get("kind", ""))
                    .replace("__DEFAULT_DEST__", DEFAULT_DEST)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            # Warm the local cache in the background so ffmpeg ops are fast.
            try:
                threading.Thread(
                    target=_prefetch_cached_source,
                    args=(sid,), daemon=True,
                ).start()
            except Exception:
                pass
            return

        if path.startswith("/hls/"):
            # /hls/<sid>/playlist.m3u8 | /hls/<sid>/seg/<n> | /hls/<sid>/init/<n>
            parts = path.strip("/").split("/")
            if len(parts) >= 2:
                sid = parts[1]
                sess = _editor_get(sid)
                if not sess or sess.get("kind") != "hls":
                    self.send_response(404); self.end_headers(); return
                if len(parts) == 3 and parts[2] == "playlist.m3u8":
                    return self._serve_local_playlist(sid, sess)
                if len(parts) == 4 and parts[2] == "seg":
                    try:
                        n = int(parts[3])
                    except ValueError:
                        self.send_response(404); self.end_headers(); return
                    return self._proxy_hls_segment(sess, n)
                if len(parts) == 4 and parts[2] == "init":
                    return self._proxy_hls_init(sess)
            self.send_response(404); self.end_headers(); return

        if path.startswith("/proxy/"):
            sid = path[len("/proxy/"):].strip("/")
            sess = _editor_get(sid)
            if not sess or sess.get("kind") != "mp4":
                self.send_response(404); self.end_headers(); return
            return self._proxy_mp4(sess)

        if path.startswith("/cached/"):
            # Local-disk equivalent of /proxy/<sid>. The editor frontend
            # polls /editor/cache-status and only swaps the player's src
            # to /cached/<sid> after the background prefetch lands. After
            # the swap, every seek hits local disk → ~10ms instead of a
            # CDN round trip. Works for both kind=mp4 and kind=hls
            # (the HLS path remuxes to mp4 in _ensure_cached_source).
            sid = path[len("/cached/"):].strip("/")
            sess = _editor_get(sid)
            if not sess:
                self.send_response(404); self.end_headers(); return
            return self._serve_cached_file(sess)

        if path == "/queue/events":
            # Page-side SSE: pushes URLs that arrive at POST /queue (from
            # the bookmarklet, terminal curl, etc.) into the page's addUrl.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            listener: Queue = Queue()
            with _QUEUE_LISTENERS_LOCK:
                _QUEUE_LISTENERS.append(listener)
            try:
                while True:
                    try:
                        payload = listener.get(timeout=20)
                    except Empty:
                        try:
                            self.wfile.write(b": ping\n\n"); self.wfile.flush()
                        except Exception:
                            return
                        continue
                    try:
                        self.wfile.write(b"data: " + payload + b"\n\n")
                        self.wfile.flush()
                    except Exception:
                        return
            except Exception:
                return
            finally:
                with _QUEUE_LISTENERS_LOCK:
                    try: _QUEUE_LISTENERS.remove(listener)
                    except ValueError: pass

        if path.startswith("/events/"):
            jid = path[len("/events/"):]
            with jobs_lock:
                job = jobs.get(jid)
            if not job:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q: Queue = job["queue"]
            try:
                while True:
                    try:
                        msg = q.get(timeout=20)
                    except Empty:
                        try:
                            self.wfile.write(b": ping\n\n"); self.wfile.flush()
                        except Exception:
                            return
                        continue
                    if msg.get("type") == "_close":
                        return
                    try:
                        self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                        self.wfile.flush()
                    except Exception:
                        return
            except Exception:
                return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/queue":
                # Drop-in capture from anywhere: bookmarklet, curl, Automator,
                # etc. Routes a URL into the page's extract pipeline via SSE.
                # Accepts either JSON {"url": "..."} or a raw URL string in
                # the body — bookmarklets using `mode: "no-cors"` can only
                # send text/plain, so we have to support that.
                url = ""
                try:
                    raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "ignore")
                except Exception:
                    raw = ""
                if raw:
                    try:
                        d = json.loads(raw)
                        if isinstance(d, dict):
                            url = str(d.get("url") or "").strip()
                    except Exception:
                        url = raw.strip()
                # Fall back to ?url= query string for plain GET-style bookmarks.
                if not url:
                    from urllib.parse import parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    url = (qs.get("url", [""])[0] or "").strip()
                if not url:
                    self.send_response(400)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    return
                # Apply rewrites server-side too so script callers (curl,
                # Automator, etc.) get the same treatment the page's
                # addUrl gives. Idempotent — page-level rewrite re-runs
                # on the SSE-delivered URL but matches no pattern the
                # second time.
                url = _rewrite_url(url)
                payload = json.dumps({"url": url}).encode()
                with _QUEUE_LISTENERS_LOCK:
                    listeners = list(_QUEUE_LISTENERS)
                for q in listeners:
                    try: q.put(payload)
                    except Exception: pass
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
                return

            if path == "/probe":
                d = self._read_json()
                url = (d.get("url") or "").strip()
                referer = (d.get("referer") or "").strip()
                cookies_file = (d.get("cookies_file") or "").strip()
                cookies_browser = (d.get("cookies_browser") or "").strip()
                if not url:
                    return self._json(400, {"error": "no url"})
                try:
                    return self._json(200, probe_url(url, referer, cookies_file, cookies_browser))
                except subprocess.TimeoutExpired:
                    return self._json(504, {"error": "probe timed out"})
                except Exception as e:
                    return self._json(400, _err_payload(str(e)))

            if path == "/download":
                d = self._read_json()
                url = (d.get("url") or "").strip()
                if not url:
                    return self._json(400, {"error": "no url"})
                dest = d.get("dest") or DEFAULT_DEST
                height = d.get("height")
                audio_only = bool(d.get("audio_only"))
                container = d.get("container") or "mp4"
                generic = bool(d.get("generic"))
                referer = (d.get("referer") or "").strip()
                filename_hint = (d.get("filename_hint") or "").strip()
                cookies_file = (d.get("cookies_file") or "").strip()
                cookies_browser = (d.get("cookies_browser") or "").strip()
                embed_subs = bool(d.get("embed_subs", True))
                manifest_content = d.get("manifest_content") or ""
                variant_contents = d.get("variant_contents") or {}
                # Sniffer captured a manifest URL but didn't fetch its
                # content (the browser only fetches a variant playlist
                # when the user switches to that quality). Pre-fetch via
                # curl_cffi with the sniffed cookies so yt-dlp gets it
                # locally instead of trying — and being 403'd by — the
                # CDN.
                if (not manifest_content
                        and url
                        and (".m3u8" in url or ".mpd" in url)
                        and (url.startswith("http://") or url.startswith("https://"))):
                    manifest_content = _prefetch_manifest(
                        url, referer=referer, cookies_file=cookies_file)
                # If a browser-fetched manifest is provided, host it on
                # localhost. If variant playlists are also provided, host
                # them too and rewrite the master so yt-dlp never has to
                # fetch a manifest from the CDN. Only segments hit the CDN.
                if manifest_content and isinstance(manifest_content, str):
                    host_port = self.headers.get("Host", f"127.0.0.1:{PORT_FALLBACK}")
                    if isinstance(variant_contents, dict) and variant_contents:
                        mid = store_master_with_variants(manifest_content, variant_contents, host_port)
                    else:
                        mid = store_manifest(manifest_content)
                    url = f"http://{host_port}/manifest/{mid}"
                job = make_job()
                threading.Thread(
                    target=run_download,
                    args=(job, url, dest, height, audio_only, container, generic, referer, filename_hint, cookies_file, cookies_browser, embed_subs),
                    daemon=True,
                ).start()
                return self._json(200, {"id": job["id"]})

            if path == "/gallery_download":
                # Download a single gallery item (image, audio, or video)
                # picked out of a multi-asset post (Instagram carousel,
                # Pinterest album, Twitter thread, etc.). One job per item.
                d = self._read_json()
                item = d.get("item") or {}
                if not item.get("url"):
                    return self._json(400, {"error": "no item.url"})
                dest = d.get("dest") or DEFAULT_DEST
                height = d.get("height")
                audio_only = bool(d.get("audio_only"))
                container = d.get("container") or "mp4"
                cookies_file = (d.get("cookies_file") or "").strip()
                cookies_browser = (d.get("cookies_browser") or "").strip()
                embed_subs = bool(d.get("embed_subs", True))
                image_format = (d.get("image_format") or "original").strip().lower()
                job = make_job()
                threading.Thread(
                    target=run_gallery_item,
                    args=(job, item, dest, height, audio_only, container, cookies_file, cookies_browser, embed_subs, image_format),
                    daemon=True,
                ).start()
                return self._json(200, {"id": job["id"]})

            if path == "/hls-fetch":
                d = self._read_json()
                if not CURL_PYTHON:
                    return self._json(500, {"error": "curl_cffi python not found (pipx yt-dlp venv missing)"})
                if not Path(HLS_FETCHER).exists():
                    return self._json(500, {"error": f"hls_fetcher.py not found at {HLS_FETCHER}"})
                manifest_text = d.get("manifest_text") or ""
                manifest_url = (d.get("manifest_url") or "").strip()
                page_url = (d.get("page_url") or "").strip()
                cookies = d.get("cookies") or []
                out_path = (d.get("out_path") or "").strip()
                # manifest_text is OPTIONAL — when the user picks a
                # "live" variant whose playlist body wasn't captured
                # during sniff, we leave it empty and the helper
                # fetches it on demand from manifest_url.
                if not manifest_url or not out_path:
                    return self._json(400, {"error": "missing manifest_url/out_path"})
                spec = json.dumps({
                    "manifest_text": manifest_text,
                    "manifest_url": manifest_url,
                    "page_url": page_url,
                    "cookies": cookies,
                    "out_path": out_path,
                    # Bundled ffmpeg, used by the helper to mux separate
                    # video + audio renditions when the master playlist
                    # uses #EXT-X-MEDIA audio groups (the modern HLS
                    # default — required for 5.1 / multi-language).
                    "ffmpeg_path": _FFMPEG,
                }).encode()

                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()

                # PYTHONPATH so the subprocess sees our auto-installed
                # curl_cffi vendor dir (for users without pipx).
                _env = dict(os.environ)
                _env["PYTHONPATH"] = str(_CURL_CFFI_VENDOR) + (
                    os.pathsep + _env["PYTHONPATH"] if _env.get("PYTHONPATH") else "")
                proc = subprocess.Popen(
                    [CURL_PYTHON, "-u", HLS_FETCHER],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True,
                    env=_env,
                )
                try:
                    proc.stdin.write(spec)
                    proc.stdin.close()
                    while True:
                        line = proc.stdout.readline()
                        if not line:
                            break
                        try:
                            self.wfile.write(line)
                            self.wfile.flush()
                        except Exception:
                            try: proc.terminate()
                            except Exception: pass
                            return
                    proc.wait(timeout=5)
                    if proc.returncode != 0:
                        payload = json.dumps({"type": "error",
                                              "error": f"helper exited {proc.returncode}"}).encode() + b"\n"
                        try:
                            self.wfile.write(payload); self.wfile.flush()
                        except Exception: pass
                except Exception:
                    try: proc.kill()
                    except Exception: pass
                return

            if path == "/editor/start":
                d = self._read_json()
                kind = (d.get("kind") or "").strip()
                page_url = (d.get("page_url") or "").strip()
                cookies = d.get("cookies") or []
                title = (d.get("title") or "").strip()
                filename_hint = (d.get("filename_hint") or "").strip()
                if kind == "hls":
                    manifest_text = d.get("manifest_text") or ""
                    manifest_url = (d.get("manifest_url") or "").strip()
                    if not manifest_text or not manifest_url:
                        return self._json(400, {"error": "missing manifest_text/manifest_url"})
                    try:
                        sess = _make_editor_session(
                            kind="hls", page_url=page_url, cookies=cookies,
                            manifest_text=manifest_text, manifest_url=manifest_url,
                            title=title, filename_hint=filename_hint)
                    except Exception as e:
                        return self._json(400, {"error": str(e)})
                elif kind == "mp4":
                    src_url = (d.get("src_url") or "").strip()
                    if not src_url:
                        return self._json(400, {"error": "missing src_url"})
                    try:
                        sess = _make_editor_session(
                            kind="mp4", page_url=page_url, cookies=cookies,
                            src_url=src_url, title=title, filename_hint=filename_hint)
                    except Exception as e:
                        return self._json(400, {"error": str(e)})
                elif kind == "ytdlp":
                    target_url = (d.get("url") or "").strip()
                    if not target_url:
                        return self._json(400, {"error": "missing url"})
                    # Safety net: history's re-rip needs *something* the
                    # user can re-paste. If the caller forgot page_url,
                    # fall back to the input target_url so the session
                    # still has a working URL when history records it.
                    # Resolved CDN urls (sess["src_url"]) are NEVER a
                    # safe fallback — they're short-lived signed links.
                    if not page_url:
                        page_url = target_url
                    try:
                        sess = _make_editor_session(
                            kind="ytdlp", page_url=page_url, cookies=cookies,
                            title=title, filename_hint=filename_hint,
                            url=target_url, height=d.get("height"),
                            audio_only=bool(d.get("audio_only")),
                            generic=bool(d.get("generic")),
                            referer=(d.get("referer") or "").strip(),
                            cookies_file=(d.get("cookies_file") or "").strip(),
                            cookies_browser=(d.get("cookies_browser") or "").strip(),
                            playlist_items=str(d.get("playlist_items") or "").strip())
                    except Exception as e:
                        return self._json(400, _err_payload(str(e)))
                else:
                    return self._json(400, {"error": "kind must be 'hls', 'mp4', or 'ytdlp'"})
                sid = uuid.uuid4().hex[:12]
                with editor_lock:
                    editor_sessions[sid] = sess
                if sess["kind"] == "hls":
                    src = f"/hls/{sid}/playlist.m3u8"
                else:
                    src = f"/proxy/{sid}"
                # Optional: caller hints which quality the user picked on
                # the main page so the editor can pre-select it for new
                # clips/stills (e.g. "1080", "720", "source", "audio").
                dq = (d.get("default_quality") or "").strip()
                if dq:
                    sess["default_quality"] = dq
                # Restore prior items/markers/default_quality from disk
                # if the user has edited this URL before. The recall is
                # idempotent — applies after the explicit dq override
                # above only if no override was given OR the saved
                # default differs and is more recent.
                try:
                    _editor_state_recall(sess)
                except Exception:
                    pass
                return self._json(200, {
                    "sid": sid, "src": src, "kind": sess["kind"],
                    "duration": sess.get("duration", 0.0),
                    "title": sess.get("title", ""),
                    "filename_hint": sess.get("filename_hint", ""),
                    "default_quality": sess.get("default_quality", "best"),
                })

            if path == "/editor/items":
                # Replace the session's full items list. Editor pushes the
                # whole array on every change — items are tiny (no thumbs).
                d = self._read_json()
                sid = (d.get("sid") or "").strip()
                items = d.get("items") or []
                if not isinstance(items, list):
                    return self._json(400, {"error": "items must be a list"})
                sess = _editor_get(sid)
                if not sess:
                    return self._json(404, {"error": "unknown session"})
                sess["items"] = items
                # Markers piggyback on the same persist call when the
                # frontend includes them — keeps writes batched together.
                if "markers" in d and isinstance(d["markers"], list):
                    sess["markers"] = d["markers"]
                # Persist to URL-keyed disk store so the next time the
                # user opens this same URL we restore these selections.
                # Best-effort — disk failure shouldn't fail the save.
                try:
                    _editor_state_record(sess)
                except Exception:
                    pass
                return self._json(200, {"ok": True, "count": len(items)})

            if path == "/clip":
                d = self._read_json()
                sid = (d.get("sid") or "").strip()
                start = float(d.get("start") or 0.0)
                end = float(d.get("end") or 0.0)
                container = (d.get("container") or "mp4").strip()
                quality = (d.get("quality") or "source").strip()
                dest = (d.get("dest") or DEFAULT_DEST).strip()
                name = (d.get("name") or "").strip()
                # Live override of the session's source title — the card's
                # title input may have been renamed since the editor opened.
                hint_override = (d.get("filename_hint") or "").strip()
                # Optional crop rect ({x,y,w,h} in source pixels). When
                # set, ffmpeg's `crop` filter trims the output to that
                # window before any scale step.
                crop_arg = d.get("crop") if isinstance(d.get("crop"), dict) else None
                sess = _editor_get(sid)
                if not sess:
                    return self._json(404, {"error": "unknown session"})
                if end <= start:
                    return self._json(400, {"error": "end must be > start"})
                Path(dest).mkdir(parents=True, exist_ok=True)
                hint = hint_override or sess.get("filename_hint") or ""
                # New naming: "<source title> - <clip name>". If only one is
                # present, use it alone; if neither, fall back to "clip".
                if hint and name:
                    base = safe_basename(f"{hint} - {name}")
                elif name:
                    base = safe_basename(name)
                elif hint:
                    base = safe_basename(hint)
                else:
                    base = "clip"
                ext = "mp4" if container in ("mp4", "mp4-h264", "mp4-web") else container
                out = Path(dest) / f"{base}.{ext}"
                k = 1
                while out.exists():
                    out = Path(dest) / f"{base} ({k}).{ext}"
                    k += 1
                # Cache-build can take minutes for a long HLS source. Reply
                # to the HTTP request immediately with a job id and do the
                # work in the background — otherwise the JS fetch times out
                # at ~60s and the user sees a phantom "Load failed".
                job = make_job()
                # Capture the metadata we'll need after the encode lands —
                # the closure's view of `sess` is fine for in-process state
                # but we want the page URL (for "re-rip" in History) and
                # the user-visible name even if the session gets GC'd.
                # NOTE: deliberately do NOT fall back to sess["src_url"] —
                # that's the resolved CDN URL (signed, expires in minutes).
                # If page_url is missing, the safety net in /editor/start
                # has already populated it for ytdlp sessions; mp4/hls
                # always include it explicitly. Recording an empty url is
                # the lesser evil vs. recording a dead CDN link.
                _clip_history_url   = sess.get("page_url") or ""
                _clip_history_title = base
                def _run_clip_job(job=job, sess=sess, out=out, start=start,
                                  end=end, container=container, quality=quality,
                                  crop=crop_arg, hist_url=_clip_history_url,
                                  hist_title=_clip_history_title):
                    q: Queue = job["queue"]
                    try:
                        q.put({"type": "status", "status": "warming cache"})
                        src_path = _ensure_cached_source(sess)
                    except Exception as e:
                        q.put({"type": "error", "error": f"cache failed: {e}"})
                        q.put({"type": "_close"})
                        return
                    run_clip(job, src_path, str(out), start, end,
                             container, quality, crop=crop)
                    # run_clip emits its own done/error events; we only
                    # touch history on success. Numeric quality strings
                    # ("1080") become target heights; "best"/"source" map
                    # to None so the History row doesn't fabricate a
                    # height that the source might not even have had.
                    if job.get("status") == "done":
                        try:
                            h = int(quality) if str(quality).isdigit() else None
                        except (TypeError, ValueError):
                            h = None
                        try:
                            history_record(
                                title=hist_title,
                                url=hist_url,
                                file_path=str(out),
                                container=container,
                                height=h,
                                audio_only=False,
                            )
                        except Exception:
                            pass
                threading.Thread(target=_run_clip_job, daemon=True).start()
                return self._json(200, {"id": job["id"], "out_path": str(out)})

            if path == "/concat":
                # Stitch N clips from the editor's source into a single
                # web-safe MP4. Body shape:
                #   {sid, clips:[{start,end,crop?}], dest?, name?,
                #    filename_hint?}
                d = self._read_json()
                sid = (d.get("sid") or "").strip()
                clips = d.get("clips") or []
                dest = (d.get("dest") or DEFAULT_DEST).strip()
                name = (d.get("name") or "").strip()
                hint_override = (d.get("filename_hint") or "").strip()
                sess = _editor_get(sid)
                if not sess:
                    return self._json(404, {"error": "unknown session"})
                if not isinstance(clips, list) or len(clips) < 2:
                    return self._json(400, {"error": "need at least 2 clips"})
                Path(dest).mkdir(parents=True, exist_ok=True)
                hint = hint_override or sess.get("filename_hint") or ""
                # Naming: "<source title> - <user name>" or sensible default.
                if hint and name:
                    base = safe_basename(f"{hint} - {name}")
                elif name:
                    base = safe_basename(name)
                elif hint:
                    base = safe_basename(f"{hint} - concat")
                else:
                    base = "concat"
                ext = "mp4"
                out = Path(dest) / f"{base}.{ext}"
                k = 1
                while out.exists():
                    out = Path(dest) / f"{base} ({k}).{ext}"
                    k += 1
                # Async: return job id, do work in background. Same pattern
                # as /clip — cache-warm + ffmpeg can take a while.
                job = make_job()
                _concat_history_url   = sess.get("page_url") or ""
                _concat_history_title = base
                def _run_concat_job(job=job, sess=sess, out=out, clips=clips,
                                    hist_url=_concat_history_url,
                                    hist_title=_concat_history_title):
                    q: Queue = job["queue"]
                    try:
                        q.put({"type": "status", "status": "warming cache"})
                        src_path = _ensure_cached_source(sess)
                    except Exception as e:
                        q.put({"type": "error", "error": f"cache failed: {e}"})
                        q.put({"type": "_close"})
                        return
                    try:
                        run_concat(job, src_path, clips, str(out))
                    except Exception as e:
                        q.put({"type": "error", "error": str(e)})
                        q.put({"type": "_close"})
                        return
                    # Concat is always web-safe MP4 — no per-clip codec
                    # mixing — so the History entry can name the output
                    # container directly without inspecting the file.
                    if job.get("status") == "done":
                        try:
                            history_record(
                                title=hist_title,
                                url=hist_url,
                                file_path=str(out),
                                container="mp4-web",
                                height=None,
                                audio_only=False,
                            )
                        except Exception:
                            pass
                threading.Thread(target=_run_concat_job, daemon=True).start()
                return self._json(200, {"id": job["id"], "out_path": str(out)})

            if path == "/still":
                d = self._read_json()
                sid = (d.get("sid") or "").strip()
                t = float(d.get("t") or 0.0)
                fmt = (d.get("format") or "jpeg").lower()
                if fmt not in ("jpeg", "jpg", "png"):
                    fmt = "jpeg"
                quality = (d.get("quality") or "source").strip()
                dest = (d.get("dest") or DEFAULT_DEST).strip()
                name = (d.get("name") or "").strip()
                hint_override = (d.get("filename_hint") or "").strip()
                crop_arg = d.get("crop") if isinstance(d.get("crop"), dict) else None
                sess = _editor_get(sid)
                if not sess:
                    return self._json(404, {"error": "unknown session"})
                Path(dest).mkdir(parents=True, exist_ok=True)
                hint = hint_override or sess.get("filename_hint") or ""
                # See /clip for naming rationale.
                if hint and name:
                    base = safe_basename(f"{hint} - {name}")
                elif name:
                    base = safe_basename(name)
                elif hint:
                    base = safe_basename(hint)
                else:
                    base = "still"
                ext = "png" if fmt == "png" else "jpg"
                out = Path(dest) / f"{base}.{ext}"
                k = 1
                while out.exists():
                    out = Path(dest) / f"{base} ({k}).{ext}"
                    k += 1
                # Same async pattern as /clip — cache-warm can be slow.
                job = make_job()
                _still_history_url   = sess.get("page_url") or ""
                _still_history_title = base
                def _run_still_job(job=job, sess=sess, out=out, t=t,
                                   fmt=fmt, quality=quality, crop=crop_arg,
                                   hist_url=_still_history_url,
                                   hist_title=_still_history_title):
                    q: Queue = job["queue"]
                    try:
                        q.put({"type": "status", "status": "warming cache"})
                        src_path = _ensure_cached_source(sess)
                        q.put({"type": "status", "status": "encoding still"})
                        run_still(src_path, str(out), t, fmt, quality, crop=crop)
                        # Stills are images, so audio_only is meaningless;
                        # container = format ("jpeg" / "png"), height
                        # follows the quality dropdown's numeric value.
                        try:
                            h = int(quality) if str(quality).isdigit() else None
                        except (TypeError, ValueError):
                            h = None
                        try:
                            history_record(
                                title=hist_title,
                                url=hist_url,
                                file_path=str(out),
                                container=fmt,
                                height=h,
                                audio_only=False,
                            )
                        except Exception:
                            pass
                        q.put({"type": "done", "filename": str(out)})
                    except Exception as e:
                        q.put({"type": "error", "error": str(e)})
                    finally:
                        q.put({"type": "_close"})
                threading.Thread(target=_run_still_job, daemon=True).start()
                return self._json(200, {"id": job["id"], "out_path": str(out)})

            if path.startswith("/cancel/"):
                jid = path[len("/cancel/"):]
                with jobs_lock:
                    job = jobs.get(jid)
                if job:
                    kill_job(job)
                return self._json(200, {"ok": True})

            if path == "/yt-dlp/update":
                # User-triggered upgrade. Blocks until pipx finishes so the
                # response reports definitive success/failure. Frontend
                # shows a spinner during this call.
                try:
                    res = _ytdlp_update_blocking()
                except Exception as e:
                    return self._json(500, {"ok": False, "message": str(e)})
                return self._json(200, res)

            if path == "/app/install_update":
                # Kick off the in-app self-update flow. Returns a job id;
                # the frontend then opens an SSE stream to /events/<id>
                # to watch download/mount/copy progress. When the worker
                # emits {type: 'ready'} the frontend prompts the user to
                # apply via /app/install_update/apply.
                jid = "appupd-" + uuid.uuid4().hex[:8]
                with jobs_lock:
                    jobs[jid] = {
                        "id": jid,
                        "queue": Queue(),
                        "process": None,
                        "status": "running",
                        "filename": "",
                    }
                threading.Thread(target=_app_install_worker, args=(jid,),
                                 daemon=True).start()
                return self._json(200, {"job_id": jid})

            if path == "/app/install_update/apply":
                # Spawn the previously-staged update helper detached
                # from us. The helper waits for our parent (Swift host)
                # to exit before swapping the bundle, so the frontend
                # is expected to POST /quit immediately after this.
                res = _app_install_apply()
                code = 200 if res.get("ok") else 400
                return self._json(code, res)

            if path == "/history/add":
                # Frontend-driven history insert. Used by the Swift HLS
                # finalizer path which writes its output file directly
                # rather than through a Python job — that path bypasses
                # the in-job history_record calls used elsewhere, so the
                # JS onHlsDone handler POSTs here to keep History
                # complete. Body shape mirrors history_record's kwargs.
                d = self._read_json()
                file_path = (d.get("file_path") or "").strip()
                if not file_path:
                    return self._json(400, {"error": "file_path required"})
                try:
                    history_record(
                        title=(d.get("title") or "").strip(),
                        url=(d.get("url") or "").strip(),
                        file_path=file_path,
                        container=(d.get("container") or "").strip(),
                        height=d.get("height"),
                        audio_only=bool(d.get("audio_only")),
                        thumbnail=(d.get("thumbnail") or "").strip(),
                    )
                except Exception as e:
                    return self._json(500, {"error": str(e)})
                return self._json(200, {"ok": True})

            if path.startswith("/history/"):
                # POST /history/<id>/remove → delete that entry. Using
                # POST instead of DELETE because some HTTP clients/proxies
                # strip the body or method.
                tail = path[len("/history/"):]
                if tail.endswith("/remove"):
                    entry_id = tail[: -len("/remove")]
                    ok = history_remove(entry_id)
                    return self._json(200, {"ok": ok})
                if tail == "clear":
                    _history_save([])
                    return self._json(200, {"ok": True})
                return self._json(404, {"error": "unknown history op"})

            if path == "/reveal":
                # macOS-only: open Finder with the file selected. Used
                # by the History panel's "Reveal in Finder" action.
                d = self._read_json()
                p = (d.get("path") or "").strip()
                if not p or not os.path.exists(p):
                    return self._json(404, {"error": "file not found"})
                try:
                    subprocess.Popen(["open", "-R", p])
                except Exception as e:
                    return self._json(500, {"error": str(e)})
                return self._json(200, {"ok": True})

            if path == "/open-folder":
                # Open a Finder window for one of our well-known
                # directories. Whitelisted to prevent the frontend
                # from opening arbitrary paths.
                d = self._read_json()
                which = (d.get("which") or "").strip()
                allowed = {
                    "downloads": DEFAULT_DEST,
                    "plugin":    str(YTDLP_PLUGIN_DIR),
                    "data":      str(APP_SUPPORT),
                }
                p = allowed.get(which)
                if not p:
                    return self._json(400, {"error": "unknown folder"})
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                    subprocess.Popen(["open", p])
                except Exception as e:
                    return self._json(500, {"error": str(e)})
                return self._json(200, {"ok": True})

            if path == "/open-url":
                # Hand a URL to the system default browser. Accepts any
                # http(s) URL — used for the GitHub release page link
                # AND for the per-source "open in browser" buttons on
                # cards (so the user can debug a 111movies/streamimdb
                # link by viewing it in their actual browser). The
                # scheme check keeps file:// / smb:// / etc. out so a
                # hypothetical XSS can't be turned into a local-file
                # launcher.
                d = self._read_json()
                url = (d.get("url") or "").strip()
                if not (url.startswith("http://") or url.startswith("https://")):
                    return self._json(400, {"error": "only http(s) URLs allowed"})
                try:
                    subprocess.Popen(["open", url])
                except Exception as e:
                    return self._json(500, {"error": str(e)})
                return self._json(200, {"ok": True})

            if path == "/pick-folder":
                try:
                    res = subprocess.run(
                        ["osascript", "-e",
                         'POSIX path of (choose folder with prompt "Choose download folder")'],
                        capture_output=True, text=True, timeout=300,
                    )
                    p = res.stdout.strip().rstrip("/")
                    return self._json(200, {"path": p})
                except Exception as e:
                    return self._json(400, {"error": str(e)})

            if path == "/reveal":
                d = self._read_json()
                p = d.get("path") or ""
                if p and os.path.exists(p):
                    subprocess.Popen(["open", "-R", p])
                return self._json(200, {"ok": True})

            if path == "/quit":
                self._json(200, {"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.2), shutdown_event.set()),
                                 daemon=True).start()
                return

            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(500, {"error": str(e)})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Rip Raptor - Internet Video Downloader</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  *, *::before, *::after { box-sizing: border-box; }
  body {
    margin: 0;
    background: #008080;
    color: #000;
    font: 11px "MS Sans Serif", Tahoma, Geneva, Verdana, sans-serif;
    -webkit-font-smoothing: none;
    image-rendering: pixelated;
    overflow-x: hidden;
  }
  /* Defensive clamp — long unwrapped strings (post titles, raw URLs) must
     never push parent containers wider than the WKWebView viewport. */
  body, .window, .body, fieldset.panel, .gallery-pick { max-width: 100%; }
  fieldset.panel { overflow: hidden; }
  .row { min-width: 0; flex-wrap: wrap; }

  /* ========== Win98 scrollbars (WebKit) ==========
     Native macOS scrollbars look out of place on the beige Win98 chrome.
     Same chunky beveled track + outset thumb (flips to inset on :active)
     used by the editor — applied page-wide for visual consistency. */
  ::-webkit-scrollbar { width: 16px; height: 16px; background: #c0c0c0; }
  ::-webkit-scrollbar-track {
    background:
      repeating-conic-gradient(#c0c0c0 0% 25%, #a0a0a0 0% 50%) 0 0/2px 2px;
    border: 1px solid #808080;
  }
  ::-webkit-scrollbar-thumb {
    background: #c0c0c0;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    min-height: 24px; min-width: 24px;
  }
  ::-webkit-scrollbar-thumb:active {
    border-color: #404040 #ffffff #ffffff #404040;
  }
  ::-webkit-scrollbar-corner { background: #c0c0c0; }
  ::-webkit-scrollbar-button:single-button:vertical:start,
  ::-webkit-scrollbar-button:single-button:vertical:end,
  ::-webkit-scrollbar-button:single-button:horizontal:start,
  ::-webkit-scrollbar-button:single-button:horizontal:end {
    background: #c0c0c0;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    width: 16px; height: 16px; display: block;
  }
  ::-webkit-scrollbar-button:single-button:active {
    border-color: #404040 #ffffff #ffffff #404040;
  }

  /* Fills the whole macOS window — no fake chrome. */
  .window {
    min-height: 100vh;
    background: #c0c0c0;
    display: flex;
    flex-direction: column;
  }
  .body { padding: 10px; flex: 1 1 auto; }

  /* Banner */
  .banner {
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    margin-bottom: 8px;
    line-height: 0;
    background: #1a0a2a;
  }
  .banner img {
    display: block;
    width: 100%;
    height: auto;
    image-rendering: pixelated;
  }

  /* Win98 fieldset panels */
  fieldset.panel {
    background: #c0c0c0;
    border: 2px groove #d4d0c8;
    margin: 0 0 8px 0;
    padding: 8px 10px 10px;
  }
  fieldset.panel > legend {
    padding: 0 4px; font-weight: bold; font-size: 11px;
  }

  input[type=text], input[type=url], input:not([type]), select {
    font: 11px "MS Sans Serif", Tahoma;
    background: #fff; color: #000;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    padding: 2px 4px;
    height: 22px;
  }
  input[readonly] { background: #c0c0c0; }
  input { width: 100%; }
  select { padding: 1px 2px; }
  input:focus, select:focus { outline: 1px dotted #000; outline-offset: -3px; }

  button {
    background: #c0c0c0; color: #000;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    padding: 3px 14px;
    font: bold 11px "MS Sans Serif", Tahoma;
    min-width: 70px; cursor: pointer;
  }
  button:focus { outline: 1px dotted #000; outline-offset: -4px; }
  button:active:not(:disabled) {
    border-color: #404040 #ffffff #ffffff #404040;
    padding: 4px 13px 2px 15px;
  }
  button:disabled { color: #808080; text-shadow: 1px 1px 0 #ffffff; cursor: not-allowed; }
  button.danger { color: #800000; }
  /* Paste button is a sibling of Extract in the URL row. Override the
     default button min-width (70px) so the button shrinks to fit just
     the 📋 emoji — keeps the URL <input> the maximum useful width. */
  #btn-paste-url {
    min-width: 0;
    padding: 3px 8px;
    font-size: 14px;
    line-height: 1;
  }
  /* Rip It! is the headline action — green text in the otherwise-mono
     Win98 chrome puts a clear "this is GO" cue on the card. The colour
     overrides the default black inherited from `button {}`; we keep
     the bold weight (already inherited) so it reads from across the
     screen even at 11px. Also applies the green to the button when
     it's relabelled "Rip Again!" after a successful run — same role,
     same affordance. */
  .job .start { color: #006400; }
  .job .start:disabled { color: #80a080; }

  .row { display: flex; gap: 6px; align-items: center; }
  .row > input, .row > select { flex: 1; }
  .hint { color: #404040; font-size: 11px; margin-top: 6px; font-style: italic; }

  /* Job card — resembles a Win98 listbox row */
  .jobs { display: flex; flex-direction: column; gap: 6px; }
  .job {
    background: #c0c0c0;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    padding: 6px 8px;
    /* Defensive: never let an unwrapped child (rare title overflow,
       progress bar quirk) push the card past its container. */
    min-width: 0;
    overflow: hidden;
  }
  .job-head { display: flex; gap: 8px; align-items: center; min-width: 0; position: relative; }
  /* Top-right close button. Lives inside .job-head so it sits at the
     card's top edge regardless of whether the meta/options row grows
     (long titles, multi-source buttons, ed-summary, etc). 26×22 to
     match the card-move-stack arrow buttons; same maroon as the old
     inline Cancel so destructive-action coloring stays consistent. */
  .card-x {
    position: absolute;
    top: -2px;
    right: -2px;
    min-width: 0;
    width: 22px;
    height: 22px;
    padding: 0;
    font-size: 12px;
    line-height: 18px;
    font-weight: bold;
    flex: 0 0 auto;
  }
  .card-x:hover:not(:disabled) { background: #ffd0d0; }
  /* Per-card reorder stack — only shown for cards inside #pending. Once
     a card has graduated to Stage 3 (#jobs) the arrows disappear; you
     can't reorder a download in flight. */
  .card-move-stack {
    display: none;
    flex-direction: column;
    gap: 0;
    flex: 0 0 auto;
  }
  #pending .card-move-stack { display: inline-flex; }
  .card-move-stack button {
    min-width: 0; width: 26px; height: 22px;
    padding: 0; font-size: 13px; line-height: 16px;
    font-weight: bold;
  }
  .card-move-stack .btn-card-up   { border-bottom-width: 1px; }
  .card-move-stack .btn-card-down { border-top-width: 1px; margin-top: -1px; }
  .card-move-stack button:hover:not(:disabled) { background: #d8e4ff; }
  .card-move-stack button:disabled { color: #b8b8b8; cursor: not-allowed; }

  /* Contextual per-card action buttons — driven entirely by data-state.
     The card stays clean during normal flow; Retry only appears on
     failure, Reveal only after success. */
  .job .btn-retry,
  .job .btn-reveal { display: none; }
  .job[data-state="error"] .btn-retry { display: inline-block; }
  .job[data-state="done"]  .btn-reveal { display: inline-block; }
  /* The "Cancel" button reads as "Remove from list" once the job has
     finished (terminal state). Same destructive-red color regardless. */
  .job[data-state="done"]  .btn-retry  { display: none; }
  .job[data-state="done"]  .remove::before,
  .job[data-state="error"] .remove::before { content: ""; }
  .job-thumb {
    width: 64px; height: 48px; flex: 0 0 auto;
    background: #000; background-size: cover; background-position: center;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
  }
  .job-meta { flex: 1; min-width: 0; }
  /* Editable source-name input. Looks like plain bold text in the card —
     a faint dotted underline on hover hints it's editable; full inset
     border on focus. The value here is the prefix used when naming any
     clip/still ripped from this source. */
  input.job-title {
    width: 100%;
    box-sizing: border-box;
    font: bold 12px "MS Sans Serif", Tahoma;
    color: #000;
    background: transparent;
    border: 2px solid transparent;
    padding: 1px 3px;
    height: auto;
    text-overflow: ellipsis;
  }
  input.job-title:hover { border-bottom-color: #808080; border-bottom-style: dotted; }
  input.job-title:focus {
    outline: none;
    background: #fff;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
  }
  .job-sub { color: #404040; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* Per-source "open in browser" buttons. One per source URL the
     card knows about — IMDB multi-sniff shows two (111movies +
     streamimdb), single-source pastes show one. */
  .job-sources {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 4px;
  }
  .source-link-btn {
    font-family: "MS Sans Serif", Tahoma, sans-serif;
    font-size: 10px;
    padding: 1px 8px;
    background: #c0c0c0;
    color: #000080;
    border: 1px solid;
    border-color: #fff #404040 #404040 #fff;
    cursor: pointer;
    line-height: 1.4;
  }
  .source-link-btn:hover { background: #d8d8e8; }
  .source-link-btn:active { border-color: #404040 #fff #fff #404040; }

  .options { margin-top: 6px; display: none; flex-wrap: wrap; gap: 4px; align-items: center; }
  .options.show { display: flex; }
  /* Selects fluidly fill remaining space but can shrink as small as needed
     so the row's other items (subs label + buttons) get a chance to wrap
     to the next line instead of being clipped off the right edge. */
  .options select { flex: 1 1 140px; min-width: 0; max-width: 100%; }
  .options button, .options .subs-lbl { flex: 0 0 auto; }

  /* Gallery picker — multi-asset posts (Instagram carousels, Pinterest
     boards, Reddit galleries, etc.). Shown in place of the single quality
     dropdown when the probe returns kind=gallery. */
  .gallery-pick {
    margin-top: 6px; display: none;
    background: #ffffff;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    padding: 4px;
    overflow: hidden;
  }
  .gallery-pick.show { display: block; }
  .gallery-pick .gp-bar {
    display: flex; gap: 6px; align-items: center; margin-bottom: 4px;
    font-size: 11px; flex-wrap: wrap;
  }
  .gallery-pick .gp-bar button {
    font-size: 10px; padding: 1px 6px;
  }
  .gallery-pick .gp-strip {
    display: flex; gap: 4px; overflow-x: auto; padding: 2px 1px;
    max-height: 88px;
  }
  .gallery-pick .gp-item {
    position: relative; flex: 0 0 auto;
    width: 80px; height: 80px;
    border: 2px solid #808080;
    background: #c0c0c0 center/cover no-repeat;
    cursor: pointer; user-select: none;
  }
  .gallery-pick .gp-item.sel {
    border-color: #000080;
    box-shadow: inset 0 0 0 2px #ffff00;
  }
  .gallery-pick .gp-item .gp-kind {
    position: absolute; bottom: 0; right: 0;
    background: rgba(0,0,0,0.75); color: #fff;
    font-size: 9px; padding: 1px 3px; line-height: 1;
    font-family: "Press Start 2P", monospace;
  }
  .gallery-pick .gp-item .gp-check {
    position: absolute; top: 2px; left: 2px;
    width: 14px; height: 14px;
    background: #fff; border: 1px solid #000;
    font-family: "Press Start 2P", monospace;
    font-size: 9px; line-height: 12px; text-align: center;
    color: #000;
  }
  .gallery-pick .gp-item.sel .gp-check::before { content: "X"; }

  /* Stage 2 extract picker — multi-asset URLs (Instagram carousels,
     Pinterest boards, etc.) land here for review before being added as
     individual download cards. Bigger tiles than the in-card picker
     because Stage 2 has more horizontal space. */
  #extract-pick { display: none; }
  #extract-pick.show { display: block; }
  #extract-pick .ep-head {
    display: flex; gap: 8px; align-items: center; margin-bottom: 6px;
  }
  #extract-pick .ep-thumb {
    width: 56px; height: 56px; flex: 0 0 auto;
    background: #c0c0c0 center/cover no-repeat;
    border: 1px solid #404040;
  }
  #extract-pick .ep-info { flex: 1; min-width: 0; }
  #extract-pick .ep-title {
    font-weight: bold; font-size: 13px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  #extract-pick .ep-sub {
    font-size: 11px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  #extract-pick .gp-strip { max-height: 220px; flex-wrap: wrap; }
  #extract-pick .gp-item { width: 96px; height: 96px; }
  /* Pencil overlay on video tiles → opens the editor for that single
     carousel video. Square top-right corner button, matches Win98 look. */
  .gallery-pick .gp-item .gp-edit {
    position: absolute; top: 2px; right: 2px;
    width: 16px; height: 16px;
    background: #fff; border: 1px solid #000;
    color: #000; font-size: 10px; line-height: 13px; text-align: center;
    cursor: pointer; user-select: none;
    font-family: "Press Start 2P", monospace;
  }
  .gallery-pick .gp-item .gp-edit:hover { background: #ffff80; }
  .gallery-pick .gp-item.has-edits {
    box-shadow: inset 0 0 0 2px #ff8000;
  }
  .gallery-pick .gp-item.has-edits.sel {
    box-shadow: inset 0 0 0 2px #ff8000, inset 0 0 0 4px #ffff00;
  }
  .gallery-pick .gp-item .gp-edits-badge {
    position: absolute; top: 18px; right: 2px;
    background: #ff8000; color: #000; border: 1px solid #000;
    font-family: "Press Start 2P", monospace;
    font-size: 8px; padding: 1px 3px; line-height: 1;
  }

  /* Editor-selection thumbnail strip — shown on the main card after
     the user makes clip/still selections in the editor. Each tile
     re-opens the editor on click. */
  .ed-strip {
    display: none; gap: 4px; overflow-x: auto;
    padding: 3px 0 1px 0; margin-top: 4px;
  }
  .ed-strip.show { display: flex; }
  .ed-tile {
    position: relative; flex: 0 0 auto;
    width: 56px; height: 56px;
    border: 1px solid #404040;
    background: #c0c0c0 center/cover no-repeat;
    cursor: pointer;
  }
  .ed-tile:hover { border-color: #000080; }
  .ed-tile .ed-badge {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(0,0,0,0.78); color: #fff;
    font-family: "Press Start 2P", monospace;
    font-size: 7px; padding: 1px 2px; line-height: 1.2;
    text-align: center; letter-spacing: 0;
  }
  .ed-tile.clip .ed-badge { background: rgba(255,128,0,0.9); color: #000; }
  .ed-tile.still .ed-badge { background: rgba(0,128,0,0.9); }
  .ed-tile.concat .ed-badge { background: rgba(159,60,224,0.92); color: #fff; }

  /* Win98 chunky progress bar */
  .progress-wrap {
    height: 16px;
    background: #fff;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    padding: 1px;
    margin-top: 6px; display: none;
  }
  .progress-wrap.show { display: block; }
  .bar {
    height: 100%; width: 0;
    background-image: repeating-linear-gradient(90deg, #000080 0 9px, #c0c0c0 9px 11px);
    transition: width .15s linear;
  }
  .bar.done {
    background-image: repeating-linear-gradient(90deg, #008000 0 9px, #c0c0c0 9px 11px);
  }
  .bar.err {
    background-image: repeating-linear-gradient(90deg, #800000 0 9px, #c0c0c0 9px 11px);
  }

  .status { font-size: 11px; color: #000; margin-top: 6px; min-height: 14px;
            display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  /* Error states: red background with white text — high-contrast and
     unmistakable. The block fills the card's status row when present. */
  .status.err {
    color: #fff; background: #b00020;
    padding: 4px 8px; border: 1px solid #800010;
    align-items: flex-start;
  }
  .status.err pre {
    background: #800010; color: #fff;
    border: 1px solid #4a0008;
    padding: 4px 6px; margin: 4px 0;
    font: 11px "Courier New", monospace;
    max-height: 140px; overflow: auto; width: 100%; box-sizing: border-box;
  }
  .status.done { color: #006000; font-weight: bold; }
  .status button { padding: 1px 8px; font-size: 11px; min-width: auto; }
  .status pre { background: #fff; border: 2px solid;
                border-color: #404040 #ffffff #ffffff #404040;
                padding: 4px 6px; margin: 4px 0; font: 11px "Courier New", monospace;
                color: #800000; max-height: 140px; overflow: auto; }

  .empty {
    color: #404040; text-align: center; padding: 28px 0;
    font-size: 11px; font-style: italic;
  }
  .empty .big { font-size: 28px; display: block; margin-bottom: 6px; }
  .empty .sub { display: block; margin-top: 4px; color: #606060; }

  .spinner {
    width: 10px; height: 10px;
    background:
      conic-gradient(from 0deg,
        #000080 0deg 90deg, #c0c0c0 90deg 180deg,
        #000080 180deg 270deg, #c0c0c0 270deg 360deg);
    animation: spin .9s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Status bar — pinned to the bottom of the viewport, always visible
     (including during the title sequence). Fixed so it doesn't scroll
     with the rest of the page; sits above the title overlay too. */
  .statusbar {
    position: fixed; left: 0; right: 0; bottom: 0;
    background: #c0c0c0;
    border-top: 1px solid #ffffff;
    padding: 3px 4px;
    display: flex; gap: 4px; font-size: 11px;
    z-index: 100000;
  }
  .statusbar .seg {
    border: 1px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    padding: 1px 8px; flex: 0 0 auto;
    display: flex; align-items: center;
  }
  .statusbar .seg.grow { flex: 1 1 auto; }
  .statusbar .seg-btn {
    cursor: pointer; user-select: none;
    padding-left: 8px; padding-right: 8px;
  }
  .statusbar .seg-btn:hover { background: #d4d4d4; }
  .statusbar .seg-btn:active { background: #b0b0b0; }
  /* Settings cog — chunkier than the rest of the status bar so it
     reads as the primary entry point. */
  .statusbar #sb-settings {
    font-size: 22px; padding: 0 12px; line-height: 1;
    min-width: 40px; justify-content: center;
  }
  /* yt-dlp update pill — high-contrast amber so it actually catches the
     eye (this only appears when there's a real update available). */
  .statusbar #sb-ytdlp-update {
    background: #ffe680;
    color: #5a3000;
    font-weight: bold;
  }
  .statusbar #sb-ytdlp-update:hover { background: #ffd040; }
  .statusbar #sb-ytdlp-update.updating { background: #c0c0c0; color: #404040; }
  /* App update pill — cyan so it's visually distinct from the yt-dlp pill.
     Shown only when the local APP_VERSION is older than the latest GitHub
     release tag. Clicking opens the release page (no in-app updater for
     the 0.1 beta — manual dmg download). */
  .statusbar #sb-app-update {
    background: #b0e0ff;
    color: #003a5a;
    font-weight: bold;
  }
  .statusbar #sb-app-update:hover { background: #80c8f0; }

  /* History panel — a collapsible details/summary block. Each entry
     row is a flex line with thumb / title / metadata / actions. */
  #history-details summary {
    cursor: pointer; padding: 4px 0;
    display: flex; align-items: center; gap: 6px;
    font-size: 11px; color: #404040;
    user-select: none;
    flex-wrap: wrap;
  }
  #history-details summary::-webkit-details-marker { display: none; }
  #history-details summary::before {
    content: "▶"; display: inline-block; width: 10px;
    transition: transform 80ms linear;
    color: #404040;
    flex: 0 0 auto;
  }
  #history-details[open] summary::before { transform: rotate(90deg); }
  #history-details summary #history-summary-text { flex: 1 1 120px; min-width: 0; }
  #history-details summary button {
    font-size: 12px; padding: 2px 10px; min-width: 0;
    font-weight: bold;
  }
  /* Refresh glyph specifically — bigger + perfectly centered. The
     button sits next to the text "Clear" and was getting visually
     squashed as a thin 10px ↻. */
  #history-details summary #history-refresh {
    font-size: 16px; line-height: 1;
    padding: 1px 8px;
    min-width: 28px;
  }
  /* Search bar above the history list. */
  .history-controls {
    display: flex; gap: 6px; align-items: center;
    margin-top: 6px;
  }
  .history-controls input[type="search"] {
    flex: 1; min-width: 0;
    font-size: 11px; padding: 2px 6px; height: 22px;
  }
  .history-controls #history-filter-summary {
    color: #606060; flex: 0 0 auto;
  }
  .history-list {
    display: flex; flex-direction: column; gap: 3px;
    max-height: 320px;
    overflow-y: auto;
    margin-top: 6px;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    background: #ffffff;
    padding: 4px;
  }
  .history-row {
    display: flex; align-items: center; gap: 6px;
    padding: 4px;
    border-bottom: 1px solid #e0e0e0;
    font-size: 11px;
    /* flex-wrap so that on narrow windows the action buttons drop to a
       new line beneath the thumb + meta instead of pushing the row
       wider than the panel can hold. */
    flex-wrap: wrap;
  }
  .history-row:last-child { border-bottom: none; }
  .history-row.missing { opacity: 0.55; }
  .history-row .h-thumb {
    width: 56px; height: 32px; flex: 0 0 auto;
    background: #000 center/cover no-repeat;
    border: 1px solid #808080;
  }
  .history-row .h-meta {
    flex: 1 1 180px; min-width: 0;
  }
  .history-row .h-title {
    font-weight: bold; color: #000;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .history-row .h-sub {
    font-size: 10px; color: #606060;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .history-row .h-sub a { color: #000080; text-decoration: none; }
  .history-row .h-sub a:hover { text-decoration: underline; }
  .history-row .h-actions {
    display: flex; gap: 2px; flex: 0 0 auto;
    flex-wrap: wrap;
    margin-left: auto;  /* push to the right when there's room */
  }
  .history-row .h-actions button {
    font-size: 11px; padding: 2px 8px;
    min-width: 0; font-weight: bold;
  }
  /* The × delete glyph specifically — render it as a chunky icon. */
  .history-row .h-actions button[data-act="remove"] {
    font-size: 14px; padding: 0 8px; line-height: 18px;
  }
  .history-row .h-missing-tag {
    display: inline-block; font-size: 9px; color: #800000;
    border: 1px solid #800000; padding: 0 3px; border-radius: 1px;
    margin-left: 6px; vertical-align: middle;
  }
  /* History-controls (search + filter summary) and the list shouldn't
     ever push wider than the panel. */
  .history-controls { flex-wrap: wrap; }
  .history-list { max-width: 100%; box-sizing: border-box; }
  /* The fixed status bar reserves space at the bottom of the document
     so content doesn't render underneath it. */
  .body { padding-bottom: 36px; }
  /* Settings popup menu (anchored above the gear, far right corner) */
  .settings-menu {
    position: fixed; bottom: 32px; right: 4px;
    background: #c0c0c0;
    border: 2px solid; border-color: #fff #404040 #404040 #fff;
    box-shadow: 2px 2px 0 rgba(0,0,0,0.3);
    padding: 4px; z-index: 100001; min-width: 220px;
  }
  .settings-menu button {
    display: block; width: 100%; text-align: left;
    background: transparent; border: 1px solid transparent;
    padding: 4px 8px; font-size: 12px; cursor: pointer;
  }
  .settings-menu button:hover { border-color: #404040 #fff #fff #404040; background: #fff; }
  /* Section labels group items into Setup / Folders / About. */
  .settings-menu .settings-section-label {
    font-size: 9px; font-weight: bold; text-transform: uppercase;
    letter-spacing: 0.6px; color: #404040;
    padding: 6px 8px 2px;
    margin-top: 4px;
    border-top: 1px solid #a0a0a0;
  }
  .settings-menu .settings-section-label:first-child {
    margin-top: 0; border-top: none;
  }
  /* About footer — version readouts. Tiny + monochrome, never the
     focus of the menu. */
  .settings-menu .settings-about {
    margin-top: 6px; padding: 6px 8px 2px;
    border-top: 1px solid #a0a0a0;
    font-size: 10px; color: #404040; line-height: 1.5;
  }
  .settings-menu .settings-about strong { color: #000; }
  /* Author credit — italic, slightly muted, sits between app name and
     dependency versions. */
  /* Animation toggles — checkbox + label rows that sit between the
     button list and the about footer. Match the chrome of the buttons
     so they read as part of the same menu. */
  .settings-menu .settings-toggle {
    display: flex; align-items: center; gap: 8px;
    padding: 4px 8px;
    cursor: pointer;
    user-select: none;
    font-size: 12px;
    color: #000;
  }
  .settings-menu .settings-toggle:hover { background: #d8d8d8; }
  /* Global `input { width:100% }` rule (line ~3913) stretches every input
     across its container. Override here so the checkbox stays its native
     size and the label text gets the rest of the row. */
  .settings-menu .settings-toggle input[type="checkbox"] {
    margin: 0;
    width: auto;
    flex: 0 0 auto;
    accent-color: #000080;
  }
  .settings-menu .settings-toggle span {
    flex: 1 1 auto;
    white-space: nowrap;
  }
  .settings-menu .settings-credit {
    font-style: italic;
    color: #606060;
    margin: 1px 0 4px;
  }

  /* === IMDB movie/TV prompt ============================================
     Modal shown when the user pastes an IMDB title id (raw "tt12345" or
     a full imdb.com/title URL). Asks whether to treat it as a movie or
     a TV episode, and collects season/episode numbers in the latter
     case before resolving to a 111movies.net URL. Win98-styled card
     centered over a dim backdrop. */
  .rr-imdb-modal {
    position: fixed; inset: 0;
    z-index: 100002;
    display: flex; align-items: center; justify-content: center;
    background: rgba(0, 0, 0, 0.45);
  }
  .rr-imdb-modal .rr-imdb-card {
    background: #c0c0c0;
    color: #000;
    border: 2px solid;
    border-color: #fff #404040 #404040 #fff;
    box-shadow: 3px 3px 0 rgba(0,0,0,0.35);
    padding: 14px 16px;
    min-width: 320px;
    max-width: 420px;
    font-family: "MS Sans Serif", Tahoma, sans-serif;
  }
  .rr-imdb-modal h3 {
    margin: 0 0 8px;
    font-size: 13px;
  }
  .rr-imdb-modal h3 .mono {
    font-family: ui-monospace, "SF Mono", Menlo, monospace;
    background: #fff;
    border: 1px solid #404040;
    padding: 1px 6px;
    font-weight: normal;
    font-size: 12px;
  }
  .rr-imdb-modal p {
    margin: 0 0 10px;
    font-size: 12px;
    color: #404040;
  }
  .rr-imdb-modal .rr-imdb-radios {
    display: flex; gap: 16px;
    margin: 4px 0 10px;
    font-size: 12px;
  }
  .rr-imdb-modal .rr-imdb-radios label {
    display: inline-flex; align-items: center; gap: 6px;
    cursor: pointer; user-select: none;
  }
  .rr-imdb-modal .rr-imdb-radios input[type="radio"] {
    margin: 0; width: auto; flex: 0 0 auto;
  }
  .rr-imdb-modal .rr-imdb-tv {
    display: flex; gap: 14px;
    background: #d8d8d8;
    border: 1px solid #a0a0a0;
    padding: 8px 10px;
    margin: 0 0 12px;
    font-size: 12px;
  }
  .rr-imdb-modal .rr-imdb-tv label {
    display: inline-flex; align-items: center; gap: 4px;
  }
  .rr-imdb-modal .rr-imdb-tv input[type="number"] {
    width: 56px;
    flex: 0 0 56px;
  }
  .rr-imdb-modal .rr-imdb-actions {
    display: flex; justify-content: flex-end; gap: 8px;
    margin-top: 4px;
  }
  .rr-imdb-modal .rr-imdb-actions button {
    min-width: 72px;
  }
  .rr-imdb-modal .rr-imdb-actions .primary {
    background: #000080;
    color: #fff;
    border-color: #fff #404040 #404040 #fff;
  }
  .rr-imdb-modal .rr-imdb-actions .primary:hover { background: #1010c0; }

  /* === Self-update modal ============================================
     Reuses the .rr-imdb-modal backdrop + .rr-imdb-card chrome. The
     extras here are the progress strip (sunken bevel filled with the
     same navy as primary actions) and the status line directly under
     it. Width matches the kind/episode dialog so the page doesn't
     reflow when reopened back-to-back. */
  .rr-imdb-modal .rr-update-card {
    min-width: 380px;
    max-width: 460px;
  }
  .rr-imdb-modal .rr-update-bar {
    height: 14px;
    background: #fff;
    border: 2px solid;
    border-color: #404040 #fff #fff #404040;
    margin: 4px 0 6px;
    overflow: hidden;
  }
  .rr-imdb-modal .rr-update-fill {
    height: 100%;
    background: #000080;
    width: 0%;
    transition: width 180ms ease-out;
  }
  .rr-imdb-modal .rr-update-status {
    font-size: 11px;
    color: #404040;
    margin: 0 0 10px;
    min-height: 14px;
    word-wrap: break-word;
  }

  /* === IMDB title search ============================================
     Wider card than the kind/episode modal — the result list needs
     room. Reuses the .rr-imdb-modal backdrop. */
  .rr-imdb-modal .rr-imdb-search-card {
    background: #c0c0c0;
    color: #000;
    border: 2px solid;
    border-color: #fff #404040 #404040 #fff;
    box-shadow: 3px 3px 0 rgba(0,0,0,0.35);
    padding: 14px 16px;
    width: 520px;
    max-width: 90vw;
    max-height: 80vh;
    display: flex; flex-direction: column;
    font-family: "MS Sans Serif", Tahoma, sans-serif;
  }
  .rr-imdb-modal .rr-imdb-search-card h3 {
    margin: 0 0 8px;
    font-size: 13px;
  }
  .rr-imdb-modal #rr-search-input {
    width: 100%;
    margin-bottom: 8px;
  }
  .rr-imdb-modal .rr-search-status {
    font-size: 11px;
    color: #404040;
    margin-bottom: 4px;
    min-height: 14px;
  }
  .rr-imdb-modal #rr-search-results {
    flex: 1 1 auto;
    overflow-y: auto;
    background: #fff;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    margin-bottom: 10px;
    min-height: 100px;
  }
  /* Empty results pane while idle — matches the inset chrome of the
     other inset frames in the app. */
  .rr-imdb-modal #rr-search-results:empty::before {
    content: "";
    display: block;
    height: 100px;
  }
  .rr-search-result {
    display: flex; gap: 10px;
    padding: 8px;
    cursor: pointer;
    border-bottom: 1px solid #e0e0e0;
    align-items: center;
  }
  .rr-search-result:last-child { border-bottom: none; }
  .rr-search-result:hover {
    background: #000080;
    color: #fff;
  }
  .rr-search-result:hover .rr-search-sub { color: #b8c8ff; }
  .rr-search-thumb {
    flex: 0 0 auto;
    width: 48px; height: 72px;
    object-fit: cover;
    background: #e0e0e0;
    border: 1px solid #a0a0a0;
  }
  .rr-search-thumb-empty {
    background: linear-gradient(135deg, #e0e0e0 0%, #c0c0c0 100%);
  }
  .rr-search-meta {
    flex: 1 1 auto;
    min-width: 0;
  }
  .rr-search-title {
    font-size: 12px;
    font-weight: bold;
    margin-bottom: 4px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .rr-search-sub {
    font-size: 11px;
    color: #404040;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* === Error popup ====================================================
     Win98-style dialog window shown on card-level errors. Plays the
     "something went wrong" video once, muted, then holds on the last
     frame until the user closes the window via the title-bar X (or
     Esc / clicking the backdrop). No feather — sharp rectangular
     video framed by the beveled window chrome. */
  .rr-err-popup-overlay {
    position: fixed; inset: 0;
    z-index: 100000;  /* above the rip overlay (99999) */
    display: flex; align-items: center; justify-content: center;
    background: rgba(0, 0, 0, 0.35);
  }
  .rr-err-popup {
    background: #c0c0c0;
    border: 2px solid;
    border-color: #fff #404040 #404040 #fff;
    box-shadow: 3px 3px 0 rgba(0,0,0,0.5);
    display: flex; flex-direction: column;
    width: min(70vw, 540px);
    max-width: 90vw;
    max-height: 90vh;
    font-family: "MS Sans Serif", Tahoma, sans-serif;
  }
  .rr-err-popup-titlebar {
    background: linear-gradient(90deg, #000080 0%, #1084d0 100%);
    color: #fff;
    padding: 2px 3px 2px 6px;
    display: flex; align-items: center; justify-content: space-between;
    font-size: 11px; font-weight: bold;
    user-select: none;
  }
  .rr-err-popup-title { padding: 0; }
  .rr-err-popup-close {
    background: #c0c0c0;
    color: #000;
    border: 1px solid;
    border-color: #fff #404040 #404040 #fff;
    width: 18px; height: 16px;
    font-size: 12px;
    font-weight: bold;
    font-family: inherit;
    cursor: pointer;
    padding: 0;
    line-height: 1;
    display: flex; align-items: center; justify-content: center;
  }
  .rr-err-popup-close:active {
    border-color: #404040 #fff #fff #404040;
  }
  .rr-err-popup-body {
    padding: 4px;
    background: #c0c0c0;
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
  }
  .rr-err-popup-vid {
    display: block;
    width: 100%;
    height: auto;
    max-height: calc(90vh - 24px);
    background: #000;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
  }

  /* === Rip Raptor "GET RIPPED!" overlay animation =====================
     Plays over the whole window when the user clicks Rip It! / Rip Again! /
     Rip All. Transparent — passes pointer events through so the UI keeps
     working underneath. Ports the design at design/h/ouPjU1a3RIK7E2EGGY8SOA. */
  .rr-rip-overlay {
    position: fixed; inset: 0;
    pointer-events: none;
    z-index: 99999;
    overflow: visible;
  }
  .rr-rip-viewport {
    position: absolute; left: 50%; top: 50%;
    width: 1024px; height: 384px;
    transform: translate(-50%, -50%);
    transform-origin: center center;
    pointer-events: none;
  }
  .rr-rip-wordmark {
    position: absolute; left: 50%; top: 50%;
    width: 880px; height: auto;
    filter: drop-shadow(0 8px 16px rgba(0,0,0,0.5)) drop-shadow(0 0 24px rgba(0,0,0,0.3));
    user-select: none;
    pointer-events: none;
    transform: translate(-50%, -50%) scale(0.4) rotate(-8deg);
    transform-origin: center;
    will-change: transform;
  }
  .rr-rip-claws {
    position: absolute; inset: 0;
    pointer-events: none;
    overflow: visible;
    opacity: 0;
  }
  .rr-rip-flash {
    position: absolute; inset: 0;
    background: radial-gradient(ellipse at center, #a00000 0%, rgba(0,0,0,0) 70%);
    mix-blend-mode: screen;
    pointer-events: none;
    opacity: 0;
  }
</style>
</head>
<body>
<!-- Title-sequence overlay: HEVC-with-alpha video centred over the app's
     grey background. The .window app shell is hidden until the title
     finishes (or is skipped) — see JS below. Auto-dismisses on `ended`,
     click, or after a 10s safety timeout. -->
<!-- Edge-feather mask: stacks horizontal + vertical linear gradients
     and intersects them with mask-composite, so each of the four edges
     fades smoothly into the grey background instead of showing a sharp
     rectangular cut. Tweak the 7% stops to soften/sharpen the falloff. -->
<style>
  #rr-title-vid {
    -webkit-mask-image:
      linear-gradient(to right,  transparent 0%, black 7%, black 93%, transparent 100%),
      linear-gradient(to bottom, transparent 0%, black 7%, black 93%, transparent 100%);
    -webkit-mask-composite: source-in;
    mask-image:
      linear-gradient(to right,  transparent 0%, black 7%, black 93%, transparent 100%),
      linear-gradient(to bottom, transparent 0%, black 7%, black 93%, transparent 100%);
    mask-composite: intersect;
  }
</style>
<div id="rr-title-seq" style="position:fixed; inset:0; z-index:99999; display:flex; align-items:center; justify-content:center; cursor:pointer; transition:opacity 0.4s; background:#c0c0c0;">
  <video id="rr-title-vid"
         src="/title.mp4"
         autoplay muted playsinline preload="auto"
         style="max-width:100%; max-height:100%; display:block; background:transparent;"></video>
</div>
<div class="window" style="visibility:hidden;">
  <div class="body">

    <div class="banner">
      <img src="/banner.png?v=__BANNER_VERSION__" alt="Rip Raptor — Internet Video Downloader. The Raptor Can Rip It.">
    </div>

    <fieldset class="panel">
      <legend>1. Save Location</legend>
      <div class="row">
        <input id="dest" readonly>
        <button onclick="pickFolder()">Browse...</button>
      </div>
    </fieldset>

    <fieldset class="panel">
      <legend>2. Extraction</legend>
      <div class="row">
        <input id="url" placeholder="Enter URL" autofocus autocomplete="off" spellcheck="false">
        <button id="btn-paste-url" type="button" onclick="pasteUrlFromClipboard()" title="Paste URL(s) from clipboard" aria-label="Paste from clipboard">📋</button>
        <button class="primary" onclick="addUrl()">Extract</button>
      </div>
      <div class="hint">
        Paste a link &mdash; Rip Raptor probes the page for streams. Configure each card here, then click Rip It! to send it to Downloads.
        <span id="cookies-status" style="display:none; margin-left:6px; color:#000080;"></span>
      </div>
      <div id="extract-pick" class="gallery-pick" style="margin-top:8px;">
        <div class="ep-head">
          <div class="ep-thumb"></div>
          <div class="ep-info">
            <div class="ep-title">…</div>
            <div class="ep-sub sub">…</div>
          </div>
        </div>
        <div class="gp-bar">
          <span class="ep-count">0 selected</span>
          <button class="ep-all" type="button">All</button>
          <button class="ep-none" type="button">None</button>
          <span style="flex:1;"></span>
          <button class="primary ep-confirm" type="button">Add to Extraction</button>
          <button class="ep-sniff" type="button" title="Wrong stuff captured? Force the in-page sniffer to load the page in a hidden browser and pick up media as the player initialises.">Sniff Instead</button>
          <button class="ep-cancel" type="button">Discard</button>
        </div>
        <div class="gp-strip ep-strip"></div>
      </div>
      <div class="row" id="pending-toolbar" style="margin-top:8px; display:none;">
        <button id="btn-rip-all-pending" onclick="ripAllPending()">Rip All Pending</button>
        <button id="btn-cancel-all-pending" class="danger" onclick="cancelAllPending()" title="Discard every card in the pending list">Cancel All</button>
        <span id="rip-all-summary" class="sub" style="margin-left:6px;"></span>
      </div>
      <div id="pending" class="jobs"></div>
    </fieldset>

    <fieldset class="panel">
      <legend>3. Downloads</legend>
      <!-- Bulk-action toolbar — only shown when there's something to act
           on (any failed jobs, any completed jobs, or any rip running). -->
      <div class="row" id="downloads-toolbar" style="margin-bottom:6px; display:none;">
        <button id="btn-retry-all-failed" type="button" title="Re-run every job that failed">Retry all failed</button>
        <button id="btn-reveal-all" type="button" title="Open Finder windows for every completed file">Reveal all</button>
        <button id="btn-clear-completed" type="button" class="danger" title="Remove completed cards from the list">Clear completed</button>
        <span id="downloads-summary" class="sub" style="margin-left:6px;"></span>
      </div>
      <div id="jobs" class="jobs"></div>
      <div id="empty" class="empty">
        <span class="big">🦖</span>
        No active rips yet.
        <span class="sub">Paste a URL above and click Extract to get started.</span>
      </div>
    </fieldset>

    <!-- Persistent log of every successful whole-file rip. Click chevron
         to expand. Each row: thumb / title / when / size / source +
         actions to re-rip, reveal in Finder, copy URL, remove. -->
    <fieldset class="panel">
      <legend>4. History</legend>
      <details id="history-details">
        <summary>
          <span id="history-summary-text">No rips yet.</span>
          <button id="history-refresh" type="button" title="Refresh history">↻</button>
          <button id="history-clear" type="button" class="danger" title="Clear all history">Clear</button>
        </summary>
        <div class="history-controls">
          <input type="search" id="history-search" placeholder="Search history (title, URL, format)…">
          <span id="history-filter-summary" class="small"></span>
        </div>
        <div id="history-list" class="history-list"></div>
      </details>
    </fieldset>

  </div>
</div>
<!-- Status bar lives outside .window so it stays visible during the
     title-sequence overlay (which only hides .window). Fixed-positioned
     to the bottom of the viewport. -->
<div class="statusbar">
  <div class="seg grow" id="sb-status">Ready</div>
  <!-- yt-dlp update pill: visible only when GitHub reports a newer
       version than the installed one. Click → triggers pipx upgrade.
       Stays hidden when up-to-date so the bar isn't noisy. -->
  <div class="seg seg-btn" id="sb-ytdlp-update" style="display:none;"
       onclick="updateYtDlp()" title="Click to update yt-dlp via pipx">
    <span id="sb-ytdlp-text">yt-dlp update</span>
  </div>
  <!-- App update pill: visible only when GitHub reports a newer release of
       Rip Raptor than what's installed. Click → in-app updater (downloads
       the new dmg, swaps the bundle, relaunches). Distinct cyan from the
       amber yt-dlp pill so the two don't blur together when both visible. -->
  <div class="seg seg-btn" id="sb-app-update" style="display:none;"
       onclick="openAppUpdate()" title="Click to update Rip Raptor in-place">
    <span id="sb-app-text">app update</span>
  </div>
  <div class="seg" id="sb-jobs">0 active</div>
  <div class="seg" id="sb-app-version">v__APP_VERSION__</div>
  <div class="seg seg-btn" id="sb-settings" onclick="openSettingsMenu(event)" title="Settings">⚙</div>
</div>

<script>
const DEFAULT_DEST = "__DEFAULT_DEST__";
let dest = DEFAULT_DEST;
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];
$("#dest").value = dest;

// ───── Animation toggles ────────────────────────────────────────────────
// Three animations live in the app: the launch title sequence (intro),
// the GET RIPPED! overlay (rip), and the something-went-wrong popup
// (error). Each one independently respects a localStorage flag so the
// user can mute any subset from the settings menu. Default is on for
// all three; flag value is "off" or absent.
function isAnimEnabled(name) {
  try { return localStorage.getItem("rr.anim." + name) !== "off"; }
  catch (e) { return true; }
}
function setAnimEnabled(name, on) {
  try {
    if (on) localStorage.removeItem("rr.anim." + name);
    else    localStorage.setItem("rr.anim." + name, "off");
  } catch (e) {}
}

// URL bar: plain Enter triggers the existing extract flow.
$("#url").addEventListener("keydown", e => {
  if (e.key !== "Enter" || e.metaKey || e.ctrlKey) return;
  e.preventDefault();
  addUrl();
});

// Global Cmd/Ctrl+Enter: opens the IMDB title search modal from
// anywhere in the app. If the URL bar has text, that text is used as
// the initial query and the search fires immediately — so the user
// can type a title in the URL bar then Cmd+Enter to search for it.
// No-op if the modal is already open.
document.addEventListener("keydown", e => {
  if (e.key !== "Enter" || !(e.metaKey || e.ctrlKey)) return;
  if (document.getElementById("rr-imdb-search-modal")) return;
  e.preventDefault();
  const inp = document.getElementById("url");
  const seed = inp ? (inp.value || "").trim() : "";
  openImdbSearch(seed);
});

// Drag URLs from any browser tab (or a text file) onto the window: each
// dropped URL spawns a card. text/uri-list is the canonical drag MIME for
// browser tabs; text/plain is a fallback for things like Notes / TextEdit.
window.addEventListener("dragover", (e) => {
  // Calling preventDefault on dragover is what makes the browser allow a
  // drop event to fire. Without it, drops are silently rejected.
  if (e.dataTransfer && e.dataTransfer.types &&
      (e.dataTransfer.types.indexOf("text/uri-list") !== -1 ||
       e.dataTransfer.types.indexOf("text/plain") !== -1)) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }
});
window.addEventListener("drop", (e) => {
  if (!e.dataTransfer) return;
  const text = e.dataTransfer.getData("text/uri-list")
            || e.dataTransfer.getData("text/plain")
            || "";
  if (!text) return;
  e.preventDefault();
  // Reuse addUrl's multi-URL parser by stuffing the text into the input.
  $("#url").value = text;
  addUrl();
});

// Paste anywhere on the page also routes URLs into addUrl when the URL
// input isn't focused — so the user doesn't have to click into the field
// first. (When the input IS focused, native paste behaviour is preserved.)
window.addEventListener("paste", (e) => {
  const target = e.target;
  const tag = target && target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  const text = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
  const urls = _extractUrls(text);
  if (!urls.length) return;
  e.preventDefault();
  $("#url").value = text;
  addUrl();
});

// Paste from system clipboard. Equivalent to focusing the URL input
// and pressing Cmd+V + Enter, but in one click. Saves a step when the
// user just copied a URL from another app and the URL input doesn't
// have focus. Falls back to a focus + alert if clipboard access is
// denied (some WKWebView contexts gate it).
async function pasteUrlFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (!text || !text.trim()) {
      alert("Clipboard is empty.");
      return;
    }
    const inp = $("#url");
    if (inp) inp.value = text.trim();
    addUrl();
  } catch (e) {
    // Clipboard API may be denied in some contexts — fall back to
    // focusing the input so the user can do it manually.
    const inp = $("#url");
    if (inp) inp.focus();
    alert("Couldn't read the clipboard automatically. Press Cmd+V then Enter.");
  }
}

// Cookie source: which browser to extract cookies from. Hidden until a
// probe fails with an auth/age error — then the failing card surfaces a
// retry UI that lets the user pick a browser. Once a working browser is
// chosen it persists in localStorage and auto-applies to future probes.
const COOKIE_BROWSERS = [
  ["", "No cookies"],
  ["chrome", "Chrome"],
  ["firefox", "Firefox"],
  ["brave", "Brave"],
  ["edge", "Edge"],
  ["safari", "Safari"],
  ["chromium", "Chromium"],
  ["opera", "Opera"],
  ["vivaldi", "Vivaldi"],
];
let _cookiesBrowser = "";
try { _cookiesBrowser = localStorage.getItem("rr.cookies_browser") || ""; } catch (e) {}
function getCookiesBrowser() { return _cookiesBrowser; }
function setCookiesBrowser(name) {
  _cookiesBrowser = name || "";
  try { localStorage.setItem("rr.cookies_browser", _cookiesBrowser); } catch (e) {}
  const lbl = COOKIE_BROWSERS.find(([v]) => v === _cookiesBrowser);
  const el = document.getElementById("cookies-status");
  if (el) {
    if (_cookiesBrowser) {
      el.style.display = "";
      el.innerHTML = `🍪 Using cookies from <strong>${lbl ? lbl[1] : _cookiesBrowser}</strong>. <a href="#" id="cookies-clear" style="color:#000080;">change</a>`;
      const clear = document.getElementById("cookies-clear");
      if (clear) clear.onclick = (ev) => { ev.preventDefault(); promptCookiesBrowserChange(); };
    } else {
      el.style.display = "none";
      el.innerHTML = "";
    }
  }
}
// Cache of installed-browser keys, populated by /versions on first
// open of the cookie picker. Filters COOKIE_BROWSERS down to only
// what the user actually has (no point offering Vivaldi to someone
// who's never installed it).
let _installedBrowsers = null;
async function _loadInstalledBrowsers() {
  if (_installedBrowsers !== null) return _installedBrowsers;
  try {
    const r = await fetch("/versions");
    const j = await r.json();
    _installedBrowsers = Array.isArray(j.installed_browsers) ? j.installed_browsers : [];
  } catch (e) {
    _installedBrowsers = [];
  }
  return _installedBrowsers;
}
async function promptCookiesBrowserChange() {
  // Tiny one-shot picker rendered into the status span. Force-show the
  // span (it's hidden by default when no cookies are set, but the user
  // is explicitly asking to configure cookies, so reveal it).
  const el = document.getElementById("cookies-status");
  if (!el) return;
  el.style.display = "";
  el.innerHTML = "🍪 Cookies from: ";
  const sel = document.createElement("select");
  const installed = await _loadInstalledBrowsers();
  // Always offer "No cookies" + the user's currently-saved choice
  // (even if the corresponding app isn't detected — they may have
  // moved it). Otherwise filter to detected browsers.
  for (const [v, l] of COOKIE_BROWSERS) {
    const showAlways = (v === "" || v === _cookiesBrowser);
    const isInstalled = installed.length === 0 || installed.includes(v);
    if (!showAlways && !isInstalled) continue;
    const o = document.createElement("option");
    o.value = v;
    o.textContent = l + (v && !isInstalled ? "  (not detected)" : "");
    sel.appendChild(o);
  }
  sel.value = _cookiesBrowser;
  sel.onchange = () => { setCookiesBrowser(sel.value); };
  el.appendChild(sel);
}
// Initialise label from stored value (if any).
setCookiesBrowser(_cookiesBrowser);
// Initial UI sync — empty placeholder + pending toolbar visibility.
refreshDownloadsEmpty();
refreshPendingToolbar();
refreshDownloadsToolbar();

// Watch the #jobs panel for state-attribute or child changes — pings
// the bulk toolbar to recount running/done/failed without us having to
// instrument every individual dataset.state mutation site.
(function _wireDownloadsObserver() {
  const root = document.getElementById("jobs");
  if (!root || typeof MutationObserver === "undefined") return;
  const obs = new MutationObserver(() => {
    refreshDownloadsToolbar();
    refreshDownloadsEmpty();
  });
  obs.observe(root, {
    attributes: true,
    attributeFilter: ["data-state"],
    subtree: true,
    childList: true,
  });
})();

// ───── Stage 3 bulk actions ──────────────────────────────────────────
// Wire the toolbar buttons. Each iterates the #jobs children and
// dispatches the appropriate per-card action, leveraging the existing
// .start / .remove / reveal handlers on each card.
document.getElementById("btn-retry-all-failed").addEventListener("click", () => {
  const failed = Array.from(document.querySelectorAll("#jobs .job"))
    .filter(el => el.dataset.state === "error");
  if (failed.length === 0) return;
  for (const el of failed) {
    const card = el._card;
    if (card && typeof card.retry === "function") card.retry();
  }
});
document.getElementById("btn-reveal-all").addEventListener("click", async () => {
  const done = Array.from(document.querySelectorAll("#jobs .job"))
    .filter(el => el.dataset.state === "done")
    .map(el => el._card && el._card.getSavedFile && el._card.getSavedFile())
    .filter(Boolean);
  // Fire the reveal POSTs in parallel — Finder will queue them.
  await Promise.all(done.map(p => fetch("/reveal", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path: p}),
  }).catch(() => {})));
});
document.getElementById("btn-clear-completed").addEventListener("click", () => {
  const completed = Array.from(document.querySelectorAll("#jobs .job"))
    .filter(el => el.dataset.state === "done" || el.dataset.state === "error");
  for (const el of completed) {
    const removeBtn = el.querySelector(".remove");
    if (removeBtn) removeBtn.click();
  }
});

// ───── History panel ─────────────────────────────────────────────────
// Persistent log of every successful whole-file download. Entries are
// stored on the server (~/Library/Application Support/Rip Raptor/
// history.json) and fetched here to render. Each row exposes Re-rip /
// Reveal / Copy URL / Remove actions.
// Cached raw entries from the most recent /history fetch — search
// filters operate on this without re-hitting the server.
let _historyEntries = [];

async function loadHistory() {
  const sumEl = document.getElementById("history-summary-text");
  if (!sumEl) return;
  try {
    const r = await fetch("/history");
    const j = await r.json();
    _historyEntries = Array.isArray(j.entries) ? j.entries : [];
    sumEl.textContent = _historyEntries.length
      ? `${_historyEntries.length} rip${_historyEntries.length === 1 ? "" : "s"} in history`
      : "No rips yet.";
    renderHistory();
  } catch (err) {
    sumEl.textContent = "Couldn't load history.";
  }
}

function renderHistory() {
  const list = document.getElementById("history-list");
  const filterSum = document.getElementById("history-filter-summary");
  if (!list) return;
  const searchEl = document.getElementById("history-search");
  const q = (searchEl && searchEl.value || "").trim().toLowerCase();
  // Filter on title, URL host, container, and height — anything the user
  // is likely to search by. Empty query = all entries.
  const entries = !q ? _historyEntries : _historyEntries.filter(e => {
    const hay = [
      (e.title || "").toLowerCase(),
      (e.url || "").toLowerCase(),
      (e.container || "").toLowerCase(),
      String(e.height || "").toLowerCase(),
      (e.audio_only ? "audio" : "video"),
    ].join(" \x00 ");
    return hay.indexOf(q) !== -1;
  });
  if (filterSum) {
    filterSum.textContent = q
      ? `${entries.length}/${_historyEntries.length} match`
      : "";
  }
  list.innerHTML = "";
  if (_historyEntries.length === 0) {
    list.innerHTML = `<div class="hint" style="padding:8px;">Downloads you complete will appear here.</div>`;
    return;
  }
  if (entries.length === 0) {
    list.innerHTML = `<div class="hint" style="padding:8px;">No matches for &ldquo;${esc(q)}&rdquo;.</div>`;
    return;
  }
  for (const e of entries) {
      const row = document.createElement("div");
      row.className = "history-row" + (e.exists === false ? " missing" : "");
      const when = e.ts ? new Date(e.ts * 1000).toLocaleString() : "";
      const sizeMB = (e.size && e.size > 0) ? (e.size / (1024 * 1024)).toFixed(1) + " MB" : "";
      const fmt = e.audio_only ? "audio" : (e.container || "");
      const heightTxt = (e.height && /^\d+$/.test(String(e.height))) ? `${e.height}p` : "";
      const subBits = [when, fmt, heightTxt, sizeMB].filter(Boolean).join(" · ");
      // Source domain (cheap parse — full URL sits in copy-link).
      let host = "";
      try { host = e.url ? new URL(e.url).hostname.replace(/^www\./, "") : ""; } catch (_) {}
      const missTag = e.exists === false ? `<span class="h-missing-tag">file missing</span>` : "";
      row.innerHTML = `
        <div class="h-thumb"></div>
        <div class="h-meta">
          <div class="h-title">${esc(e.title || "(untitled)")}${missTag}</div>
          <div class="h-sub">${esc(subBits)}${host ? " · " + esc(host) : ""}</div>
        </div>
        <div class="h-actions">
          <button data-act="rerip" data-id="${e.id}" title="Download this URL again">Re-rip</button>
          <button data-act="reveal" data-id="${e.id}" title="Reveal file in Finder" ${e.exists === false ? "disabled" : ""}>Reveal</button>
          <button data-act="copy"   data-id="${e.id}" title="Copy source URL">Copy</button>
          <button data-act="remove" data-id="${e.id}" class="danger" title="Remove from history">×</button>
        </div>`;
      // Wire actions.
      row.querySelector('[data-act="rerip"]').addEventListener("click", () => {
        if (!e.url) return;
        const inp = $("#url");
        if (inp) {
          inp.value = e.url;
          // Reuse the existing addUrl() flow so the user sees the same
          // probe/extract path they'd get from a fresh paste.
          if (typeof addUrl === "function") addUrl();
        }
      });
      row.querySelector('[data-act="reveal"]').addEventListener("click", async () => {
        if (!e.file_path) return;
        try {
          await fetch("/reveal", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({path: e.file_path}),
          });
        } catch (err) {}
      });
      row.querySelector('[data-act="copy"]').addEventListener("click", async () => {
        if (!e.url) return;
        try { await navigator.clipboard.writeText(e.url); } catch (err) {}
      });
      row.querySelector('[data-act="remove"]').addEventListener("click", async () => {
        try {
          await fetch(`/history/${encodeURIComponent(e.id)}/remove`, { method: "POST" });
          loadHistory();
        } catch (err) {}
      });
      // Set thumb if we have one (data URI) — fallback: blank tile.
      if (e.thumbnail) {
        row.querySelector(".h-thumb").style.backgroundImage = `url("${e.thumbnail}")`;
      }
      list.appendChild(row);
  }
}
document.getElementById("history-refresh").addEventListener("click", (e) => {
  e.preventDefault(); e.stopPropagation();
  loadHistory();
});
// Live search: re-renders from the cached entries on every keystroke.
// `input` event fires for keyboard typing AND clear-button clicks, so
// `change` isn't enough.
document.getElementById("history-search").addEventListener("input", () => {
  renderHistory();
});
// Stop the search input from toggling the parent <details> on click.
document.getElementById("history-search").addEventListener("click", (e) => {
  e.stopPropagation();
});
document.getElementById("history-clear").addEventListener("click", async (e) => {
  e.preventDefault(); e.stopPropagation();
  if (!confirm("Clear all rip history? File on disk are not affected.")) return;
  try {
    await fetch("/history/clear", { method: "POST" });
    loadHistory();
  } catch (err) {}
});
// Stop summary clicks on the buttons from also toggling the details.
document.querySelectorAll("#history-details summary button").forEach(b => {
  b.addEventListener("click", e => e.stopPropagation());
});
// Refresh whenever a download completes — listen for our own done events.
// The simplest hook: poll briefly when the active job count drops. For
// now just refresh on a 30s interval AND when the window gains focus.
loadHistory();
setInterval(loadHistory, 30 * 1000);
window.addEventListener("focus", loadHistory);

// ───── yt-dlp update notification ────────────────────────────────────
async function checkYtDlpVersion(force) {
  try {
    const r = await fetch("/yt-dlp/version" + (force ? "?force=1" : ""));
    const j = await r.json();
    const pill = document.getElementById("sb-ytdlp-update");
    const txt = document.getElementById("sb-ytdlp-text");
    if (!pill || !txt) return j;
    if (j.update_available && j.installed && j.latest) {
      txt.textContent = `yt-dlp → ${j.latest}`;
      pill.title = `Installed ${j.installed} · ${j.latest} available · click to update`;
      pill.style.display = "";
    } else {
      pill.style.display = "none";
    }
    return j;
  } catch (e) { return null; }
}
async function updateYtDlp() {
  const pill = document.getElementById("sb-ytdlp-update");
  const txt = document.getElementById("sb-ytdlp-text");
  if (!pill || pill.classList.contains("updating")) return;
  pill.classList.add("updating");
  if (txt) txt.textContent = "updating yt-dlp…";
  try {
    const r = await fetch("/yt-dlp/update", { method: "POST" });
    const j = await r.json();
    if (j.ok) {
      if (txt) txt.textContent = `yt-dlp ${j.new_version || ""} ✓`;
      setTimeout(() => { pill.style.display = "none"; pill.classList.remove("updating"); }, 2400);
    } else {
      alert("yt-dlp update failed:\n" + (j.message || "unknown error"));
      pill.classList.remove("updating");
      checkYtDlpVersion(true);
    }
  } catch (e) {
    alert("yt-dlp update failed: " + (e.message || e));
    pill.classList.remove("updating");
  }
}
// Initial check happens after a short delay so it doesn't compete
// with the load-the-app traffic.
setTimeout(() => checkYtDlpVersion(false), 1500);

// ───── App update notification ───────────────────────────────────────
async function checkAppVersion(force) {
  try {
    const r = await fetch("/app/version" + (force ? "?force=1" : ""));
    const j = await r.json();
    const pill = document.getElementById("sb-app-update");
    const txt = document.getElementById("sb-app-text");
    if (!pill || !txt) return j;
    if (j.update_available && j.installed && j.latest) {
      txt.textContent = `Rip Raptor → ${j.latest}`;
      pill.title = `Installed v${j.installed} · v${j.latest} available · click to update in-place`;
      pill.dataset.releaseUrl = j.release_url || "";
      pill.dataset.installed = j.installed || "";
      pill.dataset.latest = j.latest || "";
      pill.style.display = "";
    } else {
      pill.style.display = "none";
    }
    return j;
  } catch (e) { return null; }
}
// In-app self-update flow.
//
// The actual download/mount/copy work happens in a Python background
// worker; we just stream its progress events over SSE and pivot the
// UI between three states: confirm → progress → ready-to-relaunch.
//
// Why a custom modal instead of confirm()/alert(): WKWebView's native
// dialogs are basic and the relaunch step takes ~10-30 s, so we need a
// surface that can show "Downloading 42% / Mounting / Copying" without
// blocking the JS event loop. Reuses the .rr-imdb-modal backdrop class
// so styling stays consistent with the IMDB / kind-prompt dialogs.
async function openAppUpdate() {
  // Bail out if a previous update modal is still around (rapid double
  // click). Re-opening would orphan the SSE stream from the first one.
  const existing = document.getElementById("rr-update-modal");
  if (existing) { existing.remove(); }

  const pill = document.getElementById("sb-app-update");
  const installed = (pill && pill.dataset.installed) || "";
  const latest    = (pill && pill.dataset.latest)    || "";
  const releaseUrl = (pill && pill.dataset.releaseUrl) ||
                     "https://github.com/henri-cmd/ripraptor/releases/latest";

  const modal = document.createElement("div");
  modal.id = "rr-update-modal";
  modal.className = "rr-imdb-modal";
  modal.innerHTML = `
    <div class="rr-imdb-card rr-update-card" role="dialog" aria-modal="true"
         aria-labelledby="rr-update-h">
      <h3 id="rr-update-h">Update Rip Raptor</h3>
      <p id="rr-update-msg">
        Installed <strong>v${installed || "?"}</strong> ·
        Latest <strong>v${latest || "?"}</strong>.
        Update will download the new app, replace the existing copy in
        Applications, and relaunch.
      </p>
      <div id="rr-update-progress" style="display:none;">
        <div class="rr-update-bar"><div class="rr-update-fill" style="width:0%"></div></div>
        <div class="rr-update-status mono" id="rr-update-status">starting…</div>
      </div>
      <div class="rr-imdb-actions">
        <button id="rr-update-notes" type="button">Release notes</button>
        <button id="rr-update-cancel" type="button">Cancel</button>
        <button id="rr-update-go" type="button" class="primary">Update now</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const $msg = modal.querySelector("#rr-update-msg");
  const $prog = modal.querySelector("#rr-update-progress");
  const $fill = modal.querySelector(".rr-update-fill");
  const $status = modal.querySelector("#rr-update-status");
  const $go = modal.querySelector("#rr-update-go");
  const $cancel = modal.querySelector("#rr-update-cancel");
  const $notes = modal.querySelector("#rr-update-notes");

  let evt = null;
  function close() {
    if (evt) { try { evt.close(); } catch(e) {} evt = null; }
    try { modal.remove(); } catch(e) {}
  }
  $cancel.addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  $notes.addEventListener("click", () => {
    fetch("/open-url", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url: releaseUrl}),
    }).catch(() => {});
  });

  $go.addEventListener("click", async () => {
    // Stage 2: kick off the install worker, swap the buttons to a
    // single "Cancel" until ready.
    $go.disabled = true;
    $go.textContent = "Working…";
    $msg.style.display = "none";
    $prog.style.display = "";
    let jobId = null;
    try {
      const r = await fetch("/app/install_update", { method: "POST" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
      jobId = j.job_id;
    } catch (e) {
      $status.textContent = "failed to start update: " + (e.message || e);
      $go.style.display = "none";
      $cancel.textContent = "Close";
      return;
    }

    // Stream progress events. Final state is either {type: 'ready'}
    // or {type: 'error', error: '...'}.
    evt = new EventSource("/events/" + jobId);
    let ready = false;
    evt.onmessage = (msg) => {
      let d;
      try { d = JSON.parse(msg.data); } catch (e) { return; }
      if (d.type === "status") {
        $status.textContent = d.msg || "";
      } else if (d.type === "progress") {
        const pct = Math.max(0, Math.min(100, +d.percent || 0));
        $fill.style.width = pct.toFixed(1) + "%";
        const mb = (d.got || 0) / 1048576;
        const tot = (d.total || 0) / 1048576;
        $status.textContent = `downloading… ${mb.toFixed(1)} / ${tot.toFixed(1)} MB`;
      } else if (d.type === "ready") {
        ready = true;
        $fill.style.width = "100%";
        $status.textContent = "Ready. Click below to relaunch into the new version.";
        $go.style.display = "";
        $go.disabled = false;
        $go.textContent = "Relaunch & update";
        $go.onclick = applyUpdate;
        $cancel.textContent = "Later";
      } else if (d.type === "error") {
        $status.textContent = "Update failed: " + (d.error || "unknown");
        $go.style.display = "none";
        $cancel.textContent = "Close";
        try { evt.close(); } catch(e) {}
      }
    };
    evt.onerror = () => {
      // SSE errors are noisy on Safari; only treat as fatal if we
      // haven't reached "ready" yet.
      if (!ready) {
        $status.textContent = "lost connection to update worker";
        $go.style.display = "none";
        $cancel.textContent = "Close";
      }
    };
  });

  async function applyUpdate() {
    $go.disabled = true;
    $go.textContent = "Relaunching…";
    $cancel.style.display = "none";
    try {
      const r = await fetch("/app/install_update/apply", { method: "POST" });
      const j = await r.json();
      if (!r.ok || !j.ok) throw new Error(j.error || ("HTTP " + r.status));
    } catch (e) {
      $status.textContent = "Apply failed: " + (e.message || e);
      $go.disabled = false;
      $go.textContent = "Retry";
      $cancel.style.display = "";
      $cancel.textContent = "Close";
      return;
    }
    // Helper is now detached and waiting for our parent (the Swift
    // host) to die. Trigger /quit and the helper takes over from
    // there. Show a transient "see you in a sec" message in case
    // the relaunch is slower than expected.
    $status.textContent = "quitting current app — the new version will open shortly…";
    setTimeout(() => {
      fetch("/quit", { method: "POST" }).catch(() => {});
    }, 350);
  }
}
// Stagger the app check after the yt-dlp one so we don't fire two
// GitHub requests back-to-back at startup.
setTimeout(() => checkAppVersion(false), 2800);

// Title-sequence overlay. The app shell (.window) is rendered with
// visibility:hidden so the user only sees the title animation against
// the grey background until it finishes. Reveal happens inside dismiss.
// Auto-dismisses on `ended`, click anywhere, or after a 10s safety
// timeout (in case the video fails to load).
(function titleSequence() {
  const overlay = document.getElementById("rr-title-seq");
  const win = document.querySelector(".window");
  let dismissed = false;
  function dismiss() {
    if (dismissed) return;
    dismissed = true;
    if (win) win.style.visibility = "";
    if (overlay) {
      overlay.style.opacity = "0";
      setTimeout(() => { try { overlay.remove(); } catch(e) {} }, 450);
    }
  }
  if (!overlay) { dismiss(); return; }
  // User-muted via Settings → Animations. Skip the video entirely and
  // dismiss instantly so the app renders without a black flash.
  if (!isAnimEnabled("intro")) { dismiss(); return; }
  const vid = document.getElementById("rr-title-vid");
  if (vid) {
    vid.addEventListener("ended", dismiss);
    vid.addEventListener("error", dismiss);
    // Some loads stall silently — bail out after a generous timeout.
    setTimeout(dismiss, 10000);
    // Best-effort autoplay (muted, so should always be allowed).
    vid.play().catch(() => {});
  } else {
    dismiss();
  }
  overlay.addEventListener("click", dismiss);
})();

// Cross-app capture: subscribe to /queue/events so URLs POSTed to /queue
// (from a bookmarklet, terminal, Automator action, etc.) drop straight
// into the extract pipeline.
(function subscribeToQueue() {
  let evt = null;
  function connect() {
    try {
      evt = new EventSource("/queue/events");
      evt.onmessage = (m) => {
        try {
          const d = JSON.parse(m.data);
          if (d && d.url) {
            $("#url").value = d.url;
            addUrl();
          }
        } catch(e) {}
      };
      evt.onerror = () => {
        if (evt) { try { evt.close(); } catch(e) {} evt = null; }
        // Reconnect with a small backoff
        setTimeout(connect, 2000);
      };
    } catch(e) {
      setTimeout(connect, 2000);
    }
  }
  connect();
})();

async function pickFolder() {
  const r = await fetch("/pick-folder", {method:"POST"});
  const j = await r.json();
  if (j.path) { dest = j.path; $("#dest").value = dest; }
}

function quit() {
  fetch("/quit", {method:"POST"}).finally(() => {
    document.body.innerHTML = '<div style="padding:60px;text-align:center;color:#8e8e93">Goodbye. You can close this tab.</div>';
  });
}

function fmtDur(s) {
  if (!s) return "";
  s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor(s%3600/60), ss = s%60;
  return h ? `${h}:${String(m).padStart(2,"0")}:${String(ss).padStart(2,"0")}`
           : `${m}:${String(ss).padStart(2,"0")}`;
}

// Format a "remaining time" value (seconds) into a human-friendly
// string. Tiers: "<1s" | "Ns" | "M:SS" | "H:MM:00". Used for both the
// time-based unified-bar ETA and the cache prefetch pill.
function fmtRemainingSec(sec) {
  if (!isFinite(sec) || sec < 0) return "";
  if (sec < 1)    return "<1s";
  if (sec < 60)   return Math.round(sec) + "s";
  if (sec < 3600) {
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    return m + ":" + String(s).padStart(2, "0");
  }
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h + ":" + String(m).padStart(2, "0") + ":00";
}

// Estimate the time remaining from elapsed seconds + a 0..1 progress
// fraction. Returns "" when the progress is too small to extrapolate
// reliably (or already effectively finished). Convenience wrapper —
// callers that already have a remaining-seconds estimate use
// fmtRemainingSec directly.
function fmtETA(elapsedSec, progress) {
  if (!isFinite(elapsedSec) || elapsedSec <= 0) return "";
  if (!isFinite(progress) || progress <= 0.01 || progress >= 0.999) return "";
  const total = elapsedSec / progress;
  return fmtRemainingSec(Math.max(0, total - elapsedSec));
}

function esc(s){return String(s).replace(/[<>&"']/g,c=>({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c]));}

const sniffMap = new Map();
const canSniff = () => !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.vdSniff);

// Hosts that yt-dlp's /probe is known to bounce off — typically
// JS-rendered streaming front-ends that only expose their actual media
// after the page loads in a real browser. Pasting a URL on one of these
// hosts skips the probe round trip and goes straight to the in-page
// sniffer, which is what /probe would have fallen back to anyway. Pure
// latency reduction — no behaviour change.
//
// Add a host here when you're certain a probe is always going to fail:
//   - 111movies.net          (IMDB modal target — both /movie and /tv)
//   - streamimdb.ru          (alternate IMDB-id streaming embedder)
const SNIFF_ONLY_HOSTS = new Set([
  "111movies.net",
  "www.111movies.net",
  "streamimdb.ru",
  "www.streamimdb.ru",
]);
function _isSniffOnly(url) {
  try {
    const u = new URL(String(url || ""));
    return SNIFF_ONLY_HOSTS.has(u.hostname.toLowerCase());
  } catch (e) {
    return false;
  }
}
// sniffMap value shape:
//   {card, multi: false}              — legacy single-source sniff
//   {card, multi: true, srcIdx: N}    — one of N parallel multi-sniffs
window.__vdSniffResult = function(jsonStr) {
  try {
    const d = JSON.parse(jsonStr);
    const entry = sniffMap.get(d.sniffId);
    if (!entry) return;
    sniffMap.delete(d.sniffId);
    if (entry.multi) {
      // Promise-based dispatch — startMultiSniff awaits each sniff
      // before kicking off the next, so it stores its resolver here.
      if (entry.onResult) entry.onResult(d);
      else if (entry.card && entry.card.onMultiSniffResult) {
        entry.card.onMultiSniffResult(entry.srcIdx, d);
      }
    } else {
      entry.card.onSniffResult(d);
    }
  } catch(e) { console.error(e); }
};
window.__vdSniffProgress = function(sniffId, n) {
  const entry = sniffMap.get(sniffId);
  if (!entry) return;
  // For multi-sniff we don't show per-source progress (would be noisy
  // with two simultaneous counters); the card-level "Searching N
  // sources…" status stays put until results merge.
  if (entry.multi) return;
  entry.card.setStatus(`<span class="spinner"></span> Sniffing in background… ${n} stream${n===1?"":"s"} captured`);
};

const hlsTasks = new Map();

// Stash for IMDB poster URLs picked from the search modal. Keyed by
// IMDB id (e.g. "tt14475988"). Used at card-construction time so the
// thumbnail shows the actual movie/show cover *immediately* — before
// multi-source sniff completes and finds (or fails to find) a poster
// in the streaming page's metadata. Persists for the page lifetime;
// no eviction needed (max ~100 lookups per session, tiny URLs).
const imdbThumbCache = new Map();

// Per-source quality labels for the "↗ host" buttons on cards. Populated
// by the multi-sniff finalize step once each streaming front-end has
// returned its master playlist. setSourceButtons reads from here so the
// user sees "↗ 111movies.net · 1080p · 6.4 Mbps · AAC 5.1" instead of
// just the bare hostname — lets them visually pick the best source when
// the same title is offered by multiple back-ends.
const sourceVariantLabels = new Map();

function parseTopVariantLabel(masterText) {
  // Lightweight HLS master-playlist parser, *display only*. Mirrors the
  // logic in hls_fetcher.py's parse_master + pick_best_audio so the UI
  // shows what the helper will actually pick. Returns "" on parse
  // failure / single-rendition manifests (no quality difference to
  // surface).
  if (!masterText || masterText.indexOf("#EXT-X-STREAM-INF") < 0) return "";
  const lines = masterText.split(/\r?\n/);
  let topH = 0, topBw = 0, audioGroup = "";
  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    if (!s.startsWith("#EXT-X-STREAM-INF:")) continue;
    const a = s.slice("#EXT-X-STREAM-INF:".length);
    const mh = a.match(/RESOLUTION=\d+x(\d+)/);
    const mb = a.match(/BANDWIDTH=(\d+)/);
    const ma = a.match(/AUDIO="([^"]+)"/);
    const h  = mh ? parseInt(mh[1], 10) : 0;
    const bw = mb ? parseInt(mb[1], 10) : 0;
    if (h > topH || (h === topH && bw > topBw)) {
      topH = h; topBw = bw;
      if (ma) audioGroup = ma[1];
    }
  }
  // Best audio in the chosen group: prefer English / no-tag, highest
  // channels. Skip URI-less entries (those describe muxed audio).
  let aTxt = "";
  if (audioGroup) {
    let bestCh = 0, bestLang = "";
    for (const line of lines) {
      const s = line.trim();
      if (!s.startsWith("#EXT-X-MEDIA:")) continue;
      const a = s.slice("#EXT-X-MEDIA:".length);
      if (!/TYPE=AUDIO/.test(a)) continue;
      const mg = a.match(/GROUP-ID="([^"]+)"/);
      if (!mg || mg[1] !== audioGroup) continue;
      if (!/URI="/.test(a)) continue;
      const lang = (a.match(/LANGUAGE="([^"]+)"/) || [,""])[1].toLowerCase();
      const ch   = parseInt((a.match(/CHANNELS="([^"]+)"/) || [,"2"])[1].split("/")[0], 10) || 2;
      // Prefer English / no-tag over foreign; within that, highest channels.
      const langRank = (lang === "en" || lang === "eng") ? 0 : (lang === "" || lang === "und") ? 1 : 2;
      const bestRank = (bestLang === "en" || bestLang === "eng") ? 0 : (bestLang === "" || bestLang === "und") ? 1 : 2;
      if (langRank < bestRank || (langRank === bestRank && ch > bestCh)) {
        bestCh = ch;
        bestLang = lang;
      }
    }
    if (bestCh) {
      const chTxt = bestCh === 6 ? "5.1" : bestCh === 8 ? "7.1" : bestCh === 2 ? "stereo" : `${bestCh}ch`;
      aTxt = `AAC ${chTxt}${bestLang && bestLang !== "en" ? ` (${bestLang})` : ""}`;
    }
  }
  const parts = [];
  if (topH) parts.push(`${topH}p`);
  if (topBw) {
    const mbps = topBw / 1_000_000;
    parts.push(mbps >= 1 ? `${mbps.toFixed(1)} Mbps` : `${Math.round(topBw/1000)} kbps`);
  }
  if (aTxt) parts.push(aTxt);
  return parts.join(" · ");
}

window.__vdHlsStatus = function(d) {
  const card = hlsTasks.get(d.taskId);
  if (card) card.onHlsStatus(d);
};
window.__vdHlsProgress = function(d) {
  const card = hlsTasks.get(d.taskId);
  if (card) card.onHlsProgress(d);
};
window.__vdHlsDone = function(d) {
  const card = hlsTasks.get(d.taskId);
  if (card) { hlsTasks.delete(d.taskId); card.onHlsDone(d); }
};
window.__vdHlsError = function(d) {
  const card = hlsTasks.get(d.taskId);
  if (card) { hlsTasks.delete(d.taskId); card.onHlsError(d); }
};
// New in 0.1.8 — fired once per rip after the helper picks the video
// variant + audio rendition. Payload carries `video`, `audio`, `ladder`,
// and `audio_groups`. The card stores it so subsequent progress lines
// can annotate "Downloading · 47%" with "1080p · AAC 5.1 (en)".
window.__vdHlsVariant = function(d) {
  const card = hlsTasks.get(d.taskId);
  if (card) card.onHlsVariant(d);
};
const canHlsDownload = () => !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.vdHlsStart);
const canEditor = () => !!(window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.vdEditorPrepare);

const editorPrepareWaiters = new Map();
const cardsBySid = new Map(); // editor sid → card (for routing /editor/items + reopens)
window.__vdEditorPrepareReply = function(token, ok, error, sid) {
  const fn = editorPrepareWaiters.get(token);
  if (!fn) return;
  editorPrepareWaiters.delete(token);
  fn(ok, error, sid || "");
};
// Stage 2 extract picker — shown when a probed URL turns out to be a
// multi-asset post (carousel, album, page with multiple media). The user
// reviews the items, picks which to add, and clicks Confirm; each picked
// item becomes its own card in Stage 3.
const extractPicker = (() => {
  const panel = document.getElementById("extract-pick");
  if (!panel) return null;
  const thumbEl = panel.querySelector(".ep-thumb");
  const titleEl = panel.querySelector(".ep-title");
  const subEl = panel.querySelector(".ep-sub");
  const stripEl = panel.querySelector(".ep-strip");
  const countEl = panel.querySelector(".ep-count");
  const allBtn = panel.querySelector(".ep-all");
  const noneBtn = panel.querySelector(".ep-none");
  const confirmBtn = panel.querySelector(".ep-confirm");
  const sniffBtn = panel.querySelector(".ep-sniff");
  const cancelBtn = panel.querySelector(".ep-cancel");
  let items = [], selected = new Set(), sourceUrl = "", sourceTitle = "";

  const updateCount = () => { countEl.textContent = `${selected.size} selected`; };

  const buildTile = (i) => {
    const it = items[i];
    const tile = document.createElement("div");
    tile.className = "gp-item" + (selected.has(i) ? " sel" : "");
    const thumbUrl = it.thumbnail || (it.kind === "image" ? it.url : "");
    if (thumbUrl) tile.style.backgroundImage = `url("${thumbUrl}")`;
    const tag = it.kind === "video" ? "VID" : it.kind === "audio" ? "AUD" : "IMG";
    tile.innerHTML = `<div class="gp-check"></div><div class="gp-kind">${tag}</div>`;
    tile.title = it.title || it.filename || it.url;
    tile.addEventListener("click", () => {
      if (selected.has(i)) { selected.delete(i); tile.classList.remove("sel"); }
      else { selected.add(i); tile.classList.add("sel"); }
      updateCount();
    });
    return tile;
  };

  const show = (info, url) => {
    items = (info.items || []).filter(it => it && it.url);
    selected = new Set(items.map((_, i) => i));
    sourceUrl = url;
    sourceTitle = info.title || url;
    if (info.thumbnail) thumbEl.style.backgroundImage = `url("${info.thumbnail}")`;
    else thumbEl.style.backgroundImage = "";
    titleEl.textContent = sourceTitle;
    const counts = [];
    if (info.n_image) counts.push(`${info.n_image} image${info.n_image===1?"":"s"}`);
    if (info.n_video) counts.push(`${info.n_video} video${info.n_video===1?"":"s"}`);
    if (info.n_audio) counts.push(`${info.n_audio} audio`);
    const subBits = [];
    if (info.uploader) subBits.push(info.uploader);
    if (counts.length) subBits.push(counts.join(", "));
    subBits.push(url);
    subEl.textContent = subBits.join(" · ");
    stripEl.innerHTML = "";
    items.forEach((_, i) => stripEl.appendChild(buildTile(i)));
    updateCount();
    panel.classList.add("show");
  };

  const hide = () => {
    panel.classList.remove("show");
    items = []; selected = new Set();
    sourceUrl = ""; sourceTitle = "";
    stripEl.innerHTML = "";
  };

  const confirm = () => {
    const idxs = [...selected].sort((a, b) => a - b);
    if (!idxs.length) return;
    const total = idxs.length;
    // Insert in carousel order (item 1 first, …) into the Stage 2 pending
    // workshop. User reviews / configures each card and clicks Rip It! to
    // promote it to the Stage 3 Downloads queue.
    let lastEl = null;
    for (let k = 0; k < total; k++) {
      const item = items[idxs[k]];
      const c = makeCard(sourceUrlForItem(item, sourceUrl));
      if (k === 0) {
        document.getElementById("pending").prepend(c.el);
      } else {
        lastEl.after(c.el);
      }
      lastEl = c.el;
      c.populateAsItem(item, sourceTitle, sourceUrl, k, total);
    }
    hide();
    updateStatusBar();
    refreshRipAllButton();
    refreshPendingToolbar();
  };

  allBtn.addEventListener("click", () => {
    selected = new Set(items.map((_, i) => i));
    for (const t of stripEl.children) t.classList.add("sel");
    updateCount();
  });
  noneBtn.addEventListener("click", () => {
    selected = new Set();
    for (const t of stripEl.children) t.classList.remove("sel");
    updateCount();
  });
  confirmBtn.addEventListener("click", confirm);
  sniffBtn.addEventListener("click", () => {
    // Force-sniff the source URL: drop whatever the probe found and let
    // the in-page sniffer try to capture real media instead. Useful when
    // probe returns just a thumbnail / og:image for a page that has a
    // real video player.
    if (!sourceUrl) return;
    if (!canSniff()) {
      alert("In-page sniffer not available in this build.");
      return;
    }
    const u = sourceUrl;
    hide();
    const c = makeCard(u);
    c.el.querySelector(".job-title").value = u;
    document.getElementById("pending").prepend(c.el);
    updateStatusBar();
    refreshRipAllButton();
    refreshPendingToolbar();
    c.startSniff();
  });
  cancelBtn.addEventListener("click", hide);

  return { show, hide };
})();


function sourceUrlForItem(item, fallbackUrl) {
  // What we put in the new item card's URL slot for display purposes.
  // Prefer the item's own webpage_url (e.g. a YouTube embed extracted
  // from a generic page); else the parent post URL.
  return (item && (item.webpage_url || item.referer)) || fallbackUrl || "";
}

function labelFor(u) {
  try {
    const p = new URL(u);
    const name = (p.pathname.split("/").pop() || u);
    const ext = (name.match(/\.(m3u8|mpd|mp4|webm|m4s|m4v)/i)||[])[1] || "";
    return name + (ext ? "" : "");
  } catch(e) { return u; }
}

// ─── "Get Ripped!" overlay animation ─────────────────────────────────────
// Ported from design bundle: 4 jagged dino-claw rips swipe diagonally over
// the wordmark PNG, 4-second story (slam → swipe → fade). Triggered on any
// Rip click (Rip It! / Rip Again! / Rip All).
const _RIP_PRESETS = [
  { angle:  22, cx: 512, cy: 192 },
  { angle: -22, cx: 512, cy: 192 },
  { angle:  35, cx: 452, cy: 222 },
  { angle: -35, cx: 452, cy: 162 },
  { angle:   8, cx: 512, cy: 202 },
  { angle:  -8, cx: 512, cy: 182 },
  { angle:  60, cx: 472, cy: 192 },
  { angle: -60, cx: 552, cy: 192 },
  { angle: 155, cx: 592, cy: 172 },
  { angle:-155, cx: 592, cy: 212 },
];
const _RIP_PALETTE = { glow: "#a00000", mid: "#b8000a" }; // blood
let _ripBusy = false;

function _ripEaseOutBack(x) {
  const c1 = 1.70158, c3 = c1 + 1;
  return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2);
}

function _ripBuildClawD(seed, length, width) {
  const segs = 24;
  const top = [], bot = [];
  const rnd = (i, salt) => {
    const x = Math.sin(seed * 12.9 + i * 78.2 + salt * 37.1) * 43758.5453;
    return x - Math.floor(x);
  };
  for (let i = 0; i <= segs; i++) {
    const s = i / segs;
    const x = s * length;
    const taper = Math.sin(s * Math.PI);
    const halfThick = width * taper;
    const jag = (rnd(i, 1) - 0.5) * 2;
    const jagAmount = halfThick * 0.45;
    if (i === 0 || i === segs) {
      top.push([x, 0]); bot.push([x, 0]);
    } else {
      top.push([x, -halfThick + jag * jagAmount]);
      bot.push([x,  halfThick + jag * jagAmount]);
    }
  }
  const pts = [...top, ...bot.reverse()];
  return "M " + pts.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(" L ") + " Z";
}

function playRipAnimation(opts) {
  // User-muted via Settings → Animations. Bail before allocating the
  // overlay so we don't waste DOM churn on something that's hidden.
  if (!isAnimEnabled("rip")) return;
  opts = opts || {};
  const count = Math.max(1, opts.count || 1);
  const stagger = opts.stagger != null ? opts.stagger : 180; // ms between bursts
  if (_ripBusy) return;
  _ripBusy = true;
  let remaining = count;
  const done = () => { remaining--; if (remaining <= 0) _ripBusy = false; };
  // For multi-burst calls (Rip All Pending: 3 staggered slashes) we
  // only want the GET RIPPED! wordmark on the *first* overlay — three
  // wordmarks stacking on top of each other looks like a render bug.
  // Subsequent bursts just contribute fresh slash claws.
  for (let i = 0; i < count; i++) {
    const showWordmark = (i === 0);
    setTimeout(() => _spawnRipOverlay(done, { withWordmark: showWordmark }), i * stagger);
  }
}

// ───── Error animation ──────────────────────────────────────────────────
// Win98-style dialog window shown when a card transitions to error
// state (see card.setStatus's edge-trigger). The video plays once,
// muted, then naturally pauses on its last frame. The popup stays
// open until the user closes it via the title-bar X (or Esc, or
// clicking the dim backdrop). Single-flight — if a popup is already
// up, additional errors don't stack.
function playErrorAnimation() {
  if (!isAnimEnabled("error")) return;
  if (document.getElementById("rr-err-popup-overlay")) return;
  const overlay = document.createElement("div");
  overlay.id = "rr-err-popup-overlay";
  overlay.className = "rr-err-popup-overlay";
  overlay.innerHTML = `
    <div class="rr-err-popup" role="dialog" aria-modal="true"
         aria-labelledby="rr-err-popup-title">
      <div class="rr-err-popup-titlebar">
        <span class="rr-err-popup-title" id="rr-err-popup-title">Error</span>
        <button class="rr-err-popup-close" aria-label="Close">×</button>
      </div>
      <div class="rr-err-popup-body">
        <video class="rr-err-popup-vid" src="/something-went-wrong.mp4"
               autoplay muted playsinline preload="auto"
               disableremoteplayback></video>
      </div>
    </div>`;

  let dismissed = false;
  function dismiss() {
    if (dismissed) return;
    dismissed = true;
    document.removeEventListener("keydown", onKey);
    try { overlay.remove(); } catch (e) {}
  }
  function onKey(e) {
    if (e.key === "Escape") { e.preventDefault(); dismiss(); }
  }

  overlay.querySelector(".rr-err-popup-close").addEventListener("click", dismiss);
  // Click the dim backdrop (overlay element itself, not the popup
  // card) closes — matches the IMDB modal affordance.
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) dismiss();
  });
  document.addEventListener("keydown", onKey);

  const vid = overlay.querySelector("video");
  if (vid) {
    // Deliberately NO `ended` handler — the video naturally pauses
    // on its last frame and the popup stays open until the user X's
    // out. `error` still dismisses; if the file 404s or fails to
    // decode there's no point keeping a broken popup up.
    vid.addEventListener("error", dismiss);
    // muted autoplay should always work in modern browsers — no
    // fallback dismiss on play() failure.
    vid.play().catch(() => {});
  }
  document.body.appendChild(overlay);
}

function _spawnRipOverlay(onDone, spawnOpts) {
  spawnOpts = spawnOpts || {};
  const withWordmark = spawnOpts.withWordmark !== false; // default true
  const W = 1024, H = 384;
  const SLAM_START = 0.05, SLAM_DUR = 0.40;
  const SWIPE_START = 0.50, SWIPE_DUR = 0.10;
  const FADE_START = SWIPE_START + SWIPE_DUR + 0.15;
  const FADE_DUR = 0.30;
  const TOTAL = FADE_START + FADE_DUR;

  const rs = _RIP_PRESETS[Math.floor(Math.random() * _RIP_PRESETS.length)];
  const claws = [
    { off: -135, len: 540, w: 26, lenStart: 0.00 },
    { off:  -45, len: 600, w: 32, lenStart: 0.02 },
    { off:   45, len: 600, w: 32, lenStart: 0.04 },
    { off:  135, len: 480, w: 24, lenStart: 0.06 },
  ];
  const seedBase = Math.random() * 1000;
  const rad = rs.angle * Math.PI / 180;
  const px = -Math.sin(rad), py = Math.cos(rad);
  const cosA = Math.cos(rad), sinA = Math.sin(rad);

  const overlay = document.createElement("div");
  overlay.className = "rr-rip-overlay";
  const clawSVG = claws.map((c, i) => {
    const ox = rs.cx + px * c.off - cosA * c.len / 2;
    const oy = rs.cy + py * c.off - sinA * c.len / 2;
    const d = _ripBuildClawD(seedBase + i * 1.7, c.len, c.w);
    return `<g transform="translate(${ox.toFixed(1)} ${oy.toFixed(1)}) rotate(${rs.angle})" clip-path="url(#rr-claw-clip-${i})">
      <path d="${d}" fill="${_RIP_PALETTE.glow}" opacity="0.55" style="filter:blur(10px)"/>
      <path d="${d}" fill="#000" stroke="#000" stroke-width="6" stroke-linejoin="round" stroke-linecap="round"/>
      <path d="${d}" fill="${_RIP_PALETTE.mid}" stroke="${_RIP_PALETTE.mid}" stroke-width="0.5" transform="scale(0.93)" style="transform-box:fill-box;transform-origin:center"/>
      <circle data-tip="${i}" cx="0" cy="0" r="14" fill="#fff" opacity="0" style="filter:drop-shadow(0 0 16px ${_RIP_PALETTE.glow})"/>
      <circle data-tipglow="${i}" cx="0" cy="0" r="28" fill="${_RIP_PALETTE.glow}" opacity="0" style="filter:blur(8px)"/>
    </g>`;
  }).join("");
  const clipDefs = claws.map((c, i) =>
    `<clipPath id="rr-claw-clip-${i}"><rect data-rect="${i}" x="0" y="-100" width="0" height="200"/></clipPath>`
  ).join("");

  overlay.innerHTML = `
    <div class="rr-rip-viewport">
      ${withWordmark ? '<img class="rr-rip-wordmark" src="/get-ripped.png" alt="GET RIPPED!" draggable="false">' : ''}
      <div class="rr-rip-flash"></div>
      <svg class="rr-rip-claws" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
        <defs>${clipDefs}</defs>
        ${clawSVG}
      </svg>
    </div>`;
  document.body.appendChild(overlay);

  const viewport = overlay.querySelector(".rr-rip-viewport");
  const wordmark = overlay.querySelector(".rr-rip-wordmark"); // null on follow-on bursts
  const flash = overlay.querySelector(".rr-rip-flash");
  const svg = overlay.querySelector(".rr-rip-claws");
  const rects = Array.from(svg.querySelectorAll("[data-rect]"));
  const tips = Array.from(svg.querySelectorAll("[data-tip]"));
  const tipGlows = Array.from(svg.querySelectorAll("[data-tipglow]"));

  const fitViewport = () => {
    const s = Math.min(window.innerWidth / W, window.innerHeight / H) * 0.85;
    viewport.style.transform = `translate(-50%, -50%) scale(${Math.max(0.1, s).toFixed(3)})`;
  };
  fitViewport();
  const onResize = () => fitViewport();
  window.addEventListener("resize", onResize);

  const startMs = performance.now();
  function tick(now) {
    const t = (now - startMs) / 1000;
    if (t >= TOTAL) {
      window.removeEventListener("resize", onResize);
      overlay.remove();
      onDone && onDone();
      return;
    }

    let op;
    if (t < SLAM_START) op = 0;
    else if (t < SLAM_START + 0.1) op = (t - SLAM_START) / 0.1;
    else if (t < FADE_START) op = 1;
    else op = Math.max(0, 1 - (t - FADE_START) / FADE_DUR);
    overlay.style.opacity = op;

    const slamT = Math.max(0, Math.min(1, (t - SLAM_START) / SLAM_DUR));
    const slamEase = _ripEaseOutBack(slamT);
    const baseScale = 0.4 + slamEase * 0.6;
    const baseRot = -8 + slamEase * 8;
    const wobble = t > SLAM_DUR ? Math.sin((t - SLAM_DUR) * 7) * 0.6 : 0;
    const shakeT = (t - SWIPE_START) / 0.25;
    const sx = (shakeT > 0 && shakeT < 1) ? Math.sin(shakeT * 60) * 4 : 0;
    const sy = (shakeT > 0 && shakeT < 1) ? Math.cos(shakeT * 55) * 4 : 0;
    if (wordmark) {
      wordmark.style.transform = `translate(calc(-50% + ${sx.toFixed(2)}px), calc(-50% + ${sy.toFixed(2)}px)) rotate(${(baseRot + wobble).toFixed(3)}deg) scale(${baseScale.toFixed(3)})`;
    }

    const swipeT = (t - SWIPE_START) / SWIPE_DUR;
    if (swipeT < 0) {
      svg.style.opacity = 0;
    } else if (swipeT < 1) {
      svg.style.opacity = 1;
    } else {
      const fadeT = (t - SWIPE_START - SWIPE_DUR) / FADE_DUR;
      svg.style.opacity = Math.max(0, 1 - fadeT);
    }
    if (swipeT >= 0) {
      for (let i = 0; i < claws.length; i++) {
        const c = claws[i];
        let localT = (swipeT - c.lenStart) / (1 - c.lenStart);
        if (localT < 0) localT = 0; else if (localT > 1) localT = 1;
        rects[i].setAttribute("width", c.len * localT + 6);
        const tipX = c.len * localT;
        const drawing = localT > 0 && localT < 1;
        tips[i].setAttribute("cx", tipX);
        tips[i].setAttribute("opacity", drawing ? 1 : 0);
        tipGlows[i].setAttribute("cx", tipX);
        tipGlows[i].setAttribute("opacity", drawing ? 0.6 : 0);
      }
    }

    const flashT = (t - (SWIPE_START + 0.02)) / 0.10;
    flash.style.opacity = (flashT >= 0 && flashT < 1) ? (1 - flashT) * 0.4 : 0;

    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function refreshDownloadsEmpty() {
  // Stage 3 placeholder is shown only when #jobs has nothing.
  const jobs = document.getElementById("jobs");
  const empty = document.getElementById("empty");
  if (!jobs || !empty) return;
  empty.style.display = jobs.children.length ? "none" : "";
}
function refreshPendingToolbar() {
  // Stage 2's "Rip All Pending" toolbar shows only when there are pending cards.
  const pending = document.getElementById("pending");
  const tb = document.getElementById("pending-toolbar");
  if (!pending || !tb) return;
  tb.style.display = pending.children.length ? "" : "none";
}
function refreshDownloadsToolbar() {
  // Stage 3's bulk-actions toolbar — only visible when there's at least
  // one job with state="error" or state="done". Counts inform the
  // summary text and which buttons are active.
  const jobs = document.getElementById("jobs");
  const tb = document.getElementById("downloads-toolbar");
  if (!jobs || !tb) return;
  let total = 0, done = 0, failed = 0, active = 0;
  for (const c of jobs.children) {
    total++;
    const s = c.dataset.state || "";
    if (s === "done") done++;
    else if (s === "error") failed++;
    else active++;
  }
  if (done === 0 && failed === 0) {
    tb.style.display = "none";
    return;
  }
  tb.style.display = "";
  document.getElementById("btn-retry-all-failed").disabled = (failed === 0);
  document.getElementById("btn-reveal-all").disabled = (done === 0);
  document.getElementById("btn-clear-completed").disabled = (done + failed === 0);
  const bits = [];
  if (active) bits.push(`${active} running`);
  if (done)   bits.push(`${done} done`);
  if (failed) bits.push(`${failed} failed`);
  document.getElementById("downloads-summary").textContent = bits.join(" · ");
}
// Move a card up (-1) or down (+1) within its parent (#pending). Used
// by the per-card ↑/↓ arrow buttons in Stage 2.
function moveCardByOne(cardEl, dir) {
  if (!cardEl || !cardEl.parentElement) return;
  const parent = cardEl.parentElement;
  if (parent.id !== "pending") return;  // arrows only meaningful pre-rip
  if (dir < 0) {
    const prev = cardEl.previousElementSibling;
    if (prev) parent.insertBefore(cardEl, prev);
  } else {
    const next = cardEl.nextElementSibling;
    if (next) parent.insertBefore(next, cardEl);
  }
}
// Wholesale "Cancel All Pending" — discards every card currently in
// #pending. Triggers each card's own remove handler so SSE/job
// teardown happens correctly.
function cancelAllPending() {
  const pending = document.getElementById("pending");
  if (!pending) return;
  const n = pending.children.length;
  if (n === 0) return;
  if (n > 1 && !confirm(`Discard all ${n} pending cards?`)) return;
  // Snapshot the children; clicking remove mutates the live collection.
  const cards = Array.from(pending.children);
  for (const c of cards) {
    const removeBtn = c.querySelector(".remove");
    if (removeBtn) removeBtn.click();
  }
}
function updateStatusBar(status) {
  const cards = $("#jobs").children;
  let active = 0;
  for (const c of cards) {
    const s = c.dataset.state || "";
    if (s !== "done" && s !== "error") active++;
  }
  $("#sb-jobs").textContent = active + " active";
  if (status !== undefined) {
    $("#sb-status").textContent = status;
  } else if (active === 0) {
    $("#sb-status").textContent = "Ready";
  } else {
    $("#sb-status").textContent = "Ripping…";
  }
}
async function probeAndPopulate(card, url) {
  // Shared probe-and-hand-off logic. Used by addUrl on first try, and by
  // card.retryProbe when the user supplies cookies after an auth error.

  // Fast-path: certain hosts (e.g. the 111movies target of the IMDB
  // rewrite) require the in-page sniffer because yt-dlp can't see their
  // media without rendering the page. Skip the probe round trip — it
  // would just fail and fall through to startSniff() anyway. Saves a
  // few seconds and avoids a misleading "Probing formats…" status.
  if (_isSniffOnly(url) && canSniff()) {
    card.el.querySelector(".job-title").value = url;
    card.startSniff();
    return { ok: true };
  }

  card.el.dataset.state = "probing";
  card.setStatus('<span class="spinner"></span> Probing formats…');
  let j, errMsg = "", errHint = "";
  try {
    const r = await fetch("/probe", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({url, cookies_browser: getCookiesBrowser()}),
    });
    j = await r.json();
    if (!r.ok) { errMsg = j.error || "probe failed"; errHint = j.hint || ""; }
  } catch (e) {
    errMsg = String(e.message || e);
  }
  if (!errMsg) {
    // Low-confidence single-image fallback on a video-likely host: prefer
    // the in-page sniffer over the picker, falling back to the picker if
    // sniff also turns up nothing.
    if (j && j.kind === "gallery" && j.low_confidence && canSniff()) {
      card._lowConfidenceFallback = { info: j, url };
      card.el.querySelector(".job-title").value = url;
      card.startSniff();
      return { ok: true };
    }
    card.populate(j);
    return { ok: true };
  }
  // Auth/age hints get the per-card retry UI rather than the sniff fallback.
  card.el.querySelector(".job-title").value = url;
  if (errHint === "auth" || errHint === "age") {
    card.showAuthRetry(errMsg, errHint);
    return { ok: false, hint: errHint };
  }
  // Other errors → fall back to the in-app browser sniff if available.
  if (canSniff()) {
    card.startSniff();
  } else {
    card.el.dataset.state = "error";
    // Show the error message + an actionable Retry button. Retry calls
    // probeAndPopulate() on the original URL, picking up any cookie /
    // network changes the user made in the meantime.
    card.setStatus(
      `Could not probe: ${esc(errMsg)}<br>` +
      `<button class="btn-probe-retry" style="margin-top:6px;">↻ Retry</button>`,
      "err"
    );
    const retry = card.el.querySelector(".btn-probe-retry");
    if (retry) {
      retry.addEventListener("click", () => {
        if (typeof card.retryProbe === "function") card.retryProbe();
      });
    }
    updateStatusBar();
  }
  return { ok: false };
}

// Pull every URL out of a chunk of pasted text. Handles space-, comma-,
// or newline-separated lists; ignores junk that doesn't look like an
// http(s) URL.
function _extractUrls(text) {
  if (!text) return [];
  const matches = String(text).match(/https?:\/\/[^\s,;<>"']+/gi) || [];
  // Strip trailing punctuation that often gets glued to a URL when copied
  // out of prose ("...read here.").
  return matches.map(u => u.replace(/[.,;:!?)\]]+$/, ""));
}

// URL rewriters. Each entry is [pattern, builder]; on a match the
// builder produces the replacement URL. New rewrites are a one-liner —
// add to the list and both addUrl() and /queue pick them up.
//
// IMDB inputs are handled separately via _detectImdbId + the
// movie/tv prompt below — the prompt resolves to the final 111movies
// URL before this list is consulted. So this list is intentionally
// empty for now and exists for future non-IMDB rewrites.
const URL_REWRITERS = [];
function _rewriteUrl(url) {
  for (const [pat, fn] of URL_REWRITERS) {
    const m = String(url || "").match(pat);
    if (m) return fn(m);
  }
  return url;
}

// Detect an IMDB title id from any of the shapes the user might type or
// paste. Returns either null or {id, season?, episode?}; the optional
// season/episode are extracted from the raw "tt12345/2/5" shorthand so
// the modal can prefill them.
function _detectImdbId(text) {
  const s = String(text || "").trim();
  // Raw "tt<digits>/<season>/<episode>" — pre-typed series shorthand.
  let m = s.match(/^(tt\d+)\/(\d+)\/(\d+)\/?$/i);
  if (m) return { id: m[1].toLowerCase(),
                  season: parseInt(m[2], 10), episode: parseInt(m[3], 10) };
  // Raw "tt<digits>" with optional trailing slash.
  m = s.match(/^(tt\d+)\/?$/i);
  if (m) return { id: m[1].toLowerCase() };
  // Full IMDB title URL.
  m = s.match(/^https?:\/\/(?:www\.)?imdb\.com\/title\/(tt\d+)/i);
  if (m) return { id: m[1].toLowerCase() };
  return null;
}

// Open a live IMDB title search modal. User types a query → we
// debounce-fetch /imdb/search → render results with poster thumbs →
// click a result to feed its tt-id straight into addUrl(), which then
// runs the existing IMDB pipeline (movie/TV modal + multi-sniff +
// auto-name). The modal is dismissable via Esc, Cancel, or clicking
// the dim backdrop.
//
// `initialQuery` (optional) pre-fills the search input and runs the
// query immediately — used when the user hits Cmd+Enter with text
// already in the URL bar so the modal opens already-populated.
function openImdbSearch(initialQuery) {
  if (document.getElementById("rr-imdb-search-modal")) return;
  const modal = document.createElement("div");
  modal.id = "rr-imdb-search-modal";
  modal.className = "rr-imdb-modal";  // reuse backdrop styling
  modal.innerHTML = `
    <div class="rr-imdb-search-card" role="dialog" aria-modal="true"
         aria-labelledby="rr-imdb-s-h">
      <h3 id="rr-imdb-s-h">Search IMDB</h3>
      <input id="rr-search-input" type="text"
             placeholder="Title — e.g. Game of Thrones, The Dark Knight"
             autocomplete="off" spellcheck="false">
      <div id="rr-search-status" class="rr-search-status">
        Type a title to search.
      </div>
      <div id="rr-search-results"></div>
      <div class="rr-imdb-actions">
        <button id="rr-search-cancel">Cancel</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const inputEl   = modal.querySelector("#rr-search-input");
  const statusEl  = modal.querySelector("#rr-search-status");
  const resultsEl = modal.querySelector("#rr-search-results");

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    try { modal.remove(); } catch (e) {}
  }
  modal.querySelector("#rr-search-cancel").addEventListener("click", close);
  modal.addEventListener("click", (e) => {
    // Click on the dim backdrop (modal itself, not the card) closes.
    if (e.target === modal) close();
  });
  modal.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
  });

  let timer = null;
  let lastQuery = "";  // outdated-response guard for racing fetches
  inputEl.addEventListener("input", () => {
    if (timer) { clearTimeout(timer); timer = null; }
    const q = inputEl.value.trim();
    if (!q) {
      lastQuery = "";
      statusEl.textContent = "Type a title to search.";
      resultsEl.innerHTML = "";
      return;
    }
    timer = setTimeout(() => doSearch(q), 250);
  });

  async function doSearch(q) {
    lastQuery = q;
    statusEl.textContent = "Searching…";
    let j;
    try {
      const r = await fetch(`/imdb/search?q=${encodeURIComponent(q)}`);
      if (!r.ok) {
        if (q !== lastQuery) return;
        // Pull the backend's error message out of the JSON body so
        // the user sees WHY (e.g. "imdb fetch failed: <ImportError>")
        // instead of just "HTTP 502".
        let detail = "";
        try {
          const errJson = await r.json();
          detail = errJson && errJson.error ? errJson.error : "";
        } catch (e) {}
        statusEl.textContent = "Search failed: " + (detail || ("HTTP " + r.status));
        return;
      }
      j = await r.json();
    } catch (e) {
      if (q !== lastQuery) return;
      statusEl.textContent = "Search error: " + (e.message || e);
      return;
    }
    if (q !== lastQuery) return;  // a newer query landed first; drop this
    const list = j.results || [];
    if (list.length === 0) {
      statusEl.textContent = "No results.";
      resultsEl.innerHTML = "";
      return;
    }
    statusEl.textContent = `${list.length} result${list.length === 1 ? "" : "s"}`;
    resultsEl.innerHTML = "";
    for (const item of list) {
      const row = document.createElement("div");
      row.className = "rr-search-result";
      row.dataset.id = item.id;
      row.dataset.kind = item.kind;
      const yearStr = item.year ? ` (${item.year})` : "";
      const subBits = [item.qLabel, item.extra].filter(Boolean);
      // referrerpolicy=no-referrer keeps IMDB CDN happy when their
      // poster URLs are loaded from a non-imdb host.
      const thumbHtml = item.thumb
        ? `<img class="rr-search-thumb" src="${item.thumb}" alt="" referrerpolicy="no-referrer">`
        : `<div class="rr-search-thumb rr-search-thumb-empty"></div>`;
      row.innerHTML = `
        ${thumbHtml}
        <div class="rr-search-meta">
          <div class="rr-search-title">${esc(item.title)}${yearStr}</div>
          <div class="rr-search-sub">${esc(subBits.join(" · "))}</div>
        </div>
      `;
      row.addEventListener("click", () => {
        close();
        // Stash the poster URL so the card we're about to spawn can
        // show the cover art *immediately* — without waiting for the
        // multi-source sniff to find a thumbnail in the streaming
        // page metadata. Keyed by IMDB id so the lookup at card-
        // construction time is unambiguous.
        if (item.thumb && item.id) {
          imdbThumbCache.set(item.id, item.thumb);
        }
        // Also cache by every URL the IMDB rewrite path will
        // generate from this id (so if the user later types the
        // 111movies/streamimdb URL directly we still find the
        // poster). Cheap belt-and-suspenders.
        const inp = $("#url");
        if (inp) {
          inp.value = item.id;
          addUrl();
        }
      });
      resultsEl.appendChild(row);
    }
  }

  // Seed-search support: if Cmd+Enter was hit with text in the URL
  // bar, plug it into the input and kick off the search now (no
  // debounce — the user already showed intent). Select all so the
  // user can immediately type to replace if the seed wasn't quite
  // what they wanted.
  setTimeout(() => {
    inputEl.focus();
    if (initialQuery && initialQuery.trim()) {
      inputEl.value = initialQuery.trim();
      inputEl.select();
      doSearch(inputEl.value.trim());
    }
  }, 0);
}

// Build the candidate URL list for an IMDB title. We sniff every entry
// in parallel via startMultiSniff and merge the variants into a single
// quality dropdown — so the user sees the best option from any source.
// New streaming front-ends are a one-liner — append a builder here.
function _imdbCandidateUrls(detected) {
  const id = detected.id;
  if (detected.kind === "tv") {
    const s = Math.max(1, parseInt(detected.season, 10) || 1);
    const e = Math.max(1, parseInt(detected.episode, 10) || 1);
    return [
      `https://111movies.net/tv/${id}/${s}/${e}`,
      `https://streamimdb.ru/embed/tv/${id}/${s}/${e}`,
    ];
  }
  return [
    `https://111movies.net/movie/${id}`,
    `https://streamimdb.ru/embed/movie/${id}`,
  ];
}

// Show the movie/TV prompt for an IMDB id. Resolves to {kind, season?,
// episode?} (or null if cancelled). The caller turns this into the
// candidate URL list via _imdbCandidateUrls. Modal is Win98-styled to
// match the rest of the app.
//
// Optional `metaPromise` is a Promise<{kind, title, year}> from
// /imdb/title. When it resolves before the user has touched the radio,
// we flip the movie/TV selection to whatever IMDB says.
//
// Optional `episodesPromise` is a Promise<{seasons:[{season, episodes:
// [{episode, name}]}]}> from /imdb/episodes. When it resolves with
// non-empty seasons, the TV section's number inputs get replaced with
// season + episode-name dropdowns so the user picks by title rather
// than guessing numbers.
function _showImdbPrompt(detected, metaPromise, episodesPromise) {
  return new Promise((resolve) => {
    // If a modal is already open (multi-paste of imdb urls), reuse the
    // single-flight slot — only one prompt at a time so the user isn't
    // overwhelmed. Subsequent ones queue via the awaited Promise chain.
    const existing = document.getElementById("rr-imdb-modal");
    if (existing) existing.remove();
    const modal = document.createElement("div");
    modal.id = "rr-imdb-modal";
    modal.className = "rr-imdb-modal";
    const initialKind = detected.season != null ? "tv" : "movie";
    const initialSeason  = detected.season  != null ? detected.season  : 1;
    const initialEpisode = detected.episode != null ? detected.episode : 1;
    modal.innerHTML = `
      <div class="rr-imdb-card" role="dialog" aria-modal="true"
           aria-labelledby="rr-imdb-h">
        <h3 id="rr-imdb-h">IMDB: <span class="mono">${detected.id}</span></h3>
        <p>Movie or TV episode?</p>
        <div class="rr-imdb-radios">
          <label><input type="radio" name="rr-imdb-kind" value="movie"
                        ${initialKind === "movie" ? "checked" : ""}> Movie</label>
          <label><input type="radio" name="rr-imdb-kind" value="tv"
                        ${initialKind === "tv" ? "checked" : ""}> TV episode</label>
        </div>
        <div class="rr-imdb-tv" ${initialKind === "movie" ? 'style="display:none;"' : ""}>
          <div class="rr-imdb-num-inputs">
            <label>Season&nbsp;<input id="rr-imdb-season"  type="number" min="1" value="${initialSeason}"></label>
            <label>Episode&nbsp;<input id="rr-imdb-episode" type="number" min="1" value="${initialEpisode}"></label>
          </div>
          <div class="rr-imdb-dropdowns" style="display:none;">
            <label>Season&nbsp;<select id="rr-imdb-season-sel"></select></label>
            <label>Episode&nbsp;<select id="rr-imdb-episode-sel"></select></label>
          </div>
        </div>
        <div class="rr-imdb-actions">
          <button id="rr-imdb-cancel">Cancel</button>
          <button id="rr-imdb-ok" class="primary">OK</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    const tvBox = modal.querySelector(".rr-imdb-tv");
    const radios = modal.querySelectorAll('input[name="rr-imdb-kind"]');
    let userTouched = false;
    radios.forEach(r => r.addEventListener("change", () => {
      userTouched = true;
      tvBox.style.display = (r.checked && r.value === "tv") ? "" : "none";
    }));

    // If we have a metaPromise, update kind selection + heading once
    // it resolves — but only if the user hasn't manually flipped the
    // radio yet. Also surface the looked-up title in the heading so
    // the user has confidence the right title was matched.
    const headingEl = modal.querySelector("#rr-imdb-h");
    if (metaPromise && headingEl) {
      metaPromise.then(meta => {
        if (!meta) return;
        if (meta.title) {
          headingEl.innerHTML = `${esc(meta.title)}${meta.year ? ` (${meta.year})` : ""}<br><span class="mono" style="font-weight:normal">${esc(detected.id)}</span>`;
        }
        if (meta.kind && !userTouched && (meta.kind === "movie" || meta.kind === "tv")) {
          const radio = modal.querySelector(`input[name="rr-imdb-kind"][value="${meta.kind}"]`);
          if (radio && !radio.checked) {
            radio.checked = true;
            tvBox.style.display = meta.kind === "tv" ? "" : "none";
          }
        }
      });
    }

    // Episode-name dropdowns. Sourced from cinemeta. When this resolves
    // with a non-empty seasons list, swap the number inputs for two
    // <select> dropdowns: Season N + "1. Episode Name" entries. We
    // sync any value the user already typed into the numbers so the
    // dropdown opens on whatever they were aiming at.
    const numBox = modal.querySelector(".rr-imdb-num-inputs");
    const ddBox  = modal.querySelector(".rr-imdb-dropdowns");
    const selSeason  = modal.querySelector("#rr-imdb-season-sel");
    const selEpisode = modal.querySelector("#rr-imdb-episode-sel");
    let episodesIndex = null;  // {seasonNum: [{episode, name}, ...]}
    function populateEpisodes(seasonNum) {
      selEpisode.innerHTML = "";
      if (!episodesIndex) return;
      const eps = episodesIndex[String(seasonNum)] || [];
      for (const ep of eps) {
        const o = document.createElement("option");
        o.value = String(ep.episode);
        o.dataset.epname = ep.name || "";
        o.textContent = `${ep.episode}. ${ep.name}`;
        selEpisode.appendChild(o);
      }
    }
    if (episodesPromise && selSeason && selEpisode) {
      episodesPromise.then(eps => {
        if (!eps || !Array.isArray(eps.seasons) || !eps.seasons.length) return;
        // Build season-number index for quick lookup on change events.
        episodesIndex = {};
        for (const s of eps.seasons) {
          episodesIndex[String(s.season)] = s.episodes || [];
        }
        // Populate Season dropdown.
        selSeason.innerHTML = "";
        for (const s of eps.seasons) {
          const o = document.createElement("option");
          o.value = String(s.season);
          o.textContent = `Season ${s.season}`;
          selSeason.appendChild(o);
        }
        // Sync dropdown selection to whatever the user has in the
        // number inputs (which start out at 1/1 or whatever was
        // detected from "tt12345/2/5"). If their typed season/episode
        // doesn't exist in the cinemeta data, fall back to S1E1.
        const numSeason  = parseInt(modal.querySelector("#rr-imdb-season").value, 10)  || 1;
        const numEpisode = parseInt(modal.querySelector("#rr-imdb-episode").value, 10) || 1;
        const seasonOpt = [...selSeason.options].find(o => o.value === String(numSeason));
        if (seasonOpt) selSeason.value = String(numSeason);
        populateEpisodes(parseInt(selSeason.value, 10));
        const epOpt = [...selEpisode.options].find(o => o.value === String(numEpisode));
        if (epOpt) selEpisode.value = String(numEpisode);
        // Re-populate episodes when the season changes.
        selSeason.addEventListener("change", () => {
          populateEpisodes(parseInt(selSeason.value, 10));
        });
        // Swap visibility: dropdowns over numbers.
        if (numBox) numBox.style.display = "none";
        if (ddBox)  ddBox.style.display  = "";
      });
    }

    let settled = false;
    function close(result) {
      if (settled) return;
      settled = true;
      try { modal.remove(); } catch (e) {}
      resolve(result);
    }
    modal.querySelector("#rr-imdb-cancel").addEventListener("click", () => close(null));
    modal.querySelector("#rr-imdb-ok").addEventListener("click", () => {
      const kind = modal.querySelector('input[name="rr-imdb-kind"]:checked').value;
      const choice = { id: detected.id, kind };
      if (kind === "tv") {
        // Read from whichever set is visible. The dropdowns appear
        // when cinemeta returned episode data; otherwise the number
        // inputs stay live and editable.
        const dropdownsActive = ddBox && ddBox.style.display !== "none";
        if (dropdownsActive) {
          choice.season  = parseInt(selSeason.value, 10)  || 1;
          choice.episode = parseInt(selEpisode.value, 10) || 1;
          // Carry the episode name through so the filename autoname
          // below can build "Series - S01E01 - Episode Title".
          const epOpt = selEpisode.selectedOptions[0];
          if (epOpt) choice.episodeName = epOpt.dataset.epname || "";
        } else {
          choice.season  = parseInt(modal.querySelector("#rr-imdb-season").value, 10) || 1;
          choice.episode = parseInt(modal.querySelector("#rr-imdb-episode").value, 10) || 1;
        }
      }
      close(choice);
    });
    // Esc cancels, Enter submits — same affordances as a native dialog.
    modal.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(null); }
      else if (e.key === "Enter") {
        e.preventDefault();
        modal.querySelector("#rr-imdb-ok").click();
      }
    });
    // Focus the OK button so Enter immediately confirms the default.
    setTimeout(() => modal.querySelector("#rr-imdb-ok").focus(), 0);
  });
}

async function addUrl() {
  const inp = $("#url");
  const raw = inp.value.trim();
  if (!raw) return;
  inp.value = "";
  // Build the candidate list. _extractUrls handles multi-URL paste; if
  // there's no http(s):// match (e.g. a bare "tt1234567"), fall back to
  // the raw input so the IMDB detector still has a chance.
  const candidates = _extractUrls(raw);
  if (candidates.length === 0) candidates.push(raw);

  // Resolve each candidate. IMDB inputs trigger the movie/TV modal,
  // then spawn a single card that sniffs N streaming front-ends in
  // parallel (111movies + streamimdb at minimum) and merges every
  // variant into one quality dropdown. Non-IMDB candidates take the
  // existing single-URL path (probe → sniff fallback).
  const imdbJobs = [];   // {choice, urls, titlePromise} for IMDB inputs
  const finalUrls = [];  // plain URLs that go through the legacy path
  for (const cand of candidates) {
    const detected = _detectImdbId(cand);
    if (detected) {
      // Fire both IMDB lookups the moment we recognise the id — they
      // overlap with the modal showing. Title gives us the kind hint
      // (auto-flips Movie/TV) and the year. Episodes gives us per-
      // season episode names (replaces number inputs with named
      // dropdowns once it lands; if it's a movie, cinemeta returns
      // empty seasons and the modal stays on number inputs).
      const metaPromise = fetch(`/imdb/title?id=${encodeURIComponent(detected.id)}`)
        .then(r => r.ok ? r.json() : null)
        .catch(() => null);
      const episodesPromise = fetch(`/imdb/episodes?id=${encodeURIComponent(detected.id)}`)
        .then(r => r.ok ? r.json() : null)
        .catch(() => null);
      const choice = await _showImdbPrompt(detected, metaPromise, episodesPromise);
      if (!choice) continue;  // user cancelled
      const urls = _imdbCandidateUrls(choice);
      imdbJobs.push({ choice, urls, titlePromise: metaPromise });
      continue;
    }
    finalUrls.push(_rewriteUrl(cand));
  }
  if (finalUrls.length === 0 && imdbJobs.length === 0) return;

  // Spawn cards for IMDB jobs first (they tend to be the user's
  // primary intent), each driving a multi-sniff under the hood.
  for (const job of imdbJobs) {
    // The card's nominal URL is the first candidate (111movies). It's
    // the referer/identity used in the UI; the sniff merge is what
    // actually surfaces variants from every source.
    const cardUrl = job.urls[0];
    const card = makeCard(cardUrl);
    // If this card came from a search-modal pick, the user has
    // already seen the poster — render it immediately on the card
    // (rather than waiting for sniff to find one in the streaming
    // page metadata, which often returns a generic player thumb).
    const pickedThumb = job.choice && job.choice.id
                        ? imdbThumbCache.get(job.choice.id) : null;
    if (pickedThumb) {
      const t = card.el.querySelector(".job-thumb");
      if (t) t.style.backgroundImage = `url("${pickedThumb}")`;
    }
    $("#pending").prepend(card.el);
    updateStatusBar();
    refreshRipAllButton();
    refreshPendingToolbar();
    // Auto-name from IMDB once that fetch lands. For TV episodes we
    // build "Series - S01E01 - Episode Title" so the saved file is
    // self-describing without the user having to type anything. Don't
    // block sniff — even if IMDB is unreachable the rip still works
    // (the user can type a name in).
    job.titlePromise.then(j => {
      if (!j || !j.title) return;
      let fn = j.title;
      const c = job.choice;
      if (c.kind === "tv" && c.season && c.episode) {
        const sNum = String(c.season).padStart(2, "0");
        const eNum = String(c.episode).padStart(2, "0");
        fn = `${j.title} - S${sNum}E${eNum}`;
        if (c.episodeName) fn += ` - ${c.episodeName}`;
      }
      const inp = card.el.querySelector(".job-title");
      if (inp && !inp.value.trim()) inp.value = fn;
    });
    card.startMultiSniff(job.urls);
  }

  for (const url of finalUrls) {
    const card = makeCard(url);
    // New cards land in Stage 2 (the staging workshop). They only
    // graduate to Stage 3 (Downloads) when the user clicks Rip It! —
    // see startBtn handler.
    $("#pending").prepend(card.el);
    updateStatusBar();
    refreshRipAllButton();
    refreshPendingToolbar();
    // Don't await — fire all probes in parallel so multi-URL paste lands
    // every card immediately rather than serialising.
    probeAndPopulate(card, url);
  }
}


function makeCard(url) {
  const el = document.createElement("div");
  el.className = "job";
  el.innerHTML = `
    <div class="job-head">
      <!-- Reorder arrows for pending cards. Hidden via CSS once a card
           leaves the pending list (#pending) — Stage 3 jobs render
           without these. -->
      <span class="card-move-stack" title="Reorder pending cards">
        <button class="btn-card-up"   title="Move up">▲</button>
        <button class="btn-card-down" title="Move down">▼</button>
      </span>
      <div class="job-thumb"></div>
      <div class="job-meta">
        <input class="job-title" type="text" placeholder="…" title="Click to rename — used as the prefix for any clips or stills ripped from this source.">
        <div class="job-sub">${esc(url)}</div>
        <!-- Per-source open-in-browser buttons. Populated by
             setSourceButtons(); one button per source URL the card
             knows about (1 for direct pastes, 2+ for IMDB multi-sniff). -->
        <div class="job-sources" style="display:none;"></div>
        <div class="ed-summary" style="display:none; margin-top:4px; font-size:11px; color:#000080;"></div>
        <div class="ed-strip"></div>
      </div>
      <!-- Cancel/Remove lives in the top-right corner of the card so the
           bottom action row can stay focused on positive actions (Rip,
           Edit, Reveal). Same .remove class so existing JS handlers
           (cancellation while running, removal when pending/done) keep
           working — only the styling/position changed. -->
      <button class="card-x remove danger" title="Remove / cancel" aria-label="Cancel">✕</button>
    </div>
    <div class="options">
      <select class="quality"></select>
      <select class="container"></select>
      <label class="subs-lbl small" title="Embed subtitles + chapters into the file when available">
        <input type="checkbox" class="subs" checked> subs
      </label>
      <!-- Edit comes before Rip It! in the DOM so when both are visible
           the user reads "Edit, then Rip It!" left-to-right (matches
           the natural workflow of trimming a clip before exporting). -->
      <button class="edit" style="display:none;">Edit</button>
      <button class="primary start">Rip It!</button>
      <!-- Contextual action buttons — visibility driven by CSS based on
           the card's dataset.state. Retry shows on errors, Reveal/Open
           after successful completion. -->
      <button class="btn-retry" title="Re-run this download">↻ Retry</button>
      <button class="btn-reveal" title="Reveal the saved file in Finder">Reveal</button>
    </div>
    <div class="progress-wrap"><div class="bar"></div></div>
    <div class="status"></div>
  `;
  const q = el.querySelector(".quality");
  const cont = el.querySelector(".container");
  const startBtn = el.querySelector(".start");
  const editBtn = el.querySelector(".edit");
  const removeBtn = el.querySelector(".remove");
  const status = el.querySelector(".status");
  const progWrap = el.querySelector(".progress-wrap");
  const bar = el.querySelector(".bar");
  const opts = el.querySelector(".options");
  const subsBox = el.querySelector(".subs");
  const thumb = el.querySelector(".job-thumb");
  const title = el.querySelector(".job-title");
  const sub = el.querySelector(".job-sub");
  const edSummary = el.querySelector(".ed-summary");
  const edStrip = el.querySelector(".ed-strip");
  let serverId = null, evt = null, savedFile = "", useGeneric = false;
  let sniffMode = false, sniffManifest = "", sniffTitle = "", sniffCookies = "";
  let sniffManifestContent = "";
  let sniffPlaylists = null; // {masterUrl, masterContent, variants:[{height,bandwidth,url}], contents:{url->text}}
  let cardSniffId = "";
  // Per-rip wall-clock anchor — set on the first download progress
  // event, used to compute ETA in the status text. The progress
  // BAR itself is purely segment-proportional (bar = downloaded /
  // total) so it never gets ahead of the actual data; only the ETA
  // string uses elapsed-time extrapolation, which the user can see
  // is approximate. Earlier versions tried to drive the bar from
  // a time estimator but that produced ugly edge cases (large
  // segment counts → bar sprinted to 99% before real progress data
  // arrived because the early "no data yet" estimate defaulted to
  // ~30s, then the monotonic clamp pinned it).
  let _ripStartedAt = 0;
  // HLS variant info (populated once per rip when the helper picks the
  // video + audio rendition; appended to status lines like "Downloading
  // · 47% · 1080p · AAC 5.1 (en)") and the most recent progress event
  // (so the variant handler can re-render the same status line as soon
  // as it arrives, without waiting for the next progress tick).
  let hlsVariantLabel = "";
  let _lastHlsProgress = null;
  function _ripReset() { _ripStartedAt = 0; hlsVariantLabel = ""; _lastHlsProgress = null; }
  // Multi-source sniff state. Single-source paths (legacy startSniff)
  // populate sniffSources with one entry; the IMDB flow uses
  // startMultiSniff which fills it with N entries (one per streaming
  // front-end). activateSource(idx) copies the chosen source's data
  // into the sniff* card vars above so the rip code stays unchanged.
  let sniffSources = [];
  let activeSourceIdx = -1;
  function activateSource(idx) {
    if (idx < 0 || idx >= sniffSources.length) return;
    const s = sniffSources[idx];
    if (!s || !s.data) return;
    activeSourceIdx        = idx;
    sniffTitle             = s.data.title || "";
    sniffCookies           = s.data.cookiesFile || "";
    sniffManifestContent   = s.data.manifestContent || "";
    sniffPlaylists         = s.data.playlists || null;
    sniffManifest          = s.data.manifestVariantUrl
                            || (sniffPlaylists && sniffPlaylists.masterUrl)
                            || "";
    // Critical: the rip path reuses the WKWebView associated with this
    // sniffId for segment fetches (referer + cookies inherit from
    // whichever WebView opened that source). Swap when source changes
    // or the rip will hit the wrong CDN context.
    cardSniffId            = s.sniffId;
  }
  // Editor session state — set after a successful /editor/start so we
  // can reuse the same sid on subsequent "Edit & Save" clicks (preserves
  // user's selections in the editor across re-opens).
  let cardSid = "";
  let editorItems = [];
  // Item mode — the card represents a single pre-extracted media asset
  // (one frame from a carousel, one image from an album, one video from
  // a multi-video page) rather than a URL that still needs probing.
  let itemMode = false;
  let itemData = null;

  const VIDEO_FORMATS = [
    ["mp4-web",  "MP4 — Web-safe (Readymag / Squarespace / HTML5 — recommended)"],
    ["mp4",      "MP4 — QuickTime friendly (HW-accel)"],
    ["mp4-h264", "MP4 — Force H.264 (HW-accel)"],
    ["mp4-h265", "MP4 — H.265/HEVC (smaller, Safari/iOS)"],
    ["mkv",      "MKV — Universal (any codec)"],
    ["webm",     "WebM"],
  ];
  const AUDIO_FORMATS = [
    ["mp3",  "MP3 — Universal"],
    ["m4a",  "M4A — AAC (Apple-friendly)"],
    ["opus", "Opus — High-efficiency (smaller)"],
    ["flac", "FLAC — Lossless (large)"],
    ["wav",  "WAV — Uncompressed (largest)"],
  ];
  const syncFormats = () => {
    const audio = q.value === "audio";
    const opts = audio ? AUDIO_FORMATS : VIDEO_FORMATS;
    const prev = cont.value;
    cont.innerHTML = "";
    for (const [v, l] of opts) {
      const o = document.createElement("option");
      o.value = v; o.textContent = l;
      cont.appendChild(o);
    }
    // Preserve selection across switches when valid; otherwise pick the first.
    if (opts.some(([v]) => v === prev)) cont.value = prev;
  };
  syncFormats();

  // Editor's QUALITY_OPTIONS now mirror the main page: "best" + every
  // height in [2160, 1440, 1080, 720, 480, 360]. Pass "best" and numeric
  // heights through verbatim; "audio"/"master"/"v:<url>" have no clean
  // editor mapping (those modes don't even open the editor) so we fall
  // back to "best" defensively.
  const qualityForEditor = () => {
    const v = String(q.value || "");
    if (v === "best" || /^\d+$/.test(v)) return v;
    return "best";
  };

  const renderSummary = () => {
    const c = editorItems.filter(x => x.kind === "clip").length;
    const s = editorItems.filter(x => x.kind === "still").length;
    const k = editorItems.filter(x => x.kind === "concat").length;
    if (c + s + k === 0) {
      edSummary.style.display = "none";
      edSummary.textContent = "";
      edStrip.classList.remove("show");
      edStrip.innerHTML = "";
      editBtn.textContent = "Edit";
      return;
    }
    const bits = [];
    if (c) bits.push(`${c} clip${c===1?"":"s"}`);
    if (s) bits.push(`${s} still${s===1?"":"s"}`);
    if (k) bits.push(`${k} concat${k===1?"":"s"}`);
    edSummary.textContent = `Selections: ${bits.join(", ")}`;
    edSummary.style.display = "";
    editBtn.textContent = "Edit";
    // Render thumbnail strip — one tile per clip/still/concat, in the
    // order the user added them. Each thumbnail is a JPEG dataURI
    // captured by the editor at save time. Click to re-open the editor.
    edStrip.innerHTML = "";
    editorItems.forEach(it => {
      const tile = document.createElement("div");
      tile.className = "ed-tile " + (it.kind || "");
      if (it.thumb) tile.style.backgroundImage = `url("${it.thumb}")`;
      const badge = document.createElement("div");
      badge.className = "ed-badge";
      if (it.kind === "clip") {
        const dur = Math.max(0, (+it.end || 0) - (+it.start || 0));
        badge.textContent = dur >= 10 ? `CLIP ${dur.toFixed(0)}s` : `CLIP ${dur.toFixed(1)}s`;
      } else if (it.kind === "concat") {
        const segs = (it.segments || []).length;
        const dur = (it.segments || []).reduce(
          (acc, x) => acc + Math.max(0, (+x.end || 0) - (+x.start || 0)), 0);
        badge.textContent = `CAT ${segs}× ${dur.toFixed(0)}s`;
      } else {
        badge.textContent = "STILL";
      }
      tile.appendChild(badge);
      const titleSuffix = (it.kind === "clip")
        ? ` · ${(+it.start||0).toFixed(2)}–${(+it.end||0).toFixed(2)}s`
        : (it.kind === "concat")
          ? ` · ${(it.segments||[]).length} segments`
          : ` · ${(+it.t||0).toFixed(2)}s`;
      tile.title = (it.name || it.kind || "") + titleSuffix;
      tile.addEventListener("click", () => editBtn.click());
      edStrip.appendChild(tile);
      // Fallback for items that arrived from the editor without a thumb
      // data URI baked in (e.g. user clicked Done before captureFromMain
      // resolved). Pull a fresh frame from the server's editor session.
      if (!it.thumb && cardSid) {
        const t = it.kind === "clip" ? (+it.start || 0) : (+it.t || 0);
        const url = `/editor/thumb?sid=${encodeURIComponent(cardSid)}&t=${t.toFixed(3)}&w=200`;
        fetch(url).then(async r => {
          if (!r.ok) return;
          const blob = await r.blob();
          if (!blob.size) return;
          const dataUri = await new Promise(res => {
            const fr = new FileReader();
            fr.onload = () => res(String(fr.result || ""));
            fr.onerror = () => res("");
            fr.readAsDataURL(blob);
          });
          if (dataUri) {
            it.thumb = dataUri;
            tile.style.backgroundImage = `url("${dataUri}")`;
          }
        }).catch(() => {});
      }
    });
    edStrip.classList.add("show");
  };

  // Edge-trigger: only fire the error animation when transitioning INTO
  // the err state (not on every status update while already errored).
  // Closure-scoped so each card tracks its own kind transitions.
  let _prevStatusKind = "";
  const card = {
    el,
    setStatus(html, kind) {
      status.innerHTML = html || "";
      status.className = "status" + (kind ? " " + kind : "");
      const k = kind || "";
      if (k === "err" && _prevStatusKind !== "err") {
        try { playErrorAnimation(); } catch (e) {}
      }
      _prevStatusKind = k;
    },
    // Render a row of small "↗ host" buttons for every source URL
    // associated with this card. Used by every populate path so
    // direct pastes show one button (the URL the user gave us), and
    // IMDB multi-sniff shows N (one per streaming front-end). Click
    // POSTs to /open-url which routes to the user's default browser
    // via the macOS `open` command.
    setSourceButtons(urls) {
      const box = el.querySelector(".job-sources");
      if (!box) return;
      box.innerHTML = "";
      const seen = new Set();
      const clean = (urls || []).filter(u => {
        if (!u || typeof u !== "string") return false;
        if (seen.has(u)) return false;
        seen.add(u);
        return true;
      });
      if (!clean.length) {
        box.style.display = "none";
        return;
      }
      for (const u of clean) {
        const host = (() => {
          try { return new URL(u).hostname.replace(/^www\./, ""); }
          catch (e) { return u; }
        })();
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "source-link-btn";
        // Per-source quality annotation. Populated by multi-sniff
        // after each source's master playlist comes back; lets the
        // user pick the best source visually rather than blindly.
        // Falls back to plain "↗ host" when we don't have a master
        // for this URL (legacy direct-paste path).
        const qLabel = sourceVariantLabels.get(u) || "";
        btn.textContent = qLabel ? `↗ ${host} · ${qLabel}` : `↗ ${host}`;
        btn.title = qLabel ? `${u}\n${qLabel}` : "Open " + u;
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          fetch("/open-url", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({url: u}),
          }).catch(() => {});
        });
        box.appendChild(btn);
      }
      box.style.display = "";
    },
    getEditorItems() { return editorItems; },
    getCardSid() { return cardSid; },
    getSourceTitle() { return title.value || url; },
    // Path to the file this card produced — used by the bulk
    // "Reveal all" action and per-card Reveal in Finder button.
    getSavedFile() { return savedFile || ""; },
    // Retry the start flow. Resets the card to "ready" state and
    // clicks Rip It! programmatically. Used by per-card Retry button
    // and the bulk "Retry all failed" action.
    retry() {
      _ripReset();
      el.dataset.state = "ready";
      bar.classList.remove("err", "done");
      bar.style.width = "0%";
      status.innerHTML = "";
      status.className = "status";
      startBtn.disabled = false;
      try { startBtn.click(); } catch (e) {}
    },
    setEditorItems(items) {
      editorItems = Array.isArray(items) ? items : [];
      renderSummary();
      refreshRipAllButton();
    },
    async ripAllItems() {
      if (!cardSid || !editorItems.length) return;
      el.dataset.state = "downloading";
      progWrap.classList.add("show");
      bar.style.width = "0%"; bar.className = "bar"; _ripReset();
      const total = editorItems.length;
      let done = 0;
      for (const it of editorItems) {
        const label = it.kind === "clip" ? "clip"
                    : it.kind === "still" ? "still"
                    : it.kind === "concat" ? "concat" : it.kind;
        card.setStatus(`<span class="spinner"></span> ${label} ${done+1}/${total}…`);
        try {
          if (it.kind === "concat") {
            // Stitch the snapshotted segments via /concat. Output is
            // always web-safe MP4. Filename = "<source> - <name>.mp4".
            const r = await fetch("/concat", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({
                sid: cardSid,
                clips: (it.segments || []).map(s => ({
                  start: s.start, end: s.end, crop: s.crop || null,
                })),
                dest, name: it.name || "",
                filename_hint: title.value || "",
              }),
            });
            const j = await r.json();
            if (!r.ok) throw new Error(j.error || "concat failed");
            await new Promise((resolve, reject) => {
              const ev = new EventSource(`/events/${j.id}`);
              ev.onmessage = (m) => {
                try {
                  const d = JSON.parse(m.data);
                  if (d.type === "status") {
                    card.setStatus(`<span class="spinner"></span> ${esc(d.status)} (concat ${done+1}/${total})`);
                  } else if (d.type === "progress" && d.percent != null) {
                    const overall = ((done + d.percent / 100) / total) * 100;
                    bar.style.width = overall + "%";
                    card.setStatus(`Stitching concat ${done+1}/${total} · ${d.percent.toFixed(0)}%`);
                  } else if (d.type === "done") {
                    ev.close(); resolve();
                  } else if (d.type === "error") {
                    ev.close(); reject(new Error(d.error || "concat failed"));
                  }
                } catch (e) {}
              };
              ev.onerror = () => {};
            });
          } else if (it.kind === "clip") {
            const r = await fetch("/clip", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({
                sid: cardSid, start: it.start, end: it.end,
                container: it.container || "mp4",
                quality: it.quality || "best",
                dest, name: it.name || "",
                // Live override: server combines source name + clip name as
                // "<source> - <clip>.ext". Pass the card's CURRENT title so
                // post-editor renames flow through.
                filename_hint: title.value || "",
                crop: it.crop || null,
              }),
            });
            const j = await r.json();
            if (!r.ok) throw new Error(j.error || "clip failed");
            // Drain the SSE until the encoder reports done/error.
            await new Promise((resolve, reject) => {
              const ev = new EventSource(`/events/${j.id}`);
              ev.onmessage = (m) => {
                try {
                  const d = JSON.parse(m.data);
                  if (d.type === "status") {
                    card.setStatus(`<span class="spinner"></span> ${esc(d.status)} (${label} ${done+1}/${total})`);
                  } else if (d.type === "progress" && d.percent != null) {
                    const overall = ((done + d.percent / 100) / total) * 100;
                    bar.style.width = overall + "%";
                    card.setStatus(`Encoding ${label} ${done+1}/${total} · ${d.percent.toFixed(0)}%`);
                  } else if (d.type === "done") {
                    ev.close(); resolve();
                  } else if (d.type === "error") {
                    ev.close(); reject(new Error(d.error || "encode failed"));
                  }
                } catch (e) {}
              };
              ev.onerror = () => {};
            });
          } else {
            const r = await fetch("/still", {
              method: "POST", headers: {"Content-Type": "application/json"},
              body: JSON.stringify({
                sid: cardSid, t: it.t,
                format: it.format || "jpeg",
                quality: it.quality || "best",
                dest, name: it.name || "",
                // See /clip note above.
                filename_hint: title.value || "",
                crop: it.crop || null,
              }),
            });
            const j = await r.json();
            if (!r.ok) throw new Error(j.error || "still failed");
            // /still is now async like /clip — wait on SSE for completion.
            await new Promise((resolve, reject) => {
              const ev = new EventSource(`/events/${j.id}`);
              ev.onmessage = (m) => {
                try {
                  const d = JSON.parse(m.data);
                  if (d.type === "status") {
                    card.setStatus(`<span class="spinner"></span> ${esc(d.status)} (${label} ${done+1}/${total})`);
                  } else if (d.type === "done") {
                    ev.close(); resolve();
                  } else if (d.type === "error") {
                    ev.close(); reject(new Error(d.error || "still failed"));
                  }
                } catch(e) {}
              };
              ev.onerror = () => {};
            });
          }
          done += 1;
          bar.style.width = (done / total * 100) + "%";
        } catch (e) {
          el.dataset.state = "error";
          bar.classList.add("err");
          card.setStatus(`Failed on ${label} ${done+1}/${total}: ${esc(String(e.message || e))}`, "err");
          return;
        }
      }
      el.dataset.state = "done";
      bar.classList.add("done");
      bar.style.width = "100%";
      card.setStatus(`✓ Ripped ${done} item${done===1?"":"s"}`, "done");
      updateStatusBar();
    },
    updateEditButton() {
      // Editor works for sniffed HLS (manifest already captured), yt-dlp
      // sources (we resolve a direct URL on demand), and itemMode video
      // cards (pre-extracted from a carousel/page).
      const haveSniff = sniffMode && (sniffManifestContent || (sniffPlaylists && sniffPlaylists.masterContent));
      const haveItem = itemMode && itemData && itemData.kind === "video";
      const havePopulated = !sniffMode && !itemMode && q.options.length > 0;
      const ok = canEditor() && (haveSniff || haveItem || havePopulated);
      editBtn.style.display = ok ? "" : "none";
    },
    showAuthRetry(errMsg, hint) {
      // Render an inline "auth wall" on this card with an embedded browser
      // picker + Retry button. The choice persists globally so subsequent
      // URLs auto-use it.
      el.dataset.state = "error";
      const heading = hint === "age"
        ? "Age-restricted." : "Looks like this needs a login.";
      const suggested = getCookiesBrowser() || "chrome";
      // Prefer browsers we've actually detected on this Mac so users
      // don't pick something they don't have. _installedBrowsers is
      // populated by /versions on demand; if not loaded yet we fall
      // back to the full list (no harm — yt-dlp's --cookies-from-browser
      // will return a clear error if the chosen browser isn't installed).
      const allBrowsers = [
        ["chrome","Chrome"],["firefox","Firefox"],["brave","Brave"],
        ["edge","Edge"],["safari","Safari"],["chromium","Chromium"],
        ["opera","Opera"],["vivaldi","Vivaldi"],
      ];
      const installed = _installedBrowsers || [];
      const filtered = installed.length
        ? allBrowsers.filter(([v]) => installed.includes(v) || v === suggested)
        : allBrowsers;
      const opts = filtered.map(([v,l]) => {
        const tag = (installed.length && !installed.includes(v)) ? "  (not detected)" : "";
        return `<option value="${v}" ${v===suggested?"selected":""}>${l}${tag}</option>`;
      }).join("");
      // Fire the detection in the background — populated on next render.
      if (typeof _loadInstalledBrowsers === "function") _loadInstalledBrowsers();
      card.setStatus(
        `<div style="margin-bottom:6px;"><strong>${heading}</strong></div>
         <div class="row" style="gap:6px; align-items:center;">
           <span class="small">Use cookies from:</span>
           <select class="auth-retry-browser">${opts}</select>
           <button class="primary auth-retry-go">Retry</button>
         </div>
         <pre style="margin:6px 0 0 0; white-space:pre-wrap; font-size:11px; color:inherit;">${esc(errMsg.split("\n").slice(-3).join("\n"))}</pre>`,
        "err");
      const sel = el.querySelector(".auth-retry-browser");
      const go = el.querySelector(".auth-retry-go");
      if (go) {
        go.addEventListener("click", async () => {
          const browser = sel ? sel.value : "chrome";
          setCookiesBrowser(browser);
          go.disabled = true;
          await card.retryProbe();
        });
      }
      updateStatusBar();
    },
    async retryProbe() {
      // Re-run the probe for the original URL using the current
      // getCookiesBrowser() value. probeAndPopulate handles populating
      // the card on success or showing the retry UI again on a second
      // failure.
      await probeAndPopulate(card, url);
    },
    populate(info) {
      if (info && info.kind === "gallery") {
        // Gallery probes are handed to the Stage 2 extract picker — the
        // user reviews items there and confirms which become cards. The
        // probing card we created up-front isn't useful any more.
        if (extractPicker) extractPicker.show(info, url);
        if (cardSid) cardsBySid.delete(cardSid);
        el.remove();
        refreshDownloadsEmpty();
        refreshPendingToolbar();
        updateStatusBar();
        refreshRipAllButton();
        return;
      }
      useGeneric = !!info.generic;
      sniffMode = false;
      if (info.thumbnail) thumb.style.backgroundImage = `url("${info.thumbnail}")`;
      title.value = info.title || url;
      const bits = [info.uploader, fmtDur(info.duration)].filter(Boolean);
      if (useGeneric) bits.push("generic extractor");
      sub.textContent = bits.join(" · ") || url;
      q.innerHTML = "";
      const add = (v, l) => { const o=document.createElement("option"); o.value=v; o.textContent=l; q.appendChild(o); };
      if (info.has_video || (!info.has_video && !info.has_audio_only)) add("best", "Best available");
      for (const h of (info.heights||[])) add(String(h), `${h}p`);
      if (info.has_audio_only) add("audio", "Audio only");
      syncFormats();
      q.addEventListener("change", syncFormats);
      // Multi-source sniff: each option carries data-source-idx telling
      // us which sniffSources[] entry it came from. Switch the active
      // source so the rip uses the right manifest / cookies / WebView
      // when the user picks a variant from a different host.
      q.addEventListener("change", () => {
        const opt = q.selectedOptions[0];
        if (!opt) return;
        const raw = opt.dataset.sourceIdx;
        if (raw == null) return;
        const sIdx = parseInt(raw, 10);
        if (Number.isFinite(sIdx) && sIdx !== activeSourceIdx) {
          activateSource(sIdx);
        }
      });
      opts.classList.add("show");
      card.setStatus("");
      card.setSourceButtons([url]);
      card.updateEditButton();
    },
    populateAsItem(item, sourceTitle, sourceUrl, idx, total) {
      // Configure this card as a single pre-extracted media asset.
      // Skips probing/sniffing entirely. Item carries: url, kind, ext,
      // thumbnail, referer, webpage_url, needs_ytdlp.
      itemMode = true;
      itemData = item;
      sniffMode = false;
      useGeneric = false;
      // Restore quality/container in case this card was reused
      q.style.display = ""; cont.style.display = "";
      if (item.thumbnail) thumb.style.backgroundImage = `url("${item.thumbnail}")`;
      const niceTitle = (sourceTitle || "").trim() ||
                        (item.title || item.filename || `Item ${idx+1}`);
      title.value = niceTitle;
      const subBits = [];
      const num = item.num || (idx + 1);
      subBits.push(`Item ${num}${total ? ` of ${total}` : ""}`);
      subBits.push(item.kind);
      if (item.width && item.height) subBits.push(`${item.width}×${item.height}`);
      if (item.duration) subBits.push(fmtDur(item.duration));
      if (sourceUrl) subBits.push(sourceUrl);
      sub.textContent = subBits.join(" · ");

      // Quality + container — only meaningful for video/audio items.
      q.innerHTML = "";
      cont.innerHTML = "";
      const add = (v, l) => { const o=document.createElement("option"); o.value=v; o.textContent=l; q.appendChild(o); };
      if (item.kind === "video") {
        add("best", "Best available");
        for (const h of [2160, 1440, 1080, 720, 480, 360]) add(String(h), `${h}p`);
        syncFormats();
        opts.classList.add("show");
        editBtn.style.display = "";
      } else if (item.kind === "audio") {
        add("audio", "Audio only");
        syncFormats();
        opts.classList.add("show");
        editBtn.style.display = "none";
      } else {
        // Image — repurpose the quality dropdown as a format converter so
        // the user can swap HEIC for something more universally viewable.
        add("original", "Keep original");
        add("jpeg",     "Convert to JPEG");
        add("png",      "Convert to PNG");
        add("webp",     "Convert to WebP");
        cont.innerHTML = "";
        cont.style.display = "none";
        opts.classList.add("show");
        editBtn.style.display = "none";
        // Subs checkbox doesn't apply to images.
        if (subsBox && subsBox.parentElement) subsBox.parentElement.style.display = "none";
      }
      startBtn.textContent = "Rip It!";
      el.dataset.state = "ready";
      card.setStatus("");

      // Carry over any editor selections the user made on the picker tile
      // before clicking Extract. The card's existing renderSummary handles
      // displaying the thumbnail strip; the editBtn handler will reuse the
      // sid for re-opens.
      if (item.cardSid) {
        cardSid = item.cardSid;
        cardsBySid.set(cardSid, card);
      }
      if (Array.isArray(item.editorItems) && item.editorItems.length) {
        card.setEditorItems(item.editorItems);
      }
    },
    startSniff() {
      cardSniffId = "s" + Math.random().toString(36).slice(2, 10);
      sniffMap.set(cardSniffId, {card: card, multi: false});
      // Single-source path: one source, one result. Stored at index 0
      // of sniffSources so the variant-change → activateSource path
      // can no-op without special-casing.
      sniffSources = [{
        idx: 0, url: url, sniffId: cardSniffId,
        ready: false, data: null,
      }];
      activeSourceIdx = 0;
      card.setStatus('<span class="spinner"></span> Sniffing in background…');
      window.webkit.messageHandlers.vdSniff.postMessage({id: cardSniffId, url});
    },
    // Multi-source sniff: hit N streaming front-ends for the same
    // underlying title (used by the IMDB flow). MUST run sequentially
    // — Swift's sniffer keeps a single in-progress collection state
    // (sniffID/sniffURL/sniffURLs/sniffPlaylists are class properties,
    // not per-id), so two `vdSniff` calls in quick succession would
    // clobber the first's capture buffer. We await each result before
    // firing the next; per-sniff is usually 5-15s thanks to the
    // adaptive grace timer landing early once a master playlist is
    // captured. Once all sources have landed _finalizeMultiSniff
    // merges every variant into a host-tagged quality dropdown.
    async startMultiSniff(urls) {
      if (!urls || !urls.length) return;
      sniffSources = urls.map((u, i) => ({
        idx: i, url: u, sniffId: "", ready: false, data: null,
      }));
      activeSourceIdx = -1;
      for (let i = 0; i < urls.length; i++) {
        const remaining = urls.length - i;
        const host = (() => { try { return new URL(urls[i]).hostname.replace(/^www\./, ""); }
                              catch(e) { return urls[i]; } })();
        card.setStatus(`<span class="spinner"></span> Searching ${host} (${i+1}/${urls.length})…`);
        const sid = "s" + Math.random().toString(36).slice(2, 10);
        sniffSources[i].sniffId = sid;
        // Promise that resolves with the sniff result data (or null
        // on per-source timeout). We hand the resolver to the sniff
        // map entry so __vdSniffResult can hand back the data.
        const data = await new Promise((resolve) => {
          let done = false;
          sniffMap.set(sid, {
            card,
            multi: true,
            srcIdx: i,
            onResult: (d) => { if (done) return; done = true; resolve(d); },
          });
          window.webkit.messageHandlers.vdSniff.postMessage({id: sid, url: urls[i]});
          // Belt-and-suspenders timeout — Swift's max-timer is 35s; we
          // give a few extra seconds of slack before bailing on the
          // source ourselves and moving to the next.
          setTimeout(() => {
            if (done) return;
            done = true;
            if (sniffMap.has(sid)) sniffMap.delete(sid);
            resolve(null);
          }, 40000);
        });
        sniffSources[i].data  = data;
        sniffSources[i].ready = true;
      }
      card._finalizeMultiSniff();
    },
    _finalizeMultiSniff() {
      // Filter to sources that actually returned a usable manifest.
      // A source is "usable" if we captured either a master playlist
      // or a single manifest body — anything else and the user can't
      // actually pick a variant from it.
      const ok = sniffSources.filter(s =>
        s.data && (s.data.manifestContent ||
                  (s.data.playlists && (s.data.playlists.masterContent || (s.data.playlists.variants || []).length))));
      if (ok.length === 0) {
        card.setStatus("No streams found on any source. Try the picker manually.", "err");
        return;
      }
      // Annotate each source's "↗ host" button with the top variant
      // it advertises (resolution / bandwidth / audio channel layout).
      // Lets the user eyeball which source has the best quality
      // before clicking. Done before activateSource so the buttons
      // render with labels on first paint.
      for (const src of ok) {
        const playlists = src.data.playlists || {};
        const masterText = playlists.masterContent || src.data.manifestContent || "";
        try {
          const lbl = parseTopVariantLabel(masterText);
          if (lbl) sourceVariantLabels.set(src.url, lbl);
        } catch (e) {}
      }

      // Activate the first usable source so card-state vars
      // (sniffPlaylists, cookies, sniffId, etc.) are populated. The
      // dropdown's `change` listener swaps them when the user picks
      // a variant from a different source.
      activateSource(ok[0].idx);

      // Merged dropdown: every variant from every source, host-tagged
      // so the user sees where each option comes from. Sorted by
      // height descending then host so the highest-quality option
      // sits at the top regardless of which source surfaced it.
      const entries = [];
      for (const src of ok) {
        const host = (() => { try { return new URL(src.url).hostname.replace(/^www\./, ""); }
                              catch (e) { return src.url; } })();
        const playlists = src.data.playlists || {};
        const variants  = playlists.variants || [];
        const contents  = playlists.contents || {};
        if (playlists.masterUrl && playlists.masterContent) {
          entries.push({
            value: "master",
            srcIdx: src.idx,
            host,
            // "Best available" entries sort above any variant from
            // the same source — synthetic "very-high" height for
            // ordering. Backend hls_fetcher.py picks the highest
            // variant from the master, so this label is accurate.
            sortHeight: 1e9,
            label: `Best available (${host})`,
          });
        }
        for (const v of variants) {
          const cached = contents[v.url] ? "" : " · live";
          const label = v.height
            ? `${v.height}p (${host})${cached}`
            : `${(v.bandwidth/1000)|0} kbps (${host})${cached}`;
          entries.push({
            value: "v:" + v.url,
            srcIdx: src.idx,
            host,
            sortHeight: v.height || 0,
            label,
          });
        }
      }
      entries.sort((a, b) => (b.sortHeight - a.sortHeight) || a.host.localeCompare(b.host));

      q.innerHTML = "";
      for (const e of entries) {
        const o = document.createElement("option");
        o.value = e.value;
        o.textContent = e.label;
        o.dataset.sourceIdx = String(e.srcIdx);
        q.appendChild(o);
      }
      sub.textContent = `Captured from ${ok.length} source${ok.length===1?"":"s"} · ${entries.length} option${entries.length===1?"":"s"}`;
      cont.style.display = "";
      opts.classList.add("show");
      card.setStatus("");
      // Show buttons for ALL candidate sources (not just the ones
      // that returned variants) so the user can investigate any
      // source that came back empty — sometimes the page loads in
      // a real browser even if our sniffer didn't catch a stream.
      card.setSourceButtons(sniffSources.map(s => s.url));
      sniffMode = true;
      card.updateEditButton();
    },
    onHlsStatus(d) {
      el.dataset.state = "downloading";
      // Encoding is the post-segment ffmpeg pass. We don't move the
      // bar during encode — the bar represents segment download,
      // which is already 100% by the time encoding starts. The text
      // status carries encode progress + ETA.
      if (d.phase === "encode") {
        const txt = d.status || "Encoding…";
        card.setStatus(`<span class="spinner"></span> ${esc(txt)}`);
      } else {
        card.setStatus(`<span class="spinner"></span> ${esc(d.status||"Browser-fetching…")}`);
      }
      updateStatusBar();
    },
    onHlsVariant(d) {
      // Stash the picks so the progress + done lines can annotate
      // "Downloading · 47%" with "1080p · AAC 5.1 (en)". Format once
      // here; cheaper than rebuilding the string per progress tick.
      const v = d.video || {};
      const a = d.audio || {};
      const parts = [];
      if (v.height) parts.push(`${v.height}p`);
      if (v.bandwidth) {
        const mbps = v.bandwidth / 1_000_000;
        parts.push(mbps >= 1 ? `${mbps.toFixed(1)} Mbps` : `${Math.round(v.bandwidth/1000)} kbps`);
      }
      if (a.separate) {
        let aTxt = "AAC";
        // CHANNELS in the manifest is "2"/"6"/"6/JOC". Map common
        // values to friendly labels.
        const ch = (a.channels || "").split("/")[0];
        if (ch === "6") aTxt += " 5.1";
        else if (ch === "8") aTxt += " 7.1";
        else if (ch === "2" || ch === "") aTxt += " stereo";
        else aTxt += ` ${ch}ch`;
        if (a.language) aTxt += ` (${a.language})`;
        parts.push(aTxt);
      } else if (a.separate === false) {
        parts.push("muxed audio");
      }
      hlsVariantLabel = parts.length ? parts.join(" · ") : "";
      // Re-render whatever is currently shown so the label appears
      // right away rather than waiting for the next progress tick.
      if (_lastHlsProgress) {
        const p = _lastHlsProgress;
        const elapsed = (Date.now() - (_ripStartedAt || Date.now())) / 1000;
        const eta = fmtETA(elapsed, (p.percent||0) / 100);
        card.setStatus(`Downloading · ${(p.percent||0).toFixed(0)}% (${p.idx}/${p.total} segments)${eta ? ` · ETA ${eta}` : ""}${hlsVariantLabel ? ` · ${esc(hlsVariantLabel)}` : ""}`);
      }
    },
    onHlsProgress(d) {
      el.dataset.state = "downloading";
      if (!_ripStartedAt) _ripStartedAt = Date.now();
      _lastHlsProgress = d;
      const pct = (d.percent||0);
      // Simple proportional bar — segment count is authoritative,
      // no estimator games. Bar tracks downloaded / total exactly.
      bar.style.width = Math.max(0, Math.min(100, pct)).toFixed(1) + "%";
      // ETA computed from elapsed + progress (text only, so an
      // imperfect estimate just shows up as a slightly-off number).
      const elapsed = (Date.now() - _ripStartedAt) / 1000;
      const eta = fmtETA(elapsed, pct / 100);
      card.setStatus(`Downloading · ${pct.toFixed(0)}% (${d.idx}/${d.total} segments)${eta ? ` · ETA ${eta}` : ""}${hlsVariantLabel ? ` · ${esc(hlsVariantLabel)}` : ""}`);
    },
    onHlsDone(d) {
      _ripReset();
      el.dataset.state = "done";
      bar.style.width = "100%"; bar.classList.add("done");
      savedFile = d.filename || "";
      const name = savedFile ? savedFile.split("/").pop() : "saved";
      card.setStatus(`✓ ${esc(name)}`, "done");
      if (savedFile) {
        const b = document.createElement("button");
        b.textContent = "Show in Finder";
        b.onclick = () => fetch("/reveal", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({path: savedFile})});
        status.appendChild(b);
      }
      // The Swift HLS finalizer writes the file directly; no Python
      // job ran, so history_record never fired. POST /history/add to
      // keep the History panel in sync. Title falls back to the URL
      // when the user hasn't renamed the card. Height comes from the
      // quality dropdown if it's a numeric pick (e.g. "1080").
      if (savedFile) {
        const heightVal = parseInt(q.value, 10);
        const payload = {
          title: (title.value || "").trim() || url,
          url: url,
          file_path: savedFile,
          container: cont.value || "",
          height: Number.isFinite(heightVal) ? heightVal : null,
          audio_only: false,
        };
        fetch("/history/add", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        }).then(() => { try { loadHistory(); } catch (e) {} })
          .catch(() => {});
      }
      startBtn.disabled = false; startBtn.textContent = "Rip Again!";
      updateStatusBar();
    },
    onHlsError(d) {
      _ripReset();
      el.dataset.state = "error";
      bar.classList.add("err");
      card.setStatus("Failed:<br><pre style='margin:4px 0;white-space:pre-wrap;font-size:11px;color:inherit'>" + esc(d.error||"") + "</pre>", "err");
      startBtn.disabled = false;
      updateStatusBar();
    },
    async onSniffResult(data) {
      const candidates = (data && data.candidates) || [];
      sniffTitle = (data && data.title) || "";
      sniffCookies = (data && data.cookiesFile) || "";
      sniffManifestContent = (data && data.manifestContent) || "";
      sniffPlaylists = (data && data.playlists) || null;
      const manifestVariantUrl = (data && data.manifestVariantUrl) || "";
      const filtered = candidates.filter(u => !/\.(ts|m4s)(\?|#|$)/i.test(u));
      filtered.sort((a, b) => {
        const score = u => /\.m3u8(\?|#|$)/i.test(u) ? 0 : /\.mpd(\?|#|$)/i.test(u) ? 1 : /\.mp4(\?|#|$)/i.test(u) ? 2 : 3;
        return score(a) - score(b);
      });
      if (!filtered.length && !sniffManifestContent) {
        // Sniff found nothing. If we got here from a low-confidence probe
        // response (yt-dlp / scraper saw only a thumbnail), fall back to
        // showing what they did find — better some result than none.
        if (card._lowConfidenceFallback) {
          const { info: fallbackInfo, url: fallbackUrl } = card._lowConfidenceFallback;
          card._lowConfidenceFallback = null;
          if (extractPicker) extractPicker.show(fallbackInfo, fallbackUrl);
          if (cardSid) cardsBySid.delete(cardSid);
          el.remove();
          refreshDownloadsEmpty();
          refreshPendingToolbar();
          updateStatusBar();
          refreshRipAllButton();
          return;
        }
        card.setStatus("No media URLs detected. The page may not have started a player.", "err");
        return;
      }
      // Sniff succeeded — drop any pending fallback so we don't re-route.
      card._lowConfidenceFallback = null;
      // Preferred path: WebView already fetched the manifest with full
      // session. We can show the master/variant index like fetchv does.
      if (sniffManifestContent || sniffPlaylists) {
        sniffMode = true;
        sniffManifest = manifestVariantUrl || (sniffPlaylists && sniffPlaylists.masterUrl) || "";
        title.value = sniffTitle || url;
        const v = (sniffPlaylists && sniffPlaylists.variants) || [];
        const cs = (sniffPlaylists && sniffPlaylists.contents) || {};
        const haveCount = Object.keys(cs).length;
        sub.textContent = `Captured from page · ${v.length} variant${v.length===1?"":"s"} · ${haveCount} playlist${haveCount===1?"":"s"} cached`;
        q.innerHTML = "";
        const add = (val, label) => { const o=document.createElement("option"); o.value=val; o.textContent=label; q.appendChild(o); };
        // "Master" option = full master playlist — yt-dlp picks variant per -f
        if (sniffPlaylists && sniffPlaylists.masterUrl && sniffPlaylists.masterContent) {
          add("master", "Best available");
        } else {
          add("best", "Best (auto)");
        }
        // Each variant from master, with cached/CDN indicator
        for (const variant of v) {
          const cached = cs[variant.url] ? "" : " · live fetch";
          const label = variant.height ? `${variant.height}p` : `${(variant.bandwidth/1000)|0} kbps`;
          add("v:" + variant.url, label + cached);
        }
        cont.style.display = "";
        opts.classList.add("show");
        card.setStatus("");
        card.setSourceButtons([url]);
        card.updateEditButton();
        return;
      }
      const manifest = filtered.find(u => /\.(m3u8|mpd)(\?|#|$)/i.test(u));
      if (manifest) {
        card.setStatus('<span class="spinner"></span> Stream captured · checking variants…');
        try {
          const r = await fetch("/probe", {
            method: "POST", headers: {"Content-Type":"application/json"},
            body: JSON.stringify({url: manifest, referer: url, cookies_file: sniffCookies, cookies_browser: getCookiesBrowser()}),
          });
          const j = await r.json();
          if (r.ok && (j.heights || []).length) {
            sniffMode = true;
            sniffManifest = manifest;
            const displayTitle = sniffTitle || j.title || labelFor(manifest);
            title.value = displayTitle;
            const bits = [`${j.heights[0]}p max`];
            if (j.has_audio_only) bits.push("audio available");
            bits.push("via sniff");
            sub.textContent = bits.join(" · ");
            q.innerHTML = "";
            const add = (v, l) => { const o=document.createElement("option"); o.value=v; o.textContent=l; q.appendChild(o); };
            add("best", `Best (${j.heights[0]}p)`);
            for (const h of j.heights) add(String(h), `${h}p`);
            if (j.has_audio_only) add("audio", "Audio only (mp3)");
            const syncCont = () => { cont.style.display = q.value === "audio" ? "none" : ""; };
            q.addEventListener("change", syncCont); syncCont();
            opts.classList.add("show");
            card.setStatus("");
            card.setSourceButtons([url]);
            return;
          }
        } catch(e) { /* fall through to candidate list */ }
      }
      // Fallback: present raw candidates
      sniffMode = true;
      sniffManifest = "";
      title.value = sniffTitle || url;
      sub.textContent = `${filtered.length} stream${filtered.length>1?'s':''} found · sniffed from page`;
      q.innerHTML = "";
      for (const u of filtered) {
        const o = document.createElement("option");
        o.value = u; o.textContent = labelFor(u);
        q.appendChild(o);
      }
      cont.style.display = "";
      opts.classList.add("show");
      card.setStatus(`Couldn't read variants — pick a stream manually.`);
      card.setSourceButtons([url]);
    },
  };

  startBtn.addEventListener("click", async () => {
    playRipAnimation();
    // Promote a Stage 2 card to Stage 3 the moment the user commits to
    // ripping it. After this point the card lives in #jobs and "Rip
    // Again!" re-runs in place; it doesn't bounce back to Stage 2.
    if (el.parentNode && el.parentNode.id === "pending") {
      document.getElementById("jobs").prepend(el);
      refreshDownloadsEmpty();
      refreshPendingToolbar();
    }
    // If the user has saved editor selections on this card, "Rip It!"
    // means rip those clips/stills (not the whole video) — the editor
    // session is still alive on the backend so we can post /clip + /still.
    if (editorItems.length && cardSid) {
      startBtn.disabled = true;
      try { await card.ripAllItems(); }
      finally { startBtn.disabled = false; startBtn.textContent = "Rip Again!"; }
      return;
    }
    // Item card (single pre-extracted asset from a carousel/album/page).
    // Rip via /gallery_download — same path as the old multi-rip flow but
    // for one item at a time, and each item has its own card/progress.
    if (itemMode && itemData) {
      startBtn.disabled = true;
      el.dataset.state = "downloading";
      progWrap.classList.add("show");
      bar.style.width = "0%"; bar.className = "bar"; _ripReset();
      updateStatusBar();
      try {
        const heightSel = q.value;
        const audioOnly = heightSel === "audio";
        const height = (heightSel === "audio" || heightSel === "best" || !heightSel) ? null : heightSel;
        const isImage = itemData.kind === "image";
        const r = await fetch("/gallery_download", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            item: itemData, dest,
            height: isImage ? null : height,
            audio_only: audioOnly,
            container: cont.value || "mp4",
            cookies_browser: getCookiesBrowser(),
            embed_subs: !!subsBox.checked,
            image_format: isImage ? (q.value || "original") : "original",
          }),
        });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || "gallery_download failed");
        serverId = j.id;
        await new Promise((resolve, reject) => {
          evt = new EventSource(`/events/${j.id}`);
          evt.onmessage = (m) => {
            try {
              const d = JSON.parse(m.data);
              if (d.type === "progress" && d.percent != null) {
                bar.style.width = Math.min(100, d.percent) + "%";
                card.setStatus(`<span class="spinner"></span> ${d.percent.toFixed(1)}%`);
              } else if (d.type === "done") {
                evt.close(); evt = null;
                el.dataset.state = "done";
                bar.classList.add("done"); bar.style.width = "100%";
                savedFile = d.filename || "";
                const name = savedFile ? savedFile.split("/").pop() : "saved";
                card.setStatus(`✓ ${esc(name)}`, "done");
                if (savedFile) {
                  const b = document.createElement("button");
                  b.textContent = "Show in Finder";
                  b.onclick = () => fetch("/reveal", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({path: savedFile})});
                  status.appendChild(b);
                }
                resolve();
              } else if (d.type === "error") {
                evt.close(); evt = null;
                el.dataset.state = "error";
                bar.classList.add("err");
                card.setStatus(`Failed: ${esc(d.error||"")}`, "err");
                reject(new Error(d.error || "download failed"));
              }
            } catch(e) {}
          };
          evt.onerror = () => {};
        });
      } catch (e) {
        el.dataset.state = "error";
        bar.classList.add("err");
        card.setStatus(`Failed: ${esc(String(e.message || e))}`, "err");
      } finally {
        startBtn.disabled = false;
        startBtn.textContent = "Rip Again!";
        updateStatusBar();
      }
      return;
    }
    let downloadUrl = url, referer = "", height = "best", audio_only = false, filenameHint = "";
    let manifestContent = "";
    if (sniffMode) {
      const choice = q.value;
      const cs = (sniffPlaylists && sniffPlaylists.contents) || {};
      if (choice === "master" && sniffPlaylists && sniffPlaylists.masterUrl && sniffPlaylists.masterContent) {
        // Variants are sorted highest-first. If we already have the
        // top variant's playlist cached from sniffing, use it directly
        // — saves a CDN refetch the helper sometimes 403s on.
        const vlist = sniffPlaylists.variants || [];
        const cached = vlist.find(x => cs[x.url]);
        if (cached) {
          downloadUrl = cached.url;
          manifestContent = cs[cached.url];
        } else {
          downloadUrl = sniffPlaylists.masterUrl;
          manifestContent = sniffPlaylists.masterContent;
        }
      } else if (choice && choice.indexOf("v:") === 0) {
        const vUrl = choice.slice(2);
        downloadUrl = vUrl;
        if (cs[vUrl]) manifestContent = cs[vUrl];
      } else if (choice === "best" || choice === "audio") {
        downloadUrl = sniffManifest || downloadUrl;
        if (sniffManifestContent) manifestContent = sniffManifestContent;
        audio_only = choice === "audio";
      } else {
        downloadUrl = choice || downloadUrl;
      }
      referer = url;
      filenameHint = sniffTitle || "";
    } else {
      const quality = q.value;
      audio_only = quality === "audio";
      height = audio_only ? null : quality;
    }
    // Whatever the source path, prefer the user-edited card title — they
    // may have renamed it. Falls back to whatever filenameHint already had.
    const editedTitle = (title.value || "").trim();
    if (editedTitle) filenameHint = editedTitle;
    const container = cont.value;
    startBtn.disabled = true;
    el.dataset.state = "downloading";
    progWrap.classList.add("show");
    bar.style.width = "0%"; bar.className = "bar";
    card.setStatus('<span class="spinner"></span> Starting…');
    updateStatusBar();

    // Best path: if the WebView is alive, browser-fetch every segment from
    // the WebView itself — same network stack the player used. Bypasses
    // yt-dlp/curl_cffi and the CDN anti-bot challenges that come with
    // them (Cloudflare cf_clearance, Akamai sensor data, etc.). The JS in
    // HLS_DOWNLOADER_JS will fetch the variant playlist itself when we
    // don't have its body cached, so we route here even for "live fetch"
    // variants the user picked from the master.
    const hlsLooksOk = sniffMode && (manifestContent ||
                          /\.(m3u8|mpd)(\?|#|$)/i.test(downloadUrl || ""));
    if (hlsLooksOk && canHlsDownload()) {
      const taskId = "h" + Math.random().toString(36).slice(2, 10);
      hlsTasks.set(taskId, card);
      window.webkit.messageHandlers.vdHlsStart.postMessage({
        taskId,
        sniffId: cardSniffId,
        manifestContent: manifestContent || "",
        manifestUrl: downloadUrl,
        dest,
        filename: (filenameHint && filenameHint.length) ? filenameHint : "video",
        format: container,
      });
      return;
    }

    const body = {url: downloadUrl, referer, dest, height, audio_only, container, generic: useGeneric, filename_hint: filenameHint, cookies_file: sniffMode ? sniffCookies : "", cookies_browser: getCookiesBrowser(), embed_subs: !!subsBox.checked};
    if (manifestContent) {
      body.manifest_content = manifestContent;
      // When using the master playlist, also ship every variant playlist we
      // have cached so yt-dlp can resolve them locally — no CDN manifest hits.
      if (sniffMode && sniffPlaylists && sniffPlaylists.contents) {
        const vc = {};
        for (const u in sniffPlaylists.contents) {
          if (u !== sniffPlaylists.masterUrl && u !== downloadUrl) {
            vc[u] = sniffPlaylists.contents[u];
          }
        }
        if (Object.keys(vc).length) body.variant_contents = vc;
      }
    }
    const r = await fetch("/download", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) {
      el.dataset.state = "error";
      bar.classList.add("err");
      card.setStatus("Failed: " + (j.error||""), "err");
      startBtn.disabled = false;
      updateStatusBar();
      return;
    }
    serverId = j.id;
    evt = new EventSource("/events/" + serverId);
    // Recognize yt-dlp postprocessor markers and rewrite to a friendly
    // label. Without this we show the raw log line, which is usually
    // the [Merger]/[VideoConvertor] header followed by a long file
    // path that got slice(-110)'d into something like "…ng formats
    // into '/Users/...'" — front of the message lost. Mapping known
    // markers to clean status text fixes that.
    function _ytdlpPrettyLine(raw) {
      if (!raw) return "";
      const s = String(raw).trim();
      // [download] lines carry size + speed + ETA — strip just the
      // marker prefix and keep the data. Looks like:
      //   "[download]  7.1% of  238.41MiB at  3.45MiB/s ETA 03:42"
      // → "7.1% of 238.41MiB at 3.45MiB/s ETA 03:42"
      if (/^\[download\]/i.test(s)) {
        return s.replace(/^\[download\]\s*/i, "").replace(/\s+/g, " ").trim();
      }
      // Postprocessor markers — replace with a clean friendly label
      // since the rest of the line is usually the input/output path,
      // which is just noise.
      const map = [
        [/^\[Merger\]/i,           "Merging audio + video streams…"],
        [/^\[VideoConvertor\]/i,   "Re-encoding video…"],
        [/^\[VideoRemuxer\]/i,     "Remuxing container…"],
        [/^\[ExtractAudio\]/i,     "Extracting audio…"],
        [/^\[Fixup\w+\]/i,         "Fixing up file…"],
        [/^\[EmbedSubtitle\]/i,    "Embedding subtitles…"],
        [/^\[Metadata\]/i,         "Writing metadata…"],
        [/^\[ThumbnailsConvertor\]/i, "Processing thumbnail…"],
      ];
      for (const [re, label] of map) {
        if (re.test(s)) return label;
      }
      // Fallback: keep the FRONT of the line (which usually identifies
      // the action) instead of slicing from the end and losing it.
      return s.length > 100 ? s.slice(0, 100) + "…" : s;
    }
    // Postprocessor phases (Merger / VideoConvertor / our own
    // _ensure_*_in_place) emit a single label line then go silent
    // while ffmpeg crunches. Tick an elapsed-time counter every
    // second so the user can see the rip is still alive — a frozen
    // "Merging…" string for 5 minutes feels broken even when it
    // isn't. _ppLabel is the most recent activity label; _ppStart
    // anchors the counter; the interval is cleared on the next
    // progress / activity / done / error event.
    let _ppLabel = "";
    let _ppStart = 0;
    let _ppTimer = null;
    const _ppStop = () => {
      if (_ppTimer) { clearInterval(_ppTimer); _ppTimer = null; }
      _ppLabel = ""; _ppStart = 0;
    };
    const _fmtElapsed = (ms) => {
      const s = Math.max(0, Math.floor(ms / 1000));
      if (s < 60) return s + "s";
      return Math.floor(s/60) + ":" + String(s%60).padStart(2, "0");
    };
    evt.onmessage = ev => {
      const d = JSON.parse(ev.data);
      if (d.type === "progress") {
        _ppStop();
        bar.style.width = d.percent + "%";
        const tail = d.line ? esc(_ytdlpPrettyLine(d.line)) : "";
        card.setStatus(`Downloading · ${d.percent.toFixed(1)}%<br><span style="color:#636366;font-size:11px">${tail}</span>`);
      } else if (d.type === "activity") {
        const pretty = esc(_ytdlpPrettyLine(d.line));
        // Fresh post-process label → restart the elapsed-time tick.
        _ppLabel = pretty;
        _ppStart = Date.now();
        if (!_ppTimer) {
          _ppTimer = setInterval(() => {
            const elapsed = Date.now() - _ppStart;
            card.setStatus(`<span class="spinner"></span> ${_ppLabel} · ${_fmtElapsed(elapsed)}`);
          }, 1000);
        }
        card.setStatus(`<span class="spinner"></span> ${pretty}`);
      } else if (d.type === "done") {
        _ppStop();
        el.dataset.state = "done";
        bar.style.width = "100%"; bar.classList.add("done");
        savedFile = d.filename || "";
        const name = savedFile ? savedFile.split("/").pop() : "saved";
        card.setStatus(`✓ ${esc(name)}`, "done");
        if (savedFile) {
          const b = document.createElement("button");
          b.textContent = "Show in Finder";
          b.onclick = () => fetch("/reveal", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({path: savedFile})});
          status.appendChild(b);
        }
        startBtn.disabled = false; startBtn.textContent = "Rip Again!";
        evt.close();
        updateStatusBar();
      } else if (d.type === "error") {
        _ppStop();
        el.dataset.state = "error";
        bar.classList.add("err");
        card.setStatus("Failed:<br><pre style='margin:4px 0;white-space:pre-wrap;font-size:11px;color:inherit'>" + esc(d.error||"") + "</pre>", "err");
        startBtn.disabled = false; evt.close();
        updateStatusBar();
      } else if (d.type === "status") {
        // Backend can push a free-form status (e.g. "Normalizing
        // video codec…" from our post-pass). Treat it like an
        // activity event — restart the elapsed-time tick.
        _ppLabel = esc(d.status||"");
        _ppStart = Date.now();
        if (!_ppTimer) {
          _ppTimer = setInterval(() => {
            const elapsed = Date.now() - _ppStart;
            card.setStatus(`<span class="spinner"></span> ${_ppLabel} · ${_fmtElapsed(elapsed)}`);
          }, 1000);
        }
        card.setStatus(`<span class="spinner"></span> ${_ppLabel}`);
      }
    };
    evt.onerror = () => { /* ignore — we handle terminal events explicitly */ };
  });

  removeBtn.addEventListener("click", () => {
    if (serverId) fetch("/cancel/" + serverId, {method:"POST"});
    if (evt) evt.close();
    if (cardSid) cardsBySid.delete(cardSid);
    el.remove();
    refreshDownloadsEmpty();
    refreshPendingToolbar();
    refreshDownloadsToolbar();
    updateStatusBar();
    refreshRipAllButton();
  });

  // ↑ / ↓ reorder buttons (only meaningful while card is in #pending —
  // CSS hides the stack once the card moves to Stage 3).
  const moveUpBtn = el.querySelector(".btn-card-up");
  const moveDownBtn = el.querySelector(".btn-card-down");
  if (moveUpBtn) {
    moveUpBtn.addEventListener("click", () => moveCardByOne(el, -1));
  }
  if (moveDownBtn) {
    moveDownBtn.addEventListener("click", () => moveCardByOne(el, +1));
  }
  // Retry — visible only when state="error". Re-runs the rip via the
  // card's `retry()` method which resets state and re-clicks Rip It!.
  const retryBtn = el.querySelector(".btn-retry");
  if (retryBtn) {
    retryBtn.addEventListener("click", () => card.retry());
  }
  // Reveal in Finder — visible only when state="done".
  const revealBtn = el.querySelector(".btn-reveal");
  if (revealBtn) {
    revealBtn.addEventListener("click", async () => {
      const p = card.getSavedFile();
      if (!p) return;
      try {
        await fetch("/reveal", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({path: p}),
        });
      } catch (e) {}
    });
  }

  editBtn.addEventListener("click", async () => {
    if (!canEditor()) return;
    const titleText = title.value || "";

    // Reopen path: we already started a session — just re-launch the
    // editor with the saved sid. Server-side state (cached source +
    // items list) is preserved, so the user sees their prior selections.
    if (cardSid) {
      window.webkit.messageHandlers.vdEditorOpen.postMessage({
        sid: cardSid, title: titleText,
      });
      return;
    }

    editBtn.disabled = true;
    card.setStatus('<span class="spinner"></span> Preparing editor…');

    if (itemMode && itemData && itemData.kind === "video") {
      // Pre-extracted carousel/page video. Two flavours:
      //  - direct CDN URL (most common after the merged probe) → kind=mp4
      //  - needs_ytdlp flag (gallery-dl placeholder yt-dlp must resolve) → kind=ytdlp
      const useYtdlp = !!itemData.needs_ytdlp || (itemData.url || "").startsWith("ytdl:");
      const body = useYtdlp ? {
        kind: "ytdlp",
        url: itemData.webpage_url || itemData.referer || url,
        // The page URL the user actually pasted on the main page —
        // critical for History's "re-rip" so the user gets sent back
        // to the carousel picker, not to the resolved direct CDN URL
        // (which expires).
        page_url: url,
        referer: itemData.referer || "",
        title: titleText, filename_hint: titleText,
        default_quality: qualityForEditor(),
        cookies_browser: getCookiesBrowser(),
        // For carousel-with-needs_ytdlp items, item.num is the 1-indexed
        // slide position. Pass it as --playlist-items so yt-dlp picks the
        // right slide rather than enumerating the whole carousel.
        playlist_items: (itemData.num != null) ? String(itemData.num) : "",
      } : {
        kind: "mp4",
        src_url: itemData.url,
        page_url: itemData.referer || url,
        title: titleText, filename_hint: titleText,
        default_quality: qualityForEditor(),
        cookies_browser: getCookiesBrowser(),
      };
      try {
        const r = await fetch("/editor/start", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body),
        });
        const j = await r.json();
        editBtn.disabled = false;
        if (!r.ok) {
          card.setStatus("Editor failed: " + (j.error || ""), "err");
          return;
        }
        card.setStatus("");
        cardSid = j.sid;
        cardsBySid.set(cardSid, card);
        window.webkit.messageHandlers.vdEditorOpen.postMessage({
          sid: j.sid, title: titleText,
        });
      } catch (e) {
        editBtn.disabled = false;
        card.setStatus("Editor failed: " + e, "err");
      }
      return;
    }

    if (sniffMode) {
      // HLS sniffed → cookies live in the sniff session's WebView. Route
      // through Swift so it can collect them before POST /editor/start.
      const choice = q.value;
      const cs = (sniffPlaylists && sniffPlaylists.contents) || {};
      let manifestText = "", manifestUrl = "";
      if (choice === "master" && sniffPlaylists && sniffPlaylists.masterUrl && sniffPlaylists.masterContent) {
        const vlist = sniffPlaylists.variants || [];
        const cached = vlist.find(x => cs[x.url]);
        if (cached) { manifestUrl = cached.url; manifestText = cs[cached.url]; }
        else { manifestUrl = sniffPlaylists.masterUrl; manifestText = sniffPlaylists.masterContent; }
      } else if (choice && choice.indexOf("v:") === 0) {
        manifestUrl = choice.slice(2);
        manifestText = cs[manifestUrl] || "";
      } else {
        manifestUrl = sniffManifest || "";
        manifestText = sniffManifestContent || "";
      }
      if (!manifestText || !manifestUrl) {
        editBtn.disabled = false;
        card.setStatus("Editor needs an HLS manifest — pick a variant first.", "err");
        return;
      }
      const token = "e" + Math.random().toString(36).slice(2, 10);
      await new Promise((resolve) => {
        editorPrepareWaiters.set(token, (ok, error, sid) => {
          editBtn.disabled = false;
          if (!ok) card.setStatus("Editor failed: " + (error || ""), "err");
          else {
            card.setStatus("");
            if (sid) { cardSid = sid; cardsBySid.set(sid, card); }
          }
          resolve();
        });
        window.webkit.messageHandlers.vdEditorPrepare.postMessage({
          kind: "hls",
          sniffId: cardSniffId,
          manifestText, manifestUrl,
          pageUrl: url,
          title: sniffTitle || "",
          filenameHint: sniffTitle || labelFor(manifestUrl),
          defaultQuality: qualityForEditor(),
          replyToken: token,
        });
      });
    } else {
      // yt-dlp source → resolve direct URL server-side. No cookies needed
      // (yt-dlp handles auth internally).
      const quality = q.value;
      const audioOnly = quality === "audio";
      const height = audioOnly ? null : quality;
      try {
        const r = await fetch("/editor/start", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            kind: "ytdlp", url, height, audio_only: audioOnly,
            // Mirror url into page_url so the editor session knows what
            // the user originally pasted. History's re-rip uses this so
            // the user lands back on the input page (YouTube, Twitter,
            // etc.) rather than the resolved-and-expiring CDN URL that
            // _resolve_via_ytdlp produces internally.
            page_url: url,
            generic: useGeneric, referer: "",
            title: titleText, filename_hint: titleText,
            default_quality: qualityForEditor(),
            cookies_browser: getCookiesBrowser(),
          }),
        });
        const j = await r.json();
        editBtn.disabled = false;
        if (!r.ok) {
          card.setStatus("Editor failed: " + (j.error || ""), "err");
          return;
        }
        card.setStatus("");
        cardSid = j.sid;
        cardsBySid.set(cardSid, card);
        window.webkit.messageHandlers.vdEditorOpen.postMessage({
          sid: j.sid, title: titleText,
        });
      } catch (e) {
        editBtn.disabled = false;
        card.setStatus("Editor failed: " + e, "err");
      }
    }
  });

  el._card = card;
  return card;
}

// === Editor → main bridge ===
// Swift calls this with the payload posted by the editor's "Done" button.
// Items are NOT promoted into separate jobs — they stay attached to the
// source card as a saved selection list. The user runs them later with
// "Rip All Selections" (global) or by reopening the editor.
window.__vdEditorReceiveItems = function(payload) {
  if (!payload || !Array.isArray(payload.items)) return;
  const sid = String(payload.sid || "");
  const card = cardsBySid.get(sid);
  if (!card) return;
  card.setEditorItems(payload.items);
};

function refreshRipAllButton() {
  // Operates on the Stage 2 #pending list — that's where un-ripped cards
  // wait. Sums up clips / stills / full-asset counts across cards so the
  // user can see what "Rip All Pending" will trigger.
  const btn = document.getElementById("btn-rip-all-pending");
  const sum = document.getElementById("rip-all-summary");
  if (!btn || !sum) return;
  let total = 0, clips = 0, stills = 0, whole = 0;
  for (const child of $("#pending").children) {
    const c = child._card;
    if (!c) continue;
    total += 1;
    const items = (c.getEditorItems && c.getEditorItems()) || [];
    if (items.length) {
      for (const it of items) {
        if (it.kind === "clip") clips += 1;
        else if (it.kind === "still") stills += 1;
      }
    } else {
      whole += 1;
    }
  }
  if (total === 0) {
    sum.textContent = "";
    refreshPendingToolbar();
    return;
  }
  const bits = [];
  if (clips) bits.push(`${clips} clip${clips===1?"":"s"}`);
  if (stills) bits.push(`${stills} still${stills===1?"":"s"}`);
  if (whole) bits.push(`${whole} item${whole===1?"":"s"}`);
  sum.textContent = bits.join(", ");
  refreshPendingToolbar();
}

function ripAllPending() {
  playRipAnimation({ count: 3, stagger: 220 });
  // Click every Stage 2 pending card's Rip It! in order. Each promotes
  // itself to Stage 3 and starts ripping. Editor selections, gallery
  // items, plain videos all do the right thing per their card type.
  // Snapshot the children list because clicking a button mutates the DOM
  // (card moves out of #pending into #jobs).
  const cards = [...$("#pending").children]
    .map(ch => ch._card).filter(Boolean);
  for (const c of cards) {
    const state = c.el.dataset.state || "";
    if (state === "downloading") continue;
    const sb = c.el.querySelector(".start");
    if (sb && !sb.disabled) sb.click();
  }
}

function fmtClock(s) {
  s = Math.max(0, +s || 0);
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return String(m).padStart(2, "0") + ":" + sec.toFixed(2).padStart(5, "0");
}

// Build a "Send to Rip Raptor" bookmarklet against THIS app's local server
// port (the WebView's window.location.origin) so it works regardless of
// which port the app picked. Show it as a draggable <a> the user can
// drop onto their browser's bookmarks bar.
function _buildBookmarkletHref() {
  const origin = window.location.origin; // e.g. http://127.0.0.1:8765
  // Inline IIFE: POST the current tab's URL to /queue using CORS so we
  // can read the response and confirm success vs failure with the
  // user. Server returns 200 + JSON on success, sets CORS headers; the
  // OPTIONS preflight is handled in do_OPTIONS. Three outcomes:
  //   • Rip Raptor accepted the URL  → green toast "✓ Sent"
  //   • Rip Raptor returned HTTP err → red toast with the status
  //   • Network failure (app off / wrong port) → red toast pointing
  //     the user at "is Rip Raptor running?"
  const body = (
    "(function(){" +
      "var u=location.href;" +
      "var O='" + origin + "';" +
      "function tst(m,bg){" +
        "var t=document.createElement('div');" +
        "t.textContent=m;" +
        "t.style.cssText='position:fixed;top:20px;right:20px;padding:12px 16px;background:'+bg+';color:#fff;border-radius:8px;font:14px/1.2 system-ui,sans-serif;z-index:2147483647;box-shadow:0 4px 16px rgba(0,0,0,0.4);max-width:320px;';" +
        "document.body.appendChild(t);" +
        "setTimeout(function(){t.style.transition='opacity .3s';t.style.opacity='0';},1800);" +
        "setTimeout(function(){t.remove();},2200);" +
      "}" +
      "fetch(O+'/queue',{method:'POST',mode:'cors',headers:{'Content-Type':'text/plain'},body:u})" +
        ".then(function(r){" +
          "if(r.ok){tst('\\uD83E\\uDD96 Sent to Rip Raptor','#0a7a30');}" +
          "else{tst('\\u26A0 Rip Raptor: HTTP '+r.status,'#a00000');}" +
        "})" +
        ".catch(function(){" +
          "tst('\\u26A0 Rip Raptor not reachable at '+O.replace(/^https?:\\/\\//,''),'#a00000');" +
        "});" +
    "})()"
  );
  return "javascript:" + encodeURI(body);
}

async function openSettingsMenu(ev) {
  if (ev) ev.stopPropagation();
  // Toggle off if already open
  const existing = document.getElementById("rr-settings-menu");
  if (existing) { existing.remove(); return; }
  const menu = document.createElement("div");
  menu.id = "rr-settings-menu";
  menu.className = "settings-menu";

  // Three labelled sections so the menu reads as scoped commands rather
  // than one flat list of unrelated actions.
  const SECTIONS = [
    {
      label: "Setup",
      items: [
        ["🦖 Send-to-Rip-Raptor bookmarklet…", () => { menu.remove(); showBookmarkletDialog(); }],
        ["🍪 Cookies source…",                  () => { menu.remove(); promptCookiesBrowserChange(); }],
        ["⬆ Check for yt-dlp updates",          async () => {
          menu.remove();
          const j = await checkYtDlpVersion(/*force=*/true);
          if (!j) { alert("Couldn't reach GitHub to check yt-dlp version."); return; }
          if (j.update_available) {
            if (confirm(`yt-dlp ${j.latest} is available (you have ${j.installed}).\n\nUpdate now?`)) {
              updateYtDlp();
            }
          } else {
            alert(`yt-dlp is up to date (${j.installed || "?"}).`);
          }
        }],
      ],
    },
    {
      label: "Folders",
      items: [
        ["📁 Open Downloads folder", () => { menu.remove(); openWellKnownFolder("downloads"); }],
        ["🧩 Open yt-dlp plugin folder", () => { menu.remove(); openWellKnownFolder("plugin"); }],
        ["⚙️ Open app data folder", () => { menu.remove(); openWellKnownFolder("data"); }],
      ],
    },
  ];

  for (const sec of SECTIONS) {
    const hd = document.createElement("div");
    hd.className = "settings-section-label";
    hd.textContent = sec.label;
    menu.appendChild(hd);
    for (const [label, fn] of sec.items) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.addEventListener("click", fn);
      menu.appendChild(b);
    }
  }

  // ───── Animations toggles ────────────────────────────────────────────
  // Three checkboxes for the three animations. Each persists to
  // localStorage immediately on change — no Save button needed. The
  // menu stays open so the user can flip several at once.
  const animLabel = document.createElement("div");
  animLabel.className = "settings-section-label";
  animLabel.textContent = "Animations";
  menu.appendChild(animLabel);
  const ANIMS = [
    ["intro", "Intro animation"],
    ["rip",   "“Rip It!” animation"],
    ["error", "Error animation"],
  ];
  for (const [key, label] of ANIMS) {
    const row = document.createElement("label");
    row.className = "settings-toggle";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = isAnimEnabled(key);
    cb.addEventListener("change", () => {
      setAnimEnabled(key, cb.checked);
    });
    // stopPropagation so clicking inside the menu doesn't trigger the
    // outside-click dismiss installed by the setTimeout below.
    row.addEventListener("click", (e) => e.stopPropagation());
    const txt = document.createElement("span");
    txt.textContent = label;
    row.appendChild(cb);
    row.appendChild(txt);
    menu.appendChild(row);
  }

  // About footer — credit + versions of every external tool we depend
  // on. Fetched lazily so the menu opens instantly; populated when the
  // response lands.
  const about = document.createElement("div");
  about.className = "settings-about";
  about.innerHTML = `
    <div><strong>Rip Raptor</strong> <span id="set-app-v">v…</span></div>
    <div class="settings-credit">Created by Henri Scott</div>
    <div>yt-dlp <span id="set-ytdlp-v">…</span></div>
    <div>ffmpeg <span id="set-ffmpeg-v">…</span></div>
  `;
  menu.appendChild(about);

  document.body.appendChild(menu);
  // Async populate the version footer.
  fetch("/versions").then(r => r.json()).then(j => {
    const setText = (id, v) => {
      const el = document.getElementById(id);
      if (el) el.textContent = v || "?";
    };
    setText("set-app-v",    "v" + (j.app || "?"));
    setText("set-ytdlp-v",  j.yt_dlp || "?");
    setText("set-ffmpeg-v", j.ffmpeg || "?");
  }).catch(() => {});

  // Close on next outside click.
  setTimeout(() => {
    document.addEventListener("click", function once(e) {
      if (!menu.contains(e.target)) { menu.remove(); }
      document.removeEventListener("click", once);
    });
  }, 0);
}

async function openWellKnownFolder(which) {
  try {
    await fetch("/open-folder", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({which}),
    });
  } catch (e) {}
}

function showBookmarkletDialog() {
  if (document.getElementById("rr-bookmarklet-modal")) return;
  const modal = document.createElement("div");
  modal.id = "rr-bookmarklet-modal";
  modal.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;";
  const href = _buildBookmarkletHref();
  modal.innerHTML = `
    <div style="background:#c0c0c0;border:2px solid;border-color:#fff #404040 #404040 #fff;padding:12px;width:520px;max-width:90vw;box-shadow:4px 4px 0 rgba(0,0,0,0.4);">
      <div style="font-weight:bold;margin-bottom:8px;">Send to Rip Raptor — Bookmarklet</div>
      <div style="font-size:11px;line-height:1.5;margin-bottom:10px;">
        Drag the link below onto your browser's bookmarks bar. Click it
        from any video page and the URL will appear in this app's queue
        automatically.
      </div>
      <div style="background:#fff;border:2px solid;border-color:#404040 #fff #fff #404040;padding:14px;text-align:center;margin-bottom:10px;">
        <a id="rr-bookmarklet-link" href="${href}" style="font-weight:bold;color:#000080;font-size:14px;cursor:grab;text-decoration:none;border:1px solid #000;background:#ffff80;padding:6px 12px;display:inline-block;">🦖 Send to Rip Raptor</a>
      </div>
      <div style="font-size:11px;color:#444;margin-bottom:10px;">
        If your browser blocks dragging the link, copy this URL and add a manual bookmark with it:
      </div>
      <textarea readonly style="width:100%;height:60px;font-family:Menlo,monospace;font-size:10px;box-sizing:border-box;">${href}</textarea>
      <div style="text-align:right;margin-top:10px;">
        <button id="rr-bookmarklet-close">Close</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
  modal.querySelector("#rr-bookmarklet-close").addEventListener("click", () => modal.remove());
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
  // Block accidental click of the link inside the modal — we only want
  // it to be dragged, not navigated.
  modal.querySelector("#rr-bookmarklet-link").addEventListener("click", (e) => e.preventDefault());
}
</script>
</body>
</html>
"""


EDITOR_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Rip Raptor — Editor</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    background: #008080;
    color: #000;
    font: 11px "MS Sans Serif", Tahoma, Geneva, Verdana, sans-serif;
    -webkit-font-smoothing: none;
  }
  .window {
    min-height: 100vh;
    background: #c0c0c0;
    padding: 10px;
  }
  h1 { margin: 0 0 8px; font-size: 14px; }
  .row { display: flex; gap: 8px; align-items: center; }
  .panel {
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    background: #c0c0c0;
    padding: 8px;
    margin-bottom: 8px;
  }
  video {
    width: 100%;
    max-height: 60vh;
    background: #000;
    display: block;
  }
  /* Premiere-style filmstrip timeline. The played portion is no longer
     a blue fill — the white playhead alone marks "now". The track itself
     is a row of evenly-spaced frame thumbnails so the user can read the
     video's content at a glance and scrub by sight. */
  .timeline {
    position: relative;
    height: 52px;            /* tall enough to make the filmstrip readable */
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    background: #2a2a2a;     /* shows through gaps between frames while loading */
    margin: 8px 0;
    cursor: pointer;
    user-select: none;
    outline: none;
    overflow: hidden;
  }
  .timeline:focus { box-shadow: inset 0 0 0 1px #6090ff; }
  .timeline.scrubbing { cursor: grabbing; }
  /* Filmstrip container — sits at the back of the stacking context. */
  .tl-filmstrip {
    position: absolute; inset: 0;
    display: flex;
    pointer-events: none;
    z-index: 0;
  }
  /* Saved-item overlay: bands for clips, narrow ticks for stills. Sits
     between the filmstrip frames and the In/Out range overlay so the
     user sees their existing coverage at a glance. Visual-only — the
     entire overlay is pointer-events:none so it never intercepts clicks
     intended for the timeline scrub. To re-load an item, click its row
     in the items table. */
  .tl-bands {
    position: absolute; inset: 0;
    z-index: 1;
    pointer-events: none;
  }
  .tl-band {
    position: absolute; top: 0; bottom: 0;
    background: rgba(255, 128, 0, 0.32);
    border-left:  2px solid rgba(255, 128, 0, 0.85);
    border-right: 2px solid rgba(255, 128, 0, 0.85);
  }
  .tl-band.active { background: rgba(255, 128, 0, 0.62); box-shadow: 0 0 0 1px #fff; }
  .tl-tick {
    position: absolute; top: 0; bottom: 0;
    width: 3px;
    background: #2e8030;
    box-shadow: 0 0 4px rgba(46, 128, 48, 0.85);
    transform: translateX(-1px);
  }
  .tl-tick.active { background: #4caf50; box-shadow: 0 0 0 1px #fff, 0 0 6px #4caf50; }
  .tl-frame {
    flex: 1 1 0;
    min-width: 0;
    background: #2a2a2a center/cover no-repeat;
    border-right: 1px solid rgba(0, 0, 0, 0.55);
  }
  .tl-frame:last-child { border-right: none; }
  .tl-frame.loading {
    background: linear-gradient(90deg, #2a2a2a 0%, #3a3a3a 50%, #2a2a2a 100%);
    background-size: 200% 100%;
    animation: tl-shimmer 1.6s linear infinite;
  }
  @keyframes tl-shimmer {
    0%   { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }
  /* Kept in DOM for layout-stability but no longer the visual anchor —
     the playhead cursor alone communicates progress. */
  .tl-played { display: none; }

  /* Minimap — always visible, sits directly under the filmstrip. Shows
     the full duration in miniature with a translucent yellow indicator
     for the currently-visible window. Acts as both a status indicator
     ("there's content offscreen this way") and a control (drag the
     window indicator, click rail to recenter, scroll-wheel to nudge).
     Saved-clip bands are mirrored as faint orange dots so coverage is
     visible at a glance. */
  .tl-mini {
    position: relative;
    height: 14px;
    margin: 2px 0 0 0;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    background: #2a2a2a;
    cursor: pointer;
    user-select: none;
    overflow: hidden;
  }
  .tl-mini-bands {
    position: absolute; inset: 0;
    pointer-events: none;
  }
  .tl-mini-bands .mb-band {
    position: absolute; top: 1px; bottom: 1px;
    background: rgba(255, 128, 0, 0.55);
  }
  .tl-mini-bands .mb-tick {
    position: absolute; top: 1px; bottom: 1px;
    width: 1px; background: #4caf50;
  }
  .tl-mini-window {
    position: absolute; top: 0; bottom: 0;
    background: rgba(255, 234, 0, 0.20);
    border-left:  2px solid rgba(255, 234, 0, 0.95);
    border-right: 2px solid rgba(255, 234, 0, 0.95);
    cursor: grab;
    box-sizing: border-box;
    min-width: 4px;
  }
  .tl-mini-window:active { cursor: grabbing; }
  /* Playhead cursor: deliberately NOT red — the OUT mark is red and
     when the playhead drifts past Out the user could otherwise see two
     red lines and think the OUT mark duplicated. White line + black halo
     reads as an unambiguous "you are here" indicator at any zoom level,
     and the downward triangle reinforces the NLE-standard playhead look. */
  .tl-cursor {
    position: absolute; top: -3px; bottom: -3px;
    width: 2px; background: #ffffff;
    box-shadow: 0 0 0 1px #000, 0 0 6px rgba(255,255,255,0.55);
    z-index: 5;
    pointer-events: none;
  }
  .tl-cursor::before {
    /* Triangle pointer at the top of the line — apex points DOWN to the
       current frame. Larger than the in/out flags so the eye reads it
       as the primary "now" indicator. */
    content: "";
    position: absolute; left: -5px; top: -10px;
    border-left: 6px solid transparent;
    border-right: 6px solid transparent;
    border-top: 9px solid #ffffff;
    filter: drop-shadow(0 0 1px #000) drop-shadow(0 0 2px rgba(0,0,0,0.6));
  }
  .tl-mark {
    position: absolute; top: -5px; bottom: -5px;
    width: 3px;
  }
  .tl-mark::after {
    /* Triangle flag at the top so marks read as "stops" not just lines. */
    content: ""; position: absolute; top: 0; left: -4px;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
  }
  /* In/Out marks use NLE-standard colors: cyan-blue for in, red for out.
     Distinct from each other and from the timeline scrub colors, no
     legacy lime green. */
  .tl-mark.in  { background: #2ea0ff; box-shadow: 0 0 4px #2ea0ff; }
  .tl-mark.in::after  { border-top: 6px solid #2ea0ff; }
  .tl-mark.out { background: #ff3030; box-shadow: 0 0 4px #ff3030; }
  .tl-mark.out::after { border-top: 6px solid #ff3030; }
  .tl-range {
    position: absolute; top: 0; bottom: 0;
    background: rgba(46, 160, 255, 0.28);
    border-left: 1px solid rgba(46, 160, 255, 0.7);
    border-right: 1px solid rgba(255, 48, 48, 0.7);
    pointer-events: none;
    z-index: 2;  /* over filmstrip, under marks + cursor */
    mix-blend-mode: screen;  /* tints frames without obscuring them */
  }
  .tl-mark { z-index: 4; }
  /* Group label between transport rows — visually segments Selection vs Export. */
  .ctrl-group-label {
    font-size: 10px; color: #404040; text-transform: uppercase;
    letter-spacing: 0.5px; padding-right: 6px;
    border-right: 1px solid #808080;
  }
  .controls-row { align-items: center; flex-wrap: wrap; gap: 6px; }

  /* Speed-control label wrapping the dropdown. Inline form so the
     "Speed" word doesn't claim its own line in a tight transport row. */
  .speed-lbl {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 11px;
  }
  .speed-lbl select { font-size: 11px; padding: 1px 2px; height: 22px; }

  /* Player header — pinned to the top of the window across page scroll
     so Done / ? / save state are always one click away. Sits at z-index
     50 so it floats above filmstrip frames and the crop overlay (which
     belongs to the player panel and is fine being scrolled past). */
  .player-header {
    position: sticky; top: 0; z-index: 50;
    display: flex; align-items: center; gap: 8px;
    background: #c0c0c0;
    padding: 6px 10px;
    border-bottom: 2px solid;
    border-bottom-color: #404040;
    box-shadow: 0 1px 0 #ffffff inset, 0 2px 0 #ffffff;
  }
  /* Timeline zoom cluster — three small buttons + a tiny readout. The
     readout shows the current zoom factor like "2.5×". Sits in the
     player header between the title and the save pill. */
  .zoom-group {
    display: inline-flex; align-items: center; gap: 2px;
    padding: 2px 4px;
    border: 1px solid #808080;
  }
  .zoom-group button {
    min-width: 28px; padding: 2px 8px; font-size: 14px;
    line-height: 1; font-weight: bold;
  }
  /* Fit button has text ("Fit") so a slightly smaller font reads better. */
  .zoom-group #btn-zoom-fit { font-size: 11px; }
  .zoom-group #zoom-readout {
    min-width: 36px; text-align: center;
    font-size: 11px; color: #404040;
  }
  .player-header h1 {
    margin: 0; font-size: 13px; line-height: 1;
    flex: 1 1 auto; min-width: 0;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .player-header .pri-action { font-weight: bold; }
  /* `?` help button — icon-style: chunkier glyph, square-ish hit area. */
  .player-header #btn-help {
    font-size: 16px; font-weight: bold;
    padding: 1px 10px;
    min-width: 32px;
    line-height: 1;
  }

  /* Bordered control groups — standard Win98 groove fieldsets. The only
     differentiator between Selection / Clip / Still is the legend text
     color. Backgrounds stay transparent (panel grey) so the row reads as
     one cohesive unit, not a stack of clashing colored cards. */
  fieldset.ctrl-group {
    display: inline-flex; align-items: center; gap: 4px;
    margin: 0; padding: 2px 8px 4px;
    border: 2px groove #d4d0c8;
    background: transparent;
  }
  fieldset.ctrl-group > legend {
    padding: 0 4px;
    font-size: 11px; font-weight: bold;
    color: #000;
  }
  fieldset.ctrl-group.ctrl-selection > legend { color: #000080; }
  fieldset.ctrl-group.ctrl-crop      > legend { color: #404040; }
  /* Small badge on item rows that were captured with an active crop. */
  .crop-badge {
    display: inline-block;
    font-size: 9px; font-weight: bold;
    color: #803a00; background: #ffeed4;
    border: 1px solid #c08030;
    padding: 0 4px; margin-right: 4px; line-height: 14px;
    vertical-align: middle;
    border-radius: 1px;
  }
  /* Live crop overlay on the player. Wraps the <video> so the rect can
     be positioned in CSS pixels relative to the video's displayed area
     while we keep the saved crop math in source pixels. */
  .player-wrap {
    position: relative;
    display: block;
  }
  .player-wrap video { display: block; max-width: 100%; }
  .crop-overlay {
    position: absolute; inset: 0;
    pointer-events: none;
    display: none;
  }
  /* Two display modes:
     .show  → editing (interactive: handles, mask, action bar)
     .view  → committed crop, persistent visual reminder. Non-interactive
              ghost outline so the user can keep seeing exactly what
              region will be exported.                                  */
  .crop-overlay.show, .crop-overlay.view { display: block; }
  .crop-overlay.show { pointer-events: auto; }
  .crop-overlay.view { pointer-events: none; }
  .crop-overlay .co-rect {
    position: absolute;
    border: 2px dashed #ffea00;
    background: rgba(255, 234, 0, 0.06);
    box-shadow: 0 0 0 9999px rgba(0,0,0,0.55);
    cursor: move;
  }
  /* View mode: thin solid outline + small corner tabs, no mask, no
     handles, no fill. Communicates "this region is committed" without
     the dimmed surround that would obscure the player video. */
  .crop-overlay.view .co-rect {
    border: 1.5px solid rgba(255, 234, 0, 0.9);
    background: transparent;
    box-shadow: none;
    cursor: default;
  }
  .crop-overlay.view .co-rect::before,
  .crop-overlay.view .co-rect::after {
    content: ""; position: absolute; width: 10px; height: 10px;
    border: 2px solid #ffea00;
  }
  .crop-overlay.view .co-rect::before { top: -2px;    left: -2px;  border-right: 0; border-bottom: 0; }
  .crop-overlay.view .co-rect::after  { bottom: -2px; right: -2px; border-left: 0;  border-top: 0; }
  .crop-overlay.view .co-handle,
  .crop-overlay.view .co-bar,
  .crop-overlay.view .co-dims { display: none; }
  .crop-overlay .co-handle {
    position: absolute; width: 12px; height: 12px;
    background: #ffea00; border: 1px solid #000;
  }
  .crop-overlay .co-handle[data-handle="nw"] { left: -6px;  top: -6px;    cursor: nwse-resize; }
  .crop-overlay .co-handle[data-handle="ne"] { right: -6px; top: -6px;    cursor: nesw-resize; }
  .crop-overlay .co-handle[data-handle="sw"] { left: -6px;  bottom: -6px; cursor: nesw-resize; }
  .crop-overlay .co-handle[data-handle="se"] { right: -6px; bottom: -6px; cursor: nwse-resize; }
  .crop-overlay .co-handle[data-handle="n"]  { left: 50%; top: -6px;    transform: translateX(-50%); cursor: ns-resize; }
  .crop-overlay .co-handle[data-handle="s"]  { left: 50%; bottom: -6px; transform: translateX(-50%); cursor: ns-resize; }
  .crop-overlay .co-handle[data-handle="e"]  { top: 50%; right: -6px;   transform: translateY(-50%); cursor: ew-resize; }
  .crop-overlay .co-handle[data-handle="w"]  { top: 50%; left: -6px;    transform: translateY(-50%); cursor: ew-resize; }
  /* Floating action bar inside the overlay — Apply / Cancel. */
  .crop-overlay .co-bar {
    position: absolute; bottom: 8px; left: 50%; transform: translateX(-50%);
    display: flex; gap: 6px; padding: 4px;
    background: #c0c0c0;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    z-index: 2;
  }
  .crop-overlay .co-dims {
    position: absolute; top: 8px; left: 50%; transform: translateX(-50%);
    background: rgba(0, 0, 0, 0.7); color: #ffea00;
    padding: 2px 8px; font: 11px monospace;
  }
  fieldset.ctrl-group.ctrl-clip      > legend { color: #803a00; }
  fieldset.ctrl-group.ctrl-still     > legend { color: #105020; }
  /* Subtle 2px accent line under each export group's button, matching
     the legend color — gives a quiet visual link from the label to the
     action without flooding the row with backgrounds. */
  fieldset.ctrl-group.ctrl-clip  #btn-add-clip { box-shadow: inset 0 -2px 0 #c08030; }
  fieldset.ctrl-group.ctrl-still #btn-still    { box-shadow: inset 0 -2px 0 #2e8030; }
  /* Vertical divider between the Selection group and the Export groups. */
  .ctrl-divider {
    width: 1px; align-self: stretch; background: #808080;
    margin: 2px 4px;
  }
  /* Lighter sub-divider for splitting buttons WITHIN a single fieldset
     into logical sub-groups (define / review / commit). Inset so it
     doesn't compete visually with the heavier between-group dividers. */
  .ctrl-subdivider {
    display: inline-block;
    width: 1px; height: 18px;
    background: #b0b0b0;
    margin: 0 6px;
    align-self: center;
  }
  .snap-lbl {
    display: inline-flex; align-items: center; gap: 3px;
    cursor: pointer; user-select: none;
    color: #404040;
  }
  .snap-lbl input { cursor: pointer; }
  /* Keyframe ticks rendered along the bottom edge of the timeline. Only
     visible when "Snap to keyframes" is enabled. Soft yellow → easy to
     distinguish from in/out flags (cyan/red) and the playhead (white). */
  .tl-keyframes {
    position: absolute; left: 0; right: 0; bottom: 0;
    height: 6px;
    pointer-events: none;
    z-index: 2;
    display: none;
  }
  .tl-keyframes.show { display: block; }
  .tl-kf {
    position: absolute; bottom: 0;
    width: 1px; height: 6px;
    background: rgba(255, 234, 0, 0.55);
  }
  /* Markers — labelled bookmarks. Inverted purple triangle hanging from
     the TOP of the timeline, with the label appearing on hover above. */
  .tl-markers {
    position: absolute; left: 0; right: 0; top: 0; bottom: 0;
    pointer-events: none;
    z-index: 3;
  }
  .tl-marker {
    position: absolute; top: -10px;
    width: 0; height: 0;
    border-left: 7px solid transparent;
    border-right: 7px solid transparent;
    border-top: 10px solid #9f3ce0;
    transform: translateX(-7px);
    pointer-events: auto;
    cursor: pointer;
    filter: drop-shadow(0 0 1px #000);
  }
  .tl-marker::after {
    /* Vertical line continuing the triangle down through the timeline so
       the user can see exactly which frame the marker tags. */
    content: "";
    position: absolute; left: -1px; top: 10px;
    width: 1px; height: 56px;
    background: rgba(159, 60, 224, 0.55);
  }
  .tl-marker:hover { filter: drop-shadow(0 0 4px #d090ff); }
  .tl-marker .mk-label {
    position: absolute; bottom: 100%; left: 50%;
    transform: translate(-50%, -2px);
    background: #c0c0c0;
    border: 1px solid #404040;
    padding: 1px 5px;
    font-size: 10px; line-height: 12px; color: #000;
    white-space: nowrap;
    display: none;
    pointer-events: none;
  }
  .tl-marker:hover .mk-label { display: block; }
  /* Minimap mirror — small purple dot for each marker. */
  .tl-mini-bands .mb-marker {
    position: absolute; top: 1px; bottom: 1px;
    width: 2px; background: #9f3ce0;
    box-shadow: 0 0 3px rgba(159, 60, 224, 0.85);
  }
  /* Markers panel below the items table. Collapsible. */
  .markers-panel {
    margin-top: 6px;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    background: #ffffff;
    padding: 4px;
    max-height: 140px;
    overflow-y: auto;
  }
  .markers-panel .mp-empty { color: #808080; font-size: 11px; padding: 4px; }
  .markers-panel .mp-row {
    display: flex; align-items: center; gap: 6px;
    padding: 2px 4px; cursor: pointer;
  }
  .markers-panel .mp-row:hover { background: #d8e0ff; }
  .markers-panel .mp-time { font-family: monospace; min-width: 64px; color: #404040; }
  .markers-panel .mp-label { flex: 1; }
  .markers-panel .mp-actions { display: flex; gap: 2px; }
  .markers-panel .mp-actions button {
    font-size: 11px; padding: 2px 8px; min-width: 0;
    font-weight: bold;
  }
  /* × delete glyph — read as an icon, not a letter. */
  .markers-panel .mp-actions button[data-act="delete"] {
    font-size: 14px; padding: 0 8px; line-height: 18px;
  }
  /* Loop + Preview live in the Clip group, not the transport row, so
     they can shrink without competing with primary playback controls.
     Tighter padding, smaller text, narrower min-width — they read as
     "review the marked region" affordances rather than chunky buttons. */
  #btn-loop, #btn-preview {
    padding: 2px 8px;
    font-size: 10px;
    min-width: 0;
  }
  /* Loop toggle "on" state — same Win98 button look, just pressed-in.
     The inset border (dark on top/left, light on bottom/right) matches
     `button:active` so the engaged state reads as a sustained press. */
  #btn-loop.on {
    border-color: #404040 #ffffff #ffffff #404040;
    padding: 3px 7px 1px 9px;
  }
  /* Play button states: ▶ when paused, ⏸ when playing. Bigger glyph. */
  #btn-play { min-width: 48px; font-size: 14px; }
  /* Mute button: emoji rendering tweak. */
  #btn-mute { min-width: 38px; font-size: 14px; }
  /* Volume slider — thin rail, contained. */
  #vol { vertical-align: middle; }
  button {
    font: inherit;
    background: #c0c0c0;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    padding: 3px 10px;
    cursor: pointer;
    min-width: 64px;
  }
  button:active { border-color: #404040 #ffffff #ffffff #404040; }
  button:disabled { color: #808080; cursor: not-allowed; }
  input[type="text"], select {
    font: inherit;
    background: #fff; color: #000;
    border: 2px solid;
    border-color: #404040 #ffffff #ffffff #404040;
    padding: 2px 4px;
  }
  table { border-collapse: collapse; width: 100%; }
  th, td {
    border: 1px solid #808080;
    padding: 2px 6px;
    text-align: left;
    background: #ffffff;
    font-size: 11px;
    vertical-align: middle;
  }
  th { background: #c0c0c0; }
  .mono { font-family: "Lucida Console", Monaco, monospace; }
  .small { font-size: 10px; color: #404040; }
  .footer { display: flex; gap: 8px; align-items: center; }
  .grow { flex: 1 1 auto; }
  /* Lightbox for clip / still thumbnails — full-screen modal preview. */
  #lightbox {
    position: fixed; inset: 0; z-index: 10000;
    background: rgba(0,0,0,0.85);
    display: none; align-items: center; justify-content: center;
    cursor: zoom-out;
  }
  #lightbox.show { display: flex; }
  #lightbox img {
    max-width: 92vw; max-height: 92vh;
    box-shadow: 0 4px 24px rgba(0,0,0,0.6);
    image-rendering: auto;
  }
  #lightbox .lb-caption {
    position: absolute; bottom: 12px; left: 50%; transform: translateX(-50%);
    color: #fff; font: 12px "MS Sans Serif", Tahoma; opacity: 0.85;
    background: rgba(0,0,0,0.5); padding: 4px 10px; border-radius: 2px;
  }
  /* Items list — clickable rows + thumb cell. Active row uses the classic
     Win98 navy selection so it reads as "this is the loaded item" without
     screaming for attention. Inputs/selects inside the row keep their
     white background so they remain readable. */
  #items tbody tr { cursor: pointer; }
  /* Narrow checkbox column. */
  #items thead th.th-check { padding: 2px 4px; text-align: center; }
  #items td.td-check { padding: 0 4px; text-align: center; vertical-align: middle; }

  /* Selection checkboxes — slightly larger, easier hit target. */
  #items thead .th-check input,
  #items td.td-check input {
    width: 14px; height: 14px;
    cursor: pointer;
    margin: 0; vertical-align: middle;
  }

  /* Up/Down move arrows — old-school reorder. Sit in their own narrow
     column right after the checkbox, stacked vertically (▲ on top of ▼).
     Disabled state for top/bottom rows. Sized for easy clicking
     (~24×18px each) with bold, high-contrast triangle glyphs. */
  #items thead th.th-move,
  #items td.td-move { padding: 0 2px; text-align: center; vertical-align: middle; }
  .move-stack {
    display: inline-flex;
    flex-direction: column;
    gap: 0;
    align-items: stretch;
  }
  .move-stack .btn-move {
    min-width: 0;
    width: 28px;
    height: 22px;
    padding: 0;
    font-size: 14px;
    line-height: 16px;
    color: #000;
    /* Default Win98 button look from the global rule, just sized down. */
  }
  /* Stack the two arrows so they share an outer border — looks like one
     unit, no gap line between them. */
  .move-stack .btn-up   { border-bottom-width: 1px; }
  .move-stack .btn-down { border-top-width: 1px; margin-top: -1px; }
  .move-stack .btn-move:hover:not(:disabled) {
    background: #d8e4ff;
  }
  .move-stack .btn-move:disabled {
    color: #b8b8b8;
    cursor: not-allowed;
  }
  #items tbody tr:hover td { background: #f0f0f0; }
  /* Single highlight style for both states — checkbox-selected and
     click-loaded rows look identical (navy bg + white text). The
     additional inset cyan stripe on the leftmost cell marks the
     LOADED row specifically (the one currently in the player). */
  #items tbody tr.active td,
  #items tbody tr.selected td {
    background: #000080;
    color: #ffffff;
  }
  #items tbody tr.active td .small,
  #items tbody tr.active td .mono,
  #items tbody tr.selected td .small,
  #items tbody tr.selected td .mono { color: #d6e0ff; }
  #items tbody tr.active td:first-child { box-shadow: inset 3px 0 0 #5a8eff; }
  .thumb-cell {
    width: 90px; height: 50px;
    background: #000 center/cover no-repeat;
    border: 1px solid #404040;
    display: block; cursor: zoom-in;
  }
  /* Floating hover preview (shown while scrubbing the timeline) */
  #hover-thumb {
    position: fixed; display: none; pointer-events: none; z-index: 50;
    width: 160px; height: 90px;
    background: #000 center/cover no-repeat;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    box-shadow: 1px 1px 0 #000;
  }
  #hover-thumb .ht-time {
    position: absolute; left: 0; right: 0; bottom: 0;
    background: rgba(0, 0, 0, 0.7);
    color: #fff; font-size: 10px;
    text-align: center;
    font-family: "Lucida Console", Monaco, monospace;
    padding: 1px 0;
  }

  /* ========== Win98 scrollbars (WebKit) ==========
     Native macOS scrollbars look out of place on the beige Win98 chrome.
     These custom rules render a chunky, beveled scrollbar with a recessed
     track and outset thumb that flips to inset on :active. */
  ::-webkit-scrollbar { width: 16px; height: 16px; background: #c0c0c0; }
  ::-webkit-scrollbar-track {
    background:
      repeating-conic-gradient(#c0c0c0 0% 25%, #a0a0a0 0% 50%) 0 0/2px 2px;
    border: 1px solid #808080;
  }
  ::-webkit-scrollbar-thumb {
    background: #c0c0c0;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    min-height: 24px; min-width: 24px;
  }
  ::-webkit-scrollbar-thumb:active {
    border-color: #404040 #ffffff #ffffff #404040;
  }
  ::-webkit-scrollbar-corner { background: #c0c0c0; }
  /* Up/down arrow buttons at the ends — disabled glyphs for now (the
     scroll arrows show as plain bevels). The chunky thumb is the win. */
  ::-webkit-scrollbar-button:single-button:vertical:start,
  ::-webkit-scrollbar-button:single-button:vertical:end,
  ::-webkit-scrollbar-button:single-button:horizontal:start,
  ::-webkit-scrollbar-button:single-button:horizontal:end {
    background: #c0c0c0;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    width: 16px; height: 16px; display: block;
  }
  ::-webkit-scrollbar-button:single-button:active {
    border-color: #404040 #ffffff #ffffff #404040;
  }

  /* Marks become draggable affordances. Wide invisible hit area so users
     don't have to pixel-hunt the 3px line. Cursor cues the gesture. */
  .tl-mark { pointer-events: auto; cursor: ew-resize; }
  .tl-mark::before {
    content: ""; position: absolute; top: -8px; bottom: -8px;
    left: -7px; right: -7px;
  }
  .tl-mark.dragging { box-shadow: 0 0 8px #fff; }

  /* Save indicator pill — bottom-right of the items panel. Aria-live so
     screen readers get the state too. */
  .save-pill {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 10px; padding: 1px 6px;
    border: 1px solid #404040; background: #c0c0c0;
    color: #404040;
    font-family: "MS Sans Serif", Tahoma;
  }
  .save-pill[data-state="saving"] { background: #ffffd0; }
  .save-pill[data-state="saved"]  { background: #d8ffd0; color: #003800; }
  .save-pill[data-state="error"]  { background: #ffd0d0; color: #800000; }
  .save-pill .save-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: #808080;
  }
  .save-pill[data-state="saving"] .save-dot { background: #c08000; animation: pill-pulse 0.9s ease-in-out infinite; }
  .save-pill[data-state="saved"]  .save-dot { background: #008000; }
  .save-pill[data-state="error"]  .save-dot { background: #c00000; }
  @keyframes pill-pulse {
    0%, 100% { opacity: 0.4; }
    50%      { opacity: 1; }
  }

  /* Cache-prefetch pill — shows progress while the editor is still on
     the live /proxy stream. Swaps the bar's background to a green
     "ready" state when /editor/cache-status flips ready, then fades out
     a few seconds later. */
  .cache-pill {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 10px; padding: 1px 6px;
    border: 1px solid #404040; background: #c0c0c0;
    color: #404040;
    font-family: "MS Sans Serif", Tahoma;
    transition: opacity 600ms ease-out;
  }
  .cache-pill[data-state="ready"] {
    background: #d8ffd0; color: #003800;
  }
  .cache-pill[data-state="error"] {
    background: #ffd0d0; color: #800000;
  }
  .cache-pill .cache-pill-track {
    display: inline-block;
    width: 80px; height: 8px;
    background: #fff;
    border: 1px solid #404040;
    overflow: hidden;
  }
  .cache-pill .cache-pill-track > span {
    display: block; height: 100%;
    background: #80c0ff;
    transition: width 250ms linear;
  }
  /* Indeterminate state — no Content-Length up front, so animate a
     shimmer instead of a percent bar. */
  .cache-pill[data-state="loading-indeterminate"] .cache-pill-track > span {
    background: linear-gradient(90deg,
      #80c0ff 0%, #c0e0ff 50%, #80c0ff 100%);
    background-size: 200% 100%;
    animation: cache-shimmer 1.4s linear infinite;
  }
  .cache-pill[data-state="ready"] .cache-pill-track > span { background: #40c060; }
  .cache-pill[data-state="error"] .cache-pill-track > span { background: #c04040; }
  @keyframes cache-shimmer {
    0%   { background-position: 100% 0; }
    100% { background-position: -100% 0; }
  }

  /* Shortcut help overlay — same layering as the lightbox. Opened with `?`. */
  #shortcut-help {
    position: fixed; inset: 0; z-index: 10001;
    background: rgba(0,0,0,0.6);
    display: none; align-items: center; justify-content: center;
  }
  #shortcut-help.show { display: flex; }
  #shortcut-help .sh-card {
    background: #c0c0c0;
    border: 2px solid;
    border-color: #ffffff #404040 #404040 #ffffff;
    padding: 14px 18px;
    min-width: 360px; max-width: 92vw; max-height: 86vh; overflow: auto;
    box-shadow: 2px 2px 0 #000;
    font: 11px "MS Sans Serif", Tahoma;
  }
  #shortcut-help h2 {
    margin: 0 0 8px; font-size: 13px; padding-bottom: 4px;
    border-bottom: 1px solid #808080;
  }
  #shortcut-help table { width: 100%; }
  #shortcut-help td {
    border: none; padding: 3px 8px 3px 0; background: transparent;
  }
  #shortcut-help kbd {
    display: inline-block; min-width: 20px; padding: 1px 6px;
    border: 1px solid #404040;
    border-radius: 2px;
    background: #fff; font: 10px "Lucida Console", monospace;
    box-shadow: 1px 1px 0 #404040;
    margin-right: 3px;
  }
  #shortcut-help .sh-section td:first-child {
    font-weight: bold; padding-top: 8px; color: #000080;
  }
  #shortcut-help .sh-close {
    position: sticky; bottom: -8px;
    text-align: right; padding-top: 6px;
  }

  /* (Selected rows are styled by the unified .selected/.active rule
     above. The "loaded" row gets an additional inset cyan stripe on
     its leftmost cell as the only distinguishing cue.) */

  /* Color-coded Type column — matches the rest of the editor's palette:
       clip   = orange (filmstrip bands, Clip fieldset legend)
       still  = green  (filmstrip ticks, Still fieldset legend)
       concat = purple (concat tile badge, marker triangles)
     Text becomes a light variant when the row is selected (navy bg)
     so it stays readable. */
  #items td.type-cell {
    font-size: 10px; font-weight: bold;
    text-transform: uppercase; letter-spacing: 0.6px;
    text-align: center;
  }
  #items td.type-clip   { color: #c0570a; }
  #items td.type-still  { color: #1e7a26; }
  #items td.type-concat { color: #6a1ca8; }
  /* On selected/active (navy bg), bump to lighter shades for contrast. */
  #items tbody tr.selected td.type-clip,
  #items tbody tr.active   td.type-clip   { color: #ffb060; }
  #items tbody tr.selected td.type-still,
  #items tbody tr.active   td.type-still  { color: #80ff80; }
  #items tbody tr.selected td.type-concat,
  #items tbody tr.active   td.type-concat { color: #d090ff; }
</style>
</head>
<body>
<div class="window">
  <!-- Hidden helpers: scratch canvas for thumbnail capture, offscreen
       video for hover-preview seeking without disturbing the main player.
       Kept at a real size + opacity:0 because WebKit skips frame decode
       on 1×1 / display:none videos that have never been played. -->
  <canvas id="thumb-canvas" style="display:none"></canvas>
  <div id="lightbox" role="dialog" aria-modal="true">
    <img id="lightbox-img" alt="">
    <div class="lb-caption" id="lightbox-caption"></div>
  </div>
  <video id="prev-video" muted playsinline preload="auto"
         crossorigin="anonymous"
         style="position:fixed;left:0;top:0;width:320px;height:180px;
                opacity:0;pointer-events:none;z-index:-1;"></video>
  <div id="hover-thumb"><div class="ht-time" id="ht-time"></div></div>

  <!-- Sticky header bar — title + save indicator + global actions. Pinned
       to the top of the .window so the user always has Done / ? / save
       state reachable, even when scrolled deep into the items list. -->
  <div class="player-header">
    <h1 id="ed-title">Editor</h1>
    <span id="save-pill" class="save-pill" data-state="saved" aria-live="polite">
      <span class="save-dot"></span><span id="save-pill-text">Saved</span>
    </span>
    <!-- Cache-prefetch pill. Shows download progress while the editor
         is on the live /proxy stream; flips to "Local cache ready" then
         fades out once swapToCachedSource runs. After the swap, every
         seek hits local disk instead of paying a CDN round trip. -->
    <span id="cache-pill" class="cache-pill" data-state="loading" aria-live="polite">
      <span class="cache-pill-track"><span id="cache-pill-bar"></span></span>
      <span id="cache-pill-text">Caching…</span>
    </span>
    <button id="btn-help" title="Keyboard shortcuts (?)">?</button>
    <button id="btn-done" class="pri-action">Done</button>
  </div>

  <div class="panel" id="player-panel">
    <!-- Player + crop overlay container. The overlay sits absolutely over
         the video so the user can see the crop region against live
         playback. Hidden by default; shown only while editing the crop. -->
    <div class="player-wrap" id="player-wrap">
      <!-- preload="metadata" only — for long videos, "auto" buffers
           multi-MB read-ahead windows from the live /proxy stream which
           hammers the upstream CDN. Fetching metadata is enough to
           establish duration + dimensions; further bytes load on demand
           as the user actually scrubs/plays. Once the background
           prefetch lands, swapToCachedSource() swaps src to /cached/<sid>
           and seeks become local-disk-fast. -->
      <video id="player" preload="metadata" crossorigin="anonymous" playsinline></video>
      <div class="crop-overlay" id="crop-overlay">
        <div class="co-dims" id="co-dims"></div>
        <div class="co-rect" id="co-rect">
          <div class="co-handle" data-handle="nw"></div>
          <div class="co-handle" data-handle="n"></div>
          <div class="co-handle" data-handle="ne"></div>
          <div class="co-handle" data-handle="e"></div>
          <div class="co-handle" data-handle="se"></div>
          <div class="co-handle" data-handle="s"></div>
          <div class="co-handle" data-handle="sw"></div>
          <div class="co-handle" data-handle="w"></div>
        </div>
        <div class="co-bar">
          <button id="btn-crop-cancel">Cancel</button>
          <button id="btn-crop-apply" class="primary">Apply crop</button>
        </div>
      </div>
    </div>

    <!-- Transport row — playback + zoom + audio. Sits BETWEEN the player
         and the timeline so the controls feel attached to the player and
         the filmstrip reads as the navigable track underneath. -->
    <div class="row controls-row">
      <button id="btn-play" title="Play / pause (Space)">▶</button>
      <button id="btn-back" title="Previous frame (←)">&laquo; 1f</button>
      <button id="btn-fwd" title="Next frame (→)">1f &raquo;</button>
      <span class="mono" id="time-display">00:00.000 / 00:00.000</span>
      <label class="speed-lbl" title="Playback speed">
        Speed
        <select id="speed">
          <option value="0.25">0.25×</option>
          <option value="0.5">0.5×</option>
          <option value="1" selected>1×</option>
          <option value="1.25">1.25×</option>
          <option value="1.5">1.5×</option>
          <option value="2">2×</option>
        </select>
      </label>
      <span class="zoom-group" title="Timeline zoom — Cmd-scroll on the filmstrip to zoom, scroll to pan when zoomed">
        <button id="btn-zoom-out" title="Zoom out (Cmd −)">−</button>
        <span id="zoom-readout" class="mono">1×</span>
        <button id="btn-zoom-in"  title="Zoom in (Cmd +)">+</button>
        <button id="btn-zoom-fit" title="Fit timeline (Cmd 0)">Fit</button>
      </span>
      <span class="grow"></span>
      <button id="btn-mute" title="Mute (M)">🔊</button>
      <input id="vol" type="range" min="0" max="1" step="0.01" value="1" title="Volume" style="width:80px;">
    </div>

    <!-- Filmstrip timeline. Sits below the transport so the controls
         read as the player's controls and the filmstrip reads as a
         navigable track underneath. -->
    <div class="timeline" id="tl" tabindex="0">
      <!-- N evenly-spaced thumbnails populated lazily after the video
           metadata loads (server-side ffmpeg via /editor/thumb, cached
           client-side). -->
      <div class="tl-filmstrip" id="tl-filmstrip"></div>
      <!-- Saved-item overlay: clip bands + still ticks. -->
      <div class="tl-bands" id="tl-bands"></div>
      <!-- Keyframe positions, shown when "Snap to keyframes" is on. -->
      <div class="tl-keyframes" id="tl-keyframes"></div>
      <!-- Markers (labelled bookmarks) rendered as inverted triangles. -->
      <div class="tl-markers" id="tl-markers"></div>
      <div class="tl-played" id="tl-played"></div>
      <div class="tl-range" id="tl-range" style="display:none;"></div>
      <div class="tl-mark in" id="tl-in" style="display:none;"></div>
      <div class="tl-mark out" id="tl-out" style="display:none;"></div>
      <div class="tl-cursor" id="tl-cursor"></div>
    </div>

    <!-- Always-visible minimap — full-duration rail with a translucent
         indicator showing the currently-visible window. At 1× the
         indicator fills the rail; zoomed in, it shrinks. Drag to pan,
         click rail to recenter. Saved-clip overlay rendered too so you
         see overall coverage even when zoomed in tight. -->
    <div class="tl-mini" id="tl-mini" title="Drag to pan · click to recenter · scroll to nudge">
      <div class="tl-mini-bands" id="tl-mini-bands"></div>
      <div class="tl-mini-window" id="tl-mini-window"></div>
    </div>

    <!-- Selection + Export + Crop row. Sits below the navigable
         filmstrip so the order reads as: player → controls →
         scrub track → "what to do with this selection". -->
    <div class="row controls-row" style="margin-top:6px;">
      <fieldset class="ctrl-group ctrl-clip">
        <legend>Clip</legend>
        <!-- Sub-group 1: define the selection. -->
        <button id="btn-mark-in"  title="Set In point at playhead (I)">Set In (I)</button>
        <button id="btn-mark-out" title="Set Out point at playhead (O)">Set Out (O)</button>
        <button id="btn-clear"    title="Clear In/Out marks">Clear</button>
        <label class="snap-lbl small" title="Snap marks to nearest keyframe — cleaner cuts, smaller files">
          <input type="checkbox" id="btn-snap-key"> snap KF
        </label>
        <span class="ctrl-subdivider" aria-hidden="true"></span>
        <!-- Sub-group 2: review the selection. -->
        <button id="btn-loop"     title="Loop between In/Out marks (L)" class="toggle">↻ Loop</button>
        <button id="btn-preview"  title="Preview marked clip once (P) · or play through a loaded concat's segments">▶ Preview</button>
        <span class="ctrl-subdivider" aria-hidden="true"></span>
        <!-- Sub-group 3: commit the selection as a clip item. -->
        <button id="btn-add-clip" class="primary" title="Save selection as a clip (C)">Add Clip (C)</button>
      </fieldset>
      <fieldset class="ctrl-group ctrl-still">
        <legend>Still</legend>
        <select id="still-format" title="Image format">
          <option value="jpeg" selected>JPEG</option>
          <option value="png">PNG</option>
        </select>
        <button id="btn-still" class="primary" title="Capture current frame as a still (S)">Capture (S)</button>
      </fieldset>
      <span class="ctrl-divider" aria-hidden="true"></span>
      <!-- Crop controls. Detect runs ffmpeg cropdetect across the source.
           Custom opens a live crop overlay on the player. Reset clears.
           The current crop is snapshotted onto every NEW clip/still at
           the moment of capture (so cropping happens before, not after). -->
      <fieldset class="ctrl-group ctrl-crop">
        <legend>Crop</legend>
        <button id="btn-crop-detect" title="Auto-detect black bars (cropdetect)">Detect bars</button>
        <button id="btn-crop-custom" title="Drag a crop rect on the player">Custom…</button>
        <select id="crop-aspect" title="Crop to aspect ratio (centered)">
          <option value="">Aspect…</option>
          <option value="1:1">1:1 — Square</option>
          <option value="9:16">9:16 — Vertical (TikTok / Reels)</option>
          <option value="4:5">4:5 — Portrait (Instagram)</option>
          <option value="16:9">16:9 — Wide</option>
          <option value="3:2">3:2 — Photo</option>
          <option value="2.39:1">2.39:1 — Cinemascope</option>
        </select>
        <button id="btn-crop-reset"  title="Clear session crop">Reset</button>
        <span id="crop-readout" class="small mono"></span>
      </fieldset>
    </div>
  </div>

  <div class="panel">
    <div class="row" style="margin-bottom:6px;">
      <strong>Items</strong>
      <span class="small">— click a row to load · checkboxes for batch ops · ↑/↓ navigate · ⌘↑/↓ reorder · ⌫ remove · ? help</span>
      <span class="grow"></span>
      <button id="btn-concat-selected" style="display:none" title="Stitch the selected clips into a single video file">Concat clips (0)</button>
      <button id="btn-remove-selected" style="display:none">Remove (0)</button>
    </div>
    <table id="items">
      <thead>
        <tr>
          <th style="width:24px" class="th-check">
            <input type="checkbox" id="items-select-all" aria-label="Select all items">
          </th>
          <th style="width:34px" class="th-move" aria-label="Reorder"></th>
          <th style="width:96px">Thumb</th>
          <th style="width:54px">Type</th>
          <th>Name</th>
          <th style="width:170px">Range / Time</th>
          <th style="width:120px">Format</th>
          <th style="width:100px">Quality</th>
          <th style="width:100px">Actions</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
    <!-- Markers panel — labelled bookmarks at any time. Press B at the
         playhead to drop one. Click a row here (or the triangle on the
         timeline) to jump to its time. -->
    <details id="markers-details" style="margin-top:8px;">
      <summary><strong>Markers</strong>
        <span class="small">— B to drop at playhead · click to jump</span>
      </summary>
      <div class="markers-panel" id="markers-panel"></div>
    </details>
  </div>
</div>

<!-- Keyboard shortcut help. Toggled with `?`. Esc or click closes. -->
<div id="shortcut-help" role="dialog" aria-modal="true" aria-labelledby="sh-title">
  <div class="sh-card">
    <h2 id="sh-title">Keyboard shortcuts</h2>
    <table>
      <tr class="sh-section"><td colspan="2">Playback</td></tr>
      <tr><td><kbd>Space</kbd></td><td>Play / pause</td></tr>
      <tr><td><kbd>,</kbd> / <kbd>.</kbd></td><td>Step ±1 frame</td></tr>
      <tr><td><kbd>←</kbd> / <kbd>→</kbd></td><td>Seek ±1 second</td></tr>
      <tr><td><kbd>Shift</kbd>+<kbd>←</kbd> / <kbd>→</kbd></td><td>Seek ±10 seconds</td></tr>
      <tr><td><kbd>L</kbd></td><td>Toggle loop between marks</td></tr>
      <tr><td><kbd>P</kbd></td><td>Preview the marked clip once</td></tr>
      <tr><td><kbd>M</kbd></td><td>Mute / unmute</td></tr>
      <tr><td><kbd>Cmd</kbd>+<kbd>=</kbd> / <kbd>-</kbd></td><td>Zoom timeline in / out</td></tr>
      <tr><td><kbd>Cmd</kbd>+<kbd>0</kbd></td><td>Fit timeline to full duration</td></tr>
      <tr><td colspan="2" class="small">Or hold <kbd>Cmd</kbd> and scroll over the timeline. Plain scroll pans when zoomed in.</td></tr>

      <tr class="sh-section"><td colspan="2">Marks</td></tr>
      <tr><td><kbd>I</kbd></td><td>Set In point at playhead</td></tr>
      <tr><td><kbd>O</kbd></td><td>Set Out point at playhead</td></tr>
      <tr><td><kbd>Q</kbd> / <kbd>W</kbd></td><td>Jump to In / Out</td></tr>
      <tr><td><kbd>[</kbd> / <kbd>]</kbd></td><td>Nudge In / Out by 1 frame</td></tr>
      <tr><td><kbd>Shift</kbd>+<kbd>[</kbd> / <kbd>]</kbd></td><td>Nudge In / Out by 1 second</td></tr>
      <tr><td colspan="2" class="small">Drag the cyan/red flags directly on the timeline to reposition. Hold <kbd>Alt</kbd> while dragging to snap to the playhead.</td></tr>

      <tr class="sh-section"><td colspan="2">Capture</td></tr>
      <tr><td><kbd>C</kbd></td><td>Add clip from current marks (or Save changes when editing)</td></tr>
      <tr><td><kbd>S</kbd></td><td>Capture still at playhead</td></tr>
      <tr><td><kbd>B</kbd></td><td>Drop a marker (labelled bookmark) at playhead</td></tr>

      <tr class="sh-section"><td colspan="2">Items</td></tr>
      <tr><td><kbd>⌫</kbd> / <kbd>Delete</kbd></td><td>Remove selected items</td></tr>
      <tr><td><kbd>↑</kbd> / <kbd>↓</kbd></td><td>Load previous / next item in the list</td></tr>
      <tr><td><kbd>Cmd</kbd>+<kbd>↑</kbd> / <kbd>Cmd</kbd>+<kbd>↓</kbd></td><td>Move loaded item up / down in the list</td></tr>
      <tr><td><kbd>Cmd</kbd>+<kbd>A</kbd></td><td>Select all items</td></tr>
      <tr><td><kbd>Cmd</kbd>+<kbd>Z</kbd></td><td>Undo last change</td></tr>
      <tr><td><kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>Z</kbd></td><td>Redo</td></tr>
      <tr><td><kbd>Esc</kbd></td><td>Close help / deselect</td></tr>

      <tr class="sh-section"><td colspan="2">Other</td></tr>
      <tr><td><kbd>?</kbd></td><td>Toggle this overlay</td></tr>
    </table>
    <div class="sh-close"><button id="sh-close-btn">Close</button></div>
  </div>
</div>

<script>
const SID = "__SID__";
const KIND = "__KIND__";
const DURATION = parseFloat("__DURATION__") || 0;
const FILENAME_HINT = "__FILENAME_HINT__" || "video";
const TITLE = "__TITLE__" || FILENAME_HINT;
const SRC = (KIND === "hls") ? `/hls/${SID}/playlist.m3u8` : `/proxy/${SID}`;

const player = document.getElementById("player");
const prevVideo = document.getElementById("prev-video");
const thumbCanvas = document.getElementById("thumb-canvas");
const hoverThumb = document.getElementById("hover-thumb");
const hoverTime = document.getElementById("ht-time");
const titleEl = document.getElementById("ed-title");
titleEl.textContent = TITLE + " — Editor";

// Hook up the source. For HLS, native Safari/WKWebView plays it directly.
// Calling .load() explicitly nudges WebKit's media element out of any
// limbo state where setting .src alone doesn't trigger a load — happens
// occasionally on first editor open while the cached source is still
// being warmed by the server in the background.
player.src = SRC;
prevVideo.src = SRC;
try { player.load(); } catch (e) {}
try { prevVideo.load(); } catch (e) {}

// ───── Local-cache swap ─────────────────────────────────────────────────
// The server kicks off a background prefetch of the source on /editor/
// page render. As long as we're on the live /proxy stream every seek
// pays a CDN round trip; once the cache lands we want to swap to
// /cached/<sid> so seeks hit local disk. /editor/cache-status returns
// {ready, bytes, total} — we poll every 2s, render a small progress
// pill, and call swapToCachedSource() once ready flips true.
let _cacheSwapped = false;
let _cachePollHandle = null;

function fmtMB(b) { return (b / (1024 * 1024)).toFixed(0) + "MB"; }

// Wall-clock anchor for the cache prefetch — wakes on the first
// non-zero progress update and feeds the ETA computation. Reset on
// pill state transitions (ready/error) so the next session starts
// fresh.
let _cacheStartedAt = 0;

function updateCachePill(ready, bytes, total, errored) {
  const pill = document.getElementById("cache-pill");
  const text = document.getElementById("cache-pill-text");
  const bar  = document.getElementById("cache-pill-bar");
  if (!pill || !text) return;
  if (errored) {
    _cacheStartedAt = 0;
    pill.dataset.state = "error";
    text.textContent = "Cache failed — using live stream";
    if (bar) bar.style.width = "0%";
    setTimeout(() => { pill.style.opacity = "0"; }, 6000);
    return;
  }
  if (ready) {
    _cacheStartedAt = 0;
    pill.dataset.state = "ready";
    text.textContent = "Local cache ready";
    if (bar) bar.style.width = "100%";
    // Fade out a few seconds after the swap so it doesn't linger.
    setTimeout(() => { pill.style.opacity = "0"; }, 2400);
    return;
  }
  pill.dataset.state = "loading";
  if (!_cacheStartedAt && bytes > 0) _cacheStartedAt = Date.now();
  if (total > 0) {
    const pct = Math.min(100, Math.max(0, (bytes / total) * 100));
    const elapsed = _cacheStartedAt ? (Date.now() - _cacheStartedAt) / 1000 : 0;
    const eta = fmtETA(elapsed, pct / 100);
    text.textContent = `Caching… ${pct.toFixed(0)}% (${fmtMB(bytes)} / ${fmtMB(total)})${eta ? ` · ETA ${eta}` : ""}`;
    if (bar) bar.style.width = pct.toFixed(1) + "%";
  } else if (bytes > 0) {
    // No content-length up front (chunked / HLS) — show MB downloaded
    // and let the bar shimmer instead of growing.
    text.textContent = `Caching… ${fmtMB(bytes)}`;
    if (bar) bar.style.width = "100%";
    pill.dataset.state = "loading-indeterminate";
  } else {
    text.textContent = "Caching…";
    if (bar) bar.style.width = "100%";
    pill.dataset.state = "loading-indeterminate";
  }
}

function swapToCachedSource() {
  if (_cacheSwapped) return;
  _cacheSwapped = true;
  const newSrc = `/cached/${SID}`;
  // Preserve the user's current viewing state across the src change.
  // <video> resets currentTime to 0 on src change, so we restore it on
  // the first loadedmetadata event for the new src. If the user was
  // playing, we resume; if paused, we leave paused.
  const t = player.currentTime || 0;
  const wasPaused = player.paused;
  const onMeta = () => {
    player.removeEventListener("loadedmetadata", onMeta);
    try {
      const cap = (player.duration || t) - 0.001;
      player.currentTime = Math.max(0, Math.min(cap, t));
    } catch (e) {}
    if (!wasPaused) {
      player.play().catch(() => {});
    }
  };
  player.addEventListener("loadedmetadata", onMeta);
  player.src = newSrc;
  // prev-video is the off-screen scrub helper used by the filmstrip /
  // hover-thumb. Same swap, no time-restore needed (it's seek-on-demand).
  prevVideo.src = newSrc;
  try { player.load(); } catch (e) {}
  try { prevVideo.load(); } catch (e) {}
}

async function pollCacheStatus() {
  if (_cacheSwapped) return;
  try {
    const r = await fetch(`/editor/cache-status?sid=${encodeURIComponent(SID)}`);
    if (!r.ok) {
      _cachePollHandle = setTimeout(pollCacheStatus, 5000);
      return;
    }
    const j = await r.json();
    if (j.error) {
      // Backend reported a fatal error — leave the player on /proxy and
      // surface a one-line warning. The user can still edit; only the
      // local-disk speedup is unavailable.
      updateCachePill(false, 0, 0, true);
      return;
    }
    if (j.ready) {
      updateCachePill(true, j.size || 0, j.size || 0, false);
      swapToCachedSource();
      return;
    }
    updateCachePill(false, j.bytes || 0, j.total || 0, false);
    _cachePollHandle = setTimeout(pollCacheStatus, 2000);
  } catch (e) {
    _cachePollHandle = setTimeout(pollCacheStatus, 5000);
  }
}
// Kick off after a short delay so the editor has time to render its
// initial chrome — no point polling before the user can even see the
// player.
setTimeout(pollCacheStatus, 1200);

const tl = document.getElementById("tl");
const tlPlayed = document.getElementById("tl-played");
const tlCursor = document.getElementById("tl-cursor");
const tlIn = document.getElementById("tl-in");
const tlOut = document.getElementById("tl-out");
const tlRange = document.getElementById("tl-range");
const timeDisplay = document.getElementById("time-display");

let inPoint = null;
let outPoint = null;
const items = []; // {kind: "clip"|"still", ...}
let nextId = 1;
let activeItemId = null;
let preSnapshot = null; // pre-selection {inPoint, outPoint, currentTime}
let defaultQuality = "best";
// Markers (labelled bookmarks). Declared up here next to `items` so that
// any code path running before the marker section finishes evaluating
// — _refreshPanBar, refreshTimeline, anything called during script
// init — can safely read them. `let` declarations have a temporal dead
// zone; even `typeof markers` would throw if accessed before the let
// line evaluated. Hoisting these to module top defuses that.
let markers = [];
let _markerNextId = 1;

// Quality options for new clips/stills — kept in sync with the main-page
// card's quality dropdown so the editor's default matches whatever the
// user picked when starting the rip. "best" = no scaling (output at
// source resolution); numeric heights downscale via ffmpeg.
const QUALITY_OPTIONS = [
  ["best", "Best available"],
  ["2160", "2160p"],
  ["1440", "1440p"],
  ["1080", "1080p"],
  ["720",  "720p"],
  ["480",  "480p"],
  ["360",  "360p"],
];
function qualityLabel(v) {
  for (const [val, lbl] of QUALITY_OPTIONS) if (val === v) return lbl;
  return v;
}
// Legacy compat: pre-0.1 sessions saved items with quality:"source", which
// is functionally identical to "best" (both map to None in the backend's
// _quality_to_height). Normalize at every read site so old saved sessions
// render the dropdown selection correctly without a migration step.
function normalizeQuality(q) {
  if (!q || q === "source") return "best";
  return q;
}

function fmtTime(s) {
  if (!isFinite(s)) return "--:--.---";
  const m = Math.floor(s / 60);
  const sec = s - m * 60;
  return String(m).padStart(2, "0") + ":" + sec.toFixed(3).padStart(6, "0");
}
// Compact MM:SS form for filenames — no millis, no leading zero on minutes
// past 9. Used for default clip / still names so files become
// self-documenting on disk: "Lilly podcast - clip 02:30-03:15.mp4".
function fmtClipStamp(s) {
  if (!isFinite(s) || s < 0) s = 0;
  const total = Math.round(s);
  const m = Math.floor(total / 60);
  const sec = total - m * 60;
  return String(m).padStart(2, "0") + ":" + String(sec).padStart(2, "0");
}

function effectiveDuration() {
  const d = player.duration;
  if (isFinite(d) && d > 0) return d;
  return DURATION || 0;
}

// Viewport state. When the user zooms in, [start, end] narrows; the rest
// of the timeline code calls `timeToPercent(t)` / `percentToTime(p)`
// instead of dividing by duration directly, so adding zoom is a single
// point of truth. `_zoom = 1` is fit-all; higher values show a smaller
// window centered on `_zoomCenter`.
let _zoom = 1;
let _zoomCenter = 0;        // seconds, recentered on cursor when zooming
function vpRange() {
  const d = effectiveDuration();
  if (d <= 0) return [0, 0];
  if (_zoom <= 1.0001) return [0, d];
  const len = d / _zoom;
  let s = _zoomCenter - len / 2;
  let e = _zoomCenter + len / 2;
  if (s < 0)   { e -= s; s = 0; }
  if (e > d)   { s -= (e - d); e = d; }
  s = Math.max(0, s);
  return [s, e];
}
function timeToPercent(t) {
  const [a, b] = vpRange();
  const len = b - a;
  if (len <= 0) return 0;
  return ((t - a) / len) * 100;
}
function percentToTime(p) {
  const [a, b] = vpRange();
  return a + (p / 100) * (b - a);
}
function timeInView(t) {
  const [a, b] = vpRange();
  return t >= a - 0.0005 && t <= b + 0.0005;
}

function refreshTimeline() {
  const d = effectiveDuration();
  if (d <= 0) return;
  const cur = player.currentTime || 0;
  const curPct = timeToPercent(cur);
  tlPlayed.style.width = Math.max(0, Math.min(100, curPct)) + "%";
  tlCursor.style.left = Math.max(0, Math.min(100, curPct)) + "%";
  // Only show marks/range that fall within the current viewport.
  if (inPoint != null && timeInView(inPoint)) {
    tlIn.style.display = ""; tlIn.style.left = timeToPercent(inPoint) + "%";
  } else { tlIn.style.display = "none"; }
  if (outPoint != null && timeInView(outPoint)) {
    tlOut.style.display = ""; tlOut.style.left = timeToPercent(outPoint) + "%";
  } else { tlOut.style.display = "none"; }
  if (inPoint != null && outPoint != null && outPoint > inPoint) {
    const [a, b] = vpRange();
    const lo = Math.max(inPoint, a);
    const hi = Math.min(outPoint, b);
    if (hi > lo) {
      tlRange.style.display = "";
      tlRange.style.left = timeToPercent(lo) + "%";
      tlRange.style.width = (timeToPercent(hi) - timeToPercent(lo)) + "%";
    } else {
      tlRange.style.display = "none";
    }
  } else {
    tlRange.style.display = "none";
  }
  timeDisplay.textContent = fmtTime(cur) + " / " + fmtTime(d);
  renderClipBands();
  // Minimap window indicator tracks the same vpRange — keep it in sync
  // with every refresh so pan/zoom from any source (wheel, buttons,
  // keyboard) all update it. Cheap: just two style assignments + a
  // small DOM mirror of the items overlay.
  if (typeof _refreshPanBar === "function") _refreshPanBar();
  // Visual filmstrip pan: between rebuilds, CSS-translate the existing
  // frames to give the illusion of smooth motion that matches the
  // marks/playhead. After buildFilmstrip lands, the transform resets
  // and frames snap to their true positions.
  if (typeof _filmstripVisualPan === "function") _filmstripVisualPan();
  // Keyframe ticks + markers re-render with the viewport too.
  if (typeof renderKeyframes === "function") renderKeyframes();
  if (typeof renderMarkers === "function") renderMarkers();
}

// Render saved clips as orange bands and saved stills as green ticks
// across the timeline. Click a band/tick to load that item back into the
// player. Re-rendered every refreshTimeline call (cheap — a handful of
// divs at most) so zoom + viewport changes Just Work.
const tlBands = document.getElementById("tl-bands");
function renderClipBands() {
  if (!tlBands) return;
  const d = effectiveDuration();
  tlBands.innerHTML = "";
  if (d <= 0) return;
  const [a, b] = vpRange();
  for (const it of items) {
    if (it.kind === "clip") {
      const s = +it.start, e = +it.end;
      if (!isFinite(s) || !isFinite(e) || e <= s) continue;
      // Skip bands fully outside the current viewport.
      if (e < a || s > b) continue;
      const lo = Math.max(s, a);
      const hi = Math.min(e, b);
      const left = timeToPercent(lo);
      const width = timeToPercent(hi) - timeToPercent(lo);
      const div = document.createElement("div");
      div.className = "tl-band" + (it.id === activeItemId ? " active" : "");
      div.style.left = left + "%";
      div.style.width = width + "%";
      div.title = `${it.name || "clip"} · ${fmtTime(s)}–${fmtTime(e)} (double-click to edit)`;
      tlBands.appendChild(div);
    } else if (it.kind === "still") {
      const t = +it.t;
      if (!isFinite(t)) continue;
      if (t < a || t > b) continue;
      const div = document.createElement("div");
      div.className = "tl-tick" + (it.id === activeItemId ? " active" : "");
      div.style.left = timeToPercent(t) + "%";
      div.title = `${it.name || "still"} · ${fmtTime(t)} (double-click to edit)`;
      tlBands.appendChild(div);
    }
  }
}

player.addEventListener("timeupdate", refreshTimeline);
player.addEventListener("loadedmetadata", refreshTimeline);
player.addEventListener("durationchange", refreshTimeline);

// ─── Filmstrip — Premiere-style frame strip across the timeline ───────
// Once metadata lands, sample N evenly-spaced frames from the source
// (server-side ffmpeg via /editor/thumb, cached client-side via
// fetchHoverThumb) and paint them as the timeline background. Refreshes
// on window resize so the frame count keeps pace with the visible width.
const tlFilmstrip = document.getElementById("tl-filmstrip");
let _filmstripBuildToken = 0;     // generation counter — abort stale builds
let _filmstripBuiltN = 0;          // frame count of the most recent build
let _filmstripResizeTimer = null;

function _frameCountForWidth(w) {
  // Aim for ~80px per thumbnail. Floor at 8 (so short windows still have
  // visible content) and ceiling at 40 (so 1500px+ timelines don't fan
  // out into hundreds of tiny fetches).
  return Math.max(8, Math.min(40, Math.round((w || 800) / 80)));
}

// Retry handle — filmstrip can be asked to build before the video has
// reported its duration (first open + cache cold). Self-rearms every
// 500ms until duration > 0, then bows out. Capped at ~30s of total
// retry so a permanently-broken source doesn't poll forever.
let _filmstripRetryTimer = null;
let _filmstripRetryStart = 0;
// vpRange snapshot at the time of the last successful build. Used by
// _filmstripVisualPan to compute the live pixel offset between where
// the existing frames "really represent" and where the user has now
// panned to. Cleared on rebuild → frames snap to true positions.
let _filmstripBaseRange = null;

function _filmstripVisualPan() {
  // Smooth visual pan during continuous scrolling. The actual frame
  // rebuild is debounced (120ms after motion stops) — without this, the
  // filmstrip appears frozen while marks/cursor/bands slide, which
  // reads as broken even though it's just a debounce. Solution: as the
  // viewport shifts, translateX the filmstrip in lockstep so the user
  // sees content moving with their scroll, then on rebuild clear the
  // transform and let real frames take over.
  if (!tlFilmstrip || !_filmstripBaseRange) return;
  const [a, b]   = vpRange();
  const [ba, bb] = _filmstripBaseRange;
  const baseLen = bb - ba;
  if (baseLen <= 0) { tlFilmstrip.style.transform = ""; return; }
  // Only valid for *pan*, not zoom. If the viewport WIDTH changed
  // (zoom), bail so the transform doesn't stretch incorrectly — the
  // upcoming rebuild will fix it.
  if (Math.abs((b - a) - baseLen) > 0.001) {
    tlFilmstrip.style.transform = "";
    return;
  }
  const tlW = tl.clientWidth || 1;
  const px = ((a - ba) / baseLen) * tlW;
  tlFilmstrip.style.transform = `translateX(${(-px).toFixed(2)}px)`;
}

async function buildFilmstrip(force) {
  const d = effectiveDuration();
  if (!tlFilmstrip) return;
  if (d <= 0) {
    if (!_filmstripRetryStart) _filmstripRetryStart = Date.now();
    if (Date.now() - _filmstripRetryStart > 30000) return;
    if (_filmstripRetryTimer) clearTimeout(_filmstripRetryTimer);
    _filmstripRetryTimer = setTimeout(() => buildFilmstrip(force), 500);
    return;
  }
  // Duration showed up — clear retry state.
  if (_filmstripRetryTimer) { clearTimeout(_filmstripRetryTimer); _filmstripRetryTimer = null; }
  _filmstripRetryStart = 0;
  const tlW = tl.clientWidth || 800;
  const N = _frameCountForWidth(tlW);
  const myToken = ++_filmstripBuildToken;
  _filmstripBuiltN = N;
  const [vpA, vpB] = vpRange();
  const cells = [];
  // In-place update: if the DOM already has the right number of frames
  // (typical pan / zoom delta), reuse the existing divs. We just rewrite
  // their backgroundImage as new fetches resolve. No shimmer flash, no
  // layout reflow — much smoother during continuous panning. We do NOT
  // reset .loading either; the existing image stays visible until the
  // new one arrives, so the user sees content the whole time.
  if (tlFilmstrip.children.length === N && !force) {
    const divs = Array.from(tlFilmstrip.children);
    for (let i = 0; i < N; i++) {
      cells.push({ div: divs[i], t: vpA + ((i + 0.5) / N) * (vpB - vpA) });
    }
  } else {
    tlFilmstrip.innerHTML = "";
    for (let i = 0; i < N; i++) {
      const div = document.createElement("div");
      div.className = "tl-frame loading";
      tlFilmstrip.appendChild(div);
      cells.push({ div, t: vpA + ((i + 0.5) / N) * (vpB - vpA) });
    }
  }
  // Frames are now positioned for the *current* viewport. Snap any
  // visual-pan transform off and snapshot this range so subsequent
  // pans translate from a correct baseline.
  tlFilmstrip.style.transform = "";
  _filmstripBaseRange = [vpA, vpB];
  // Concurrency-capped pool — 4 in flight is the sweet spot for the
  // local ffmpeg pipeline (CPU-bound, but I/O on the cached source is
  // negligible). Higher counts don't speed it up and starve hover thumbs.
  let cursor = 0;
  const work = async () => {
    while (cursor < cells.length) {
      const idx = cursor++;
      const { div, t } = cells[idx];
      try {
        // Use the no-abort twin — fetchHoverThumb cancels prior in-flight
        // requests via a shared AbortController, which would mean only
        // the last of our N concurrent fetches survives.
        const url = await fetchFilmstripThumb(t);
        // The build was superseded by a newer one (resize, source swap)
        // — abandon this token quietly.
        if (myToken !== _filmstripBuildToken) return;
        if (url) {
          div.style.backgroundImage = `url("${url}")`;
          div.classList.remove("loading");
        }
      } catch (e) { /* ignore single-frame failures */ }
    }
  };
  await Promise.all([work(), work(), work(), work()]);
}

player.addEventListener("loadedmetadata", () => buildFilmstrip(true));
player.addEventListener("durationchange",  () => buildFilmstrip(true));
// Extra safety nets for first-open: HLS sometimes fires durationchange
// before metadata, sometimes the other way around, sometimes neither
// fires until a `loadeddata` after the first segment lands. Cover all.
player.addEventListener("loadeddata",      () => buildFilmstrip(true));
player.addEventListener("canplay",         () => buildFilmstrip(true));

// Debounced wrapper for callers that fire continuously (pan drag, wheel
// zoom). Coalesces a flurry of "rebuild!" requests into one rebuild
// after the motion stops for ~120ms. Critical for filmstrip smoothness
// during panning — without this, every wheel tick or pan-drag mousemove
// would queue a fresh fetch storm.
let _filmstripDebounceTimer = null;
function buildFilmstripSoon(ms) {
  if (_filmstripDebounceTimer) clearTimeout(_filmstripDebounceTimer);
  _filmstripDebounceTimer = setTimeout(() => {
    _filmstripDebounceTimer = null;
    buildFilmstrip(false);
  }, ms || 120);
}

window.addEventListener("resize", () => {
  if (_filmstripResizeTimer) clearTimeout(_filmstripResizeTimer);
  // 300ms debounce: rebuilds while the user is mid-drag-resize would
  // thrash the filmstrip and waste fetches.
  _filmstripResizeTimer = setTimeout(() => buildFilmstrip(false), 300);
});

// Click + drag scrub. mousedown seeks immediately; while held, mousemove
// at the document level keeps seeking so the user can drag past the
// timeline edges without losing capture.
let scrubbing = false;
function _scrubTo(clientX) {
  const rect = tl.getBoundingClientRect();
  const x = Math.max(0, Math.min(rect.width, clientX - rect.left)) / rect.width;
  const d = effectiveDuration();
  if (d <= 0) return;
  // Viewport-aware: when zoomed in, x=0..1 maps to vpRange()[0]..[1],
  // not 0..duration.
  const t = percentToTime(x * 100);
  // Any user-initiated scrub cancels an in-flight clip / concat preview —
  // they wanted to look at a different frame, not bounce back.
  if (typeof _previewing !== "undefined") _previewing = false;
  if (typeof _concatPreview !== "undefined") _concatPreview = null;
  // Suppress the loop wrap for the next 500ms so the scrub lands.
  if (typeof markUserSeek === "function") markUserSeek();
  player.currentTime = Math.max(0, Math.min(d - 0.001, t));
}
// Double-click anywhere on the filmstrip: if there's a saved clip whose
// range covers that point (or a still within an 8px tolerance), load
// that item into the editor. Lets the user open an item for editing
// without having to scroll the items table to find it. Single click
// continues to scrub — only dblclick triggers the load.
tl.addEventListener("dblclick", (e) => {
  if (e.target.closest && e.target.closest(".tl-mark")) return;
  const rect = tl.getBoundingClientRect();
  const d = effectiveDuration();
  if (d <= 0) return;
  const t = percentToTime(((e.clientX - rect.left) / rect.width) * 100);
  const [vpA, vpB] = vpRange();
  const tlW = tl.clientWidth || 1;
  const stillTol = (8 / tlW) * (vpB - vpA);  // 8px in current zoom
  let pick = null;
  for (const it of items) {
    if (it.kind === "clip") {
      if (t >= it.start && t <= it.end) {
        // Prefer the smallest-spanning clip when overlaps exist — picks
        // the most specific match.
        if (!pick || pick.kind !== "clip" ||
            (it.end - it.start) < (pick.end - pick.start)) {
          pick = it;
        }
      }
    } else if (it.kind === "still") {
      if (Math.abs(t - it.t) < stillTol && !pick) pick = it;
    }
  }
  if (pick) {
    e.preventDefault(); e.stopPropagation();
    loadItem(pick);
  }
});

tl.addEventListener("mousedown", (e) => {
  // Don't initiate a scrub when the click is actually starting a mark
  // drag — those handlers stop propagation, but on browsers that fire
  // mousedown on the parent first we double-check the target.
  if (e.target.closest && e.target.closest(".tl-mark")) return;
  scrubbing = true;
  tl.classList.add("scrubbing");
  tl.focus();
  _scrubTo(e.clientX);
  e.preventDefault();
  // Show a hover preview bubble locked to the scrub position. Stays
  // visible for the whole drag even if the user wanders off the timeline
  // vertically — it's keyed off `scrubbing`, not mouse-over-timeline.
  _showScrubPreview();
});
// RAF-throttle: mousemove fires at ~120Hz on macOS; coalesce to ~60Hz.
// We always remember the latest X and apply it on the next frame.
let _scrubRafPending = false;
let _scrubRafX = 0;
document.addEventListener("mousemove", (e) => {
  if (!scrubbing) return;
  _scrubRafX = e.clientX;
  if (_scrubRafPending) return;
  _scrubRafPending = true;
  requestAnimationFrame(() => {
    _scrubRafPending = false;
    if (scrubbing) {
      _scrubTo(_scrubRafX);
      _showScrubPreview();
    }
  });
});
document.addEventListener("mouseup", () => {
  if (scrubbing) {
    scrubbing = false;
    tl.classList.remove("scrubbing");
    // Hide the bubble unless the mouse settled back on top of the
    // timeline — in that case the regular hover handler will manage it.
    if (!tl.matches(":hover")) hoverThumb.style.display = "none";
  }
});

// Render the hover bubble pinned to the playhead during a scrub. Updates
// every RAF tick so the user sees the *frame they're scrubbing to* in
// near-real-time, not a stale frame from when they let go. Bypasses the
// 15ms hover debounce on purpose — scrub callers fire at most ~60Hz, the
// per-bucket cache absorbs repeats.
function _showScrubPreview() {
  const d = effectiveDuration();
  if (d <= 0) return;
  const rect = tl.getBoundingClientRect();
  const t = player.currentTime || 0;
  const cursorX = rect.left + (timeToPercent(t) / 100) * rect.width;
  const tw = 160, th = 90;
  let left = cursorX - tw / 2;
  let top = rect.top - th - 8;
  if (top < 4) top = rect.bottom + 8;
  left = Math.max(4, Math.min(window.innerWidth - tw - 4, left));
  hoverThumb.style.display = "block";
  hoverThumb.style.left = left + "px";
  hoverThumb.style.top = top + "px";
  hoverTime.textContent = fmtTime(t);
  fetchHoverThumb(t).then(url => {
    // The user may have stopped scrubbing by the time the fetch returns —
    // only paint if the bubble is still meant to be visible.
    if (url && hoverThumb.style.display === "block") {
      hoverThumb.style.backgroundImage = `url("${url}")`;
    }
  });
}

// Frame capture. The main player is the only video element that's
// guaranteed to have its decoder warm (the offscreen prev-video stays
// black on WebKit if it's never visible / never play()'d). For thumbs,
// we briefly hijack the main player: pause, seek, capture, restore.
// For hover preview we keep prev-video — it's nice-to-have, and the
// rapid seeks would be jarring on the main player.
function _drawTo(video, maxW) {
  const w = video.videoWidth, h = video.videoHeight;
  if (!w || !h) return "";
  const scale = Math.min(1, (maxW || 160) / w);
  thumbCanvas.width = Math.max(1, Math.round(w * scale));
  thumbCanvas.height = Math.max(1, Math.round(h * scale));
  try {
    thumbCanvas.getContext("2d").drawImage(
      video, 0, 0, thumbCanvas.width, thumbCanvas.height);
    return thumbCanvas.toDataURL("image/jpeg", 0.7);
  } catch (e) { return ""; }
}

function _seekAndDraw(video, t, maxW) {
  return new Promise((resolve) => {
    const target = Math.max(0, t || 0);
    let done = false;
    let timeoutId = null;
    const finish = (val) => {
      if (done) return; done = true;
      if (timeoutId) clearTimeout(timeoutId);
      try { video.removeEventListener("seeked", onSeeked); } catch (e) {}
      resolve(val);
    };
    const draw = () => finish(_drawTo(video, maxW));
    // After the seek event we wait for the new frame to actually be
    // rendered to the video element. Strategy:
    //   1. requestVideoFrameCallback fires when the compositor presents
    //      a new frame — fastest when it works.
    //   2. WebKit (Safari) sometimes doesn't fire rVFC on paused seeks,
    //      so we backup with a setTimeout that draws regardless.
    //   3. Each path uses a "fired" flag so only one drawTo runs.
    const onSeeked = () => {
      let drew = false;
      const tryDraw = () => { if (drew) return; drew = true; draw(); };
      if (typeof video.requestVideoFrameCallback === "function") {
        video.requestVideoFrameCallback(tryDraw);
      }
      // Backup: 80ms after the seek event, force-draw whatever's painted.
      // 80ms is enough for WebKit to paint a freshly-seeked frame on most
      // hardware; tighter than the full 4s timeout below.
      setTimeout(tryDraw, 80);
    };
    timeoutId = setTimeout(() => finish(""), 4000);
    video.addEventListener("seeked", onSeeked, { once: true });
    try {
      const cur = video.currentTime;
      if (Math.abs(cur - target) < 0.001) {
        // We're already there — paint after one frame so the video
        // element has a guaranteed rendered frame to draw from.
        requestAnimationFrame(() => requestAnimationFrame(draw));
      } else {
        video.currentTime = target;
      }
    } catch (e) { finish(""); }
  });
}

// Item thumbnails: ask the server for a JPEG frame at time t, generated by
// ffmpeg from the editor's cached source. Way more reliable than canvas
// drawImage of the live <video>, which on WebKit often returns black for
// hardware-decoded frames (the visible video surface isn't readable).
//
// On failure we render the error message into the tile itself so the user
// can see why without devtools.
function _errorThumbDataURI(msg) {
  thumbCanvas.width = 200; thumbCanvas.height = 100;
  const ctx = thumbCanvas.getContext("2d");
  ctx.fillStyle = "#330000"; ctx.fillRect(0, 0, 200, 100);
  ctx.fillStyle = "#ffd0d0"; ctx.font = "9px monospace";
  ctx.fillText("THUMB FAIL", 6, 14);
  ctx.fillStyle = "#fff";
  const words = String(msg || "").match(/.{1,28}/g) || [];
  for (let i = 0; i < Math.min(6, words.length); i++) {
    ctx.fillText(words[i], 6, 30 + i * 12);
  }
  try { return thumbCanvas.toDataURL("image/jpeg", 0.6); }
  catch (e) { return ""; }
}
let mainBusy = Promise.resolve();
function captureFromMain(t, maxW) {
  mainBusy = mainBusy.then(async () => {
    try {
      const w = Math.max(40, Math.min(600, maxW || 200));
      const url = `/editor/thumb?sid=${encodeURIComponent(SID)}&t=${Math.max(0, t || 0).toFixed(3)}&w=${w}`;
      const r = await fetch(url);
      if (!r.ok) {
        let detail = "";
        try { detail = (await r.json()).error || ""; } catch (e) {}
        return _errorThumbDataURI(`HTTP ${r.status} ${detail}`);
      }
      const blob = await r.blob();
      if (!blob.size) return _errorThumbDataURI("empty body");
      const dataUri = await new Promise((resolve) => {
        const fr = new FileReader();
        fr.onload = () => resolve(String(fr.result || ""));
        fr.onerror = () => resolve("");
        fr.readAsDataURL(blob);
      });
      return dataUri || _errorThumbDataURI("read failed");
    } catch (e) {
      return _errorThumbDataURI(String(e.message || e));
    }
  });
  return mainBusy;
}

// Hover preview: prev-video is fine for this — black thumbnail just
// means no preview, but the bubble still shows the time.
let prevBusy = Promise.resolve();
function captureFromPrev(t, maxW) {
  prevBusy = prevBusy.then(() => _seekAndDraw(prevVideo, t, maxW));
  return prevBusy;
}

// Hover preview — fetched from /editor/thumb (server-side ffmpeg). With
// per-bucket caching, repeated hovers in the same area are instant. The
// first hover at a new position has roughly the same latency as a
// network round-trip + a single ffmpeg keyframe extraction (~50-150ms).
const hoverCache = new Map();        // bucket-key → dataURI
const HOVER_BUCKET = 0.5;            // seconds — coarser = more cache hits
const HOVER_CACHE_MAX = 240;
let hoverInflight = null;            // AbortController for current fetch
let hoverPending = null;
async function fetchHoverThumb(t) {
  const bucket = Math.round(t / HOVER_BUCKET) * HOVER_BUCKET;
  const key = bucket.toFixed(2);
  if (hoverCache.has(key)) return hoverCache.get(key);
  if (hoverInflight) hoverInflight.abort();
  hoverInflight = new AbortController();
  try {
    const r = await fetch(
      `/editor/thumb?sid=${encodeURIComponent(SID)}&t=${bucket.toFixed(3)}&w=240`,
      { signal: hoverInflight.signal }
    );
    if (!r.ok) return "";
    const blob = await r.blob();
    if (!blob.size) return "";
    const dataUri = await new Promise((resolve) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result || ""));
      fr.onerror = () => resolve("");
      fr.readAsDataURL(blob);
    });
    hoverCache.set(key, dataUri);
    if (hoverCache.size > HOVER_CACHE_MAX) {
      // Drop the oldest entry — Map preserves insertion order.
      const first = hoverCache.keys().next().value;
      hoverCache.delete(first);
    }
    return dataUri;
  } catch (e) { return ""; }
}
// Twin of fetchHoverThumb, but WITHOUT the shared-AbortController cancel
// behaviour. The hover bubble wants "newer mouse pos cancels older fetch"
// — perfect for a single tracked target. The filmstrip fires N concurrent
// fetches that must all complete; sharing the abort controller would
// have each new fetch cancel the previous one (only the last frame would
// ever paint). Cache writes are shared so hover hits land instantly on
// any bucket the filmstrip already warmed.
async function fetchFilmstripThumb(t) {
  const bucket = Math.round(t / HOVER_BUCKET) * HOVER_BUCKET;
  const key = bucket.toFixed(2);
  if (hoverCache.has(key)) return hoverCache.get(key);
  try {
    const r = await fetch(
      `/editor/thumb?sid=${encodeURIComponent(SID)}&t=${bucket.toFixed(3)}&w=240`
    );
    if (!r.ok) return "";
    const blob = await r.blob();
    if (!blob.size) return "";
    const dataUri = await new Promise((resolve) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result || ""));
      fr.onerror = () => resolve("");
      fr.readAsDataURL(blob);
    });
    hoverCache.set(key, dataUri);
    if (hoverCache.size > HOVER_CACHE_MAX) {
      const first = hoverCache.keys().next().value;
      hoverCache.delete(first);
    }
    return dataUri;
  } catch (e) { return ""; }
}
function onTimelineHover(e) {
  const rect = tl.getBoundingClientRect();
  const x = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
  const d = effectiveDuration();
  if (d <= 0) return;
  // Viewport-aware: hover at the same pixel maps to different times
  // depending on zoom level.
  const t = percentToTime((x / rect.width) * 100);
  const tw = 160, th = 90;
  let left = e.clientX - tw / 2;
  let top = rect.top - th - 8;
  if (top < 4) top = rect.bottom + 8;
  left = Math.max(4, Math.min(window.innerWidth - tw - 4, left));
  hoverThumb.style.display = "block";
  hoverThumb.style.left = left + "px";
  hoverThumb.style.top = top + "px";
  hoverTime.textContent = fmtTime(t);
  // Tight debounce (15ms) — feels instant once cached and never spams the
  // server during fast mouse moves.
  if (hoverPending) clearTimeout(hoverPending);
  hoverPending = setTimeout(async () => {
    const url = await fetchHoverThumb(t);
    if (url && hoverThumb.style.display === "block") {
      hoverThumb.style.backgroundImage = `url("${url}")`;
    }
  }, 15);
}
tl.addEventListener("mousemove", onTimelineHover);
tl.addEventListener("mouseleave", () => {
  // Scrub mode owns the bubble visibility — see _showScrubPreview. Don't
  // hide here while the user is dragging off the timeline mid-drag.
  if (scrubbing) return;
  hoverThumb.style.display = "none";
});

// ─── Custom transport controls ────────────────────────────────────────
const playBtn = document.getElementById("btn-play");
const muteBtn = document.getElementById("btn-mute");
const volEl = document.getElementById("vol");
const loopBtn = document.getElementById("btn-loop");
let loopEnabled = true;  // default ON: when both marks exist, loop within them
function refreshPlayBtn() {
  playBtn.textContent = player.paused ? "▶" : "⏸";
}
function refreshMuteBtn() {
  muteBtn.textContent = (player.muted || player.volume === 0) ? "🔇" : "🔊";
}
function refreshLoopBtn() {
  loopBtn.classList.toggle("on", loopEnabled);
}
player.addEventListener("play", refreshPlayBtn);
player.addEventListener("pause", refreshPlayBtn);
player.addEventListener("volumechange", refreshMuteBtn);
playBtn.addEventListener("click", () => {
  if (player.paused) {
    // Only snap to In-mark when LOOP is engaged — that's the user
    // explicitly saying "I want to play this clip". Without Loop, plain
    // play should always start from the playhead position regardless of
    // any active in/out marks. (Earlier behavior snapped on every play
    // and made scrubbing-then-play unusable.)
    if (loopEnabled && inPoint != null && outPoint != null &&
        (player.currentTime < inPoint - 0.05 || player.currentTime >= outPoint - 0.05)) {
      player.currentTime = inPoint;
    }
    player.play();
  } else {
    player.pause();
  }
});
muteBtn.addEventListener("click", () => {
  player.muted = !player.muted;
  refreshMuteBtn();
});
volEl.addEventListener("input", () => {
  const v = parseFloat(volEl.value);
  player.volume = isFinite(v) ? Math.max(0, Math.min(1, v)) : 1;
  if (v > 0 && player.muted) player.muted = false;
  refreshMuteBtn();
});
loopBtn.addEventListener("click", () => {
  loopEnabled = !loopEnabled;
  refreshLoopBtn();
});

// Speed control. We keep the actual rate steps in this list so `,` and
// `.` keys can step through them in order. 1.0 always lives at the
// middle index so consecutive speed-down keys land cleanly back at 1×.
const SPEED_STEPS = [0.25, 0.5, 1.0, 1.25, 1.5, 2.0];
const speedEl = document.getElementById("speed");
function setSpeed(rate) {
  rate = +rate || 1.0;
  // Snap to nearest step so the dropdown always reflects an option.
  let nearest = SPEED_STEPS[0];
  for (const s of SPEED_STEPS) {
    if (Math.abs(s - rate) < Math.abs(nearest - rate)) nearest = s;
  }
  player.playbackRate = nearest;
  speedEl.value = String(nearest);
}
function nudgeSpeed(dir) {
  const cur = +player.playbackRate || 1.0;
  let idx = SPEED_STEPS.findIndex(s => Math.abs(s - cur) < 0.01);
  if (idx < 0) idx = SPEED_STEPS.indexOf(1.0);
  idx = Math.max(0, Math.min(SPEED_STEPS.length - 1, idx + dir));
  setSpeed(SPEED_STEPS[idx]);
}
speedEl.addEventListener("change", () => setSpeed(speedEl.value));

// ─── Crop ──────────────────────────────────────────────────────────────
// Session-level crop: applied to all *new* clips/stills until reset.
// Per-item crop snapshots this on creation, so changing the session
// crop later doesn't retroactively edit existing items. The user can
// also override any single item via its row's "Crop…" button.
//
// Shape: { w, h, x, y, src_w, src_h } in source pixels, or null.
let sessionCrop = null;
const cropReadout = document.getElementById("crop-readout");
function _cropDescribe(c) {
  if (!c) return "";
  return `${c.w}×${c.h} @ ${c.x},${c.y}`;
}
// Returns whichever crop the user is currently looking at:
//   - If an item is loaded → that item's crop
//   - Else if multiple items are checkbox-selected → first selected's
//     crop (the others may differ; this is the "preview" value for the
//     overlay when entering edit mode)
//   - Otherwise → sessionCrop (the default for new captures)
// Writes go through the corresponding setter so the right scope mutates.
function _activeItem() {
  return (typeof activeItemId !== "undefined" && activeItemId != null)
    ? items.find(x => x.id === activeItemId) : null;
}
function _selectedItems() {
  // Items currently checkbox-selected. Used for batch operations like
  // "apply this crop to all selected".
  if (typeof selectedIds === "undefined") return [];
  return items.filter(it => selectedIds.has(it.id));
}
function getEffectiveCrop() {
  const it = _activeItem();
  if (it) return it.crop || null;
  const sel = _selectedItems();
  if (sel.length >= 1) return sel[0].crop || null;
  return sessionCrop;
}
function setEffectiveCrop(c) {
  const it = _activeItem();
  if (it) {
    it.crop = c;
    persistItems();
    renderItems();
    return;
  }
  // Batch path: when multiple items are checkbox-selected (and no
  // single item is loaded), apply the crop to ALL of them. Concat
  // items propagate the crop down to every segment.
  const sel = _selectedItems();
  if (sel.length >= 1) {
    pushUndo();
    for (const s of sel) {
      if (s.kind === "concat" && Array.isArray(s.segments)) {
        for (const seg of s.segments) seg.crop = c;
      } else {
        s.crop = c;
      }
    }
    persistItems();
    renderItems();
    return;
  }
  sessionCrop = c;
}
function refreshCropReadout() {
  if (!cropReadout) return;
  const it = _activeItem();
  const sel = _selectedItems();
  const crop = getEffectiveCrop();
  let label, color;
  if (it) {
    label = "Item: ";
    color = "#000080";
  } else if (sel.length >= 1) {
    label = `${sel.length} selected: `;
    color = "#000080";
  } else {
    label = "Auto: ";
    color = "#803a00";
  }
  if (crop) {
    cropReadout.textContent = label + _cropDescribe(crop);
    cropReadout.style.color = color;
  } else {
    if (it) {
      cropReadout.textContent = "(no crop on this item)";
    } else if (sel.length >= 1) {
      cropReadout.textContent = `(no crop on ${sel.length} selected)`;
    } else {
      cropReadout.textContent = "";
    }
    cropReadout.style.color = "#404040";
  }
}
document.getElementById("btn-crop-detect").addEventListener("click", async () => {
  const btn = document.getElementById("btn-crop-detect");
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "Detecting…";
  try {
    const r = await fetch(`/editor/detect-crop?sid=${encodeURIComponent(SID)}`);
    const j = await r.json();
    if (!r.ok) { alert(j.error || "detect failed"); return; }
    if (!j.detected) {
      // Routes to active item if loaded, else session crop.
      setEffectiveCrop(null);
      refreshCropReadout();
      refreshCropGhost();
      alert("No black bars detected — frame already fills source dimensions.");
      return;
    }
    setEffectiveCrop({ w: j.w, h: j.h, x: j.x, y: j.y,
                       src_w: j.src_w, src_h: j.src_h });
    refreshCropReadout();
    refreshCropGhost();
  } catch (e) {
    alert("Crop detect failed: " + (e.message || e));
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
});
document.getElementById("btn-crop-reset").addEventListener("click", () => {
  // Reset clears whichever crop is currently in effect — the loaded
  // item's, or the session default if no item is loaded.
  setEffectiveCrop(null);
  refreshCropReadout();
  refreshCropGhost();
});

// Aspect-ratio presets — instantly compute a centered crop rect at the
// chosen ratio relative to the source dimensions. Routes through
// setEffectiveCrop so it lands on the active item if one's loaded.
document.getElementById("crop-aspect").addEventListener("change", (e) => {
  const choice = e.target.value;
  e.target.value = "";  // reset to "Aspect…" placeholder for re-pick
  if (!choice) return;
  const sw = player.videoWidth || 0;
  const sh = player.videoHeight || 0;
  if (!sw || !sh) {
    alert("Video dimensions not available yet — wait for the player to load and try again.");
    return;
  }
  // Parse "W:H" — supports decimals (e.g. 2.39:1).
  const [aw, ah] = choice.split(":").map(parseFloat);
  if (!aw || !ah) return;
  const targetAR = aw / ah;
  const sourceAR = sw / sh;
  let cw, ch;
  if (targetAR > sourceAR) {
    // Wider than source → constrained by width, crop top + bottom.
    cw = sw; ch = Math.round(sw / targetAR);
  } else {
    // Taller / square → constrained by height, crop left + right.
    ch = sh; cw = Math.round(sh * targetAR);
  }
  // Even integers (libx264 + yuv420p alignment).
  cw -= (cw % 2); ch -= (ch % 2);
  const cx = Math.round((sw - cw) / 2);
  const cy = Math.round((sh - ch) / 2);
  setEffectiveCrop({ x: cx, y: cy, w: cw, h: ch, src_w: sw, src_h: sh });
  refreshCropReadout();
  refreshCropGhost();
});

// ─── Live crop overlay on the player ──────────────────────────────────
// Toggled on by the "Custom…" button in the Crop fieldset. Renders a
// draggable rect directly over the <video> element so the user can play
// and scrub to verify the crop region against actual content. Apply
// commits to sessionCrop (which gets snapshotted onto every NEW clip
// or still at capture time). Cancel exits without changing.
//
// All saved coordinates live in SOURCE pixel space using the video's
// `videoWidth`/`videoHeight`. The displayed rect is mapped to/from the
// player's actual on-screen size each frame so it stays correctly
// positioned even if the player resizes / letterboxes.
const cropOverlay = document.getElementById("crop-overlay");
const coRect      = document.getElementById("co-rect");
const coDims      = document.getElementById("co-dims");
let _coState = null;            // {x,y,w,h} in source pixels, or null
let _coDisplayed = null;        // {w,h} of the displayed video area

function _videoDisplayedRect() {
  // <video> letterboxes the source inside its CSS box. Compute the
  // actual painted region so the crop rect aligns visually with frame
  // content rather than the black bars added by object-fit: contain.
  const vw = player.videoWidth || 0;
  const vh = player.videoHeight || 0;
  const cw = player.clientWidth || 0;
  const ch = player.clientHeight || 0;
  if (!vw || !vh || !cw || !ch) {
    return { left: 0, top: 0, w: cw, h: ch, srcW: vw || cw, srcH: vh || ch };
  }
  const sourceAR = vw / vh, boxAR = cw / ch;
  let w, h;
  if (sourceAR > boxAR) {
    // Wider source — fits to box width, letterbox top/bottom.
    w = cw; h = cw / sourceAR;
  } else {
    h = ch; w = ch * sourceAR;
  }
  return {
    left: (cw - w) / 2,
    top:  (ch - h) / 2,
    w, h, srcW: vw, srcH: vh,
  };
}
function _coSyncRect() {
  // Editing mode uses _coState (live drag); view mode uses whatever
  // getEffectiveCrop() returns — that's the active item's crop when
  // one is loaded, sessionCrop otherwise. So the ghost on the player
  // always reflects "what would I export right now if I captured?".
  const editing = cropOverlay.classList.contains("show");
  const viewing = cropOverlay.classList.contains("view");
  if (!editing && !viewing) return;
  const crop = editing ? _coState : getEffectiveCrop();
  if (!crop) return;
  const d = _videoDisplayedRect();
  _coDisplayed = { w: d.w, h: d.h, srcW: d.srcW, srcH: d.srcH,
                   left: d.left, top: d.top };
  if (!d.srcW || !d.srcH) return;
  const sx = d.w / d.srcW, sy = d.h / d.srcH;
  coRect.style.left   = (d.left + crop.x * sx) + "px";
  coRect.style.top    = (d.top  + crop.y * sy) + "px";
  coRect.style.width  = (crop.w * sx) + "px";
  coRect.style.height = (crop.h * sy) + "px";
  if (editing) {
    coDims.textContent =
      `${Math.round(crop.w)}×${Math.round(crop.h)} ` +
      `@ ${Math.round(crop.x)},${Math.round(crop.y)}  ` +
      `(source ${d.srcW}×${d.srcH})`;
  }
}
function _coClamp() {
  if (!_coState || !_coDisplayed) return;
  const W = _coDisplayed.srcW, H = _coDisplayed.srcH;
  _coState.x = Math.max(0, Math.min(W - 4, _coState.x));
  _coState.y = Math.max(0, Math.min(H - 4, _coState.y));
  _coState.w = Math.max(4, Math.min(W - _coState.x, _coState.w));
  _coState.h = Math.max(4, Math.min(H - _coState.y, _coState.h));
}
function _coStartFromSession() {
  // Seed crop state from the effective crop (active item's, falling
  // back to session) or full-frame default. Source dimensions come
  // from the live video element; if metadata isn't ready yet we fall
  // back to the displayed pixel size.
  const d = _videoDisplayedRect();
  _coDisplayed = { w: d.w, h: d.h, srcW: d.srcW, srcH: d.srcH,
                   left: d.left, top: d.top };
  const seed = getEffectiveCrop();
  if (seed && seed.w && seed.h) {
    _coState = { x: seed.x, y: seed.y, w: seed.w, h: seed.h };
  } else {
    _coState = { x: 0, y: 0, w: d.srcW, h: d.srcH };
  }
  _coClamp(); _coSyncRect();
}

function openCropOverlay() {
  // Edit mode wins over view mode while open.
  cropOverlay.classList.remove("view");
  cropOverlay.classList.add("show");
  _coStartFromSession();
  player.addEventListener("loadedmetadata", _coStartFromSession);
}
function closeCropOverlay() {
  cropOverlay.classList.remove("show");
  player.removeEventListener("loadedmetadata", _coStartFromSession);
  _coState = null;
  // If a session crop is committed, drop into view mode so the user
  // continues to see the active crop region as a non-interactive ghost.
  // Reset / no-crop → fully hidden.
  refreshCropGhost();
}

// Promote / demote the persistent crop view mode based on the EFFECTIVE
// crop (active item's, or session). Called whenever the effective crop
// changes (Apply, Reset, Detect bars, item load, item deselect).
// Skips when we're actively editing.
function refreshCropGhost() {
  if (cropOverlay.classList.contains("show")) return;  // editing — leave alone
  if (getEffectiveCrop()) {
    cropOverlay.classList.add("view");
    _coSyncRect();
  } else {
    cropOverlay.classList.remove("view");
  }
}
// Re-sync the ghost on player resize so it tracks letterboxing changes.
window.addEventListener("resize", () => {
  if (cropOverlay.classList.contains("view")) _coSyncRect();
});
// And when the video first reports its dimensions (so the ghost can
// position correctly on initial load if a session crop was preloaded).
player.addEventListener("loadedmetadata", () => {
  if (cropOverlay.classList.contains("view")) _coSyncRect();
});

// Drag rect → translate. Drag handle → resize. Coordinates resolve via
// the displayed video rect → source pixels.
(function _wireCoDrag() {
  let mode = null;
  let startX = 0, startY = 0, startState = null;
  function down(e, m) {
    e.preventDefault(); e.stopPropagation();
    mode = m;
    startX = e.clientX; startY = e.clientY;
    startState = Object.assign({}, _coState);
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  }
  function move(e) {
    if (!mode || !_coDisplayed) return;
    const sx = _coDisplayed.srcW / _coDisplayed.w;
    const sy = _coDisplayed.srcH / _coDisplayed.h;
    const dx = (e.clientX - startX) * sx;
    const dy = (e.clientY - startY) * sy;
    const s = Object.assign({}, startState);
    if (mode === "move") {
      s.x += dx; s.y += dy;
    } else {
      if (mode.includes("e")) s.w += dx;
      if (mode.includes("w")) { s.x += dx; s.w -= dx; }
      if (mode.includes("s")) s.h += dy;
      if (mode.includes("n")) { s.y += dy; s.h -= dy; }
    }
    _coState = s; _coClamp(); _coSyncRect();
  }
  function up() {
    mode = null;
    document.removeEventListener("mousemove", move);
    document.removeEventListener("mouseup", up);
  }
  coRect.addEventListener("mousedown", e => {
    const h = e.target.dataset && e.target.dataset.handle;
    down(e, h || "move");
  });
  // Re-sync on player resize so the rect tracks letterboxing changes.
  window.addEventListener("resize", _coSyncRect);
})();

document.getElementById("btn-crop-custom").addEventListener("click", openCropOverlay);
document.getElementById("btn-crop-cancel").addEventListener("click", closeCropOverlay);
document.getElementById("btn-crop-apply").addEventListener("click", () => {
  if (!_coState || !_coDisplayed) return closeCropOverlay();
  const W = _coDisplayed.srcW, H = _coDisplayed.srcH;
  // No-op if the rect covers the full frame — clear instead.
  const full = (_coState.x < 1 && _coState.y < 1 &&
                Math.abs(_coState.w - W) < 2 &&
                Math.abs(_coState.h - H) < 2);
  // Routes to the active item's crop if loaded, else session crop.
  if (full) {
    setEffectiveCrop(null);
  } else {
    setEffectiveCrop({
      x: Math.round(_coState.x), y: Math.round(_coState.y),
      w: Math.round(_coState.w), h: Math.round(_coState.h),
      src_w: W, src_h: H,
    });
  }
  refreshCropReadout();
  // closeCropOverlay() drops edit mode and (if an effective crop exists)
  // flips back to persistent view mode automatically.
  closeCropOverlay();
});

// ─── Timeline zoom ─────────────────────────────────────────────────────
// _zoom and _zoomCenter live up next to the viewport helpers. Here we
// expose the controls: +/- buttons, a "Fit" reset, mouse-wheel pan when
// zoomed, Cmd+wheel to zoom. Anchors zoom on the cursor under the mouse
// (so the time the user is *looking at* stays in place visually).
const ZOOM_MIN = 1, ZOOM_MAX = 80;
const zoomReadout = document.getElementById("zoom-readout");
function _refreshZoomUI() {
  if (zoomReadout) zoomReadout.textContent =
    _zoom < 1.05 ? "1×" : _zoom.toFixed(_zoom < 10 ? 1 : 0) + "×";
  _refreshPanBar();
}

// Minimap — always visible under the filmstrip. The yellow window
// indicator's left/width track vpRange() over the full duration; at 1×
// zoom it spans the whole rail. Drag the window to pan; click the rail
// to recenter on that point; scroll-wheel over it to nudge ±10% of the
// current visible window. Saved-clip bands mirror onto the rail as
// faint orange bars + green ticks so coverage stays visible when zoomed
// in tight.
const tlMini = document.getElementById("tl-mini");
const tlMiniWindow = document.getElementById("tl-mini-window");
const tlMiniBands = document.getElementById("tl-mini-bands");
function _refreshPanBar() {
  if (!tlMini || !tlMiniWindow) return;
  const d = effectiveDuration();
  if (d <= 0) {
    tlMini.style.display = "none";
    return;
  }
  tlMini.style.display = "";
  const [a, b] = vpRange();
  const widthPct = Math.max(0.5, ((b - a) / d) * 100);
  const leftPct = (a / d) * 100;
  tlMiniWindow.style.left  = leftPct + "%";
  tlMiniWindow.style.width = widthPct + "%";
  // Mirror the items overlay so users can see saved-clip coverage even
  // when zoomed in tight on the main filmstrip.
  if (tlMiniBands) {
    tlMiniBands.innerHTML = "";
    for (const it of items) {
      if (it.kind === "clip") {
        const s = +it.start, e = +it.end;
        if (!isFinite(s) || !isFinite(e) || e <= s) continue;
        const div = document.createElement("div");
        div.className = "mb-band";
        div.style.left  = (s / d * 100) + "%";
        div.style.width = ((e - s) / d * 100) + "%";
        tlMiniBands.appendChild(div);
      } else if (it.kind === "still") {
        const t = +it.t;
        if (!isFinite(t)) continue;
        const div = document.createElement("div");
        div.className = "mb-tick";
        div.style.left = (t / d * 100) + "%";
        tlMiniBands.appendChild(div);
      }
    }
    // Markers (purple) — even when zoomed in tight, you can see the
    // overall "story" of bookmarks across the full duration.
    if (typeof markers !== "undefined" && Array.isArray(markers)) {
      for (const m of markers) {
        if (!isFinite(m.t)) continue;
        const div = document.createElement("div");
        div.className = "mb-marker";
        div.style.left = (m.t / d * 100) + "%";
        tlMiniBands.appendChild(div);
      }
    }
  }
}

// Drag the window indicator to pan. Click the rail to recenter on that
// point. Scroll-wheel inside the rail nudges by 10% of the visible
// window per tick (smooth-feeling without fighting OS-level scroll).
let _miniDrag = null;
tlMini.addEventListener("mousedown", (e) => {
  const d = effectiveDuration();
  if (d <= 0) return;
  const rect = tlMini.getBoundingClientRect();
  if (e.target !== tlMiniWindow) {
    // Click rail → recenter on click point. If we're at 1× this is a
    // no-op visually but harmless.
    const x = (e.clientX - rect.left) / rect.width;
    _zoomCenter = Math.max(0, Math.min(d, x * d));
    refreshTimeline(); buildFilmstripSoon(80);
    return;
  }
  e.preventDefault();
  _miniDrag = {
    startMouseX: e.clientX,
    startCenter: _zoomCenter,
    railWidth: rect.width,
    duration: d,
  };
});
document.addEventListener("mousemove", (e) => {
  if (!_miniDrag) return;
  const { startMouseX, startCenter, railWidth, duration } = _miniDrag;
  const dx = e.clientX - startMouseX;
  const dt = (dx / railWidth) * duration;
  _zoomCenter = Math.max(0, Math.min(duration, startCenter + dt));
  refreshTimeline();
});
document.addEventListener("mouseup", () => {
  if (_miniDrag) {
    _miniDrag = null;
    // Rebuild filmstrip once at drag-end (cheaper than per-move).
    buildFilmstripSoon(60);
  }
});
tlMini.addEventListener("wheel", (e) => {
  const d = effectiveDuration();
  if (d <= 0 || _zoom <= 1.001) return;
  e.preventDefault();
  // Continuous pan — each wheel pixel maps to one rail pixel of motion.
  // Way smoother than discrete 10% steps and respects the trackpad's
  // momentum scroll.
  const railW = tlMini.clientWidth || 1;
  const px = (e.deltaX !== 0 ? e.deltaX : e.deltaY);
  const dt = (px / railW) * d;
  _zoomCenter = Math.max(0, Math.min(d, _zoomCenter + dt));
  refreshTimeline();
  buildFilmstripSoon(120);
}, { passive: false });
function _applyZoom(targetZoom, anchorTime) {
  const d = effectiveDuration();
  if (d <= 0) return;
  const newZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, targetZoom));
  if (Math.abs(newZoom - _zoom) < 0.001 && newZoom > ZOOM_MIN) return;
  _zoom = newZoom;
  if (typeof anchorTime === "number" && isFinite(anchorTime)) {
    _zoomCenter = anchorTime;
  } else if (_zoomCenter <= 0 || _zoomCenter > d) {
    _zoomCenter = player.currentTime || d / 2;
  }
  _refreshZoomUI();
  refreshTimeline();
  // Filmstrip frame timestamps depend on viewport, so rebuild — but
  // debounced so rapid wheel-zoom doesn't fire a fetch storm.
  buildFilmstripSoon(120);
}
function zoomBy(factor, anchorClientX) {
  const d = effectiveDuration();
  if (d <= 0) return;
  const rect = tl.getBoundingClientRect();
  let anchorT;
  if (typeof anchorClientX === "number") {
    const x = Math.max(0, Math.min(rect.width, anchorClientX - rect.left));
    anchorT = percentToTime((x / rect.width) * 100);
  } else {
    anchorT = player.currentTime || _zoomCenter;
  }
  _applyZoom(_zoom * factor, anchorT);
}
function zoomFit() { _applyZoom(1, 0); }
document.getElementById("btn-zoom-in").addEventListener("click",  () => zoomBy(1.6));
document.getElementById("btn-zoom-out").addEventListener("click", () => zoomBy(1/1.6));
document.getElementById("btn-zoom-fit").addEventListener("click", zoomFit);

// Mouse wheel on the timeline:
//   - Cmd/Ctrl + wheel  → zoom around mouse cursor
//   - plain wheel       → pan when zoomed in (no-op when fit)
tl.addEventListener("wheel", (e) => {
  if (e.metaKey || e.ctrlKey) {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.18 : 1/1.18;
    zoomBy(factor, e.clientX);
    return;
  }
  if (_zoom <= 1.001) return;
  e.preventDefault();
  // Continuous pan tied to actual wheel/trackpad delta. 1 wheel pixel =
  // 1 timeline pixel — feels like dragging the strip directly under the
  // mouse, and the trackpad's natural momentum scroll just works.
  const tlW = tl.clientWidth || 1;
  const [a, b] = vpRange();
  const len = b - a;
  const px = (e.deltaX !== 0 ? e.deltaX : e.deltaY);
  const dt = (px / tlW) * len;
  _zoomCenter = Math.max(0, Math.min(effectiveDuration(),
                                     _zoomCenter + dt));
  refreshTimeline();
  buildFilmstripSoon(120);
}, { passive: false });

_refreshZoomUI();

refreshPlayBtn();
refreshMuteBtn();
refreshLoopBtn();

// User-seek grace: any deliberate seek (arrow keys, scrub, jump-to-mark,
// click-mark) sets _userSeekUntil = now + 500ms. While that's in the
// future, the loop / preview wrap-checks skip — so the user can step
// past outPoint with arrow keys without getting yanked back.
let _userSeekUntil = 0;
function markUserSeek(graceMs) {
  _userSeekUntil = performance.now() + (graceMs || 500);
}

// Loop wrap — fires on every painted frame via requestVideoFrameCallback
// (frame-accurate). Wraps when the current frame's media time is at or
// past outPoint, so the LAST frame the user sees is the one at outPoint
// — not the next one. Old timeupdate-based check overshot by 1-3 frames
// because timeupdate only fires every ~100-250ms.
//
// rVFC also delivers an EXACT mediaTime per frame, much better than
// reading player.currentTime which can lag the actual displayed frame.
const _hasRVFC = typeof player.requestVideoFrameCallback === "function";

function _shouldSuppressLoop() {
  // Skip wrap when paused, when the user just seeked, or when the
  // current playback obviously crossed outPoint via a manual jump (the
  // "natural crossing" check from the timeupdate fallback).
  if (player.paused) return true;
  if (performance.now() < _userSeekUntil) return true;
  return false;
}

if (_hasRVFC) {
  function _rvfcLoopCheck(now, meta) {
    // Loop wrap (in/out marks).
    if (loopEnabled && inPoint != null && outPoint != null
        && !_shouldSuppressLoop()
        && meta.mediaTime >= outPoint) {
      try { player.currentTime = inPoint; } catch (e) {}
    }
    // Concat preview — jump to next segment when the current one ends.
    // Uses the same per-frame mediaTime check, so segment boundaries
    // are frame-accurate (no overshoot).
    if (_concatPreview && !_shouldSuppressLoop() && !player.paused) {
      const cp = _concatPreview;
      const seg = cp.segments[cp.idx];
      if (seg && meta.mediaTime >= seg.end) {
        cp.idx += 1;
        if (cp.idx < cp.segments.length) {
          markUserSeek();
          try { player.currentTime = cp.segments[cp.idx].start; } catch (e) {}
        } else {
          _concatPreview = null;
          try { player.pause(); } catch (e) {}
        }
      }
    }
    player.requestVideoFrameCallback(_rvfcLoopCheck);
  }
  player.requestVideoFrameCallback(_rvfcLoopCheck);
} else {
  // Fallback for browsers without rVFC: timeupdate + natural-crossing.
  let _loopLastTime = 0;
  player.addEventListener("timeupdate", () => {
    const prev = _loopLastTime;
    const cur = player.currentTime;
    _loopLastTime = cur;
    if (!loopEnabled || inPoint == null || outPoint == null) return;
    if (_shouldSuppressLoop()) return;
    const naturalCross = (prev < outPoint &&
                          cur >= outPoint &&
                          (cur - prev) < 0.5);
    if (naturalCross) {
      try { player.currentTime = inPoint; } catch (e) {}
    }
  });
}

document.getElementById("btn-back").addEventListener("click", () => {
  markUserSeek();
  player.currentTime = Math.max(0, player.currentTime - 1 / 30);
});
document.getElementById("btn-fwd").addEventListener("click", () => {
  markUserSeek();
  player.currentTime = Math.min(effectiveDuration() - 0.001, player.currentTime + 1 / 30);
});
// Keyframe times of the source's video stream. Populated by the
// /editor/keyframes endpoint, used to snap mark times when the user
// has "snap KF" enabled. Empty array means "not loaded yet" — we treat
// that as no-snap.
let _keyframes = [];
let _snapToKeyframes = false;
const tlKeyframes = document.getElementById("tl-keyframes");

function snapKF(t) {
  if (!_snapToKeyframes || _keyframes.length === 0) return t;
  // Binary search for the nearest keyframe time.
  let lo = 0, hi = _keyframes.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (_keyframes[mid] < t) lo = mid + 1; else hi = mid;
  }
  const after = _keyframes[lo];
  const before = lo > 0 ? _keyframes[lo - 1] : after;
  return (Math.abs(t - before) <= Math.abs(after - t)) ? before : after;
}
function renderKeyframes() {
  if (!tlKeyframes) return;
  if (!_snapToKeyframes || _keyframes.length === 0) {
    tlKeyframes.classList.remove("show");
    return;
  }
  tlKeyframes.classList.add("show");
  // Render only KFs visible in the current viewport — at deep zoom on a
  // long video this can be hundreds of ticks otherwise.
  const [a, b] = vpRange();
  tlKeyframes.innerHTML = "";
  for (const t of _keyframes) {
    if (t < a || t > b) continue;
    const div = document.createElement("div");
    div.className = "tl-kf";
    div.style.left = timeToPercent(t) + "%";
    tlKeyframes.appendChild(div);
  }
}
async function _loadKeyframes() {
  try {
    const r = await fetch(`/editor/keyframes?sid=${encodeURIComponent(SID)}`);
    if (!r.ok) return;
    const j = await r.json();
    if (Array.isArray(j.times)) {
      // ffprobe may emit out-of-order — sort defensively for binary search.
      _keyframes = j.times.slice().sort((a, b) => a - b);
      renderKeyframes();
    }
  } catch (e) {}
}
document.getElementById("btn-snap-key").addEventListener("change", (e) => {
  _snapToKeyframes = !!e.target.checked;
  if (_snapToKeyframes && _keyframes.length === 0) _loadKeyframes();
  renderKeyframes();
});

// ───── Markers ─────────────────────────────────────────────────────────
// Labelled bookmarks. Press B at the playhead to drop a marker. They
// render as inverted purple triangles on the timeline (with a vertical
// drop-line through the filmstrip), small purple ticks on the minimap,
// and a list inside the collapsible Markers panel below the items table.
// They don't export — they're navigational only.
// (`markers` and `_markerNextId` are declared near the top of the
// script — see comment there.)
const tlMarkers = document.getElementById("tl-markers");
const markersPanel = document.getElementById("markers-panel");

function _markerSort() { markers.sort((a, b) => a.t - b.t); }
function persistMarkers() {
  // Piggyback onto persistItems by calling it — the items POST body
  // includes the markers array now.
  persistItems();
}
function addMarker(t, label) {
  if (!isFinite(t)) return;
  const m = { id: _markerNextId++, t, label: label || `Marker ${markers.length + 1}` };
  markers.push(m); _markerSort();
  renderMarkers(); persistMarkers();
}
function removeMarker(id) {
  const idx = markers.findIndex(m => m.id === id);
  if (idx < 0) return;
  markers.splice(idx, 1);
  renderMarkers(); persistMarkers();
}
function renameMarker(id, label) {
  const m = markers.find(m => m.id === id);
  if (!m) return;
  m.label = label;
  renderMarkers(); persistMarkers();
}
function renderMarkers() {
  // Triangles on the timeline (only those in current viewport).
  if (tlMarkers) {
    tlMarkers.innerHTML = "";
    const [a, b] = vpRange();
    for (const m of markers) {
      if (m.t < a || m.t > b) continue;
      const el = document.createElement("div");
      el.className = "tl-marker";
      el.style.left = timeToPercent(m.t) + "%";
      el.title = `${m.label} · ${fmtTime(m.t)} (click to jump · right-click to rename/delete)`;
      const lbl = document.createElement("div");
      lbl.className = "mk-label";
      lbl.textContent = m.label;
      el.appendChild(lbl);
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        markUserSeek();
        try { player.currentTime = m.t; } catch (e) {}
      });
      el.addEventListener("contextmenu", (ev) => {
        // Right-click → delete the marker. (Renaming via right-click
        // would need prompt(), which WKWebView's default config
        // silently swallows. Use the Markers panel below the items
        // table to rename inline.)
        ev.preventDefault(); ev.stopPropagation();
        removeMarker(m.id);
      });
      tlMarkers.appendChild(el);
    }
  }
  // Markers panel — full list (not viewport-restricted). Renaming is
  // inline (no prompt() — WKWebView default config silently swallows
  // those). Click "Rename" → label cell becomes a text input, autofocus,
  // commit on Enter or blur, cancel on Esc.
  if (markersPanel) {
    if (markers.length === 0) {
      markersPanel.innerHTML = `<div class="mp-empty">No markers yet — press B to drop one at the playhead.</div>`;
    } else {
      markersPanel.innerHTML = "";
      for (const m of markers) {
        const row = document.createElement("div");
        row.className = "mp-row";
        row.innerHTML = `
          <span class="mp-time">${fmtTime(m.t)}</span>
          <span class="mp-label"></span>
          <span class="mp-actions">
            <button data-act="rename">Rename</button>
            <button data-act="delete" class="danger">×</button>
          </span>`;
        const labelEl = row.querySelector(".mp-label");
        labelEl.textContent = m.label;
        row.addEventListener("click", (ev) => {
          if (ev.target.closest("button") || ev.target.closest("input")) return;
          markUserSeek();
          try { player.currentTime = m.t; } catch (e) {}
        });
        // Inline rename — replace the label span with a text input.
        const startEdit = () => {
          if (labelEl.querySelector("input")) return;  // already editing
          labelEl.textContent = "";
          const input = document.createElement("input");
          input.type = "text";
          input.value = m.label;
          input.style.cssText =
            "width:100%;font:inherit;padding:1px 3px;border:1px solid #404040;background:#fff;color:#000;";
          labelEl.appendChild(input);
          input.focus(); input.select();
          let committed = false;
          const commit = () => {
            if (committed) return; committed = true;
            const v = input.value.trim();
            if (v && v !== m.label) renameMarker(m.id, v);
            else labelEl.textContent = m.label;  // re-render label
          };
          const cancel = () => {
            committed = true;
            labelEl.textContent = m.label;
          };
          input.addEventListener("keydown", (ev) => {
            ev.stopPropagation();  // don't trigger editor shortcuts
            if (ev.key === "Enter") { ev.preventDefault(); commit(); input.blur(); }
            else if (ev.key === "Escape") { ev.preventDefault(); cancel(); input.blur(); }
          });
          input.addEventListener("blur", commit);
          input.addEventListener("click", e => e.stopPropagation());
          input.addEventListener("mousedown", e => e.stopPropagation());
        };
        // Double-click the label to rename too — quicker than going to button.
        labelEl.addEventListener("dblclick", (ev) => {
          ev.stopPropagation(); startEdit();
        });
        row.querySelector('[data-act="rename"]').addEventListener("click", (ev) => {
          ev.stopPropagation(); startEdit();
        });
        row.querySelector('[data-act="delete"]').addEventListener("click", (ev) => {
          ev.stopPropagation();
          removeMarker(m.id);
        });
        markersPanel.appendChild(row);
      }
    }
  }
  // Mirror on the minimap (calls into _refreshPanBar which reads
  // markers list directly — see the band loop additions there).
  if (typeof _refreshPanBar === "function") _refreshPanBar();
}

// Mark-edit functions keep activeItemId intact — the user adjusting an
// existing clip's in/out should stay in "editing this clip" mode so the
// Add Clip button morphs to Save Changes (see refreshAddClipButton).
// Esc / clicking the active row again still deselects via deselectActive.
function setMarkIn() {
  inPoint = snapKF(player.currentTime);
  if (outPoint != null && outPoint <= inPoint) outPoint = null;
  refreshTimeline(); renderItems(); refreshAddClipButton();
}
function setMarkOut() {
  outPoint = snapKF(player.currentTime);
  if (inPoint != null && outPoint <= inPoint) inPoint = null;
  refreshTimeline(); renderItems(); refreshAddClipButton();
}
function clearMarks() {
  inPoint = null; outPoint = null;
  refreshTimeline(); renderItems(); refreshAddClipButton();
}
document.getElementById("btn-mark-in").addEventListener("click", setMarkIn);
document.getElementById("btn-mark-out").addEventListener("click", setMarkOut);
document.getElementById("btn-clear").addEventListener("click", clearMarks);
document.getElementById("btn-preview").addEventListener("click", () => previewClip());

// Updates the Add Clip button label/title based on whether we're in
// "create new" or "edit existing" mode. Edit mode kicks in whenever an
// item is loaded into the editor (activeItemId set + it's a clip).
const _AUTO_CLIP_NAME_RE = /^clip \d{2,}:\d{2}-\d{2,}:\d{2}$/;
function refreshAddClipButton() {
  const btn = document.getElementById("btn-add-clip");
  if (!btn) return;
  const activeIt = (activeItemId != null)
    ? items.find(x => x.id === activeItemId) : null;
  if (activeIt && activeIt.kind === "clip") {
    btn.textContent = "Save changes (C)";
    btn.title = "Update the active clip's in/out · Esc to discard";
  } else {
    btn.textContent = "Add Clip (C)";
    btn.title = "Save selection as a clip (C)";
  }
}

document.getElementById("btn-add-clip").addEventListener("click", async () => {
  if (inPoint == null || outPoint == null || outPoint <= inPoint) {
    alert("Set both In and Out marks first.");
    return;
  }
  pushUndo();
  // Editing an existing clip → update in place. Otherwise → push new.
  const activeIt = (activeItemId != null)
    ? items.find(x => x.id === activeItemId) : null;
  if (activeIt && activeIt.kind === "clip") {
    activeIt.start = inPoint;
    activeIt.end = outPoint;
    // If the name still matches the auto-generated "clip MM:SS-MM:SS"
    // pattern, regenerate it with the new timestamps. Custom names are
    // left alone — the user took the trouble to rename, don't undo it.
    if (_AUTO_CLIP_NAME_RE.test(activeIt.name || "")) {
      activeIt.name = `clip ${fmtClipStamp(inPoint)}-${fmtClipStamp(outPoint)}`;
    }
    // Re-capture the thumb at the new start time.
    activeIt.thumb = await captureFromMain(activeIt.start, 200);
    // Exit edit mode — clip now reflects the new bounds.
    inPoint = null; outPoint = null;
    activeItemId = null; preSnapshot = null;
    refreshTimeline();
    renderItems(); persistItems();
    refreshAddClipButton();
    // Crop view reverts to session default now that no item is loaded.
    refreshCropReadout();
    refreshCropGhost();
    return;
  }
  // No active clip → create a new one.
  const id = nextId++;
  const it = {
    id, kind: "clip", start: inPoint, end: outPoint,
    // Self-documenting default — final filename becomes
    // "<source name> - clip 02:30-03:15.mp4". User can rename freely;
    // we stamp the actual time range in so the file isn't anonymous on
    // disk if they don't bother.
    name: `clip ${fmtClipStamp(inPoint)}-${fmtClipStamp(outPoint)}`,
    container: "mp4-web",
    quality: defaultQuality,
    thumb: "",
    // Snapshot the session-level crop at creation time. If the user
    // toggles auto-crop later, existing items keep what they had —
    // they can still override per-row via Crop…
    crop: sessionCrop ? Object.assign({}, sessionCrop) : null,
  };
  items.push(it);
  // Clear in/out marks so the canvas is fresh for the next selection.
  // The clip itself is preserved — visible as an orange band on the
  // filmstrip + minimap, and as a row in the items table. Clicking the
  // row later restores its in/out for adjustment via loadItem().
  inPoint = null;
  outPoint = null;
  activeItemId = null;
  preSnapshot = null;
  refreshTimeline();
  renderItems(); persistItems();
  refreshAddClipButton();
  refreshCropReadout();
  refreshCropGhost();
  it.thumb = await captureFromMain(it.start, 200);
  renderItems(); persistItems();
});

document.getElementById("btn-still").addEventListener("click", async () => {
  pushUndo();
  const id = nextId++;
  const fmt = document.getElementById("still-format").value;
  const it = {
    id, kind: "still", t: player.currentTime,
    // Stamp the timestamp so multiple stills don't collide on disk.
    name: `still ${fmtClipStamp(player.currentTime)}`,
    format: fmt,
    quality: defaultQuality,
    thumb: "",
    crop: sessionCrop ? Object.assign({}, sessionCrop) : null,
  };
  items.push(it);
  renderItems(); persistItems();
  it.thumb = await captureFromMain(it.t, 200);
  renderItems(); persistItems();
});

document.getElementById("btn-done").addEventListener("click", finishEditor);

// Lightbox for clip/still thumbnails. Click any thumb-cell in the items
// table to open. Escape or backdrop click closes.
const lightbox = document.getElementById("lightbox");
const lightboxImg = document.getElementById("lightbox-img");
const lightboxCap = document.getElementById("lightbox-caption");
function openLightbox(src, caption) {
  if (!src) return;
  lightboxImg.src = src;
  lightboxCap.textContent = caption || "";
  lightbox.classList.add("show");
}
function closeLightbox() {
  lightbox.classList.remove("show");
  lightboxImg.src = "";
}
lightbox.addEventListener("click", closeLightbox);
document.querySelector("#items").addEventListener("click", (e) => {
  const cell = e.target.closest(".thumb-cell");
  if (!cell) return;
  e.stopPropagation();
  // Pull the data URI out of the inline background-image. This is the
  // already-captured thumb stored on the item — no extra fetch needed.
  const bg = cell.style.backgroundImage || "";
  const m = bg.match(/url\((["']?)(.*?)\1\)/);
  if (!m || !m[2]) return;
  const tr = cell.closest("tr");
  const id = +tr.dataset.id;
  const it = items.find(x => x.id === id);
  let cap = "";
  if (it) {
    cap = it.kind === "clip"
      ? `${it.name} · ${fmtTime(it.start)} → ${fmtTime(it.end)}`
      : `${it.name} · ${fmtTime(it.t)}`;
  }
  openLightbox(m[2], cap);
});

document.addEventListener("keydown", (e) => {
  // Cmd-key shortcuts work even with focus in inputs, since they're
  // editor-global (Undo, Redo, Select All).
  const meta = e.metaKey || e.ctrlKey;
  if (meta && (e.key === "z" || e.key === "Z")) {
    e.preventDefault();
    if (e.shiftKey) redo(); else undo();
    return;
  }
  if (meta && (e.key === "y" || e.key === "Y")) {
    e.preventDefault(); redo(); return;
  }
  if (meta && (e.key === "a" || e.key === "A")) {
    // Don't override the OS Edit menu's Select All when an input has focus.
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    e.preventDefault(); selectAllItems(); return;
  }
  if (meta && e.key === "0") {
    e.preventDefault(); zoomFit(); return;
  }
  if (meta && (e.key === "=" || e.key === "+")) {
    e.preventDefault(); zoomBy(1.6); return;
  }
  if (meta && e.key === "-") {
    e.preventDefault(); zoomBy(1/1.6); return;
  }

  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;

  // Help overlay accepts Esc to close at any priority.
  if (_help.classList.contains("show")) {
    if (e.key === "Escape" || e.key === "?") { hideHelp(); }
    return;
  }
  if (e.key === "Escape" && lightbox.classList.contains("show")) {
    closeLightbox(); return;
  }
  if (e.key === "?" || (e.shiftKey && e.key === "/")) {
    e.preventDefault(); showHelp(); return;
  }
  // Every handled shortcut calls preventDefault — without it, WKWebView
  // bubbles the keyDown to AppKit which beeps because no input field
  // claimed it. The shortcuts already worked; the beep was just noise.
  if (e.key === "i" || e.key === "I") {
    e.preventDefault(); setMarkIn();
  } else if (e.key === "o" || e.key === "O") {
    e.preventDefault(); setMarkOut();
  } else if (e.key === "q" || e.key === "Q") {
    e.preventDefault(); jumpToIn();
  } else if (e.key === "w" || e.key === "W") {
    e.preventDefault(); jumpToOut();
  } else if (e.key === "[") {
    e.preventDefault(); nudgeMark("in",  e.shiftKey ? -1 : -1/30);
  } else if (e.key === "]") {
    e.preventDefault(); nudgeMark("out", e.shiftKey ?  1 :  1/30);
  } else if (e.key === "p" || e.key === "P") {
    e.preventDefault(); previewClip();
  } else if (e.key === "s" || e.key === "S") {
    e.preventDefault(); document.getElementById("btn-still").click();
  } else if (e.key === "c" || e.key === "C") {
    e.preventDefault(); document.getElementById("btn-add-clip").click();
  } else if (e.key === "l" || e.key === "L") {
    e.preventDefault(); loopBtn.click();
  } else if (e.key === "m" || e.key === "M") {
    e.preventDefault(); muteBtn.click();
  } else if (e.key === "b" || e.key === "B") {
    // Drop a marker at the playhead — labelled bookmark for navigation.
    e.preventDefault();
    addMarker(player.currentTime || 0);
  } else if (e.key === "," || e.key === "<") {
    // Frame-step backward. Frame-step is an explicit user seek —
    // suppress loop wrap so the user can step PAST the out point
    // without getting bounced back. Used to be playback-speed
    // slower; speed now lives only in the dropdown control.
    e.preventDefault();
    markUserSeek();
    player.currentTime = Math.max(0, player.currentTime - 1/30);
  } else if (e.key === "." || e.key === ">") {
    e.preventDefault();
    markUserSeek();
    player.currentTime = Math.min(effectiveDuration() - 0.001,
                                  player.currentTime + 1/30);
  } else if (e.key === "Backspace" || e.key === "Delete") {
    if (selectedIds.size || activeItemId != null) {
      e.preventDefault();
      deleteSelected();
    }
  } else if (e.key === "Escape") {
    // Esc priorities: cancel concat preview > clear selection >
    // deselect active. Each early-returns to avoid double-action.
    if (_concatPreview) {
      e.preventDefault();
      _concatPreview = null;
      try { player.pause(); } catch (e2) {}
      return;
    }
    if (selectedIds.size) { e.preventDefault(); setRowSelection([]); return; }
    if (activeItemId != null) { e.preventDefault(); deselectActive(true); }
  } else if (e.key === " ") {
    e.preventDefault();
    playBtn.click();
  } else if (e.key === "ArrowLeft") {
    // Plain ← seeks 1s; Shift+← seeks 10s.
    e.preventDefault();
    markUserSeek();
    player.currentTime = Math.max(0, player.currentTime - (e.shiftKey ? 10 : 1));
  } else if (e.key === "ArrowRight") {
    e.preventDefault();
    markUserSeek();
    player.currentTime = Math.min(effectiveDuration() - 0.001,
                                  player.currentTime + (e.shiftKey ? 10 : 1));
  } else if (e.key === "ArrowUp") {
    // Cmd/Ctrl+↑ → reorder loaded item up. Plain ↑ → navigate to
    // previous item in the list (load it). The reorder behavior used
    // to live on plain ↑ but it conflicted with the more natural
    // expectation of arrow keys as list navigation. Now matches the
    // mac standard: arrows move selection, modifier+arrow rearranges.
    if (e.metaKey || e.ctrlKey) {
      if (activeItemId != null) {
        e.preventDefault();
        moveItemByOne(activeItemId, -1);
      }
      return;
    }
    if (items.length === 0) return;
    e.preventDefault();
    const curIdx = activeItemId != null
      ? items.findIndex(x => x.id === activeItemId) : -1;
    if (curIdx === -1) {
      // Nothing loaded → load the last item so ↑ feels like "select
      // the bottom of the list" (mirrors a standard list navigator).
      loadItem(items[items.length - 1]);
    } else if (curIdx > 0) {
      loadItem(items[curIdx - 1]);
    }
    // else: at top of list, no-op
  } else if (e.key === "ArrowDown") {
    if (e.metaKey || e.ctrlKey) {
      if (activeItemId != null) {
        e.preventDefault();
        moveItemByOne(activeItemId, +1);
      }
      return;
    }
    if (items.length === 0) return;
    e.preventDefault();
    const curIdx = activeItemId != null
      ? items.findIndex(x => x.id === activeItemId) : -1;
    if (curIdx === -1) {
      loadItem(items[0]);
    } else if (curIdx < items.length - 1) {
      loadItem(items[curIdx + 1]);
    }
    // else: at bottom of list, no-op
  }
});

function renderItems() {
  const tbody = document.querySelector("#items tbody");
  tbody.innerHTML = "";
  for (const it of items) {
    const tr = document.createElement("tr");
    tr.dataset.id = String(it.id);
    // Reorder is wired manually below — see the grip mousedown handler.
    // (HTML5 native drag on <tr> is unreliable in WKWebView.)
    if (it.id === activeItemId) tr.classList.add("active");
    if (selectedIds.has(it.id)) tr.classList.add("selected");
    let rangeCell, fmtCell;
    if (it.kind === "clip") {
      rangeCell = `<span class="mono">${fmtTime(it.start)} → ${fmtTime(it.end)}</span>`;
      fmtCell = `<select data-id="${it.id}" class="fmt-clip">
        <option value="mp4-web" ${it.container === "mp4-web" ? "selected" : ""}>MP4 (Web-safe)</option>
        <option value="mp4" ${it.container === "mp4" ? "selected" : ""}>MP4 (QuickTime)</option>
        <option value="mp4-h264" ${it.container === "mp4-h264" ? "selected" : ""}>MP4 (H.264)</option>
        <option value="mp4-h265" ${it.container === "mp4-h265" ? "selected" : ""}>MP4 (H.265)</option>
        <option value="mkv" ${it.container === "mkv" ? "selected" : ""}>MKV</option>
        <option value="webm" ${it.container === "webm" ? "selected" : ""}>WebM</option>
      </select>`;
    } else if (it.kind === "concat") {
      // Range cell summarises the stitched length + segment count.
      const total = (it.segments || []).reduce(
        (s, x) => s + Math.max(0, (+x.end || 0) - (+x.start || 0)), 0);
      const segs = (it.segments || []).length;
      rangeCell = `<span class="mono">${segs} segs · ${fmtTime(total)}</span>`;
      // Concat output is always web-safe MP4 (no per-segment codec
      // mixing) — no format dropdown needed, but show it for clarity.
      fmtCell = `<span class="small">MP4 (Web-safe)</span>`;
    } else {
      rangeCell = `<span class="mono">${fmtTime(it.t)}</span>`;
      fmtCell = `<select data-id="${it.id}" class="fmt-still">
        <option value="jpeg" ${it.format === "jpeg" ? "selected" : ""}>JPEG</option>
        <option value="png" ${it.format === "png" ? "selected" : ""}>PNG</option>
      </select>`;
    }
    // Single quotes around the URL — the outer style="…" uses double quotes,
    // and data: URIs contain none of these so this is unambiguous.
    const thumbStyle = it.thumb ? `background-image:url('${it.thumb}')` : "";
    const itq = normalizeQuality(it.quality);
    const qOpts = QUALITY_OPTIONS.map(([v, l]) =>
      `<option value="${v}" ${itq === v ? "selected" : ""}>${l}</option>`
    ).join("");
    // Index of this row in items[] — used to disable up at top, down
    // at bottom. Cheap because items lists are short.
    const idx = items.indexOf(it);
    const upDisabled   = (idx <= 0)              ? "disabled" : "";
    const downDisabled = (idx >= items.length-1) ? "disabled" : "";
    tr.innerHTML = `
      <td class="td-check"><input type="checkbox" class="row-check" data-id="${it.id}" ${selectedIds.has(it.id) ? "checked" : ""} aria-label="Select item"></td>
      <td class="td-move">
        <span class="move-stack">
          <button data-id="${it.id}" class="btn-move btn-up"   title="Move up (↑)"   ${upDisabled}>▲</button>
          <button data-id="${it.id}" class="btn-move btn-down" title="Move down (↓)" ${downDisabled}>▼</button>
        </span>
      </td>
      <td><span class="thumb-cell" style="${thumbStyle}"></span></td>
      <td class="type-cell type-${it.kind}">${it.kind}</td>
      <td><input type="text" data-id="${it.id}" class="name" value="${escapeHtml(it.name)}"></td>
      <td>${rangeCell}</td>
      <td>${fmtCell}</td>
      <td><select data-id="${it.id}" class="qual">${qOpts}</select></td>
      <td>
        ${it.crop ? '<span class="crop-badge" title="This item was captured with a crop">Cropped</span>' : ''}
        <button data-id="${it.id}" class="btn-remove">Remove</button>
      </td>`;
    tbody.appendChild(tr);
  }
  // Stop row-click from firing on interactive children.
  tbody.querySelectorAll("input, select, button").forEach(el => {
    el.addEventListener("click", e => e.stopPropagation());
    el.addEventListener("mousedown", e => e.stopPropagation());
  });
  tbody.querySelectorAll(".name").forEach(el => {
    el.addEventListener("change", e => {
      const it = items.find(x => x.id == e.target.dataset.id);
      if (it && it.name !== e.target.value) {
        pushUndo();
        it.name = e.target.value;
        persistItems();
      }
    });
  });
  tbody.querySelectorAll(".fmt-clip").forEach(el => {
    el.addEventListener("change", e => {
      const it = items.find(x => x.id == e.target.dataset.id);
      if (it && it.container !== e.target.value) {
        pushUndo();
        it.container = e.target.value;
        persistItems();
      }
    });
  });
  tbody.querySelectorAll(".fmt-still").forEach(el => {
    el.addEventListener("change", e => {
      const it = items.find(x => x.id == e.target.dataset.id);
      if (it && it.format !== e.target.value) {
        pushUndo();
        it.format = e.target.value;
        persistItems();
      }
    });
  });
  tbody.querySelectorAll(".qual").forEach(el => {
    el.addEventListener("change", e => {
      const it = items.find(x => x.id == e.target.dataset.id);
      if (it && it.quality !== e.target.value) {
        pushUndo();
        it.quality = e.target.value;
        persistItems();
      }
    });
  });
  // Checkbox per row → toggle membership in selectedIds. Stops
  // propagation so the underlying row click (load item) doesn't also
  // fire when you tick the box.
  tbody.querySelectorAll(".row-check").forEach(el => {
    el.addEventListener("click", e => e.stopPropagation());
    el.addEventListener("mousedown", e => e.stopPropagation());
    el.addEventListener("change", e => {
      const id = +e.target.dataset.id;
      if (e.target.checked) {
        // Entering multi-select mode → unload any single-selected item
        // first. Single and multi can't coexist.
        if (activeItemId != null) deselectActive(/*restore=*/true);
        selectedIds.add(id);
      } else {
        selectedIds.delete(id);
      }
      // Lighter than re-render — just sync class + selection-counts UI.
      refreshSelectionUI();
    });
  });
  tbody.querySelectorAll(".btn-remove").forEach(el => {
    el.addEventListener("click", e => {
      const id = +e.target.dataset.id;
      pushUndo();
      const idx = items.findIndex(x => x.id === id);
      if (idx >= 0) items.splice(idx, 1);
      if (activeItemId === id) {
        activeItemId = null;
        // Removing the active item discards its restoration snapshot —
        // the user explicitly chose to drop it.
        preSnapshot = null;
        refreshTimeline();
      }
      selectedIds.delete(id);
      renderItems(); persistItems();
    });
  });
  // Reorder via ↑ / ↓ buttons in the Actions cell. Old-school but
  // deterministic — no drag flake. Disabled at the boundaries.
  tbody.querySelectorAll(".btn-move").forEach(el => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      if (el.disabled) return;
      const id = +el.dataset.id;
      const dir = el.classList.contains("btn-up") ? -1 : +1;
      moveItemByOne(id, dir);
    });
  });

  tbody.querySelectorAll("tr").forEach(tr => {
    tr.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = +tr.dataset.id;
      // Strict mode separation:
      //   - Single-select  = click row body. Loads the item.
      //   - Multi-select   = tick checkboxes. Used for batch ops.
      // The two are mutually exclusive. While ANY checkbox is ticked
      // (multi-select active), row body clicks are inert — the user
      // must clear the checkboxes (untick or use the header "Select
      // all" toggle) to return to single-select mode.
      if (selectedIds.size > 0) return;
      // Click the already-active row to unload it (toggle off).
      if (activeItemId === id) {
        deselectActive(/*restore=*/true);
        return;
      }
      const it = items.find(x => x.id === id);
      if (it) {
        // Single-select: load this item. Don't touch selectedIds —
        // checkboxes stay empty in single-select mode.
        loadItem(it);
      }
    });
  });
}

function loadItem(it) {
  // Snapshot the pre-selection state once, so the user can return to
  // exactly where they were by clicking elsewhere or pressing Esc.
  // Switching between items keeps the original snapshot.
  if (activeItemId == null) {
    preSnapshot = {
      inPoint, outPoint,
      currentTime: player.currentTime,
    };
  }
  activeItemId = it.id;
  if (it.kind === "clip") {
    inPoint = it.start;
    outPoint = it.end;
    try { player.currentTime = it.start; } catch (e) {}
  } else if (it.kind === "concat") {
    inPoint = null;
    outPoint = null;
    // Seek to the first segment's start so the user has a sensible
    // playback position. Press Preview (P) to play through the concat.
    const first = (it.segments || [])[0];
    if (first && isFinite(+first.start)) {
      try { player.currentTime = +first.start; } catch (e) {}
    }
  } else {
    inPoint = null;
    outPoint = null;
    try { player.currentTime = it.t; } catch (e) {}
  }
  // Loading a different item cancels any concat preview in flight.
  if (typeof _concatPreview !== "undefined") _concatPreview = null;
  // If the loaded item sits outside the current viewport (zoomed in
  // somewhere else), recenter so the user can SEE what they just
  // loaded. For clips we also widen the centering bias toward the
  // middle of the clip so its in/out flags both fall inside the view.
  const [vpA, vpB] = vpRange();
  const itStart = it.kind === "clip" ? it.start : it.t;
  const itEnd   = it.kind === "clip" ? it.end   : it.t;
  if (itStart < vpA || itEnd > vpB) {
    const targetCenter = (itStart + itEnd) / 2;
    const d = effectiveDuration();
    if (d > 0) {
      _zoomCenter = Math.max(0, Math.min(d, targetCenter));
      // Use the in-place-update path (no shimmer) since this is a
      // discrete user-triggered jump.
      buildFilmstripSoon(0);
    }
  }
  refreshTimeline();
  renderItems();
  refreshAddClipButton();
  // Crop ghost + readout swap to show this item's crop (or "no crop"
  // if none is set on the item).
  refreshCropReadout();
  refreshCropGhost();
}

function deselectActive(restore) {
  if (activeItemId == null) return;
  activeItemId = null;
  if (restore && preSnapshot) {
    inPoint = preSnapshot.inPoint;
    outPoint = preSnapshot.outPoint;
    try { player.currentTime = preSnapshot.currentTime; } catch (e) {}
  }
  preSnapshot = null;
  refreshTimeline();
  renderItems();
  refreshAddClipButton();
  // Crop ghost + readout revert to session crop (the default for new
  // captures) now that no item is selected.
  refreshCropReadout();
  refreshCropGhost();
}

// Note: there used to be a document-level mousedown listener here that
// auto-deselected the active item on any click outside the items table.
// That made it impossible to edit a loaded clip — clicking the timeline
// to scrub, clicking a control, or even clicking the player itself
// would silently revert marks back to preSnapshot and clear
// activeItemId, so subsequent Save Changes pressed Add Clip's
// "create new" path instead. Deselection now requires an explicit
// gesture: Esc (restores preSnapshot), or clicking the active row
// again in the items table (toggles off).

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// Server persistence — keeps the items list alive across editor
// open/close so the user can re-open and adjust without losing work.
//
// Save pill states: idle "Saved" / yellow "Saving…" / red "Failed". The
// pill flicks to "Saving…" the moment a mutation queues the debounce, so
// users see immediate feedback even while the 200 ms timer is ticking.
const _savePill = document.getElementById("save-pill");
const _savePillText = document.getElementById("save-pill-text");
function setSaveState(state, msg) {
  if (!_savePill) return;
  _savePill.dataset.state = state;
  _savePillText.textContent = msg || (
    state === "saving" ? "Saving…" :
    state === "error"  ? "Save failed — retrying" :
                          "Saved"
  );
}
let persistTimer = null;
let _persistInflight = 0;
function persistItems() {
  setSaveState("saving");
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    persistTimer = null;
    const payload = items.map(it => ({
      id: it.id, kind: it.kind, name: it.name, thumb: it.thumb || "",
      start: it.kind === "clip" ? it.start : null,
      end:   it.kind === "clip" ? it.end : null,
      t:     it.kind === "still" ? it.t : null,
      container: (it.kind === "clip" || it.kind === "concat")
                 ? (it.container || "mp4-web") : null,
      format:    it.kind === "still" ? (it.format || "jpeg") : null,
      quality:   normalizeQuality(it.quality),
      crop:      it.crop || null,
      // Concat items carry their stitched-from segments here.
      segments:  it.kind === "concat" ? (it.segments || []) : null,
    }));
    _persistInflight += 1;
    // Markers ride along on the same POST — small payload, atomic save.
    const markersPayload = (typeof markers !== "undefined" && Array.isArray(markers))
      ? markers.map(m => ({id: m.id, t: m.t, label: m.label})) : [];
    fetch("/editor/items", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({sid: SID, items: payload, markers: markersPayload}),
    }).then(r => {
      if (!r.ok) throw new Error("HTTP " + r.status);
      _persistInflight -= 1;
      // Only flip back to "Saved" when no further saves are queued/inflight.
      if (_persistInflight === 0 && persistTimer == null) setSaveState("saved");
    }).catch(() => {
      _persistInflight -= 1;
      setSaveState("error");
      // Auto-retry once a few seconds later by re-queueing.
      setTimeout(() => persistItems(), 3000);
    });
  }, 200);
}

// ===== Undo / Redo =====
// The unit of undo is "items array snapshot". We capture a JSON snapshot
// before any mutation (add, remove, edit-name, change format, etc.).
// Cmd+Z restores the previous snapshot, pushing the current state to the
// redo stack. Cmd+Shift+Z (or Cmd+Y) re-applies the most-recent redo.
const _undoStack = [];
const _redoStack = [];
const _UNDO_CAP = 80;
function _itemsSnapshot() {
  // Deep-clone via JSON so the snapshot can't be mutated by reference.
  return JSON.stringify(items);
}
function _restoreSnapshot(json) {
  let arr;
  try { arr = JSON.parse(json); } catch (e) { return; }
  if (!Array.isArray(arr)) return;
  items.length = 0;
  for (const it of arr) items.push(it);
}
function pushUndo() {
  _undoStack.push(_itemsSnapshot());
  while (_undoStack.length > _UNDO_CAP) _undoStack.shift();
  _redoStack.length = 0;
}
function undo() {
  if (!_undoStack.length) return;
  _redoStack.push(_itemsSnapshot());
  _restoreSnapshot(_undoStack.pop());
  // Active selection might point at a removed/rearranged id — clear it.
  if (activeItemId != null && !items.some(it => it.id === activeItemId)) {
    activeItemId = null; preSnapshot = null;
  }
  selectedIds.clear();
  renderItems(); persistItems(); refreshTimeline();
}
function redo() {
  if (!_redoStack.length) return;
  _undoStack.push(_itemsSnapshot());
  _restoreSnapshot(_redoStack.pop());
  if (activeItemId != null && !items.some(it => it.id === activeItemId)) {
    activeItemId = null; preSnapshot = null;
  }
  selectedIds.clear();
  renderItems(); persistItems(); refreshTimeline();
}

// ===== Multi-select (rows) =====
// `selectedIds` is the set selected for bulk operations (delete). It's
// distinct from `activeItemId` which is "loaded into the player". A row
// can be selected without being active; clicking the row sets active +
// selection-singleton; Cmd-click toggles, Shift-click extends a range.
const selectedIds = new Set();
let _lastSelectedId = null;
function setRowSelection(ids) {
  selectedIds.clear();
  for (const id of ids) selectedIds.add(id);
  refreshSelectionUI();
}
function toggleRowSelection(id) {
  if (selectedIds.has(id)) selectedIds.delete(id);
  else selectedIds.add(id);
  refreshSelectionUI();
}
function rangeSelect(toId) {
  if (_lastSelectedId == null) { selectedIds.add(toId); refreshSelectionUI(); return; }
  const a = items.findIndex(x => x.id === _lastSelectedId);
  const b = items.findIndex(x => x.id === toId);
  if (a < 0 || b < 0) { selectedIds.add(toId); refreshSelectionUI(); return; }
  const [lo, hi] = a < b ? [a, b] : [b, a];
  selectedIds.clear();
  for (let i = lo; i <= hi; i++) selectedIds.add(items[i].id);
  refreshSelectionUI();
}
function selectAllItems() {
  // Cmd+A enters multi-select mode → unload any single-selected item.
  if (activeItemId != null) deselectActive(/*restore=*/true);
  selectedIds.clear();
  for (const it of items) selectedIds.add(it.id);
  refreshSelectionUI();
}
function refreshSelectionUI() {
  // Toggle the .selected class on existing rows without a full re-render —
  // keeps any focused name input from losing its caret/composition.
  const rows = document.querySelectorAll("#items tbody tr");
  rows.forEach(tr => {
    const id = +tr.dataset.id;
    tr.classList.toggle("selected", selectedIds.has(id));
    // Sync the per-row checkbox without firing change events.
    const cb = tr.querySelector(".row-check");
    if (cb) cb.checked = selectedIds.has(id);
  });
  // Header "select all" tristate: checked when all items selected,
  // indeterminate when some, unchecked when none.
  const allCb = document.getElementById("items-select-all");
  if (allCb) {
    if (items.length === 0) {
      allCb.checked = false; allCb.indeterminate = false;
    } else if (selectedIds.size === 0) {
      allCb.checked = false; allCb.indeterminate = false;
    } else if (selectedIds.size === items.length) {
      allCb.checked = true; allCb.indeterminate = false;
    } else {
      allCb.checked = false; allCb.indeterminate = true;
    }
  }
  const btn = document.getElementById("btn-remove-selected");
  if (btn) {
    if (selectedIds.size > 1) {
      btn.style.display = "";
      btn.textContent = `Remove (${selectedIds.size})`;
    } else {
      btn.style.display = "none";
    }
  }
  // Concat clips — only visible when 2+ CLIPS are in the selection.
  // Stills don't concat (they're images), so we filter accordingly.
  const concatBtn = document.getElementById("btn-concat-selected");
  if (concatBtn) {
    let clipCount = 0;
    for (const id of selectedIds) {
      const it = items.find(x => x.id === id);
      if (it && it.kind === "clip") clipCount++;
    }
    if (clipCount >= 2) {
      concatBtn.style.display = "";
      concatBtn.textContent = `Concat clips (${clipCount})`;
    } else {
      concatBtn.style.display = "none";
    }
  }
  // Crop scope changes with selection: when nothing is loaded but rows
  // are checkbox-selected, the Crop fieldset operates on all of them.
  // Refresh the readout + on-player ghost so the user can see what the
  // current "effective crop" is.
  if (typeof refreshCropReadout === "function") refreshCropReadout();
  if (typeof refreshCropGhost   === "function") refreshCropGhost();
}
// Move an item one slot up (-1) or down (+1) in items[]. Wires both
// the row's ↑/↓ buttons and the global ↑/↓ keyboard shortcuts.
function moveItemByOne(id, dir) {
  const idx = items.findIndex(x => x.id === id);
  if (idx < 0) return;
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= items.length) return;
  pushUndo();
  // Swap-with-neighbour is the cleanest one-step move and preserves
  // the order of other items.
  [items[idx], items[newIdx]] = [items[newIdx], items[idx]];
  renderItems(); persistItems();
  try { renderClipBands(); } catch (e) {}
}

function deleteSelected() {
  if (!selectedIds.size) {
    // Fall back to the active item if there's no multi-select.
    if (activeItemId != null) selectedIds.add(activeItemId);
    else return;
  }
  pushUndo();
  for (let i = items.length - 1; i >= 0; i--) {
    if (selectedIds.has(items[i].id)) items.splice(i, 1);
  }
  if (activeItemId != null && !items.some(x => x.id === activeItemId)) {
    activeItemId = null; preSnapshot = null; refreshTimeline();
  }
  selectedIds.clear();
  _lastSelectedId = null;
  renderItems(); persistItems();
}

// ===== Preview clip / concat =====
// One-shot playback of inPoint→outPoint. Distinct from Loop, which
// repeats. Useful for sanity-checking a selection before saving it.
//
// If a CONCAT item is the active selection, Preview switches mode:
// it plays through the concat's segments in order, jumping between
// them via the rVFC frame-check loop below. Lets the user feel out the
// stitched video before they bother ripping it.
let _previewing = false;
let _concatPreview = null;     // {segments:[{start,end}], idx} during concat playback

function previewClip() {
  // Concat preview takes priority when the active item is a concat.
  const it = (activeItemId != null) ? items.find(x => x.id === activeItemId) : null;
  if (it && it.kind === "concat") {
    return previewConcat(it);
  }
  if (inPoint == null || outPoint == null || outPoint <= inPoint) return;
  _previewing = true;
  _concatPreview = null;
  markUserSeek();
  try { player.currentTime = inPoint; } catch (e) {}
  player.play().catch(() => {});
}

function previewConcat(it) {
  const segs = (it.segments || []).filter(
    s => isFinite(+s.start) && isFinite(+s.end) && +s.end > +s.start
  ).map(s => ({ start: +s.start, end: +s.end }));
  if (segs.length === 0) return;
  _previewing = false;
  _concatPreview = { segments: segs, idx: 0 };
  markUserSeek();
  try { player.currentTime = segs[0].start; } catch (e) {}
  player.play().catch(() => {});
}
function cancelConcatPreview() {
  if (!_concatPreview) return;
  _concatPreview = null;
}
// Preview auto-pause — same frame-accurate path as Loop, gated by the
// same user-seek grace so user scrubs during preview don't trigger an
// early stop.
if (_hasRVFC) {
  function _rvfcPreviewCheck(now, meta) {
    if (_previewing) {
      if (outPoint == null) {
        _previewing = false;
      } else if (!_shouldSuppressLoop() && meta.mediaTime >= outPoint) {
        _previewing = false;
        try { player.pause(); } catch (e) {}
        try { player.currentTime = outPoint; } catch (e) {}
      }
    }
    player.requestVideoFrameCallback(_rvfcPreviewCheck);
  }
  player.requestVideoFrameCallback(_rvfcPreviewCheck);
} else {
  let _previewLastTime = 0;
  player.addEventListener("timeupdate", () => {
    const prev = _previewLastTime;
    const cur = player.currentTime;
    _previewLastTime = cur;
    if (!_previewing) return;
    if (outPoint == null) { _previewing = false; return; }
    if (_shouldSuppressLoop()) return;
    const naturalCross = (prev < outPoint &&
                          cur >= outPoint &&
                          (cur - prev) < 0.5);
    if (naturalCross) {
      _previewing = false;
      try { player.pause(); } catch (e) {}
      try { player.currentTime = outPoint; } catch (e) {}
    }
  });
}

// ===== Mark navigation + nudging =====
// Mark navigation = explicit user seek → suppress loop wrap so the
// jump-to-out doesn't immediately bounce back to in.
function jumpToIn()  {
  if (inPoint != null) {
    markUserSeek();
    try { player.currentTime = inPoint; } catch (e) {}
  }
}
function jumpToOut() {
  if (outPoint != null) {
    markUserSeek();
    try { player.currentTime = outPoint; } catch (e) {}
  }
}
function nudgeMark(which, dt) {
  const d = effectiveDuration(); if (d <= 0) return;
  const clamp = v => Math.max(0, Math.min(d - 0.001, v));
  if (which === "in") {
    if (inPoint == null) return;
    inPoint = clamp(inPoint + dt);
    if (outPoint != null && inPoint >= outPoint) inPoint = clamp(outPoint - 1/30);
  } else {
    if (outPoint == null) return;
    outPoint = clamp(outPoint + dt);
    if (inPoint != null && outPoint <= inPoint) outPoint = clamp(inPoint + 1/30);
  }
  refreshTimeline();
}

// ===== Draggable in/out marks =====
// Each mark is a separate drag context. The timeline's own scrub mousedown
// is gated by a target check that ignores .tl-mark, so dragging a mark
// doesn't also scrub. Holding Alt during the drag snaps the mark to the
// current playhead (useful for "set this mark exactly where I'm watching").
function _wireMarkDrag(el, which) {
  el.addEventListener("mousedown", (e) => {
    if (which === "in" && inPoint == null) return;
    if (which === "out" && outPoint == null) return;
    e.preventDefault(); e.stopPropagation();
    el.classList.add("dragging");
    const d0 = effectiveDuration(); if (d0 <= 0) { el.classList.remove("dragging"); return; }
    const startX = e.clientX;
    let didMove = false;        // becomes true once mouse exceeds 3px threshold
    let raf = false, lastX = e.clientX, altHeld = e.altKey;
    const apply = () => {
      raf = false;
      const rect = tl.getBoundingClientRect();
      let x = (lastX - rect.left) / rect.width;
      x = Math.max(0, Math.min(1, x));
      // Viewport-aware: x=0..1 maps to vpRange()[0]..[1] when zoomed.
      let t = percentToTime(x * 100);
      if (altHeld) {
        // Snap to playhead position.
        t = player.currentTime || 0;
      }
      // Then snap to the nearest keyframe if the user has snap enabled
      // (and we have keyframe times loaded). Cleaner cuts.
      t = snapKF(t);
      if (which === "in") {
        if (outPoint != null && t >= outPoint) t = Math.max(0, outPoint - 1/30);
        inPoint = t;
      } else {
        if (inPoint != null && t <= inPoint) t = Math.min(d0 - 0.001, inPoint + 1/30);
        outPoint = t;
      }
      refreshTimeline();
    };
    const onMove = (ev) => {
      // Only count as a real drag once the mouse has moved more than 3px
      // — protects single-click jumps from being mis-read as zero-length
      // drags that would re-set the mark to the click position.
      if (!didMove && Math.abs(ev.clientX - startX) > 3) didMove = true;
      if (!didMove) return;
      lastX = ev.clientX; altHeld = ev.altKey;
      if (raf) return; raf = true;
      requestAnimationFrame(apply);
    };
    const onUp = () => {
      el.classList.remove("dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      if (!didMove) {
        // Click without drag → jump playhead to this mark's time. Lets
        // the user quickly seek to In/Out by tapping the flag.
        const t = (which === "in") ? inPoint : outPoint;
        if (t != null) {
          markUserSeek();
          try { player.currentTime = t; } catch (e) {}
          refreshTimeline();
        }
      }
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}
_wireMarkDrag(tlIn,  "in");
_wireMarkDrag(tlOut, "out");

// ===== Shortcut help overlay =====
const _help = document.getElementById("shortcut-help");
function showHelp() { _help.classList.add("show"); }
function hideHelp() { _help.classList.remove("show"); }
_help.addEventListener("click", (e) => {
  // Click the dim backdrop or the explicit Close button to dismiss.
  if (e.target === _help || e.target.id === "sh-close-btn") hideHelp();
});
document.getElementById("btn-help").addEventListener("click", showHelp);
document.getElementById("sh-close-btn").addEventListener("click", hideHelp);

// Bulk-remove button (only visible when 2+ rows are selected).
document.getElementById("btn-remove-selected").addEventListener("click", deleteSelected);

// Header "Select all" checkbox — toggle every row's selection at once.
// Empty checkbox or indeterminate → check all; checked → uncheck all.
// Entering multi-select unloads any single-selected item.
document.getElementById("items-select-all").addEventListener("change", (e) => {
  selectedIds.clear();
  if (e.target.checked) {
    if (activeItemId != null) deselectActive(/*restore=*/true);
    for (const it of items) selectedIds.add(it.id);
  }
  refreshSelectionUI();
});

// ───── Concat selected clips ──────────────────────────────────────────
// Pushes a NEW item with kind="concat" onto the items list, snapshotting
// the included clips' time ranges + per-clip crops. The actual encode
// happens later when the user clicks Rip It! on the main page card —
// same lifecycle as clips and stills (define in editor → rip from main
// page). The concat item carries `segments: [{start,end,crop}, …]` and
// gets exported via the /concat endpoint at rip time.
async function concatSelectedClips() {
  // Pull selected clips IN ITEMS ORDER so concat respects the user's
  // drag-reorder, not the order they happened to click.
  const selectedClips = items.filter(it =>
    selectedIds.has(it.id) && it.kind === "clip"
  );
  if (selectedClips.length < 2) {
    alert("Select 2 or more clips to concat.");
    return;
  }
  // Auto-name. The items-table Name column is editable, so the user can
  // rename inline. (We don't prompt() because WKWebView's default UI
  // delegate config silently swallows it.)
  const defaultName = `concat ${selectedClips.length} clips`;
  const name = defaultName;
  pushUndo();
  const id = nextId++;
  // Snapshot the clip data — if a referenced clip is later edited or
  // deleted, the concat item still has its captured time ranges. The
  // user can re-create the concat if they want to incorporate edits.
  const segments = selectedClips.map(it => ({
    start: it.start,
    end:   it.end,
    crop:  it.crop || null,
  }));
  const totalDur = segments.reduce((s, x) => s + Math.max(0, x.end - x.start), 0);
  const it = {
    id, kind: "concat",
    name: name || defaultName,
    segments,
    container: "mp4-web",
    quality: defaultQuality,
    thumb: "",
  };
  items.push(it);
  // Use the first segment's start frame as the visual thumbnail.
  it.thumb = await captureFromMain(segments[0].start, 200);
  renderItems(); persistItems();
  refreshAddClipButton();
}
document.getElementById("btn-concat-selected").addEventListener("click", concatSelectedClips);

async function loadInitialState() {
  try {
    const r = await fetch("/editor/state?sid=" + encodeURIComponent(SID));
    if (!r.ok) return;
    const j = await r.json();
    if (j.default_quality) defaultQuality = j.default_quality;
    if (Array.isArray(j.items) && j.items.length) {
      let maxId = 0;
      for (const raw of j.items) {
        const it = {
          id: +raw.id || (++maxId),
          kind: raw.kind,
          name: raw.name || `${raw.kind} ${(+raw.id) || (++maxId)}`,
          thumb: raw.thumb || "",
          // normalizeQuality maps legacy "source" → "best" so old sessions
          // render correctly with the new option list.
          quality: raw.quality ? normalizeQuality(raw.quality) : defaultQuality,
          crop: (raw.crop && raw.crop.w && raw.crop.h) ? raw.crop : null,
        };
        if (raw.kind === "clip") {
          it.start = +raw.start || 0;
          it.end = +raw.end || 0;
          it.container = raw.container || "mp4";
        } else if (raw.kind === "still") {
          it.t = +raw.t || 0;
          it.format = raw.format || "jpeg";
        } else if (raw.kind === "concat") {
          it.segments = Array.isArray(raw.segments)
            ? raw.segments.map(s => ({
                start: +s.start || 0,
                end: +s.end || 0,
                crop: (s.crop && s.crop.w && s.crop.h) ? s.crop : null,
              }))
            : [];
          it.container = raw.container || "mp4-web";
        } else continue;
        if (it.id > maxId) maxId = it.id;
        items.push(it);
      }
      nextId = maxId + 1;
      renderItems();
      // Re-capture missing thumbs lazily in the background.
      for (const it of items) {
        if (it.thumb) continue;
        const t = it.kind === "clip" ? it.start : it.t;
        captureFromMain(t, 200).then((url) => {
          if (url) { it.thumb = url; renderItems(); persistItems(); }
        });
      }
    }
    // Restore markers if the session has any.
    if (Array.isArray(j.markers)) {
      let maxMid = 0;
      for (const raw of j.markers) {
        const id = +raw.id || 0;
        const t  = +raw.t || 0;
        const lbl = String(raw.label || "Marker");
        markers.push({ id, t, label: lbl });
        if (id > maxMid) maxMid = id;
      }
      _markerNextId = maxMid + 1;
      renderMarkers();
    } else {
      // Empty state — render the placeholder.
      renderMarkers();
    }
  } catch (e) {}
}

async function finishEditor() {
  // Make one last persist pass synchronously — main page only needs the
  // counts (everything else lives server-side keyed by sid).
  if (persistTimer) { clearTimeout(persistTimer); persistTimer = null; }
  // If the user just added a clip/still and immediately clicked Done,
  // captureFromMain() may still be in flight — wait for the queue to
  // drain so the items in `items` get their `thumb` data URI populated
  // BEFORE we snapshot for the main page.
  try { await mainBusy; } catch (e) {}
  // Belt-and-braces: if anything still has no thumb (e.g. the request
  // failed), kick off a fresh fetch and wait briefly. Don't block forever.
  const missing = items.filter(it => !it.thumb);
  if (missing.length) {
    await Promise.race([
      Promise.all(missing.map(async it => {
        try {
          const t = it.kind === "clip" ? it.start : it.t;
          it.thumb = await captureFromMain(t || 0, 200);
        } catch (e) {}
      })),
      new Promise(r => setTimeout(r, 2500)),
    ]);
  }
  const clipCount = items.filter(x => x.kind === "clip").length;
  const stillCount = items.filter(x => x.kind === "still").length;
  const concatCount = items.filter(x => x.kind === "concat").length;
  const itemsForMain = items.map(it => ({
    id: it.id, kind: it.kind, name: it.name,
    start: it.kind === "clip" ? it.start : null,
    end:   it.kind === "clip" ? it.end : null,
    t:     it.kind === "still" ? it.t : null,
    container: (it.kind === "clip" || it.kind === "concat")
               ? (it.container || "mp4-web") : null,
    format:    it.kind === "still" ? (it.format || "jpeg") : null,
    quality:   it.quality || "source",
    thumb:     it.thumb || "",
    crop:      it.crop || null,
    segments:  it.kind === "concat" ? (it.segments || []) : null,
  }));
  const payload = {
    sid: SID,
    title: TITLE,
    filenameHint: FILENAME_HINT,
    clipCount, stillCount, concatCount,
    items: itemsForMain,
  };
  // Persist final state, then notify main + close.
  fetch("/editor/items", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({sid: SID, items: itemsForMain}),
  }).catch(() => {}).finally(() => {
    const mh = window.webkit && window.webkit.messageHandlers;
    if (mh && mh.vdEditorComplete) {
      mh.vdEditorComplete.postMessage(payload);
    } else {
      window.close();
    }
  });
}

renderItems();
refreshTimeline();
loadInitialState().finally(() => {
  // Belt-and-braces: kick a filmstrip build after initial state lands.
  // If duration is already known we get an instant build; if not, the
  // self-retry loop in buildFilmstrip will keep polling until metadata
  // arrives (capped at 30s).
  refreshTimeline();
  buildFilmstrip(true);
});
</script>
</body>
</html>
"""


def find_port() -> int:
    for p in PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port in range")


# Where users can drop their own yt-dlp plugin packages (custom extractors
# for sites that aren't in the upstream yt-dlp). Each plugin lives in its
# own subfolder per yt-dlp's plugin spec. Created on launch so the path
# always exists for users to drop files into.
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Rip Raptor"
YTDLP_PLUGIN_DIR = APP_SUPPORT / "yt-dlp-plugins"
try:
    YTDLP_PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


# ───── Rip history ───────────────────────────────────────────────────
# Persistent log of every successful download. Covers full-file rips
# (yt-dlp + gallery-dl) AND editor-derived outputs (clips, stills,
# concats). The "url" field on editor-derived rows points at the parent
# source URL — clicking re-rip from history re-opens the source so the
# user can re-edit; we pair this with editor-state.json (URL-keyed
# selection persistence) so the re-opened editor restores their prior
# clips/stills/markers.
HISTORY_PATH = APP_SUPPORT / "history.json"
HISTORY_MAX = 200       # cap entries so the file doesn't grow forever
_history_lock = threading.Lock()


def _history_load() -> list:
    """Read history from disk. Tolerates missing/corrupt file by
    returning an empty list."""
    with _history_lock:
        try:
            if not HISTORY_PATH.exists():
                return []
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []


def _history_save(entries: list) -> None:
    """Atomic-ish write — temp file + replace — so a crash mid-write
    doesn't corrupt the JSON."""
    with _history_lock:
        try:
            APP_SUPPORT.mkdir(parents=True, exist_ok=True)
            tmp = HISTORY_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(entries[-HISTORY_MAX:], f, ensure_ascii=False, indent=2)
            os.replace(tmp, HISTORY_PATH)
        except Exception:
            pass


def history_record(*, title: str, url: str, file_path: str,
                   container: str = "", height=None, audio_only: bool = False,
                   thumbnail: str = "") -> None:
    """Append a successful rip to history. Fields beyond the basics are
    optional — caller passes whatever it has."""
    if not file_path:
        return
    try:
        size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    except Exception:
        size = 0
    entry = {
        "id":         uuid.uuid4().hex[:12],
        "ts":         time.time(),
        "title":      (title or "").strip() or os.path.basename(file_path),
        "url":        url or "",
        "file_path":  file_path,
        "container":  container or "",
        "height":     height,
        "audio_only": bool(audio_only),
        "thumbnail":  thumbnail or "",
        "size":       size,
    }
    items = _history_load()
    items.append(entry)
    _history_save(items)


def history_remove(entry_id: str) -> bool:
    items = _history_load()
    new_items = [e for e in items if e.get("id") != entry_id]
    if len(new_items) == len(items):
        return False
    _history_save(new_items)
    return True


# ───── Editor selection persistence ─────────────────────────────────────
# Keyed by source URL (page_url, falling back to src_url / url depending
# on session kind). When the user opens the editor on a URL they've
# edited before — same machine, same app, same URL — we restore their
# previous items / markers / default_quality so they don't have to mark
# in/out points again. Persists across app restarts.
EDITOR_STATE_PATH = APP_SUPPORT / "editor-state.json"
EDITOR_STATE_MAX = 100  # cap entries; oldest by saved_at fall off
_editor_state_lock = threading.Lock()


def _editor_state_key(sess: dict) -> str:
    """Stable URL key for an editor session. Uses the user-visible page
    URL whenever available so re-pasting the same link finds the same
    saved selections; falls back to the resolved src_url if there's no
    page context (rare — direct .mp4 paste with no referer)."""
    raw = (sess.get("page_url") or sess.get("src_url")
           or sess.get("url") or "").strip()
    if not raw:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(raw)
        # Drop fragment + trailing slash; lowercase scheme/host. Query
        # string is preserved because YouTube / similar use ?v= as the
        # actual identity. Fragment is always purely client-side state.
        path = (p.path or "").rstrip("/") or "/"
        out = f"{p.scheme.lower()}://{p.netloc.lower()}{path}"
        if p.query:
            out += f"?{p.query}"
        return out
    except Exception:
        return raw


def _editor_state_load() -> dict:
    with _editor_state_lock:
        try:
            if not EDITOR_STATE_PATH.exists():
                return {}
            with open(EDITOR_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _editor_state_save(states: dict) -> None:
    with _editor_state_lock:
        try:
            APP_SUPPORT.mkdir(parents=True, exist_ok=True)
            # Cap to most-recent N (LRU by saved_at). Without this, a
            # power user editing dozens of URLs grows the file
            # unboundedly; 100 is generous for a beta.
            keep = sorted(
                states.items(),
                key=lambda kv: (kv[1] or {}).get("saved_at", 0),
                reverse=True,
            )[:EDITOR_STATE_MAX]
            tmp = EDITOR_STATE_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(keep), f, ensure_ascii=False, indent=2)
            os.replace(tmp, EDITOR_STATE_PATH)
        except Exception:
            pass


def _editor_state_record(sess: dict) -> None:
    """Persist the session's items/markers/default_quality keyed by URL.
    Called from /editor/items so every save round-trips through disk."""
    key = _editor_state_key(sess)
    if not key:
        return
    states = _editor_state_load()
    states[key] = {
        "items":           sess.get("items", []),
        "markers":         sess.get("markers", []),
        "default_quality": sess.get("default_quality", "best"),
        "title":           sess.get("title", ""),
        "filename_hint":   sess.get("filename_hint", ""),
        "saved_at":        time.time(),
    }
    _editor_state_save(states)


def _editor_state_recall(sess: dict) -> None:
    """Restore items/markers/default_quality from disk into a fresh
    session. Called from /editor/start before responding so the editor
    HTML page that's about to render sees the prior state."""
    key = _editor_state_key(sess)
    if not key:
        return
    prev = _editor_state_load().get(key)
    if not isinstance(prev, dict):
        return
    if isinstance(prev.get("items"), list):
        sess["items"] = prev["items"]
    if isinstance(prev.get("markers"), list):
        sess["markers"] = prev["markers"]
    dq = prev.get("default_quality")
    if isinstance(dq, str) and dq:
        sess["default_quality"] = dq


# ───── yt-dlp version awareness ──────────────────────────────────────
# Track installed yt-dlp version + latest available, so the UI can show
# "update available" without forcing it. The bg auto-update below still
# runs (it's the safety net for users who never click the button), but
# the explicit notification gives users agency.
_ytdlp_version_cache = {"installed": None, "latest": None,
                        "checked": 0, "lock": threading.Lock()}


def _ytdlp_installed_version() -> str:
    """Return the installed yt-dlp's version string or '' on failure."""
    try:
        res = subprocess.run([YT_DLP, "--version"],
                             capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    return ""


def _ytdlp_latest_version() -> str:
    """Hit GitHub API for the latest yt-dlp release tag. Returns '' on
    network failure — we don't want a startup hang if GitHub is slow."""
    try:
        from urllib.request import Request, urlopen
        req = Request("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
                      headers={"User-Agent": "RipRaptor"})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # tag_name like "2024.12.13" or "2024.12.13.232319"
        return (data.get("tag_name") or "").strip().lstrip("v")
    except Exception:
        return ""


def _parse_ytdlp_version(s: str) -> tuple:
    """Parse a yt-dlp version string into a comparable tuple of ints.

    yt-dlp uses date-based version numbers but with multiple variants:
        "2026.03.17"             ← GitHub release tag
        "2026.3.17"              ← un-zero-padded
        "2026.03.17.232108"      ← stable + build suffix
        "2026.3.17.232108.dev0"  ← pipx pre-release / nightly
        "v2026.03.17"            ← occasionally tagged with leading v

    Returns a tuple like (2026, 3, 17, 232108) so older < newer compares
    correctly. Returns () on any parse failure (caller treats that as
    'unknown', skips the comparison)."""
    s = (s or "").strip().lstrip("v")
    if not s:
        return ()
    # Drop trailing .dev0 / .rc1 / .post3 etc — keep only the leading
    # numeric.dotted portion.
    head = re.split(r"[^\d.]", s, maxsplit=1)[0]
    nums = []
    for part in head.split("."):
        if not part:
            continue
        try:
            nums.append(int(part))
        except ValueError:
            break
    return tuple(nums)


def _ytdlp_update_available(installed: str, latest: str) -> bool:
    """True iff `latest` (GitHub tag) is strictly newer than `installed`
    (yt-dlp --version output). Naive string comparison fails because the
    same release shows up as e.g. 2026.3.17 on GitHub vs
    2026.03.17.232108.dev0 from pipx — different padding, extra build
    suffix. Numeric-tuple comparison is the only reliable way."""
    iv = _parse_ytdlp_version(installed)
    lv = _parse_ytdlp_version(latest)
    if not iv or not lv:
        return False  # if either side is unparseable, don't nag the user
    # Compare on the YYYY.MM.DD prefix only — the trailing build number
    # (4th component) varies between pipx pre-releases and tagged
    # stables for the same calendar release. We don't want to flag
    # "update available" for a build-number difference within the same
    # date.
    return iv[:3] < lv[:3]


# ───── Rip Raptor self-update check ────────────────────────────────────
# Same shape as the yt-dlp version cache. Polled at most every 6h to
# stay under GitHub API's 60-req/h unauthenticated rate limit.
_app_version_cache = {
    "lock":        threading.Lock(),
    "installed":   APP_VERSION,
    "latest":      "",
    "release_url": "",
    "checked":     0.0,
}


def _app_latest_release() -> tuple[str, str]:
    """Hit GitHub releases/latest for henri-cmd/ripraptor. Returns
    (tag_without_leading_v, html_url) or ('', '') on failure. We swallow
    network errors — there's no useful UX in surfacing 'GitHub unreachable'
    on the status bar; we just won't show the pill."""
    try:
        from urllib.request import Request, urlopen
        req = Request(
            "https://api.github.com/repos/henri-cmd/ripraptor/releases/latest",
            headers={"User-Agent": "RipRaptor/" + APP_VERSION})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = (data.get("tag_name") or "").strip().lstrip("v")
        url = (data.get("html_url") or "").strip()
        return tag, url
    except Exception:
        return "", ""


# ───── In-app self-update ─────────────────────────────────────────────
# Lets the user upgrade Rip Raptor without re-downloading the dmg by
# hand, mounting it, dragging into Applications, etc. Flow:
#
#   1. /app/install_update creates a job and runs _app_install_worker
#      in the background. Worker downloads the latest .dmg from GitHub,
#      mounts it via hdiutil, ditto's the new .app to a /tmp staging
#      dir, and writes a small bash helper script that knows how to
#      swap the bundle once we exit. Worker streams JSONL events to
#      /events/<job_id> the same way HLS fetches do.
#   2. Once the worker emits {type: "ready"}, the user clicks
#      "Relaunch & Update". The frontend POSTs /app/install_update/apply
#      which spawns the helper script detached from this process, then
#      POSTs /quit to shut down the server.
#   3. The Swift host quits, the helper script (now reparented to init)
#      sees our PID die, sleeps a moment, atomically swaps the bundle,
#      and `open`s the new one.
#
# This works because macOS lets you delete/replace running app bundles
# (unlike Windows). The helper just needs to wait for the user-visible
# process to fully exit so the relaunch gets a clean state.

_app_install_lock = threading.Lock()
_app_install_state: dict = {
    "helper": "",   # path to staged bash helper script
    "staged": "",   # path to new .app waiting in /tmp
    "bundle": "",   # path to existing install (the swap target)
}


def _locate_bundle_root() -> Path | None:
    """Find the path to the running .app bundle, if we're inside one.
    Walks up from this module's path looking for a *.app component.
    Returns None when running from the dev source tree (no .app)."""
    here = Path(__file__).resolve()
    for p in [here, *here.parents]:
        if p.suffix == ".app":
            return p
    return None


def _latest_dmg_asset() -> tuple[str, str, int]:
    """Return (download_url, asset_name, size_bytes) for the most
    recent .dmg asset on GitHub releases/latest. ('', '', 0) on
    failure or when the latest release has no dmg."""
    try:
        from urllib.request import Request, urlopen
        req = Request(
            "https://api.github.com/repos/henri-cmd/ripraptor/releases/latest",
            headers={"User-Agent": "RipRaptor/" + APP_VERSION})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for a in (data.get("assets") or []):
            n = (a.get("name") or "").lower()
            if n.endswith(".dmg"):
                return (a.get("browser_download_url") or "",
                        a.get("name") or "",
                        int(a.get("size") or 0))
    except Exception:
        pass
    return "", "", 0


def _app_install_worker(jid: str) -> None:
    """Background worker for in-app self-update. Posts events to the
    job queue the same way HLS fetcher events flow. Final event is
    either {type: 'ready'} (helper staged, awaiting apply call) or
    {type: 'error', error: '...'}.
    """
    import shlex
    import tempfile
    import textwrap

    with jobs_lock:
        job = jobs.get(jid)
    if not job:
        return
    q: Queue = job["queue"]

    def post(**kw):
        q.put(kw)

    mountpoint = None
    try:
        bundle = _locate_bundle_root()
        if not bundle:
            post(type="error", error="not running from a .app bundle (dev mode)")
            return
        if not bundle.exists():
            post(type="error", error=f"bundle path missing: {bundle}")
            return

        # ───── Resolve dmg URL ────────────────────────────────────
        post(type="status", msg="checking latest release")
        dmg_url, dmg_name, dmg_size = _latest_dmg_asset()
        if not dmg_url:
            post(type="error", error="no .dmg asset found on latest GitHub release")
            return

        # ───── Download dmg ───────────────────────────────────────
        post(type="status", msg=f"downloading {dmg_name}")
        from urllib.request import Request, urlopen
        fd, tmp_dmg_path = tempfile.mkstemp(prefix="ripraptor-update-", suffix=".dmg")
        os.close(fd)
        try:
            req = Request(dmg_url,
                          headers={"User-Agent": "RipRaptor/" + APP_VERSION})
            with urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length") or dmg_size or 0)
                got = 0
                last_percent = -1
                with open(tmp_dmg_path, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            pct = int(got / total * 100)
                            # Throttle progress events — one per percent
                            # is plenty and keeps the SSE stream cheap.
                            if pct != last_percent:
                                last_percent = pct
                                post(type="progress", got=got, total=total,
                                     percent=got / total * 100.0)
        except Exception as e:
            post(type="error", error=f"download failed: {e}")
            return

        # ───── Mount via hdiutil ──────────────────────────────────
        post(type="status", msg="mounting update")
        mountpoint = tempfile.mkdtemp(prefix="ripraptor-update-mnt-")
        try:
            res = subprocess.run(
                ["hdiutil", "attach", "-nobrowse", "-noverify", "-quiet",
                 "-mountpoint", mountpoint, tmp_dmg_path],
                capture_output=True, text=True, timeout=60)
            if res.returncode != 0:
                post(type="error",
                     error=f"mount failed: {(res.stderr or res.stdout).strip()[-200:]}")
                return
        except Exception as e:
            post(type="error", error=f"mount failed: {e}")
            return

        # ───── Locate the new .app inside the dmg ─────────────────
        new_app = None
        try:
            for entry in os.listdir(mountpoint):
                if entry.endswith(".app"):
                    new_app = Path(mountpoint) / entry
                    break
        except Exception:
            pass
        if not new_app:
            post(type="error", error="no .app found inside disk image")
            return

        # ───── Stage to /tmp via ditto ────────────────────────────
        # `cp -R` doesn't preserve resource forks / xattrs cleanly on
        # signed bundles; ditto is what installers use. Stage to a
        # fresh dir so the swap is a single rename.
        post(type="status", msg="copying new app")
        stage_dir = Path(tempfile.mkdtemp(prefix="ripraptor-update-stg-"))
        staged = stage_dir / new_app.name
        try:
            res = subprocess.run(
                ["ditto", str(new_app), str(staged)],
                capture_output=True, text=True, timeout=180)
            if res.returncode != 0:
                post(type="error",
                     error=f"copy failed: {(res.stderr or res.stdout).strip()[-200:]}")
                return
        except Exception as e:
            post(type="error", error=f"copy failed: {e}")
            return

        # Detach the dmg now that we've copied out — leaving it
        # mounted just adds a stray volume in Finder.
        try:
            subprocess.run(["hdiutil", "detach", mountpoint, "-force", "-quiet"],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        mountpoint = None
        try:
            os.unlink(tmp_dmg_path)
        except Exception:
            pass

        # ───── Write the swap-and-relaunch helper ─────────────────
        # We wait on the Swift host's PID — that's the user-facing
        # process; we (Python) are its child and exit when it does.
        # `kill -0` is a presence check (signal 0 doesn't actually
        # send a signal). Sleep loop caps at 30s; macOS lets us
        # replace running bundles regardless, so we proceed even
        # if the wait times out.
        parent_pid = os.getppid()
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        log_path = APP_SUPPORT / "update.log"
        helper_fd, helper_path = tempfile.mkstemp(
            prefix="ripraptor-update-helper-", suffix=".sh")
        os.close(helper_fd)
        helper_script = textwrap.dedent(f"""\
            #!/bin/bash
            # Rip Raptor self-update helper. Spawned detached from the
            # running app; waits for the app to exit, then swaps the
            # installed bundle for the freshly-staged copy and
            # relaunches it.
            set -u
            LOG={shlex.quote(str(log_path))}
            STAGED={shlex.quote(str(staged))}
            INSTALL={shlex.quote(str(bundle))}
            PARENT_PID={parent_pid}
            {{
              echo ""
              echo "[$(date)] update helper starting (parent pid $PARENT_PID)"
              for i in $(seq 1 60); do
                if ! kill -0 "$PARENT_PID" 2>/dev/null; then
                  break
                fi
                sleep 0.5
              done
              # Small grace period in case launch services has not
              # fully released file handles in the bundle yet.
              sleep 0.8
              echo "[$(date)] swapping $INSTALL <- $STAGED"
              # Use ditto for the swap so xattrs/codesign survive.
              # The previous bundle is deleted *after* the new copy
              # lands, in case the copy fails midway (we'd rather
              # leave the old one in place than have nothing).
              if ditto "$STAGED" "$INSTALL.new"; then
                rm -rf "$INSTALL"
                mv "$INSTALL.new" "$INSTALL"
                # Strip the quarantine xattr so Gatekeeper doesn't
                # re-prompt for the brand-new bundle the user just
                # consented to install.
                xattr -dr com.apple.quarantine "$INSTALL" 2>/dev/null || true
                echo "[$(date)] swap complete; relaunching"
                # rm leftover stage dir
                rm -rf "$(dirname "$STAGED")" 2>/dev/null || true
                # And the helper itself, on a delay (we're still
                # executing it — can't unlink while running).
                (sleep 2; rm -f "$0") &
                open "$INSTALL"
              else
                echo "[$(date)] copy failed; leaving existing install in place"
                open "$INSTALL"
              fi
              echo "[$(date)] done"
            }} >> "$LOG" 2>&1
        """)
        with open(helper_path, "w") as f:
            f.write(helper_script)
        os.chmod(helper_path, 0o755)

        with _app_install_lock:
            _app_install_state["helper"] = helper_path
            _app_install_state["staged"] = str(staged)
            _app_install_state["bundle"] = str(bundle)

        post(type="ready", msg="ready to relaunch")
    except Exception as e:
        post(type="error", error=str(e))
    finally:
        # Best-effort cleanup of the mountpoint if we errored before
        # detaching above.
        if mountpoint:
            try:
                subprocess.run(["hdiutil", "detach", mountpoint, "-force", "-quiet"],
                               capture_output=True, timeout=20)
            except Exception:
                pass
        q.put({"type": "_close"})


def _app_install_apply() -> dict:
    """Spawn the staged update helper detached from this process.
    Returns {ok, error?}. Caller is expected to follow up with /quit
    to shut down the running app — the helper waits for our parent
    PID to die before doing the swap."""
    with _app_install_lock:
        helper = _app_install_state.get("helper")
    if not helper or not os.path.exists(helper):
        return {"ok": False, "error": "no staged update — call /app/install_update first"}
    try:
        # start_new_session=True puts the helper in its own process
        # group so it survives our shutdown. close_fds prevents it
        # from inheriting file descriptors that would otherwise pin
        # them open. DEVNULL on stdio so it has no terminal to die on.
        subprocess.Popen(
            ["/bin/bash", helper],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _parse_semver(s: str) -> tuple:
    """Parse a semver-ish version string into a comparable tuple.
    Accepts '0.1.0', 'v0.1.0', '0.1', '0.1.0-beta1' (suffix dropped).
    Returns () on parse failure (caller treats as 'unknown')."""
    s = (s or "").strip().lstrip("v")
    if not s:
        return ()
    head = re.split(r"[^\d.]", s, maxsplit=1)[0]
    nums = []
    for part in head.split("."):
        if not part:
            continue
        try:
            nums.append(int(part))
        except ValueError:
            break
    return tuple(nums)


def _app_update_available(installed: str, latest: str) -> bool:
    """True iff `latest` is strictly newer than `installed` by semver
    comparison. Both unparseable → False (don't nag the user)."""
    iv = _parse_semver(installed)
    lv = _parse_semver(latest)
    if not iv or not lv:
        return False
    return iv < lv


def _app_check_versions(force: bool = False) -> dict:
    """Refresh the in-memory cache of latest GitHub release. 6h TTL
    unless forced. Always returns a snapshot dict."""
    with _app_version_cache["lock"]:
        now = time.time()
        if not force and (now - _app_version_cache["checked"]) < 6 * 3600 \
           and _app_version_cache["latest"]:
            return {
                "installed":   APP_VERSION,
                "latest":      _app_version_cache["latest"],
                "release_url": _app_version_cache["release_url"],
                "checked":     _app_version_cache["checked"],
            }
        latest, url = _app_latest_release()
        _app_version_cache["latest"] = latest
        _app_version_cache["release_url"] = url
        _app_version_cache["checked"] = now
        return {
            "installed":   APP_VERSION,
            "latest":      latest,
            "release_url": url,
            "checked":     now,
        }


def _ytdlp_check_versions(force: bool = False) -> dict:
    """Refresh the in-memory cache of installed + latest versions. Cache
    TTL is 6h unless forced. Returns a snapshot dict."""
    with _ytdlp_version_cache["lock"]:
        now = time.time()
        if not force and (now - _ytdlp_version_cache["checked"]) < 6 * 3600 \
           and _ytdlp_version_cache["installed"]:
            return {
                "installed": _ytdlp_version_cache["installed"],
                "latest":    _ytdlp_version_cache["latest"],
                "checked":   _ytdlp_version_cache["checked"],
            }
        installed = _ytdlp_installed_version()
        latest = _ytdlp_latest_version()
        _ytdlp_version_cache["installed"] = installed
        _ytdlp_version_cache["latest"] = latest
        _ytdlp_version_cache["checked"] = now
        return {"installed": installed, "latest": latest, "checked": now}


def _ytdlp_update_blocking(timeout: int = 240) -> dict:
    """User-triggered update via pipx. Blocks until done or times out
    so the UI can show a definitive success/failure. Returns
    {ok, message, new_version}."""
    if not shutil.which("pipx"):
        return {"ok": False, "message": "pipx not found — install with `brew install pipx`"}
    try:
        res = subprocess.run(["pipx", "upgrade", "yt-dlp"],
                             capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            return {"ok": False, "message": (res.stderr or res.stdout or "").strip()[-400:]}
        # Bust the cache + re-read the installed version.
        _ytdlp_version_cache["checked"] = 0
        snap = _ytdlp_check_versions(force=True)
        return {"ok": True, "message": (res.stdout or "").strip()[-400:],
                "new_version": snap.get("installed", "")}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "pipx upgrade timed out"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _maybe_update_yt_dlp() -> None:
    """Background pipx upgrade once per 24h. yt-dlp ships fixes daily and
    a stale binary is the #1 cause of 'this URL stopped working' reports —
    keeping it fresh transparently is one of the best things we can do."""
    if not shutil.which("pipx"):
        return
    stamp = APP_SUPPORT / "ytdlp_last_update"
    try:
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        if stamp.exists() and (time.time() - stamp.stat().st_mtime) < 24 * 3600:
            return
    except Exception:
        pass
    try:
        # Run silently. If a new version comes out we'll just have it on
        # the next launch — nothing user-facing to interrupt.
        subprocess.run(["pipx", "upgrade", "yt-dlp"],
                       capture_output=True, timeout=120)
        try: stamp.touch()
        except Exception: pass
    except Exception:
        pass


def main():
    if not Path(YT_DLP).exists():
        print(f"yt-dlp not found at {YT_DLP}", file=sys.stderr); sys.exit(1)
    threading.Thread(target=_maybe_update_yt_dlp, daemon=True).start()
    port = find_port()
    server = ThreadingServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}"
    print(f"Video Downloader running at {url}")
    print("Close this Terminal window or press Ctrl-C to quit.")
    if not os.environ.get("VIDEODOWNLOADER_EMBEDDED"):
        threading.Thread(target=lambda: (time.sleep(0.4), webbrowser.open(url)), daemon=True).start()
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        while not shutdown_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    server.shutdown()


if __name__ == "__main__":
    main()
