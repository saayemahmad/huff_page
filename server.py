#!/usr/bin/env python3
"""Live visitor log for the pakhir-basha page. Standard library only.

Run:   python3 server.py
Open:  http://localhost:8000

Keep pakhir-basha.html in the same folder as this file.
The server serves the page and adds a small tracker script to it. The terminal
prints, live: the device model (navigator.userAgentData high-entropy "model"),
every "huff" press, the anger level shown after each press, which moods the
visitor has seen so far, and when they leave or come back.

Every line printed to stdout/stderr (including errors and tracebacks) is also
mirrored, in order, to a Firebase Realtime Database under /logs, and /log on the
site shows them live. Override the database with FIREBASE_DB_URL if needed.
"""
import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

FIREBASE_URL = os.environ.get("FIREBASE_DB_URL", "https://dev-ca098-default-rtdb.firebaseio.com").rstrip("/")
FIREBASE_LOG_PATH = os.environ.get("FIREBASE_LOG_PATH", "logs").strip("/")

# The four moods the page can show, in the order the meter reaches them.
MOODS = ["cooled down", "angry", "very angry", "MAX (about to burst)"]


def mood_of(level):
    if level is None:
        return "?"
    if level >= 100:
        return MOODS[3]
    if level >= 85:
        return MOODS[2]
    if level >= 60:
        return MOODS[1]
    return MOODS[0]


class FirebaseLog:
    """Sends every log line to Firebase RTDB from a background thread.

    Lines are queued, sent in order in small batches, and a failed batch is
    retried until it goes through, so a network blip never drops a line.
    Key = zero-padded epoch ms + counter, so the database sorts chronologically.
    """

    def __init__(self, base, path):
        self.url = "%s/%s.json" % (base, path)
        self.q = queue.Queue()
        self.seq = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._run, name="firebase-log", daemon=True).start()

    def add(self, stream, msg):
        ms = int(time.time() * 1000)
        with self.lock:
            self.seq += 1
            key = "%013d_%06d" % (ms, self.seq % 1000000)
        self.q.put((key, {"t": ms, "s": stream, "m": msg[:8000]}))

    def _run(self):
        while True:
            batch = [self.q.get()]
            while len(batch) < 200:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            body = json.dumps(dict(batch)).encode("utf-8")
            delay = 1
            while True:
                try:
                    req = urllib.request.Request(self.url, data=body, method="PATCH",
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=15).close()
                    break
                except Exception as e:
                    # Write to the real stderr so a failure here can't loop back into the mirror.
                    sys.__stderr__.write("[firebase-log] send failed (%r), retrying in %ds\n" % (e, delay))
                    sys.__stderr__.flush()
                    time.sleep(delay)
                    delay = min(delay * 2, 15)
            for _ in batch:
                self.q.task_done()

    def flush(self, timeout=8):
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(0.05)


class Tee:
    """Wraps stdout/stderr: output still goes to the console, and each line is mirrored to Firebase."""

    def __init__(self, stream, name, sink):
        self._stream = stream
        self._name = name
        self._sink = sink
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text):
        n = self._stream.write(text)
        with self._lock:
            *lines, self._buf = (self._buf + text).split("\n")
        for line in lines:
            line = line.rstrip("\r")
            if line.strip():
                self._sink.add(self._name, line)
        return n

    def drain(self):
        with self._lock:
            rest, self._buf = self._buf, ""
        if rest.strip():
            self._sink.add(self._name, rest.rstrip("\r"))

    def flush(self):
        self._stream.flush()

    def __getattr__(self, attr):
        return getattr(self._stream, attr)


HEAD = (
    '<!doctype html><html lang="bn"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);"
    "padding-bottom:env(safe-area-inset-bottom,0px)}body{margin:0;font:14px system-ui}</style>"
    "</head><body>"
)

TRACKER = r"""<script>
(function () {
  var sid = Math.random().toString(36).slice(2, 8);
  var t0 = Date.now(), presses = 0;
  function post(d) {
    d.sid = sid;
    d.secs = Math.round((Date.now() - t0) / 1000);
    var body = JSON.stringify(d);
    try {
      var ok = navigator.sendBeacon && navigator.sendBeacon('/log', new Blob([body], { type: 'text/plain' }));
      if (!ok) fetch('/log', { method: 'POST', body: body, keepalive: true });
    } catch (e) {}
  }
  function level() {
    var m = document.getElementById('meter');
    return m ? Number(m.getAttribute('aria-valuenow')) : null;
  }
  function open(model) { post({ type: 'open', model: model, secure: window.isSecureContext, level: level() }); }
  var uad = navigator.userAgentData;
  if (uad && uad.getHighEntropyValues) {
    uad.getHighEntropyValues(['model']).then(function (v) { open(v.model || ''); }, function () { open(null); });
  } else {
    open(null);
  }
  var btn = document.getElementById('huff');
  if (btn) btn.addEventListener('click', function () {
    presses++;
    post({ type: 'huff', n: presses, level: level() });
  });
  document.addEventListener('visibilitychange', function () {
    post({ type: document.visibilityState === 'hidden' ? 'hide' : 'show', n: presses });
  });
  window.addEventListener('pagehide', function () { post({ type: 'hide', n: presses }); });
})();
</script>"""


