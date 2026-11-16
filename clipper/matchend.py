"""Find where a WWE 2K capture's match really ends.

Clips are timed so the 2K finish lands as the clip ends. That only works if the app knows
where the finish is, and a raw capture usually runs on past it into replays and menus.
Rather than making you trim every recording, Claude looks at a sheet of frames from the
end of the capture and points at the winner graphic (or, when the game shows none, the
victory celebration). A second, closer look pins the moment down.

Each capture is checked once and the answer is remembered (captures.json), keyed to the
file's size and date so an edited file is checked again. Without an API key, or if the
check fails, the capture is used to its very end, as before.
"""
import base64
import datetime as dt
from pathlib import Path

import numpy as np

from . import config, jobs, llm, util

LOOK_BACK = 300.0        # search the last five minutes of a capture
SHEET_TILES = 48         # frames per sheet, 6 across
COARSE_GAP = 2.0         # seconds between frames in the wide look (stretches to cover the search)
MAX_COARSE = 96          # at most two sheets, so a five-minute search has a frame every ~3 s
FINE_TILES = 24          # close-up sheet, half a second apart
HOLD_GRAPHIC = 4.0       # seconds of the winner graphic to keep (2 s on screen at 2x)
HOLD_CELEBRATION = 12.0  # seconds of a victory celebration to keep when there's no graphic

FIND_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean", "description": "true if the end of the match is visible in these frames"},
        "kind": {"type": "string", "enum": ["winner_graphic", "celebration", "none"]},
        "first": {"type": "number", "description": "label of the first frame showing it, or -1"},
        "last": {"type": "number", "description": "label of the last frame showing it before anything else starts, or -1"},
        "what_follows": {"type": "string", "description": "what comes after it: replay, menu, another match, nothing"},
    },
    "required": ["found", "kind", "first", "last", "what_follows"],
}

COARSE_PROMPT = """These frames are from the end of a WWE 2K wrestling video game recording. Each frame
is labelled with its time in seconds. Frames run left to right, top to bottom.

Find where the match ends: the winner announcement (a banner or panel naming the winner,
usually across the lower part of the screen while the winner poses). If the game shows no
such graphic, use the winner's victory celebration right after the pin, submission or bell.

Report the labels of the first and last frames that show it. Stop before anything that
comes afterwards: instant replays, highlight reels, menus, loading screens, or a new match.
If the frames only show ordinary match action, or you can't tell, answer found = false."""

FINE_PROMPT = """These frames are a closer look at the end of a WWE 2K match, half a second apart, each
labelled with its time in seconds.

{hint}

Look for the winner announcement first (a banner or panel naming the winner). Only if no
such graphic appears in these frames, use the winner's victory celebration instead.

Report the label of the first frame where it is clearly on screen, and the label of the
last frame where it is still on screen before the picture changes to something else (a
replay, a menu, a different scene). If neither is in these frames, answer found = false."""


def _key(path):
    p = Path(path)
    st = p.stat()
    return f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"


def _cache_path():
    return config.APP_DIR / "captures.json"


def cached(path):
    """A remembered answer for this capture: a dict, or None if it hasn't been checked."""
    try:
        return util.read_json(_cache_path(), {}).get(_key(path))
    except OSError:
        return None


def _remember(path, entry):
    data = util.read_json(_cache_path(), {})
    data[_key(path)] = dict(entry, checked=dt.datetime.now().isoformat(timespec="seconds"))
    # Forget files that no longer exist, so the list doesn't grow forever.
    data = {k: v for k, v in data.items() if Path(k.split("|")[0]).exists()}
    util.write_json(_cache_path(), data, indent=1)


