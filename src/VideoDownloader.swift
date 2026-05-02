import Cocoa
import WebKit

let BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

/// Locate one of the bundled tools we ship inside the .app
/// (Contents/Resources/bin/<name>). Falls back to Homebrew / system
/// paths so the dev source tree (no Resources/bin yet) still works.
func bundledBinary(_ name: String) -> String? {
    if let res = Bundle.main.resourcePath {
        let p = "\(res)/bin/\(name)"
        if FileManager.default.isExecutableFile(atPath: p) { return p }
    }
    return nil
}

func ffmpegPath() -> String {
    if let p = bundledBinary("ffmpeg") { return p }
    for p in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"] {
        if FileManager.default.isExecutableFile(atPath: p) { return p }
    }
    return "/opt/homebrew/bin/ffmpeg"
}

func ffprobePath() -> String {
    if let p = bundledBinary("ffprobe") { return p }
    for p in ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "/usr/bin/ffprobe"] {
        if FileManager.default.isExecutableFile(atPath: p) { return p }
    }
    return "/opt/homebrew/bin/ffprobe"
}

/// MM:SS formatter for the encode-progress status line. We deliberately
/// don't show milliseconds — the value updates twice a second already
/// and the precision is just visual noise at that pace.
func formatMMSS(_ sec: Double) -> String {
    if !sec.isFinite || sec < 0 { return "00:00" }
    let total = Int(sec)
    let m = total / 60
    let s = total % 60
    return String(format: "%02d:%02d", m, s)
}

/// Inspect codec and pixel/profile info on the first video + first audio
/// stream of a media file. Used by the smart-copy decision in
/// finishHLSDownload — if a source's HLS .ts already contains H.264
/// video + AAC audio (the overwhelming majority of cases), we can
/// stream-copy into MP4 in seconds instead of re-encoding for minutes.
struct HLSSourceCodecs {
    var videoCodec: String  = ""
    var audioCodec: String  = ""
    var videoProfile: String = ""
    var pixFmt: String      = ""
}

func ffprobeCodecs(input: String) -> HLSSourceCodecs {
    var out = HLSSourceCodecs()
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: ffprobePath())
    proc.arguments = [
        "-v", "error",
        "-show_streams",
        "-of", "json",
        input,
    ]
    let outPipe = Pipe()
    proc.standardOutput = outPipe
    proc.standardError = Pipe()
    do { try proc.run() } catch { return out }
    proc.waitUntilExit()
    let data = outPipe.fileHandleForReading.readDataToEndOfFile()
    guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let streams = json["streams"] as? [[String: Any]] else { return out }
    for s in streams {
        let kind = s["codec_type"] as? String ?? ""
        if kind == "video" && out.videoCodec.isEmpty {
            out.videoCodec   = s["codec_name"] as? String ?? ""
            out.videoProfile = (s["profile"] as? String ?? "").lowercased()
            out.pixFmt       = s["pix_fmt"] as? String ?? ""
        } else if kind == "audio" && out.audioCodec.isEmpty {
            out.audioCodec = s["codec_name"] as? String ?? ""
        }
    }
    return out
}

/// True iff the source can be stream-copied straight into an MP4 with
/// only the standard HLS bitstream filter (`aac_adtstoasc`) — meaning
/// video is H.264 and audio is either AAC or absent. This is true for
/// the vast majority of HLS streams; falling back to re-encode is the
/// safety net for the rare source that ships HEVC/Opus/etc.
func canHlsStreamCopy(input: String) -> Bool {
    let c = ffprobeCodecs(input: input)
    if c.videoCodec != "h264" { return false }
    if !c.audioCodec.isEmpty && c.audioCodec != "aac" { return false }
    return true
}

/// Probe a media file for its container duration (seconds). Used to
/// translate ffmpeg's `out_time_us=…` progress output into a percentage
/// during HLS finalization. Returns 0 on any failure — the UI degrades
/// gracefully to an indeterminate "Encoding…" status.
func ffprobeDuration(input: String) -> Double {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: ffprobePath())
    proc.arguments = [
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        input,
    ]
    let outPipe = Pipe()
    proc.standardOutput = outPipe
    proc.standardError = Pipe()
    do { try proc.run() } catch { return 0 }
    proc.waitUntilExit()
    let data = outPipe.fileHandleForReading.readDataToEndOfFile()
    let s = String(data: data, encoding: .utf8)?
        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return Double(s) ?? 0
}

/// Mirrors Python's safe_basename in app.py — replace any path-dangerous
/// or filesystem-reserved character with "_", trim leading/trailing
/// whitespace and dots, cap at 180 characters. Without this a card whose
/// user-visible title is a URL ends up writing to a fictitious path tree
/// like /Users/x/Downloads/https:/host.net/.../foo.mp4 because both
/// `appendingPathComponent` and ffmpeg's output writer interpret slashes
/// in the filename as directory separators.
func sanitizeBasename(_ s: String) -> String {
    let bad: Set<Character> = ["/", "\\", ":", "?", "*", "\"", "<", ">", "|"]
    var out = ""
    out.reserveCapacity(s.count)
    for c in s {
        if let v = c.asciiValue, v < 0x20 {
            out.append("_")
        } else if bad.contains(c) {
            out.append("_")
        } else {
            out.append(c)
        }
    }
    let trimmed = out.trimmingCharacters(in: CharacterSet(charactersIn: " ."))
    let capped = String(trimmed.prefix(180))
    return capped.isEmpty ? "video" : capped
}

