"""The app's backend: jobs, Stop, deleting, settings, and the local web server."""
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from clipper import config, db, jobs, pipeline, server, util
from clipper.api import Api
from conftest import needs_ffmpeg


@pytest.fixture
def api(cfg):
    return Api()


def wait(api, seconds=10):
    end = time.time() + seconds
    while api._busy and time.time() < end:
        time.sleep(0.05)
    assert not api._busy, "job didn't finish"
    return api.poll(0)["data"]


SLOW = ["ffmpeg", "-v", "error", "-re", "-f", "lavfi", "-i", "sine=d=30", "-f", "null", "-"]


@needs_ffmpeg
def test_stop_kills_the_running_step_and_keeps_the_app_usable(api):
    def job():
        api._step("download", "active")
        util.run(SLOW)
    api._start("Making clips", ["download", "transcribe"], job)
    time.sleep(0.6)
    t = time.time()
    assert api.stop()["data"] is True
    state = wait(api, 5)
    assert time.time() - t < 3
    assert state["steps"][0]["state"] == "stopped" and not state["error"]
    assert any("Stopped" in line for line in state["log"])
    # The Stop flag is cleared with the job, or the interface's own ffmpeg calls would die.
    assert not jobs.requested()
    assert util.run(["ffmpeg", "-version"]).returncode == 0


def test_stop_with_nothing_running_does_nothing(api):
    assert api.stop()["data"] is False and not jobs.requested()


def test_only_one_job_at_a_time(api):
    api._start("Making clips", [], lambda: time.sleep(0.5))
    out = api.cleanup_now()
    assert "still running" in out["error"]
    wait(api)


def test_job_errors_reach_the_log_without_crashing(api):
    api._start("Making clips", ["download"], lambda: (_ for _ in ()).throw(RuntimeError("disk is full")))
    state = wait(api)
    assert "disk is full" in state["error"] and any("disk is full" in line for line in state["log"])


def test_the_daily_run_and_the_app_never_work_at_once(api):
    lock = util.RunLock(config.APP_DIR / "run.lock")
    assert lock.acquire()
    try:
        api.make_clips()
        assert "daily run" in wait(api)["error"]
    finally:
        lock.release()
    second = util.RunLock(config.APP_DIR / "run.lock")
    assert second.acquire()
    second.release()


def test_deleting_an_interview_keeps_its_finished_clips(api, cfg):
    iid = db.add_interview("Herd", url="https://youtu.be/x", workdir=str(Path(cfg["workspace"]) / "interviews" / "herd"))
    wd = Path(db.get_interview(iid)["workdir"])
    wd.mkdir(parents=True)
    (wd / "source.mp4").write_bytes(b"0" * 100)
    db.update_interview(iid, status="done", source_path=str(wd / "source.mp4"))
    db.add_clips(iid, [{"title": "kept", "start": 0, "end": 5}, {"title": "dropped", "start": 0, "end": 5}])
    db.update_clip(db.list_clips(iid)[0]["id"], status="rendered")
    assert "data" in api.remove_interview(iid)
    assert not wd.exists()
    assert [c["title"] for c in api.library("to_post")["data"]["items"]] == ["kept"]
    assert all(i["id"] != iid for i in api.get_state()["data"]["interviews"])
    assert pipeline.resumable(lambda m: None) is None

    plain = db.add_interview("Nothing", workdir=str(Path(cfg["workspace"]) / "interviews" / "plain"))
    api.remove_interview(plain)
    assert db.get_interview(plain) is None


def test_settings_are_checked_and_saved(api):
    out = api.save_settings({"gameplay_speed": "1.5", "auto_clips": "3", "name_plate": False,
                             "cleanup_downloads_days": "14", "made_up_setting": 1})["data"]
    assert out["gameplay_speed"] == 1.5 and out["auto_clips"] == 3 and out["name_plate"] is False
    assert out["cleanup_downloads_days"] == 14 and "made_up_setting" not in out
    assert config.load()["gameplay_speed"] == 1.5
    assert "above zero" in api.save_settings({"gameplay_speed": 0})["error"]
    assert "valid value" in api.save_settings({"auto_clips": "lots"})["error"]
    assert config.load()["gameplay_speed"] == 1.5           # a rejected save changes nothing


def test_damaged_settings_and_data_files_fall_back_to_defaults(cfg):
    config.CONFIG_PATH.write_text('{"gameplay_speed": 3', encoding="utf-8")
    assert config.load()["gameplay_speed"] == config.DEFAULTS["gameplay_speed"]
    ws = Path(config.load()["workspace"])
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "picks.json").write_text("[{", encoding="utf-8")
    (ws / "news.json").write_text("nope", encoding="utf-8")
    api = Api()
    state = api.get_state()
    assert "data" in state and state["data"]["picks"] == [] and state["data"]["news"] is None


def test_found_interviews_show_story_counts(api, cfg):
    pick = {"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "Shoot", "channel": "CVV", "duration": 3600,
            "views": 10, "why": "w", "topic": "t",
            "screen": {"stories": [], "summary": "", "story_count": 5, "story_minutes": 14.0, "score": 9,
                       "top": ["Bus fire"], "duration": 3600}}
    util.write_json(Path(cfg["workspace"]) / "picks.json", [pick])
    shown = api.get_state()["data"]["picks"]
    assert shown[0]["screen"]["story_count"] == 5 and "abcdefghijk" in shown[0]["thumb"]
    api.dismiss_pick(pick["url"])
    assert api.get_state()["data"]["picks"] == []


def test_web_server_needs_the_token(api, tmp_path, cfg):
    httpd, port = server.start(api, 0)
    base = f"http://127.0.0.1:{port}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "Shoot Clipper" in page and "%%TOKEN%%" not in page

        def post(token):
            req = urllib.request.Request(base + "/api/poll", data=json.dumps([0]).encode(),
                                         headers={"X-Token": token, "Content-Type": "application/json"})
            return urllib.request.urlopen(req)
        with pytest.raises(urllib.error.HTTPError) as bad:
            post("wrong")
        assert bad.value.code == 403
        assert "data" in json.loads(post(api._token).read())
    finally:
        httpd.shutdown()
        httpd.server_close()
