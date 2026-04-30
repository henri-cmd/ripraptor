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
# Segments per write-batch. We download a batch in parallel, write it to
# disk in order, then move on. Bounds peak memory to roughly
# CHUNK_SEGMENTS × segment_size (~4 MB), so 50 × 4 = ~200 MB ceiling.
CHUNK_SEGMENTS = 50


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


def fetch(session, url, page_url, cookies, *, attempts=4):
    # NB: this CDN's Cloudflare config 403s any request that bears a
    # Referer (likely an anti-hotlink rule). Real Safari's <video>
    # element omits Referer for cross-origin media fetches by default.
    # We do the same. Origin/Sec-Fetch-* only matter for browser CORS
    # preflight which curl_cffi doesn't do, so we skip them too.
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    ck = cookies_for(url, cookies)
    # Try Safari first — same TLS fingerprint as the WKWebView that
    # established the player session, so CDNs treat us identically. If
    # that's somehow rejected (rare), fall back to Chrome variants.
    profiles = ["safari17_0", "safari18_0", "chrome131", "chrome120"]
    last_err = None
    for i in range(attempts):
        prof = profiles[min(i, len(profiles) - 1)]
        try:
            return session.get(
                url, headers=headers, cookies=ck,
                impersonate=prof, timeout=60,
            )
        except Exception as e:
            last_err = e
            time.sleep(min(8.0, 0.5 * (2 ** i)) + random.uniform(0, 0.3))
    raise last_err


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
        first_error = [None]
        error_lock = threading.Lock()

        def emit_progress():
            with progress_lock:
                emitted_progress[0] += 1
                idx = emitted_progress[0]
            emit({"type": "progress", "idx": idx, "total": total,
                  "percent": idx / total * 100.0})

        def fetch_one(i, seg):
            if cancel_flag.is_set():
                return i, None
            try:
                # Each worker uses its OWN curl_cffi session — sharing one
                # across threads with concurrent requests is unsafe.
                ts = _thread_session()
                r = fetch(ts, seg, page_url, cookies)
                if r.status_code not in (200, 206):
                    msg = f"segment {i+1} HTTP {r.status_code}" + diag_for(seg, page_url, origin, cookies, r)
                    raise RuntimeError(msg)
                return i, r.content
            except Exception as e:
                with error_lock:
                    if first_error[0] is None:
                        first_error[0] = str(e)
                cancel_flag.set()
                return i, None

        try:
            with open(out_path, "wb") as f:
                if init_url:
                    r = fetch(session, init_url, page_url, cookies)
                    if r.status_code not in (200, 206):
                        emit({"type": "error",
                              "error": f"init HTTP {r.status_code}" + diag_for(init_url, page_url, origin, cookies, r)})
                        return
                    f.write(r.content)

                ex = concurrent.futures.ThreadPoolExecutor(max_workers=PARALLEL_SEGMENTS)
                try:
                    futures = [ex.submit(fetch_one, i, seg) for i, seg in enumerate(segs)]
                    next_to_write = 0
                    pending = {}
                    for fut in concurrent.futures.as_completed(futures):
                        if cancel_flag.is_set() and first_error[0]:
                            # On the first error, drop all pending work and
                            # bail. Workers that haven't started yet will see
                            # cancel_flag and short-circuit; in-flight ones
                            # finish quickly because we don't wait for them.
                            break
                        try:
                            i, data = fut.result()
                        except Exception as e:
                            with error_lock:
                                if first_error[0] is None:
                                    first_error[0] = str(e)
                            cancel_flag.set()
                            break
                        if data is None:
                            continue
                        pending[i] = data
                        emit_progress()
                        while next_to_write in pending:
                            f.write(pending.pop(next_to_write))
                            next_to_write += 1
                finally:
                    ex.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            emit({"type": "error", "error": f"writer crashed: {e}"})
            return

        if first_error[0]:
            emit({"type": "error", "error": first_error[0]})
            return

        emit({"type": "done"})
    except Exception as e:
        emit({"type": "error", "error": str(e)})


if __name__ == "__main__":
    main()
