"""Match your own WWE 2K captures to a clip by wrestler name.

Works with either layout in the 2K folder:
  2K\\Ventura vs Hogan.mp4          (names in the filename)
  2K\\Jim Cornette\\vs HBK.mp4       (subfolder per wrestler)
Nicknames from names.txt count ("Shawn Michaels = HBK"), as do distinctive last names.
A capture that has BOTH people in the story beats one that has only one of them.
"""
import hashlib
import re
from pathlib import Path

from . import config

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def _norm(s):
    return " " + re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() + " "


def library(folder):
    if not folder:
        return []
    root = Path(folder)
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.suffix.lower() in VIDEO_EXT]


def keys_for(name, aliases):
    """Strings that identify this person in a filename."""
    keys = [name] + aliases.get(name, [])
    last = name.split()[-1] if name.split() else ""
    if len(last) >= 5:
        keys.append(last)
    return [_norm(k) for k in keys if k]


def wanted_names(clip, interview, clip_text):
    """Who should appear: Claude's pick first, then names said in the clip, then the speaker."""
    out = []
    for n in (clip.get("gameplay_hint") or "").split(","):
        if n.strip():
            out.append(n.strip())
    low = _norm(clip_text)
    aliases = config.load_aliases()
    names = [n for n in config.load_names() if len(n.split()) >= 2]
    firsts = {}
    for n in names:
        firsts.setdefault(n.split()[0].lower(), []).append(n)
    for n in names:
        parts = n.split()
        said = (_norm(n) in low
                or any(_norm(a) in low for a in aliases.get(n, []) if len(a) >= 3)
                or (len(parts[-1]) >= 5 and _norm(parts[-1]) in low)
                or (len(parts[0]) >= 4 and len(firsts[parts[0].lower()]) == 1 and _norm(parts[0]) in low))
        if said and n not in out:
            out.append(n)
    for n in [clip.get("speaker", "")] + (interview.get("speakers") or "").split(","):
        if n and n.strip() and n.strip() not in out:
            out.append(n.strip())
    return out


def pick(folder, clip, interview, clip_text):
    """Return the best matching capture path, or '' if nothing fits."""
    files = library(folder)
    if not files:
        return ""
    root = Path(folder)
    aliases = config.load_aliases()
    wanted = wanted_names(clip, interview, clip_text)[:4]
    if not wanted:
        return ""
    scored = []
    for f in files:
        hay = _norm(str(f.relative_to(root)))
        score = 0
        for rank, name in enumerate(wanted):
            if any(k in hay for k in keys_for(name, aliases)):
                score += 10 - rank          # earlier (more relevant) names weigh more
        if score:
            scored.append((score, f))
    if not scored:
        return ""
    best = max(s for s, _ in scored)
    top = sorted(f for s, f in scored if s == best)
    # Rotate between equally good captures so a repeat matchup doesn't reuse one file.
    h = int(hashlib.md5(str(clip.get("id", 0)).encode()).hexdigest(), 16)
    return str(top[h % len(top)])


def siblings(folder, primary, clip, interview, clip_text):
    """Other captures of the same matchup as `primary` (same set of people in the name).
    Used to fill time before the finish when one capture is too short at the chosen speed."""
    files = library(folder)
    if not files or not primary:
        return []
    root = Path(folder)
    aliases = config.load_aliases()
    wanted = wanted_names(clip, interview, clip_text)[:4]

    def signature(f):
        try:
            hay = _norm(str(Path(f).relative_to(root)))
        except ValueError:
            hay = _norm(Path(f).name)
        return frozenset(n for n in wanted if any(k in hay for k in keys_for(n, aliases)))

    sig = signature(primary)
    if not sig:
        return []
    here = Path(primary).resolve()
    return [str(f) for f in sorted(files) if f.resolve() != here and signature(f) == sig]
