"""Version comparison and installing a zip over a source copy."""
import zipfile

import pytest

from clipper import updater


@pytest.mark.parametrize("remote, local, newer", [
    ("2026.11.14", "2026.11.13.3", True),
    ("2026.11.13.3", "2026.11.13", True),
    ("2026.12.1", "2026.11.30", True),
    ("2026.11.13", "2026.11.13", False),
    ("2026.11.9", "2026.11.13", False),
    ("", "2026.11.13", False),
])
def test_is_newer(remote, local, newer):
    assert updater.is_newer(remote, local) is newer


def make_app(folder, version, extra=()):
    (folder / "clipper").mkdir(parents=True)
    (folder / "web").mkdir()
    (folder / "app.py").write_text("print('app')")
    (folder / "VERSION").write_text(version)
    (folder / "clipper" / "core.py").write_text(f"V = '{version}'")
    for rel in extra:
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text("x")


def test_zip_update_replaces_code_and_keeps_everything_else(tmp_path):
    installed = tmp_path / "installed"
    make_app(installed, "1.0", extra=["clipper/removed_module.py", ".venv/marker", "my_notes.txt"])
    new = tmp_path / "build" / "ShootClipper"
    make_app(new, "2.0", extra=["clipper/added.py"])
    z = tmp_path / "update.zip"
    with zipfile.ZipFile(z, "w") as f:
        for p in new.rglob("*"):
            f.write(p, p.relative_to(new.parent))
    assert updater.apply_zip(z, installed) == ("1.0", "2.0")
    assert (installed / "clipper" / "core.py").read_text() == "V = '2.0'"
    assert (installed / "clipper" / "added.py").exists()
    assert not (installed / "clipper" / "removed_module.py").exists()      # stale code can't linger
    assert (installed / ".venv" / "marker").exists() and (installed / "my_notes.txt").exists()


def test_zip_that_is_not_an_update_is_refused(tmp_path):
    notzip = tmp_path / "readme.txt"
    notzip.write_text("hello")
    with pytest.raises(ValueError, match="isn't a zip"):
        updater.apply_zip(notzip, tmp_path)
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as f:
        f.writestr("photos/cat.jpg", "x")
    with pytest.raises(ValueError, match="isn't a Shoot Clipper update"):
        updater.apply_zip(other, tmp_path)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as f:
        f.writestr("../outside.txt", "x")
    with pytest.raises(ValueError):
        updater.apply_zip(evil, tmp_path / "target")
    assert not (tmp_path / "outside.txt").exists()
