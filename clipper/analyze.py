"""Ask Claude for clip picks. Two routes, same output:
  - API: automatic, structured via a forced tool call.
  - Paste: builds one text packet for the Claude Project; you paste the JSON reply back.
"""
import base64
import json
import re
from pathlib import Path

from . import config, db, llm, signals, transcribe, util

EDITORIAL = """You pick TikTok clips from pro wrestling interviews (shoot interviews, podcasts, sit-downs like
Chris Van Vliet or Inside the Ropes) for a wrestling TikTok channel.

What wins, in order:
0. Timeliness. If a moment connects to something in CURRENT TOPICS, it jumps the queue: name the
   connection in "timely", and let the hook/caption make it obvious why this matters this week.
1. Crossover appeal. Clips that land with someone who has never watched wrestling beat niche clips. Fights, firings, feuds, insults with a punchline, "wrestling is real" moments, funny stories with a clean payoff.
2. Clip architecture over raw quote quality. A story with setup, escalation and a payoff beats a sharper line with no arc. A single devastating line can stand alone if it needs zero context.
3. Named names. More recognizable wrestlers named in the clip = more search traffic.
4. Unexpected tone shifts (comedy to serious, calm delivery of something wild).

Rules:
- Use the transcript timestamps. start = the first word of the clip, end = the last word. Do not start mid-sentence or cut before a punchline lands.
- Length: most clips should run 60-120 seconds; the channel's best performers tell a whole story. Go up to
  150 seconds if the story holds, and only go under 60 for a single devastating line. Quality over quantity:
  return only clips you'd rate 3+ (roughly 3-8 per hour of footage, fewer if the material is thin).
- hook: on-screen text at the top of the video for the whole clip. Max 8 words, plain and specific, no emoji, no clickbait words like "SHOCKING". Example: "Cornette on the man who ran OVW into the ground".
- context_card: a 2-3 second text card shown before the clip that gives a non-fan the setup. Max 20 words. Example: "2005. Jim Cornette runs WWE's training ground. John Laurinaitis just took over talent relations."
- caption: the TikTok description. Under 150 characters, conversational, can end with a question to drive comments. Never name the production company or distributor (RF Video, Title Match, Kayfabe Commentaries, Inside the Ropes, YouShoot) in hooks, cards or captions.
- hashtags: 8-12, mixing wrestler names, era/promotion tags and niche tags. Always include #wrestling #wrestlingtiktok #shootinterview.
- framing: "face" for a single speaker on screen; "wide" if the clip is mostly a two-shot or panel where cropping to one face would lose the other person.
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
                    "rating": {"type": "integer", "minimum": 1, "maximum": 5},
                    "crossover": {"type": "boolean"},
                    "hook": {"type": "string"},
                    "context_card": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "framing": {"type": "string", "enum": ["face", "wide"]},
                    "why": {"type": "string", "description": "one or two sentences on why it works"},
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
    """Compact transcript with one-decimal timestamps in seconds (easy for the model to copy)."""
    out = []
    for s in data["segments"]:
        out.append(f"[{s['start']:.1f}] {s['text']}")
    return "\n".join(out)


def build_context(interview, data, energy, news_text=""):
    lib = db.library_digest()
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
        "",
        "CLIPS ALREADY IN THE LIBRARY (for series connections; don't duplicate):",
        "\n".join(f"- {x}" for x in lib) if lib else "- (none yet)",
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


def run_api(interview, data, energy, contact_path, model, log, news_text=""):
    import anthropic

    key = config.get_api_key()
    if not key:
        raise RuntimeError("No API key saved. Add one in Settings, or switch to Paste mode.")
    client = anthropic.Anthropic(api_key=key)
    content = []
    if contact_path and Path(contact_path).exists():
        content.append(_image_block(contact_path))
    content.append({"type": "text", "text": build_context(interview, data, energy, news_text)})
    log(f"   asking {model} for clip picks…")
    result, responses = llm.structured(
        client, model=model, schema=CLIP_SCHEMA, max_tokens=12000,
        system=[{"type": "text", "text": EDITORIAL, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
    )
    pin, pout = config.MODEL_PRICES.get(model, (2.0, 10.0))
    in_tok = out_tok = 0
    for r in responses:
        i, o = llm.usage_tokens(r.usage)
        in_tok, out_tok = in_tok + i, out_tok + o
    cost = in_tok / 1e6 * pin + out_tok / 1e6 * pout
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
    (wd / "analysis.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    clips = []
    for c in result.get("clips", []):
        try:
            s, e = transcribe.snap(data, float(c["start"]), float(c["end"]))
        except Exception:
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
