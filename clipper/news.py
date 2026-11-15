"""Find out what wrestling fans care about right now, then find interviews that speak to it.

Stage 1 (scout): Claude + web search -> this week's topics, each with YouTube search queries.
Stage 2 (discover): yt-dlp searches YouTube for those queries (no download) -> candidate list.
Stage 3 (rank): Claude picks the candidates most likely to contain timely, clippable moments.
"""
import datetime as dt
import json
import re
from pathlib import Path

from . import config, db, jobs, llm, screen, util

WEB_SEARCH_TOOL = {"type": "web_search_20260318", "name": "web_search", "max_uses": 5,
                   "response_inclusion": "excluded"}

TOPICS_SCHEMA = {
    "type": "object",
    "properties": {
        "as_of": {"type": "string"},
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "short label"},
                    "why_now": {"type": "string", "description": "one sentence, with the date if relevant"},
                    "people": {"type": "array", "items": {"type": "string"}},
                    "urgency": {"type": "integer", "minimum": 1, "maximum": 5,
                                "description": "5 = peaks in the next few days"},
                    "peak_date": {"type": "string", "description": "YYYY-MM-DD the topic peaks, or ''"},
                    "youtube_queries": {"type": "array", "items": {"type": "string"},
                                        "description": "2-3 YouTube searches likely to surface interviews (not match footage) about this"},
                },
                "required": ["topic", "why_now", "people", "urgency", "peak_date", "youtube_queries"],
            },
        },
    },
    "required": ["as_of", "topics"],
}

PICKS_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "topic": {"type": "string"},
                    "why": {"type": "string"},
                    "score": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["url", "topic", "why", "score"],
            },
        }
    },
    "required": ["picks"],
}

SCOUT_PROMPT = """Today is {today}. You're the research desk for a TikTok channel that posts clips from
wrestling interviews: shoot interviews, podcasts and sit-downs (Chris Van Vliet, Inside the Ropes,
Busted Open, Talk Is Jericho, Something to Wrestle, 83 Weeks, RF Video, Kayfabe Commentaries and similar).

Search the web for what wrestling fans are talking about THIS WEEK and in the next two weeks:
upcoming premium live events and their main matches, returns, debuts, retirements, deaths and
anniversaries, injuries, contract news, feuds (on-screen and real), viral moments, big podcast
quotes making headlines. Prefer reputable wrestling news outlets.

Finish with a plain-text list of 6-10 topics, ranked by how much an interview clip about them
would ride current attention. For each give: a short label, why it matters right now (with dates),
the people involved, urgency 1-5 (5 = peaks in the next few days), the peak date if there is one,
and 2-3 YouTube searches that would find long INTERVIEWS where someone tells stories about the
people involved (shoot interviews, podcast sit-downs; old interviews count if they're newly
relevant). Avoid searches that would surface official match footage, news recaps or reactions."""

STRUCTURE_PROMPT = """Today is {today}. Convert these research notes into the topic list, keeping
the order and every topic. Use '' for peak_date when none is given.

NOTES:
{notes}"""

RANK_PROMPT = """Today is {today}. Pick the YouTube videos most worth clipping for a wrestling TikTok
channel this week.

The channel posts STORIES: anecdotes told by someone who was there (backstage fights, ribs,
firings, road stories, run-ins with bosses, how famous moments really happened). Any era; a
current star telling stories is as good as a legend.

What makes a strong pick, in order:
1. Long-form storytelling: a shoot interview, podcast sit-down or career-retrospective
   conversation where the guest tells stories firsthand. Titles and channels that suggest
   "stories", "backstage", "the truth about", "career", "untold" are good signs.
2. A connection to a person or story currently in the news (a boost, not a requirement).
3. Recognizable names, and enough length to hold several stories (20+ minutes ideal).
Weak picks: news recap channels reading headlines, reaction videos, predictions, fantasy
booking, panel debates, match footage, promo clips. Stronger channels: {channels}.
Prefer videos that have NOT already been clipped by the channel.
In "why", say what stories the video likely holds.

CURRENT TOPICS:
{topics}

CANDIDATES:
{candidates}

Return up to 6 picks, best first. Use the exact URLs given."""


def _client():
    return llm.client()


def _dump(block):
    return block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else block