func ffmpegArgsForHLS(format: String, input: String, base: String) -> (String, [String]) {
    // base is the destination path with no extension. We pick the extension
    // and ffmpeg encoding/copy args based on `format`.
    // -progress pipe:1 emits machine-readable key=value lines on stdout
    // every ~0.5s during the encode (out_time_us, frame, fps, etc.) so
    // we can render a real progress bar instead of just "Encoding…".
    // -nostats suppresses the human-readable bar that would otherwise
    // mix into stderr alongside actual error messages.
    let progressFlags = ["-progress", "pipe:1", "-nostats"]
    switch format {
    case "mp4-h264":
        // VideoToolbox H.264 — hardware-accelerated on Apple Silicon,
        // 5–10× faster than libx264 with negligible visual difference at
        // 8 Mbps. -allow_sw 1 falls back to a software encoder on any
        // input the HW path refuses (rare, but cheap insurance).
        let out = base + ".mp4"
        return (out, ["-y", "-loglevel", "error"] + progressFlags +
                     ["-fflags", "+genpts", "-i", input,
                      "-map", "0:v:0?", "-map", "0:a:0?",
                      "-c:v", "h264_videotoolbox", "-b:v", "8M", "-allow_sw", "1",
                      "-c:a", "aac", "-b:a", "192k",
                      "-movflags", "+faststart",
                      "-avoid_negative_ts", "make_zero",
                      out])
    case "mp4-web":
        // Web-safe preset constrained to Main@4.0 H.264 + AAC-LC at
        // 48 kHz stereo, yuv420p, CFR, faststart. VideoToolbox honors
        // -profile:v / -level so we keep the upload-service-friendly
        // bitstream characteristics while running on the HW encoder.
        // Roughly 5–10× faster than the previous libx264 path; if a
        // user runs into an upload validator that rejects the output
        // we can wire a fallback toggle, but that's rare in practice.
        let out = base + ".mp4"
        return (out, ["-y", "-loglevel", "error"] + progressFlags +
                     ["-fflags", "+genpts", "-i", input,
                      "-map", "0:v:0?", "-map", "0:a:0?",
                      "-c:v", "h264_videotoolbox",
                      "-profile:v", "main", "-level", "4.0",
                      "-b:v", "8M", "-allow_sw", "1",
                      "-pix_fmt", "yuv420p",
                      "-fps_mode", "cfr",
                      "-c:a", "aac", "-profile:a", "aac_low",
                      "-ar", "48000", "-ac", "2", "-b:a", "192k",
                      "-af", "aresample=async=1",
                      "-movflags", "+faststart",
                      "-avoid_negative_ts", "make_zero",
                      out])
    case "mkv":
        let out = base + ".mkv"
        return (out, ["-y", "-loglevel", "error"] + progressFlags +
                     ["-fflags", "+genpts", "-i", input,
                      "-c", "copy", "-avoid_negative_ts", "make_zero", out])
    case "mp3":
        let out = base + ".mp3"
        return (out, ["-y", "-loglevel", "error"] + progressFlags +
                     ["-i", input,
                      "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out])
    case "m4a":
        let out = base + ".m4a"
        return (out, ["-y", "-loglevel", "error"] + progressFlags +
                     ["-i", input,
                      "-vn", "-c:a", "aac", "-b:a", "192k",
                      "-movflags", "+faststart", out])
    default:
        // mp4: stream-copy, fix HLS-specific quirks for QuickTime.
        // aac_adtstoasc converts ADTS-framed AAC (HLS-TS) into MP4-compatible
        // AAC; without it, the audio track in the .mp4 won't play. faststart
        // moves moov to the front so QuickTime can open before the EOF.
        let out = base + ".mp4"
        return (out, ["-y", "-loglevel", "error"] + progressFlags +
                     ["-fflags", "+genpts", "-i", input,
                      "-map", "0:v:0?", "-map", "0:a:0?",
                      "-c", "copy", "-bsf:a", "aac_adtstoasc",
                      "-movflags", "+faststart",
                      "-avoid_negative_ts", "make_zero",
                      out])
    }
}

final class HLSDownload {
    let taskId: String
    let dest: String
    let filename: String
    let format: String
    let tsPath: String
    let webView: WKWebView
    let window: NSWindow?
    var handle: FileHandle?
    init(taskId: String, dest: String, filename: String, format: String, webView: WKWebView, window: NSWindow?) {
        self.taskId = taskId
        self.dest = dest
        self.filename = filename
        self.format = format
        self.webView = webView
        self.window = window
        let tmp = (NSTemporaryDirectory() as NSString).appendingPathComponent("vd-\(taskId).bin")
        self.tsPath = tmp
        FileManager.default.createFile(atPath: tmp, contents: nil)
        self.handle = FileHandle(forWritingAtPath: tmp)
    }
    func write(_ data: Data) { handle?.write(data) }
    func close() { try? handle?.close(); handle = nil }
    func cleanup() {
        close()
        try? FileManager.default.removeItem(atPath: tsPath)
        webView.stopLoading()
        window?.orderOut(nil)
    }
}

let HLS_DOWNLOADER_JS = """
function vdAbToB64(ab) {
  const bytes = new Uint8Array(ab);
  let s = '';
  const N = 32768;
  for (let i = 0; i < bytes.length; i += N) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + N));
  }
  return btoa(s);
}
window.__vdDoHlsDownload = async function(taskId, manifestText, manifestUrl) {
  const post = m => {
    try { window.webkit.messageHandlers.vdHlsChunk.postMessage(Object.assign({taskId}, m)); }
    catch(e) {}
  };
  try {
    let text = manifestText || '';
    let baseUrl = manifestUrl;
    // If the caller didn't pre-fetch the manifest, do it ourselves via the
    // WebView's network stack — that's the whole point of this path: we
    // pick up cf_clearance / signed-URL cookies that the player set during
    // sniff, which a curl_cffi subprocess can't easily replicate.
    if (!text && manifestUrl) {
      post({status: 'fetching manifest'});
      const r = await fetch(manifestUrl, {credentials:'omit', mode:'cors', cache:'no-store'});
      if (!r.ok) throw new Error('manifest ' + r.status);
      text = await r.text();
    }
    if (/^#EXT-X-STREAM-INF/m.test(text)) {
      const lines = text.split(/\\r?\\n/);
      const variants = [];
      for (let i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('#EXT-X-STREAM-INF')) {
          const bw = parseInt((lines[i].match(/BANDWIDTH=(\\d+)/)||[])[1]||'0');
          const res = lines[i].match(/RESOLUTION=(\\d+)x(\\d+)/);
          const h = res ? parseInt(res[2]) : 0;
          const next = (lines[i+1]||'').trim();
          if (next && !next.startsWith('#')) {
            variants.push({bw, h, url: new URL(next, baseUrl).toString()});
          }
        }
      }
      variants.sort((a,b) => (b.h - a.h) || (b.bw - a.bw));
      if (variants[0]) {
        baseUrl = variants[0].url;
        const r = await fetch(baseUrl, {credentials:'omit', mode:'cors', cache:'no-store'});
        if (!r.ok) throw new Error('variant ' + r.status);
        text = await r.text();
      }
    }
    let initUrl = null;
    const segs = [];
    for (const line of text.split(/\\r?\\n/)) {
      const t = line.trim();
      if (!t) continue;
      if (t.startsWith('#EXT-X-MAP:')) {
        const m = t.match(/URI="([^"]+)"/);
        if (m) initUrl = new URL(m[1], baseUrl).toString();
      } else if (!t.startsWith('#')) {
        segs.push(new URL(t, baseUrl).toString());
      }
    }
    post({status: 'segments=' + segs.length});
    if (initUrl) {
      const r = await fetch(initUrl, {credentials:'omit', mode:'cors', cache:'no-store'});
      if (!r.ok) throw new Error('init ' + r.status);
      post({chunk: vdAbToB64(await r.arrayBuffer())});
    }
    // Parallel fetch with ordered write-back. We keep up to PARALLEL
    // segments in flight; as each completes we stash its bytes by index
    // and drain in order so the .ts file stays sequential.
    const PARALLEL = 12;
    const total = segs.length;
    const buffered = new Map();
    let nextToPost = 0;
    let done = 0;
    let failed = null;
    const drainOrdered = () => {
      while (buffered.has(nextToPost)) {
        const data = buffered.get(nextToPost);
        buffered.delete(nextToPost);
        post({chunk: data});
        nextToPost++;
      }
    };
    let cursor = 0;
    const pumpOne = async (slot) => {
      while (cursor < total && !failed) {
        const i = cursor++;
        try {
          const r = await fetch(segs[i], {credentials:'omit', mode:'cors', cache:'no-store'});
          if (!r.ok) throw new Error('segment ' + i + ' ' + r.status);
          buffered.set(i, vdAbToB64(await r.arrayBuffer()));
          done++;
          post({progress: done / total * 100, idx: done, total});
          drainOrdered();
        } catch (e) {
          failed = e;
          throw e;
        }
      }
    };
    const workers = [];
    for (let w = 0; w < Math.min(PARALLEL, total); w++) {
      workers.push(pumpOne(w));
    }
    await Promise.all(workers);
    drainOrdered();
    post({done: true});
  } catch(e) {
    post({error: (e && e.message) || String(e)});
  }
};
"""

let SNIFF_JS = """
(function(){
  if (window.__vdHooked) return;
  window.__vdHooked = true;
  const seen = new Set();
  const isMedia = (u) => {
    if (!u || typeof u !== 'string') return false;
    if (/\\.(m3u8|mpd|mp4|webm|m4s|m4v|m4a|mov)(\\?|#|$)/i.test(u)) return true;
    if (/(manifest|playlist|master).*\\.(m3u8|mpd)/i.test(u)) return true;
    return false;
  };
  const flush = () => {
    try { window.webkit.messageHandlers.vdCapture.postMessage({urls: Array.from(seen), title: document.title || ''}); } catch(e){}
  };
  const triedExplicit = new Set();
  async function tryExplicitFetch(u) {
    if (triedExplicit.has(u) || m3u8s.has(u)) return;
    triedExplicit.add(u);
    // Give the teeing path a chance before paying for an extra request
    await new Promise(r => setTimeout(r, 1200));
    if (m3u8s.has(u)) return;
    try {
      const r = await fetch(u, {credentials:'omit', mode:'cors', cache:'no-store'});
      if (!r.ok) return;
      const text = await r.text();
      if (text && text.indexOf('#EXTM3U') !== -1) notePlaylist(u, text);
    } catch(e) {}
  }
  const add = (u) => {
    if (typeof u !== 'string') return;
    try { u = new URL(u, document.baseURI).toString(); } catch(e) {}
    if (isMedia(u) && !seen.has(u)) {
      seen.add(u); flush();
      // Native HLS via <video src> bypasses fetch/XHR — no tee possible.
      // Fall back to an explicit fetch so we can grab the manifest text.
      if (/\\.m3u8(\\?|#|$)/i.test(u)) tryExplicitFetch(u);
    }
  };
  // Tee an HLS playlist response captured from the player. We get the exact
  // bytes that already reached the page (no second request needed).
  const m3u8s = new Map(); // url -> text
  function notePlaylist(u, text) {
    if (!u || !text) return;
    if (text.indexOf('#EXTM3U') === -1) return;
    m3u8s.set(u, text);
    onPlaylistsUpdated();
  }
  function rewriteAbsolute(text, base) {
    return text.split(/\\r?\\n/).map(line => {
      const t = line.trim();
      if (!t || t.startsWith('#')) return line;
      try { return new URL(t, base).toString(); } catch(e) { return line; }
    }).join('\\n');
  }
  function parseVariantsFromMaster(masterText, masterUrl) {
    const lines = masterText.split(/\\r?\\n/);
    const variants = [];
    for (let i = 0; i < lines.length; i++) {
      if (lines[i].startsWith('#EXT-X-STREAM-INF')) {
        const bw = parseInt((lines[i].match(/BANDWIDTH=(\\d+)/)||[])[1]||'0');
        const res = lines[i].match(/RESOLUTION=(\\d+)x(\\d+)/);
        const h = res ? parseInt(res[2]) : 0;
        const next = (lines[i+1]||'').trim();
        if (next && !next.startsWith('#')) {
          variants.push({bandwidth: bw, height: h, url: new URL(next, masterUrl).toString()});
        }
      }
    }
    variants.sort((a,b) => (b.height - a.height) || (b.bandwidth - a.bandwidth));
    return variants;
  }
  function onPlaylistsUpdated() {
    let masterUrl = null, masterText = null;
    for (const [u, t] of m3u8s) {
      if (/^#EXT-X-STREAM-INF/m.test(t)) { masterUrl = u; masterText = t; break; }
    }
    const variants = masterText ? parseVariantsFromMaster(masterText, masterUrl) : [];
    // Build {url -> rewritten content} for every playlist we've teed.
    const contents = {};
    for (const [u, t] of m3u8s) contents[u] = rewriteAbsolute(t, u);
    if (masterText && masterUrl) {
      contents[masterUrl] = (function(){
        // Master needs variant URLs rewritten too (they may be relative)
        return masterText.split(/\\r?\\n/).map(line => {
          const t = line.trim();
          if (!t || t.startsWith('#')) return line;
          try { return new URL(t, masterUrl).toString(); } catch(e) { return line; }
        }).join('\\n');
      })();
    }
    try {
      window.webkit.messageHandlers.vdCapture.postMessage({
        playlists: { masterUrl, masterContent: contents[masterUrl] || null, variants, contents }
      });
    } catch(e) {}
  }
  function maybeTeeFetch(u, promise) {
    if (!u || !/\\.m3u8(\\?|#|$)/i.test(u)) return promise;
    return promise.then(resp => {
      try {
        if (resp && resp.ok) {
          const clone = resp.clone();
          clone.text().then(t => notePlaylist(u, t)).catch(()=>{});
        }
      } catch(e) {}
      return resp;
    });
  }
  const _fetch = window.fetch;
  if (_fetch) {
    window.fetch = function(input, init){
      let u;
      try { u = (typeof input === 'string') ? input : (input && input.url); } catch(e) { u = ''; }
      try { u = new URL(u, document.baseURI).toString(); } catch(e) {}
      add(u);
      return maybeTeeFetch(u, _fetch.apply(this, arguments));
    };
  }
  const _open = XMLHttpRequest.prototype.open;
  const _send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url){
    try { this.__vdUrl = new URL(url, document.baseURI).toString(); } catch(e) { this.__vdUrl = url; }
    add(this.__vdUrl);
    return _open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(){
    const xhr = this;
    if (xhr.__vdUrl && /\\.m3u8(\\?|#|$)/i.test(xhr.__vdUrl)) {
      xhr.addEventListener('load', () => {
        try {
          const rt = xhr.responseType;
          if (rt === '' || rt === 'text') notePlaylist(xhr.__vdUrl, xhr.responseText || '');
          else if (rt === 'arraybuffer' && xhr.response) {
            try {
              const t = new TextDecoder().decode(new Uint8Array(xhr.response));
              notePlaylist(xhr.__vdUrl, t);
            } catch(e) {}
          }
        } catch(e) {}
      });
    }
    return _send.apply(this, arguments);
  };
  const watch = (n) => {
    if (!n || n.nodeType !== 1) return;
    if (n.src) add(n.src);
    if (n.tagName === 'VIDEO' || n.tagName === 'AUDIO' || n.tagName === 'SOURCE') {
      if (n.currentSrc) add(n.currentSrc);
    }
  };
  const obs = new MutationObserver(muts => {
    for (const m of muts) {
      if (m.type === 'attributes') watch(m.target);
      for (const n of (m.addedNodes||[])) {
        watch(n);
        if (n.querySelectorAll) for (const e of n.querySelectorAll('video, source, audio, iframe')) watch(e);
      }
    }
  });
  obs.observe(document.documentElement || document, {childList:true, subtree:true, attributes:true, attributeFilter:['src','data-src']});
  setInterval(flush, 1500);
})();
"""

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var pythonProcess: Process?
    var loadedURL: URL?
    var startupBuffer = ""

    var editorWindows: [NSWindow] = []
    var editorWindowsBySid: [String: NSWindow] = [:]

    var sniffWindow: NSWindow?
    var sniffWebView: WKWebView?
    var sniffID: String = ""
    var sniffURL: String = ""
    var sniffURLs: Set<String> = []
    var sniffPageTitle: String = ""
    var sniffManifestContent: String = ""
    var sniffManifestVariantURL: String = ""
    var sniffPlaylists: [String: Any] = [:]
    var hlsDownloads: [String: HLSDownload] = [:]
    // Sniffs that have completed but whose WebView we keep alive for a
    // potential download. Keyed by sniffId.
    var sniffSessions: [String: (wv: WKWebView, win: NSWindow)] = [:]
    var sniffPageURLById: [String: String] = [:]
    var sniffContentsById: [String: [String: String]] = [:]
    var sniffMaxTimer: Timer?
    var sniffGraceTimer: Timer?
    var sniffProgressTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        createWindow()
        startServer()
    }

    @objc func showAboutPanel(_ sender: Any?) {
        // Standard panel + a custom credits attributed string. The
        // standard panel reads ApplicationName / Version / Copyright
        // from Info.plist; our credits paragraph adds the "Created by
        // Henri Scott" line in styled form below those.
        let credits = NSMutableAttributedString()
        let center = NSMutableParagraphStyle()
        center.alignment = .center
        credits.append(NSAttributedString(
            string: "Created by Henri Scott\n",
            attributes: [
                .font: NSFont.boldSystemFont(ofSize: 12),
                .foregroundColor: NSColor.labelColor,
                .paragraphStyle: center,
            ]))
        credits.append(NSAttributedString(
            string: "An internet video ripper for the modern web.",
            attributes: [
                .font: NSFont.systemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor,
                .paragraphStyle: center,
            ]))
        NSApp.orderFrontStandardAboutPanel(options: [
            .credits: credits,
        ])
    }

    func buildMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(title: "About Rip Raptor",
                                   action: #selector(showAboutPanel(_:)),
                                   keyEquivalent: ""))
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(NSMenuItem(title: "Hide Rip Raptor",
                                   action: #selector(NSApplication.hide(_:)),
                                   keyEquivalent: "h"))
        appMenu.addItem(NSMenuItem(title: "Quit Rip Raptor",
                                   action: #selector(NSApplication.terminate(_:)),
                                   keyEquivalent: "q"))
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(NSMenuItem(title: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = editMenu

        NSApp.mainMenu = mainMenu
    }

    func createWindow() {
        let frame = NSRect(x: 0, y: 0, width: 820, height: 720)
        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Rip Raptor"
        // Lock the minimum content size to the narrowest width the card
        // layout can comfortably handle — selects, subs checkbox, and
        // three buttons fit on one or two rows without clipping. Below
        // this, things would either wrap badly or get cut off.
        window.contentMinSize = NSSize(width: 640, height: 480)
        window.center()
        window.delegate = self
        window.setFrameAutosaveName("RipRaptorMain")

        let cfg = WKWebViewConfiguration()
        let uc = WKUserContentController()
        uc.add(self, name: "vdSniff")
        uc.add(self, name: "vdHlsStart")
        uc.add(self, name: "vdHlsChunk")
        uc.add(self, name: "vdEditorOpen")
        uc.add(self, name: "vdEditorPrepare")
        uc.add(self, name: "vdEditorComplete")
        cfg.userContentController = uc
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = false
        let webPrefs = WKWebpagePreferences()
        webPrefs.allowsContentJavaScript = true
        cfg.defaultWebpagePreferences = webPrefs

        webView = WKWebView(frame: window.contentView!.bounds, configuration: cfg)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        if #available(macOS 13.3, *) { webView.isInspectable = true }

        let placeholder = NSTextField(labelWithString: "Starting…")
        placeholder.font = .systemFont(ofSize: 13)
        placeholder.textColor = .secondaryLabelColor
        placeholder.alignment = .center
        placeholder.frame = NSRect(x: 0, y: window.contentView!.bounds.midY - 12,
                                   width: window.contentView!.bounds.width, height: 24)
        placeholder.autoresizingMask = [.width, .minYMargin, .maxYMargin]
        placeholder.tag = 999
        window.contentView?.addSubview(placeholder)
        window.contentView?.addSubview(webView)
        webView.isHidden = true

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func startServer() {
        guard let resPath = Bundle.main.resourcePath else { failStart("Bundle missing"); return }
        let appPy = (resPath as NSString).appendingPathComponent("app.py")
        guard FileManager.default.fileExists(atPath: appPy) else { failStart("app.py not found"); return }

        let python = firstExisting([
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ]) ?? "/usr/bin/python3"

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: python)
        proc.arguments = ["-u", appPy]
        let home = NSHomeDirectory()
        proc.environment = [
            "HOME": home,
            "PATH": "/opt/homebrew/bin:\(home)/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "VIDEODOWNLOADER_EMBEDDED": "1",
        ]

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            if data.isEmpty { return }
            guard let chunk = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                self?.startupBuffer += chunk
                self?.consumeStartup(chunk)
            }
        }

        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if self.loadedURL == nil {
                    // Show the user the actual reason Python died — last
                    // 8 non-empty lines of stdout/stderr, which usually
                    // contains the traceback or syntax error. Saves the
                    // user (and us) from blind "status 1" debugging.
                    let tail = self.startupBuffer
                        .split(whereSeparator: { $0 == "\n" || $0 == "\r" })
                        .map { $0.trimmingCharacters(in: .whitespaces) }
                        .filter { !$0.isEmpty }
                        .suffix(8)
                        .joined(separator: "\n")
                    let detail = tail.isEmpty
                        ? "Server exited (status \(p.terminationStatus))"
                        : "Server exited (status \(p.terminationStatus)):\n\n\(tail)"
                    self.failStart(detail)
                } else {
                    // Server died after we'd already loaded the UI. With
                    // a clean exit (status 0) this is the user-initiated
                    // /quit path — could be the in-app updater asking us
                    // to step out so a helper can swap the bundle, or
                    // could just be the app shutting down from the
                    // sidebar quit button. Either way, terminate the
                    // host app: there's no functioning backend left, so
                    // staying open with a broken WebView is the worst
                    // possible UX.
                    //
                    // Non-zero exits are a Python crash mid-session;
                    // those are rare enough that we don't try to auto-
                    // relaunch — let the user see the empty webview and
                    // file a bug.
                    if p.terminationStatus == 0 {
                        NSApp.terminate(nil)
                    }
                }
            }
        }

        do {
            try proc.run()
            pythonProcess = proc
        } catch {
            failStart("Could not start Python: \(error.localizedDescription)")
        }
    }

    func consumeStartup(_ chunk: String) {
        // Buffer is appended in the readabilityHandler before this is
        // called so the terminationHandler can dump it on failure. We
        // still keep the WebView load on first URL match.
        if loadedURL != nil { return }
        if let range = startupBuffer.range(of: #"http://127\.0\.0\.1:\d+"#, options: .regularExpression) {
            let urlStr = String(startupBuffer[range])
            if let url = URL(string: urlStr) { load(url) }
        }
    }

    func load(_ url: URL) {
        loadedURL = url
        webView.load(URLRequest(url: url))
        webView.isHidden = false
        if let v = window.contentView?.viewWithTag(999) { v.removeFromSuperview() }
    }

    func failStart(_ msg: String) {
        // Multi-line errors (e.g. Python tracebacks captured from stderr)
        // can't fit in the single-line placeholder text field. Show an
        // NSAlert so the user actually sees the diagnostic.
        let multiline = msg.contains("\n")
        if !multiline, let v = window.contentView?.viewWithTag(999) as? NSTextField {
            v.stringValue = "Couldn't start: \(msg)"
            v.textColor = .systemRed
            return
        }
        // Show single-line summary in the placeholder too, so the user
        // can see SOMETHING even if they dismissed the alert.
        if let v = window.contentView?.viewWithTag(999) as? NSTextField {
            v.stringValue = "Couldn't start (see dialog)"
            v.textColor = .systemRed
        }
        let alert = NSAlert()
        alert.messageText = "Could not start Rip Raptor"
        alert.informativeText = msg
        alert.runModal()
    }

    func firstExisting(_ paths: [String]) -> String? {
        paths.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    // MARK: - Sniff

    func userContentController(_ uc: WKUserContentController, didReceive msg: WKScriptMessage) {
        if msg.name == "vdSniff" {
            guard let body = msg.body as? [String: Any],
                  let id = body["id"] as? String,
                  let url = body["url"] as? String else { return }
            startSniff(id: id, url: url)
        } else if msg.name == "vdCapture" {
            guard let body = msg.body as? [String: Any] else { return }
            if let arr = body["urls"] as? [String] {
                let before = sniffURLs.count
                for u in arr { sniffURLs.insert(u) }
                if sniffURLs.count > before { startGraceTimerIfNeeded() }
            }
            if let t = body["title"] as? String, !t.isEmpty {
                sniffPageTitle = t
            }
            if let m = body["manifest"] as? [String: Any] {
                if let c = m["content"] as? String { sniffManifestContent = c }
                if let v = m["variantUrl"] as? String { sniffManifestVariantURL = v }
                startGraceTimerIfNeeded()
            }
            if let p = body["playlists"] as? [String: Any] {
                sniffPlaylists = p
                if let contents = p["contents"] as? [String: String], !sniffID.isEmpty {
                    sniffContentsById[sniffID] = contents
                }
                startGraceTimerIfNeeded()
            }
        } else if msg.name == "vdHlsStart" {
            guard let b = msg.body as? [String: Any],
                  let taskId = b["taskId"] as? String,
                  let sniffId = b["sniffId"] as? String,
                  let manifestText = b["manifestContent"] as? String,
                  let manifestUrl = b["manifestUrl"] as? String,
                  let dest = b["dest"] as? String,
                  let filename = b["filename"] as? String else { return }
            let format = (b["format"] as? String) ?? "mp4"
            startHLSDownload(taskId: taskId, sniffId: sniffId,
                             manifestText: manifestText,
                             manifestUrl: manifestUrl, dest: dest,
                             filename: filename, format: format)
        } else if msg.name == "vdHlsChunk" {
            guard let b = msg.body as? [String: Any],
                  let taskId = b["taskId"] as? String,
                  let dl = hlsDownloads[taskId] else { return }
            if let b64 = b["chunk"] as? String, let data = Data(base64Encoded: b64) {
                dl.write(data)
            }
            if let pct = b["progress"] as? Double {
                let idx = b["idx"] as? Int ?? 0
                let total = b["total"] as? Int ?? 0
                notifyHls("__vdHlsProgress", taskId: taskId,
                          extra: ["percent": pct, "idx": idx, "total": total])
            }
            if let s = b["status"] as? String {
                notifyHls("__vdHlsStatus", taskId: taskId, extra: ["status": s])
            }
            if (b["done"] as? Bool) == true {
                finishHLSDownload(dl: dl)
            }
            if let err = b["error"] as? String {
                failHLSDownload(dl: dl, error: err)
            }
        } else if msg.name == "vdEditorOpen" {
            guard let b = msg.body as? [String: Any],
                  let sid = b["sid"] as? String, !sid.isEmpty else { return }
            let title = (b["title"] as? String) ?? "Editor"
            openEditorWindow(sid: sid, title: title)
        } else if msg.name == "vdEditorPrepare" {
            guard let b = msg.body as? [String: Any] else { return }
            prepareEditor(body: b)
        } else if msg.name == "vdEditorComplete" {
            guard let b = msg.body as? [String: Any] else { return }
            completeEditor(body: b)
        }
    }

    func completeEditor(body: [String: Any]) {
        let sid = (body["sid"] as? String) ?? ""
        // Forward the whole payload to the main webview so it can render
        // the items as rip cards. Use JSONSerialization → JSON literal in
        // the eval, which sidesteps escaping pitfalls with data: URLs.
        if let data = try? JSONSerialization.data(withJSONObject: body, options: []),
           let json = String(data: data, encoding: .utf8) {
            let js = "window.__vdEditorReceiveItems && window.__vdEditorReceiveItems(\(json))"
            webView.evaluateJavaScript(js, completionHandler: nil)
        }
        // Close the editor window for this sid (if any).
        if !sid.isEmpty, let win = editorWindowsBySid[sid] {
            DispatchQueue.main.async { win.close() }
        }
    }

    func prepareEditor(body: [String: Any]) {
        let kind = (body["kind"] as? String) ?? "hls"
        let sniffId = (body["sniffId"] as? String) ?? ""
        let title = (body["title"] as? String) ?? ""
        let filenameHint = (body["filenameHint"] as? String) ?? ""
        let pageURL = (body["pageUrl"] as? String) ?? ""
        let replyToken = (body["replyToken"] as? String) ?? ""
        let defaultQuality = (body["defaultQuality"] as? String) ?? ""

        // Pull cookies from the sniff session's WebView so we get the
        // CDN/anti-bot cookies the player picked up.
        let store: WKHTTPCookieStore
        if let session = sniffSessions[sniffId] {
            store = session.wv.configuration.websiteDataStore.httpCookieStore
        } else {
            store = WKWebsiteDataStore.default().httpCookieStore
        }
        store.getAllCookies { [weak self] cookies in
            guard let self = self else { return }
            let cookieDicts: [[String: String]] = cookies.map { c in
                ["name": c.name, "value": c.value, "domain": c.domain, "path": c.path]
            }
            var spec: [String: Any] = [
                "kind": kind,
                "page_url": pageURL,
                "cookies": cookieDicts,
                "title": title,
                "filename_hint": filenameHint,
            ]
            if !defaultQuality.isEmpty {
                spec["default_quality"] = defaultQuality
            }
            if kind == "hls" {
                spec["manifest_text"] = (body["manifestText"] as? String) ?? ""
                spec["manifest_url"]  = (body["manifestUrl"] as? String) ?? ""
            } else {
                spec["src_url"] = (body["srcUrl"] as? String) ?? ""
            }
            self.startEditorSession(spec: spec, title: title.isEmpty ? filenameHint : title, replyToken: replyToken)
        }
    }

    func startEditorSession(spec: [String: Any], title: String, replyToken: String) {
        guard let base = loadedURL,
              let endpoint = URL(string: "/editor/start", relativeTo: base),
              let data = try? JSONSerialization.data(withJSONObject: spec) else {
            replyEditorPrepare(token: replyToken, ok: false, error: "server unavailable")
            return
        }
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = data
        req.timeoutInterval = 60
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, err in
            guard let self = self else { return }
            if let err = err {
                DispatchQueue.main.async {
                    self.replyEditorPrepare(token: replyToken, ok: false, error: err.localizedDescription)
                }
                return
            }
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                DispatchQueue.main.async {
                    self.replyEditorPrepare(token: replyToken, ok: false, error: "bad response")
                }
                return
            }
            let http = (resp as? HTTPURLResponse)?.statusCode ?? 0
            if http != 200 {
                let msg = (json["error"] as? String) ?? "HTTP \(http)"
                DispatchQueue.main.async {
                    self.replyEditorPrepare(token: replyToken, ok: false, error: msg)
                }
                return
            }
            let sid = (json["sid"] as? String) ?? ""
            DispatchQueue.main.async {
                if !sid.isEmpty {
                    self.openEditorWindow(sid: sid, title: title)
                    self.replyEditorPrepare(token: replyToken, ok: true, error: "", sid: sid)
                } else {
                    self.replyEditorPrepare(token: replyToken, ok: false, error: "no sid", sid: "")
                }
            }
        }.resume()
    }

    func replyEditorPrepare(token: String, ok: Bool, error: String, sid: String = "") {
        if token.isEmpty { return }
        let safeToken = token.replacingOccurrences(of: "'", with: "")
        let safeErr = error.replacingOccurrences(of: "\\", with: "\\\\")
                          .replacingOccurrences(of: "'", with: "\\'")
        let safeSid = sid.replacingOccurrences(of: "'", with: "")
        let js = "window.__vdEditorPrepareReply && window.__vdEditorPrepareReply('\(safeToken)', \(ok ? "true" : "false"), '\(safeErr)', '\(safeSid)')"
        webView?.evaluateJavaScript(js, completionHandler: nil)
    }

    func openEditorWindow(sid: String, title: String) {
        guard let base = loadedURL else { return }
        var comps = URLComponents(url: base, resolvingAgainstBaseURL: false)
        comps?.path = "/editor"
        comps?.queryItems = [URLQueryItem(name: "sid", value: sid)]
        guard let url = comps?.url else { return }

        let frame = NSRect(x: 0, y: 0, width: 1100, height: 820)
        let win = NSWindow(contentRect: frame,
                           styleMask: [.titled, .closable, .miniaturizable, .resizable],
                           backing: .buffered, defer: false)
        win.title = "Rip Raptor — \(title)"
        win.center()
        win.isReleasedWhenClosed = false

        let cfg = WKWebViewConfiguration()
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = false
        let webPrefs = WKWebpagePreferences()
        webPrefs.allowsContentJavaScript = true
        cfg.defaultWebpagePreferences = webPrefs
        // The editor's "Done" button posts back to Swift through this
        // handler — it must be installed on the editor window's own
        // WKUserContentController, not the main window's.
        let uc = WKUserContentController()
        uc.add(self, name: "vdEditorComplete")
        cfg.userContentController = uc

        let wv = WKWebView(frame: win.contentView!.bounds, configuration: cfg)
        wv.autoresizingMask = [.width, .height]
        if #available(macOS 13.3, *) { wv.isInspectable = true }
        win.contentView?.addSubview(wv)

        editorWindows.append(win)
        editorWindowsBySid[sid] = win
        win.delegate = self
        win.makeKeyAndOrderFront(nil)
        wv.load(URLRequest(url: url))
    }

    func startHLSDownload(taskId: String, sniffId: String, manifestText: String,
                          manifestUrl: String, dest: String, filename: String, format: String) {
        NSLog("VD: startHLSDownload sniffId=%@ sessions=%@", sniffId, Array(sniffSessions.keys).joined(separator: ","))
        guard let session = sniffSessions[sniffId] else {
            let diag = "no downloader for sniff '\(sniffId)' (sessions=\(sniffSessions.keys.joined(separator: ",")))"
            notifyHls("__vdHlsError", taskId: taskId, extra: ["error": diag])
            return
        }
        let wv = session.wv
        let win = session.win

        let dl = HLSDownload(taskId: taskId, dest: dest, filename: filename, format: format, webView: wv, window: win)
        hlsDownloads[taskId] = dl

        // Pull cookies from the sniff WebView's cookie store —
        // includes any cf_clearance / signed-token cookies the player
        // established. The Python helper replays them via curl_cffi
        // with Safari TLS impersonation. We previously experimented
        // with running the download inside the WebView itself (better
        // cookie + TLS scope) but ran into CORS preflight failures
        // on cross-origin worker.dev hosts; the Python path with
        // hardened retries is more reliable.
        let pageURL = sniffPageURLById[sniffId] ?? manifestUrl
        let cookieStore = wv.configuration.websiteDataStore.httpCookieStore
        cookieStore.getAllCookies { [weak self] cookies in
            self?.runPythonHLS(dl: dl, manifestText: manifestText,
                               manifestUrl: manifestUrl, pageURL: pageURL,
                               cookies: cookies)
        }
    }

    func runPythonHLS(dl: HLSDownload, manifestText: String,
                      manifestUrl: String, pageURL: String,
                      cookies: [HTTPCookie]) {
        guard let base = loadedURL,
              let endpoint = URL(string: "/hls-fetch", relativeTo: base) else {
            failHLSDownload(dl: dl, error: "server URL unknown")
            return
        }
        let cookieDicts: [[String: String]] = cookies.map { c in
            ["name": c.name, "value": c.value, "domain": c.domain, "path": c.path]
        }
        let body: [String: Any] = [
            "manifest_text": manifestText,
            "manifest_url": manifestUrl,
            "page_url": pageURL,
            "cookies": cookieDicts,
            "out_path": dl.tsPath,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: body) else {
            failHLSDownload(dl: dl, error: "json encode failed"); return
        }
        var req = URLRequest(url: endpoint)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = data
        req.timeoutInterval = 3600

        let stream = HLSStreamReader(taskId: dl.taskId, owner: self)
        let session = URLSession(configuration: .default, delegate: stream, delegateQueue: OperationQueue())
        stream.session = session
        stream.task = session.dataTask(with: req)
        stream.task?.resume()
    }

    func handleHLSEvent(taskId: String, event: [String: Any]) {
        guard let dl = hlsDownloads[taskId] else { return }
        let type = event["type"] as? String ?? ""
        switch type {
        case "status":
            if let m = event["msg"] as? String {
                notifyHls("__vdHlsStatus", taskId: taskId, extra: ["status": m])
            }
        case "progress":
            let pct = event["percent"] as? Double ?? 0
            let idx = event["idx"] as? Int ?? 0
            let total = event["total"] as? Int ?? 0
            notifyHls("__vdHlsProgress", taskId: taskId,
                      extra: ["percent": pct, "idx": idx, "total": total])
        case "done":
            finishHLSDownload(dl: dl)
        case "error":
            let err = event["error"] as? String ?? "unknown"
            failHLSDownload(dl: dl, error: err)
        default:
            break
        }
    }

    func finishHLSDownload(dl: HLSDownload) {
        dl.close()
        let outDir = dl.dest
        try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
        // Sanitize first — the JS side passes the card's title field as
        // the filename, and that field can be a raw URL when the sniffer
        // didn't find a proper page title. Slashes in a URL would
        // otherwise nest the output into directory components that
        // don't exist (and that ffmpeg won't auto-create).
        let safeFilename = sanitizeBasename(dl.filename)
        let base = (outDir as NSString).appendingPathComponent(safeFilename)

        // Smart stream-copy decision. When the user picked an .mp4
        // option (mp4-h264 or mp4-web), check whether the source TS is
        // already H.264 + AAC — if so, remap to the default "mp4" case
        // which does a fast stream-copy with the HLS-specific
        // aac_adtstoasc bitstream filter. Roughly 50-100× faster than
        // a VideoToolbox re-encode for sources that don't actually
        // need re-encoding (which is the vast majority of HLS feeds).
        // Re-encode is only triggered for the edge cases: HEVC sources,
        // unusual audio codecs, etc.
        var effectiveFormat = dl.format
        var fastEncode = false
        if dl.format == "mp4-h264" || dl.format == "mp4-web" {
            if canHlsStreamCopy(input: dl.tsPath) {
                effectiveFormat = "mp4"
                fastEncode = true
            }
        }
        // mkv is also stream-copy and finishes in seconds — flag it
        // so the JS bar estimator knows the back end of the rip will
        // be near-instant. mp3/m4a re-encode but they're audio-only
        // and typically very fast too.
        if dl.format == "mkv" { fastEncode = true }
        if fastEncode {
            notifyHls("__vdHlsStatus", taskId: dl.taskId, extra: [
                "status": "Source is fast-remux compatible",
                "phase":  "encode",
                "fastEncode": true,
            ])
        }
        let (outPath, args) = ffmpegArgsForHLS(format: effectiveFormat, input: dl.tsPath, base: base)
        // Quick ffprobe pass to get the input duration. The re-encode's
        // -progress output emits microseconds-of-output; dividing by
        // duration_us gives a real percentage. ffprobe on a local .ts
        // takes 50-100 ms; the value is also used to format ETA.
        let durationSec = ffprobeDuration(input: dl.tsPath)
        let durationUs  = durationSec * 1_000_000.0
        let initialStatus = durationSec > 0
            ? "Encoding… 0% · 00:00 / \(formatMMSS(durationSec))"
            : "Encoding…"
        notifyHls("__vdHlsStatus", taskId: dl.taskId, extra: ["status": initialStatus])

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: ffmpegPath())
        proc.arguments = args

        // CRITICAL: drain stdout AND stderr asynchronously while the
        // process runs. macOS Process pipes have a ~64 KB kernel buffer;
        // if no one reads, ffmpeg blocks on its next write and the whole
        // process deadlocks indefinitely (terminationHandler never fires
        // because the process never terminates). The previous version
        // accumulated stderr inside terminationHandler via
        // readDataToEndOfFile, which works for short stderr output but
        // hangs forever on chatty re-encodes (a 30-min video with HLS
        // discontinuity warnings can easily exceed 64 KB). Drain into
        // an in-memory buffer here instead, capped so we don't bloat
        // RAM if something runs amok.
        let stderrBuf  = NSMutableData()
        let stderrLock = NSLock()
        let stdoutBuf  = NSMutableData()  // we don't surface stdout, but we
        let stdoutLock = NSLock()         // still drain so it can't block.
        let errPipe = Pipe()
        let outPipe = Pipe()
        proc.standardError = errPipe
        proc.standardOutput = outPipe
        let cap = 64 * 1024  // tail-trim threshold per stream
        errPipe.fileHandleForReading.readabilityHandler = { fh in
            let chunk = fh.availableData
            if chunk.isEmpty {
                fh.readabilityHandler = nil   // EOF — stop calling us
                return
            }
            stderrLock.lock()
            stderrBuf.append(chunk)
            if stderrBuf.length > cap {
                let drop = stderrBuf.length - cap
                stderrBuf.replaceBytes(in: NSRange(location: 0, length: drop),
                                       withBytes: nil, length: 0)
            }
            stderrLock.unlock()
        }
        // Progress parsing lives in the stdout drain. Each ~0.5s
        // ffmpeg writes a batch of `key=value` lines ending with
        // `progress=continue` (or `progress=end` on completion). We
        // pull `out_time_us=<n>` out, divide by duration_us, and emit
        // a status update. Throttle isn't needed — ffmpeg already
        // paces itself via -stats_period (default 0.5s).
        let taskIdLocal = dl.taskId
        let progressTail = NSMutableData()
        let progressLock = NSLock()
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] fh in
            let chunk = fh.availableData
            if chunk.isEmpty {
                fh.readabilityHandler = nil
                return
            }
            stdoutLock.lock()
            stdoutBuf.append(chunk)
            if stdoutBuf.length > cap {
                let drop = stdoutBuf.length - cap
                stdoutBuf.replaceBytes(in: NSRange(location: 0, length: drop),
                                       withBytes: nil, length: 0)
            }
            stdoutLock.unlock()

            // Buffer chunks across reads — a key=value line can split
            // across the 4 KB read boundary. Accumulate until we have
            // newline-terminated text, parse complete lines, keep any
            // trailing partial line for the next round.
            progressLock.lock()
            progressTail.append(chunk)
            let pending = String(data: progressTail as Data, encoding: .utf8) ?? ""
            // Find the last newline; everything after is incomplete.
            let split = pending.split(omittingEmptySubsequences: false,
                                      whereSeparator: { $0 == "\n" || $0 == "\r" })
            let completed: [Substring]
            let leftover: String
            if pending.hasSuffix("\n") || pending.hasSuffix("\r") {
                completed = Array(split)
                leftover = ""
            } else {
                completed = Array(split.dropLast())
                leftover = String(split.last ?? "")
            }
            progressTail.length = 0
            if let lo = leftover.data(using: .utf8) { progressTail.append(lo) }
            progressLock.unlock()

            for raw in completed {
                let line = raw.trimmingCharacters(in: .whitespaces)
                guard line.hasPrefix("out_time_us=") else { continue }
                let v = String(line.dropFirst("out_time_us=".count))
                guard let us = Double(v), us >= 0 else { continue }
                let outSec = us / 1_000_000.0
                let label: String
                let pct: Double
                if durationUs > 0 {
                    pct = min(100.0, max(0.0, us / durationUs * 100.0))
                    label = "Encoding… \(Int(pct.rounded()))% · \(formatMMSS(outSec)) / \(formatMMSS(durationSec))"
                } else {
                    pct = -1   // sentinel: unknown duration → JS skips bar update
                    label = "Encoding… \(formatMMSS(outSec))"
                }
                DispatchQueue.main.async {
                    var extra: [String: Any] = [
                        "status": label,
                        "phase":  "encode",
                    ]
                    if pct >= 0 { extra["percent"] = pct }
                    self?.notifyHls("__vdHlsStatus", taskId: taskIdLocal,
                                    extra: extra)
                }
            }
        }
        proc.terminationHandler = { [weak self] p in
            // Disconnect the readability handlers; any further bytes
            // sitting in the pipe after termination are negligible
            // (ffmpeg has already stopped writing). We avoid
            // readDataToEndOfFile here because if a reader handler is
            // still attached it can race and block.
            errPipe.fileHandleForReading.readabilityHandler = nil
            outPipe.fileHandleForReading.readabilityHandler = nil
            stderrLock.lock()
            let data = stderrBuf as Data
            stderrLock.unlock()
            let stderr = String(data: data, encoding: .utf8) ?? ""
            let tail = stderr
                .split(whereSeparator: { $0 == "\n" || $0 == "\r" })
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
                .suffix(4)
                .joined(separator: " | ")
            DispatchQueue.main.async {
                let ok = (p.terminationStatus == 0)
                dl.cleanup()
                self?.hlsDownloads[dl.taskId] = nil
                if ok {
                    self?.notifyHls("__vdHlsDone", taskId: dl.taskId, extra: ["filename": outPath])
                } else {
                    let base = "ffmpeg failed (status \(p.terminationStatus))"
                    let msg = tail.isEmpty ? base : "\(base): \(tail)"
                    self?.notifyHls("__vdHlsError", taskId: dl.taskId, extra: ["error": msg])
                }
            }
        }
        do { try proc.run() }
        catch { failHLSDownload(dl: dl, error: "ffmpeg launch: \(error.localizedDescription)") }
    }

    func failHLSDownload(dl: HLSDownload, error: String) {
        dl.cleanup()
        hlsDownloads[dl.taskId] = nil
        notifyHls("__vdHlsError", taskId: dl.taskId, extra: ["error": error])
    }

    func notifyHls(_ fn: String, taskId: String, extra: [String: Any]) {
        var payload: [String: Any] = ["taskId": taskId]
        for (k, v) in extra { payload[k] = v }
        guard let d = try? JSONSerialization.data(withJSONObject: payload) else { return }
        let b64 = d.base64EncodedString()
        let js = "window.\(fn) && window.\(fn)(JSON.parse(new TextDecoder().decode(Uint8Array.from(atob('\(b64)'), c => c.charCodeAt(0)))));"
        webView?.evaluateJavaScript(js, completionHandler: nil)
    }

    func startSniff(id: String, url: String) {
        teardownSniff()
        sniffID = id
        sniffURL = url
        sniffURLs = []
        sniffPageTitle = ""
        sniffManifestContent = ""
        sniffManifestVariantURL = ""
        sniffPlaylists = [:]

        let cfg = WKWebViewConfiguration()
        let uc = WKUserContentController()
        uc.add(self, name: "vdCapture")
        uc.add(self, name: "vdHlsChunk")
        let sniffScript = WKUserScript(source: SNIFF_JS, injectionTime: .atDocumentStart, forMainFrameOnly: false)
        uc.addUserScript(sniffScript)
        let dlScript = WKUserScript(source: HLS_DOWNLOADER_JS, injectionTime: .atDocumentEnd, forMainFrameOnly: true)
        uc.addUserScript(dlScript)
        // Belt-and-suspenders mute: catch any <video>/<audio> element the
        // page creates and force .muted = true on it. The autoplay block
        // below already prevents most playback from starting; this script
        // ensures that even if something does sneak through, it's silent.
        let muteScript = WKUserScript(source: """
            (function(){
              var force = function(el){ try { el.muted = true; el.volume = 0; } catch(e){} };
              var sweep = function(){
                document.querySelectorAll('video,audio').forEach(force);
              };
              sweep();
              new MutationObserver(sweep).observe(document.documentElement || document, {childList:true, subtree:true});
              // Override the constructor too — hls.js etc. can attach to
              // <video> elements that already exist; this catches creates.
              var orig = document.createElement;
              document.createElement = function(tag){
                var el = orig.apply(document, arguments);
                if (typeof tag === 'string' && /^(video|audio)$/i.test(tag)) force(el);
                return el;
              };
            })();
        """, injectionTime: .atDocumentStart, forMainFrameOnly: false)
        uc.addUserScript(muteScript)
        cfg.userContentController = uc
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = true
        // We DON'T block media auto-play here — the muteScript above
        // handles the user-facing problem (no audible audio) without
        // stopping the player from actually fetching segments. Letting
        // segments load is important: it's how CDN cookies (Cloudflare's
        // cf_clearance, Akamai's _abck, etc.) get set against the CDN
        // host so the helper can reuse them when downloading segments
        // later. Blocking media playback would skip that handshake and
        // cause CF challenges mid-download.

        let frame = NSRect(x: 0, y: 0, width: 1024, height: 768)
        let wv = WKWebView(frame: frame, configuration: cfg)
        wv.customUserAgent = BROWSER_UA
        if #available(macOS 13.3, *) { wv.isInspectable = true }
        sniffWebView = wv

        // Off-screen window keeps the WebView fully alive so JS/timers/network
        // don't get throttled, while staying invisible to the user.
        let win = NSWindow(contentRect: NSRect(x: -10000, y: -10000, width: 1024, height: 768),
                           styleMask: [.borderless],
                           backing: .buffered, defer: false)
        win.isReleasedWhenClosed = false
        win.ignoresMouseEvents = true
        win.contentView?.addSubview(wv)
        wv.autoresizingMask = [.width, .height]
        wv.frame = win.contentView!.bounds
        sniffWindow = win
        win.orderFrontRegardless()

        // Track the session immediately. Even if anything later nils
        // sniffWebView, this entry persists until the download claims it.
        sniffSessions[id] = (wv: wv, win: win)
        sniffPageURLById[id] = url
        NSLog("VD: startSniff id=%@ sessions=%@", id, Array(sniffSessions.keys).joined(separator: ","))

        if let u = URL(string: url) { wv.load(URLRequest(url: u)) }

        sniffMaxTimer?.invalidate()
        sniffMaxTimer = Timer.scheduledTimer(withTimeInterval: 35, repeats: false) { [weak self] _ in
            self?.finishSniff()
        }
        sniffProgressTimer?.invalidate()
        sniffProgressTimer = Timer.scheduledTimer(withTimeInterval: 0.7, repeats: true) { [weak self] _ in
            self?.sendSniffProgress()
        }
    }

    func startGraceTimerIfNeeded() {
        // Adaptive grace: scale the wait down as we accumulate data so we
        // can finish quickly when the page has already surfaced everything
        // we need (the master playlist + at least one variant body).
        let haveContents = (sniffPlaylists["contents"] as? [String: String])?.isEmpty == false
        let interval: TimeInterval
        if !sniffManifestContent.isEmpty && haveContents {
            interval = 0.4   // master + variant body cached → done
        } else if !sniffManifestContent.isEmpty {
            interval = 1.5   // master in hand; give variants a beat to land
        } else if sniffGraceTimer != nil {
            return           // already running on the slow path
        } else {
            interval = 5.0   // got a URL but not the body yet
        }
        sniffGraceTimer?.invalidate()
        sniffGraceTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: false) { [weak self] _ in
            self?.finishSniff()
        }
    }

    func sendSniffProgress() {
        guard !sniffID.isEmpty else { return }
        let n = sniffURLs.count
        let safeId = sniffID.replacingOccurrences(of: "'", with: "")
        webView?.evaluateJavaScript(
            "window.__vdSniffProgress && window.__vdSniffProgress('\(safeId)', \(n))",
            completionHandler: nil)
    }

    func finishSniff() {
        guard !sniffID.isEmpty else { return }
        let id = sniffID
        let pageURL = sniffURL
        let pageTitle = sniffPageTitle
        let candidates = Array(sniffURLs)
        let manifest = sniffManifestContent
        let manifestVariant = sniffManifestVariantURL
        let playlists = sniffPlaylists
        let wv = sniffWebView
        sniffID = ""; sniffURL = ""; sniffURLs = []; sniffPageTitle = ""
        sniffManifestContent = ""; sniffManifestVariantURL = ""; sniffPlaylists = [:]

        let dispatch: (String?) -> Void = { [weak self] cookiesPath in
            var payload: [String: Any] = [
                "sniffId": id,
                "candidates": candidates,
                "url": pageURL,
                "title": pageTitle,
            ]
            if let p = cookiesPath { payload["cookiesFile"] = p }
            if !manifest.isEmpty {
                payload["manifestContent"] = manifest
                payload["manifestVariantUrl"] = manifestVariant
            }
            if !playlists.isEmpty {
                payload["playlists"] = playlists
            }
            if let data = try? JSONSerialization.data(withJSONObject: payload) {
                let b64 = data.base64EncodedString()
                let js = "window.__vdSniffResult(new TextDecoder().decode(Uint8Array.from(atob('\(b64)'), c => c.charCodeAt(0))));"
                self?.webView?.evaluateJavaScript(js, completionHandler: nil)
            }
            // Session is already in sniffSessions from startSniff; just
            // stop the timers and let the WebView live until download.
            self?.sniffWebView = nil
            self?.sniffWindow = nil
            self?.sniffMaxTimer?.invalidate(); self?.sniffMaxTimer = nil
            self?.sniffGraceTimer?.invalidate(); self?.sniffGraceTimer = nil
            self?.sniffProgressTimer?.invalidate(); self?.sniffProgressTimer = nil
        }

        if let wv = wv {
            wv.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
                let path = AppDelegate.writeCookiesFile(cookies)
                DispatchQueue.main.async { dispatch(path) }
            }
        } else {
            dispatch(nil)
        }
    }

    static func writeCookiesFile(_ cookies: [HTTPCookie]) -> String? {
        if cookies.isEmpty { return nil }
        var lines: [String] = [
            "# Netscape HTTP Cookie File",
            "# This file is generated by Rip Raptor. Do not edit.",
        ]
        for c in cookies {
            let dom = c.domain
            let includeSub = dom.hasPrefix(".") ? "TRUE" : "FALSE"
            let httpOnlyPrefix = c.isHTTPOnly ? "#HttpOnly_" : ""
            let path = c.path.isEmpty ? "/" : c.path
            let secure = c.isSecure ? "TRUE" : "FALSE"
            let exp = c.expiresDate.map { String(Int($0.timeIntervalSince1970)) } ?? "0"
            lines.append("\(httpOnlyPrefix)\(dom)\t\(includeSub)\t\(path)\t\(secure)\t\(exp)\t\(c.name)\t\(c.value)")
        }
        let content = lines.joined(separator: "\n") + "\n"
        let outPath = (NSTemporaryDirectory() as NSString).appendingPathComponent("vd-cookies-\(UUID().uuidString).txt")
        do { try content.write(toFile: outPath, atomically: true, encoding: .utf8); return outPath }
        catch { return nil }
    }

    func teardownSniff() {
        // We only clear the in-progress class members. The actual WebView/
        // Window for any completed sniff lives in sniffSessions and stays
        // alive until a download claims it (or the app quits).
        sniffMaxTimer?.invalidate(); sniffMaxTimer = nil
        sniffGraceTimer?.invalidate(); sniffGraceTimer = nil
        sniffProgressTimer?.invalidate(); sniffProgressTimer = nil
        sniffWebView = nil
        sniffWindow = nil
    }

    // MARK: - Window delegate

    func windowWillClose(_ notification: Notification) {
        guard let w = notification.object as? NSWindow else { return }
        if w === window {
            NSApp.terminate(nil)
            return
        }
        if let idx = editorWindows.firstIndex(where: { $0 === w }) {
            editorWindows.remove(at: idx)
        }
        for (sid, win) in editorWindowsBySid where win === w {
            editorWindowsBySid.removeValue(forKey: sid)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let p = pythonProcess, p.isRunning {
            p.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + 1.5) {
                if p.isRunning { kill(p.processIdentifier, SIGKILL) }
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }
}

final class HLSStreamReader: NSObject, URLSessionDataDelegate {
    let taskId: String
    weak var owner: AppDelegate?
    var session: URLSession?
    var task: URLSessionDataTask?
    var buffer = Data()
    var sawError = false
    // The helper exits as soon as it emits {"type":"done"}. ffmpeg muxing
    // then runs ASYNC on the Swift side, during which hlsDownloads[taskId]
    // is still populated. If the URL stream's didCompleteWithError fires
    // before ffmpeg finishes, we'd spuriously fail a download that's
    // actually succeeding. sawDone gates that.
    var sawDone = false
    init(taskId: String, owner: AppDelegate) { self.taskId = taskId; self.owner = owner }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buffer.append(data)
        while let nlIdx = buffer.firstIndex(of: 0x0A) {
            let lineRange = buffer.startIndex..<nlIdx
            let lineData = buffer.subdata(in: lineRange)
            buffer.removeSubrange(buffer.startIndex...nlIdx)
            guard !lineData.isEmpty,
                  let obj = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any]
            else { continue }
            let t = obj["type"] as? String
            if t == "error" { sawError = true }
            if t == "done" { sawDone = true }
            DispatchQueue.main.async { [weak self] in
                guard let self = self else { return }
                self.owner?.handleHLSEvent(taskId: self.taskId, event: obj)
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let taskId = self.taskId
        let hadError = sawError
        let hadDone = sawDone
        DispatchQueue.main.async { [weak self] in
            guard let self = self, let owner = self.owner else { return }
            // If we got a clean "done" event, the helper succeeded and
            // ffmpeg is muxing. Don't touch anything — the ffmpeg
            // termination handler will mark success/failure.
            if hadDone { return }
            if let e = error, !hadError {
                if let dl = owner.hlsDownloads[taskId] {
                    owner.failHLSDownload(dl: dl, error: "stream: \(e.localizedDescription)")
                }
            } else if !hadError, owner.hlsDownloads[taskId] != nil {
                // Stream ended without a terminal event — treat as error.
                if let dl = owner.hlsDownloads[taskId] {
                    owner.failHLSDownload(dl: dl, error: "fetcher exited without status")
                }
            }
        }
        session.invalidateAndCancel()
        self.session = nil
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
