"""Caption proofreading, cold opens, learning from views, spend totals, reminders, covers."""
import datetime as dt
import json
from pathlib import Path

import pytest

from clipper import analyze, config, db, llm, pipeline, proof, remind, render, transcribe, util
from clipper.api import Api
from conftest import needs_ffmpeg


def transcript(tmp_path, text, start=10.0):
    words = [{"w": w, "s": round(start + i * 0.5, 2), "e": round(start + i * 0.5 + 0.4, 2)}
             for i, w in enumerate(text.split())]
    data = {"duration": 600.0, "partial": False, "ranges": [[0.0, 600.0]],
            "segments": [{"start": start, "end": words[-1]["e"], "text": text, "words": words}]}
    transcribe.save(tmp_path, data, "t")
    return data


def an_interview(tmp_path, clip=None):
    iid = db.add_interview("Grilling JR with Jim Herd", workdir=str(tmp_path))
    db.add_clips(iid, [clip or {"title": "Firing Flair", "start": 10.0, "end": 20.0, "speaker": "Jim Herd"}])
    return db.get_interview(iid), db.list_clips(iid)[0]


# ── proofreading ────────────────────────────────────────────────────────────

def test_misheard_names_are_fixed_and_saved(cfg, claude, log, tmp_path):
    data = transcript(tmp_path, "so Jim Hurd calls Rick Flare and fires him over the phone that night")
    iv, clip = an_interview(tmp_path)
    claude.answers["fixes"] = {"fixes": [{"n": 2, "to": "Herd"}, {"n": 4, "to": "Ric"}, {"n": 5, "to": "Flair"}]}
    proof.ensure(cfg, iv, clip, data, log)
    text = " ".join(w["w"] for w in transcribe.load(tmp_path)["segments"][0]["words"])
    assert text.startswith("so Jim Herd calls Ric Flair and fires him")
    assert "Hurd → Herd" in log.text()
    body = claude.requests[0]
    assert body["model"] == "claude-haiku-5-5" and "2:Hurd" in body["messages"][0]["content"]
    # Once per clip: rendering again doesn't ask again.
    proof.ensure(cfg, iv, db.get_clip(clip["id"]), data, log)
    assert len(claude.requests) == 1


def test_proofreader_cannot_add_drop_or_rewrite(cfg, claude, log, tmp_path):
    original = "so Jim Hurd calls Rick Flare and fires him over the phone that night"
    data = transcript(tmp_path, original)
    iv, clip = an_interview(tmp_path)
    claude.answers["fixes"] = {"fixes": [{"n": 2, "to": "Herd the boss"}, {"n": 99, "to": "x"}, {"n": 0, "to": ""},
                                         {"n": "two", "to": "x"}, {"n": 3, "to": "calls"}]}
    proof.ensure(cfg, iv, clip, data, log)
    assert " ".join(w["w"] for w in transcribe.load(tmp_path)["segments"][0]["words"]) == original

    iv2, clip2 = an_interview(tmp_path)
    claude.answers["fixes"] = {"fixes": [{"n": i, "to": f"changed{i}"} for i in range(12)]}
    out = proof.ensure(cfg, iv2, clip2, data, log)
    assert "changed too much" in log.text() or "change too much" in log.text()
    assert " ".join(w["w"] for w in out["segments"][0]["words"]) == original


def test_proofreading_can_be_switched_off_and_never_blocks_a_render(cfg, claude, log, tmp_path):
    data = transcript(tmp_path, "so Jim Hurd calls Rick Flare and fires him over the phone that night")
    iv, clip = an_interview(tmp_path)
    cfg["proofread_captions"] = False
    proof.ensure(cfg, iv, clip, data, log)
    assert claude.requests == []
    cfg["proofread_captions"] = True
    claude.on_request = lambda body: ("__error__", 401, "invalid x-api-key")
    assert proof.ensure(cfg, iv, clip, data, log) is not None
    assert "proofreading skipped" in log.text()


# ── cold open ───────────────────────────────────────────────────────────────

