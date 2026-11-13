"""Plain-text app log at %APPDATA%\\ShootClipper\\app.log, so runs can be sent for review."""
import datetime as dt
import threading

from . import config

_lock = threading.Lock()
MAX_BYTES = 2_000_000


def path():
    return config.APP_DIR / "app.log"


def write(msg):
    try:
        config.ensure_app_dir()
        p = path()
        with _lock:
            if p.exists() and p.stat().st_size > MAX_BYTES:
                old = p.with_suffix(".log.1")
                if old.exists():
                    old.unlink()
                p.rename(old)
            with open(p, "a", encoding="utf-8") as f:
                stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for line in str(msg).splitlines() or [""]:
                    f.write(f"[{stamp}] {line}\n")
    except Exception:
        pass  # logging must never break a run
