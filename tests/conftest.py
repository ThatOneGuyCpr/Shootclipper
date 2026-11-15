"""Shared test setup.

Every test runs against a throwaway settings folder and workspace, a fake Claude API
server on localhost (so the real anthropic SDK builds and sends real requests), and small
videos generated with ffmpeg. Nothing touches the network or your real library.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

# Point the app at a temp home BEFORE it is imported: config reads these at import time.
_HOME = Path(tempfile.mkdtemp(prefix="sc_tests_"))
for var in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
    os.environ[var] = str(_HOME)
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clipper import config, db, jobs, ytdlp  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """Empty library, default settings and a fresh workspace for every test."""
    jobs.reset()
    config.ensure_app_dir()
    for name in ("config.json", "dismissed.json", "captures.json", "run.lock"):
        try:
            (config.APP_DIR / name).unlink()
        except OSError:
            pass
    db.init()
    with db._conn() as c:
        c.execute("DELETE FROM clips")
        c.execute("DELETE FROM interviews")
    monkeypatch.setattr(config, "get_api_key", lambda: "sk-test")
    # No test may reach PyPI for a newer yt-dlp; test_ytdlp.py opts back in with a fake.
    monkeypatch.setattr(ytdlp, "update", lambda log=None: None)
    monkeypatch.setattr(ytdlp, "update_if_due", lambda log=None: None)
    yield
    jobs.reset()


@pytest.fixture
def cfg(tmp_path):
    c = config.load()
    c["workspace"] = str(tmp_path / "workspace")
    Path(c["workspace"]).mkdir()
    c["open_folder_when_done"] = False
    config.save(c)
    return c


@pytest.fixture
def log():
    lines = []

    def write(msg):
        lines.append(str(msg))
    write.lines = lines
    write.text = lambda: "\n".join(lines)
    return write


# ── fake Claude ─────────────────────────────────────────────────────────────

class FakeClaude:
    """Stands in for api.anthropic.com. Tests set `on_request` (or use `answers`) to say
    what comes back; every request body is kept in `requests`."""

    def __init__(self):
        self.requests = []
        self.answers = {}          # schema property name -> object to return
        self.on_request = None     # fn(body) -> (obj | str, stop_reason) | ("__error__", status, message)
        self.usage = {"input_tokens": 1000, "output_tokens": 200}

    def schema_props(self, body):
        fmt = (body.get("output_config") or {}).get("format") or {}
        return set((fmt.get("schema") or {}).get("properties") or {})

    def reply(self, body):
        if self.on_request:
            out = self.on_request(body)
            if out is not None:
                return out
        props = self.schema_props(body)
        for key, obj in self.answers.items():
            if key in props:
                return (obj(body) if callable(obj) else obj), "end_turn"
        if not props:
            return "notes", "end_turn"
        raise AssertionError(f"FakeClaude has no answer for a schema with {sorted(props)}")


def _serve(fake):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            fake.requests.append(body)
            try:
                out = fake.reply(body)
            except AssertionError as e:
                out = ("__error__", 500, str(e))
            if out[0] == "__error__":
                payload = json.dumps({"type": "error", "error": {"type": "api_error", "message": out[2]}}).encode()
                self.send_response(out[1])
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            content, stop = out
            text = content if isinstance(content, str) else json.dumps(content)
            usage = dict(fake.usage)
            message = {"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                       "content": [{"type": "text", "text": text}], "stop_reason": stop,
                       "stop_sequence": None, "usage": usage}
            if body.get("stream"):
                start = dict(message, content=[], stop_reason=None, usage=dict(usage, output_tokens=1))
                events = [
                    ("message_start", {"type": "message_start", "message": start}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": text}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta",
                                       "delta": {"stop_reason": stop, "stop_sequence": None},
                                       "usage": {"output_tokens": usage["output_tokens"]}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                payload = "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()
                ctype = "text/event-stream"
            else:
                payload, ctype = json.dumps(message).encode(), "application/json"
            self.send_response(200)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude()
    server = _serve(fake)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    yield fake
    server.shutdown()
    server.server_close()


# ── media ───────────────────────────────────────────────────────────────────

def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """Small generated videos, made once per run."""
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("media")
    interview = d / "interview.mp4"          # 40 s, 640x360, tone audio, no faces
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=40", "-f", "lavfi", "-i", "sine=f=220:d=40",
            "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(interview))
    capture = d / "Hogan vs Ventura.mp4"     # 30 s stand-in for a 2K capture, no audio
    _ffmpeg("-f", "lavfi", "-i", "testsrc=s=640x360:r=30:d=30", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(capture))
    short = d / "Hogan vs Ventura 2.mp4"     # 6 s, for chaining
    _ffmpeg("-f", "lavfi", "-i", "smptebars=s=640x360:r=30:d=6", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(short))
    return SimpleNamespace(dir=d, interview=interview, capture=capture, short=short)


# ── fake Whisper ────────────────────────────────────────────────────────────

class FakeWhisper:
    """Says "word" twice a second for as long as the audio it is given. Records the length
    of every piece of audio it was asked to transcribe."""

    def __init__(self):
        self.asked = []            # seconds of audio per call

    def transcribe(self, audio, **kw):
        span = len(audio) / 16000.0
        self.asked.append(round(span, 1))

        def segments():
            t = 0.25
            while t + 2.0 <= span:
                words = [SimpleNamespace(word=f" w{int((t + k * 0.5) * 2)}", start=t + k * 0.5, end=t + k * 0.5 + 0.4)
                         for k in range(4)]
                yield SimpleNamespace(start=t, end=t + 2.0, text=" ".join(w.word.strip() for w in words),
                                      words=words)
                t += 2.0
        return segments(), SimpleNamespace(duration=span)


@pytest.fixture
def whisper(monkeypatch):
    from clipper import transcribe
    fake = FakeWhisper()
    monkeypatch.setattr(transcribe, "_model", lambda cfg, log, progress=None: (fake, "fake"))
    return fake
