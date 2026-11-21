"""Runs the steps in order. Used by the GUI and by autopilot.py (scheduled runs).

Interview status flow:
  queued -> downloaded -> transcribed -> prepped -> analyzed -> done
  (needs_paste when analysis mode is Paste; error on failure)

Stop (jobs.Cancelled) unwinds without marking anything as failed: finished steps
stay finished, so pressing Make clips again picks up where it stopped.
"""
import traceback
from pathlib import Path

from . import (analyze, blocklist, cleanup, config, db, gameplay, ingest, jobs, news, proof, render, screen,
               signals, transcribe, util)

MAX_AUTO_RETRIES = 2  # Make clips retries a failed interview this many times, then skips it

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


def wants_screening(cfg, iv):
    """Screening reads YouTube captions with Claude, so it needs a link and API mode."""
    return bool(iv.get("url")) and cfg.get("analysis_mode") == "api" \
        and cfg.get("transcribe_mode", "stories") == "stories"


def story_sections(cfg, iv, wd, log, step=_no_step):
    """Find this interview's story sections from its captions (once; saved as screen.json).
    Returns [[start, end], ...] for Whisper, or [] to transcribe everything."""
    if not wants_screening(cfg, iv):
        step("screen", "skip")
        return []
    result = screen.load(wd)
    if result is None:
        step("screen", "active")
        log("🔎 Reading the captions to find the stories…")
        try:
            result = screen.screen_url(cfg, iv["url"])
        except jobs.Cancelled:
            raise
        except Exception as e:
            result = {"available": False, "reason": str(e)[:160]}
        screen.save(wd, result)
        if result.get("available"):
            db.update_interview(iv["id"], cost=(iv.get("cost") or 0) + (result.get("cost") or 0))
        if result.get("title") and iv["title"] == iv.get("url"):
            db.update_interview(iv["id"], title=result["title"], duration=result.get("duration") or 0)
        step("screen", "done")
    else:
        step("screen", "skip")
    if not result.get("available"):
        log(f"   {result.get('reason') or 'captions unavailable'}; transcribing the whole interview")
        return []
    secs = screen.sections(result.get("stories") or [], result.get("duration") or 0)
    if not secs:
        log("   no clear stories in the captions; transcribing the whole interview to be sure")
        return []
    log(f"   {result.get('story_count', 0)} stories found, about {result.get('story_minutes', 0):g} min "
        f"of a {util.ts(result.get('duration') or 0)} interview")
    return secs


def process_interview(cfg, iid, log, progress=lambda p: None, step=_no_step):
    """Bring one interview as far as analysis. Safe to call repeatedly; skips finished steps.
    step(name, state) reports progress to the UI: screen / download / transcribe / analyze."""
    iv = db.get_interview(iid)
    wd = Path(iv["workdir"])
    cur = "screen"
    if iv["source_path"] and not Path(iv["source_path"]).exists():
        if not iv.get("url"):
            db.update_interview(iid, status="error", attempts=MAX_AUTO_RETRIES,
                                error="The video file was moved or deleted.")
            step("download", "error")
            raise RuntimeError(f"The video file for “{iv['title']}” was moved or deleted: "
                               f"{iv['source_path']}")
        db.update_interview(iid, source_path="")  # cleaned up earlier: download again
        iv = db.get_interview(iid)
    try:
        # 0. first pass over the captions (only needed until there's a transcript)
        data = transcribe.load(wd)
        sections = story_sections(cfg, iv, wd, log, step) if data is None else []
        if data is not None:
            step("screen", "skip")
        iv = db.get_interview(iid)

        # 1. download
        jobs.check()
        cur = "download"
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
            log(f"   got “{title}” ({util.ts(info['duration'])}, {info['width']}x{info['height']})")
            why = blocklist.reason("", title)
            if why:
                log(f"   ⚠ This looks like a promotion's own show ({why}). Clips from it are likely to be taken down.")
            if info["has_video"] and info["height"] < 700:
                log("   ⚠ YouTube only offered a low-resolution copy, so clips from this one will look soft.")
            step("download", "done")

        # 2. transcribe
        jobs.check()
        cur = "transcribe"
        step("transcribe", "active" if data is None or not (wd / "energy.json").exists() else "skip")
        if data is None:
            log(f"✎ Transcribing “{iv['title']}”")
            data = transcribe.transcribe(iv["source_path"], iv["title"], wd, cfg, log, progress,
                                         ranges=sections)
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
        energy = util.read_json(wd / "energy.json", {})
        contact = wd / "contact_sheet.jpg"
        result, cost = analyze.run_api(iv, data, energy, contact if contact.exists() else None,
                                       cfg["model"], log, current_news_text(cfg),
                                       notes=screen.load(wd), learn=cfg.get("learn_from_views", True))
        n = analyze.ingest_result(iv, data, result)
        if n == 0 and data.get("partial"):
            # The first pass pointed at the wrong places. Fall back to the full transcript once.
            log("   no usable clips in the story sections; transcribing the whole interview instead")
            step("transcribe", "active")
            data = transcribe.transcribe(iv["source_path"], iv["title"], wd, cfg, log, progress)
            step("transcribe", "done")
            result, more = analyze.run_api(iv, data, energy, contact if contact.exists() else None,
                                           cfg["model"], log, current_news_text(cfg),
                                           learn=cfg.get("learn_from_views", True))
            cost += more
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
    energy = util.read_json(wd / "energy.json", {})
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
    if data:    # a clip stretched past a story section still gets captions
        try:
            data = transcribe.cover(iv["source_path"], iv["title"], iv["workdir"], cfg, data,
                                    clip["start"], clip["end"], log)
        except jobs.Cancelled:
            raise
        except Exception as e:
            log(f"   couldn't transcribe the extra stretch ({str(e)[:100]}); captions may stop early")
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
    data = proof.ensure(cfg, iv, clip, data, log)
    report = {}
    clip["_report"] = report
    out = render.render_clip(cfg, iv, clip, data, ready_dir(cfg), log)
    if cfg.get("cold_open") and render.teaser_span(clip):
        try:
            render.add_cold_open(cfg, iv, clip, data, out, log, report.get("game_start"))
        except jobs.Cancelled:
            raise
        except Exception as e:
            log(f"      cold open skipped ({str(e)[:100]})")
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
    rules = blocklist.load()     # also hides finds from before a source was blocked
    return [p for p in news.load_picks(cfg) if p["url"] not in used and p["url"] not in gone
            and not blocklist.reason(p.get("channel", ""), p.get("title", ""), rules)]


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
    db.update_interview(iid, title=p["title"], pick_reason=p["why"], topic=p.get("topic", ""),
                        duration=p.get("duration") or 0)
    if p.get("screen"):      # already screened when it was found: no need to do it again
        screen.save(db.get_interview(iid)["workdir"], dict(p["screen"], available=True))
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
