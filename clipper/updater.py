"""Updates.

Installed app (ShootClipper.exe): checks GitHub Releases for a newer version,
downloads its setup file and runs it silently; the installer replaces the program
files and reopens the app.

Source copy (Launch Shoot Clipper.bat): installs a ShootClipper.zip over the folder.

Either way, settings, the library and your text files live in %APPDATA%\\ShootClipper
and are never touched.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = "ThatOneGuyCpr/Shootclipper"
FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
SKIP = {".venv", "__pycache__", ".git"}
CODE_DIRS = ("clipper", "web")


def version(folder=None):
    if folder is None:
        here = Path(__file__).resolve().parent.parent  # VERSION ships beside the code
        candidates = [here / "VERSION", APP_DIR / "VERSION"]
    else:
        candidates = [Path(folder) / "VERSION"]
    for p in candidates:
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return "unknown"


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def is_newer(remote, local):
    return _vtuple(remote) > _vtuple(local)


# ── Installed app: GitHub Releases ──────────────────────────────────────────

def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "ShootClipper",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def check():
    """{'available': bool, 'current', 'latest', 'notes', 'url'} for the newest release."""
    current = version()
    with _get(f"https://api.github.com/repos/{REPO}/releases/latest") as r:
        rel = json.load(r)
    latest = (rel.get("tag_name") or "").lstrip("v")
    asset = next((a for a in rel.get("assets", [])
                  if a["name"].lower().endswith(".exe") and "setup" in a["name"].lower()), None)
    return {"available": bool(asset) and is_newer(latest, current), "current": current,
            "latest": latest, "notes": (rel.get("body") or "").strip()[:1500],
            "url": asset["browser_download_url"] if asset else "", "size": asset["size"] if asset else 0}


def download(url, progress=None):
    dest = Path(tempfile.gettempdir()) / "ShootClipper-update" / url.rsplit("/", 1)[-1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _get(url, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    return dest


def run_installer(setup_path):
    """Start the installer detached; it waits for this app to exit, updates, and reopens it."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(setup_path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                      "/CLOSEAPPLICATIONS"], creationflags=flags, close_fds=True)


# ── Source copy: zip over the folder ────────────────────────────────────────

def _find_root(extracted):
    for p in [extracted, *sorted(extracted.rglob("app.py"))]:
        d = p if p.is_dir() else p.parent
        if (d / "app.py").is_file() and (d / "clipper").is_dir():
            return d
    return None


def apply_zip(zip_path, app_dir=APP_DIR):
    """Copy the program files from zip_path over app_dir. Returns (old, new) versions."""
    zip_path, app_dir = Path(zip_path), Path(app_dir)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("That file isn't a zip.")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(zip_path) as z:
            for name in z.namelist():
                if not str((tmp / name).resolve()).startswith(str(tmp.resolve())):
                    raise ValueError("The zip contains unsafe paths.")
            z.extractall(tmp)
        root = _find_root(tmp)
        if not root:
            raise ValueError("That zip isn't a Shoot Clipper update (no app.py and clipper folder inside).")
        old, new = version(app_dir), version(root)
        for d in CODE_DIRS:
            if not (root / d).is_dir():
                continue
            for f in list((app_dir / d).rglob("*")):
                if f.is_file() and not (SKIP & set(f.relative_to(app_dir).parts)):
                    if not (root / f.relative_to(app_dir)).exists():
                        f.unlink()
        for src in root.rglob("*"):
            rel = src.relative_to(root)
            if SKIP & set(rel.parts):
                continue
            dst = app_dir / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    return old, new
