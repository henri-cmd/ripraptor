#!/usr/bin/env python3
"""HLS segment downloader using curl_cffi with chrome120 impersonation.

Reads a JSON job spec from stdin, streams JSONL progress events to stdout.
Spec:
  {
    "manifest_text": "...",          // optional — fetched if empty
    "manifest_url":  "https://...m3u8",
    "page_url":      "https://...",
    "cookies":       [{"name", "value", "domain", "path"}, ...],
    "out_path":      "/tmp/vd-xxxx.ts",
    "ffmpeg_path":   "/path/to/ffmpeg" // optional; required for separate-audio mux
  }
Emits one JSON object per line:
  {"type":"status","msg":"..."}
  {"type":"variant","video":{...},"audio":{...}}   // diagnostics
  {"type":"progress","idx":N,"total":M,"percent":P}
  {"type":"done"}
  {"type":"error","error":"..."}
"""
# Defer annotation evaluation so PEP 604 unions (`X | None`) parse on
# Python 3.9 (Apple's stock CLT Python on macOS 12-15).
from __future__ import annotations

import concurrent.futures
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlparse

from curl_cffi import requests as cr

# Tunable: how many segments to fetch in parallel inside a single batch.
# Most CDNs handle 8-16 simultaneous fetches fine; some throttle past that.
PARALLEL_SEGMENTS = 8


_emit_lock = threading.Lock()


def emit(obj):
    line = json.dumps(obj) + "\n"
    with _emit_lock:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass


def cookies_for(url, all_cookies):
    host = (urlparse(url).hostname or "").lower()
    out = {}
    for c in all_cookies:
        d = (c.get("domain") or "").lstrip(".").lower()
        if not d or host == d or host.endswith("." + d):
            out[c["name"]] = c["value"]
    return out


# ───── Master playlist parser ──────────────────────────────────────────
# HLS master playlists list video variants (#EXT-X-STREAM-INF) and
# alternative renditions (#EXT-X-MEDIA: TYPE=AUDIO/SUBTITLES/CLOSED-CAPTIONS).
# Per the spec, a video variant declares which audio group it consumes via
# the AUDIO="<group-id>" attribute. The matching #EXT-X-MEDIA entries can
# either:
#   - Have a URI    → audio is a *separate* playlist; we must fetch it and
#                     mux against the video stream during finalize.
#   - Omit URI      → audio is *muxed* into the video segments; nothing
#                     extra to fetch.
#
# Modern CDNs heavily prefer separate audio so a single video variant can
# back multiple audio tracks (English stereo, English 5.1, foreign-language,
# descriptive). If we ignore audio groups (as we did pre-0.1.8), we pick up
# whichever audio happens to be muxed-in by default and silently miss the
# higher-bitrate / surround / preferred-language alternatives.

_ATTR_RE = re.compile(r'\s*([A-Z0-9-]+)\s*=')


def _parse_attrs(s: str) -> dict:
    """Parse the attribute list of an #EXT-X tag.
    Examples:
      'BANDWIDTH=6280000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"'
      'TYPE=AUDIO,GROUP-ID="aac",NAME="English",DEFAULT=YES,LANGUAGE="en",URI="audio_en/index.m3u8"'
    Quoted values may contain commas; unquoted values terminate at the next comma.
    """
    out = {}
    i = 0
    n = len(s)
    while i < n:
        m = _ATTR_RE.match(s, i)
        if not m:
            break
        key = m.group(1)
        i = m.end()
        if i < n and s[i] == '"':
            j = s.find('"', i + 1)
            if j == -1:
                break
            out[key] = s[i + 1:j]
            i = j + 1
        else:
            j = i
            while j < n and s[j] != ',':
                j += 1
            out[key] = s[i:j]
            i = j
        # Skip the separator + whitespace
        while i < n and s[i] in ' ,':
            i += 1
    return out


