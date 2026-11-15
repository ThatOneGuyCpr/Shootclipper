"""The Make clips flow end to end, with a fake Whisper, a fake Claude and real ffmpeg."""
import shutil
from pathlib import Path

import pytest

from clipper import db, ingest, jobs, news, pipeline, screen, transcribe, util
from conftest import needs_ffmpeg

URL = "https://www.youtube.com/watch?v=abcdefghijk"


def analysis(start, end, **extra):
    clip = {"title": "Bus fire", "start": start, "end": end, "hook": "", "caption": "The bus caught fire",
            "hashtags": ["#wrestling"], "rating": 5, "crossover": True, "why": "A full story.",
            "framing": "face", "speaker": "Hulk Hogan", "gameplay_wrestlers": ["Hulk Hogan", "Jesse Ventura"]}
    clip.update(extra)
    return {"interview": {"source_label": "Shoot", "speakers": ["Hulk Hogan"], "summary": "Stories."},
            "clips": [clip]}


def fake_download(media):
    def download(iv, log):
        dst = Path(iv["workdir"]) / "source.mp4"
        shutil.copy(media.interview, dst)
        return dst, "Hogan shoot interview"
    return download


@pytest.fixture
def short_margins(monkeypatch):
    """The test interview is 40 seconds long, so shrink the 30 s margins around stories."""
    monkeypatch.setattr(screen, "PAD_BEFORE", 3.0)
    monkeypatch.setattr(screen, "PAD_AFTER", 3.0)
    monkeypatch.setattr(screen, "MERGE_GAP", 2.0)


def picks(n=4):
    return [{"url": f"https://www.youtube.com/watch?v=pick{i}xxxxxx", "title": f"Pick {i}", "channel": "CVV",
             "duration": 3600, "views": 1000, "why": "stories", "topic": "t", "score": 8} for i in range(n)]


@needs_ffmpeg
def test_link_to_finished_clip_transcribing_only_the_stories(cfg, media, claude, whisper, log, monkeypatch,
                                                             short_margins):
    """Screen the captions, download, transcribe just the story section, pick, render."""
    cues = [(float(t), "and then the bus caught fire") for t in range(0, 40, 2)]
    monkeypatch.setattr(screen, "fetch_captions",
                        lambda url: ({"title": "Hogan shoot interview", "duration": 40.0, "channel": "RF"}, cues))
    monkeypatch.setattr(ingest, "download", fake_download(media))
    claude.answers["stories"] = {"summary": "Road stories.", "stories": [
        {"start": 12, "end": 22, "title": "Bus fire", "strength": 5},
        {"start": 14, "end": 20, "title": "Same bus", "strength": 4}]}
    claude.answers["clips"] = analysis(12.0, 22.0)
    cfg["framing_mode"] = "face only"
    iid = ingest.add_url(cfg, URL, "None / unknown")
    steps = []

    outs = pipeline.go(cfg, log, step=lambda name, state: steps.append((name, state)), iid=iid)

    assert len(outs) == 1 and Path(outs[0]).exists()
    info = util.probe(outs[0])
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["duration"] == pytest.approx(10.5, abs=1.5)
    # Whisper ran on the story section (with its margins), not the whole 40 seconds.
    assert len(whisper.asked) == 1 and whisper.asked[0] < 40
    data = transcribe.load(db.get_interview(iid)["workdir"])
    assert data["partial"] is True
    assert steps.index(("screen", "done")) < steps.index(("download", "active"))
    assert ("render", "done") in steps
    iv = db.get_interview(iid)
    assert iv["status"] == "done" and iv["title"] == "Hogan shoot interview"
    # The clip picker was told the transcript is partial and given the first-pass notes.
    picker = next(b for b in claude.requests if "clips" in claude.schema_props(b))
    text = " ".join(part.get("text", "") for part in picker["messages"][0]["content"])
    assert "ONLY PARTS OF THIS INTERVIEW ARE TRANSCRIBED" in text and "Bus fire (strength 5)" in text
    assert Path(outs[0]).with_suffix(".txt").read_text(encoding="utf-8").startswith("The bus caught fire")


