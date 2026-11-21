"""Promotion-owned and closely affiliated shows are kept out of Found interviews."""
from pathlib import Path

from clipper import blocklist, config, news, pipeline, util

BUNDLED = (config.BUNDLED_DATA / "blocked_channels.txt").read_text(encoding="utf-8")
RULES = blocklist.parse(BUNDLED)


def blocked(channel, title):
    return blocklist.reason(channel, title, RULES) is not None


def test_promotion_channels_and_shows_are_blocked():
    assert blocked("WWE", "anything")
    assert blocked("All Elite Wrestling", "Dynamite highlights")
    assert blocked("Fanatics", "Bayley opens up | What's Your Story? with Steph McMahon")
    assert blocked("The Undertaker", "Six Feet Under: Mark Henry tells all")
    assert blocked("Some Clips Channel", "The Sessions with Renée Paquette - Kenny Omega")
    assert blocked("Renee Paquette", "Full episode")
    assert blocked("Being The Elite", "BTE ep 400")
    assert blocked("Stone Cold", "Steve Austin's Broken Skull Sessions with Kurt Angle")


def test_independent_shows_and_promotion_guests_are_fine():
    assert not blocked("Chris Van Vliet", "Cody Rhodes on WWE, AEW and the Bloodline")
    assert not blocked("Inside The Ropes", "Shawn Michaels shoot interview")
    assert not blocked("Kayfabe Commentaries", "Timeline: WWE 1997 with Jim Cornette")
    assert not blocked("RF Video", "Road Dogg shoot interview")
    assert not blocked("Busted Open", "Bully Ray reacts")
    assert not blocked("WWE Fan Nation", "Interview")        # channel names match whole, not in part


def test_names_match_whatever_the_punctuation():
    assert blocklist.norm("What’s Your  Story?!") == "whats your story"
    assert blocked("x", "WHATS YOUR STORY w/ Steph")


def test_existing_lists_get_the_new_entries_once_and_keep_your_edits(tmp_path):
    mine = "# my list\nWWE\nSome Channel I Hate\n"
    merged = blocklist.merge_bundled(mine, BUNDLED, 2)
    assert merged.startswith(mine) and "show: Six Feet Under" in merged and merged.count("\nWWE\n") == 1
    assert "Some Channel I Hate" in merged


def test_upgrade_updates_saved_lists_once(tmp_path):
    (config.APP_DIR / ".lists_version").unlink(missing_ok=True)
    (config.APP_DIR / "blocked_channels.txt").write_text("WWE\nAEW\n", encoding="utf-8")
    (config.APP_DIR / "channels.txt").write_text("Chris Van Vliet\nTalk Is Jericho\nGrilling JR\n", encoding="utf-8")
    config.ensure_app_dir()
    text = (config.APP_DIR / "blocked_channels.txt").read_text(encoding="utf-8")
    assert "show: What's Your Story" in text
    assert (config.APP_DIR / "channels.txt").read_text(encoding="utf-8").split() == ["Chris", "Van", "Vliet"]
    # You un-block something; the next start doesn't re-add it.
    (config.APP_DIR / "blocked_channels.txt").write_text(text.replace("show: Grilling JR\n", ""), encoding="utf-8")
    config.ensure_app_dir()
    assert "Grilling JR" not in (config.APP_DIR / "blocked_channels.txt").read_text(encoding="utf-8")


def test_search_results_and_old_finds_are_filtered(cfg, claude, log, monkeypatch):
    cfg["transcribe_mode"] = "full"
    config.ensure_app_dir()
    (config.APP_DIR / "blocked_channels.txt").write_text(BUNDLED, encoding="utf-8")
    vids = [{"url": "https://www.youtube.com/watch?v=steph000000", "title": "Becky Lynch | What's Your Story?",
             "channel": "Fanatics", "duration": 3600, "views": 1},
            {"url": "https://www.youtube.com/watch?v=cvv00000000", "title": "Becky Lynch full interview",
             "channel": "Chris Van Vliet", "duration": 3600, "views": 1}]
    monkeypatch.setattr(news, "youtube_search", lambda q, n=6: [dict(v) for v in vids])
    seen = []

    def rank(body):
        seen.append(body["messages"][0]["content"])
        return {"picks": [{"url": vids[1]["url"], "topic": "t", "why": "w", "score": 8}]}
    claude.answers["picks"] = rank
    topics = {"fetched_at": "2026-11-20T10:00:00", "topics": [{"topic": "t", "why_now": "", "people": [], "urgency": 5,
                                                              "peak_date": "", "youtube_queries": ["becky lynch interview"]}]}
    news.discover(cfg, topics, log)
    assert "steph000000" not in seen[0] and "cvv00000000" in seen[0]
    assert "Never pick a show that WWE or AEW owns" in seen[0]
    # A find saved before the block (like the Stephanie McMahon one) disappears from the list.
    util.write_json(Path(cfg["workspace"]) / "picks.json", [dict(vids[0], why="", topic=""), dict(vids[1], why="", topic="")])
    assert [p["url"] for p in pipeline.fresh_picks(cfg)] == [vids[1]["url"]]