def parse_master(text: str, base: str):
    """Parse a master playlist into video variants + audio groups.
    Returns (variants, audio_groups) where:
      variants: list of dicts sorted by (height DESC, bandwidth DESC).
        Each dict has: height, bandwidth, codecs, audio_group, uri.
      audio_groups: dict {group_id: [{language, channels, default,
        autoselect, name, uri, group_id}]}.
    """
    variants = []
    audio_groups: dict = {}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attrs(s[len("#EXT-X-MEDIA:"):])
            if (attrs.get("TYPE") or "").upper() != "AUDIO":
                continue
            group_id = attrs.get("GROUP-ID") or ""
            uri = attrs.get("URI") or ""
            entry = {
                "group_id":    group_id,
                "language":    attrs.get("LANGUAGE") or "",
                "name":        attrs.get("NAME") or "",
                "default":     (attrs.get("DEFAULT") or "NO").upper() == "YES",
                "autoselect":  (attrs.get("AUTOSELECT") or "NO").upper() == "YES",
                "channels":    attrs.get("CHANNELS") or "",
                "uri":         urljoin(base, uri) if uri else "",
            }
            audio_groups.setdefault(group_id, []).append(entry)
        elif s.startswith("#EXT-X-STREAM-INF:"):
            attrs = _parse_attrs(s[len("#EXT-X-STREAM-INF:"):])
            uri = ""
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and not nxt.startswith("#"):
                    uri = nxt
            h = 0
            mr = re.search(r"\d+x(\d+)", attrs.get("RESOLUTION") or "")
            if mr:
                h = int(mr.group(1))
            try:
                bw = int(attrs.get("BANDWIDTH") or "0")
            except ValueError:
                bw = 0
            variants.append({
                "height":       h,
                "bandwidth":    bw,
                "codecs":       attrs.get("CODECS") or "",
                "audio_group":  attrs.get("AUDIO") or "",
                "uri":          urljoin(base, uri) if uri else "",
            })
    variants.sort(key=lambda v: (v["height"], v["bandwidth"]), reverse=True)
    return variants, audio_groups


def pick_best_audio(audio_groups: dict, group_id: str,
                    prefer_lang: str = "en") -> dict | None:
    """Pick the highest-quality audio rendition from a group.
    Preference order:
      1. Language match (English / no-language / others, in that order)
      2. Channels (5.1 > 5.0 > stereo)
      3. Default-flagged renditions
      4. List order (CDN's intended default)

    Returns the chosen entry dict (with `uri` already absolutized via the
    master's base) or None if there's no matching group / it's empty / it
    contains only muxed-into-video entries (no URI).
    """
    if not group_id:
        return None
    group = audio_groups.get(group_id) or []
    # Filter to entries that actually have a URI — the URI-less ones are
    # just labels for muxed-in audio; nothing to fetch.
    with_uri = [a for a in group if a.get("uri")]
    if not with_uri:
        return None

    pref = (prefer_lang or "").lower()

    def lang_rank(a):
        l = (a.get("language") or "").lower()
        if pref and (l == pref or l == pref + "g" or pref == l + "g"):
            return 0  # exact match (e.g. "en"/"eng")
        if l in ("", "und"):
            return 1  # no tag — usually the original
        return 2

    def channels_int(a):
        c = (a.get("channels") or "").split("/")[0].strip()
        try:
            return int(c)
        except ValueError:
            return 2  # assume stereo when unspecified

    scored = sorted(with_uri, key=lambda a: (
        lang_rank(a),
        -channels_int(a),
        0 if a.get("default") else 1,
    ))
    return scored[0]


def parse_segments(text: str, base: str):
    """Parse a media playlist (the per-variant or per-audio m3u8) into
    (init_url_or_None, [segment_url, ...])."""
    init_url = None
    segs = []
    for raw in text.splitlines():
        t = raw.strip()
        if not t:
            continue
        if t.startswith("#EXT-X-MAP:"):
            m = re.search(r'URI="([^"]+)"', t)
            if m:
                init_url = urljoin(base, m.group(1))
        elif not t.startswith("#"):
            segs.append(urljoin(base, t))
    return init_url, segs


_thread_local = threading.local()