def sheet(path, times, cols, tile_w=320):
    """A grid of frames at `times`, each stamped with its time. Returns JPEG bytes."""
    import cv2
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Couldn't open {Path(path).name}")
    w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 16
    h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 9
    tile_h = int(tile_w * h / w)
    rows = -(-len(times) // cols)
    grid = np.zeros((tile_h * rows, tile_w * cols, 3), dtype=np.uint8)
    for i, t in enumerate(times):
        jobs.check()
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        tile = cv2.resize(frame, (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        label = f"{t:.1f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(tile, (0, 0), (tw + 10, th + 12), (0, 0, 0), -1)
        cv2.putText(tile, label, (5, th + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        r, c = divmod(i, cols)
        grid[r * tile_h:(r + 1) * tile_h, c * tile_w:(c + 1) * tile_w] = tile
    cap.release()
    ok, buf = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise RuntimeError("Couldn't build the frame sheet")
    return buf.tobytes()


def _ask(client, model, jpegs, prompt):
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.standard_b64encode(j).decode()}}
               for j in jpegs]
    content.append({"type": "text", "text": prompt})
    result, responses = llm.structured(client, model=model, schema=FIND_SCHEMA, max_tokens=1500,
                                       messages=[{"role": "user", "content": content}])
    return result, llm.spend("2K finish", model, responses)


def _nearest(times, value):
    return min(times, key=lambda t: abs(t - value))


def coarse_times(duration):
    """Frame times for the wide look: the last LOOK_BACK seconds, evenly spaced."""
    t0 = max(0.0, duration - LOOK_BACK)
    last = max(t0, duration - 0.3)
    n = int(min(MAX_COARSE, max(8, (last - t0) / COARSE_GAP + 1)))
    return [round(t0 + (last - t0) * i / (n - 1), 1) for i in range(n)]


def choose_end(kind, first, last, duration):
    """Where the capture should stop: on the graphic a moment after it has settled (or a
    few seconds into the celebration), and never past whatever follows it."""
    hold = HOLD_GRAPHIC if kind == "winner_graphic" else HOLD_CELEBRATION
    last = min(max(last, first), duration)
    if last - first < 1.0:
        return min(duration, first + 1.0)
    return min(last, first + hold)


def locate(path, cfg, client=None):
    """Look at the capture and return {"found", "kind", "first", "end", "cost"}.
    first: where the winner graphic or celebration starts (source seconds).
    end:   where the capture should stop when it supplies a clip's finish."""
    duration = util.probe(path)["duration"]
    if duration < 8:
        return {"found": False, "kind": "none", "cost": 0.0}
    client = client or llm.client()
    model = cfg["model"]
    coarse = coarse_times(duration)
    step = coarse[1] - coarse[0]
    sheets = [sheet(path, coarse[i:i + SHEET_TILES], 6) for i in range(0, len(coarse), SHEET_TILES)]
    a, cost = _ask(client, model, sheets, COARSE_PROMPT)
    if not a.get("found") or a.get("kind") == "none" or a.get("first", -1) < 0:
        return {"found": False, "kind": "none", "cost": round(cost, 4)}
    kind = a["kind"]
    first = _nearest(coarse, float(a["first"]))
    last = max(first, _nearest(coarse, float(a["last"]) if a.get("last", -1) >= 0 else first))

    # Closer look: half-second frames from just before it starts, running on past it.
    f0 = max(0.0, first - step)
    fine = [round(f0 + 0.5 * i, 1) for i in range(FINE_TILES) if f0 + 0.5 * i < duration - 0.1]
    what = "winner graphic" if kind == "winner_graphic" else "victory celebration"
    hint = (f"A wider look put the {what} at about {first:.0f} seconds; "
            f"afterwards comes: {a.get('what_follows') or 'unknown'}.")
    b, more = _ask(client, model, [sheet(path, fine, 6, tile_w=400)], FINE_PROMPT.format(hint=hint))
    cost += more
    if b.get("found") and b.get("kind") != "none" and b.get("first", -1) >= 0:
        same_kind = b.get("kind") == kind
        kind = b["kind"]
        first = _nearest(fine, float(b["first"]))
        fine_last = _nearest(fine, float(b["last"])) if b.get("last", -1) >= 0 else fine[-1]
        # Still on screen in the final close-up frame: the wider look knows how long it lasts.
        last = max(last, fine_last) if (same_kind and fine_last >= fine[-1] - 0.01) else fine_last
    return {"found": True, "kind": kind, "first": round(first, 2),
            "end": round(choose_end(kind, first, last, duration), 2), "cost": round(cost, 4)}


def finish(path, cfg, log=None):
    """Where this capture's match ends, from memory or a fresh look. Returns a dict with
    "first" and "end" (source seconds), or None when it isn't known: no API key, the
    setting is off, the look failed, or nothing was found."""
    if not cfg.get("find_match_end", True):
        return None
    try:
        hit = cached(path)
    except OSError:
        return None
    if hit is None:
        if cfg.get("analysis_mode") != "api" or not config.get_api_key():
            return None
        if log:
            log(f"      finding the finish in {Path(path).name}…")
        try:
            hit = locate(path, cfg)
        except jobs.Cancelled:
            raise
        except Exception as e:
            if log:
                log(f"      couldn't find the finish ({str(e)[:100]}); using the end of the file")
            return None
        _remember(path, hit)
        if log:
            if hit.get("found"):
                log(f"      finish found at {util.ts(hit['end'])} of {util.ts(util.probe(path)['duration'])}")
            else:
                log("      no winner screen in the last five minutes; using the end of the file")
    return hit if hit.get("found") else None
