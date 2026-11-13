"""Getting source media into the workspace: YouTube links or local files."""
from pathlib import Path

from . import db, util


def make_workdir(workspace, iid, title):
    d = Path(workspace) / "interviews" / f"{iid:03d}_{util.slug(title, 40)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_local_file(cfg, path, preset):
    path = Path(path)
    title = path.stem
    iid = db.add_interview(title, source_path=str(path), preset=preset)
    wd = make_workdir(cfg["workspace"], iid, title)
    info = util.probe(path)
    db.update_interview(iid, workdir=str(wd), duration=info["duration"], status="queued")
    return iid


def add_url(cfg, url, preset):
    iid = db.add_interview(url, url=url, preset=preset)
    wd = make_workdir(cfg["workspace"], iid, "download")
    db.update_interview(iid, workdir=str(wd), status="queued")
    return iid


def download(interview, log):
    """Download with yt-dlp into the interview's workdir. Returns (path, title)."""
    import yt_dlp

    wd = Path(interview["workdir"])
    last = {"pct": -10}

    def hook(d):
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
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(interview["url"], download=True)
        title = info.get("title") or "interview"
    path = next((p for p in wd.glob("source.*") if p.suffix.lower() in
                 (".mp4", ".mkv", ".webm", ".mov")), None)
    if not path:
        raise RuntimeError("Download finished but no video file was found.")
    return path, title