def test_teaser_rules():
    clip = {"start": 100.0, "end": 160.0}
    assert render.teaser_span(dict(clip, teaser_start=140.0, teaser_end=142.5)) == (140.0, 142.5)
    assert render.teaser_span(dict(clip, teaser_start=0, teaser_end=0)) is None
    assert render.teaser_span(dict(clip, teaser_start=101.0, teaser_end=103.0)) is None     # just the opening line
    assert render.teaser_span(dict(clip, teaser_start=140.0, teaser_end=150.0)) is None     # too long
    assert render.teaser_span(dict(clip, teaser_start=159.0, teaser_end=163.0)) is None     # outside the clip
    assert render.teaser_span(clip) is None


@needs_ffmpeg
def test_cold_open_goes_in_front_of_the_clip(cfg, media, claude, log):
    from clipper import ingest
    cfg.update(cold_open=True, proofread_captions=False, framing_mode="face only")
    iid = ingest.add_local_file(cfg, str(media.interview), "None / unknown")
    iv = db.get_interview(iid)
    words = [{"w": f"w{i}", "s": 5 + i * 0.5, "e": 5.4 + i * 0.5} for i in range(40)]
    transcribe.save(iv["workdir"], {"duration": 40.0, "partial": False, "ranges": [[0, 40.0]],
                                    "segments": [{"start": 5, "end": 25, "text": "", "words": words}]}, "t")
    db.add_clips(iid, [{"title": "Bus fire", "start": 5.0, "end": 17.0, "teaser_start": 13.0, "teaser_end": 15.0,
                        "speaker": "Hulk Hogan"}])
    cid = db.list_clips(iid)[0]["id"]
    out = pipeline.render_clip(cfg, cid, log)
    assert util.probe(out)["duration"] == pytest.approx(14.0, abs=0.4)       # 2 s teaser + 12 s clip
    assert "cold open added" in log.text()
    cfg["cold_open"] = False
    assert util.probe(pipeline.render_clip(cfg, cid, log))["duration"] == pytest.approx(12.0, abs=0.4)


def test_teaser_times_are_stored_with_the_clip(tmp_path):
    iv, clip = an_interview(tmp_path, {"title": "t", "start": 10, "end": 60, "teaser_start": 40.5, "teaser_end": 43})
    assert (clip["teaser_start"], clip["teaser_end"]) == (40.5, 43.0)


# ── learning from views ─────────────────────────────────────────────────────

def posted(n):
    iid = db.add_interview("Shoot", workdir="x")
    db.update_interview(iid, source_label="RF Video")
    db.add_clips(iid, [{"title": f"Clip {i}", "start": 0, "end": 30 + i * 8, "speaker": "Jim Cornette" if i % 2 else "Kevin Nash"}
                       for i in range(n)])
    for i, c in enumerate(db.list_clips(iid)):
        db.update_clip(c["id"], status="posted", views=(i + 1) * 1000, posted_at=f"2026-11-{i + 1:02d}T19:30")


def test_view_history_needs_enough_clips_to_mean_something():
    posted(5)
    assert analyze.performance_notes() == ""


def test_view_history_reaches_the_clip_picker(tmp_path):
    posted(10)
    notes = analyze.performance_notes()
    assert "10 posted clips" in notes and "Best performers:" in notes and "10,000 views" in notes
    assert "By speaker:" in notes and "Jim Cornette" in notes
    data = {"duration": 100.0, "segments": [{"start": 0, "end": 5, "text": "hi", "words": []}]}
    iv = {"id": 1, "title": "t", "pick_reason": "", "source_preset": "None", "workdir": str(tmp_path)}
    assert "HOW THIS CHANNEL'S CLIPS HAVE DONE" in analyze.build_context(iv, data, {})
    assert "HOW THIS CHANNEL'S CLIPS HAVE DONE" not in analyze.build_context(iv, data, {}, learn=False)


# ── spend ───────────────────────────────────────────────────────────────────

