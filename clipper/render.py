"""Turn an approved clip into an upload-ready 1080x1920 MP4.

Pipeline (one ffmpeg pass per clip):
  optional context card (blurred still + text)  ->  clip with a 9:16 crop that follows the
  speaker (see tracking.py)
  (or blurred-background "wide" layout), watermark bar, hook text, word-highlight captions,
  loudness-normalized audio.
"""
import json
import re
import shutil
import tempfile
from pathlib import Path


from . import config, matchend, tracking, transcribe, util

OUT_W, OUT_H, FPS = 1080, 1920, 30
# TikTok's UI covers the bottom ~20% and a column of icons on the right,
# so text stays in the middle band with a wider right margin.
CAPTION_MARGIN_V = 600
SPLIT_GAME_H = 912     # split layout: your 2K match on top, interview underneath (matches your edits)
HOOK_MARGIN_V = 250
SIDE_L, SIDE_R = 90, 170

SWEARS = ("fuck", "shit", "bitch", "cunt", "pussy", "dick", "cock", "whore", "slut")


# ── subtitles ───────────────────────────────────────────────────────────────

FONT_DIR = config.BUNDLED_DATA / "fonts"


def _clean(text):
    return text.replace("{", "(").replace("}", ")").replace("\\", "")


def _mask(word):
    low = word.lower()
    if any(s in low for s in SWEARS):
        letters = [i for i, ch in enumerate(word) if ch.isalpha()]
        if len(letters) > 2:
            chars = list(word)
            for i in letters[1:-1]:
                chars[i] = "*"
            return "".join(chars)
    return word


