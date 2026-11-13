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


def load_audio(media_path, sr=16000):
    """Decode any audio/video file to 16 kHz mono float32 with the bundled ffmpeg.
    (faster-whisper's own decoder depends on PyAV versions that keep changing.)"""
    import numpy as np
    code, out, err = util.run_bytes(["ffmpeg", "-nostdin", "-v", "error", "-i", str(media_path),
                                     "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
    if code != 0 or not out:
        lines = err.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"Couldn't read the audio ({lines[-1] if lines else 'no audio track'})")
    return np.frombuffer(out, np.float32)


def transcribe(media_path, title, out_dir, cfg, log, progress=None):
    from faster_whisper import WhisperModel

    out_dir = Path(out_dir)
    device = detect_device(cfg.get("device", "auto"))
    model_name = pick_model(cfg.get("whisper_model", "auto"), device)
    log(f"   Whisper model: {model_name} on {device.upper()}")

    try:
        model = WhisperModel(str(ensure_model(model_name, log, progress)), device=device,
                             compute_type="float16" if device == "cuda" else "int8")
    except Exception as e:
        if device == "cuda":
            log(f"   GPU load failed ({str(e)[:120]}), falling back to CPU")
            device = "cpu"
            model_name = pick_model(cfg.get("whisper_model", "auto"), device)
            model = WhisperModel(str(ensure_model(model_name, log, progress)), device="cpu",
                                 compute_type="int8")
        else:
            raise

    names = config.load_names() + [a for v in config.load_aliases().values() for a in v if len(a) > 3]
    prompt = build_prompt(title, names)
    fixes = config.load_fixes()
    audio = load_audio(media_path)
    segments_iter, info = model.transcribe(
        audio, language="en", beam_size=5, word_timestamps=True,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
        initial_prompt=prompt, condition_on_previous_text=False,
    )
    duration = info.duration or 0
    segments = []
    for seg in segments_iter:
        jobs.check()
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
