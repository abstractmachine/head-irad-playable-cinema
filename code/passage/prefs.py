import json
from pathlib import Path

_PREFS_FILE = Path.home() / ".passage" / "prefs.json"


def load() -> dict:
    if _PREFS_FILE.exists():
        return json.loads(_PREFS_FILE.read_text())
    return {}


def save(data: dict) -> None:
    _PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PREFS_FILE.write_text(json.dumps(data, indent=2))


def get(key: str, default=None):
    return load().get(key, default)


def set(key: str, value) -> None:
    data = load()
    data[key] = value
    save(data)
