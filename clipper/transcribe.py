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

from . import applog, config, jobs, util

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


# Approximate download sizes, for progress in the log.
MODEL_MB = {"tiny": 75, "base": 145, "small": 485, "medium": 1530, "large-v3-turbo": 1620}
MODEL_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def _model_ok(d):
    mb = d / "model.bin"
    return mb.is_file() and mb.stat().st_size > 10_000_000 and (d / "config.json").is_file()


def _folder_mb(d):
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1e6


def ensure_model(name, log, progress=None):
    """Download a Whisper model into %APPDATA%\\ShootClipper\\models\\<name> as plain files and
    return that folder. Avoids the shared Hugging Face cache, whose half-finished downloads
    faster-whisper would otherwise pick up silently (missing model.bin)."""
    import os
    import shutil
    import threading
    from faster_whisper.utils import _MODELS
    from huggingface_hub import snapshot_download

    if name not in _MODELS:
        raise ValueError(f"Unknown Whisper model '{name}'")
    dest = config.APP_DIR / "models" / name
    if _model_ok(dest):
        return dest
    total = MODEL_MB.get(name, 500)
    log(f"   downloading the Whisper {name} model (about {total} MB, one time only)…")

    last_err = None
    for attempt in range(2):
        if attempt == 1:
            # Second try over plain HTTPS instead of Hugging Face's Xet transfer, which
            # some networks and antivirus tools interfere with.
            os.environ["HF_HUB_DISABLE_XET"] = "1"
            shutil.rmtree(dest, ignore_errors=True)
            log("   retrying the model download another way…")
        dest.mkdir(parents=True, exist_ok=True)
        done = threading.Event()

        def watch():
            while not done.wait(3):
                mb = _folder_mb(dest)
                if progress:
                    progress(min(mb / total, 0.99))
        threading.Thread(target=watch, daemon=True).start()
        try:
            snapshot_download(_MODELS[name], local_dir=str(dest), allow_patterns=MODEL_FILES)
        except Exception as e:
            last_err = e
            applog.write(f"Whisper model download failed (attempt {attempt + 1}): {e!r}")
        finally:
            done.set()
        if _model_ok(dest):
            log("   model downloaded")
            return dest
    raise RuntimeError(f"Couldn't download the Whisper {name} model ({last_err or 'model.bin missing'}). "
                       "Check your internet connection and try again; if it keeps failing, "
                       "an antivirus or firewall may be blocking huggingface.co.")


def load_audio(media_path, start=None, dur=None, sr=16000):
    """Decode audio (all of it, or `dur` seconds from `start`) to 16 kHz mono float32 with
    the bundled ffmpeg. Goes through a temp file so a two-hour interview isn't held in
    memory twice. (faster-whisper's own decoder depends on PyAV versions that keep changing.)"""
    import os
    import tempfile
    import numpy as np
    fd, raw = tempfile.mkstemp(suffix=".f32", prefix="sc_audio_")
    os.close(fd)
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{max(0.0, start):.3f}"]
    cmd += ["-i", str(media_path)]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", raw]
    try:
        p = util.run(cmd, check=False)
        if p.returncode != 0 or os.path.getsize(raw) == 0:
            lines = (p.stderr or "").strip().splitlines()
            raise RuntimeError(f"Couldn't read the audio ({lines[-1] if lines else 'no audio track'})")
        return np.fromfile(raw, dtype=np.float32)
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass


_loaded = {}  # the Whisper model stays loaded between calls (render-time fill-ins reuse it)


def _model(cfg, log, progress=None):
    from faster_whisper import WhisperModel

    device = detect_device(cfg.get("device", "auto"))
    name = pick_model(cfg.get("whisper_model", "auto"), device)
    if _loaded.get("key") == (name, device):
        return _loaded["model"], name
    log(f"   Whisper model: {name} on {device.upper()}")
    try:
        model = WhisperModel(str(ensure_model(name, log, progress)), device=device,
                             compute_type="float16" if device == "cuda" else "int8")
    except jobs.Cancelled:
        raise
    except Exception as e:
        if device != "cuda":
            raise
        log(f"   GPU load failed ({str(e)[:120]}), falling back to CPU")
        device = "cpu"
        name = pick_model(cfg.get("whisper_model", "auto"), device)
        model = WhisperModel(str(ensure_model(name, log, progress)), device="cpu", compute_type="int8")
    _loaded.update(key=(name, device), model=model)
    return model, name


def _whisper(model, media_path, start, length, prompt, fixes, on_progress=None):
    """Transcribe `length` seconds from `start`. Times in the result are in whole-file seconds."""
    audio = load_audio(media_path, start, length)
    segments_iter, _ = model.transcribe(
        audio, language="en", beam_size=5, word_timestamps=True,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=prompt, condition_on_previous_text=False,
    )
    off = start or 0.0
    span = len(audio) / 16000.0
    out = []
    for seg in segments_iter:
        jobs.check()
        words = [{"w": w.word.strip(), "s": round(w.start + off, 3), "e": round(w.end + off, 3)}
                 for w in (seg.words or []) if w.word.strip()]
        words = apply_fixes(words, fixes)
        text = " ".join(w["w"] for w in words) if words else seg.text.strip()
        out.append({"start": round(seg.start + off, 3), "end": round(seg.end + off, 3),
                    "text": text, "words": words})
        if on_progress and span:
            on_progress(min(1.0, seg.end / span))
    return out


