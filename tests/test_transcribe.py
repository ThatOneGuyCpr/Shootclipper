"""Transcription: whole files, story sections only, and filling gaps later."""
import pytest

from clipper import transcribe
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def test_whole_file(cfg, media, whisper, tmp_path, log):
    data = transcribe.transcribe(media.interview, "Test", tmp_path, cfg, log)
    assert data["partial"] is False and data["ranges"] == [[0.0, pytest.approx(40.0, abs=0.2)]]
    assert whisper.asked == [pytest.approx(40.0, abs=0.2)]
    assert transcribe.load(tmp_path)["segments"] == data["segments"]
    assert (tmp_path / "transcript.txt").exists()


def test_only_the_story_sections_are_transcribed(cfg, media, whisper, tmp_path, log):
    seen = []
    data = transcribe.transcribe(media.interview, "Test", tmp_path, cfg, log, progress=seen.append,
                                 ranges=[[20, 28], [5, 11]])
    assert whisper.asked == [6.0, 8.0]                      # 14 s of audio instead of 40
    assert data["partial"] and data["ranges"] == [[5.0, 11.0], [20.0, 28.0]]
    starts = [w["s"] for s in data["segments"] for w in s["words"]]
    assert min(starts) >= 5.0 and max(starts) < 28.0        # times are in whole-file seconds
    assert not any(11.5 < t < 19.9 for t in starts)
    assert seen == sorted(seen) and seen[-1] == pytest.approx(1.0, abs=0.2)
    assert "story sections" in log.text()


def test_sections_covering_nearly_everything_become_a_full_transcript(cfg, media, whisper, tmp_path, log):
    data = transcribe.transcribe(media.interview, "Test", tmp_path, cfg, log, ranges=[[0, 36]])
    assert data["partial"] is False and len(whisper.asked) == 1


def test_missing_and_covered():
    data = {"partial": True, "duration": 100.0, "ranges": [[10.0, 20.0], [40.0, 60.0]]}
    assert transcribe.missing(data, 12, 18) == []
    assert transcribe.missing(data, 5, 45) == [[5.0, 10.0], [20.0, 40.0]]
    assert transcribe.missing(data, 55, 70) == [[60.0, 70.0]]
    assert transcribe.missing(data, 19.5, 20.4) == []       # under a second: not worth a run
    assert transcribe.missing({"partial": False, "duration": 100.0}, 0, 100) == []


def test_cover_fills_only_the_gap(cfg, media, whisper, tmp_path, log):
    data = transcribe.transcribe(media.interview, "Test", tmp_path, cfg, log, ranges=[[10, 18]])
    whisper.asked.clear()
    before = [w["s"] for s in data["segments"] for w in s["words"]]
    data = transcribe.cover(media.interview, "Test", tmp_path, cfg, data, 14, 26, log)
    assert whisper.asked == [10.0]                          # 18 to 28 (26 plus the 2 s margin)
    assert data["ranges"] == [[10.0, 28.0]]
    after = [w["s"] for s in data["segments"] for w in s["words"]]
    assert after == sorted(after) and set(before) <= set(after)
    assert len(after) == len(set(after))                    # no doubled words at the join
    assert transcribe.load(tmp_path)["ranges"] == [[10.0, 28.0]]
    # asking again does nothing
    whisper.asked.clear()
    transcribe.cover(media.interview, "Test", tmp_path, cfg, data, 14, 26, log)
    assert whisper.asked == []


def test_cover_leaves_full_transcripts_alone(cfg, media, whisper, tmp_path, log):
    data = transcribe.transcribe(media.interview, "Test", tmp_path, cfg, log)
    whisper.asked.clear()
    assert transcribe.cover(media.interview, "Test", tmp_path, cfg, data, 0, 40, log) is data
    assert whisper.asked == []


def test_normalize_ranges():
    assert transcribe.normalize_ranges([[30, 50], [10, 20], [18, 32], [70, 70.2], [90, 200]], 100) == \
        [[10.0, 50.0], [90.0, 100.0]]
    assert transcribe.normalize_ranges(None, 100) == []


def test_fixes_and_snap():
    words = [{"w": "Jim", "s": 0, "e": 0.3}, {"w": "Hurd", "s": 0.3, "e": 0.6}, {"w": "said", "s": 0.6, "e": 0.9}]
    fixed = transcribe.apply_fixes(words, [("jim hurd", "Jim Herd")])
    assert " ".join(w["w"] for w in fixed) == "Jim Herd said"
    data = {"segments": [{"start": 10, "end": 14, "text": "", "words": [
        {"w": "a", "s": 10.0, "e": 10.4}, {"w": "b", "s": 11.0, "e": 11.4}, {"w": "c", "s": 13.0, "e": 13.5}]}]}
    s, e = transcribe.snap(data, 10.9, 13.2)
    assert s < 11.0 and e > 13.5                            # snapped out to whole words, with breathing room


def test_damaged_transcript_reads_as_missing(tmp_path):
    (tmp_path / "transcript.json").write_text('{"segments": [', encoding="utf-8")
    assert transcribe.load(tmp_path) is None


def test_unreadable_audio_is_a_clear_error(tmp_path):
    bad = tmp_path / "not_a_video.mp4"
    bad.write_bytes(b"nope")
    with pytest.raises(RuntimeError, match="Couldn't read the audio"):
        transcribe.load_audio(bad)
