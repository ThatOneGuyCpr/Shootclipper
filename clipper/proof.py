"""Proofread a clip's captions before they are burned in.

Whisper mishears wrestling names ("Jim Hurd", "Rick Flare"). The name list and fixes file
catch the regulars; this catches the rest. A small Claude model reads the clip's words
next to your name list and the interview's title, and returns only the words that are
wrong. Corrections are saved into the transcript, so each clip is checked once.

It may only respell words. It can't add, drop or reorder them, so caption timing is
untouched, and anything that would change the word count is ignored.
"""
from . import config, db, jobs, llm, transcribe

SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "the word's number"},
                    "to": {"type": "string", "description": "the corrected single word"},
                },
                "required": ["n", "to"],
            },
        },
    },
    "required": ["fixes"],
}

PROMPT = """These are the captions for a short clip from a pro wrestling interview, transcribed by speech
recognition. Each word has a number. They will be burned into the video, so mistakes show.

Fix only clear transcription errors:
- misheard names of wrestlers, promoters, companies, shows and moves (the list below has the
  usual spellings; use your own knowledge of wrestling for names that aren't on it)
- a word that is plainly a mishearing of another given the sentence

Leave everything else exactly as it is: slang, grammar, swearing, filler words, repeated
words. Don't add punctuation. Each fix replaces ONE numbered word with ONE word (a
two-word name is two fixes). If nothing is wrong, return an empty list.

INTERVIEW: {title}
SPEAKER: {speaker}
NAMES: {names}

CAPTIONS:
{words}"""


def clip_words(data, start, end):
    """The word dicts (shared with the transcript, so edits stick) inside a clip."""
    return [w for s in data["segments"] for w in s["words"]
            if w["s"] >= start - 0.05 and w["e"] <= end + 0.3]


def proofread(cfg, iv, clip, data, log, client=None):
    """Correct the clip's words in place and save the transcript. Returns how many changed."""
    words = clip_words(data, clip["start"], clip["end"])
    if len(words) < 5:
        return 0
    names = config.load_names()
    numbered = " ".join(f"{i}:{w['w']}" for i, w in enumerate(words))
    model = cfg.get("screen_model") or cfg["model"]
    prompt = PROMPT.format(title=iv.get("title", ""), speaker=clip.get("speaker") or "unknown",
                           names=", ".join(names[:300]) or "(none)", words=numbered)
    try:
        result, responses = llm.structured(client or llm.client(), model=model, schema=SCHEMA,
                                           max_tokens=2000, messages=[{"role": "user", "content": prompt}])
    except jobs.Cancelled:
        raise
    except Exception as e:
        if model == cfg["model"] or "isn't available on this key" not in str(e):
            raise
        model = cfg["model"]
        result, responses = llm.structured(client or llm.client(), model=model, schema=SCHEMA,
                                           max_tokens=2000, messages=[{"role": "user", "content": prompt}])
    llm.spend("proofreading", model, responses)
    changed = []
    for fix in result.get("fixes") or []:
        try:
            i, to = int(fix["n"]), str(fix["to"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        # One word in, one word out, and not a wholesale rewrite of the clip.
        if not (0 <= i < len(words)) or not to or " " in to or len(to) > 30 or to == words[i]["w"]:
            continue
        changed.append((words[i]["w"], to))
        words[i]["w"] = to
    if len(changed) > max(6, len(words) // 4):
        raise RuntimeError("the proofreader wanted to change too much; keeping the original captions")
    if changed:
        for seg in data["segments"]:
            seg["text"] = " ".join(w["w"] for w in seg["words"]) or seg["text"]
        transcribe.save(iv["workdir"], data, iv.get("title", ""))
        shown = ", ".join(f"{a} → {b}" for a, b in changed[:6])
        log(f"   caption fixes: {shown}{'…' if len(changed) > 6 else ''}")
    return len(changed)


def ensure(cfg, iv, clip, data, log):
    """Proofread once per clip (again if its edges moved by more than a few seconds)."""
    if not cfg.get("proofread_captions", True) or cfg.get("analysis_mode") != "api" or not data:
        return data
    stamp = int(clip["start"]) * 100000 + int(clip["end"])
    if clip.get("proofed") == stamp:
        return data
    try:
        proofread(cfg, iv, clip, data, log)
        db.update_clip(clip["id"], proofed=stamp)
    except jobs.Cancelled:
        raise
    except Exception as e:
        log(f"   caption proofreading skipped ({str(e)[:100]})")
        return transcribe.load(iv["workdir"]) or data      # drop any half-applied edits
    return data
