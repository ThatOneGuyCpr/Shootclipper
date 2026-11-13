"""Runs the steps in order. Used by the GUI and by autopilot.py (scheduled runs).

Interview status flow:
  queued -> downloaded -> transcribed -> prepped -> analyzed -> done
  (needs_paste when analysis mode is Paste; error on failure)

Stop (jobs.Cancelled) unwinds without marking anything as failed: finished steps
stay finished, so pressing Make clips again picks up where it stopped.
"""

MAX_AUTO_RETRIES = 2  # Make clips retries a failed interview this many times, then skips it
import json
import traceback
from pathlib import Path

from . import analyze, cleanup, config, db, gameplay, ingest, jobs, news, render, signals, transcribe, util

STEP_ORDER = ["queued", "downloaded", "transcribed", "prepped", "analyzed", "done"]


def ready_dir(cfg):
    d = Path(cfg["workspace"]) / "Ready to post"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _at_least(status, target):
    try:
        return STEP_ORDER.index(status) >= STEP_ORDER.index(target)
    except ValueError:
        return False


def current_news_text(cfg):
    return news.topics_text(news.load_news(cfg)) if news.load_news(cfg) else ""


def _no_step(name, state):
    pass


def process_interview(cfg, iid, log, progress=lambda p: None, step=_no_step):
    """Bring one interview as far as analysis. Safe to call repeatedly; skips finished steps.
    step(name, state) reports progress to the UI: download / transcribe / analyze."""
    iv = db.get_interview(iid)
    wd = Path(iv["workdir"])
    cur = "download"
    if iv["source_path"] and not Path(iv["source_path"]).exists() and iv.get("url"):
        db.update_interview(iid, source_path="")  # cleaned up earlier: download again
        iv = db.get_interview(iid)
    try:
        # 1. download
        if iv["source_path"]:
            step("download", "skip")
        else:
            step("download", "active")
            log(f"⬇ Downloading {iv['url']}")
            path, title = ingest.download(iv, log)
            info = util.probe(path)
            db.update_interview(iid, source_path=str(path), title=title,
                                duration=info["duration"], status="downloaded")
            iv = db.get_interview(iid)
            log(f"   got “{title}” ({util.ts(info['duration'])})")
            step("download", "done")

        # 2. transcribe
        jobs.check()
        cur = "transcribe"
        data = transcribe.load(wd)
        step("transcribe", "active" if data is None or not (wd / "energy.json").exists() else "skip")
        if data is None:
            log(f"✎ Transcribing “{iv['title']}”")
            data = transcribe.transcribe(iv["source_path"], iv["title"], wd, cfg, log, progress)
            db.update_interview(iid, status="transcribed")
        progress(1.0)

        # 3. local signals
        jobs.check()
        if not (wd / "energy.json").exists():
            log("   mapping audio energy and frames…")
            signals.energy_map(iv["source_path"], wd)
            if util.probe(iv["source_path"])["has_video"]:
                signals.contact_sheet(iv["source_path"], wd)
            db.update_interview(iid, status="prepped")
            step("transcribe", "done")
        iv = db.get_interview(iid)

        # 4. analysis
        jobs.check()
        cur = "analyze"
        if iv["status"] in ("analyzed", "done"):
            step("analyze", "skip")
            return iv
        step("analyze", "active")
        if cfg.get("analysis_mode") == "paste":
            step("analyze", "wait")
            db.update_interview(iid, status="needs_paste")
            log("   ready for Claude: use “Copy packet”, paste into your Project, then “Paste reply”.")
            return db.get_interview(iid)
        energy = json.loads((wd / "energy.json").read_text())
        contact = wd / "contact_sheet.jpg"
        result, cost = analyze.run_api(iv, data, energy, contact if contact.exists() else None,
                                       cfg["model"], log, current_news_text(cfg))
        n = analyze.ingest_result(iv, data, result)
        db.update_interview(iid, status="analyzed", cost=(iv.get("cost") or 0) + cost, error="",
                            attempts=0)
        log(f"✓ {n} clip ideas from “{iv['title']}”")
        step("analyze", "done")
        return db.get_interview(iid)
    except Exception as e:
        if isinstance(e, jobs.Cancelled) or jobs.requested():
            step(cur, "stopped")
            raise jobs.Cancelled() from None
        step(cur, "error")
        db.update_interview(iid, status="error", error=str(e)[:500],
                            attempts=(iv.get("attempts") or 0) + 1)
        log(f"✗ {iv['title']}: {e}")
        log(traceback.format_exc(limit=2))
        raise


