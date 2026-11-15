"""Finding where a 2K capture's match ends."""
import pytest

from clipper import config, matchend, render
from conftest import needs_ffmpeg


def test_coarse_frames_cover_the_last_five_minutes():
    times = matchend.coarse_times(1000.0)
    assert times[0] == pytest.approx(700.0) and times[-1] == pytest.approx(999.7)
    assert len(times) == matchend.MAX_COARSE
    assert times[1] - times[0] < 3.5                 # a winner graphic can't slip between frames
    short = matchend.coarse_times(30.0)
    assert short[0] == 0.0 and len(short) == 15


def test_choose_end():
    # winner graphic on screen from 100 to 109: stop four seconds in
    assert matchend.choose_end("winner_graphic", 100, 109, 200) == 104
    # it only lasts two seconds before a replay: stop while it's still there
    assert matchend.choose_end("winner_graphic", 100, 102, 200) == 102
    # a celebration with no graphic gets longer
    assert matchend.choose_end("celebration", 100, 130, 200) == 112
    # seen in a single frame: hold one second, never past the end of the file
    assert matchend.choose_end("winner_graphic", 100, 100, 200) == 101
    assert matchend.choose_end("winner_graphic", 199.5, 199.5, 200) == 200


@needs_ffmpeg
def test_locate_takes_a_wide_look_then_a_close_one(cfg, media, claude):
    def on_request(body):
        images = [p for p in body["messages"][0]["content"] if p["type"] == "image"]
        assert images and all(p["source"]["media_type"] == "image/jpeg" for p in images)
        if len(claude.requests) == 1:
            return {"found": True, "kind": "winner_graphic", "first": 22.1, "last": 24.2,
                    "what_follows": "replay"}, "end_turn"
        return {"found": True, "kind": "winner_graphic", "first": 21.5, "last": 26.0,
                "what_follows": "replay"}, "end_turn"
    claude.on_request = on_request
    hit = matchend.locate(str(media.capture), cfg)
    assert len(claude.requests) == 2
    assert hit["found"] and hit["kind"] == "winner_graphic"
    assert hit["first"] == pytest.approx(21.5, abs=0.3)
    assert hit["end"] == pytest.approx(hit["first"] + matchend.HOLD_GRAPHIC, abs=0.6)
    assert hit["cost"] > 0


@needs_ffmpeg
def test_answers_are_remembered_per_file(cfg, media, claude, log):
    claude.answers["found"] = {"found": True, "kind": "celebration", "first": 20.0, "last": 29.0, "what_follows": "nothing"}
    first = matchend.finish(str(media.capture), cfg, log)
    n = len(claude.requests)
    assert first["found"] and n == 2 and "finish found" in log.text()
    again = matchend.finish(str(media.capture), cfg, log)
    assert again["end"] == first["end"] and len(claude.requests) == n       # no second look


@needs_ffmpeg
def test_nothing_found_falls_back_to_the_end_of_the_file(cfg, media, claude, log):
    claude.answers["found"] = {"found": False, "kind": "none", "first": -1, "last": -1, "what_follows": ""}
    assert matchend.finish(str(media.capture), cfg, log) is None
    assert len(claude.requests) == 1                 # no close-up when the wide look finds nothing
    assert matchend.finish(str(media.capture), cfg, log) is None
    assert len(claude.requests) == 1                 # "not found" is remembered too
    (_, start, length, _), = render.gameplay_plan([str(media.capture)], 10, 2.0, ends={})
    assert start + length == pytest.approx(30.0, abs=0.2)


@needs_ffmpeg
def test_no_key_or_switched_off_never_calls_claude(cfg, media, claude, log, monkeypatch):
    cfg["find_match_end"] = False
    assert matchend.finish(str(media.capture), cfg, log) is None
    cfg["find_match_end"] = True
    monkeypatch.setattr(config, "get_api_key", lambda: "")
    assert matchend.finish(str(media.capture), cfg, log) is None
    cfg["analysis_mode"] = "paste"
    assert matchend.finish(str(media.capture), cfg, log) is None
    assert claude.requests == []


@needs_ffmpeg
def test_a_failed_look_is_not_remembered(cfg, media, claude, log):
    claude.on_request = lambda body: ("__error__", 401, "invalid x-api-key")
    assert matchend.finish(str(media.capture), cfg, log) is None
    assert "couldn't find the finish" in log.text()
    claude.on_request = None
    claude.answers["found"] = {"found": True, "kind": "winner_graphic", "first": 20.0, "last": 25.0, "what_follows": ""}
    assert matchend.finish(str(media.capture), cfg, log)["found"]            # tried again next time
