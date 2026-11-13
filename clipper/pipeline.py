"""Runs the steps in order. Used by the GUI and by autopilot.py (scheduled runs).

Interview status flow:
  queued -> downloaded -> transcribed -> prepped -> analyzed -> done
  (needs_paste when analysis mode is Paste; error on failure)
"""
import json
import traceback
from pathlib import Path

from . import analyze, config, db, gameplay, ingest, news, render, signals, transcribe, util

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
        cur = "transcribe"
        data = transcribe.load(wd)
        step("transcribe", "active" if data is None or not (wd / "energy.json").exists() else "skip")
        if data is None:
            log(f"✎ Transcribing “{iv['title']}”")
            data = transcribe.transcribe(iv["source_path"], iv["title"], wd, cfg, log, progress)
            db.update_interview(iid, status="transcribed")
        progress(1.0)

        # 3. local signals
        if not (wd / "energy.json").exists():
            log("   mapping audio energy and frames…")
            signals.energy_map(iv["source_path"], wd)
            if util.probe(iv["source_path"])["has_video"]:
                signals.contact_sheet(iv["source_path"], wd)
            db.update_interview(iid, status="prepped")
            step("transcribe", "done")
        iv = db.get_interview(iid)

        # 4. analysis
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
        db.update_interview(iid, status="analyzed", cost=(iv.get("cost") or 0) + cost, error="")
        log(f"✓ {n} clip ideas from “{iv['title']}”")
        step("analyze", "done")
        return db.get_interview(iid)
    except Exception as e:
        step(cur, "error")
        db.update_interview(iid, status="error", error=str(e)[:500])
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


def render_clip(cfg, clip_id, log):
    clip = db.get_clip(clip_id)
    iv = db.get_interview(clip["interview_id"])
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
    # Timely clips first, then rating, then crossover.
    clips.sort(key=lambda c: (c["why"].startswith("[Timely"), c["rating"], c["crossover"]),
               reverse=True)
    chosen = [c for c in clips if c["rating"] >= floor][:want] or clips[:1]
    outs = []
    data = transcribe.load(db.get_interview(iid)["workdir"])
    for c in chosen:
        db.update_clip(c["id"], status="approved")
        choose_layout(cfg, db.get_clip(c["id"]), db.get_interview(iid), data, log)
        try:
            outs.append(render_clip(cfg, c["id"], log))
        except Exception as e:
            log(f"✗ render failed for “{c['title']}”: {e}")
    db.update_interview(iid, status="done")
    return outs


def next_source(cfg, log):
    """Manual queue first; otherwise the top unused pick from the news scout."""
    pending = [i for i in db.list_interviews()
               if i["status"] not in ("analyzed", "done", "needs_paste", "error")]
    if pending:
        return pending[-1]["id"]  # oldest first (list is newest-first)
    if cfg.get("analysis_mode") == "paste":
        return None
    picks = news.load_picks(cfg)
    used = {i["url"] for i in db.list_interviews() if i.get("url")}
    fresh = [p for p in picks if p["url"] not in used]
    if not fresh:
        n = news.load_news(cfg)
        if not n:
            return None
        fresh = [p for p in news.discover(cfg, n, log) if p["url"] not in used]
    if not fresh:
        return None
    p = fresh[0]
    log(f"➜ Picked: {p['title']} ({p['channel']}). {p['why']}")
    iid = ingest.add_url(cfg, p["url"], "None / unknown")
    db.update_interview(iid, title=p["title"], pick_reason=p["why"], topic=p.get("topic", ""))
    return iid


def go(cfg, log, progress=lambda p: None, step=_no_step):
    """The one button: news check -> choose a source -> process -> render the best clips."""
    db.init()
    if cfg.get("analysis_mode") == "api" and not news.news_is_fresh(cfg):
        try:
            news.scout(cfg, log)
        except Exception as e:
            log(f"   news scout skipped: {e}")
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
    return outs
