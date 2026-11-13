"""Settings, editable data files, and API key storage.

User data lives in %APPDATA%\\ShootClipper (Windows) so updating the app
never wipes the name list, fixes, presets or database.
"""
import json
import os
import shutil
from pathlib import Path

APP_NAME = "ShootClipper"
BUNDLED_DATA = Path(__file__).resolve().parent.parent / "data"

if os.name == "nt":
    APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
else:
    APP_DIR = Path.home() / ".shootclipper"

CONFIG_PATH = APP_DIR / "config.json"
NAMES_PATH = APP_DIR / "names.txt"
FIXES_PATH = APP_DIR / "fixes.txt"
SOURCES_PATH = APP_DIR / "sources.json"
DB_PATH = APP_DIR / "library.db"

DEFAULTS = {
    "workspace": str(Path.home() / "ShootClipper"),
    "analysis_mode": "api",            # "api" or "paste"
    "model": "claude-sonnet-5-5",
    "whisper_model": "auto",           # auto | small | medium | large-v3-turbo | base
    "device": "auto",                  # auto | cpu | cuda
    "handle": "",                      # e.g. @yourchannel, printed in the watermark bar
    "mask_swears": True,               # f**k style masking in burned-in captions
    "context_cards": False,            # your edits don't use them; on = setup card before the clip
    "post_days": [0, 2, 4],            # Mon, Wed, Fri
    "post_time": "19:30",
    "caption_color": "&H00FFFFFF",     # ASS BGR: white
    "highlight_color": "&H0000F2FF",   # ASS BGR: bright yellow, like your CapCut captions
    "font": "Poppins Black",           # bundled; Komika Axis is closer if you install it
    "font_italic": True,
    "caption_size": 116,               # measured against your exports (capitals ~50px tall)
    "caption_style": "seam",           # seam = 3 words on the split line, current word yellow; single = one big word
    "hook_text": False,
    "name_plate": True,                # lower-third with speaker + source, like your HBK clip
    "interview_zoom": 1.35,            # how tight the interview panel is in split layout
    "news_refresh_days": 2,          # how often the scout re-searches the news
    "auto_clips": 2,                 # clips Autopilot renders per interview
    "auto_min_rating": 4,
    "open_folder_when_done": True,
    "gameplay_dir": "",              # folder of your own 2K captures
    "gameplay_speed": 2.0,           # fixed speed-up applied to every 2K capture
    "theme": "system",               # system | light | dark
    "config_version": 2,
    "framing_mode": "split when matched",  # split when matched | alternate (A/B test) | face only
}

# Prices per million tokens (input, output), from Anthropic's pricing page, Nov 2026.
MODEL_PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

KEYRING_SERVICE = "ShootClipper"


def ensure_app_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("names.txt", "fixes.txt", "sources.json", "channels.txt", "blocked_channels.txt"):
        dst = APP_DIR / name
        if not dst.exists() and (BUNDLED_DATA / name).exists():
            shutil.copy(BUNDLED_DATA / name, dst)


def load():
    ensure_app_dir()
    cfg = dict(DEFAULTS)
    saved = {}
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            saved = {}
    cfg.update(saved)
    if saved and saved.get("config_version", 1) < 2:
        # Early builds saved caption_size 66, which rendered captions at about half the intended size.
        if saved.get("caption_size") == 66:
            cfg["caption_size"] = DEFAULTS["caption_size"]
        cfg["config_version"] = 2
    Path(cfg["workspace"]).mkdir(parents=True, exist_ok=True)
    return cfg


def save(cfg):
    ensure_app_dir()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── API key: Windows Credential Manager via keyring, file fallback ──────────

def get_api_key():
    try:
        import keyring
        k = keyring.get_password(KEYRING_SERVICE, "anthropic")
        if k:
            return k
    except Exception:
        pass
    p = APP_DIR / ".apikey"
    return p.read_text().strip() if p.exists() else os.environ.get("ANTHROPIC_API_KEY", "")


def set_api_key(key):
    key = key.strip()
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, "anthropic", key)
        return "Saved to Windows Credential Manager"
    except Exception:
        (APP_DIR / ".apikey").write_text(key)
        return "Saved to app folder (keyring unavailable)"


# ── Editable data files ─────────────────────────────────────────────────────

def _name_lines():
    ensure_app_dir()
    if not NAMES_PATH.exists():
        return []
    return [l.strip() for l in NAMES_PATH.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def load_names():
    """Canonical names (left of '=' when a line lists nicknames)."""
    return [l.split("=", 1)[0].strip() for l in _name_lines()]


def load_aliases():
    """{'Shawn Michaels': ['HBK', 'Heartbreak Kid'], ...} from lines like 'Shawn Michaels = HBK, Heartbreak Kid'."""
    out = {}
    for l in _name_lines():
        if "=" in l:
            name, rest = l.split("=", 1)
            out[name.strip()] = [a.strip() for a in rest.split(",") if a.strip()]
    return out


def load_fixes():
    """Lines of 'wrong => right'. Returns list of (wrong, right)."""
    ensure_app_dir()
    if not FIXES_PATH.exists():
        return []
    out = []
    for line in FIXES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        wrong, right = [x.strip() for x in line.split("=>", 1)]
        if wrong and right:
            out.append((wrong, right))
    return out


def load_sources():
    ensure_app_dir()
    if not SOURCES_PATH.exists():
        return {"None": {"bar": "none", "height": 0, "risk": "unknown"}}
    return json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
