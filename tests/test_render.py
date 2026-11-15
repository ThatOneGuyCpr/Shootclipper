"""Rendering: caption timing, 2K planning, and real renders of both layouts."""
from pathlib import Path

import pytest

from clipper import render, util
from conftest import needs_ffmpeg


def test_caption_chunks_keep_to_three_short_words():
    words = [{"w": w, "s": i * 0.3, "e": i * 0.3 + 0.25} for i, w in
             enumerate("so Vince calls me at midnight and says unbelievable things".split())]
    chunks = render.caption_chunks(words)
    assert all(len(c) <= 3 for c in chunks)
    assert [w["w"] for c in chunks for w in c] == [w["w"] for w in words]
    assert all(sum(len(w["w"]) for w in c) <= 18 or len(c) == 1 for c in chunks)


def test_swears_are_masked():
    assert render._mask("fucking") != "fucking" and render._mask("fucking")[0].lower() == "f"
    assert render._mask("Vince") == "Vince"


def test_output_name_is_safe_for_windows():
    name = render.output_name({"id": 7, "title": 'He said: "what?" <no>'}, "Kliq This #212 | Nash")
    assert not set(name) & set('<>:"/\\|?*') and name.startswith("0007_")


@needs_ffmpeg
class TestGameplayPlan:
    def test_long_match_starts_partway_in(self, media):
        # 10 s clip at 2x needs 20 s of a 30 s capture: start 10 s in, finish on the last frame
        (path, start, length, loop), = render.gameplay_plan([str(media.capture)], 10, 2.0)
        assert (start, length, loop) == (pytest.approx(10, abs=0.2), 20, False)

    def test_short_match_loops_when_it_is_all_there_is(self, media):
        (path, start, length, loop), = render.gameplay_plan([str(media.short)], 10, 2.0)
        assert loop and length == 20

    def test_short_match_is_preceded_by_other_captures(self, media):
        plan = render.gameplay_plan([str(media.short), str(media.capture)], 10, 2.0)
        assert [Path(p).name for p, *_ in plan] == [media.capture.name, media.short.name]
        assert plan[-1][1:3] == (0.0, pytest.approx(6, abs=0.2))             # the finish plays whole, last
        assert sum(seg[2] for seg in plan) == pytest.approx(20, abs=0.1)

    def test_known_finish_replaces_the_end_of_the_file(self, media):
        ends = {str(media.capture): {"first": 22.0, "end": 24.0}}
        (path, start, length, loop), = render.gameplay_plan([str(media.capture)], 10, 2.0, ends=ends)
        assert start == pytest.approx(4.0) and start + length == pytest.approx(24.0)

    def test_filler_stops_before_its_own_winner_screen(self, media):
        ends = {str(media.short): {"first": 4.0, "end": 5.0},
                str(media.capture): {"first": 12.0, "end": 14.0}}
        plan = render.gameplay_plan([str(media.short), str(media.capture)], 10, 2.0, ends=ends)
        assert plan[-1] == (str(media.short), 0.0, 5.0, False)               # primary ends at its finish
        filler = [seg for seg in plan[:-1] if seg[0] == str(media.capture)]
        assert filler and all(seg[1] + seg[2] <= 12.0 + 1e-6 for seg in filler)
        assert sum(seg[2] for seg in plan) == pytest.approx(20, abs=0.1)

    def test_short_match_with_a_known_finish_repeats_without_its_tail(self, media):
        ends = {str(media.short): {"first": 3.0, "end": 4.0}}
        plan = render.gameplay_plan([str(media.short)], 5, 2.0, ends=ends)
        assert not any(loop for *_, loop in plan)
        assert plan[-1][1:3] == (0.0, 4.0)
        assert all(seg[1] + seg[2] <= 3.0 + 1e-6 for seg in plan[:-1])       # repeats never show the winner screen
        assert sum(seg[2] for seg in plan) == pytest.approx(10, abs=0.1)


def words(start, end):
    out, t, i = [], start + 0.2, 0
    while t + 0.4 < end:
        out.append({"w": f"word{i}", "s": round(t, 2), "e": round(t + 0.35, 2)})
        t, i = t + 0.5, i + 1
    return {"duration": 40.0, "segments": [{"start": start, "end": end, "text": "", "words": out}]}


def clip(**kw):
    c = {"id": 1, "title": "Bus fire", "start": 5.0, "end": 13.0, "framing": "face", "speaker": "Hulk Hogan",
         "hook": "", "context_card": "", "caption": "The bus caught fire", "hashtags": "#wrestling"}
    c.update(kw)
    return c


@needs_ffmpeg
def test_face_layout_renders(cfg, media, tmp_path, log):
    iv = {"source_path": str(media.interview), "title": "Hogan shoot", "source_preset": "", "source_label": "RF Video"}
    out = render.render_clip(cfg, iv, clip(), words(5, 13), tmp_path, log)
    info = util.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["duration"] == pytest.approx(8.0, abs=0.3) and info["has_audio"]


@needs_ffmpeg
def test_split_layout_renders_with_chained_2k(cfg, media, tmp_path, log):
    cfg["find_match_end"] = False
    iv = {"source_path": str(media.interview), "title": "Hogan shoot", "source_preset": "", "source_label": "RF Video"}
    c = clip(framing="split", gameplay=str(media.short), gameplay_extra=[str(media.capture)])
    out = render.render_clip(cfg, iv, c, words(5, 13), tmp_path, log)
    info = util.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["duration"] == pytest.approx(8.0, abs=0.3)


@needs_ffmpeg
def test_wide_layout_renders(cfg, media, tmp_path, log):
    iv = {"source_path": str(media.interview), "title": "Panel", "source_preset": "", "source_label": ""}
    out = render.render_clip(cfg, iv, clip(framing="wide"), words(5, 13), tmp_path, log)
    assert util.probe(out)["height"] == 1920


@needs_ffmpeg
def test_split_layout_reframes_up_and_down_when_the_speaker_changes(cfg, media, tmp_path, log, monkeypatch):
    """Two people at different heights: the crop moves sideways and vertically at the cut."""
    from clipper import tracking
    seen = {}

    def fake_track(src, start, dur, sw, sh, crop_w, log=None, stats=None, speakers=True):
        stats.update(cy=120.0, fh=80.0, cuts=1, heights=[(0.0, 120.0), (4.0, 260.0)])
        seen["crop_w"] = crop_w
        return [(0.0, 0), (4.0, sw - crop_w - (sw - crop_w) % 2)]
    monkeypatch.setattr(tracking, "face_track", fake_track)
    cfg["find_match_end"] = False
    iv = {"source_path": str(media.interview), "title": "Two shot", "source_preset": "", "source_label": ""}
    c = clip(framing="split", gameplay=str(media.capture))
    out = render.render_clip(cfg, iv, c, words(5, 13), tmp_path, log)
    info = util.probe(out)
    assert (info["width"], info["height"]) == (1080, 1920) and info["duration"] == pytest.approx(8.0, abs=0.3)
    assert seen["crop_w"] > 0
