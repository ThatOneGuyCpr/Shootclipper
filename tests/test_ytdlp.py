"""yt-dlp keeps itself current: fetch, verify, swap in, and fall back if a copy is bad."""
import hashlib
import io
import json
import shutil
import zipfile

import pytest

from clipper import ingest, ytdlp

# conftest switches updates off for every test. Keep the real functions to switch back on here.
REAL_UPDATE, REAL_UPDATE_IF_DUE = ytdlp.update, ytdlp.update_if_due


def wheel(version, broken=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("yt_dlp/__init__.py", "import nonexistent_module_xyz\n" if broken else
                   "from . import version\nclass YoutubeDL:\n    pass\n")
        z.writestr("yt_dlp/version.py", f"__version__ = \'{version}\'\n")
        z.writestr(f"yt_dlp-{version}.dist-info/METADATA", "Name: yt-dlp\n")
        z.writestr("../escape.py", "x = 1\n")
    return buf.getvalue()


@pytest.fixture
def pypi(monkeypatch):
    """A fake PyPI. Set state["version"]; state["tamper"] corrupts the download."""
    state = {"version": "2099.01.01", "broken": False, "tamper": False, "fetched": []}

    def get(url, timeout=30):
        state["fetched"].append(url)
        blob = wheel(state["version"], state["broken"])
        if url == ytdlp.PYPI:
            return json.dumps({"info": {"version": state["version"]}, "urls": [
                {"packagetype": "sdist", "filename": "yt_dlp.tar.gz", "url": "sdist", "digests": {"sha256": "x"}},
                {"packagetype": "bdist_wheel", "filename": f"yt_dlp-{state['version']}-py3-none-any.whl",
                 "url": "https://files.example/wheel", "digests": {"sha256": hashlib.sha256(blob).hexdigest()}}]}).encode()
        return blob + (b"tampered" if state["tamper"] else b"")
    monkeypatch.setattr(ytdlp, "_get", get)
    yield state


@pytest.fixture(autouse=True)
def real_functions_and_cleanup(monkeypatch):
    monkeypatch.setattr(ytdlp, "update", REAL_UPDATE)
    monkeypatch.setattr(ytdlp, "update_if_due", REAL_UPDATE_IF_DUE)
    yield
    shutil.rmtree(ytdlp._dir(), ignore_errors=True)
    ytdlp._module = None
    ytdlp._loaded_from = None
    ytdlp._drop_from_path()
    ytdlp._purge()


def test_newer_version_is_fetched_checked_and_used(pypi, log):
    builtin = ytdlp.version()
    assert ytdlp.update(log) == "2099.01.01"
    assert ytdlp.version() == "2099.01.01"
    assert "updated the YouTube downloader to 2099.01.01" in log.text()
    assert not (ytdlp._dir().parent / "escape.py").exists()      # nothing outside yt_dlp/ is unpacked
    assert ytdlp.update(log) is None                             # already current
    # Removing the fetched copy brings the built-in one back.
    shutil.rmtree(ytdlp._dir())
    assert ytdlp.version() == builtin


def test_tampered_download_is_rejected(pypi):
    pypi["tamper"] = True
    builtin = ytdlp.version()
    assert ytdlp.update() is None
    assert ytdlp.version() == builtin and ytdlp._fetched() == []


def test_copy_that_will_not_load_is_thrown_away(pypi):
    pypi["broken"] = True
    builtin = ytdlp.version()
    assert ytdlp.update() == "2099.01.01"                        # fetched fine...
    assert ytdlp.version() == builtin                            # ...but it can't be imported
    assert ytdlp._fetched() == []


def test_no_swap_while_a_download_is_running(pypi):
    with ytdlp.session() as running:
        before = running.version.__version__
        ytdlp.update()
        with ytdlp.session() as second:
            assert second is running                             # still the old one mid-download
    with ytdlp.session() as after:
        assert after.version.__version__ == "2099.01.01" != before


def test_daily_check_runs_once_a_day(pypi):
    assert ytdlp.update_if_due() == "2099.01.01"
    n = len(pypi["fetched"])
    pypi["version"] = "2099.02.02"
    assert ytdlp.update_if_due() is None and len(pypi["fetched"]) == n


def test_offline_is_not_an_error(monkeypatch):
    monkeypatch.setattr(ytdlp, "_get", lambda *a, **k: (_ for _ in ()).throw(OSError("no network")))
    assert ytdlp.update() is None


def test_failed_download_fetches_a_newer_ytdlp_and_retries(pypi, log, monkeypatch, tmp_path):
    calls = []

    def flaky(interview, log):
        calls.append(ytdlp.version())
        if len(calls) == 1:
            raise RuntimeError("\x1b[0;31mERROR:\x1b[0m [youtube] abc: Sign in to confirm you're not a bot")
        return tmp_path / "source.mp4", "title"
    monkeypatch.setattr(ingest, "_download", flaky)
    assert ingest.download({"workdir": str(tmp_path), "url": "u"}, log) == (tmp_path / "source.mp4", "title")
    assert calls[1] == "2099.01.01" and "trying the download again" in log.text()


def test_failed_download_with_nothing_newer_gives_a_clean_message(log, monkeypatch, tmp_path):
    monkeypatch.setattr(ytdlp, "update", lambda log=None: None)
    monkeypatch.setattr(ingest, "_download", lambda iv, log: (_ for _ in ()).throw(
        RuntimeError("\x1b[0;31mERROR:\x1b[0m [youtube] abc: Video unavailable")))
    with pytest.raises(RuntimeError) as e:
        ingest.download({"workdir": str(tmp_path), "url": "u"}, log)
    assert str(e.value) == "Download failed: [youtube] abc: Video unavailable"
