"""Ask Claude for clip picks. Two routes, same output:
  - API: automatic, as a structured (schema-checked) reply.
  - Paste: builds one text packet for the Claude Project; you paste the JSON reply back.
"""
import base64
import json
import re
from pathlib import Path

from . import db, llm, signals, transcribe, util

EDITORIAL = """You pick TikTok clips from pro wrestling interviews (shoot interviews, podcasts, sit-downs like
Chris Van Vliet or Inside the Ropes) for a wrestling TikTok channel.

The channel is built on STORIES: anecdotes told by someone who was there. What wins, in order:
1. A real story. Something happened, with a setup, escalation and a payoff: a backstage fight, a
   rib that went wrong, a firing, a road trip, a confrontation with a boss, how a famous moment
   really came together. The viewer should be able to retell it afterwards. Era doesn't matter;
   a current star's story counts as much as an old-timer's. Opinions, rankings, hot takes,
   news reactions and promo-style talk are weaker than any decent story; include one only if
   it's exceptional, and rate it lower.
2. Crossover appeal. Stories that land with someone who has never watched wrestling beat inside
   baseball: fights, firings, feuds, insults with a punchline, "wrestling is real" moments,
   funny stories with a clean payoff.
3. Timeliness as a boost. If a story connects to something in CURRENT TOPICS, name it in
   "timely" and let the hook/caption make the connection obvious. Timeliness lifts a good story;
   it doesn't rescue a weak one.
4. Named names. More recognizable wrestlers in the story = more search traffic.
5. Unexpected tone shifts (comedy to serious, calm delivery of something wild).

rating: 5 = a complete, surprising story a non-fan would watch to the end; 4 = a strong story
with a clear payoff; 3 = a decent story or an exceptional opinion; 2 or below, leave it out.

Rules:
- Use the transcript timestamps. start = the first word of the clip, end = the last word. Do not start mid-sentence or cut before a punchline lands.
- Length: most clips should run 60-120 seconds; the channel's best performers tell a whole story. Go up to
  150 seconds if the story holds, and only go under 60 for a single devastating line. Quality over quantity:
  return only clips you'd rate 3+ (roughly 3-8 per hour of footage, fewer if the material is thin),
  and never more than 12 in total: if there are more, keep the 12 best stories.
- hook: on-screen text at the top of the video for the whole clip. Max 8 words, plain and specific, no emoji, no clickbait words like "SHOCKING". Example: "Cornette on the man who ran OVW into the ground".
- context_card: a 2-3 second text card shown before the clip that gives a non-fan the setup. Max 20 words. Example: "2005. Jim Cornette runs WWE's training ground. John Laurinaitis just took over talent relations."
- caption: the TikTok description. Under 150 characters, conversational, can end with a question to drive comments. Never name the production company or distributor (RF Video, Title Match, Kayfabe Commentaries, Inside the Ropes, YouShoot) in hooks, cards or captions.
- hashtags: 8-12, mixing wrestler names, era/promotion tags and niche tags. Always include #wrestling #wrestlingtiktok #shootinterview.
- teaser_start / teaser_end: the 1.5 to 3 seconds inside the clip that would stop someone scrolling if shown first (the wildest line or the reaction to it; not the opening line, and cut on word boundaries). Use 0 for both if nothing stands out.
- framing: "face" for almost everything, including two-person shots: the app crops to whoever is talking and follows the conversation. Use "wide" only when the picture needs everyone at once (a panel of three or more, or something being shown or acted out on screen).
- flags: note anything needing care: deceased subjects (treat with affection, never mockery), Chris Benoit, graphic violence described about a living person, accusations of crimes, slurs, Teddy Hart's legal history. Empty list if none.
- gameplay_wrestlers: the channel stages a WWE 2K match above each clip between the people in the story.
  Name the two who should fight, usually the speaker and the person they're talking about
  (Gotch roasting Enzo -> ["Simon Gotch", "Enzo Amore"]; Ventura on Hogan -> ["Jesse Ventura", "Hulk Hogan"]).
  Managers, bookers and executives count; the owner builds custom characters.
- series: a short tag if the clip belongs to a recurring arc (e.g. "CM Punk files", "Enzo vs Gotch", "Vince McMahon", "WCW collapse", "Montreal Screwjob"), else "".
- Transcripts come from Whisper and mangle names. Use the right spelling in your text fields."""

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "interview": {
            "type": "object",
            "properties": {
                "speakers": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string", "description": "2 sentences on what the interview covers"},
                "copyright_risk": {"type": "string", "enum": ["low", "medium", "high", "unknown"]},
                "watermark": {"type": "string", "description": "where any burned-in logo/text sits, or 'none'"},
                "source_label": {"type": "string", "description": "short second line for the name plate, e.g. '2000 Interview', 'Chris Van Vliet, 2023', 'Shoot Interview'. Never a distributor name like RF Video."},
            },
            "required": ["speakers", "summary", "copyright_risk", "watermark", "source_label"],
        },
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "short internal name"},
                    "speaker": {"type": "string", "description": "full name of who is talking in this clip"},
                    "start": {"type": "number", "description": "seconds"},
                    "end": {"type": "number", "description": "seconds"},
                    "rating": {"type": "integer", "minimum": 1, "maximum": 5,
                               "description": "story strength, per the rating guide"},
                    "crossover": {"type": "boolean"},
                    "hook": {"type": "string"},
                    "context_card": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "framing": {"type": "string", "enum": ["face", "wide"]},
                    "teaser_start": {"type": "number", "description": "start of the cold-open moment in seconds, or 0"},
                    "teaser_end": {"type": "number", "description": "end of the cold-open moment in seconds, or 0"},
                    "why": {"type": "string", "description": "one or two sentences: the story in brief and why it works"},
                    "flags": {"type": "array", "items": {"type": "string"}},
                    "series": {"type": "string"},
                    "timely": {"type": "string", "description": "current topic this connects to, or ''"},
                    "gameplay_wrestlers": {"type": "array", "items": {"type": "string"},
                                           "description": "the two people who should fight in the 2K match shown with this clip"},
                },
                "required": ["title", "speaker", "start", "end", "rating", "crossover", "hook",
                             "context_card", "caption", "hashtags", "framing", "why",
                             "flags", "series", "timely", "gameplay_wrestlers"],
            },
        },
    },
    "required": ["interview", "clips"],
}


