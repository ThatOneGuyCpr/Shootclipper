"""First pass on a YouTube interview, before anything is downloaded.

YouTube's own captions arrive in a couple of seconds. A small, cheap Claude model reads
them and marks where the stories are. That does two jobs:

  * Found interviews can show how many stories each one holds, and duds drop out.
  * Whisper (the slow, accurate transcription) only has to run on the story sections
    instead of the whole interview, which is most of the waiting.

Everything here is best-effort. No captions, a blocked request or a failed model call
just means the interview is transcribed in full, the way it always was.
"""
import json
import re
from pathlib import Path

from . import jobs, llm, util

PAD_BEFORE, PAD_AFTER = 30.0, 25.0   # captions are rough, so take generous margins
MERGE_GAP = 40.0                     # sections closer than this become one
MAX_SECTION_SECONDS = 50 * 60        # never hand Whisper more than this from one interview
MIN_STRENGTH = 3

SCREEN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "one or two sentences on what the interview covers"},
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "seconds, where the setup begins"},
                    "end": {"type": "number", "description": "seconds, just after the payoff"},
                    "title": {"type": "string", "description": "what happens, in under 10 words"},
                    "strength": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": ["start", "end", "title", "strength"],
            },
        },
    },
    "required": ["summary", "stories"],
}

SCREEN_PROMPT = """You're screening a pro wrestling interview for a TikTok channel that posts STORIES:
anecdotes told by someone who was there, with a setup and a payoff (a backstage fight, a rib that
went wrong, a firing, a road trip, a run-in with a boss, how a famous moment really happened).
Any era counts.

Below are the video's captions with [seconds] markers. They're auto-generated, so names are
often misspelled and there's no punctuation.

List every stretch where someone tells a real story. For each one give:
- start: the [seconds] marker where the setup begins (include the question that prompts it)
- end: the [seconds] marker just after the payoff lands
- title: what happens, in under 10 words, with names spelled correctly if you can tell
- strength: 5 = a complete, surprising story a non-fan would watch to the end; 4 = a strong
  story with a clear payoff; 3 = a decent story; 2 = thin or mostly opinion; 1 = barely a story

Leave out opinions, rankings, hot takes, news reactions, plugs, intros and small talk unless
they're part of a story. Most stories run 40 seconds to 4 minutes. If the interview has no
real stories, return an empty list. Return at most 20, the strongest ones.

VIDEO: {title}
LENGTH: {length}

CAPTIONS:
{captions}"""


# ── captions ────────────────────────────────────────────────────────────────

def video_id(url):
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([\w-]{6,})", url or "")
    return m.group(1) if m else ""


def parse_json3(text):
    """YouTube's json3 caption format -> [(start seconds, text)]."""
    cues = []
    for ev in json.loads(text).get("events", []):
        words = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
        if words:
            cues.append((ev.get("tStartMs", 0) / 1000.0, words))
    return cues


_VTT_TIME = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->")


def parse_vtt(text):
    """WebVTT -> [(start seconds, text)]. Auto-captions repeat each line as it scrolls;
    repeats are dropped."""
    cues, start, seen = [], None, None
    for line in text.splitlines():
        m = _VTT_TIME.match(line.strip())
        if m:
            h, mi, s, ms = m.groups()
            start = int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0
            continue
        if start is None or not line.strip() or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        clean = re.sub(r"<[^>]+>", "", line).replace("&nbsp;", " ").strip()
        if clean and clean != seen:
            cues.append((start, clean))
            seen = clean
    return cues


def _pick_track(info):
    """The best English caption track: (url, ext). Hand-made captions beat automatic ones."""
    for kind in ("subtitles", "automatic_captions"):
        tracks = info.get(kind) or {}
        langs = [l for l in ("en", "en-orig", "en-US", "en-GB") if l in tracks]
        langs += sorted(l for l in tracks if l.startswith("en") and l not in langs)
        for lang in langs:
            for ext in ("json3", "vtt"):
                for f in tracks[lang]:
                    if f.get("ext") == ext and f.get("url"):
                        return f["url"], ext
    return None, None


