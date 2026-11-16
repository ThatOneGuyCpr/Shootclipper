"""Disk cleanup: downloaded interviews and finished clips.

Two settings, both in days (0 = never):
  cleanup_downloads_days  delete an interview's downloaded video this long after its clips
                          were made. Transcripts stay, so its clips still show in Review.
  cleanup_clips_days      delete a finished clip's video this long after it was rendered.
                          Scheduled clips are always kept. Posted clips keep their row
                          (and view counts) in History.

Only files inside the workspace are ever deleted. A video you added from your own
folders (Add files) is never touched.
"""
import shutil
import time
from pathlib import Path

from . import db

DAY = 86400
SOURCE_GLOBS = ("source.*", "proxy*.mp4", "preview*.mp4")


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception:
        return False


def _size(paths):
    return sum(p.stat().st_size for p in paths if p.is_file())


def _download_files(iv, workspace):
    """Big media files in an interview's folder that we created (never a user's original)."""
    wd = Path(iv.get("workdir") or "")
    if not iv.get("workdir") or not wd.is_dir() or not _inside(wd, workspace):
        return []
    files = [f for g in SOURCE_GLOBS for f in wd.glob(g) if f.is_file()] + list(wd.glob("*.part"))
    src = iv.get("source_path") or ""
    if src and not _inside(src, wd):
        # Added from your own folder: only the preview copy in here is ours.
        files = [f for f in files if not f.name.startswith("source.")]
    return list(dict.fromkeys(files))


def _clip_files(c, workspace):
    out = c.get("output_path") or ""
    if not out or not _inside(out, workspace):
        return []
    p = Path(out)
    return [f for f in (p, p.with_suffix(".txt"), p.with_suffix(".cover.jpg")) if f.is_file()]


def _finished_at(iv):
    """When an interview's clips were made: the transcript's age, else the folder's."""
    wd = Path(iv.get("workdir") or "")
    for name in ("transcript.json", "energy.json"):
        if (wd / name).exists():
            return (wd / name).stat().st_mtime
    return wd.stat().st_mtime if wd.exists() else time.time()


def usage(cfg):
    ws = cfg["workspace"]
    ivs = db.list_interviews()
    dl = [f for iv in ivs for f in _download_files(iv, ws)]
    clips = [f for c in db.list_clips() for f in _clip_files(c, ws) if f.suffix == ".mp4"]
    return {"downloads": _size(dl), "n_downloads": sum(1 for iv in ivs if _download_files(iv, ws)),
            "clips": _size(clips), "n_clips": len(clips)}


def delete_interview_files(cfg, iv):
    """Remove an interview's whole folder (downloads, transcript, previews). Returns bytes freed."""
    wd = Path(iv.get("workdir") or "")
    if not iv.get("workdir") or not wd.is_dir() or not _inside(wd, cfg["workspace"]):
        return 0
    if Path(cfg["workspace"]).resolve() == wd.resolve():
        return 0
    freed = _size(list(wd.rglob("*")))
    shutil.rmtree(wd, ignore_errors=True)
    return freed


def run(cfg, log, downloads_days=None, clips_days=None, now=None):
    """Apply the cleanup rules. Returns (files deleted, bytes freed)."""
    now = now or time.time()
    ws = cfg["workspace"]
    dd = int(cfg.get("cleanup_downloads_days", 0) if downloads_days is None else downloads_days)
    cd = int(cfg.get("cleanup_clips_days", 0) if clips_days is None else clips_days)
    n = freed = 0

    if dd > 0:
        for iv in db.list_interviews():
            if iv["status"] not in ("analyzed", "done"):
                continue
            if now - _finished_at(iv) < dd * DAY:
                continue
            for f in _download_files(iv, ws):
                freed += f.stat().st_size
                f.unlink(missing_ok=True)
                n += 1

    if cd > 0:
        for c in db.list_clips(statuses=["rendered", "posted"]):
            files = _clip_files(c, ws)
            if not files or now - files[0].stat().st_mtime < cd * DAY:
                continue
            for f in files:
                freed += f.stat().st_size
                f.unlink(missing_ok=True)
                n += 1
            if c["status"] == "rendered":
                # Never posted: take it off the to-post list too.
                db.update_clip(c["id"], status="rejected", output_path="")
            else:
                db.update_clip(c["id"], output_path="")

    if n:
        log(f"🧹 Cleaned up {n} old file(s), freed {freed / 1e9:.1f} GB")
    return n, freed