def test_every_claude_call_lands_in_the_monthly_total(cfg, claude):
    claude.answers["fixes"] = {"fixes": []}
    claude.usage = {"input_tokens": 1_000_000, "output_tokens": 0}
    _, responses = llm.structured(llm.client(), model="claude-sonnet-5-5", schema=proof.SCHEMA,
                                  messages=[{"role": "user", "content": "x"}])
    assert llm.spend("clip picks", "claude-sonnet-5-5", responses) == pytest.approx(2.0)
    llm.spend("screening", "claude-haiku-5-5", responses)
    last_month = (dt.datetime.now().replace(day=1) - dt.timedelta(days=3)).timestamp()
    with db._conn() as c:
        c.execute("INSERT INTO usage (at, kind, model, cost) VALUES (?,?,?,?)", (last_month, "news", "m", 0.5))
    s = Api().spend()["data"]
    assert s["total"] == pytest.approx(2.10) and s["calls"] == 2 and s["last_total"] == pytest.approx(0.5)
    assert s["by_kind"][0] == {"kind": "clip picks", "calls": 1, "cost": 2.0}


# ── reminders ───────────────────────────────────────────────────────────────

def scheduled(title, at):
    iid = db.add_interview("Shoot", workdir="x")
    db.add_clips(iid, [{"title": title, "start": 0, "end": 30}])
    cid = db.list_clips(iid)[0]["id"]
    db.update_clip(cid, status="scheduled", scheduled_for="label", scheduled_at=at.isoformat(timespec="minutes"))
    return cid


def test_due_clips_are_announced_once():
    now = dt.datetime(2026, 11, 16, 19, 31)
    scheduled("Bus <fire> & more", now - dt.timedelta(minutes=1))
    scheduled("Tomorrow's clip", now + dt.timedelta(days=1))
    scheduled("Missed while the PC was off", now - dt.timedelta(days=2))
    shown = []
    assert remind.check(now, show=lambda t, b: shown.append((t, b))) == 1
    assert shown == [("Time to post", "“Bus <fire> & more” is scheduled for now. It's in Ready to post.")]
    assert remind.check(now, show=lambda t, b: shown.append((t, b))) == 0 and len(shown) == 1
    assert remind.check(now + dt.timedelta(days=1, minutes=5), show=lambda t, b: shown.append((t, b))) == 1


def test_notification_text_is_escaped():
    xml = remind.toast_xml("Time to post", "“Bus <fire> & 'more'”")
    assert "&lt;fire&gt; &amp; &apos;more&apos;" in xml and "<fire>" not in xml


def test_scheduling_records_a_real_time(cfg):
    cid = scheduled("x", dt.datetime(2020, 1, 1))
    db.update_clip(cid, status="rendered", scheduled_for="", scheduled_at="", reminded=1)
    api = Api()
    label = api.schedule(cid)["data"]
    c = db.get_clip(cid)
    assert label == c["scheduled_for"] and dt.datetime.fromisoformat(c["scheduled_at"]) > dt.datetime.now()
    assert c["reminded"] == 0


# ── covers, support log, settings ───────────────────────────────────────────

@needs_ffmpeg
def test_cover_frame_is_saved_next_to_the_video(cfg, media):
    import shutil
    ready = pipeline.ready_dir(cfg)
    video = ready / "0001_clip.mp4"
    shutil.copy(media.capture, video)
    cid = scheduled("x", dt.datetime(2030, 1, 1))
    db.update_clip(cid, output_path=str(video))
    out = Api().save_cover(cid, 3.5)["data"]
    assert Path(out["path"]).name == "0001_clip.cover.jpg" and Path(out["path"]).stat().st_size > 1000
    assert db.get_clip(cid)["cover"] == out["path"]


def test_support_log_has_version_and_recent_lines(cfg):
    from clipper import applog, updater
    applog.write("something specific happened")
    text = Api().support_log()["data"]
    assert updater.version() in text and "something specific happened" in text


def test_folder_no_longer_opens_by_itself_after_updating():
    config.CONFIG_PATH.write_text(json.dumps({"config_version": 2, "open_folder_when_done": True,
                                              "gameplay_speed": 1.5}), encoding="utf-8")
    cfg = config.load()
    assert cfg["open_folder_when_done"] is False and cfg["gameplay_speed"] == 1.5
    cfg["open_folder_when_done"] = True                     # turning it back on afterwards sticks
    config.save(cfg)
    assert config.load()["open_folder_when_done"] is True
