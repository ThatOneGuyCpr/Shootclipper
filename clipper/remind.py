"""Posting reminders: a Windows notification when a scheduled clip's slot arrives.

Task Scheduler runs `ShootClipper.exe --remind` every ten minutes (set up from Settings).
That check is tiny: it looks for scheduled clips whose time has come, shows one
notification, and exits. It works whether or not the app is open.
"""
import datetime as dt
import subprocess
import sys
from pathlib import Path

from . import db, util

TASK = "ShootClipper Reminders"
WINDOW = dt.timedelta(hours=3)        # a slot missed by more than this (PC was off) is let go

_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:SC_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show($toast)
"""


def _xml_text(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def toast_xml(title, body):
    return ("<toast><visual><binding template='ToastGeneric'>"
            f"<text>{_xml_text(title)}</text><text>{_xml_text(body)}</text>"
            "</binding></visual></toast>")


def notify(title, body):
    """Show a Windows notification. Returns True if it was handed to Windows."""
    if not util.IS_WINDOWS:
        return False
    import os
    env = dict(os.environ, SC_TOAST_XML=toast_xml(title, body))
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", _TOAST], env=env, capture_output=True, timeout=30,
                           creationflags=util.NO_WINDOW)
        return p.returncode == 0
    except Exception:
        return False


def due(now=None):
    """Scheduled clips whose slot has arrived and haven't been announced yet."""
    now = now or dt.datetime.now()
    out = []
    for c in db.list_clips(statuses=["scheduled"]):
        if c.get("reminded") or not c.get("scheduled_at"):
            continue
        try:
            at = dt.datetime.fromisoformat(c["scheduled_at"])
        except ValueError:
            continue
        if at <= now:
            out.append((c, at))
    return out


def check(now=None, show=notify):
    """Announce what's due. Returns the number of clips announced."""
    now = now or dt.datetime.now()
    db.init()
    items = due(now)
    fresh = [c for c, at in items if now - at <= WINDOW]
    for c, _ in items:
        db.update_clip(c["id"], reminded=1)       # including ones too old to announce
    if not fresh:
        return 0
    if len(fresh) == 1:
        show("Time to post", f"“{fresh[0]['title']}” is scheduled for now. It's in Ready to post.")
    else:
        show("Time to post", f"{len(fresh)} clips are scheduled for now. They're in Ready to post.")
    return len(fresh)


def command(root):
    """What Task Scheduler should run."""
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{exe}" --remind'
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(root) / "app.py"}" --remind'


def set_enabled(on, root):
    if not util.IS_WINDOWS:
        raise RuntimeError("Posting reminders use Windows notifications.")
    if not on:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"], capture_output=True,
                       creationflags=util.NO_WINDOW)
        return "Posting reminders are off."
    p = subprocess.run(["schtasks", "/Create", "/SC", "MINUTE", "/MO", "10", "/TN", TASK,
                        "/TR", command(root), "/F"], capture_output=True, text=True,
                       creationflags=util.NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "Task Scheduler refused the task.").strip())
    return "You'll get a Windows notification when a scheduled clip is due."
