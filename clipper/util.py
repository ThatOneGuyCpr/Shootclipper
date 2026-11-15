"""Small shared helpers: subprocess wrappers, time formatting, slugs."""
import json
import os
import re
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")
# Stop a black console window flashing up for every ffmpeg call on Windows.
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def run(cmd, cwd=None, check=True):
    """Run a command, return CompletedProcess. Raises with stderr tail on failure.
    Killed (raising jobs.Cancelled) if Stop is pressed while it runs."""
    from . import jobs
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW)
    out, err = jobs.communicate(proc)
    p = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check and p.returncode != 0:
        tail = (p.stderr or "")[-1500:]
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed:\n{tail}")
    return p


def run_bytes(cmd):
    """Run a command and return its stdout as bytes (stops with jobs.Cancelled on Stop)."""
    from . import jobs
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    out, err = jobs.communicate(proc)
    return proc.returncode, out, err


def popen(cmd, **kw):
    return subprocess.Popen(cmd, creationflags=NO_WINDOW, **kw)


def have(binary):
    return shutil.which(binary) is not None


def probe(path):
    """Basic media info via ffprobe."""
    p = run(["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)])
    d = json.loads(p.stdout)
    streams = d.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = 30.0
    if v and v.get("avg_frame_rate", "0/0") != "0/0":
        n, dnm = v["avg_frame_rate"].split("/")
        fps = float(n) / float(dnm) if float(dnm) else 30.0
    return {
        "duration": float(d.get("format", {}).get("duration", 0) or 0),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps": fps,
        "has_video": v is not None,
        "has_audio": a is not None,
    }


def write_json(path, obj, indent=None):
    """Write JSON so a crash or power cut mid-write can't leave a half-written file."""
    path = str(path)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=indent)
    os.replace(tmp, path)


def read_json(path, default=None):
    """Read JSON, returning `default` if the file is missing or damaged."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class RunLock:
    """One Make clips run at a time across the app and the daily run (two processes
    working the same interview would trip over each other). Held as an OS file lock,
    so it frees itself if the process dies."""

    def __init__(self, path):
        self.path, self.f = str(path), None

    def acquire(self):
        f = open(self.path, "a+")
        try:
            if IS_WINDOWS:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.f = f
        return True

    def release(self):
        if not self.f:
            return
        try:
            if IS_WINDOWS:
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.f.close()
        self.f = None


def ts(seconds, decimals=0):
    """Seconds -> M:SS (or H:MM:SS). decimals=1 adds tenths."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    sec = f"{s:04.1f}" if decimals else f"{int(s):02d}"
    return f"{h}:{m:02d}:{sec}" if h else f"{m}:{sec}"


def parse_ts(text):
    """'1:02:03.5', '2:03', '123.4' -> seconds."""
    text = str(text).strip()
    if not text:
        raise ValueError("empty timestamp")
    parts = text.split(":")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def slug(text, maxlen=60):
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return text[:maxlen] or "interview"


def open_path(path):
    """Open a file or folder with the OS default app."""
    path = str(path)
    if IS_WINDOWS:
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