@needs_ffmpeg
def test_no_captions_means_a_full_transcript(cfg, media, claude, whisper, log, monkeypatch):
    monkeypatch.setattr(screen, "fetch_captions", lambda url: ({"title": "x", "duration": 40.0, "channel": ""}, None))
    monkeypatch.setattr(ingest, "download", fake_download(media))
    claude.answers["clips"] = analysis(12.0, 22.0)
    iid = ingest.add_url(cfg, URL, "None / unknown")
    iv = pipeline.process_interview(cfg, iid, log)
    assert iv["status"] == "analyzed"
    assert whisper.asked == [pytest.approx(40.0, abs=0.2)]
    assert "transcribing the whole interview" in log.text()
    assert len(db.list_clips(iid)) == 1


@needs_ffmpeg
def test_local_files_skip_screening(cfg, media, claude, whisper, log):
    claude.answers["clips"] = analysis(5.0, 20.0)
    iid = ingest.add_local_file(cfg, str(media.interview), "None / unknown")
    steps = []
    pipeline.process_interview(cfg, iid, log, step=lambda n, s: steps.append((n, s)))
    assert ("screen", "skip") in steps
    assert not any("stories" in claude.schema_props(b) for b in claude.requests)
    assert transcribe.load(db.get_interview(iid)["workdir"])["partial"] is False


@needs_ffmpeg
def test_wrong_story_sections_fall_back_to_the_full_transcript(cfg, media, claude, whisper, log, monkeypatch,
                                                               short_margins):
    """If nothing usable comes out of the story sections, transcribe everything and ask again."""
    monkeypatch.setattr(screen, "fetch_captions",
                        lambda url: ({"title": "x", "duration": 40.0, "channel": ""}, [(float(t), "x") for t in range(40)]))
    monkeypatch.setattr(ingest, "download", fake_download(media))
    claude.answers["stories"] = {"summary": "", "stories": [{"start": 2, "end": 12, "title": "a", "strength": 4},
                                                            {"start": 3, "end": 11, "title": "b", "strength": 4}]}
    asked = []

    def clips(body):
        asked.append(1)
        return {"interview": {}, "clips": []} if len(asked) == 1 else analysis(25.0, 36.0)
    claude.answers["clips"] = clips
    iid = ingest.add_url(cfg, URL, "None / unknown")
    iv = pipeline.process_interview(cfg, iid, log)
    assert iv["status"] == "analyzed" and len(asked) == 2
    assert len(whisper.asked) == 2 and whisper.asked[-1] == pytest.approx(40.0, abs=0.2)
    assert db.list_clips(iid)[0]["start"] == pytest.approx(25.0, abs=1.0)


@needs_ffmpeg
def test_stop_during_transcription_keeps_everything_so_far(cfg, media, log, monkeypatch):
    iid = ingest.add_local_file(cfg, str(media.interview), "None / unknown")

    def stopped(*a, **k):
        jobs.request()
        jobs.check()
    monkeypatch.setattr(transcribe, "transcribe", stopped)
    steps = []
    with pytest.raises(jobs.Cancelled):
        pipeline.process_interview(cfg, iid, log, step=lambda n, s: steps.append((n, s)))
    iv = db.get_interview(iid)
    assert iv["status"] != "error" and not iv["error"] and not iv["attempts"]
    assert ("transcribe", "stopped") in steps