def transcript_lines(data):
    """Compact transcript with one-decimal timestamps in seconds (easy for the model to copy).
    A partial transcript gets a marker wherever a stretch was skipped."""
    out, prev_end = [], 0.0
    partial = bool(data.get("partial"))
    for s in data["segments"]:
        if partial and s["start"] - prev_end > 45:
            out.append(f"--- {util.ts(prev_end)} to {util.ts(s['start'])} not transcribed ---")
        out.append(f"[{s['start']:.1f}] {s['text']}")
        prev_end = s["end"]
    return "\n".join(out)


def story_notes(notes):
    """The first pass's story list, as hints for the clip picker."""
    stories = (notes or {}).get("stories") or []
    return "\n".join(f"- [{s['start']:.0f}-{s['end']:.0f}] {s['title']} (strength {s['strength']})"
                     for s in stories if s.get("strength", 0) >= 3)


def performance_notes(rows=None, min_clips=8):
    """What your posted clips' view counts say, for Claude. Empty until there are enough to
    mean something."""
    rows = db.performance() if rows is None else rows
    if len(rows) < min_clips:
        return ""
    views = sorted(r["views"] for r in rows)
    median = views[len(views) // 2]

    def line(r):
        return (f"- {r['views']:,} views: “{r['title']}” ({r['speaker'] or 'unknown'}, "
                f"{r['end'] - r['start']:.0f}s, {'2K split' if r['framing'] == 'split' else r['framing']})")

    def avg(group):
        return round(sum(r["views"] for r in group) / len(group)) if group else 0
    ranked = sorted(rows, key=lambda r: -r["views"])
    n = min(5, len(rows) // 3)
    out = [f"{len(rows)} posted clips with view counts; the median is {median:,} views.",
           "Best performers:"] + [line(r) for r in ranked[:n]] + \
          ["Weakest performers:"] + [line(r) for r in ranked[-n:]]
    buckets = [("under 45s", lambda d: d < 45), ("45 to 75s", lambda d: 45 <= d <= 75), ("over 75s", lambda d: d > 75)]
    lengths = [(name, [r for r in rows if test(r["end"] - r["start"])]) for name, test in buckets]
    lengths = [f"{name}: {avg(g):,} avg ({len(g)})" for name, g in lengths if len(g) >= 2]
    if len(lengths) >= 2:
        out.append("By length: " + "; ".join(lengths))
    speakers = {}
    for r in rows:
        if r["speaker"]:
            speakers.setdefault(r["speaker"], []).append(r)
    by_speaker = sorted(((avg(g), s, len(g)) for s, g in speakers.items() if len(g) >= 2), reverse=True)
    if len(by_speaker) >= 2:
        out.append("By speaker: " + "; ".join(f"{s}: {a:,} avg ({k})" for a, s, k in by_speaker[:8]))
    return "\n".join(out)


def build_context(interview, data, energy, news_text="", notes=None, learn=True):
    lib = db.library_digest()
    perf = performance_notes() if learn else ""
    learned = ["", "HOW THIS CHANNEL'S CLIPS HAVE DONE (lean toward what works when choosing and "
               "rating, without repeating old clips; small numbers, so treat it as a hint):", perf] if perf else []
    first_pass = story_notes(notes) if data.get("partial") else ""
    coverage = []
    if data.get("partial"):
        coverage = [
            "",
            "ONLY PARTS OF THIS INTERVIEW ARE TRANSCRIBED: a first pass over rough captions marked "
            "where the stories are, and just those sections were transcribed accurately. Pick clips "
            "only from transcribed text, and keep start and end at least 2 seconds inside a section.",
            "First-pass story notes ([seconds] from rough captions; check them against the transcript):",
            first_pass or "- (none)",
        ]
    parts = [
        f"INTERVIEW: {interview['title']}",
        f"Why this video was picked: {interview.get('pick_reason') or 'added manually'}",
        "",
        "CURRENT TOPICS (this week in wrestling):",
        news_text or "(none supplied)",
        "",
        f"Source preset: {interview.get('source_preset', 'unknown')}",
        f"Duration: {util.ts(data['duration'])} ({data['duration']:.0f}s)",
        "",
        "AUDIO ENERGY: " + signals.energy_summary(energy),
        *coverage,
        "",
        "CLIPS ALREADY IN THE LIBRARY (for series connections; don't duplicate):",
        "\n".join(f"- {x}" for x in lib) if lib else "- (none yet)",
        *learned,
        "",
        "The attached contact sheet shows frames across the interview with timestamps; "
        "use it to see who is on screen, wide vs close shots, and watermarks.",
        "",
        "TRANSCRIPT ([seconds] text):",
        transcript_lines(data),
    ]
    return "\n".join(parts)


def _image_block(path):
    b64 = base64.standard_b64encode(Path(path).read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}}


def run_api(interview, data, energy, contact_path, model, log, news_text="", notes=None, learn=True):
    client = llm.client()
    content = []
    if contact_path and Path(contact_path).exists():
        content.append(_image_block(contact_path))
    content.append({"type": "text", "text": build_context(interview, data, energy, news_text, notes, learn)})
    log(f"   asking {model} for clip picks…")
    result, responses = llm.structured(
        client, model=model, schema=CLIP_SCHEMA, max_tokens=32000,
        system=[{"type": "text", "text": EDITORIAL, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )
    in_tok = out_tok = 0
    for r in responses:
        i, o = llm.usage_tokens(r.usage)
        in_tok, out_tok = in_tok + i, out_tok + o
    cost = llm.spend("clip picks", model, responses)
    log(f"   {in_tok:,} tokens in, {out_tok:,} out, about ${cost:.3f}")
    return result, cost


def build_packet(interview, data, energy, news_text=""):
    return (
        "SHOOT CLIPPER PACKET. Reply with ONE ```json code block and nothing else, "
        "matching this JSON schema exactly:\n"
        + json.dumps(CLIP_SCHEMA, indent=1)
        + "\n\n=== EDITORIAL BRIEF ===\n" + EDITORIAL
        + "\n\n=== DATA ===\n" + build_context(interview, data, energy, news_text)
        + "\n\n(If a contact sheet image is attached, use it for framing and watermarks.)"
    )


def parse_reply(text):
    """Pull the JSON object out of a pasted Claude reply."""
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"):]
    obj, _ = json.JSONDecoder().raw_decode(raw.strip())
    if "clips" not in obj:
        raise ValueError("No 'clips' list found in the reply.")
    return obj


def ingest_result(interview, data, result):
    """Snap times to word boundaries, store clips, save analysis.json."""
    wd = Path(interview["workdir"])
    util.write_json(wd / "analysis.json", result, indent=2)
    duration = float(data.get("duration") or 0)
    clips = []
    for c in result.get("clips", []):
        try:
            want_s, want_e = float(c["start"]), float(c["end"])
            if want_e - want_s < 3:
                continue
            s, e = transcribe.snap(data, want_s, want_e)
        except Exception:
            continue
        if duration:
            e = min(e, duration)
        # Drop picks that don't hold up: times that make no sense, a start that had to jump
        # a long way to find any words (it pointed into an untranscribed stretch), or a clip
        # with next to nothing said in it.
        if e - s < 5 or e - s > 240 or abs(s - want_s) > 20:
            continue
        if len(transcribe.text_between(data, s, e).split()) < 8:
            continue
        c["start"], c["end"] = s, e
        if c.get("timely"):
            c["why"] = f"[Timely: {c['timely']}] " + c.get("why", "")
        clips.append(c)
    db.clear_suggestions(interview["id"])
    db.add_clips(interview["id"], clips)
    info = result.get("interview", {})
    db.update_interview(interview["id"], source_label=info.get("source_label", ""),
                        speakers=", ".join(info.get("speakers", [])),
                        summary=info.get("summary", ""),
                        copyright_risk=info.get("copyright_risk", ""))
    return len(clips)