def fetch_captions(url):
    """Returns ({"title", "duration", "channel"}, cues) for a YouTube link. cues is None when
    the video has no English captions or they can't be fetched."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        meta = {"title": info.get("title") or "", "duration": float(info.get("duration") or 0),
                "channel": info.get("channel") or info.get("uploader") or ""}
        track, ext = _pick_track(info)
        if not track:
            return meta, None
        raw = ydl.urlopen(track).read().decode("utf-8", "replace")
    cues = parse_json3(raw) if ext == "json3" else parse_vtt(raw)
    return meta, (cues if len(cues) >= 20 else None)


def caption_lines(cues, every=15.0):
    """Captions as '[seconds] text' lines, one line per ~15 seconds, to keep the request small."""
    out, cur, t0 = [], [], None
    for t, text in cues:
        if t0 is None:
            t0 = t
        if cur and t - t0 >= every:
            out.append(f"[{int(t0)}] {' '.join(cur)}")
            cur, t0 = [], t
        cur.append(text)
    if cur:
        out.append(f"[{int(t0)}] {' '.join(cur)}")
    return "\n".join(out)


# ── story finding ───────────────────────────────────────────────────────────

def _clean_stories(stories, duration):
    out = []
    for s in stories or []:
        try:
            a, b = float(s["start"]), float(s["end"])
            strength = max(1, min(5, int(s.get("strength", 3))))
        except (KeyError, TypeError, ValueError):
            continue
        if duration:
            a, b = max(0.0, min(a, duration)), max(0.0, min(b, duration))
        if b - a < 8:
            continue
        out.append({"start": round(a, 1), "end": round(b, 1),
                    "title": str(s.get("title", "")).strip()[:120], "strength": strength})
    out.sort(key=lambda s: s["start"])
    return out


def score(stories):
    """One number for how story-rich an interview is. A 5 counts three times a 3."""
    return sum(max(0, s["strength"] - 2) for s in stories)


def summarize(stories):
    good = [s for s in stories if s["strength"] >= MIN_STRENGTH]
    return {"story_count": len(good), "score": score(stories),
            "story_minutes": round(sum(s["end"] - s["start"] for s in good) / 60, 1),
            "top": [s["title"] for s in sorted(good, key=lambda s: -s["strength"])[:3]]}


def sections(stories, duration, limit=MAX_SECTION_SECONDS):
    """The stretches Whisper should transcribe: each good story with margins, merged where
    they touch, and capped in total (weakest stories dropped first). [] means nothing useful."""
    good = [s for s in stories if s["strength"] >= MIN_STRENGTH]
    if len(good) < 2:
        good = [s for s in stories if s["strength"] >= MIN_STRENGTH - 1]
    keep, used = [], 0.0
    for s in sorted(good, key=lambda s: (-s["strength"], s["start"])):
        length = s["end"] - s["start"] + PAD_BEFORE + PAD_AFTER
        if keep and used + length > limit:
            continue
        keep.append(s)
        used += length
    out = []
    for s in sorted(keep, key=lambda s: s["start"]):
        a = max(0.0, s["start"] - PAD_BEFORE)
        b = s["end"] + PAD_AFTER
        if duration:
            b = min(duration, b)
        if out and a - out[-1][1] <= MERGE_GAP:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([round(a, 1), round(b, 1)])
    return out


def find_stories(cfg, title, cues, duration, client=None):
    """Ask the screening model where the stories are. Returns (stories, summary, cost)."""
    client = client or llm.client()
    prompt = SCREEN_PROMPT.format(title=title or "(untitled)", length=util.ts(duration),
                                  captions=caption_lines(cues))
    model = cfg.get("screen_model") or "claude-haiku-5-5"
    try:
        result, responses = llm.structured(client, model=model, schema=SCREEN_SCHEMA, max_tokens=4000,
                                           messages=[{"role": "user", "content": prompt}])
    except jobs.Cancelled:
        raise
    except Exception as e:
        if model == cfg.get("model") or "isn't available on this key" not in str(e):
            raise
        model = cfg["model"]         # the small model isn't available on this key: use the main one
        result, responses = llm.structured(client, model=model, schema=SCREEN_SCHEMA, max_tokens=4000,
                                           messages=[{"role": "user", "content": prompt}])
    return (_clean_stories(result.get("stories"), duration), str(result.get("summary", "")),
            llm.cost(model, responses))


def screen_url(cfg, url, client=None):
    """Captions + story finding for one link. Always returns a dict:
       {"available": False, "reason": ...}                       captions couldn't be used
       {"available": True, "stories": [...], "summary", "cost",
        "title", "duration", "channel", + summarize() fields}"""
    try:
        meta, cues = fetch_captions(url)
    except jobs.Cancelled:
        raise
    except Exception as e:
        return {"available": False, "reason": f"couldn't read the captions ({str(e)[:120]})"}
    if not cues:
        return dict(meta, available=False, reason="no English captions on this video")
    duration = meta["duration"] or (cues[-1][0] + 5)
    stories, summary, cost = find_stories(cfg, meta["title"], cues, duration, client)
    return dict(meta, available=True, stories=stories, summary=summary, cost=round(cost, 4),
                duration=duration, **summarize(stories))


def load(workdir):
    return util.read_json(Path(workdir) / "screen.json")


def save(workdir, result):
    util.write_json(Path(workdir) / "screen.json", result, indent=1)
