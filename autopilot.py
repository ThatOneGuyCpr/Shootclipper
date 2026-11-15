"""Headless Go button, for Windows Task Scheduler.

Runs one full pass (news -> pick -> transcribe -> analyze -> render) and logs to
%APPDATA%\\ShootClipper\\autopilot.log. Set it up from Settings in the app.
"""
import datetime as dt
import os
import sys
from pathlib import Path

# Windowless runs (ShootClipper.exe, pythonw) have no console: printing, or a
# library drawing a progress bar, would raise. Send that output nowhere instead.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clipper import applog, config, db, pipeline, util  # noqa: E402


def main():
    cfg = config.load()
    db.init()
    log_path = config.APP_DIR / "autopilot.log"

    def log(msg):
        line = f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {msg}"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line)
        applog.write(f"[autopilot] {msg}")

    lock = util.RunLock(config.APP_DIR / "run.lock")
    if not lock.acquire():
        log("Skipped: Shoot Clipper is already making clips.")
        return
    log("Autopilot run started")
    runs = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1
    outs = []
    try:
        for _ in range(runs):
            try:
                outs += pipeline.go(cfg, log)
            except Exception as e:
                log(f"Run failed: {e}")
    finally:
        lock.release()
    if outs and cfg.get("open_folder_when_done"):
        try:
            util.open_path(pipeline.ready_dir(cfg))
        except Exception:
            pass
    log(f"Autopilot finished: {len(outs)} clip(s)")


if __name__ == "__main__":
    main()
