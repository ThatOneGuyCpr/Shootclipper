"""Keeps yt-dlp (the YouTube downloader) current without waiting for an app update.

YouTube changes its site every few weeks and downloads break until yt-dlp catches up.
The installed app carries the yt-dlp it was built with, so it would stay broken until the
next Shoot Clipper release. Instead, the app fetches newer yt-dlp versions itself: the
official package from PyPI (checked against PyPI's SHA-256), unpacked into
%APPDATA%\\ShootClipper\\yt-dlp and loaded in place of the built-in copy.

It checks once a day, and straight away when a download fails. If a fetched copy won't
load, it is thrown away and the built-in one is used.

Everything that uses yt-dlp goes through `session()`, which is also what makes swapping
versions safe: a swap only happens when no download or search is in progress.
"""
import hashlib
import importlib
import json
import re
import shutil
import sys
import threading
import time
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

from . import applog, config

PYPI = "https://pypi.org/pypi/yt-dlp/json"
CHECK_EVERY = 24 * 3600

_lock = threading.Lock()
_active = 0
_module = None
_loaded_from = None      # folder of the fetched copy in use, or "" for the built-in one


def _dir():
    return config.APP_DIR / "yt-dlp"


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def _purge():
    for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


def _drop_from_path():
    root = str(_dir())
    sys.path[:] = [p for p in sys.path if not p.startswith(root)]


def _fetched():
    """Fetched copies on disk as [(version tuple, folder)], newest first."""
    d = _dir()
    if not d.is_dir():
        return []
    out = [(_vtuple(p.name), p) for p in d.iterdir()
           if p.is_dir() and (p / "yt_dlp" / "__init__.py").exists() and not p.name.endswith(".tmp")]
    return sorted(out, reverse=True)


def _import_builtin():
    _drop_from_path()
    _purge()
    import yt_dlp
    return yt_dlp


def _load_best():
    """Import the newest yt-dlp available: a fetched copy if it beats the built-in one."""
    global _loaded_from
    fetched = _fetched()
    want = str(fetched[0][1]) if fetched else ""
    if _module is not None and _loaded_from == want:
        return _module
    mod = _import_builtin()
    builtin = _vtuple(mod.version.__version__)
    _loaded_from = ""
    for version, folder in fetched:
        if version <= builtin:
            shutil.rmtree(folder, ignore_errors=True)       # the app itself now ships something newer
            continue
        try:
            _purge()
            sys.path.insert(0, str(folder))
            import yt_dlp
            yt_dlp.YoutubeDL                                 # touch it: a broken copy fails here
            mod, _loaded_from = yt_dlp, str(folder)
            break
        except Exception as e:
            applog.write(f"Fetched yt-dlp {folder.name} wouldn't load ({e}); removing it")
            shutil.rmtree(folder, ignore_errors=True)
            mod = _import_builtin()
    # Keep only the copy in use.
    for _, folder in _fetched():
        if str(folder) != _loaded_from:
            shutil.rmtree(folder, ignore_errors=True)
    return mod


@contextmanager
def session():
    """`with ytdlp.session() as yt_dlp:` gives the newest yt-dlp. A newer fetched version is
    switched in here, but only when nothing else is using yt-dlp at that moment."""
    global _active, _module
    with _lock:
        if _active == 0 or _module is None:
            _module = _load_best()
        _active += 1
        mod = _module
    try:
        yield mod
    finally:
        with _lock:
            _active -= 1


def version():
    with session() as yt_dlp:
        return yt_dlp.version.__version__


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "ShootClipper"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def update(log=None):
    """Fetch the newest yt-dlp from PyPI if it beats the one in use. Returns the new
    version string, or None if there was nothing newer (or it couldn't be fetched)."""
    try:
        current = version()
        info = json.loads(_get(PYPI))
        latest = info["info"]["version"]
        if _vtuple(latest) <= _vtuple(current):
            return None
        wheel = next(f for f in info["urls"] if f["packagetype"] == "bdist_wheel"
                     and f["filename"].endswith("py3-none-any.whl"))
        blob = _get(wheel["url"], timeout=120)
        if hashlib.sha256(blob).hexdigest() != wheel["digests"]["sha256"]:
            raise RuntimeError("the download didn't match PyPI's checksum")
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp, final = d / f"{latest}.tmp", d / latest
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(final, ignore_errors=True)
        archive = d / "download.whl"
        archive.write_bytes(blob)
        with zipfile.ZipFile(archive) as z:
            names = [n for n in z.namelist() if n.startswith("yt_dlp/") and ".." not in n]
            z.extractall(tmp, names)
        archive.unlink(missing_ok=True)
        tmp.rename(final)
    except Exception as e:
        applog.write(f"yt-dlp update check failed: {e}")
        return None
    if log:
        log(f"   updated the YouTube downloader to {latest} (was {current})")
    applog.write(f"yt-dlp updated to {latest} (was {current})")
    return latest


def update_if_due(log=None):
    """The once-a-day check, run in the background when the app opens."""
    stamp = _dir() / "checked"
    try:
        if stamp.exists() and time.time() - stamp.stat().st_mtime < CHECK_EVERY:
            return None
        _dir().mkdir(parents=True, exist_ok=True)
        stamp.write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
    except OSError:
        return None
    return update(log)
