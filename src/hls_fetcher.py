#!/usr/bin/env python3
"""HLS segment downloader using curl_cffi with chrome120 impersonation.

Reads a JSON job spec from stdin, streams JSONL progress events to stdout.
Spec:
  {
    "manifest_text": "...",
    "manifest_url": "https://...m3u8",
    "page_url": "https://...",
    "cookies": [{"name", "value", "domain", "path"}, ...],
    "out_path": "/tmp/vd-xxxx.ts"
  }
Emits one JSON object per line:
  {"type":"status","msg":"..."}
  {"type":"progress","idx":N,"total":M,"percent":P}
  {"type":"done"}
  {"type":"error","error":"..."}
"""
# Defer annotation evaluation so PEP 604 unions (`X | None`) parse on
# Python 3.9 (Apple's stock CLT Python on macOS 12-15).
from __future__ import annotations

import concurrent.futures
import json
import random
import re
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


def parse_master(text, base):
    lines = text.splitlines()
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        bw = 0
        h = 0
        mb = re.search(r"BANDWIDTH=(\d+)", line)
        if mb:
            bw = int(mb.group(1))
        mr = re.search(r"RESOLUTION=\d+x(\d+)", line)
        if mr:
            h = int(mr.group(1))
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not nxt.startswith("#"):
                variants.append((h, bw, urljoin(base, nxt)))
    variants.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return variants


def parse_segments(text, base):
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