def packet_for(cfg, iid):
    iv = db.get_interview(iid)
    wd = Path(iv["workdir"])
    data = transcribe.load(wd)
    energy = json.loads((wd / "energy.json").read_text()) if (wd / "energy.json").exists() else {}
    return analyze.build_packet(iv, data, energy, current_news_text(cfg)), wd / "contact_sheet.jpg"


def accept_pasted(cfg, iid, text):
    iv = db.get_interview(iid)
    data = transcribe.load(iv["workdir"])
    result = analyze.parse_reply(text)
    n = analyze.ingest_result(iv, data, result)
    db.update_interview(iid, status="analyzed", error="")
    return n


def match_gameplay(cfg, clip, iv, data):
    text = transcribe.text_between(data, clip["start"], clip["end"]) if data else ""
    return gameplay.pick(cfg.get("gameplay_dir", ""), clip, iv, text)


def choose_layout(cfg, clip, iv, data, log):
    """Autopilot's framing decision. Alternate mode gives you an even A/B split to compare."""
    mode = cfg.get("framing_mode", "alternate")
    if mode == "face only" or clip["framing"] == "wide" or not cfg.get("gameplay_dir"):
        return
    game = match_gameplay(cfg, clip, iv, data)
    if not game:
        log(f"      no 2K match for “{clip['title']}”")
        return
    if mode == "alternate":
        done = db.list_clips(statuses=["rendered", "scheduled", "posted"])
        n_split = sum(1 for c in done if c["framing"] == "split")
        if n_split > len(done) - n_split:
            return
    db.update_clip(clip["id"], framing="split", gameplay=game)


def ensure_source(iv, log):
    """Re-download an interview's video if cleanup removed it. Returns the fresh row."""
    if iv["source_path"] and Path(iv["source_path"]).exists():
        return iv
    if not iv.get("url"):
        raise RuntimeError("This interview's video file is gone, and there's no link to download it again.")
    log(f"⬇ The video for “{iv['title']}” was cleaned up; downloading it again")
    path, _ = ingest.download(iv, log)
    db.update_interview(iv["id"], source_path=str(path))
    return db.get_interview(iv["id"])


def render_clip(cfg, clip_id, log):
    clip = db.get_clip(clip_id)
    iv = ensure_source(db.get_interview(clip["interview_id"]), log)
    data = transcribe.load(iv["workdir"])
    if clip["framing"] == "split" and not (clip.get("gameplay") and Path(clip["gameplay"]).exists()):
        game = match_gameplay(cfg, clip, iv, data)
        if game:
            db.update_clip(clip_id, gameplay=game)
            clip = db.get_clip(clip_id)
    if clip["framing"] == "split" and clip.get("gameplay"):
        text = transcribe.text_between(data, clip["start"], clip["end"]) if data else ""
        clip["gameplay_extra"] = gameplay.siblings(cfg.get("gameplay_dir", ""), clip["gameplay"],
                                                   clip, iv, text)
    log(f"🎬 Rendering “{clip['title']}” ({util.ts(clip['end'] - clip['start'])})")
    out = render.render_clip(cfg, iv, clip, data, ready_dir(cfg), log)
    db.update_clip(clip_id, status="rendered", output_path=str(out))
    log(f"   saved {out.name}")
    return out


def render_approved(cfg, log, interview_id=None):
    outs = []
    for c in db.list_clips(interview_id, statuses=["approved"]):
        try:
            outs.append(render_clip(cfg, c["id"], log))
        except jobs.Cancelled:
            raise
        except Exception as e:
            log(f"✗ render failed for “{c['title']}”: {e}")
    return outs


