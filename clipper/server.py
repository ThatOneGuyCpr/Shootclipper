"""Tiny local web server (127.0.0.1 only) behind the desktop window.

Serves the bundled interface from web/, streams local video with Range support so the
player can seek, and mirrors the Api over POST /api/<name> as a fallback transport.
Media and API requests need the per-launch token.
"""
import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

WEB = Path(__file__).resolve().parent.parent / "web"
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("video/mp4", ".mp4")
CHUNK = 256 * 1024


def make_handler(api, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _file(self, path, cache=False):
            size = path.stat().st_size
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                a, _, b = rng[6:].split(",")[0].partition("-")
                try:
                    if a == "":
                        start = max(0, size - int(b))
                    else:
                        start = int(a)
                        end = min(size - 1, int(b)) if b else size - 1
                    code = 206
                except ValueError:
                    pass
                if start > end or start >= size:
                    return self._send(416, extra={"Content-Range": f"bytes */{size}"})
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        buf = f.read(min(CHUNK, left))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        left -= len(buf)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the player seeks by dropping connections; that's normal

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/media":
                if q.get("k", [""])[0] != token:
                    return self._send(403, b"forbidden")
                p = Path(unquote(q.get("p", [""])[0]))
                if not p.is_file() or not api._media_allowed(p):
                    return self._send(404, b"not found")
                return self._file(p, cache=True)
            rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            p = (WEB / rel).resolve()
            if WEB.resolve() not in p.parents or not p.is_file():
                return self._send(404, b"not found")
            if rel == "index.html":
                html = p.read_text(encoding="utf-8")
                html = html.replace("%%TOKEN%%", token).replace("%%THEME%%", api._cfg.get("theme", "system"))
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return self._file(p)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""     # always drain, or keep-alive breaks
            if not u.path.startswith("/api/") or self.headers.get("X-Token") != token:
                return self._send(403, b"forbidden")
            name = u.path[5:]
            fn = getattr(api, name, None)
            if name.startswith("_") or not callable(fn):
                return self._send(404, b"unknown call")
            try:
                args = json.loads(body or b"[]")
                res = fn(*args)
            except Exception as e:
                res = {"error": str(e)}
            self._send(200, json.dumps(res).encode("utf-8"), "application/json")

    return Handler


def start(api, port=0):
    token = secrets.token_urlsafe(16)
    api._token = token
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api, token))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]
