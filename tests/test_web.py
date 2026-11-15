"""The interface files: valid JavaScript, and every backend call it makes exists."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from clipper.api import Api

WEB = Path(__file__).resolve().parent.parent / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_parses():
    code = "new Function(require('fs').readFileSync(process.argv[1], 'utf8'))"
    subprocess.run(["node", "-e", code, str(WEB / "app.js")], check=True)


def test_every_call_in_the_interface_has_a_backend_method():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    called = set(re.findall(r"""call\(\s*["']([a-z_]+)["']""", js))
    assert len(called) > 30
    missing = sorted(name for name in called if not callable(getattr(Api, name, None)))
    assert missing == []


def test_every_element_the_script_looks_up_exists():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r"""\$\(\s*["']#([A-Za-z][\w-]*)["']\s*\)""", js))
    made_in_js = set(re.findall(r"""id=\\?["']([A-Za-z][\w-]*)""", js))
    missing = sorted(i for i in ids if f'id="{i}"' not in html and i not in made_in_js)
    assert missing == []


def test_state_objects_are_only_read_through_fields_that_exist():
    """Catches typos like R.id for R.cid, which would silently be undefined."""
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for name in ("R", "S"):
        declared = re.search(rf"const {name} = \{{(.*?)\}};", js, re.S).group(1)
        fields = set(re.findall(r"(\w+)\s*:", declared))
        fields |= set(re.findall(rf"\b{name}\.(\w+)\s*=[^=]", js))          # assigned later
        used = set(re.findall(rf"(?<![\w.]){name}\.(\w+)", js))
        assert sorted(used - fields) == [], f"{name} has no such field"
