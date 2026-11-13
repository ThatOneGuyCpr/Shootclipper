"""Stopping a running job.

The Stop button sets a flag. Long steps check it (between downloads chunks, Whisper
segments and YouTube searches), and every ffmpeg call made through util.run is killed
when it's set. The job then unwinds with Cancelled, keeping whatever finished.
"""
import subprocess
import threading


class Cancelled(Exception):
    def __init__(self, msg="Stopped"):
        super().__init__(msg)


_flag = threading.Event()


def reset():
    _flag.clear()


def request():
    _flag.set()


def requested():
    return _flag.is_set()


def check():
    if _flag.is_set():
        raise Cancelled()


def communicate(p):
    """Wait for a Popen with pipes, killing it if Stop is pressed. Returns (out, err)."""
    while True:
        try:
            return p.communicate(timeout=0.4)
        except subprocess.TimeoutExpired:
            if _flag.is_set():
                p.kill()
                p.communicate()
                raise Cancelled()