def news_path(cfg):
    return Path(cfg["workspace"]) / "news.json"


def load_news(cfg):
    n = util.read_json(news_path(cfg))
    return n if isinstance(n, dict) and n.get("topics") else None


def news_is_fresh(cfg):
    n = load_news(cfg)
    if not n:
        return False
    try:
        age = dt.datetime.now() - dt.datetime.fromisoformat(n["fetched_at"])
    except (KeyError, ValueError):
        return False
    return age.total_seconds() < float(cfg.get("news_refresh_days", 2)) * 86400


def scout(cfg, log):
    """Stage 1. Web-search the week in wrestling; returns and caches topics.

    Research runs with web search and ends in plain-text notes; a second, small call
    turns the notes into structured topics (structured outputs can't share a request
    with web search citations).
    """
    model = cfg["model"]
    client = _client()
    today = dt.date.today().isoformat()
    messages = [{"role": "user", "content": SCOUT_PROMPT.format(today=today)}]
    total, notes = 0.0, ""
    log("   scouting this week's wrestling news…")
    for _ in range(6):
        resp = _search_turn(client, model=model, messages=messages)
        searches = getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0) or 0
        total += llm.cost(model, [resp], searches)
        notes += "".join(getattr(b, "text", "") for b in resp.content
                         if getattr(b, "type", "") == "text")
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": [_dump(b) for b in resp.content]})
    if not notes.strip():
        raise RuntimeError("News scout came back empty.")
    result, responses = llm.structured(
        client, model=model, schema=TOPICS_SCHEMA, max_tokens=4000,
        messages=[{"role": "user", "content": STRUCTURE_PROMPT.format(today=today, notes=notes)}])
    total += llm.cost(model, responses)
    if not result.get("topics"):
        raise RuntimeError("News scout didn't return topics.")
    result["fetched_at"] = dt.datetime.now().isoformat(timespec="seconds")
    result["cost"] = round(total, 4)
    util.write_json(news_path(cfg), result, indent=2)
    log(f"   {len(result['topics'])} topics found, about ${total:.3f}")
    return result


@llm.friendly
def _search_turn(client, *, model, messages):
    """One web-search turn, streamed so Stop works while Claude is searching."""
    with client.messages.stream(model=model, max_tokens=6000, tools=[WEB_SEARCH_TOOL],
                                messages=messages) as stream:
        for _ in stream:
            jobs.check()
        return stream.get_final_message()


def topics_text(news, limit=10):
    if not news:
        return "(no current-events data)"
    out = []
    for t in sorted(news.get("topics", []), key=lambda t: -t.get("urgency", 0))[:limit]:
        when = f" (peaks {t['peak_date']})" if t.get("peak_date") else ""
        out.append(f"- [{t.get('urgency', 3)}/5] {t['topic']}{when}: {t['why_now']} "
                   f"People: {', '.join(t.get('people', []))}")
    return "\n".join(out)


def _load_list(name):
    p = config.APP_DIR / name
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def youtube_search(query, n=6):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        res = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    out = []
    for e in res.get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        out.append({
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "duration": int(e.get("duration") or 0),
            "views": int(e.get("view_count") or 0),
        })
    return out