def auto_pick_and_render(cfg, iid, log):
    clips = db.list_clips(iid, statuses=["suggested"])
    if not clips:
        log("   no suggestions to render")
        return []
    want = int(cfg.get("auto_clips", 2))
    floor = int(cfg.get("auto_min_rating", 4))
    # Story strength first; timeliness and crossover break ties.
    clips.sort(key=lambda c: (c["rating"], c["why"].startswith("[Timely"), c["crossover"]),
               reverse=True)
    chosen = [c for c in clips if c["rating"] >= floor][:want] or clips[:1]
    outs = []
    data = transcribe.load(db.get_interview(iid)["workdir"])
    for c in chosen:
        db.update_clip(c["id"], status="approved")
        choose_layout(cfg, db.get_clip(c["id"]), db.get_interview(iid), data, log)
        try:
            jobs.check()
            outs.append(render_clip(cfg, c["id"], log))
        except jobs.Cancelled:
            raise
        except Exception as e:
            log(f"✗ render failed for “{c['title']}”: {e}")
    db.update_interview(iid, status="done")
    return outs


def resumable(log):
    """The oldest interview Make clips should continue: anything queued or part-done, plus
    failed ones that haven't failed MAX_AUTO_RETRIES times yet."""
    for i in reversed(db.list_interviews()):  # list is newest-first
        st = i["status"]
        if st in ("analyzed", "done", "needs_paste", "archived"):
            continue
        if st == "error":
            if (i.get("attempts") or 0) >= MAX_AUTO_RETRIES:
                continue
            log(f"↻ Trying “{i['title']}” again (it stopped with an error last time)")
        return i["id"]
    return None


def fresh_picks(cfg):
    """Found interviews you haven't used or turned down yet."""
    used = {i["url"] for i in db.list_interviews() if i.get("url")}
    gone = news.dismissed(cfg)
    return [p for p in news.load_picks(cfg) if p["url"] not in used and p["url"] not in gone]


def find_candidates(cfg, log, more=False):
    """Make sure there are interviews to choose from. Returns them."""
    fresh = [] if more else fresh_picks(cfg)
    if len(fresh) >= 3:
        return fresh
    n = news.load_news(cfg)
    if not n:
        return fresh
    news.discover(cfg, n, log)
    return fresh_picks(cfg)


def queue_pick(cfg, url):
    p = next((p for p in news.load_picks(cfg) if p["url"] == url), None)
    if not p:
        raise ValueError("That suggestion is no longer in the list.")
    iid = ingest.add_url(cfg, p["url"], "None / unknown")
    db.update_interview(iid, title=p["title"], pick_reason=p["why"], topic=p.get("topic", ""))
    return iid


def next_source(cfg, log):
    """Automatic choice: something you queued or a part-done interview first; otherwise the
    top found interview."""
    iid = resumable(log)
    if iid:
        return iid
    if cfg.get("analysis_mode") == "paste":
        return None
    fresh = find_candidates(cfg, log)
    if not fresh:
        return None
    p = fresh[0]
    log(f"➜ Picked: {p['title']} ({p['channel']}). {p['why']}")
    return queue_pick(cfg, p["url"])


def go(cfg, log, progress=lambda p: None, step=_no_step, iid=None, ask=False):
    """The one button: news check -> choose a source -> process -> render the best clips.

    iid: process this interview (from a Make clips button on a card or a found interview).
    ask: with nothing queued, find interviews and stop so you can choose one, instead of
         picking automatically. The daily run never asks.
    """
    db.init()
    if iid is None:
        iid = resumable(log)
    if iid is None:
        if cfg.get("analysis_mode") == "api" and not news.news_is_fresh(cfg):
            try:
                news.scout(cfg, log)
            except jobs.Cancelled:
                raise
            except Exception as e:
                log(f"   news scout skipped: {e}")
        jobs.check()
        if ask and cfg.get("analysis_mode") == "api":
            found = find_candidates(cfg, log)
            if found:
                log(f"☞ Found {len(found)} interviews. Pick one under Found interviews and press "
                    "Make clips on it.")
            else:
                log("No interviews found. Paste a link, or press Find more.")
            return []
        iid = next_source(cfg, log)
    if iid is None:
        log("Nothing queued. Paste a link, add a file, or switch to API mode for automatic picks.")
        return []
    iv = process_interview(cfg, iid, log, progress, step)
    if iv["status"] != "analyzed":
        return []
    step("render", "active")
    outs = auto_pick_and_render(cfg, iid, log)
    step("render", "done" if outs else "error")
    if outs:
        log(f"★ Done. {len(outs)} clip(s) in “Ready to post”. Review them before posting.")
    try:
        cleanup.run(cfg, log)
    except Exception as e:
        log(f"   cleanup skipped: {e}")
    return outs
