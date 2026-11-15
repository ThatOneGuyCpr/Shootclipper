"""Storage cleanup never deletes what it shouldn't."""
import os
import time
from pathlib import Path

from clipper import cleanup, db, pipeline

OLD = time.time() - 40 * 86400


def aged(path, data=b"0" * 1000):
    path.write_bytes(data)
    os.utime(path, (OLD, OLD))
    return path


def interview(cfg, name, status="done", source=None, url="https://youtu.be/x"):
    iid = db.add_interview(name, url=url, workdir=str(Path(cfg["workspace"]) / "interviews" / name))
    wd = Path(db.get_interview(iid)["workdir"])
    wd.mkdir(parents=True)
    aged(wd / "transcript.json", b"{}")
    src = source or aged(wd / "source.mp4")
    aged(wd / "preview_proxy.mp4")
    db.update_interview(iid, source_path=str(src), status=status)
    return iid, wd


def test_old_downloads_go_but_transcripts_stay(cfg, log):
    iid, wd = interview(cfg, "old")
    n, freed = cleanup.run(cfg, log)
    assert n == 2 and freed == 2000
    assert not (wd / "source.mp4").exists() and not (wd / "preview_proxy.mp4").exists()
    assert (wd / "transcript.json").exists()


def test_your_own_files_are_never_deleted(cfg, log, tmp_path):
    mine = aged(tmp_path / "my_recording.mp4")
    iid, wd = interview(cfg, "local", source=mine, url="")
    cleanup.run(cfg, log)
    assert mine.exists()
    assert not (wd / "preview_proxy.mp4").exists()         # the app's own preview copy does go


def test_unfinished_and_recent_interviews_are_left_alone(cfg, log):
    _, wd_queue = interview(cfg, "queued", status="downloaded")
    _, wd_new = interview(cfg, "new")
    now = time.time()
    os.utime(wd_new / "transcript.json", (now, now))
    assert cleanup.run(cfg, log) == (0, 0)
    assert (wd_queue / "source.mp4").exists() and (wd_new / "source.mp4").exists()


def test_old_clips_go_except_scheduled_ones(cfg, log):
    iid, _ = interview(cfg, "clips")
    db.add_clips(iid, [{"title": t, "start": 0, "end": 60} for t in ("rendered", "posted", "scheduled")])
    ready = pipeline.ready_dir(cfg)
    for c in db.list_clips(iid):
        f = aged(ready / f"{c['title']}.mp4")
        aged(f.with_suffix(".txt"), b"caption")
        db.update_clip(c["id"], status=c["title"], output_path=str(f), views=1234 if c["title"] == "posted" else 0)
    cfg["cleanup_downloads_days"] = 0
    cleanup.run(cfg, log)
    by = {c["title"]: c for c in db.list_clips()} | {c["title"]: db.get_clip(c["id"]) for c in db.list_clips(iid)}
    assert not (ready / "rendered.mp4").exists() and not (ready / "rendered.txt").exists()
    assert (ready / "scheduled.mp4").exists() and by["scheduled"]["status"] == "scheduled"
    assert by["posted"]["status"] == "posted" and by["posted"]["views"] == 1234 and by["posted"]["output_path"] == ""


def test_never_means_never(cfg, log):
    _, wd = interview(cfg, "keep")
    cfg["cleanup_downloads_days"] = 0
    cfg["cleanup_clips_days"] = 0
    assert cleanup.run(cfg, log) == (0, 0) and (wd / "source.mp4").exists()


def test_files_outside_the_workspace_are_out_of_reach(cfg, log, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    aged(outside / "source.mp4")
    iid = db.add_interview("odd", url="https://youtu.be/x", workdir=str(outside))
    aged(outside / "transcript.json", b"{}")
    db.update_interview(iid, source_path=str(outside / "source.mp4"), status="done")
    cleanup.run(cfg, log)
    assert (outside / "source.mp4").exists()
    assert cleanup.delete_interview_files(cfg, db.get_interview(iid)) == 0 and outside.exists()


def test_cleaned_source_downloads_again_for_a_re_render(cfg, log, monkeypatch):
    from clipper import ingest
    iid, wd = interview(cfg, "again")
    cleanup.run(cfg, log)

    def download(iv, log):
        (Path(iv["workdir"]) / "source.mp4").write_bytes(b"1")
        return Path(iv["workdir"]) / "source.mp4", "t"
    monkeypatch.setattr(ingest, "download", download)
    iv = pipeline.ensure_source(db.get_interview(iid), log)
    assert Path(iv["source_path"]).exists() and "downloading it again" in log.text()
