"""Shoot Clipper: desktop window (pywebview / Edge WebView2) around the local web UI.

    python app.py              normal: native window
    python app.py --browser    open in your default browser instead (also the automatic
                               fallback if the window can't start)
    ShootClipper.exe --autopilot [N]   one headless Make clips pass (Task Scheduler uses this)
    ShootClipper.exe --remind          show a notification if a scheduled clip is due
    ShootClipper.exe --selftest OUT    check the packaged build and write a report to OUT
"""
import multiprocessing
import os
import sys
import time
import webbrowser
from pathlib import Path

# Windowless runs (ShootClipper.exe, pythonw) have no console: printing, or a
# library drawing a progress bar, would raise. Send that output nowhere instead.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    # The installer puts ffmpeg next to the exe; put it first on PATH so every
    # ffmpeg/ffprobe call (ours and yt-dlp's) finds it without a system install.
    _ff = Path(sys.executable).resolve().parent / "ffmpeg"
    if _ff.is_dir():
        os.environ["PATH"] = str(_ff) + os.pathsep + os.environ.get("PATH", "")
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def run_in_browser(api, url):
    """Fallback: serve to the default browser and exit once the tab has been closed a while."""
    from clipper import applog
    webbrowser.open(url)
    applog.write(f"Running in browser mode at {url}")
    import datetime as dt
    api._last_poll = dt.datetime.now()
    while True:
        time.sleep(5)
        idle = (dt.datetime.now() - api._last_poll).total_seconds()
        if idle > 45 and not api._busy:
            break


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        from clipper import selftest
        i = args.index("--selftest")
        out = args[i + 1] if i + 1 < len(args) else "selftest.txt"
        sys.exit(selftest.run(out))
    if "--remind" in args:
        from clipper import remind
        remind.check()
        return
    if "--autopilot" in args:
        import autopilot
        sys.argv = [sys.argv[0]] + [a for a in args if a != "--autopilot"]
        return autopilot.main()

    from clipper import applog, server
    from clipper.api import Api
    api = Api()
    port = 0
    for a in args:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    _, port = server.start(api, port)
    url = f"http://127.0.0.1:{port}/"
    if "--browser" in args:
        return run_in_browser(api, url)
    try:
        import webview
        window = webview.create_window("Shoot Clipper", url, js_api=api, width=1380, height=900,
                                       min_size=(1100, 700), background_color="#1b1b1c")
        api._window = window
        webview.start()
    except Exception as e:
        applog.write(f"Desktop window failed ({e}); falling back to the browser")
        if "--no-fallback" in args:
            raise
        run_in_browser(api, url)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
