"""Everything the interface can ask for. The web UI calls these through pywebview's
js_api (window.pywebview.api.<name>) and gets back {"data": ...} or {"error": "..."}.

Only names without a leading underscore are exposed to JavaScript.
"""
import datetime as dt
import functools
import hashlib
import json
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from urllib.parse import quote

from . import (applog, cleanup, config, db, ingest, jobs, news, pipeline, render, signals, transcribe,
               updater, util, ytdlp)

ROOT = Path(__file__).resolve().parent.parent

STATUS_LABEL = {
    "queued": "Queued", "downloaded": "Downloaded", "transcribed": "Transcribed",
    "prepped": "Ready to analyze", "needs_paste": "Waiting for paste",
    "analyzed": "Clips suggested", "done": "Done", "error": "Error",
}
STEP_LABELS = {"news": "News", "screen": "Find stories", "download": "Download", "transcribe": "Transcribe",
               "analyze": "Analyze", "render": "Render", "preview": "Preview file"}
CLIP_FIELDS = {"start", "end", "hook", "context_card", "caption", "hashtags", "framing",
               "gameplay", "speaker", "notes"}
VIDEO_TYPES = ("Video files (*.mp4;*.mov;*.mkv;*.avi;*.webm;*.m4v)", "All files (*.*)")


def _yt_thumb(url):
    import re
    m = re.search(r"[?&]v=([\w-]{6,})", url or "")
    return f"https://i.ytimg.com/vi/{m.group(1)}/mqdefault.jpg" if m else ""


def exposed(fn):
    @functools.wraps(fn)
    def wrap(self, *args):
        try:
            return {"data": fn(self, *args)}
        except Exception as e:
            applog.write(f"UI call {fn.__name__} failed: {e}\n{traceback.format_exc(limit=4)}")
            return {"error": str(e) or e.__class__.__name__}
    return wrap


