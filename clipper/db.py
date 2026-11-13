"""SQLite library: interviews and clips."""
import json
import sqlite3
import threading
import time

from . import config

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, url TEXT, source_path TEXT, workdir TEXT,
    source_preset TEXT DEFAULT 'None / unknown',
    duration REAL DEFAULT 0, status TEXT DEFAULT 'queued',
    error TEXT DEFAULT '', speakers TEXT DEFAULT '', summary TEXT DEFAULT '',
    copyright_risk TEXT DEFAULT '', cost REAL DEFAULT 0,
    pick_reason TEXT DEFAULT '', topic TEXT DEFAULT '', source_label TEXT DEFAULT '',
    created REAL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER, title TEXT, start REAL, end REAL,
    hook TEXT DEFAULT '', context_card TEXT DEFAULT '', caption TEXT DEFAULT '',
    hashtags TEXT DEFAULT '', rating INTEGER DEFAULT 3, crossover INTEGER DEFAULT 0,
    why TEXT DEFAULT '', flags TEXT DEFAULT '', series TEXT DEFAULT '',
    framing TEXT DEFAULT 'face',
    gameplay TEXT DEFAULT '', gameplay_hint TEXT DEFAULT '', speaker TEXT DEFAULT '',
    status TEXT DEFAULT 'suggested',
    output_path TEXT DEFAULT '', scheduled_for TEXT DEFAULT '',
    posted_at TEXT DEFAULT '', views INTEGER DEFAULT 0, notes TEXT DEFAULT '',
    created REAL
);
"""

# Clip status flow: suggested -> approved -> rendered -> scheduled -> posted
#                   suggested -> rejected


def _conn():
    config.ensure_app_dir()
    c = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        # light migrations for older databases
        cols = {r[1] for r in c.execute("PRAGMA table_info(interviews)")}
        for col in ("pick_reason", "topic", "source_label"):
            if col not in cols:
                c.execute(f"ALTER TABLE interviews ADD COLUMN {col} TEXT DEFAULT ''")
        if "attempts" not in cols:
            c.execute("ALTER TABLE interviews ADD COLUMN attempts INTEGER DEFAULT 0")
        ccols = {r[1] for r in c.execute("PRAGMA table_info(clips)")}
        for col in ("gameplay", "gameplay_hint", "speaker"):
            if col not in ccols:
                c.execute(f"ALTER TABLE clips ADD COLUMN {col} TEXT DEFAULT ''")


def _rows(sql, args=()):
    with _lock, _conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _exec(sql, args=()):
    with _lock, _conn() as c:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid


# ── interviews ──────────────────────────────────────────────────────────────

def add_interview(title, url="", source_path="", workdir="", preset="None / unknown",
                  pick_reason="", topic=""):
    return _exec(
        "INSERT INTO interviews (title,url,source_path,workdir,source_preset,pick_reason,topic,created) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (title, url, source_path, workdir, preset, pick_reason, topic, time.time()))


def update_interview(iid, **fields):
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE interviews SET {keys} WHERE id=?", (*fields.values(), iid))


def get_interview(iid):
    r = _rows("SELECT * FROM interviews WHERE id=?", (iid,))
    return r[0] if r else None


def list_interviews():
    return _rows("SELECT * FROM interviews ORDER BY id DESC")


def delete_clip(cid):
    _exec("DELETE FROM clips WHERE id=?", (cid,))


def delete_interview(iid):
    _exec("DELETE FROM clips WHERE interview_id=?", (iid,))
    _exec("DELETE FROM interviews WHERE id=?", (iid,))


# ── clips ───────────────────────────────────────────────────────────────────

def add_clips(iid, clips):
    for c in clips:
        _exec(
            "INSERT INTO clips (interview_id,title,start,end,hook,context_card,caption,"
            "hashtags,rating,crossover,why,flags,series,framing,gameplay_hint,speaker,created) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, c.get("title", ""), float(c["start"]), float(c["end"]),
             c.get("hook", ""), c.get("context_card", ""), c.get("caption", ""),
             " ".join(c.get("hashtags", [])) if isinstance(c.get("hashtags"), list)
             else c.get("hashtags", ""),
             int(c.get("rating", 3)), int(bool(c.get("crossover", False))),
             c.get("why", ""), json.dumps(c.get("flags", [])), c.get("series", ""),
             c.get("framing", "face"), ", ".join(c.get("gameplay_wrestlers", []) or []),
             c.get("speaker", ""), time.time()))


def clear_suggestions(iid):
    """Drop un-actioned suggestions before a re-analysis (keeps approved/rendered)."""
    _exec("DELETE FROM clips WHERE interview_id=? AND status IN ('suggested','rejected')", (iid,))


def update_clip(cid, **fields):
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE clips SET {keys} WHERE id=?", (*fields.values(), cid))


def get_clip(cid):
    r = _rows("SELECT * FROM clips WHERE id=?", (cid,))
    return r[0] if r else None


def list_clips(interview_id=None, statuses=None):
    sql = ("SELECT clips.*, interviews.title AS interview_title FROM clips "
           "JOIN interviews ON interviews.id = clips.interview_id WHERE 1=1")
    args = []
    if interview_id is not None:
        sql += " AND interview_id=?"
        args.append(interview_id)
    if statuses:
        sql += f" AND clips.status IN ({','.join('?' * len(statuses))})"
        args += list(statuses)
    sql += " ORDER BY clips.interview_id DESC, clips.rating DESC, clips.start"
    return _rows(sql, args)


def library_digest(limit=60):
    """Short list of existing clips so Claude can spot series connections."""
    rows = _rows("SELECT title, series FROM clips WHERE status IN "
                 "('approved','rendered','scheduled','posted') ORDER BY id DESC LIMIT ?",
                 (limit,))
    return [f"{r['title']}" + (f" [series: {r['series']}]" if r["series"] else "") for r in rows]


def taken_slots():
    return {r["scheduled_for"] for r in _rows(
        "SELECT scheduled_for FROM clips WHERE scheduled_for != ''")}