@needs_ffmpeg
def test_failures_are_recorded_and_retried_twice(cfg, media, log, monkeypatch):
    iid = ingest.add_local_file(cfg, str(media.interview), "None / unknown")
    monkeypatch.setattr(transcribe, "transcribe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    for attempt in (1, 2):
        assert pipeline.resumable(log) == iid
        with pytest.raises(RuntimeError, match="boom"):
            pipeline.process_interview(cfg, iid, log)
        assert db.get_interview(iid)["attempts"] == attempt
    assert pipeline.resumable(log) is None            # two failures: wait for the user


@needs_ffmpeg
def test_moved_file_is_a_clear_error(cfg, media, log, tmp_path):
    src = tmp_path / "talk.mp4"
    shutil.copy(media.interview, src)
    iid = ingest.add_local_file(cfg, str(src), "None / unknown")
    src.unlink()
    with pytest.raises(RuntimeError, match="moved or deleted"):
        pipeline.process_interview(cfg, iid, log)
    assert pipeline.resumable(log) is None            # nothing to retry


def test_ask_mode_stops_with_interviews_to_choose_from(cfg, log, monkeypatch):
    util.write_json(Path(cfg["workspace"]) / "picks.json", picks())
    monkeypatch.setattr(news, "news_is_fresh", lambda cfg: True)
    monkeypatch.setattr(pipeline, "process_interview", lambda *a, **k: pytest.fail("should wait for a choice"))
    assert pipeline.go(cfg, log, ask=True) == []
    assert "Found 4 interviews" in log.text()
    news.dismiss(picks()[0]["url"])
    assert len(pipeline.fresh_picks(cfg)) == 3


@needs_ffmpeg
def test_queued_interviews_go_before_found_ones(cfg, media, log, monkeypatch):
    util.write_json(Path(cfg["workspace"]) / "picks.json", picks())
    iid = ingest.add_local_file(cfg, str(media.interview), "None / unknown")
    seen = []
    monkeypatch.setattr(pipeline, "process_interview",
                        lambda cfg, i, *a, **k: seen.append(i) or dict(db.get_interview(i), status="queued"))
    pipeline.go(cfg, log, ask=True)
    assert seen == [iid]


def test_queueing_a_screened_pick_carries_its_stories(cfg):
    p = picks(1)[0]
    p["screen"] = {"stories": [{"start": 10, "end": 90, "title": "Bus fire", "strength": 5}], "summary": "s",
                   "story_count": 1, "story_minutes": 1.3, "score": 3, "top": ["Bus fire"], "duration": 3600}
    util.write_json(Path(cfg["workspace"]) / "picks.json", [p])
    iid = pipeline.queue_pick(cfg, p["url"])
    saved = screen.load(db.get_interview(iid)["workdir"])
    assert saved["available"] and saved["stories"][0]["title"] == "Bus fire"
    assert pipeline.fresh_picks(cfg) == []            # it's in Your interviews now


def test_bad_clip_picks_are_dropped(cfg, tmp_path):
    words = [{"w": f"w{i}", "s": 100 + i * 0.5, "e": 100.4 + i * 0.5} for i in range(120)]
    data = {"duration": 3600.0, "partial": True, "ranges": [[100.0, 160.0]],
            "segments": [{"start": 100.0, "end": 160.0, "text": "", "words": words}]}
    iid = db.add_interview("T", workdir=str(tmp_path))
    iv = db.get_interview(iid)
    from clipper import analyze
    result = {"interview": {}, "clips": [
        {"title": "good", "start": 105, "end": 140},
        {"title": "in an untranscribed stretch", "start": 900, "end": 960},
        {"title": "backwards", "start": 140, "end": 120},
        {"title": "two seconds", "start": 110, "end": 112},
        {"title": "no times"},
    ]}
    assert analyze.ingest_result(iv, data, result) == 1
    assert [c["title"] for c in db.list_clips(iid)] == ["good"]


def test_adding_a_file_that_is_not_a_video_leaves_no_trace(cfg, tmp_path):
    bad = tmp_path / "notes.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(ValueError, match="can't be read|can be read"):
        ingest.add_local_file(cfg, str(bad), "None / unknown")
    assert db.list_interviews() == []
