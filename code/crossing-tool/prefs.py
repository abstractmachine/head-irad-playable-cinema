import json
from pathlib import Path

# Keys that belong to the user's home-directory prefs (~/.crossing/prefs.json).
# Everything else is stored inside the active project folder.
_USER_KEYS = {"path"}

_USER_FILE = Path.home() / ".crossing" / "prefs.json"
_PROJECT_PREFS_REL = Path("preferences") / "preferences.json"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _user_load() -> dict:
    if _USER_FILE.exists():
        return json.loads(_USER_FILE.read_text())
    return {}


def _user_save(data: dict) -> None:
    _USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _USER_FILE.write_text(json.dumps(data, indent=2))


def _project_file() -> Path | None:
    path = _user_load().get("path")
    if path:
        return Path(path) / _PROJECT_PREFS_REL
    return None


def _project_load() -> dict:
    pf = _project_file()
    if pf is not None and pf.exists():
        return json.loads(pf.read_text())
    return {}


def _project_save(data: dict) -> None:
    pf = _project_file()
    if pf is None:
        return
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load() -> dict:
    """Return a merged view: user prefs overlaid with project prefs."""
    merged = _user_load()
    merged.update(_project_load())
    return merged


def save(data: dict) -> None:
    """Partition *data* between user and project prefs files."""
    user_data = {k: v for k, v in data.items() if k in _USER_KEYS}
    proj_data = {k: v for k, v in data.items() if k not in _USER_KEYS}
    if user_data:
        _user_save(user_data)
    if proj_data:
        _project_save(proj_data)


def get(key: str, default=None):
    if key in _USER_KEYS:
        return _user_load().get(key, default)
    return _project_load().get(key, default)


def set(key: str, value) -> None:
    if key in _USER_KEYS:
        data = _user_load()
        data[key] = value
        _user_save(data)
    else:
        data = _project_load()
        data[key] = value
        _project_save(data)
