"""Sources Shoot Clipper never uses: promotions' own channels, and shows a promotion owns,
produces or is closely tied to. They're the likeliest to have clips taken down, and WWE
in particular files claims quickly.

The list lives in blocked_channels.txt (Settings > Edit Blocked channels):
    WWE                       a channel name
    show: Six Feet Under      any video whose title contains this, from any channel
"""
import re

from . import config

FILE = "blocked_channels.txt"


def norm(text):
    """Lowercase, apostrophes gone, everything else that isn't a letter or digit a space."""
    text = (text or "").lower().replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def parse(text):
    channels, shows = set(), []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("show:"):
            s = norm(line[5:])
            if s:
                shows.append(s)
        else:
            channels.add(norm(line))
    return channels, shows


def load():
    p = config.APP_DIR / FILE
    try:
        return parse(p.read_text(encoding="utf-8"))
    except OSError:
        return set(), []


def reason(channel, title, rules=None):
    """Why a video is blocked (a short phrase), or None if it's fine."""
    channels, shows = rules or load()
    if norm(channel) in channels:
        return f"channel {channel}"
    t = f" {norm(title)} "
    for s in shows:
        if f" {s} " in t:
            return f"show “{s}”"
    return None


def merge_bundled(user_text, bundled_text, version):
    """Add bundled entries the user's file is missing, once per list version, under a
    dated header. Lines the user deleted from an earlier version stay deleted, because
    only entries new to this version are offered."""
    have = {l.strip().lower() for l in user_text.splitlines() if l.strip()}
    new = [l.strip() for l in bundled_text.splitlines()
           if l.strip() and not l.strip().startswith("#") and l.strip().lower() not in have]
    if not new:
        return user_text
    head = f"\n# Added by Shoot Clipper (list version {version}). Delete any line to allow it again.\n"
    return user_text.rstrip("\n") + "\n" + head + "\n".join(new) + "\n"
