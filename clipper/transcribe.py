"""Faster-Whisper transcription with word timestamps, name hints and auto-fixes.

Output (transcript.json):
{
  "duration": 731.2,
  "segments": [{"start": 0.0, "end": 4.1, "text": "...",
                "words": [{"w": "Hello", "s": 0.0, "e": 0.4}, ...]}]
}
Also writes transcript.txt in the old [M:SS] format so it still works in the Project.
"""
import json
import re
from pathlib import Path

from . import config, util

_TOKEN = re.compile(r"[^\w']+")


def detect_device(pref="auto"):
    if pref in ("cpu", "cuda"):
        return pref
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    return "cpu"


def pick_model(pref, device):
    if pref and pref != "auto":
        return pref
    # turbo is far more accurate on names but slow without a GPU.
    return "large-v3-turbo" if device == "cuda" else "small"


def build_prompt(title, names, limit=700):
    """Whisper's prompt is a spelling hint. Names found in the title go first."""
    t = title.lower()
    first = [n for n in names if any(part.lower() in t for part in n.split() if len(part) > 3)]
    rest = [n for n in names if n not in first]
    out, total = [], 0
    for n in first + rest:
        if total + len(n) + 2 > limit:
            break
        out.append(n)
        total += len(n) + 2
    return "Pro wrestling shoot interview. Names mentioned may include: " + ", ".join(out) + "."


def _norm(w):
    return _TOKEN.sub("", w.lower())


def apply_fixes(words, fixes):
    """Replace mis-heard phrases across word tokens, keeping timestamps.

    words: list of {"w","s","e"}; fixes: list of (wrong, right).
    """
    rules = [([_norm(x) for x in wrong.split()], right) for wrong, right in fixes]
    rules.sort(key=lambda r: -len(r[0]))  # longest match first
    out, i = [], 0
    while i < len(words):
        matched = False
        for pat, right in rules:
            n = len(pat)
            if n and i + n <= len(words) and [_norm(words[i + k]["w"]) for k in range(n)] == pat:
                # carry trailing punctuation from the last replaced word
                tail = re.search(r"[^\w']+$", words[i + n - 1]["w"])
                out.append({"w": right + (tail.group(0) if tail else ""),
                            "s": words[i]["s"], "e": words[i + n - 1]["e"]})
                i += n
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    return out


def transcribe(media_path, title, out_dir, cfg, log, progress=None):
    from faster_whisper import WhisperModel

    out_dir = Path(out_dir)
    device = detect_device(cfg.get("device", "auto"))
    model_name = pick_model(cfg.get("whisper_model", "auto"), device)
    log(f"   Whisper model: {model_name} on {device.upper()} (first run downloads the model)")

    try:
        model = WhisperModel(model_name, device=device,
                             compute_type="float16" if device == "cuda" else "int8")
    except Exception as e:
        if device == "cuda":
            log(f"   GPU load failed ({str(e)[:120]}), falling back to CPU")
            device = "cpu"
            model_name = pick_model(cfg.get("whisper_model", "auto"), device)
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
        else:
            raise

    names = config.load_names() + [a for v in config.load_aliases().values() for a in v if len(a) > 3]
    prompt = build_prompt(title, names)
    fixes = config.load_fixes()
    segments_iter, info = model.transcribe(
        str(media_path), language="en", beam_size=5, word_timestamps=True,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=prompt, condition_on_previous_text=False,
    )
    duration = info.duration or 0
    segments = []
    for seg in segments_iter:
        words = [{"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3)}
                 for w in (seg.words or []) if w.word.strip()]
        words = apply_fixes(words, fixes)
        text = " ".join(w["w"] for w in words) if words else seg.text.strip()
        segments.append({"start": round(seg.start, 3), "end": round(seg.end, 3),
                         "text": text, "words": words})
        if progress and duration:
            progress(min(1.0, seg.end / duration))

    data = {"duration": duration, "model": model_name, "segments": segments}
    (out_dir / "transcript.json").write_text(json.dumps(data), encoding="utf-8")
    write_txt(data, title, out_dir / "transcript.txt")
    return data


def write_txt(data, title, path):
    lines = [f"TRANSCRIPT: {title}", f"Duration: {util.ts(data['duration'])}", "=" * 60, ""]
    for s in data["segments"]:
        lines.append(f"[{util.ts(s['start'])}]  {s['text']}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def load(workdir):
    p = Path(workdir) / "transcript.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def all_words(data):
    return [w for s in data["segments"] for w in s["words"]]


def text_between(data, start, end):
    return " ".join(w["w"] for w in all_words(data) if w["s"] >= start - 0.05 and w["e"] <= end + 0.05)


def snap(data, start, end):
    """Nudge Claude's times onto word boundaries; extend end to the sentence close."""
    words = all_words(data)
    if not words:
        return start, end
    # start: first word beginning at or after (start - 0.75s)
    s_word = next((w for w in words if w["s"] >= start - 0.75), words[0])
    new_start = max(0.0, s_word["s"] - 0.12)
    # end: last word ending at or before end + 0.5, then reach for punctuation within 2s
    candidates = [w for w in words if w["e"] <= end + 0.5 and w["s"] >= new_start]
    e_word = candidates[-1] if candidates else words[-1]
    if not re.search(r"[.!?]$", e_word["w"]):
        ahead = [w for w in words if e_word["e"] < w["e"] <= e_word["e"] + 2.0]
        stop = next((w for w in ahead if re.search(r"[.!?]$", w["w"])), None)
        if stop:
            e_word = stop
    new_end = e_word["e"] + 0.25
    return round(new_start, 2), round(max(new_end, new_start + 1.0), 2)