def _ass_time(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_header(styles):
    return (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {OUT_W}\nPlayResY: {OUT_H}\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "".join(styles)
        + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _style(name, font, size, primary, outline_col, back_col, border_style, outline, shadow,
           align, ml, mr, mv, bold=-1, italic=0, spacing=0):
    return (f"Style: {name},{font},{size},{primary},&H000000FF,{outline_col},{back_col},"
            f"{bold},{italic},0,0,100,100,{spacing},0,{border_style},{outline},{shadow},"
            f"{align},{ml},{mr},{mv},1\n")


def caption_chunks(words, max_words=3, max_chars=18):
    """Group words into short bursts, breaking on pauses and punctuation."""
    chunks, cur = [], []
    for w in words:
        if cur:
            gap = w["s"] - cur[-1]["e"]
            chars = sum(len(x["w"]) for x in cur) + len(w["w"])
            if (len(cur) >= max_words or chars > max_chars or gap > 0.6
                    or re.search(r"[.,!?;:]$", cur[-1]["w"])):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def caption_size(cfg):
    """ASS font size. With Poppins Black, capital letters come out about 0.41 x this tall, so
    116 gives the ~50px capitals measured in the channel's CapCut exports."""
    if cfg.get("caption_style", "seam") == "single":
        return int(cfg.get("caption_size", 116) * 1.75)
    return int(cfg.get("caption_size", 116))


def build_subs(cfg, clip, words, path, layout):
    """layout: dict with caption_margin, hook_margin, bar_px, handle_base, plate_y."""
    font = cfg.get("font", "Poppins Black")
    italic = -1 if cfg.get("font_italic", True) else 0
    white = cfg.get("caption_color", "&H00FFFFFF")
    hi = cfg.get("highlight_color", "&H0000F2FF")
    single = cfg.get("caption_style", "seam") == "single"
    size = caption_size(cfg)
    styles = [
        _style("Cap", font, size, white, "&H00000000", "&H96000000", 1, 8 if single else 5, 3, 2,
               SIDE_L, SIDE_R, layout["caption_margin"], italic=italic, spacing=1),
        _style("Hook", font, 54, "&H00FFFFFF", "&H00101010", "&H00101010", 3, 14, 0, 8,
               SIDE_L, SIDE_R, layout["hook_margin"], italic=italic),
        _style("Handle", "Arial", 40, "&H00D0D0D0", "&H00000000", "&H00000000", 1, 0, 0, 2,
               20, 20, layout["handle_base"] + max(10, layout["bar_px"] // 2 - 22)),
        _style("Plate", "Arial", 56, "&H00111111", "&H00FFFFFF", "&H00000000", 3, 18, 0, 1,
               0, 0, 0),
    ]
    dur = clip["end"] - clip["start"]
    lines = [_ass_header(styles)]

    hook = _clean(clip.get("hook", "")).strip()
    if hook and cfg.get("hook_text", False):
        lines.append(f"Dialogue: 2,{_ass_time(0)},{_ass_time(dur)},Hook,,0,0,0,,{hook}\n")
    handle = cfg.get("handle", "").strip()
    if handle and layout["bar_px"]:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(dur)},Handle,,0,0,0,,{_clean(handle)}\n")

    # Name plate: slides in bottom-left, like a broadcast lower third.
    name = _clean(clip.get("speaker") or "").strip()
    if name and cfg.get("name_plate", True) and dur > 7:
        label = _clean(clip.get("source_label") or "").strip()
        y = layout["plate_y"]
        body = name + (f"\\N{{\\fs34\\b0\\c&H555555&}}{label}" if label else "")
        lines.append(f"Dialogue: 3,{_ass_time(0.8)},{_ass_time(6.0)},Plate,,0,0,0,,"
                     f"{{\\an1\\move(-700,{y},46,{y},0,260)\\fad(0,250)}}{body}\n")

    rel = [{"w": w["w"], "s": w["s"] - clip["start"], "e": w["e"] - clip["start"]} for w in words]
    mask = cfg.get("mask_swears", True)
    chunks = caption_chunks(rel, max_words=1 if single else 3, max_chars=16)
    for ci, chunk in enumerate(chunks):
        next_start = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else dur
        shown = [_clean((_mask(w["w"]) if mask else w["w"]).upper()) for w in chunk]
        for wi, w in enumerate(chunk):
            s = w["s"] if wi else max(0.0, w["s"] - 0.05)
            e = chunk[wi + 1]["s"] if wi + 1 < len(chunk) else min(next_start, w["e"] + 0.35)
            if e <= s:
                e = s + 0.08
            if single:
                text = shown[wi]
            else:
                text = " ".join("{\\c" + hi + "&}" + t + "{\\c" + white + "&}" if k == wi else t
                                for k, t in enumerate(shown))
            pop = "{\\fscx90\\fscy90\\t(0,80,\\fscx100\\fscy100)}" if (wi == 0 or single) else ""
            lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{pop}{text}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def build_card_subs(cfg, text, dur, path):
    font = cfg.get("font", "Poppins Black")
    styles = [_style("Card", font, 70, "&H00FFFFFF", "&H00000000", "&H96000000", 1, 0, 4, 5,
                     110, 110, 0, italic=-1 if cfg.get("font_italic", True) else 0)]
    body = _clean(text).strip()
    lines = [_ass_header(styles),
             f"Dialogue: 0,{_ass_time(0)},{_ass_time(dur)},Card,,0,0,0,,{{\\fad(250,150)}}{body}\n"]
    Path(path).write_text("".join(lines), encoding="utf-8")


def card_duration(text):
    return round(max(2.2, min(4.5, 1.0 + 0.28 * len(text.split()))), 2)


# ── main render ─────────────────────────────────────────────────────────────

def output_name(clip, interview_title):
    return f"{clip['id']:04d}_{util.slug(interview_title, 30)}_{util.slug(clip['title'], 40)}.mp4"


def gameplay_plan(files, dur, speed, max_segments=16, ends=None):
    """Decide which stretch of which 2K capture plays, in order, at a fixed speed.

    The primary capture (files[0]) always ends exactly as the clip ends, so the finish
    and Winner screen land on the last frame. Returns [(path, start, length, loop)] in
    source seconds. If the match is longer than needed it starts partway in; if shorter,
    other captures of the same matchup (files[1:]) play first; with none, it repeats.

    ends: {path: {"first": t, "end": t}} for captures whose finish has been found
    (matchend.finish). The primary stops at its "end" instead of the end of the file, and
    a capture used as filler stops at "first", before its own winner screen.
    """
    ends = ends or {}
    need = dur * speed
    primary = files[0]
    full = util.probe(primary)["duration"]
    if full <= 0.5:
        raise RuntimeError(f"2K capture is unreadable or empty: {primary}")
    known = primary in ends
    g = min(full, float(ends[primary]["end"])) if known else full
    if g >= need:
        return [(primary, g - need, need, False)]
    others = [f for f in files[1:] if Path(f).exists()]
    if not others and not known:
        s0 = (g - (need % g)) % g
        return [(primary, s0, need, True)]
    segs, remaining = [(primary, 0.0, g, False)], need - g
    cycle = list(reversed(others)) + [primary]      # walking backwards from the finish
    durs = {}
    for f in set(cycle):
        d = full if f == primary else util.probe(f)["duration"]
        durs[f] = min(d, float(ends[f]["first"])) if f in ends else d
    i = 0
    while remaining > 0.05 and len(segs) < max_segments:
        f = cycle[i % len(cycle)]
        i += 1
        d = durs[f]
        if d <= 0.5:
            if i > 4 * len(cycle):
                break
            continue
        take = min(d, remaining)
        segs.insert(0, (f, d - take, take, False))
        remaining -= take
    return segs


def _gameplay_inputs(plan, first_index, speed, w, h):
    """ffmpeg inputs and the filter text that turns the plan into one [game] stream. Always muted."""
    inputs, parts, labels = [], [], []
    for k, (path, ss, t, loop) in enumerate(plan):
        if loop:
            inputs += ["-stream_loop", "-1"]
        inputs += ["-ss", f"{ss:.3f}", "-t", f"{t:.3f}", "-an", "-i", str(path)]
        label = "game" if len(plan) == 1 else f"g{k}"
        parts.append(f"[{first_index + k}:v]setpts=(PTS-STARTPTS)/{speed:g},fps={FPS},"
                     f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[{label}]")
        labels.append(f"[{label}]")
    if len(plan) > 1:
        parts.append("".join(labels) + f"concat=n={len(plan)}:v=1:a=0[game]")
    return inputs, ";".join(parts)


def render_clip(cfg, interview, clip, transcript, out_dir, log):
    src = interview["source_path"]
    info = util.probe(src)
    if not info["has_video"]:
        raise RuntimeError("Source has no video stream.")
    start, end = float(clip["start"]), float(clip["end"])
    dur = round(end - start, 3)
    sw, sh = info["width"], info["height"]
    preset = config.load_sources().get(interview.get("source_preset") or "",
                                       {"bar": "none", "height": 0})
    clip = dict(clip)
    clip.setdefault("source_label", interview.get("source_label", ""))
    if not clip.get("source_label"):
        clip["source_label"] = interview.get("source_label", "")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / output_name(clip, interview["title"])
    tmp = Path(tempfile.mkdtemp(prefix="clip_"))
    try:
        # Fonts ride along in the temp folder so libass finds them on any PC.
        if FONT_DIR.exists():
            shutil.copytree(FONT_DIR, tmp / "fonts")
        ass_opt = "ass=subs.ass:fontsdir=fonts" if FONT_DIR.exists() else "ass=subs.ass"
        words = [w for w in transcribe.all_words(transcript)
                 if w["s"] >= start - 0.05 and w["e"] <= end + 0.3]

        framing = clip.get("framing") or "face"
        game = clip.get("gameplay") or ""
        if framing == "split" and not (game and Path(game).exists()):
            log("      no 2K footage matched, using face framing")
            framing = "face"
        single = cfg.get("caption_style", "seam") == "single"
        layout = {"caption_margin": CAPTION_MARGIN_V, "hook_margin": HOOK_MARGIN_V,
                  "bar_px": 0, "handle_base": 0, "plate_y": OUT_H - 330}
        bar_filter = ""

        if framing == "split":
            game_h, int_h = SPLIT_GAME_H, OUT_H - SPLIT_GAME_H
            zoom = max(1.0, float(cfg.get("interview_zoom", 1.35)))
            aspect = OUT_W / int_h
            crop_h = int(sh / zoom) // 2 * 2
            crop_w = int(crop_h * aspect) // 2 * 2
            if crop_w > sw:
                crop_w = sw // 2 * 2
                crop_h = int(crop_w / aspect) // 2 * 2
            log("      tracking faces…")
            stats = {}
            track = tracking.face_track(src, start, dur, sw, sh, crop_w, log, stats,
                                        speakers=cfg.get("speaker_tracking", True))
            cy = stats.get("cy", sh * 0.42)
            y = cy - crop_h * 0.42                      # eyes a little above centre
            band = float(preset.get("height", 0)) if preset.get("bar") == "bottom" else 0
            if band:                                    # keep burned-in lower-third watermarks out
                y = min(y, sh * (1 - band) - crop_h)
            y = int(min(max(y, 0), sh - crop_h)) // 2 * 2

            def y_for(face_cy):
                v = face_cy - crop_h * 0.42
                if band:
                    v = min(v, sh * (1 - band) - crop_h)
                return int(min(max(v, 0), sh - crop_h)) // 2 * 2
            # People sit at different heights, and so do different camera shots: move the crop
            # up or down at the same moments it cuts sideways.
            moves = {}
            for t, x in track:
                moves.setdefault(round(t, 3), []).append(f"crop@track x {x}")
            heights = stats.get("heights") or []
            if len({y_for(c) for _, c in heights}) > 1:
                y = y_for(heights[0][1])
                for t, c in heights:
                    moves.setdefault(round(t, 3), []).append(f"crop@track y {y_for(c)}")
            cmds = "\n".join(f"{t:.3f} {', '.join(moves[t])};" for t in sorted(moves))
            (tmp / "cmds.txt").write_text(cmds + "\n", encoding="utf-8")
            frame_chain = (
                f"sendcmd=f=cmds.txt,crop@track={crop_w}:{crop_h}:{track[0][1]}:{y},"
                f"scale={OUT_W}:{int_h}:flags=lanczos,setsar=1[int];"
                f"{{gamechain}};[game][int]vstack=2,setsar=1")
            card_chain = (f"crop={crop_w}:{crop_h}:{track[0][1]}:{y},"
                          f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                          f"crop={OUT_W}:{OUT_H},setsar=1")
            # Measured from the channel's own edits: caption baseline sits 40px below the seam.
            # libass leaves ~0.36 x size of descender space under the baseline.
            layout["caption_margin"] = (400 if single else
                                        int_h - 40 - int(0.36 * caption_size(cfg)))
            layout["hook_margin"] = 230
            layout["plate_y"] = OUT_H - 190
        else:
            crop_w = min(sw, int(round(sh * 9 / 16)) // 2 * 2)
            if framing == "face" and crop_w < sw:
                log("      tracking faces…")
                track = tracking.face_track(src, start, dur, sw, sh, crop_w, log,
                                            speakers=cfg.get("speaker_tracking", True))
                cmds = "\n".join(f"{t:.3f} crop@track x {x};" for t, x in track)
                (tmp / "cmds.txt").write_text(cmds + "\n", encoding="utf-8")
                x0 = track[0][1]
                frame_chain = (f"sendcmd=f=cmds.txt,crop@track={crop_w}:{sh}:{x0}:0,"
                               f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1")
                card_chain = f"crop={crop_w}:{sh}:{x0}:0,scale={OUT_W}:{OUT_H},setsar=1"
                region = (0, OUT_H)
            else:
                fg_h = int(round(OUT_W * sh / sw / 2)) * 2
                fg_y = (OUT_H - fg_h) // 2 - 150
                frame_chain = (f"split=2[bg0][fg0];[bg0]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                               f"crop={OUT_W}:{OUT_H},gblur=sigma=28,eq=brightness=-0.12[bgb];"
                               f"[fg0]scale={OUT_W}:{fg_h},setsar=1[fgs];[bgb][fgs]overlay=0:{fg_y},setsar=1")
                card_chain = (f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                              f"crop={OUT_W}:{OUT_H},setsar=1")
                region = (fg_y, fg_y + fg_h)
            # Watermark bar from the source preset (face and wide layouts only).
            if preset.get("bar") in ("bottom", "top") and preset.get("height", 0) > 0:
                top, bottom = region
                bar_px = int((bottom - top) * float(preset["height"]))
                yb = top if preset["bar"] == "top" else bottom - bar_px
                bar_filter = f",drawbox=x=0:y={yb}:w={OUT_W}:h={bar_px}:color=0x0E0E0E@1:t=fill"
                if framing == "face" and preset["bar"] == "bottom":
                    layout["bar_px"] = bar_px
                    layout["plate_y"] = OUT_H - bar_px - 40

        build_subs(cfg, clip, words, tmp / "subs.ass", layout)
        main_v = f"[{{vin}}:v]fps={FPS},{frame_chain}{bar_filter},{ass_opt},format=yuv420p[mv]"
        main_a = ("[{ain}:a]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
                  "aformat=sample_fmts=fltp:channel_layouts=stereo[ma]")

        card_text = (clip.get("context_card") or "").strip() if cfg.get("context_cards", False) else ""
        inputs, filters = [], []
        if card_text:
            cdur = card_duration(card_text)
            build_card_subs(cfg, card_text, cdur, tmp / "card.ass")
            util.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", src, "-frames:v", "1",
                      "-vf", f"{card_chain},gblur=sigma=26,eq=brightness=-0.28:saturation=0.7",
                      str(tmp / "card_bg.png")], cwd=tmp)
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{cdur}", "-i", "card_bg.png",
                       "-f", "lavfi", "-t", f"{cdur}", "-i", "anullsrc=r=48000:cl=stereo"]
            card_ass = "ass=card.ass:fontsdir=fonts" if FONT_DIR.exists() else "ass=card.ass"
            filters.append(f"[0:v]fps={FPS},scale={OUT_W}:{OUT_H},setsar=1,{card_ass},format=yuv420p[cv]")
            filters.append("[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[ca]")
            vin = 2
        else:
            vin = 0
        inputs += ["-ss", f"{start:.3f}", "-t", f"{dur}", "-i", src]
        ain, nxt, gamechain = vin, vin + 1, ""
        if framing == "split":
            speed = min(8.0, max(0.25, float(cfg.get("gameplay_speed", 2.0))))
            files = [game] + list(clip.get("gameplay_extra") or [])
            # Where does the match really end? (Found once per capture, then remembered.)
            ends = {}
            hit = matchend.finish(game, cfg, log)
            if hit:
                ends[game] = hit
            reach = hit["end"] if hit else util.probe(game)["duration"]
            if reach < dur * speed:                 # other captures will fill in before it
                for f in files[1:]:
                    other = matchend.finish(f, cfg, log)
                    if other:
                        ends[f] = other
            plan = gameplay_plan(files, dur, speed, ends=ends)
            if len(plan) > 1:
                log(f"      2K: {len(plan)} captures chained at {speed:g}x")
            g_inputs, gamechain = _gameplay_inputs(plan, nxt, speed, OUT_W, SPLIT_GAME_H)
            inputs += g_inputs
            nxt += len(plan)
        if not info["has_audio"]:
            inputs += ["-f", "lavfi", "-t", f"{dur}", "-i", "anullsrc=r=48000:cl=stereo"]
            ain = nxt
        filters.append(main_v.format(vin=vin, gamechain=gamechain))
        filters.append(main_a.format(ain=ain))
        if card_text:
            filters.append("[cv][ca][mv][ma]concat=n=2:v=1:a=1[v][a]")
            vmap, amap = "[v]", "[a]"
        else:
            vmap, amap = "[mv]", "[ma]"

        cmd = (["ffmpeg", "-y", "-v", "error"] + inputs +
               ["-filter_complex", ";".join(filters), "-map", vmap, "-map", amap,
                "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", "60",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-movflags", "+faststart", "out.mp4"])
        log("      encoding…")
        util.run(cmd, cwd=tmp)
        shutil.move(str(tmp / "out.mp4"), out_path)
        out_path.with_suffix(".txt").write_text(post_text(clip), encoding="utf-8")
        return out_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def post_text(clip):
    tags = clip.get("hashtags") or ""
    if isinstance(tags, list):
        tags = " ".join(tags)
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in tags.split())
    return f"{(clip.get('caption') or '').strip()}\n\n{tags}".strip()
