"""Caption screening: parsing, section building, and the story finder."""
import json

from clipper import screen


def test_json3_captions():
    raw = json.dumps({"events": [
        {"tStartMs": 0, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 1200, "dDurationMs": 3000, "segs": [{"utf8": "so"}, {"utf8": " vince"}, {"utf8": " calls"}]},
        {"tStartMs": 4500, "segs": [{"utf8": "me\n"}]},
        {"tStartMs": 9000},
    ]})
    assert screen.parse_json3(raw) == [(1.2, "so vince calls"), (4.5, "me")]


def test_vtt_captions_drop_scrolling_repeats():
    raw = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000 align:start position:0%
so<00:00:01.400><c> vince</c><00:00:01.900><c> calls</c>

00:00:03.000 --> 00:00:05.500
so vince calls
me at midnight

01:02:03.250 --> 01:02:05.000
and&nbsp;fires me
"""
    assert screen.parse_vtt(raw) == [(1.0, "so vince calls"), (3.0, "me at midnight"), (3723.25, "and fires me")]


def test_video_id():
    assert screen.video_id("https://www.youtube.com/watch?v=6k_849fM-PI&t=10s") == "6k_849fM-PI"
    assert screen.video_id("https://youtu.be/6k_849fM-PI") == "6k_849fM-PI"
    assert screen.video_id("C:/videos/file.mp4") == ""


def test_caption_lines_group_by_time():
    cues = [(float(t), f"w{t}") for t in range(0, 60, 5)]
    lines = screen.caption_lines(cues, every=15).splitlines()
    assert lines[0] == "[0] w0 w5 w10"
    assert lines[1].startswith("[15] ")
    assert len(lines) == 4


def test_hand_made_captions_beat_automatic_ones():
    info = {"subtitles": {"en": [{"ext": "vtt", "url": "manual-vtt"}]},
            "automatic_captions": {"en": [{"ext": "json3", "url": "auto-json3"}]}}
    assert screen._pick_track(info) == ("manual-vtt", "vtt")
    info = {"automatic_captions": {"es": [{"ext": "json3", "url": "x"}],
                                   "en-orig": [{"ext": "vtt", "url": "v"}, {"ext": "json3", "url": "j"}]}}
    assert screen._pick_track(info) == ("j", "json3")
    assert screen._pick_track({"automatic_captions": {"es": [{"ext": "json3", "url": "x"}]}}) == (None, None)


def story(start, end, strength, title="t"):
    return {"start": start, "end": end, "title": title, "strength": strength}


def test_sections_pad_merge_and_skip_weak_stories():
    stories = [story(100, 160, 4), story(200, 260, 3), story(900, 960, 5), story(1500, 1530, 1)]
    secs = screen.sections(stories, duration=2000)
    # The first two are 40 s apart, closer than their padding: one section.
    assert secs == [[70.0, 285.0], [870.0, 985.0]]


def test_sections_are_clipped_to_the_video():
    assert screen.sections([story(5, 60, 4), story(1960, 1995, 4)], duration=2000) == [[0.0, 85.0], [1930.0, 2000.0]]


def test_sections_cap_drops_the_weakest_first():
    stories = [story(i * 400, i * 400 + 200, 5 if i % 2 else 3) for i in range(12)]
    secs = screen.sections(stories, duration=6000, limit=20 * 60)
    kept = sum(b - a for a, b in secs)
    assert kept <= 20 * 60
    # every strength-5 story fits before any strength-3 one is considered
    assert sum(1 for a, b in secs if (round((a + 30) / 400) % 2)) >= 4


def test_a_lone_good_story_brings_in_the_near_misses():
    secs = screen.sections([story(100, 160, 4), story(600, 660, 2)], duration=2000)
    assert len(secs) == 2
    assert screen.sections([], duration=2000) == []


def test_summarize_counts_only_real_stories():
    s = screen.summarize([story(0, 120, 5, "Bus fire"), story(200, 260, 3, "Rib"), story(300, 330, 2, "Opinion")])
    assert s["story_count"] == 2 and s["story_minutes"] == 3.0
    assert s["top"] == ["Bus fire", "Rib"] and s["score"] == 4


def test_bad_stories_are_cleaned_out():
    out = screen._clean_stories([{"start": 50, "end": 52, "title": "too short", "strength": 4},
                                 {"start": "x", "end": 9, "title": "junk"},
                                 {"start": 1990, "end": 2500, "title": "runs past the end", "strength": 9}], 2000)
    assert out == [{"start": 1990.0, "end": 2000.0, "title": "runs past the end", "strength": 5}]


def test_screen_url_finds_stories_with_the_small_model(cfg, claude, monkeypatch):
    cues = [(float(t), "and then vince walks in") for t in range(0, 1800, 4)]
    monkeypatch.setattr(screen, "fetch_captions",
                        lambda url: ({"title": "Shoot", "duration": 1800.0, "channel": "CVV"}, cues))
    claude.answers["stories"] = {"summary": "Road stories.",
                                 "stories": [story(100, 220, 5, "Bus fire"), story(700, 790, 3, "Rib on Jack")]}
    r = screen.screen_url(cfg, "https://www.youtube.com/watch?v=abcdefghijk")
    assert r["available"] and r["story_count"] == 2 and r["title"] == "Shoot"
    body = claude.requests[0]
    assert body["model"] == "claude-haiku-5-5"
    assert "[96] and then vince walks in" in body["messages"][0]["content"]


def test_screen_url_falls_back_when_the_small_model_is_missing(cfg, claude, monkeypatch):
    monkeypatch.setattr(screen, "fetch_captions",
                        lambda url: ({"title": "S", "duration": 600.0, "channel": ""}, [(float(t), "x") for t in range(0, 600, 5)]))

    def on_request(body):
        if body["model"] == "claude-haiku-5-5":
            return "__error__", 404, "model: claude-haiku-5-5 not found"
        return {"summary": "", "stories": [story(10, 100, 4)]}, "end_turn"
    claude.on_request = on_request
    r = screen.screen_url(cfg, "https://youtu.be/abcdefghijk")
    assert r["available"] and [b["model"] for b in claude.requests] == ["claude-haiku-5-5", cfg["model"]]


def test_no_captions_is_reported_not_raised(cfg, monkeypatch):
    monkeypatch.setattr(screen, "fetch_captions", lambda url: ({"title": "S", "duration": 600.0, "channel": ""}, None))
    r = screen.screen_url(cfg, "https://youtu.be/abcdefghijk")
    assert r["available"] is False and "captions" in r["reason"]

    def boom(url):
        raise OSError("HTTP Error 429")
    monkeypatch.setattr(screen, "fetch_captions", boom)
    assert screen.screen_url(cfg, "https://youtu.be/abcdefghijk")["available"] is False