def discover(cfg, news, log):
    """Stages 2 and 3. Returns ranked picks and saves them to picks.json."""
    blocked = [b.lower() for b in _load_list("blocked_channels.txt")]
    preferred = _load_list("channels.txt")
    done = {i["url"] for i in db.list_interviews() if i.get("url")} | dismissed()
    cands, seen = [], set()
    topics = sorted(news.get("topics", []), key=lambda t: -t.get("urgency", 0))[:6]
    log(f"   searching YouTube for {len(topics)} topics…")
    for t in topics:
        for q in t.get("youtube_queries", [])[:2]:
            try:
                jobs.check()
                for c in youtube_search(q):
                    if c["url"] in seen or c["url"] in done:
                        continue
                    if c["channel"].lower() in blocked:
                        continue
                    if not (180 <= c["duration"] <= 3 * 3600):
                        continue
                    seen.add(c["url"])
                    c["found_for"] = t["topic"]
                    cands.append(c)
            except jobs.Cancelled:
                raise
            except Exception as e:
                log(f"   search failed for '{q}': {str(e)[:80]}")
    if not cands:
        raise RuntimeError("YouTube search returned nothing usable.")
    log(f"   ranking {len(cands)} candidates…")
    lines = [f"- {c['url']} | {c['title']} | channel: {c['channel']} | "
             f"{c['duration'] // 60} min | {c['views']:,} views | found for: {c['found_for']}"
             for c in cands[:60]]
    model = cfg["model"]
    client = _client()
    result, responses = llm.structured(
        client, model=model, schema=PICKS_SCHEMA, max_tokens=2500,
        messages=[{"role": "user", "content": RANK_PROMPT.format(
            today=dt.date.today().isoformat(), channels=", ".join(preferred[:20]) or "n/a",
            topics=topics_text(news), candidates="\n".join(lines))}])
    picks = result.get("picks", [])
    by_url = {c["url"]: c for c in cands}
    picks = [dict(p, **{k: by_url[p["url"]].get(k, 0) for k in ("title", "channel", "duration", "views")})
             for p in picks if p["url"] in by_url]
    cost = llm.cost(model, responses)
    log(f"   {len(picks)} picks, about ${cost:.3f}")
    if cfg.get("transcribe_mode", "stories") == "stories":
        picks = screen_picks(cfg, picks, log)
    # Keep earlier finds you haven't used or turned down; new ones go first.
    fresh = {p["url"] for p in picks}
    keep = [p for p in load_picks(cfg) if p["url"] not in fresh and p["url"] not in done]
    util.write_json(Path(cfg["workspace"]) / "picks.json", (picks + keep)[:24], indent=2)
    return picks


def screen_picks(cfg, picks, log):
    """Read each pick's captions and count its stories. Picks with no real stories are
    dropped; the rest are ordered by how story-rich they are. Picks whose captions can't
    be read stay in, unscreened."""
    from concurrent.futures import ThreadPoolExecutor
    if not picks:
        return picks
    log(f"   reading captions for {len(picks)} interviews to count their stories…")
    client = _client()

    def one(p):
        try:
            return screen.screen_url(cfg, p["url"], client)
        except jobs.Cancelled:
            raise
        except Exception as e:
            return {"available": False, "reason": str(e)[:160]}

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(one, picks))
    jobs.check()
    out, cost, dropped = [], 0.0, 0
    for p, r in zip(picks, results):
        cost += r.get("cost", 0) or 0
        if not r.get("available"):
            out.append(dict(p, screen=None))
            continue
        if r["story_count"] == 0:
            dropped += 1
            dismiss(p["url"])            # don't search it up and screen it again next time
            log(f"   skipped “{p['title'][:60]}”: no real stories in it")
            continue
        out.append(dict(p, screen={k: r[k] for k in ("stories", "summary", "story_count",
                                                     "story_minutes", "score", "top", "duration")}))
    # Story-rich first. Unscreened picks sit behind anything with a couple of good stories.
    out.sort(key=lambda p: -(p["screen"]["score"] if p.get("screen") else 2.5))
    n = sum(1 for p in out if p.get("screen"))
    log(f"   {n} screened, {dropped} dropped, about ${cost:.3f}")
    return out


def load_picks(cfg):
    p = util.read_json(Path(cfg["workspace"]) / "picks.json", [])
    return p if isinstance(p, list) else []


def _dismissed_path():
    return config.APP_DIR / "dismissed.json"


def dismissed(cfg=None):
    """URLs you turned down with Not this. They never come back as found interviews."""
    urls = util.read_json(_dismissed_path(), [])
    return set(urls) if isinstance(urls, list) else set()


def dismiss(url):
    urls = dismissed()
    urls.add(url)
    util.write_json(_dismissed_path(), sorted(urls), indent=1)


def scout_packet():
    """Paste-mode version of the scout for the Claude Project (turn web search on there)."""
    return (SCOUT_PROMPT.format(today=dt.date.today().isoformat())
            + "\n\nInstead of a plain list, reply with ONE ```json block matching this schema and nothing else:\n"
            + json.dumps(TOPICS_SCHEMA, indent=1))


def save_pasted_topics(cfg, text):
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"):]
    obj, _ = json.JSONDecoder().raw_decode(raw.strip())
    obj["fetched_at"] = dt.datetime.now().isoformat(timespec="seconds")
    util.write_json(news_path(cfg), obj, indent=2)
    return obj