def _thread_session():
    """Each worker thread gets its own curl_cffi Session — sharing one
    across threads can crash because the underlying CURL handle isn't
    thread-safe across concurrent requests."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = cr.Session()
        _thread_local.session = s
    return s


def fetch(session, url, page_url, cookies, *, attempts=2, max_backoff=1.0):
    """Single-shot fetch with bounded retries.

    NB: this CDN's Cloudflare config 403s any request that bears a
    Referer (likely an anti-hotlink rule). Real Safari's <video>
    element omits Referer for cross-origin media fetches by default.
    We do the same. Origin/Sec-Fetch-* only matter for browser CORS
    preflight which curl_cffi doesn't do, so we skip them too.

    The defaults (attempts=2, max_backoff=1s) are tuned for the *main*
    parallel pass: 1 retry then bail, so a flaky segment costs at most
    ~1s of wall time inside the executor. Failed segments aren't dead —
    they get queued for a sequential retry pass after the main fan-out
    completes (see main()), where we use much more generous timing.
    """
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    ck = cookies_for(url, cookies)
    profiles = ["safari17_0", "chrome131"]
    last_err = None
    last_resp = None
    for i in range(attempts):
        prof = profiles[min(i, len(profiles) - 1)]
        try:
            r = session.get(
                url, headers=headers, cookies=ck,
                impersonate=prof, timeout=60,
            )
            if r.status_code in (200, 206):
                return r
            last_resp = r
            # Hard 4xx — don't retry; same response on second try.
            if r.status_code in (400, 404, 410):
                return r
            # 5xx, 401/403/429/451 — retry with a fresh TLS profile.
        except Exception as e:
            last_err = e
        time.sleep(min(max_backoff, 0.5 * (2 ** i)) + random.uniform(0, 0.15))
    if last_resp is not None:
        return last_resp
    raise last_err if last_err else RuntimeError("fetch failed (unknown)")


def diag_for(url, page_url, origin, cookies, response):
    host = urlparse(url).hostname or ""
    ck = cookies_for(url, cookies)
    all_domains = sorted({(c.get("domain") or "").lstrip(".") for c in cookies})
    body_snippet = ""
    try:
        body_snippet = (response.text or "")[:200].replace("\n", " ")
    except Exception:
        pass
    keep = ("server", "cf-ray", "cf-cache-status", "x-cache", "via",
            "content-type", "www-authenticate", "x-amz-cf-id", "x-amz-cf-pop")
    resp_headers = {k: v for k, v in response.headers.items() if k.lower() in keep}
    return (
        f"\n  host={host} sent_cookies={list(ck.keys())}"
        f"\n  all_cookie_domains={all_domains}"
        f"\n  referer={page_url} origin={origin}"
        f"\n  resp_headers={resp_headers}"
        f"\n  body={body_snippet!r}"
    )


def _humanize_bw(bw: int) -> str:
    if bw >= 1_000_000:
        return f"{bw / 1_000_000:.1f} Mbps"
    if bw >= 1_000:
        return f"{bw / 1_000:.0f} kbps"
    return f"{bw} bps"


def _segment_extension(seg_urls: list) -> str:
    """Best-guess file extension for a list of segment URLs. Falls back
    to .ts which is what most CDNs serve. Used to name the temporary
    per-stream file so ffmpeg's container probe doesn't get confused."""
    for u in seg_urls[:5]:
        path = urlparse(u).path.lower()
        for ext in (".m4s", ".mp4", ".aac", ".ts"):
            if path.endswith(ext):
                return ext
    return ".ts"


def _download_playlist_to_file(
    session,
    playlist_text: str,
    playlist_base: str,
    page_url: str,
    cookies: list,
    out_path: str,
    *,
    label: str,
    progress_total_ref: list,    # mutable [video_count, audio_count, done_count]
    progress_lock: threading.Lock,
    deferred_jobs: list,         # appended-to: (label, idx, url)
    error_holder: list,          # [first_error_or_None]
    cancel_flag: threading.Event,
    executor: concurrent.futures.ThreadPoolExecutor,
    origin: str,
):
    """Download all segments of a media playlist into `out_path`. Returns
    a (futures_list, in_order_writer) tuple — the caller drives the
    completion loop and writes results in order.

    NB: progress is reported globally across both video and audio streams
    via progress_total_ref so the user sees one unified percentage.
    """
    # This implementation is slightly unusual — instead of returning the
    # full result, we set up state and return functions/lists that the
    # caller's main as_completed loop drives. That keeps both streams
    # interleaved through a single executor + a single status stream.
    raise NotImplementedError("inlined into main() for clarity")


def main():
    spec = json.loads(sys.stdin.read())
    manifest_text = spec.get("manifest_text") or ""
    manifest_url = spec["manifest_url"]
    page_url = spec.get("page_url") or manifest_url
    cookies = spec.get("cookies") or []
    out_path = spec["out_path"]
    ffmpeg_path = (spec.get("ffmpeg_path") or "").strip()

    try:
        session = cr.Session()
        text = manifest_text
        base = manifest_url
        p = urlparse(page_url)
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""

        # ───── Manifest ───────────────────────────────────────────────
        if not text:
            emit({"type": "status", "msg": "fetching playlist"})
            try:
                r = fetch(session, manifest_url, page_url, cookies)
            except Exception as e:
                emit({"type": "error", "error": f"playlist fetch failed: {e}"})
                return
            if r.status_code != 200:
                emit({"type": "error",
                      "error": f"playlist HTTP {r.status_code}"
                      + diag_for(manifest_url, page_url, origin, cookies, r)})
                return
            text = r.text

        # ───── Variant selection ──────────────────────────────────────
        chosen_audio = None  # entry from EXT-X-MEDIA, or None for muxed
        if "#EXT-X-STREAM-INF" in text:
            variants, audio_groups = parse_master(text, manifest_url)
            if not variants:
                emit({"type": "error", "error": "master playlist has no variants"})
                return

            v = variants[0]
            chosen_audio = pick_best_audio(audio_groups, v["audio_group"])

            # Diagnostic event so the caller (and the user) can see what
            # we picked. JS surfaces this in the status pill.
            emit({
                "type": "variant",
                "video": {
                    "height":     v["height"],
                    "bandwidth":  v["bandwidth"],
                    "codecs":     v["codecs"],
                },
                "audio": ({
                    "language":   chosen_audio.get("language"),
                    "channels":   chosen_audio.get("channels"),
                    "name":       chosen_audio.get("name"),
                    "separate":   True,
                } if chosen_audio else {"separate": False}),
                "ladder": [
                    {"height": x["height"], "bandwidth": x["bandwidth"],
                     "codecs": x["codecs"]}
                    for x in variants
                ],
                "audio_groups": {
                    g: [{"language": e["language"], "channels": e["channels"],
                         "name": e["name"], "default": e["default"],
                         "has_uri": bool(e["uri"])} for e in entries]
                    for g, entries in audio_groups.items()
                },
            })

            v_label = f"{v['height']}p"
            if v["bandwidth"]:
                v_label += f" @ {_humanize_bw(v['bandwidth'])}"
            emit({"type": "status", "msg": f"video: {v_label}"})
            if chosen_audio:
                a_label = chosen_audio.get("name") or "audio"
                if chosen_audio.get("language"):
                    a_label += f" ({chosen_audio['language']})"
                if chosen_audio.get("channels"):
                    a_label += f" · {chosen_audio['channels']}ch"
                emit({"type": "status",
                      "msg": f"audio: {a_label} (separate stream)"})

            # Fetch the variant's video playlist.
            try:
                r = fetch(session, v["uri"], page_url, cookies)
            except Exception as e:
                emit({"type": "error", "error": f"variant fetch failed: {e}"})
                return
            if r.status_code != 200:
                emit({"type": "error",
                      "error": f"variant HTTP {r.status_code}"
                      + diag_for(v["uri"], page_url, origin, cookies, r)})
                return
            text = r.text
            base = v["uri"]

        # ───── Video segments ─────────────────────────────────────────
        v_init_url, v_segs = parse_segments(text, base)
        if not v_segs:
            emit({"type": "error", "error": "no segments in video playlist"})
            return

        # ───── Audio segments (if separate) ───────────────────────────
        a_init_url = None
        a_segs: list = []
        if chosen_audio:
            try:
                r = fetch(session, chosen_audio["uri"], page_url, cookies)
            except Exception as e:
                emit({"type": "error",
                      "error": f"audio playlist fetch failed: {e}"})
                return
            if r.status_code != 200:
                emit({"type": "error",
                      "error": f"audio playlist HTTP {r.status_code}"
                      + diag_for(chosen_audio["uri"], page_url, origin, cookies, r)})
                return
            a_init_url, a_segs = parse_segments(r.text, chosen_audio["uri"])
            if not a_segs:
                emit({"type": "error",
                      "error": "audio playlist has no segments"})
                return
            if not ffmpeg_path or not os.path.exists(ffmpeg_path):
                # Without ffmpeg we can't mux video + separate audio.
                # Rather than silently dropping audio (the pre-0.1.8
                # behavior), surface the problem.
                emit({"type": "error",
                      "error": ("separate audio rendition selected but no "
                                "ffmpeg available to mux — pass "
                                "ffmpeg_path in the spec")})
                return

        total = len(v_segs) + len(a_segs)
        emit({"type": "status",
              "msg": (f"segments: video={len(v_segs)} audio={len(a_segs)}"
                      if a_segs else f"segments={len(v_segs)}")})

        # ───── Shared progress state ─────────────────────────────────
        emitted_progress = [0]
        progress_lock = threading.Lock()
        cancel_flag = threading.Event()

        def emit_progress():
            with progress_lock:
                emitted_progress[0] += 1
                idx = emitted_progress[0]
            emit({"type": "progress", "idx": idx, "total": total,
                  "percent": idx / total * 100.0})

        # fetch_one returns (kind, idx, data, err)
        # kind: "v" or "a" — which stream this segment belongs to
        # data: bytes on success, None on failure
        # err:  human-readable error string if failure
        def fetch_one(kind: str, i: int, seg_url: str):
            if cancel_flag.is_set():
                return kind, i, None, None
            try:
                ts = _thread_session()
                r = fetch(ts, seg_url, page_url, cookies)
                if r.status_code in (200, 206):
                    return kind, i, r.content, None
                err = (f"{kind}-segment {i+1} HTTP {r.status_code}"
                       + diag_for(seg_url, page_url, origin, cookies, r))
                return kind, i, None, err
            except Exception as e:
                return kind, i, None, f"{kind}-segment {i+1}: {e}"

        # Per-stream output paths. If we're not muxing (no separate
        # audio), v_out_path == out_path so the existing Swift pipeline
        # keeps working unchanged. If we are muxing, v_out_path and
        # a_out_path are temp files that ffmpeg consumes into out_path.
        v_ext = _segment_extension(v_segs)
        a_ext = _segment_extension(a_segs) if a_segs else ".ts"
        if a_segs:
            v_out_path = out_path + ".video" + v_ext
            a_out_path = out_path + ".audio" + a_ext
        else:
            v_out_path = out_path
            a_out_path = None

        try:
            v_file = open(v_out_path, "wb")
            a_file = open(a_out_path, "wb") if a_out_path else None

            # Init segments are written first.
            try:
                if v_init_url:
                    r = fetch(session, v_init_url, page_url, cookies)
                    if r.status_code not in (200, 206):
                        emit({"type": "error",
                              "error": f"video init HTTP {r.status_code}"
                              + diag_for(v_init_url, page_url, origin, cookies, r)})
                        return
                    v_file.write(r.content)
                if a_init_url and a_file:
                    r = fetch(session, a_init_url, page_url, cookies)
                    if r.status_code not in (200, 206):
                        emit({"type": "error",
                              "error": f"audio init HTTP {r.status_code}"
                              + diag_for(a_init_url, page_url, origin, cookies, r)})
                        return
                    a_file.write(r.content)

                v_pending: dict = {}; v_next = 0
                a_pending: dict = {}; a_next = 0
                deferred: list = []  # list of (kind, idx)

                ex = concurrent.futures.ThreadPoolExecutor(
                    max_workers=PARALLEL_SEGMENTS)
                try:
                    futures = []
                    for i, seg in enumerate(v_segs):
                        futures.append(ex.submit(fetch_one, "v", i, seg))
                    for i, seg in enumerate(a_segs):
                        futures.append(ex.submit(fetch_one, "a", i, seg))

                    for fut in concurrent.futures.as_completed(futures):
                        if cancel_flag.is_set():
                            break
                        try:
                            kind, i, data, err = fut.result()
                        except Exception as e:
                            cancel_flag.set()
                            emit({"type": "error",
                                  "error": f"worker crash: {e}"})
                            return
                        if data is not None:
                            if kind == "v":
                                v_pending[i] = data
                                emit_progress()
                                while v_next in v_pending:
                                    v_file.write(v_pending.pop(v_next))
                                    v_next += 1
                            else:
                                a_pending[i] = data
                                emit_progress()
                                while a_next in a_pending:
                                    a_file.write(a_pending.pop(a_next))
                                    a_next += 1
                        elif err is not None:
                            deferred.append((kind, i))
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)

                if cancel_flag.is_set():
                    emit({"type": "error", "error": "canceled"})
                    return

                # ───── Retry pass ─────────────────────────────────────
                # Same defer-and-retry strategy as 0.1.7, just split per
                # stream. Cooldown lets the upstream Cloudflare Worker
                # warm a fresh isolate; the sequential retry hits each
                # bad segment with a longer attempt budget.
                if deferred:
                    deferred.sort()
                    emit({"type": "status",
                          "msg": f"retrying {len(deferred)} flaky segment(s)"})
                    time.sleep(2.5)
                    for idx_pass, (kind, i) in enumerate(deferred):
                        seg_url = v_segs[i] if kind == "v" else a_segs[i]
                        try:
                            r = fetch(session, seg_url, page_url, cookies,
                                      attempts=4, max_backoff=2.0)
                        except Exception as e:
                            emit({"type": "error",
                                  "error": f"{kind}-segment {i+1} (retry): {e}"})
                            return
                        if r.status_code not in (200, 206):
                            emit({"type": "error",
                                  "error": (f"{kind}-segment {i+1} HTTP "
                                            f"{r.status_code} (after retry)")
                                  + diag_for(seg_url, page_url, origin,
                                             cookies, r)})
                            return
                        if kind == "v":
                            v_pending[i] = r.content
                            emit_progress()
                            while v_next in v_pending:
                                v_file.write(v_pending.pop(v_next))
                                v_next += 1
                        else:
                            a_pending[i] = r.content
                            emit_progress()
                            while a_next in a_pending:
                                a_file.write(a_pending.pop(a_next))
                                a_next += 1
                        if idx_pass < len(deferred) - 1:
                            time.sleep(0.3 + random.uniform(0, 0.3))
            finally:
                try: v_file.close()
                except Exception: pass
                if a_file:
                    try: a_file.close()
                    except Exception: pass

            # ───── Mux (only if we have separate audio) ───────────────
            if a_out_path:
                emit({"type": "status", "msg": "muxing audio + video"})
                # -c copy: stream-copy both video and audio (no
                # re-encode). -f mpegts: keep the output as MPEG-TS so
                # the existing Swift `canHlsStreamCopy` path works
                # unchanged. -bsf:a aac_adtstoasc: handle AAC ADTS-in-TS
                # which some CDNs serve; harmless for non-AAC.
                cmd = [
                    ffmpeg_path,
                    "-hide_banner", "-loglevel", "error",
                    "-y",
                    "-i", v_out_path,
                    "-i", a_out_path,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c", "copy",
                    "-f", "mpegts",
                    out_path,
                ]
                try:
                    res = subprocess.run(cmd, capture_output=True,
                                         text=True, timeout=600)
                except Exception as e:
                    emit({"type": "error", "error": f"mux subprocess: {e}"})
                    return
                if res.returncode != 0:
                    emit({"type": "error",
                          "error": ("mux failed: "
                                    + (res.stderr or res.stdout or "").strip()[-500:])})
                    return

                # Clean up the per-stream temp files. Best-effort —
                # leftover .video.ts / .audio.ts in /tmp is harmless.
                for p in (v_out_path, a_out_path):
                    try: os.unlink(p)
                    except Exception: pass

        except Exception as e:
            emit({"type": "error", "error": f"writer crashed: {e}"})
            return

        emit({"type": "done"})
    except Exception as e:
        emit({"type": "error", "error": str(e)})


if __name__ == "__main__":
    main()