def main():
    spec = json.loads(sys.stdin.read())
    manifest_text = spec["manifest_text"]
    manifest_url = spec["manifest_url"]
    page_url = spec.get("page_url") or manifest_url
    cookies = spec.get("cookies") or []
    out_path = spec["out_path"]

    try:
        session = cr.Session()
        text = manifest_text
        base = manifest_url
        p = urlparse(page_url)
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""

        # If the caller didn't pre-cache the manifest body (e.g. user
        # picked a "live fetch" variant whose playlist wasn't captured
        # during sniff), fetch it now via curl_cffi with the player's
        # cookies/Cloudflare clearance still warm.
        if not text:
            emit({"type": "status", "msg": "fetching playlist"})
            try:
                r = fetch(session, manifest_url, page_url, cookies)
            except Exception as e:
                emit({"type": "error", "error": f"playlist fetch failed: {e}"})
                return
            if r.status_code != 200:
                emit({"type": "error",
                      "error": f"playlist HTTP {r.status_code}" + diag_for(manifest_url, page_url, origin, cookies, r)})
                return
            text = r.text

        if "#EXT-X-STREAM-INF" in text:
            variants = parse_master(text, manifest_url)
            if variants:
                h, _, vurl = variants[0]
                emit({"type": "status", "msg": f"variant {h}p"})
                r = fetch(session, vurl, page_url, cookies)
                if r.status_code != 200:
                    emit({"type": "error",
                          "error": f"variant HTTP {r.status_code}" + diag_for(vurl, page_url, origin, cookies, r)})
                    return
                text = r.text
                base = vurl

        init_url, segs = parse_segments(text, base)
        if not segs:
            emit({"type": "error", "error": "no segments in playlist"})
            return
        emit({"type": "status", "msg": f"segments={len(segs)}"})

        total = len(segs)
        emitted_progress = [0]
        progress_lock = threading.Lock()
        cancel_flag = threading.Event()

        def emit_progress():
            with progress_lock:
                emitted_progress[0] += 1
                idx = emitted_progress[0]
            emit({"type": "progress", "idx": idx, "total": total,
                  "percent": idx / total * 100.0})

        # ───── fetch_one: single-segment worker ────────────────────────
        # Returns (i, data, err):
        #   data is bytes on success, None on failure
        #   err is None on success or a human-readable string on failure
        # IMPORTANT: a failure here does *not* abort the run. Failed
        # segments get queued and retried sequentially after the main
        # parallel pass — the Cloudflare Worker behind these segments
        # often returns transient 500s under our parallel burst, but
        # the same URL succeeds 2-5 seconds later. Bailing immediately
        # turns a recoverable hiccup into a failed download; deferring
        # converts it back into a slight slowdown.
        def fetch_one(i, seg):
            if cancel_flag.is_set():
                return i, None, None
            try:
                ts = _thread_session()
                r = fetch(ts, seg, page_url, cookies)
                if r.status_code in (200, 206):
                    return i, r.content, None
                # Non-2xx — defer for the retry pass. Capture
                # diagnostics in case the retry also fails.
                err = (f"segment {i+1} HTTP {r.status_code}"
                       + diag_for(seg, page_url, origin, cookies, r))
                return i, None, err
            except Exception as e:
                return i, None, f"segment {i+1}: {e}"

        try:
            with open(out_path, "wb") as f:
                if init_url:
                    r = fetch(session, init_url, page_url, cookies)
                    if r.status_code not in (200, 206):
                        emit({"type": "error",
                              "error": f"init HTTP {r.status_code}" + diag_for(init_url, page_url, origin, cookies, r)})
                        return
                    f.write(r.content)

                pending = {}
                next_to_write = 0
                deferred = []  # list of segment indices that need retry
                last_err = None  # most-recent failure string for diagnostics

                ex = concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_SEGMENTS)
                try:
                    futures = [ex.submit(fetch_one, i, seg)
                               for i, seg in enumerate(segs)]
                    for fut in concurrent.futures.as_completed(futures):
                        if cancel_flag.is_set():
                            break
                        try:
                            i, data, err = fut.result()
                        except Exception as e:
                            # Unexpected — fetch_one should catch its own.
                            last_err = str(e)
                            cancel_flag.set()
                            break
                        if data is not None:
                            pending[i] = data
                            emit_progress()
                            # Drain in-order writes whenever the next
                            # expected index is now in pending.
                            while next_to_write in pending:
                                f.write(pending.pop(next_to_write))
                                next_to_write += 1
                        elif err is not None:
                            # Defer; don't abort. The retry pass below
                            # will surface a final error if it persists.
                            deferred.append(i)
                            last_err = err
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)

                if cancel_flag.is_set():
                    emit({"type": "error", "error": last_err or "canceled"})
                    return

                # ───── Retry pass ─────────────────────────────────────
                # Sequential, with cooldown between hits and a more
                # generous attempt budget. The Cloudflare Workers
                # serving these segments tend to recover within a few
                # seconds — a fresh-isolate retry usually succeeds.
                if deferred:
                    deferred.sort()
                    emit({"type": "status",
                          "msg": f"retrying {len(deferred)} flaky segment(s)"})
                    # Initial cooldown: give whichever upstream worker
                    # blew up time to fall out of cache and warm a new
                    # isolate. 2.5s is empirically enough; longer
                    # waits don't help.
                    time.sleep(2.5)
                    for idx_pass, i in enumerate(deferred):
                        seg = segs[i]
                        try:
                            # attempts=4 with max_backoff=2 → worst-case
                            # ~6s per segment on the retry pass. With a
                            # typical handful of deferred segments that's
                            # tens of seconds added in the absolute worst
                            # case — far better than failing the whole
                            # rip 80% of the way through.
                            r = fetch(session, seg, page_url, cookies,
                                      attempts=4, max_backoff=2.0)
                        except Exception as e:
                            emit({"type": "error",
                                  "error": f"segment {i+1} (retry): {e}"})
                            return
                        if r.status_code not in (200, 206):
                            emit({"type": "error",
                                  "error": f"segment {i+1} HTTP {r.status_code} (after retry)"
                                  + diag_for(seg, page_url, origin, cookies, r)})
                            return
                        pending[i] = r.content
                        emit_progress()
                        while next_to_write in pending:
                            f.write(pending.pop(next_to_write))
                            next_to_write += 1
                        # Small jitter between retry-pass segments so
                        # we don't burst the upstream worker again.
                        if idx_pass < len(deferred) - 1:
                            time.sleep(0.3 + random.uniform(0, 0.3))

                # Sanity: anything left in pending means an index hole,
                # which would mean we logically lost data. Shouldn't
                # happen with the deferred-retry pass succeeding.
                if pending:
                    missing = sorted(set(range(total)) - set(range(next_to_write)))
                    emit({"type": "error",
                          "error": f"internal: {len(missing)} segment(s) never written"})
                    return
        except Exception as e:
            emit({"type": "error", "error": f"writer crashed: {e}"})
            return

        emit({"type": "done"})
    except Exception as e:
        emit({"type": "error", "error": str(e)})


if __name__ == "__main__":
    main()