def _prompt_and_fixes(title):
    names = config.load_names() + [a for v in config.load_aliases().values() for a in v if len(a) > 3]
    return build_prompt(title, names), config.load_fixes()


def normalize_ranges(ranges, duration):
    """Sort, clip to the file and merge overlapping [start, end] pairs."""
    out = []
    for a, b in sorted((float(a), float(b)) for a, b in ranges or []):
        a, b = max(0.0, a), (min(duration, b) if duration else b)
        if b - a < 0.5:
            continue
        if out and a <= out[-1][1] + 0.01:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([round(a, 2), round(b, 2)])
    return out


def transcribe(media_path, title, out_dir, cfg, log, progress=None, ranges=None):
    """Transcribe the whole file, or only `ranges` ([[start, end], ...] in seconds).
    A partial transcript records what it covers, so missing stretches can be filled in later."""
    out_dir = Path(out_dir)
    duration = util.probe(media_path)["duration"]
    ranges = normalize_ranges(ranges, duration)
    partial = bool(ranges)
    if partial and sum(b - a for a, b in ranges) > 0.85 * duration:
        ranges, partial = [], False                 # nearly everything: just do it all
    todo = ranges if partial else [[0.0, duration]]
    model, model_name = _model(cfg, log, progress)
    prompt, fixes = _prompt_and_fixes(title)
    total = sum(b - a for a, b in todo) or 1.0
    if partial:
        log(f"   transcribing {len(todo)} story sections ({util.ts(total)} of {util.ts(duration)})")
    segments, done = [], 0.0
    for a, b in todo:
        jobs.check()
        base = done

        def on_progress(frac, base=base, length=b - a):
            if progress:
                progress(min(1.0, (base + frac * length) / total))
        segments += _whisper(model, media_path, a if partial else None, (b - a) if partial else None,
                             prompt, fixes, on_progress)
        done += b - a
    data = {"duration": duration, "model": model_name, "segments": segments,
            "partial": partial, "ranges": todo}
    save(out_dir, data, title)
    return data


def save(out_dir, data, title=""):
    out_dir = Path(out_dir)
    util.write_json(out_dir / "transcript.json", data)
    write_txt(data, title or "interview", out_dir / "transcript.txt")


def covered(data):
    """The stretches a transcript covers, as [[start, end], ...]."""
    if data.get("partial"):
        return data.get("ranges") or []
    return [[0.0, float(data.get("duration") or 0) or 1e9]]


def missing(data, start, end):
    """Parts of [start, end] the transcript doesn't cover."""
    gaps, at = [], float(start)
    for a, b in covered(data):
        if b <= at:
            continue
        if a >= end:
            break
        if a > at:
            gaps.append([at, min(a, end)])
        at = max(at, b)
    if at < end:
        gaps.append([at, float(end)])
    return [[a, b] for a, b in gaps if b - a >= 1.0]


def cover(media_path, title, out_dir, cfg, data, start, end, log, margin=2.0):
    """Make sure the transcript covers [start, end] (a clip stretched past a story section
    in Review, say). Transcribes only what's missing and saves. Returns the transcript."""
    if not data or not data.get("partial"):
        return data
    duration = float(data.get("duration") or 0)
    gaps = missing(data, max(0.0, start - margin), min(duration, end + margin) if duration else end + margin)
    if not gaps:
        return data
    log(f"   transcribing {util.ts(sum(b - a for a, b in gaps))} more to cover this clip…")
    model, _ = _model(cfg, log)
    prompt, fixes = _prompt_and_fixes(title)
    new = []
    for a, b in gaps:
        jobs.check()
        new += [s for s in _whisper(model, media_path, a, b - a, prompt, fixes)]
    # Words that straddle an old boundary would be doubled: keep the existing ones.
    old = data["segments"]
    taken = [(s["start"], s["end"]) for s in old]
    for seg in new:
        seg["words"] = [w for w in seg["words"]
                        if not any(a - 0.02 <= w["s"] and w["e"] <= b + 0.02 for a, b in taken)]
        seg["text"] = " ".join(w["w"] for w in seg["words"]) or seg["text"]
    data["segments"] = sorted(old + [s for s in new if s["words"]], key=lambda s: s["start"])
    data["ranges"] = normalize_ranges(covered(data) + gaps, duration)
    if duration and sum(b - a for a, b in data["ranges"]) >= duration - 1.0:
        data["partial"] = False
    save(out_dir, data, title)
    return data


def write_txt(data, title, path):
    lines = [f"TRANSCRIPT: {title}", f"Duration: {util.ts(data['duration'])}", "=" * 60, ""]
    for s in data["segments"]:
        lines.append(f"[{util.ts(s['start'])}]  {s['text']}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def load(workdir):
    data = util.read_json(Path(workdir) / "transcript.json")
    return data if isinstance(data, dict) and "segments" in data else None


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