class Visitor:
    def __init__(self, model):
        self.model = model
        self.start = time.time()
        self.presses = 0
        self.levels = []
        self.moods = set()
        self.saw_all = False


visitors = {}
lock = threading.Lock()


def say(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def describe_model(model, secure):
    if model:
        return model
    if model == "":
        return "(not reported: desktop, or not Chromium on Android)"
    if not secure:
        return "(unavailable: page is not on HTTPS)"
    return "(unavailable: browser has no userAgentData)"


def handle_event(d):
    sid = str(d.get("sid", "?"))[:12]
    kind = d.get("type")
    level = d.get("level")
    with lock:
        v = visitors.get(sid)
        if kind == "open":
            v = Visitor(d.get("model"))
            visitors[sid] = v
            if isinstance(level, (int, float)):
                v.moods.add(mood_of(level))
            say("NEW VISIT   #%s   model: %s" % (sid, describe_model(d.get("model"), d.get("secure"))))
            return
        if v is None:
            v = visitors[sid] = Visitor(None)
        if kind == "huff":
            v.presses = int(d.get("n", v.presses + 1))
            v.levels.append(level)
            mood = mood_of(level)
            v.moods.add(mood)
            say("HUFF #%d      #%s   level %s%%  ->  %s   | moods seen %d/%d: %s" % (
                v.presses, sid, level, mood, len(v.moods), len(MOODS),
                ", ".join(m for m in MOODS if m in v.moods)))
            if len(v.moods) == len(MOODS) and not v.saw_all:
                v.saw_all = True
                say("SEEN EVERYTHING   #%s   all %d moods shown after %d presses (levels: %s)" % (
                    sid, len(MOODS), v.presses, ", ".join(str(x) for x in v.levels)))
        elif kind == "hide":
            say("LEFT/HIDDEN #%s   after %ss   presses: %d   moods seen %d/%d" % (
                sid, d.get("secs"), v.presses, len(v.moods), len(MOODS)))
        elif kind == "show":
            say("BACK        #%s   after %ss away-or-total" % (sid, d.get("secs")))


class Handler(BaseHTTPRequestHandler):
    page_path = HERE / "pakhir-basha.html"

    def log_message(self, *args):
        pass  # keep the terminal for the live log only

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            say("PAGE REQUESTED   (waiting for the tracker to report the model...)")
            html =HEAD + self.page_path.read_text(encoding="utf-8") + TRACKER + "</body></html>"
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.split("?")[0] in ("/log", "/log/", "/log.html"):
            self._send(200, (HERE / "log.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/favicon"):
            self._send(204)
        else:
            self._send(404, b"not found")

    def do_POST(self):
        if self.path != "/log":
            return self._send(404, b"not found")
        try:
            size = min(int(self.headers.get("Content-Length", "0")), 4096)
            handle_event(json.loads(self.rfile.read(size) or b"{}"))
        except Exception as e:
            say("bad /log request: %r" % (e,))
        self._send(204)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Hosts like Render set PORT and need 0.0.0.0; locally we stay on 127.0.0.1:8000.
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--host", default="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
                    help="use 0.0.0.0 to listen on your network")
    ap.add_argument("--page", default=str(Handler.page_path), help="path to the HTML page")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    fb = FirebaseLog(FIREBASE_URL, FIREBASE_LOG_PATH)
    tees = [Tee(sys.stdout, "out", fb), Tee(sys.stderr, "err", fb)]
    sys.stdout, sys.stderr = tees
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # Render stops with SIGTERM: flush first
    say("SERVER STARTING   mirroring all logs to Firebase /%s" % FIREBASE_LOG_PATH)
    Handler.page_path = Path(args.page)
    if not Handler.page_path.is_file():
        print("Page not found: %s (put pakhir-basha.html next to server.py)" % Handler.page_path, file=sys.stderr)
        tees[1].drain()
        fb.flush()
        sys.exit(1)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Serving the page at http://%s:%d" % ("localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host, args.port))
    print("The device model needs HTTPS on a phone. Expose it with a tunnel, for example:")
    print("  cloudflared tunnel --url http://localhost:%d     or     ngrok http %d" % (args.port, args.port))
    print("Live log at /log  (e.g. http://localhost:%d/log)" % args.port)
    print("Waiting for visitors (Ctrl+C to stop)...", flush=True)
    try:
        srv.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("Stopped.")
    finally:
        for t in tees:
            t.drain()
        fb.flush()


if __name__ == "__main__":
    main()
