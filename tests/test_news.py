"""Finding interviews: ranking, screening, and what stays in the list."""
from pathlib import Path

from clipper import news, screen, util

NEWS = {"as_of": "2026-11-15", "fetched_at": "2026-11-15T10:00:00", "topics": [
    {"topic": "Survivor Series", "why_now": "this month", "people": ["CM Punk"], "urgency": 5,
     "peak_date": "", "youtube_queries": ["cm punk shoot interview stories"]}]}


def results(*ids):
    return [{"url": f"https://www.youtube.com/watch?v={i}", "title": f"Interview {i}", "channel": "CVV",
             "duration": 3600, "views": 5000} for i in ids]


def rank(*ids):
    return {"picks": [{"url": f"https://www.youtube.com/watch?v={i}", "topic": "Survivor Series",
                       "why": "likely stories", "score": 8} for i in ids]}


def screened(count):
    stories = [{"start": 100.0 + 300 * k, "end": 200.0 + 300 * k, "title": f"Story {k}", "strength": 4}
               for k in range(count)]
    return dict(available=True, stories=stories, summary="s", cost=0.002, duration=3600.0, title="t",
                channel="c", **screen.summarize(stories))


def test_discover_screens_drops_duds_and_orders_by_stories(cfg, claude, log, monkeypatch):
    monkeypatch.setattr(news, "youtube_search", lambda q, n=6: results("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"))
    claude.answers["picks"] = rank("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd")
    by_id = {"aaaaaaaaaaa": screened(1), "bbbbbbbbbbb": screened(6), "ccccccccccc": screened(0),
             "ddddddddddd": {"available": False, "reason": "no captions"}}
    monkeypatch.setattr(screen, "screen_url", lambda cfg, url, client=None: by_id[url[-11:]])
    picks = news.discover(cfg, NEWS, log)
    assert [p["url"][-11:] for p in picks] == ["bbbbbbbbbbb", "ddddddddddd", "aaaaaaaaaaa"]
    assert picks[0]["screen"]["story_count"] == 6 and picks[1]["screen"] is None
    assert "no real stories" in log.text()
    # The dud is remembered, so it isn't searched up and screened again.
    assert "https://www.youtube.com/watch?v=ccccccccccc" in news.dismissed()
    assert [p["url"] for p in news.load_picks(cfg)] == [p["url"] for p in picks]


def test_find_more_adds_to_the_list_instead_of_replacing_it(cfg, claude, log, monkeypatch):
    cfg["transcribe_mode"] = "full"                          # no screening in this one
    monkeypatch.setattr(news, "youtube_search", lambda q, n=6: results("aaaaaaaaaaa"))
    claude.answers["picks"] = rank("aaaaaaaaaaa")
    news.discover(cfg, NEWS, log)
    monkeypatch.setattr(news, "youtube_search", lambda q, n=6: results("aaaaaaaaaaa", "bbbbbbbbbbb"))
    claude.answers["picks"] = rank("bbbbbbbbbbb")
    news.discover(cfg, NEWS, log)
    assert [p["url"][-11:] for p in news.load_picks(cfg)] == ["bbbbbbbbbbb", "aaaaaaaaaaa"]


def test_turned_down_interviews_never_come_back(cfg, claude, log, monkeypatch):
    cfg["transcribe_mode"] = "full"
    news.dismiss("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    seen = []
    monkeypatch.setattr(news, "youtube_search", lambda q, n=6: results("aaaaaaaaaaa", "bbbbbbbbbbb"))

    def answer(body):
        seen.append(body["messages"][0]["content"])
        return rank("bbbbbbbbbbb")
    claude.answers["picks"] = answer
    news.discover(cfg, NEWS, log)
    assert "aaaaaaaaaaa" not in seen[0] and "bbbbbbbbbbb" in seen[0]


def test_stale_or_damaged_news_is_not_fresh(cfg):
    assert news.news_is_fresh(cfg) is False
    util.write_json(news.news_path(cfg), {"topics": [{"topic": "x"}], "fetched_at": "garbage"})
    assert news.news_is_fresh(cfg) is False
    Path(news.news_path(cfg)).write_text("{", encoding="utf-8")
    assert news.load_news(cfg) is None


def test_scout_searches_then_structures(cfg, claude, log):
    def on_request(body):
        if body.get("tools"):
            assert body["tools"][0]["type"].startswith("web_search") and body["stream"] is True
            return "1. Survivor Series: Punk vs Rollins, 29 Nov. Search: cm punk shoot interview", "end_turn"
        return None
    claude.on_request = on_request
    claude.answers["topics"] = {"as_of": "2026-11-15", "topics": NEWS["topics"]}
    out = news.scout(cfg, log)
    assert out["topics"][0]["topic"] == "Survivor Series" and news.news_is_fresh(cfg)
    structured = claude.requests[-1]
    assert "Survivor Series: Punk vs Rollins" in structured["messages"][0]["content"]