class Api:
    def __init__(self):
        self._cfg = config.load()
        db.init()
        self._window = None
        self._token = ""
        self._lock = threading.RLock()
        self._busy = False
        self._label = ""
        self._steps = []
        self._progress = 0.0
        self._job_error = ""
        self._log_lines = []
        self._rev = 1
        self._last_poll = dt.datetime.now()
        self._log("Shoot Clipper started")
        if not util.have("ffmpeg") or not util.have("ffprobe"):
            self._log("⚠ ffmpeg wasn't found. Install it (winget install Gyan.FFmpeg) and restart.")
        if self._cfg.get("analysis_mode") == "api" and not config.get_api_key():
            self._log("⚠ No API key yet. Add one in Settings, or switch Analysis to Paste.")
        self._update = None
        if updater.FROZEN:
            threading.Thread(target=self._check_update_bg, daemon=True).start()
        threading.Thread(target=self._cleanup_bg, daemon=True).start()
        threading.Thread(target=lambda: ytdlp.update_if_due(self._log), daemon=True).start()

    # ── internals ───────────────────────────────────────────────────────────

    def _log(self, msg):
        msg = str(msg)
        applog.write(msg)
        with self._lock:
            self._log_lines.append(msg)

    def _bump(self):
        with self._lock:
            self._rev += 1

    def _step(self, key, state):
        with self._lock:
            for s in self._steps:
                if s["key"] == key:
                    s["state"] = state

    def _set_progress(self, p):
        self._progress = float(p)

    def _start(self, label, steps, fn):
        with self._lock:
            if self._busy:
                raise RuntimeError(f"{self._label} is still running. Press Stop first, or wait for it.")
            self._busy, self._label, self._job_error, self._progress = True, label, "", 0.0
            self._steps = [{"key": k, "label": STEP_LABELS[k], "state": "pending"} for k in steps]
            jobs.reset()

        def work():
            try:
                fn()
            except jobs.Cancelled:
                self._log("■ Stopped. Anything that finished is kept; Make clips picks up from here.")
                with self._lock:
                    for s in self._steps:
                        if s["state"] == "active":
                            s["state"] = "stopped"
            except Exception as e:
                self._job_error = str(e)
                self._log(f"✗ {e}")
                applog.write(traceback.format_exc(limit=6))
                with self._lock:
                    for s in self._steps:
                        if s["state"] == "active":
                            s["state"] = "error"
            finally:
                # Clear a pressed Stop, or it would kill the next ffmpeg call the interface
                # makes on its own (thumbnails, previews).
                jobs.reset()
                with self._lock:
                    for s in self._steps:
                        if s["state"] == "active":
                            s["state"] = "done"
                    self._busy = False
                    self._rev += 1

        threading.Thread(target=work, daemon=True).start()
        return True

    def _media(self, path):
        if not path:
            return ""
        p = Path(path)
        if not p.exists():
            return ""
        return f"/media?p={quote(str(p))}&k={self._token}&v={int(p.stat().st_mtime)}"

    def _media_allowed(self, path):
        try:
            p = Path(path).resolve()
        except Exception:
            return False
        roots = [self._cfg.get("workspace"), self._cfg.get("gameplay_dir")]
        for r in roots:
            if r:
                try:
                    p.relative_to(Path(r).resolve())
                    return True
                except ValueError:
                    pass
        known = {i["source_path"] for i in db.list_interviews() if i.get("source_path")}
        known |= {c["output_path"] for c in db.list_clips() if c.get("output_path")}
        known |= {c["gameplay"] for c in db.list_clips() if c.get("gameplay")}
        return any(k and Path(k).resolve() == p for k in known)

    def _dialog(self, kind, **kw):
        if not self._window:
            raise RuntimeError("File pickers need the desktop window.")
        import webview
        if hasattr(webview, "FileDialog"):
            t = {"open": webview.FileDialog.OPEN, "folder": webview.FileDialog.FOLDER}[kind]
        else:
            t = {"open": webview.OPEN_DIALOG, "folder": webview.FOLDER_DIALOG}[kind]
        res = self._window.create_file_dialog(t, **kw)
        return list(res) if res else []

    def _thumb(self, clip):
        out = clip.get("output_path") or ""
        if not out or not Path(out).exists():
            return ""
        d = Path(self._cfg["workspace"]) / ".thumbs"
        d.mkdir(parents=True, exist_ok=True)
        key = hashlib.md5(f"{out}{Path(out).stat().st_mtime}".encode()).hexdigest()[:16]
        t = d / f"{key}.jpg"
        if not t.exists():
            try:
                dur = util.probe(out)["duration"]
                util.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{dur * 0.35:.2f}", "-i", out,
                          "-frames:v", "1", "-vf", "scale=360:640", "-q:v", "4", str(t)])
            except Exception:
                return ""
        return self._media(t)

    def _next_slot(self):
        taken = db.taken_slots()
        days = self._cfg.get("post_days", [0, 2, 4])
        hh, mm = map(int, str(self._cfg.get("post_time", "19:30")).split(":"))
        now = dt.datetime.now()
        for i in range(0, 120):
            slot = (now + dt.timedelta(days=i)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot.weekday() in days and slot > now:
                label = slot.strftime("%a %b %d %I:%M %p")
                if label not in taken:
                    return label
        return ""

    # ── status ──────────────────────────────────────────────────────────────

    @exposed
    def poll(self, since=0):
        self._last_poll = dt.datetime.now()
        with self._lock:
            lines = self._log_lines[int(since):]
            return {"busy": self._busy, "label": self._label, "steps": list(self._steps),
                    "progress": self._progress, "error": self._job_error, "rev": self._rev,
                    "log": lines, "next": int(since) + len(lines),
                    "update": self._update["latest"] if self._update else ""}

    @exposed
    def get_state(self):
        cfg = self._cfg
        rows = db.list_interviews()
        clips = db.list_clips()
        by_iv = {}
        for c in clips:
            by_iv[c["interview_id"]] = by_iv.get(c["interview_id"], 0) + 1
        interviews = [{
            "id": i["id"], "title": i["title"], "status": i["status"],
            "status_label": STATUS_LABEL.get(i["status"], i["status"]),
            "clips": by_iv.get(i["id"], 0), "duration": i.get("duration") or 0,
            "topic": i.get("topic") or "", "error": i.get("error") or "", "url": i.get("url") or "",
        } for i in rows if i["status"] != "archived"][:60]
        return {
            "cfg": cfg, "has_key": bool(config.get_api_key()),
            "ffmpeg": util.have("ffmpeg") and util.have("ffprobe"),
            "presets": list(config.load_sources().keys()),
            "models": list(config.MODEL_PRICES.keys()),
            "counts": {s: sum(1 for c in clips if c["status"] == s)
                       for s in ("suggested", "approved", "rendered", "scheduled", "posted")},
            "interviews": interviews,
            "news": news.load_news(cfg),
            "picks": [dict(p, thumb=_yt_thumb(p["url"])) for p in pipeline.fresh_picks(cfg)][:8],
            "log_path": str(applog.path()),
            "version": updater.version(), "installed": updater.FROZEN,
            "data_dir": str(config.APP_DIR),
        }

    # ── home ────────────────────────────────────────────────────────────────

    def _go(self, label, iid=None, ask=False):
        def job():
            lock = util.RunLock(config.APP_DIR / "run.lock")
            if not lock.acquire():
                raise RuntimeError("The daily run is making clips right now. Try again when it finishes.")
            try:
                outs = pipeline.go(self._cfg, self._log, self._set_progress, self._step, iid=iid, ask=ask)
            finally:
                lock.release()
            if outs and self._cfg.get("open_folder_when_done"):
                try:
                    util.open_path(pipeline.ready_dir(self._cfg))
                except Exception:
                    pass
        return self._start(label, ["screen", "download", "transcribe", "analyze", "render"], job)

    @exposed
    def make_clips(self):
        return self._go("Making clips", ask=self._cfg.get("pick_mode", "ask") == "ask")

    @exposed
    def make_interview(self, iid):
        """Make clips from one interview on Home (also how a failed one is retried)."""
        iid = int(iid)
        iv = db.get_interview(iid)
        if not iv:
            raise ValueError("That interview is no longer in the list.")
        if iv["status"] == "error":
            db.update_interview(iid, status="queued" if not iv["source_path"] else "downloaded",
                                error="", attempts=0)
        return self._go("Making clips", iid=iid)

    @exposed
    def make_from_pick(self, url):
        with self._lock:
            if self._busy:
                raise RuntimeError("Something is already running. Stop it first, or queue this one.")
        iid = pipeline.queue_pick(self._cfg, url)
        self._bump()
        return self._go("Making clips", iid=iid)

    @exposed
    def dismiss_pick(self, url):
        news.dismiss(url)
        self._bump()
        return True

    @exposed
    def stop(self):
        with self._lock:
            if not self._busy:
                return False
        jobs.request()
        self._log("Stopping…")
        return True

    @exposed
    def run_interview(self, iid):
        return self._start("Processing interview", ["screen", "download", "transcribe", "analyze"],
                           lambda: pipeline.process_interview(self._cfg, int(iid), self._log,
                                                              self._set_progress, self._step))

    @exposed
    def add_url(self, url, preset):
        url = (url or "").strip()
        if not url.lower().startswith("http"):
            raise ValueError("Paste a full link, starting with https://")
        iid = ingest.add_url(self._cfg, url, preset or "None / unknown")
        self._log(f"Queued {url}")
        self._bump()
        return iid

    @exposed
    def add_files(self, preset):
        files = self._dialog("open", allow_multiple=True, file_types=VIDEO_TYPES)
        for f in files:
            ingest.add_local_file(self._cfg, f, preset or "None / unknown")
            self._log(f"Queued {Path(f).name}")
        self._bump()
        return len(files)

    @exposed
    def queue_pick(self, url):
        iid = pipeline.queue_pick(self._cfg, url)
        self._log(f"Queued {db.get_interview(iid)['title']}")
        self._bump()
        return iid

    @exposed
    def remove_interview(self, iid):
        """Delete an interview: its row, suggestions and folder. Finished clips in Ready to post stay."""
        iid = int(iid)
        iv = db.get_interview(iid)
        if not iv:
            return 0
        with self._lock:
            if self._busy and self._label.startswith("Making"):
                raise RuntimeError("Stop the current job before deleting an interview.")
        freed = cleanup.delete_interview_files(self._cfg, iv)
        finished = [c for c in db.list_clips(iid) if c["status"] in ("rendered", "scheduled", "posted")]
        if finished:
            # Keep finished clips (and their stats): hide the interview instead of deleting it.
            for c in db.list_clips(iid):
                if c not in finished:
                    db.delete_clip(c["id"])
            db.update_interview(iid, status="archived", source_path="")
        else:
            db.delete_interview(iid)
        self._log(f"Deleted “{iv['title']}”" + (f", freed {freed / 1e9:.1f} GB" if freed > 5e7 else ""))
        self._bump()
        return freed

    @exposed
    def storage(self):
        return cleanup.usage(self._cfg)

    @exposed
    def cleanup_now(self):
        def job():
            n, freed = cleanup.run(self._cfg, self._log)
            if not n:
                self._log("Nothing old enough to clean up with the current settings.")
        return self._start("Cleaning up", [], job)

    def _cleanup_bg(self):
        try:
            cleanup.run(self._cfg, self._log)
        except Exception as e:
            applog.write(f"Startup cleanup failed: {e}")

    @exposed
    def open_url(self, url):
        import webbrowser
        if not str(url).startswith("https://"):
            raise ValueError("Not a web link.")
        webbrowser.open(url)
        return True

    @exposed
    def scout_news(self):
        if self._cfg.get("analysis_mode") == "paste":
            raise RuntimeError("News search needs API mode. In Paste mode, use Settings > Copy news request.")

        def job():
            self._step("news", "active")
            news.scout(self._cfg, self._log)
        return self._start("Checking wrestling news", ["news"], job)

    @exposed
    def discover(self):
        if self._cfg.get("analysis_mode") == "paste":
            raise RuntimeError("Finding interviews needs API mode. Switch Analysis to API in Settings.")

        def job():
            self._step("news", "active")
            if not news.news_is_fresh(self._cfg):
                news.scout(self._cfg, self._log)
            found = pipeline.find_candidates(self._cfg, self._log, more=True)
            self._log(f"☞ {len(found)} interviews to choose from under Found interviews.")
        return self._start("Finding interviews", ["news"], job)

    @exposed
    def packet(self, iid):
        text, sheet = pipeline.packet_for(self._cfg, int(iid))
        return {"text": text, "sheet": str(sheet) if sheet.exists() else ""}

    @exposed
    def paste_reply(self, iid, text):
        n = pipeline.accept_pasted(self._cfg, int(iid), text)
        self._log(f"✓ {n} clip ideas loaded")
        self._bump()
        return n

    @exposed
    def news_packet(self):
        return news.scout_packet()

    @exposed
    def paste_news(self, text):
        news.save_pasted_topics(self._cfg, text)
        self._bump()
        return True

    # ── review ──────────────────────────────────────────────────────────────

    @exposed
    def review_list(self):
        out = []
        for i in db.list_interviews():
            if i["status"] not in ("analyzed", "done"):
                continue
            clips = [{
                "id": c["id"], "title": c["title"], "rating": c["rating"], "status": c["status"],
                "start": c["start"], "end": c["end"], "framing": c["framing"],
                "timely": c["why"].startswith("[Timely"),
            } for c in db.list_clips(i["id"])]
            out.append({"id": i["id"], "title": i["title"], "summary": i.get("summary") or "",
                        "speakers": i.get("speakers") or "", "clips": clips})
        return out

    @exposed
    def clip_detail(self, cid):
        c = db.get_clip(int(cid))
        if not c:
            raise ValueError("That clip no longer exists.")
        iv = db.get_interview(c["interview_id"])
        wd = Path(iv["workdir"])
        data = transcribe.load(wd) or {"segments": [], "duration": iv.get("duration") or 0}
        total = data.get("duration") or iv.get("duration") or c["end"]
        length = c["end"] - c["start"]
        pad = min(90.0, max(20.0, 0.6 * length))
        w0, w1 = max(0.0, c["start"] - pad), min(total, c["end"] + pad)
        words = [w for w in transcribe.all_words(data) if w["e"] >= w0 and w["s"] <= w1]
        peaks, rate = [], signals.PEAK_RATE
        try:
            pk = signals.load_peaks(iv["source_path"], wd)
            rate = pk["rate"]
            peaks = pk["peaks"][int(w0 * rate):int(w1 * rate) + 1]
        except Exception:
            pass
        proxy = wd / "preview_proxy.mp4"
        flags = []
        try:
            flags = json.loads(c["flags"] or "[]")
        except Exception:
            pass
        return {
            "clip": c, "flags": flags,
            "interview": {"id": iv["id"], "title": iv["title"], "duration": total,
                          "preset": iv.get("source_preset") or "",
                          "source_label": iv.get("source_label") or ""},
            "source_url": self._media(proxy) if proxy.exists() else self._media(iv["source_path"]),
            "is_proxy": proxy.exists(),
            "output_url": self._media(c.get("output_path")),
            "gameplay_name": Path(c["gameplay"]).name if c.get("gameplay") else "",
            "window": [w0, w1], "words": words, "peaks": peaks, "peak_rate": rate,
            "gaps": transcribe.missing(data, w0, w1) if data.get("partial") else [],
            "speed": self._cfg.get("gameplay_speed", 2.0),
        }

    @exposed
    def extend_transcript(self, cid):
        """Transcribe a minute either side of a clip, for interviews where only the story
        sections were transcribed and you want to move a clip's edges past them."""
        c = db.get_clip(int(cid))
        iv = db.get_interview(c["interview_id"])

        def job():
            self._step("transcribe", "active")
            src = pipeline.ensure_source(iv, self._log)
            data = transcribe.load(src["workdir"])
            transcribe.cover(src["source_path"], src["title"], src["workdir"], self._cfg, data,
                             max(0.0, c["start"] - 60), c["end"] + 60, self._log, margin=0)
        return self._start("Transcribing more", ["transcribe"], job)

    @exposed
    def update_clip(self, cid, fields):
        clean = {k: v for k, v in (fields or {}).items() if k in CLIP_FIELDS}
        for k in ("start", "end"):
            if k in clean:
                clean[k] = round(float(clean[k]), 2)
        if "start" in clean and "end" in clean and clean["end"] - clean["start"] < 1:
            raise ValueError("A clip needs to be at least a second long.")
        db.update_clip(int(cid), **clean)
        return True

    @exposed
    def set_clip_status(self, cid, status):
        if status not in ("suggested", "approved", "rejected"):
            raise ValueError("Unknown status")
        db.update_clip(int(cid), status=status)
        self._bump()
        return True

    @exposed
    def snap(self, cid, start, end):
        c = db.get_clip(int(cid))
        data = transcribe.load(db.get_interview(c["interview_id"])["workdir"])
        s, e = transcribe.snap(data, float(start), float(end))
        return [s, e]

    @exposed
    def auto_match(self, cid):
        if not self._cfg.get("gameplay_dir"):
            raise RuntimeError("Set your 2K footage folder in Settings first.")
        c = db.get_clip(int(cid))
        iv = db.get_interview(c["interview_id"])
        g = pipeline.match_gameplay(self._cfg, c, iv, transcribe.load(iv["workdir"]))
        if not g:
            raise RuntimeError("No capture matched. Name files after the matchup, like “Ventura vs Hogan.mp4”.")
        db.update_clip(int(cid), gameplay=g, framing="split")
        return Path(g).name

    @exposed
    def choose_gameplay(self, cid):
        files = self._dialog("open", allow_multiple=False, file_types=VIDEO_TYPES,
                             directory=self._cfg.get("gameplay_dir") or "")
        if not files:
            return ""
        db.update_clip(int(cid), gameplay=files[0], framing="split")
        return Path(files[0]).name

    @exposed
    def render_clip(self, cid):
        cid = int(cid)
        db.update_clip(cid, status="approved")

        def job():
            self._step("render", "active")
            pipeline.render_clip(self._cfg, cid, self._log)
        return self._start("Rendering clip", ["render"], job)

    @exposed
    def render_approved(self):
        def job():
            self._step("render", "active")
            pipeline.render_approved(self._cfg, self._log)
        return self._start("Rendering approved clips", ["render"], job)

    @exposed
    def make_proxy(self, iid):
        iv = db.get_interview(int(iid))

        def job():
            self._step("preview", "active")
            self._log(f"Making a preview copy of “{iv['title']}” for the player…")
            out = Path(iv["workdir"]) / "preview_proxy.mp4"
            util.run(["ffmpeg", "-y", "-v", "error", "-i", iv["source_path"], "-vf", "scale=-2:480",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-g", "30",
                      "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)])
            self._log("   preview copy ready")
        return self._start("Preparing preview", ["preview"], job)

    # ── library ─────────────────────────────────────────────────────────────

    @exposed
    def library(self, which="to_post"):
        statuses = {"to_post": ["rendered"], "scheduled": ["scheduled"], "posted": ["posted"],
                    "all": ["rendered", "scheduled", "posted"]}.get(which, ["rendered"])
        clips = db.list_clips(statuses=statuses)
        if which == "scheduled":
            clips.sort(key=lambda c: c["scheduled_for"])
        items = [{
            "id": c["id"], "interview_id": c["interview_id"], "title": c["title"],
            "interview": c["interview_title"], "rating": c["rating"], "status": c["status"],
            "length": c["end"] - c["start"], "framing": c["framing"],
            "scheduled_for": c["scheduled_for"], "posted_at": c["posted_at"], "views": c["views"],
            "caption": c["caption"], "thumb": self._thumb(c), "video": self._media(c["output_path"]),
            "missing": not (c["output_path"] and Path(c["output_path"]).exists()),
        } for c in clips]
        every = db.list_clips(statuses=["rendered", "scheduled", "posted"])
        banked = sum(1 for c in every if c["status"] != "posted")
        per_week = max(1, len(self._cfg.get("post_days", [0, 2, 4])))
        posted = [c for c in every if c["status"] == "posted" and c["views"]]
        split = [c["views"] for c in posted if c["framing"] == "split"]
        plain = [c["views"] for c in posted if c["framing"] != "split"]
        return {"items": items, "banked": banked, "per_week": per_week,
                "runway_weeks": round(banked / per_week, 1),
                "ab": {"split": {"n": len(split), "avg": sum(split) // len(split) if split else 0},
                       "plain": {"n": len(plain), "avg": sum(plain) // len(plain) if plain else 0}}}

    @exposed
    def post_text(self, cid):
        return render.post_text(db.get_clip(int(cid)))

    @exposed
    def schedule(self, cid):
        slot = self._next_slot()
        db.update_clip(int(cid), status="scheduled", scheduled_for=slot)
        self._log(f"Slotted for {slot}. Set the same time in TikTok Studio when you upload.")
        return slot

    @exposed
    def mark_posted(self, cid):
        db.update_clip(int(cid), status="posted",
                       posted_at=dt.datetime.now().isoformat(timespec="minutes"))
        return True

    @exposed
    def unpost(self, cid):
        db.update_clip(int(cid), status="rendered", posted_at="", scheduled_for="")
        return True

    @exposed
    def set_views(self, cid, views):
        db.update_clip(int(cid), views=int(str(views).replace(",", "").strip() or 0))
        return True

    @exposed
    def open_clip_file(self, cid):
        c = db.get_clip(int(cid))
        if c.get("output_path") and Path(c["output_path"]).exists():
            util.open_path(c["output_path"])
        return True

    @exposed
    def open_folder(self, which="ready", iid=None):
        if which == "ready":
            util.open_path(pipeline.ready_dir(self._cfg))
        elif which == "data":
            util.open_path(config.APP_DIR)
        elif which == "workspace":
            util.open_path(self._cfg["workspace"])
        elif which == "interview" and iid:
            util.open_path(db.get_interview(int(iid))["workdir"])
        return True

    @exposed
    def copy_text(self, text):
        """Clipboard fallback for when the browser API is blocked."""
        if util.IS_WINDOWS:
            subprocess.run(["clip"], input=str(text).encode("utf-16le"), creationflags=util.NO_WINDOW)
            return True
        raise RuntimeError("Clipboard isn't available here.")

    # ── settings ────────────────────────────────────────────────────────────

    @exposed
    def save_settings(self, values):
        cfg = dict(self._cfg)
        for key, val in (values or {}).items():
            if key not in config.DEFAULTS:
                continue
            ref = config.DEFAULTS[key]
            try:
                if isinstance(ref, bool):
                    val = bool(val)
                elif isinstance(ref, int):
                    val = int(float(val))
                elif isinstance(ref, float):
                    val = float(val)
                elif isinstance(ref, list):
                    val = [int(x) for x in val]
                else:
                    val = str(val).strip()
            except (TypeError, ValueError):
                raise ValueError(f"“{val}” isn't a valid value for {key.replace('_', ' ')}.")
            cfg[key] = val
        if cfg["gameplay_speed"] <= 0:
            raise ValueError("2K speed has to be above zero.")
        config.save(cfg)
        self._cfg = config.load()
        self._bump()
        return self._cfg

    @exposed
    def set_theme(self, theme):
        if theme not in ("system", "light", "dark"):
            raise ValueError("Unknown theme")
        cfg = dict(self._cfg, theme=theme)
        config.save(cfg)
        self._cfg = cfg
        return theme

    @exposed
    def save_key(self, key):
        if not (key or "").strip():
            raise ValueError("Paste the key first.")
        msg = config.set_api_key(key)
        self._log(f"✓ API key saved. {msg}.")
        return msg

    def _check_update_bg(self):
        import time
        time.sleep(4)
        try:
            info = updater.check()
        except Exception as e:
            applog.write(f"Update check failed: {e}")
            return
        if info["available"]:
            self._update = info
            self._log(f"Update available: version {info['latest']}. Press Update in the bottom bar.")
            self._bump()

    @exposed
    def check_update(self):
        if not updater.FROZEN:
            raise RuntimeError("This copy runs from source. Updates come as a zip: use Install update.")
        info = updater.check()
        self._update = info if info["available"] else None
        return info

    @exposed
    def install_update(self):
        if not updater.FROZEN:
            return self._install_zip()
        info = self._update or updater.check()
        if not info["available"]:
            return f"You're on the newest version ({info['current']})."

        def job():
            self._log(f"Downloading version {info['latest']} ({info['size'] / 1e6:.0f} MB)…")
            setup = updater.download(info["url"], progress=self._set_progress)
            self._log("Installing. Shoot Clipper will close and reopen in a minute.")
            updater.run_installer(setup)
            if self._window:
                self._window.destroy()
            else:
                import os
                os._exit(0)
        self._start("Updating", [], job)
        return f"Downloading version {info['latest']}…"

    def _install_zip(self):
        if self._busy:
            raise RuntimeError("Wait for the current job to finish, then install the update.")
        picked = self._dialog("open", file_types=("Shoot Clipper update (*.zip)",))
        if not picked:
            return ""
        old, new = updater.apply_zip(picked[0])
        self._log(f"✓ Updated Shoot Clipper from {old} to {new}. Restarting…")
        threading.Thread(target=self._restart, daemon=True).start()
        return f"Updated to {new}. Restarting…"

    def _restart(self):
        import time
        time.sleep(1.5)
        launcher = updater.APP_DIR / "Launch Shoot Clipper.bat"
        if util.IS_WINDOWS and launcher.exists():
            # A fresh console runs the launcher. --wait gives this process time to exit
            # first, so pip can replace packages this app has loaded.
            subprocess.Popen([str(launcher), "--wait"], cwd=str(updater.APP_DIR),
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        if self._window:
            self._window.destroy()

    @exposed
    def choose_folder(self, key):
        if key not in ("workspace", "gameplay_dir"):
            raise ValueError("Unknown folder setting")
        res = self._dialog("folder")
        return res[0] if res else ""

    @exposed
    def edit_data_file(self, name):
        allowed = {"names": config.NAMES_PATH, "fixes": config.FIXES_PATH,
                   "sources": config.SOURCES_PATH, "channels": config.APP_DIR / "channels.txt",
                   "blocked": config.APP_DIR / "blocked_channels.txt", "log": applog.path()}
        if name not in allowed:
            raise ValueError("Unknown file")
        util.open_path(allowed[name])
        return True

    @exposed
    def autopilot(self, enable, time_text="16:00"):
        if not util.IS_WINDOWS:
            raise RuntimeError("Daily runs use Windows Task Scheduler.")
        if not enable:
            subprocess.run(["schtasks", "/Delete", "/TN", "ShootClipper Autopilot", "/F"],
                           capture_output=True, creationflags=util.NO_WINDOW)
            return "Daily run turned off."
        t = dt.datetime.strptime(time_text.strip(), "%H:%M").strftime("%H:%M")
        exe = Path(sys.executable)
        if updater.FROZEN:
            tr = f'"{exe}" --autopilot'
        else:
            pyw = exe.with_name("pythonw.exe")
            tr = f'"{pyw if pyw.exists() else exe}" "{ROOT / "autopilot.py"}"'
        p = subprocess.run(["schtasks", "/Create", "/SC", "DAILY", "/TN", "ShootClipper Autopilot",
                            "/TR", tr, "/ST", t, "/F"], capture_output=True, text=True,
                           creationflags=util.NO_WINDOW)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "Task Scheduler refused the task.").strip())
        return f"Make clips will run every day at {t}."
