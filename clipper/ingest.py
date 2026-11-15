"""Getting source media into the workspace: YouTube links or local files."""
from pathlib import Path

from . import db, jobs, util, ytdlp


def make_workdir(workspace, iid, title):
    d = Path(workspace) / "interviews" / f"{iid:03d}_{util.slug(title, 40)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_local_file(cfg, path, preset):
    path = Path(path)
    title = path.stem
    try:
        info = util.probe(path)      # before anything is added, so a bad file leaves no trace
    except Exception:
        info = None
    if not info or info["duration"] <= 0:
        raise ValueError(f"“{path.name}” isn't a video or audio file that can be read.")
    iid = db.add_interview(title, source_path=str(path), preset=preset)
    wd = make_workdir(cfg["workspace"], iid, title)
    db.update_interview(iid, workdir=str(wd), duration=info["duration"], status="queued")
    return iid


def add_url(cfg, url, preset):
    iid = db.add_interview(url, url=url, preset=preset)
    wd = make_workdir(cfg["workspace"], iid, "download")
    db.update_interview(iid, workdir=str(wd), status="queued")
    return iid


def download(interview, log):
    """Download with yt-dlp into the interview's workdir. Returns (path, title).
    If it fails and a newer yt-dlp is out (YouTube changes often), fetch it and try once more."""
    try:
        return _download(interview, log)
    except jobs.Cancelled:
        raise
    except Exception as e:
        if not ytdlp.update(log):
            raise RuntimeError(_plain_error(e)) from e
        log("   trying the download again…")
        try:
            return _download(interview, log)
        except jobs.Cancelled:
            raise
        except Exception as e2:
            raise RuntimeError(_plain_error(e2)) from e2


def _plain_error(e):
    """yt-dlp's messages, minus the colour codes and its own 'ERROR:' prefix."""
    import re
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    return f"Download failed: {text[:300]}"


def _download(interview, log):
    wd = Path(interview["workdir"])
    last = {"pct": -10}

    def hook(d):
        jobs.check()  # Stop pressed: abort the download (the partial file resumes next time)
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                pct = int(d.get("downloaded_bytes", 0) * 100 / total)
                if pct >= last["pct"] + 10:
                    last["pct"] = pct
                    log(f"   downloading… {pct}%")

    opts = {
        # 1080p max is plenty for a 9:16 crop and keeps files manageable.
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(wd / "source.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [hook],
    }
    with ytdlp.session() as yt_dlp, yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(interview["url"], download=True)
        except Exception:
            jobs.check()
            raise
        title = info.get("title") or "interview"
    path = next((p for p in wd.glob("source.*") if p.suffix.lower() in
                 (".mp4", ".mkv", ".webm", ".mov")), None)
    if not path:
        raise RuntimeError("Download finished but no video file was found.")
    return path, title
